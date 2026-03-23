"""File system scanning engine using pytsk3 (The Sleuth Kit)."""

from __future__ import annotations

import logging
import struct
import time
from datetime import datetime, timezone
from typing import Callable

try:
    import pytsk3
    HAS_PYTSK3 = True
except ImportError:
    pytsk3 = None  # type: ignore[assignment]
    HAS_PYTSK3 = False

from .models import (
    DiskInfo,
    FileStatus,
    FileSystemType,
    PartitionInfo,
    RecoveredEntry,
    ScanResult,
)

logger = logging.getLogger(__name__)

# TSK file system type mapping (populated only when pytsk3 is available)
TSK_FS_MAP: dict[int, FileSystemType] = {}
if HAS_PYTSK3:
    TSK_FS_MAP = {
        pytsk3.TSK_FS_TYPE_NTFS: FileSystemType.NTFS,
        pytsk3.TSK_FS_TYPE_FAT12: FileSystemType.FAT12,
        pytsk3.TSK_FS_TYPE_FAT16: FileSystemType.FAT16,
        pytsk3.TSK_FS_TYPE_FAT32: FileSystemType.FAT32,
        pytsk3.TSK_FS_TYPE_EXFAT: FileSystemType.EXFAT,
        pytsk3.TSK_FS_TYPE_EXT2: FileSystemType.EXT2,
        pytsk3.TSK_FS_TYPE_EXT3: FileSystemType.EXT3,
        pytsk3.TSK_FS_TYPE_EXT4: FileSystemType.EXT4,
        pytsk3.TSK_FS_TYPE_HFS: FileSystemType.HFS_PLUS,
        pytsk3.TSK_FS_TYPE_ISO9660: FileSystemType.ISO9660,
        pytsk3.TSK_FS_TYPE_FFS1: FileSystemType.UFS,
        pytsk3.TSK_FS_TYPE_FFS2: FileSystemType.UFS,
    }


class ScanCancelled(Exception):
    """Raised when a scan is cancelled by the user."""


def build_tree_from_flat(flat_entries: list[RecoveredEntry]) -> list[RecoveredEntry]:
    """Rebuild a directory hierarchy from a flat list of entries with paths.

    Entries returned by _walk_directory are flat (deleted files from deep
    inside active directories have full paths like /Users/Docs/file.txt but
    no parent directory wrappers).  This function reconstructs the tree so
    the UI can display a proper folder hierarchy.
    """
    dir_nodes: dict[str, RecoveredEntry] = {}
    root_children: list[RecoveredEntry] = []

    def _ensure_dir(dir_path: str) -> RecoveredEntry:
        if dir_path in dir_nodes:
            return dir_nodes[dir_path]
        parts = dir_path.strip("/").split("/")
        current = ""
        parent_list = root_children
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current not in dir_nodes:
                node = RecoveredEntry(
                    name=part,
                    path=f"/{current}",
                    is_directory=True,
                    status=FileStatus.DELETED,
                    confidence=0.5,
                    children=[],
                )
                dir_nodes[current] = node
                parent_list.append(node)
            parent_list = dir_nodes[current].children
        return dir_nodes[dir_path]

    for entry in flat_entries:
        norm = entry.path.strip("/")
        if "/" in norm:
            parent_dir = norm.rsplit("/", 1)[0]
            parent_node = _ensure_dir(parent_dir)
            parent_node.children.append(entry)
        else:
            root_children.append(entry)

    return root_children


class DiskScanner:
    """Scans a disk partition for files and folders, including deleted ones."""

    def __init__(self):
        self._cancelled = False
        self._progress_callback: Callable[[str, int, int], None] | None = None
        self._files_found = 0
        self._deleted_found = 0
        self._entries_scanned = 0
        self._extension_filter: list[str] = []

    def set_extension_filter(self, extensions: list[str]) -> None:
        """Set file extension filter.

        Args:
            extensions: List of lowercase extensions (e.g. [".jpg", ".png"]).
                        Empty list means no filtering.
        """
        self._extension_filter = [e.lower() for e in extensions]

    def _matches_extension_filter(self, name: str) -> bool:
        """Check if a filename matches the extension filter.

        Returns True if the file should be included (matches filter or no filter set).
        """
        if not self._extension_filter:
            return True
        dot = name.rfind(".")
        if dot < 0:
            return False
        return name[dot:].lower() in self._extension_filter

    def cancel(self) -> None:
        """Request scan cancellation."""
        self._cancelled = True

    def set_progress_callback(
        self, callback: Callable[[str, int, int], None]
    ) -> None:
        """Set a callback for progress updates.

        Callback signature: (current_path, files_found, deleted_found)
        """
        self._progress_callback = callback

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise ScanCancelled("Scan cancelled by user")

    def _report_progress(self, path: str) -> None:
        if self._progress_callback:
            self._progress_callback(path, self._entries_scanned, self._deleted_found)

    def scan_partition(
        self,
        img_info: pytsk3.Img_Info,
        partition: PartitionInfo,
        disk: DiskInfo,
        target_path: str = "/",
    ) -> ScanResult:
        """Scan a partition for files and folders.

        Args:
            img_info: Open disk image from pytsk3.Img_Info
            partition: The partition to scan
            disk: Parent disk info
            target_path: Directory path to scan (default "/" = entire partition).
                         Use e.g. "/Users/Documents" to scan only that subtree.

        Returns:
            ScanResult with the complete tree of recovered entries
        """
        self._cancelled = False
        self._files_found = 0
        self._deleted_found = 0
        self._entries_scanned = 0
        start_time = time.monotonic()

        # Normalize path
        if not target_path or target_path == "/":
            target_path = "/"
        else:
            target_path = "/" + target_path.strip("/")

        logger.info(
            "Starting scan of partition %d at path '%s' (offset=%d, size=%s)",
            partition.index, target_path, partition.offset_bytes, partition.size_display,
        )

        self._report_progress(f"[Directory Scan] Opening file system...")

        # Open the file system at the partition offset
        try:
            fs_info = pytsk3.FS_Info(
                img_info,
                offset=partition.offset_bytes,
            )
        except Exception as exc:
            logger.error("Failed to open file system at offset %d: %s",
                         partition.offset_bytes, exc)
            raise

        # Detect file system type
        fs_type_id = fs_info.info.ftype
        partition.fs_type = TSK_FS_MAP.get(fs_type_id, FileSystemType.UNKNOWN)
        logger.info("Detected file system: %s", partition.fs_type.value)

        # Open the target directory
        try:
            start_dir = fs_info.open_dir(path=target_path)
        except Exception as exc:
            logger.error("Cannot open directory '%s': %s", target_path, exc)
            raise

        root_entries = self._walk_directory(fs_info, start_dir, target_path)

        self._report_progress(f"[Directory Scan] Complete — {self._entries_scanned:,} entries, {self._deleted_found:,} deleted")

        elapsed = time.monotonic() - start_time
        total_size = self._sum_sizes(root_entries)

        result = ScanResult(
            disk=disk,
            partition=partition,
            root_entries=root_entries,
            total_files=self._files_found,
            total_deleted=self._deleted_found,
            total_size_bytes=total_size,
            scan_duration_seconds=elapsed,
            target_path=target_path,
        )

        logger.info(
            "Scan complete: %d files found (%d deleted) in %.1f seconds",
            self._files_found, self._deleted_found, elapsed,
        )
        return result

    # NTFS constants for $FILE_NAME attribute parsing
    _NTFS_ROOT_INODE = 5
    _NTFS_FNAME_TYPE = 0x30  # $FILE_NAME attribute type

    def scan_unallocated(
        self,
        img_info: pytsk3.Img_Info,
        partition: PartitionInfo,
        disk: DiskInfo,
    ) -> ScanResult:
        """Scan a partition using file system metadata for deleted entries.

        This is the deep scan that looks at unallocated MFT entries,
        deleted directory entries, and orphan inodes.  For NTFS volumes
        it extracts real filenames from the $FILE_NAME attribute and
        reconstructs original directory paths.
        """
        # Don't reset counters — keep totals from the directory walk phase
        start_time = time.monotonic()

        try:
            fs_info = pytsk3.FS_Info(
                img_info,
                offset=partition.offset_bytes,
            )
        except Exception as exc:
            logger.error("Failed to open FS for unallocated scan: %s", exc)
            raise

        # Walk all inodes including unallocated
        try:
            inode_count = fs_info.info.inum_count
        except Exception:
            inode_count = 0

        # Maps inode → (best_name, parent_inode) for path reconstruction
        inode_map: dict[int, tuple[str, int]] = {}
        deleted_entries: list[RecoveredEntry] = []

        if inode_count > 0:
            logger.info("Scanning %d inodes for deleted entries...", inode_count)
            first_inum = fs_info.info.first_inum
            total_inodes = inode_count - first_inum
            self._report_progress(
                f"[Deep Scan] Scanning {total_inodes:,} inodes..."
            )

            for inode_num in range(first_inum, inode_count):
                self._check_cancelled()

                try:
                    f = fs_info.open_meta(inode=inode_num)
                except Exception:
                    continue

                if f.info.meta is None:
                    continue

                self._entries_scanned += 1

                # Extract $FILE_NAME for ALL inodes (need parents for path chains)
                fname = self._extract_filename_info(f)
                if fname:
                    inode_map[inode_num] = fname

                # Only collect deleted/unallocated entries
                flags = f.info.meta.flags
                is_unalloc = bool(flags & pytsk3.TSK_FS_META_FLAG_UNALLOC)
                if not is_unalloc:
                    continue

                # Use real name from $FILE_NAME when available
                entry_name = fname[0] if fname else f"[orphan-{inode_num}]"

                meta = f.info.meta
                is_dir = meta.type == pytsk3.TSK_FS_META_TYPE_DIR

                # Apply extension filter for non-directory files
                if not is_dir and not self._matches_extension_filter(entry_name):
                    continue

                entry = RecoveredEntry(
                    name=entry_name,
                    path="",  # filled in after path reconstruction
                    is_directory=is_dir,
                    size_bytes=meta.size if meta.size else 0,
                    status=FileStatus.DELETED,
                    confidence=self._estimate_confidence(meta, is_dir),
                    created=_tsk_timestamp(meta.crtime),
                    modified=_tsk_timestamp(meta.mtime),
                    accessed=_tsk_timestamp(meta.atime),
                    inode=inode_num,
                )
                deleted_entries.append(entry)
                self._deleted_found += 1
                self._files_found += 1

                if (inode_num - first_inum) % 2000 == 0 and total_inodes > 0:
                    pct = (inode_num - first_inum) * 100 // total_inodes
                    self._report_progress(
                        f"[Deep Scan] inode {inode_num - first_inum:,}/"
                        f"{total_inodes:,} ({pct}%) — "
                        f"{self._deleted_found:,} deleted"
                    )

        # ---- Reconstruct paths from the inode parent chain ----
        self._report_progress(
            f"[Deep Scan] Reconstructing paths for {len(deleted_entries):,} entries..."
        )
        true_orphans: list[RecoveredEntry] = []
        resolved: list[RecoveredEntry] = []

        for entry in deleted_entries:
            dir_path = self._reconstruct_path(entry.inode, inode_map)
            if dir_path and dir_path != "/":
                entry.path = f"{dir_path}/{entry.name}"
                resolved.append(entry)
            else:
                entry.path = f"/{entry.name}"
                true_orphans.append(entry)

        # Build a proper tree from entries that have reconstructed paths
        root_entries = build_tree_from_flat(resolved)

        # Wrap true orphans in a synthetic directory
        if true_orphans:
            orphan_dir = RecoveredEntry(
                name=f"Orphan Files ({len(true_orphans):,})",
                path="/[Orphan Files]",
                is_directory=True,
                status=FileStatus.DELETED,
                confidence=0.3,
                children=true_orphans,
            )
            root_entries.append(orphan_dir)

        elapsed = time.monotonic() - start_time
        total_size = self._sum_sizes(root_entries)

        return ScanResult(
            disk=disk,
            partition=partition,
            root_entries=root_entries,
            total_files=self._files_found,
            total_deleted=self._deleted_found,
            total_size_bytes=total_size,
            scan_duration_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # $FILE_NAME attribute helpers
    # ------------------------------------------------------------------

    def _extract_filename_info(
        self, file_obj
    ) -> tuple[str, int] | None:
        """Extract the best filename and parent inode from $FILE_NAME attributes.

        Returns ``(name, parent_inode)`` or ``None`` if not found.
        """
        best_name: str | None = None
        best_parent = 0
        best_prio = -1

        # Namespace priority: Win32 > Win32+DOS > POSIX > DOS
        _ns_prio = {1: 4, 3: 3, 0: 2, 2: 1}

        try:
            for attr in file_obj:
                if attr.info.type != self._NTFS_FNAME_TYPE:
                    continue
                try:
                    data = file_obj.read_random(
                        0, attr.info.size, attr.info.type, attr.info.id
                    )
                    if not data or len(data) < 68:
                        continue
                    parent_ref = struct.unpack_from("<Q", data, 0)[0]
                    parent_inode = parent_ref & 0x0000FFFFFFFFFFFF
                    name_len = data[64]
                    name_ns = data[65]
                    if name_len > 0 and len(data) >= 66 + name_len * 2:
                        name = data[66 : 66 + name_len * 2].decode("utf-16-le")
                        prio = _ns_prio.get(name_ns, 0)
                        if prio > best_prio:
                            best_name = name
                            best_parent = parent_inode
                            best_prio = prio
                except Exception:
                    continue
        except Exception:
            pass

        if best_name:
            return (best_name, best_parent)
        return None

    def _reconstruct_path(
        self, inode_num: int, inode_map: dict[int, tuple[str, int]]
    ) -> str:
        """Walk up the parent chain to reconstruct the directory path.

        Returns the directory path (excluding the file's own name),
        e.g. ``/Users/Documents``.  Returns ``/`` if unable to resolve.
        """
        info = inode_map.get(inode_num)
        if not info:
            return "/"

        parts: list[str] = []
        current = info[1]  # start from parent
        seen: set[int] = set()

        while len(parts) < 50:
            if current == self._NTFS_ROOT_INODE:
                break
            if current in seen:
                break  # cycle
            seen.add(current)
            parent_info = inode_map.get(current)
            if not parent_info:
                break
            parts.append(parent_info[0])
            current = parent_info[1]

        if not parts:
            return "/"
        parts.reverse()
        return "/" + "/".join(parts)

    def _walk_directory(
        self,
        fs_info: pytsk3.FS_Info,
        directory: pytsk3.Directory,
        path: str,
        visited: set[int] | None = None,
    ) -> list[RecoveredEntry]:
        """Recursively walk a directory tree.

        Args:
            fs_info: The file system info object
            directory: The directory to walk
            path: Current path string
            visited: Set of visited inode numbers (cycle prevention)

        Returns:
            List of RecoveredEntry objects
        """
        if visited is None:
            visited = set()

        entries: list[RecoveredEntry] = []

        for entry in directory:
            self._check_cancelled()

            name = entry.info.name.name
            if isinstance(name, bytes):
                try:
                    name = name.decode("utf-8")
                except UnicodeDecodeError:
                    name = name.decode("latin-1", errors="replace")

            # Skip . and .. and special TSK entries
            if name in (".", "..", "$OrphanFiles"):
                continue

            # Skip system metadata files (NTFS $MFT etc.) at root unless deleted
            if (
                path == "/"
                and name.startswith("$")
                and entry.info.meta
                and not (entry.info.meta.flags & pytsk3.TSK_FS_META_FLAG_UNALLOC)
            ):
                continue

            meta = entry.info.meta
            is_dir = (
                entry.info.name.type == pytsk3.TSK_FS_NAME_TYPE_DIR
                if entry.info.name.type is not None
                else (meta.type == pytsk3.TSK_FS_META_TYPE_DIR if meta else False)
            )

            full_path = f"{path}{name}" if path == "/" else f"{path}/{name}"

            # Determine deleted status
            is_deleted = False
            if meta:
                flags = meta.flags
                is_unalloc = bool(flags & pytsk3.TSK_FS_META_FLAG_UNALLOC)
                name_flags = entry.info.name.flags
                name_unalloc = bool(name_flags & pytsk3.TSK_FS_NAME_FLAG_UNALLOC)
                is_deleted = is_unalloc or name_unalloc

            self._entries_scanned += 1
            # Report progress every 200 entries so the UI stays alive
            if self._entries_scanned % 200 == 0:
                self._report_progress(
                    f"[Directory Scan] {full_path}"
                )

            # For active directories: recurse to find deleted children.
            # Include the directory as a tree container if it has any
            # deleted descendants, so users can browse the full structure.
            if is_dir and not is_deleted:
                if meta and meta.addr and meta.addr not in visited:
                    visited.add(meta.addr)
                    try:
                        sub_dir = fs_info.open_dir(inode=meta.addr)
                        child_entries = self._walk_directory(
                            fs_info, sub_dir, full_path, visited
                        )
                        if child_entries:
                            dir_node = RecoveredEntry(
                                name=name,
                                path=full_path,
                                is_directory=True,
                                status=FileStatus.INTACT,
                                confidence=1.0,
                                children=child_entries,
                            )
                            entries.append(dir_node)
                    except Exception as exc:
                        logger.debug("Cannot open dir %s: %s", full_path, exc)
                continue

            # Skip active non-directory files entirely
            if not is_deleted:
                continue

            # Apply extension filter for non-directory files
            if not is_dir and not self._matches_extension_filter(name):
                continue

            recovered = RecoveredEntry(
                name=name,
                path=full_path,
                is_directory=is_dir,
            )

            if meta:
                recovered.size_bytes = meta.size if meta.size else 0
                recovered.inode = meta.addr if meta.addr else 0
                recovered.created = _tsk_timestamp(meta.crtime)
                recovered.modified = _tsk_timestamp(meta.mtime)
                recovered.accessed = _tsk_timestamp(meta.atime)
                recovered.status = FileStatus.DELETED
                recovered.confidence = self._estimate_confidence(meta, is_dir)

            self._files_found += 1
            self._deleted_found += 1
            # Always report immediately when a deleted file is found
            self._report_progress(
                f"[Directory Scan] {full_path}"
            )

            # Recurse into deleted directories
            if is_dir and meta and meta.addr and meta.addr not in visited:
                visited.add(meta.addr)
                try:
                    sub_dir = fs_info.open_dir(inode=meta.addr)
                    recovered.children = self._walk_directory(
                        fs_info, sub_dir, full_path, visited
                    )
                except Exception as exc:
                    logger.debug("Cannot open dir %s: %s", full_path, exc)

            entries.append(recovered)

        return entries

    def _meta_to_entry(
        self, file_obj, name: str, path: str
    ) -> RecoveredEntry | None:
        """Convert a pytsk3 file object to a RecoveredEntry."""
        meta = file_obj.info.meta
        if meta is None:
            return None

        is_dir = meta.type == pytsk3.TSK_FS_META_TYPE_DIR

        return RecoveredEntry(
            name=name,
            path=f"{path}/{name}" if path != "/" else f"/{name}",
            is_directory=is_dir,
            size_bytes=meta.size if meta.size else 0,
            status=FileStatus.DELETED,
            confidence=self._estimate_confidence(meta, is_dir),
            created=_tsk_timestamp(meta.crtime),
            modified=_tsk_timestamp(meta.mtime),
            accessed=_tsk_timestamp(meta.atime),
            inode=meta.addr if meta.addr else 0,
        )

    def _estimate_confidence(self, meta, is_dir: bool) -> float:
        """Estimate recovery confidence for a deleted entry."""
        if is_dir:
            return 0.9  # Directories are usually recoverable from metadata

        if meta.size == 0:
            return 0.1  # No data to recover

        # Check if data runs are intact
        # Higher confidence for smaller files (less likely to be overwritten)
        if meta.size < 1024 * 1024:  # < 1MB
            return 0.8
        elif meta.size < 100 * 1024 * 1024:  # < 100MB
            return 0.6
        else:
            return 0.4

    def _sum_sizes(self, entries: list[RecoveredEntry]) -> int:
        """Sum the sizes of all entries recursively."""
        total = 0
        for e in entries:
            total += e.size_bytes
            if e.children:
                total += self._sum_sizes(e.children)
        return total


def _tsk_timestamp(ts) -> datetime | None:
    """Convert a TSK timestamp to a datetime, or None if invalid."""
    if ts is None or ts == 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
