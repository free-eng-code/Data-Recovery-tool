"""Windows-native file system scanner (no pytsk3 dependency).

Scans NTFS/FAT volumes using:
- os.scandir for recursive file tree walking
- Raw MFT parsing for deleted file detection (NTFS)
- $Recycle.Bin parsing for user-deleted files with original paths
- NTFS USN change journal for recently deleted file detection
"""

from __future__ import annotations

import logging
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import (
    DiskInfo,
    FileStatus,
    PartitionInfo,
    RecoveredEntry,
    ScanResult,
)

logger = logging.getLogger(__name__)

# Windows IOCTLs
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

# USN reason flags
USN_REASON_FILE_DELETE = 0x00000200

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3


class ScanCancelled(Exception):
    """Raised when a scan is cancelled."""


class WindowsScanner:
    """Scans Windows volumes using native OS APIs."""

    def __init__(self):
        self._cancelled = False
        self._progress_callback: Callable[[str, int, int], None] | None = None
        self._files_found = 0
        self._deleted_found = 0

    def cancel(self):
        self._cancelled = True

    def set_progress_callback(
        self, callback: Callable[[str, int, int], None]
    ) -> None:
        self._progress_callback = callback

    def _check_cancelled(self):
        if self._cancelled:
            raise ScanCancelled()

    def _report_progress(self, path: str):
        if self._progress_callback:
            self._progress_callback(path, self._files_found, self._deleted_found)

    def scan_volume(
        self,
        drive_letter: str,
        disk: DiskInfo,
        partition: PartitionInfo,
        target_path: str = "/",
    ) -> ScanResult:
        """Scan a Windows volume like Disk Drill / EaseUS.

        Comprehensive scan that finds BOTH existing and deleted files,
        then adds carved files from unallocated space.

        Scanning phases:
        1. Parse raw MFT for ALL file entries (active + deleted) with full paths
        2. Parse $Recycle.Bin for user-deleted files with original paths
        3. Read USN journal for recently deleted files
        4. Carve unallocated space for files with lost metadata ("Reconstructed")

        Args:
            drive_letter: e.g. "C:" or "D:"
            disk: Parent disk info
            partition: Partition info
            target_path: Sub-directory to scope results ("/" for entire volume)
        """
        self._cancelled = False
        self._files_found = 0
        self._deleted_found = 0
        start_time = time.monotonic()

        logger.info("Scanning volume %s (scope: %s)", drive_letter, target_path)

        # Collect ALL entries with their original full paths
        # Each entry is (original_path_from_root, RecoveredEntry)
        all_entries: list[tuple[str, RecoveredEntry]] = []
        # Volume geometry (set by MFT phase if available)
        self._vol_bytes_per_cluster = 4096
        self._vol_total_clusters = 0

        # Phase 1: Parse raw MFT for ALL files (needs admin / raw read)
        try:
            self._report_progress("[MFT] Starting deep scan...")
            mft_results = self._scan_mft_all(drive_letter)
            all_entries.extend(mft_results)
            self._files_found = len(all_entries)
            self._deleted_found = sum(1 for _, e in all_entries if e.status != FileStatus.INTACT)
            self._report_progress(f"[MFT] {self._files_found} entries found")
            logger.info("MFT scan found %d entries", len(mft_results))
        except ScanCancelled:
            raise
        except Exception as exc:
            logger.warning("MFT scan failed (may need admin): %s", exc)

        self._check_cancelled()

        # Read $Bitmap to identify unallocated clusters for carving
        self._bitmap_data: bytes | None = None
        try:
            if self._vol_total_clusters > 0:
                self._report_progress("[Bitmap] Reading cluster allocation map...")
                self._bitmap_data = self._read_bitmap(drive_letter)
                if self._bitmap_data:
                    logger.info("Bitmap loaded: %d bytes", len(self._bitmap_data))
        except ScanCancelled:
            raise
        except Exception as exc:
            logger.warning("Bitmap read failed: %s", exc)

        self._check_cancelled()

        # Phase 2: Parse $Recycle.Bin for user-deleted files
        try:
            self._report_progress("[Recycle Bin] Scanning...")
            recycle_results = self._scan_recycle_bin_flat(drive_letter)
            known_paths = {path.lower() for path, _ in all_entries}
            added = 0
            for path, entry in recycle_results:
                if path.lower() not in known_paths:
                    all_entries.append((path, entry))
                    added += 1
            self._files_found += added
            self._deleted_found += added
            self._report_progress(f"[Recycle Bin] {added} entries added")
            logger.info("Recycle Bin added %d unique entries", added)
        except ScanCancelled:
            raise
        except Exception as exc:
            logger.warning("Recycle Bin scan failed: %s", exc)

        self._check_cancelled()

        # Phase 3: USN journal for recently deleted
        try:
            self._report_progress("[USN Journal] Scanning...")
            usn_results = self._scan_usn_flat(drive_letter)
            existing_names = {e.name.lower() for _, e in all_entries}
            added = 0
            for path, entry in usn_results:
                if entry.name.lower() not in existing_names:
                    all_entries.append((path, entry))
                    added += 1
            self._files_found += added
            self._deleted_found += added
            self._report_progress(f"[USN] {added} entries added")
            logger.info("USN journal added %d unique entries", added)
        except ScanCancelled:
            raise
        except Exception as exc:
            logger.warning("USN journal scan failed: %s", exc)

        self._check_cancelled()

        # Phase 4: File carving from unallocated space
        try:
            self._report_progress("[Carving] Scanning unallocated space...")
            from .carver import FileCarver
            carver = FileCarver()
            # Wrap progress callback so carved counts ADD to accumulated totals
            base_files = self._files_found
            base_deleted = self._deleted_found

            def _carver_progress(msg: str, carved: int, _deleted: int) -> None:
                self._files_found = base_files + carved
                self._deleted_found = base_deleted + carved
                self._report_progress(msg)

            carver.set_progress_callback(_carver_progress)
            # Share cancellation: carver checks parent scanner's flag
            carver._parent_scanner = self

            # Apply scan size limit if set
            carve_total = self._vol_total_clusters
            limit_gb = getattr(self, '_scan_size_limit_gb', 0)
            if limit_gb > 0 and self._vol_bytes_per_cluster > 0:
                limit_bytes = limit_gb * (1024 ** 3)
                limit_clusters = limit_bytes // self._vol_bytes_per_cluster
                if limit_clusters < carve_total:
                    carve_total = limit_clusters

            carved_results = carver.carve_volume(
                drive_letter,
                bytes_per_cluster=self._vol_bytes_per_cluster,
                total_clusters=carve_total,
                bitmap_data=self._bitmap_data,
            )
            all_entries.extend(carved_results)
            self._files_found += len(carved_results)
            self._deleted_found += len(carved_results)
            self._report_progress(f"[Carving] {len(carved_results)} files carved")
            logger.info("Carving found %d files", len(carved_results))
        except ScanCancelled:
            raise
        except Exception as exc:
            logger.warning("File carving failed: %s", exc)

        # Filter by target_path scope
        if target_path and target_path != "/":
            scope = target_path.strip("/").replace("\\", "/").lower()
            all_entries = [
                (p, e) for p, e in all_entries
                if p.lower().startswith(scope)
            ]

        # Separate entries with unresolvable paths ("?" prefix)
        resolved: list[tuple[str, RecoveredEntry]] = []
        lost_path: list[tuple[str, RecoveredEntry]] = []
        for path, entry in all_entries:
            if path.startswith("?/") or path == "?":
                lost_path.append((path, entry))
            else:
                resolved.append((path, entry))

        # Build directory tree from resolved paths
        self._report_progress("Building directory tree...")
        root_entries = self._build_tree_from_paths(resolved)

        # Add "File Path Lost" virtual folder (like Disk Drill / EaseUS)
        if lost_path:
            lost_folder = RecoveredEntry(
                name=f"File Path Lost ({len(lost_path)})",
                path="/File Path Lost",
                is_directory=True,
                status=FileStatus.DELETED,
                confidence=0.3,
                children=[],
            )
            for _path, entry in lost_path:
                entry.path = f"/File Path Lost/{entry.name}"
                lost_folder.children.append(entry)
            root_entries.append(lost_folder)

        # Add "Reconstructed" folder for carved files (like EaseUS)
        carved = [
            (p, e) for p, e in all_entries
            if p.startswith("Reconstructed/")
        ]
        if carved:
            reconstructed = RecoveredEntry(
                name=f"Reconstructed ({self._format_count(len(carved))})",
                path="/Reconstructed",
                is_directory=True,
                status=FileStatus.PARTIAL,
                confidence=0.5,
                children=[e for _, e in carved],
            )
            root_entries.append(reconstructed)

        # Add file type category virtual folders (like Disk Drill sidebar)
        type_categories = self._categorize_by_type(all_entries)
        for cat_name, cat_entries in type_categories.items():
            if cat_entries:
                cat_folder = RecoveredEntry(
                    name=cat_name,
                    path=f"/[{cat_name}]",
                    is_directory=True,
                    status=FileStatus.DELETED,
                    confidence=0.5,
                    children=cat_entries,
                )
                root_entries.append(cat_folder)

        # Count totals
        self._files_found = len(all_entries)
        self._deleted_found = sum(
            1 for _, e in all_entries
            if e.status in (FileStatus.DELETED, FileStatus.PARTIAL, FileStatus.METADATA_ONLY)
        )

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
            "Scan complete: %d total files, %d deleted in %.1fs",
            self._files_found, self._deleted_found, elapsed,
        )
        return result

    # File extension → category mapping (like Disk Drill sidebar)
    _TYPE_MAP: dict[str, str] = {}
    _CATEGORIES = {
        "Pictures": {
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
            ".webp", ".svg", ".ico", ".raw", ".cr2", ".nef", ".psd",
            ".heic", ".heif", ".avif",
        },
        "Videos": {
            ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm",
            ".m4v", ".mpg", ".mpeg", ".3gp", ".vob",
        },
        "Audio": {
            ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
            ".opus", ".aiff", ".mid", ".midi",
        },
        "Documents": {
            ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
            ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv", ".md",
            ".html", ".htm", ".xml", ".yaml", ".yml",
        },
        "Archives": {
            ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
            ".iso", ".cab", ".msi",
        },
        "Code": {
            # C# / .NET
            ".cs", ".csproj", ".sln", ".razor", ".cshtml", ".vb",
            ".fsproj", ".fs", ".xaml", ".resx", ".config", ".nuspec",
            # JavaScript / TypeScript / Node.js
            ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts",
            # Web (CSS / HTML / Templating)
            ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
            ".astro", ".ejs", ".hbs", ".pug",
            # Config / Build / Package
            ".json", ".jsonc", ".env", ".toml", ".ini",
            ".editorconfig", ".prettierrc", ".eslintrc",
            ".babelrc", ".npmrc", ".nvmrc",
            # Python
            ".py", ".pyw", ".pyi", ".pyx", ".pxd", ".pyd",
            ".ipynb", ".pyc", ".pyo", ".pyz", ".pywz",
            ".egg", ".whl", ".cfg", ".setup.cfg",
            ".pyproject", ".pylintrc", ".flake8", ".mypy.ini",
            ".python-version", ".pipfile",
            # Java / Kotlin
            ".java", ".kt", ".kts", ".gradle",
            # C / C++ / Rust / Go
            ".c", ".h", ".cpp", ".hpp", ".cc", ".hh",
            ".rs", ".go", ".zig",
            # Ruby / PHP / Lua / Shell
            ".rb", ".php", ".lua", ".sh", ".bash", ".ps1", ".psm1",
            # SQL / Database
            ".sql", ".prisma", ".graphql", ".gql",
            # Markup / Docs
            ".mdx", ".rst", ".tex", ".latex",
            # Docker / CI / IaC
            ".dockerfile", ".dockerignore", ".tf", ".tfvars",
            # Misc dev files
            ".gitignore", ".gitattributes", ".gitmodules",
            ".lock", ".log", ".map", ".d.ts",
        },
    }
    # Build reverse lookup
    for _cat, _exts in _CATEGORIES.items():
        for _ext in _exts:
            _TYPE_MAP[_ext] = _cat

    def _categorize_by_type(
        self, flat_entries: list[tuple[str, RecoveredEntry]]
    ) -> dict[str, list[RecoveredEntry]]:
        """Group file entries by type category (Pictures, Videos, etc.).

        Like Disk Drill's left sidebar categories.
        Returns dict of category_name -> list of RecoveredEntry (files only).
        """
        categories: dict[str, list[RecoveredEntry]] = {
            f"Pictures": [],
            f"Videos": [],
            f"Audio": [],
            f"Documents": [],
            f"Archives": [],
            f"Code": [],
        }

        for _path, entry in flat_entries:
            if entry.is_directory:
                continue
            ext = ""
            dot_idx = entry.name.rfind(".")
            if dot_idx >= 0:
                ext = entry.name[dot_idx:].lower()
            cat = self._TYPE_MAP.get(ext)
            if cat:
                # Create a copy so the entry can belong to both trees
                cat_entry = RecoveredEntry(
                    name=entry.name,
                    path=f"/[{cat}]/{entry.name}",
                    is_directory=False,
                    size_bytes=entry.size_bytes,
                    status=entry.status,
                    confidence=entry.confidence,
                    created=entry.created,
                    modified=entry.modified,
                    accessed=entry.accessed,
                    inode=entry.inode,
                )
                categories[cat].append(cat_entry)

        # Add counts to category names
        result: dict[str, list[RecoveredEntry]] = {}
        for cat, entries in categories.items():
            if entries:
                label = f"{cat} ({self._format_count(len(entries))})"
                result[label] = entries
        return result

    @staticmethod
    def _format_count(n: int) -> str:
        """Format a count like Disk Drill: 1.6K, 147.5K, 1.9M."""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    def _build_tree_from_paths(
        self, flat_entries: list[tuple[str, RecoveredEntry]]
    ) -> list[RecoveredEntry]:
        """Build a directory tree from flat (path, entry) pairs.

        Reconstructs the original directory hierarchy, like Disk Drill.
        Each path is e.g. "Users/ABC/Documents/file.txt"
        """
        # dir_path -> RecoveredEntry (directory node)
        dir_nodes: dict[str, RecoveredEntry] = {}
        root_children: list[RecoveredEntry] = []

        def _ensure_dir(dir_path: str) -> RecoveredEntry:
            """Get or create directory nodes along the path."""
            if dir_path in dir_nodes:
                return dir_nodes[dir_path]

            parts = dir_path.split("/")
            current = ""
            parent_list = root_children

            for part in parts:
                current = f"{current}/{part}" if current else part
                if current not in dir_nodes:
                    vpath = f"/{current}"
                    node = RecoveredEntry(
                        name=part,
                        path=vpath,
                        is_directory=True,
                        status=FileStatus.DELETED,
                        confidence=0.5,
                        children=[],
                    )
                    dir_nodes[current] = node
                    parent_list.append(node)

                parent_list = dir_nodes[current].children

            return dir_nodes[dir_path]

        for full_path, entry in flat_entries:
            self._check_cancelled()
            norm_path = full_path.replace("\\", "/").strip("/")

            if "/" in norm_path:
                parent_dir = norm_path.rsplit("/", 1)[0]
                parent_node = _ensure_dir(parent_dir)
                entry.path = f"/{norm_path}"
                parent_node.children.append(entry)
            else:
                entry.path = f"/{norm_path}" if norm_path else f"/{entry.name}"
                root_children.append(entry)

        return root_children

    def _walk_directory(
        self, dir_path: str, virtual_path: str
    ) -> list[RecoveredEntry]:
        """Recursively walk a directory using os.scandir."""
        entries: list[RecoveredEntry] = []

        try:
            with os.scandir(dir_path) as it:
                for item in it:
                    self._check_cancelled()

                    try:
                        name = item.name
                    except (OSError, FileNotFoundError):
                        continue

                    vpath = (
                        f"/{name}" if virtual_path == "/"
                        else f"{virtual_path}/{name}"
                    )

                    try:
                        is_dir = item.is_dir(follow_symlinks=False)
                    except (PermissionError, OSError):
                        is_dir = False

                    try:
                        stat = item.stat(follow_symlinks=False)
                    except (PermissionError, OSError):
                        entry = RecoveredEntry(
                            name=name,
                            path=vpath,
                            is_directory=is_dir,
                            status=FileStatus.METADATA_ONLY,
                            confidence=0.0,
                        )
                        entries.append(entry)
                        self._files_found += 1
                        continue

                    entry = RecoveredEntry(
                        name=name,
                        path=vpath,
                        is_directory=is_dir,
                        size_bytes=stat.st_size if not is_dir else 0,
                        status=FileStatus.INTACT,
                        confidence=1.0,
                        created=_safe_datetime(stat.st_ctime),
                        modified=_safe_datetime(stat.st_mtime),
                        accessed=_safe_datetime(stat.st_atime),
                    )

                    self._files_found += 1
                    if self._files_found % 500 == 0:
                        self._report_progress(vpath)

                    if is_dir:
                        try:
                            entry.children = self._walk_directory(
                                item.path, vpath
                            )
                        except PermissionError:
                            logger.debug("Access denied: %s", item.path)
                        except FileNotFoundError:
                            logger.debug("Vanished during scan: %s", item.path)
                        except OSError as exc:
                            logger.debug("OS error walking %s: %s", item.path, exc)

                    entries.append(entry)
        except PermissionError:
            logger.debug("Access denied listing: %s", dir_path)
        except FileNotFoundError:
            logger.debug("Directory vanished during scan: %s", dir_path)
        except OSError as exc:
            logger.debug("OS error scanning %s: %s", dir_path, exc)

        return entries

    def _scan_usn_flat(
        self, drive_letter: str
    ) -> list[tuple[str, RecoveredEntry]]:
        """Read NTFS USN journal for recently deleted files.
        
        Returns flat list of (path, entry) tuples.
        USN records only have filename (no full path), so path = filename.
        """
        import win32file

        volume = f"\\\\.\\{drive_letter}"
        handle = win32file.CreateFile(
            volume,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )

        deleted_entries: list[tuple[str, RecoveredEntry]] = []

        try:
            # Query journal info
            journal_data = win32file.DeviceIoControl(
                handle, FSCTL_QUERY_USN_JOURNAL, None, 64
            )
            if len(journal_data) < 24:
                return []

            journal_id = struct.unpack_from("<Q", journal_data, 0)[0]
            first_usn = struct.unpack_from("<q", journal_data, 8)[0]
            next_usn = struct.unpack_from("<q", journal_data, 16)[0]

            # Read last 256 MB of journal for delete events (covers weeks of history)
            start_usn = max(first_usn, next_usn - 256 * 1024 * 1024)

            seen: set[str] = set()
            current_usn = start_usn
            READ_CHUNK = 1024 * 1024  # 1 MB per IOCTL call

            while current_usn < next_usn:
                self._check_cancelled()

                read_data = struct.pack(
                    "<qIIQQQ",
                    current_usn,
                    USN_REASON_FILE_DELETE,
                    0,  # ReturnOnlyOnClose
                    0,  # Timeout
                    0,  # BytesToWaitFor
                    journal_id,
                )

                try:
                    result = win32file.DeviceIoControl(
                        handle, FSCTL_READ_USN_JOURNAL, read_data, READ_CHUNK
                    )
                except Exception:
                    break

                if len(result) <= 8:
                    break

                # First 8 bytes = NextUsn
                new_usn = struct.unpack_from("<q", result, 0)[0]
                if new_usn <= current_usn:
                    break  # No progress
                current_usn = new_usn

                # Parse USN_RECORD_V2 entries
                offset = 8

                while offset + 60 < len(result):
                    record_len = struct.unpack_from("<I", result, offset)[0]
                    if record_len == 0 or offset + record_len > len(result):
                        break

                    major = struct.unpack_from("<H", result, offset + 4)[0]
                    if major != 2 or record_len < 60:
                        offset += max(record_len, 8)
                        continue

                    file_ref = struct.unpack_from("<Q", result, offset + 8)[0]
                    timestamp = struct.unpack_from("<Q", result, offset + 32)[0]
                    file_attrs = struct.unpack_from("<I", result, offset + 52)[0]
                    name_len = struct.unpack_from("<H", result, offset + 56)[0]
                    name_off = struct.unpack_from("<H", result, offset + 58)[0]

                    if name_off + name_len <= record_len:
                        try:
                            name = result[
                                offset + name_off : offset + name_off + name_len
                            ].decode("utf-16-le")
                        except Exception:
                            name = None

                        if name and not name.startswith("$") and name not in seen:
                            seen.add(name)
                            is_dir = bool(file_attrs & 0x10)
                            dt = _filetime_to_datetime(timestamp)

                            entry = RecoveredEntry(
                                name=name,
                                path=f"/[Recently Deleted]/{name}",
                                is_directory=is_dir,
                                status=FileStatus.DELETED,
                                confidence=0.3,
                                modified=dt,
                                inode=file_ref & 0xFFFFFFFFFFFF,
                            )
                            # USN only has filename, no directory info
                            deleted_entries.append((name, entry))

                    offset += record_len

        finally:
            win32file.CloseHandle(handle)

        logger.info(
            "Found %d recently deleted files in USN journal",
            len(deleted_entries),
        )
        return deleted_entries

    def _count_entries(self, entries: list[RecoveredEntry]) -> int:
        """Count total entries including nested children."""
        count = 0
        for e in entries:
            count += 1
            if e.children:
                count += self._count_entries(e.children)
        return count

    def _scan_recycle_bin_flat(
        self, drive_letter: str
    ) -> list[tuple[str, RecoveredEntry]]:
        """Parse $Recycle.Bin and return flat (original_path, entry) tuples.

        Reads $I metadata files to recover original paths, sizes, and
        deletion timestamps. Walks directories inside recycle bin to
        recover their contents too.
        """
        recycle_root = Path(f"{drive_letter}\\$Recycle.Bin")
        if not recycle_root.exists():
            return []

        results: list[tuple[str, RecoveredEntry]] = []
        seen_keys: set[str] = set()

        try:
            sid_dirs = list(recycle_root.iterdir())
        except PermissionError:
            logger.debug("Access denied to $Recycle.Bin root")
            return []

        for sid_dir in sid_dirs:
            if not sid_dir.is_dir():
                continue
            try:
                items = list(sid_dir.iterdir())
            except PermissionError:
                continue

            for item in items:
                self._check_cancelled()
                name = item.name

                if not name.upper().startswith("$I"):
                    continue

                r_name = "$R" + name[2:]
                r_path = sid_dir / r_name

                try:
                    info = self._parse_recycle_info(item)
                except Exception:
                    continue
                if not info:
                    continue

                original_path, file_size, deletion_time = info
                original_name = Path(original_path).name

                key = f"{original_path}|{file_size}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                is_dir = r_path.is_dir() if r_path.exists() else False

                if r_path.exists():
                    confidence = 0.9
                else:
                    confidence = 0.3

                # Strip drive letter prefix (e.g. "C:\Users\..." -> "Users/...")
                rel_path = original_path
                if len(rel_path) >= 3 and rel_path[1] == ":":
                    rel_path = rel_path[3:]
                rel_path = rel_path.replace("\\", "/")

                entry = RecoveredEntry(
                    name=original_name,
                    path=f"/{rel_path}",
                    is_directory=is_dir,
                    size_bytes=file_size,
                    status=FileStatus.DELETED,
                    confidence=confidence,
                    modified=deletion_time,
                )
                entry._recycle_path = str(r_path)  # type: ignore[attr-defined]

                results.append((rel_path, entry))

                # If directory still exists in recycle bin, flatten its contents
                if is_dir and r_path.exists():
                    self._flatten_recycle_dir(
                        r_path, rel_path, results
                    )

        return results

    def _flatten_recycle_dir(
        self,
        dir_path: Path,
        base_path: str,
        out: list[tuple[str, RecoveredEntry]],
    ) -> None:
        """Recursively flatten a recycle bin directory into (path, entry) tuples."""
        try:
            for item in dir_path.iterdir():
                self._check_cancelled()
                name = item.name
                rel = f"{base_path}/{name}"
                is_dir = item.is_dir()

                try:
                    stat = item.stat()
                    entry = RecoveredEntry(
                        name=name,
                        path=f"/{rel}",
                        is_directory=is_dir,
                        size_bytes=stat.st_size if not is_dir else 0,
                        status=FileStatus.DELETED,
                        confidence=0.9,
                        created=_safe_datetime(stat.st_ctime),
                        modified=_safe_datetime(stat.st_mtime),
                    )
                except (PermissionError, OSError):
                    entry = RecoveredEntry(
                        name=name,
                        path=f"/{rel}",
                        is_directory=is_dir,
                        status=FileStatus.DELETED,
                        confidence=0.3,
                    )

                out.append((rel, entry))

                if is_dir:
                    self._flatten_recycle_dir(item, rel, out)
        except (PermissionError, OSError):
            pass

    def _scan_recycle_bin(
        self, drive_letter: str
    ) -> list[RecoveredEntry]:
        """Parse $Recycle.Bin for deleted files with original paths.

        Works without admin by reading the current user's recycle bin folder.
        Parses $I files to get original path, size, and deletion time.
        """
        recycle_root = Path(f"{drive_letter}\\$Recycle.Bin")
        if not recycle_root.exists():
            return []

        deleted_entries: list[RecoveredEntry] = []
        seen_names: set[str] = set()

        # Walk all SID subdirectories we can access
        try:
            sid_dirs = list(recycle_root.iterdir())
        except PermissionError:
            logger.debug("Access denied to $Recycle.Bin root")
            return []

        for sid_dir in sid_dirs:
            if not sid_dir.is_dir():
                continue
            try:
                items = list(sid_dir.iterdir())
            except PermissionError:
                continue

            for item in items:
                self._check_cancelled()
                name = item.name

                # $I files contain metadata about deleted items
                if not name.upper().startswith("$I"):
                    continue

                # Corresponding $R file has the actual data
                r_name = "$R" + name[2:]
                r_path = sid_dir / r_name

                try:
                    info = self._parse_recycle_info(item)
                except Exception:
                    continue

                if not info:
                    continue

                original_path, file_size, deletion_time = info
                original_name = Path(original_path).name

                # Deduplicate
                key = f"{original_path}|{file_size}"
                if key in seen_names:
                    continue
                seen_names.add(key)

                is_dir = r_path.is_dir() if r_path.exists() else False

                # Determine status
                if r_path.exists():
                    status = FileStatus.DELETED  # Data still in recycle bin
                    confidence = 0.9
                else:
                    status = FileStatus.DELETED
                    confidence = 0.3  # $R file gone, only metadata

                entry = RecoveredEntry(
                    name=original_name,
                    path=f"/[Recycle Bin - Deleted Files]/{original_name}",
                    is_directory=is_dir,
                    size_bytes=file_size,
                    status=status,
                    confidence=confidence,
                    modified=deletion_time,
                )

                # Store the real path in data_runs so recovery can find it
                # We encode the recycle bin $R path as a string via inode
                entry._recycle_path = str(r_path)  # type: ignore[attr-defined]

                # If it's a directory in recycle bin, walk it
                if is_dir and r_path.exists():
                    try:
                        entry.children = self._walk_recycle_dir(
                            r_path, entry.path, original_path
                        )
                    except (PermissionError, OSError):
                        pass

                deleted_entries.append(entry)

        return deleted_entries

    def _parse_recycle_info(
        self, info_path: Path
    ) -> tuple[str, int, datetime | None] | None:
        """Parse a $Recycle.Bin $I file.

        Returns (original_path, file_size, deletion_time) or None.
        """
        try:
            data = info_path.read_bytes()
        except (PermissionError, OSError):
            return None

        if len(data) < 28:
            return None

        # $I file format:
        # Version 1 (Win Vista/7): header_version=1, 8-byte size, 8-byte timestamp, then path as UTF-16
        # Version 2 (Win 10+): header_version=2, 8-byte size, 8-byte timestamp, 4-byte path_len, then path as UTF-16
        version = struct.unpack_from("<Q", data, 0)[0]
        file_size = struct.unpack_from("<Q", data, 8)[0]
        deletion_filetime = struct.unpack_from("<Q", data, 16)[0]

        deletion_time = _filetime_to_datetime(deletion_filetime)

        if version == 2 and len(data) >= 28:
            # Version 2: path length in characters at offset 24
            path_len_chars = struct.unpack_from("<I", data, 24)[0]
            path_start = 28
            path_bytes = path_len_chars * 2
            if path_start + path_bytes <= len(data):
                try:
                    original_path = data[path_start:path_start + path_bytes].decode(
                        "utf-16-le"
                    ).rstrip("\x00")
                except Exception:
                    return None
            else:
                return None
        elif version == 1:
            # Version 1: path at offset 24, fixed 520 bytes (260 chars UTF-16)
            path_start = 24
            try:
                original_path = data[path_start:path_start + 520].decode(
                    "utf-16-le"
                ).rstrip("\x00")
            except Exception:
                return None
        else:
            return None

        if not original_path:
            return None

        return (original_path, file_size, deletion_time)

    def _walk_recycle_dir(
        self, dir_path: Path, virtual_base: str, original_base: str
    ) -> list[RecoveredEntry]:
        """Walk a directory inside the recycle bin to show its tree."""
        entries: list[RecoveredEntry] = []
        try:
            for item in dir_path.iterdir():
                self._check_cancelled()
                name = item.name
                vpath = f"{virtual_base}/{name}"
                is_dir = item.is_dir()

                try:
                    stat = item.stat()
                    entry = RecoveredEntry(
                        name=name,
                        path=vpath,
                        is_directory=is_dir,
                        size_bytes=stat.st_size if not is_dir else 0,
                        status=FileStatus.DELETED,
                        confidence=0.9,
                        created=_safe_datetime(stat.st_ctime),
                        modified=_safe_datetime(stat.st_mtime),
                    )
                except (PermissionError, OSError):
                    entry = RecoveredEntry(
                        name=name,
                        path=vpath,
                        is_directory=is_dir,
                        status=FileStatus.DELETED,
                        confidence=0.3,
                    )

                if is_dir:
                    try:
                        entry.children = self._walk_recycle_dir(
                            item, vpath, f"{original_base}\\{name}"
                        )
                    except (PermissionError, OSError):
                        pass

                entries.append(entry)
        except (PermissionError, OSError):
            pass
        return entries

    def _scan_mft_all(
        self, drive_letter: str
    ) -> list[tuple[str, RecoveredEntry]]:
        """Parse raw NTFS MFT to find ALL files with full directory paths.

        Like Disk Drill / EaseUS — scans both active and deleted entries.
        Two-pass approach:
        Pass 1: Read ALL MFT records, build maps of:
                - record_num -> name, parent_ref, flags, timestamps, size
        Pass 2: Reconstruct full paths for ALL entries.

        Also extracts volume geometry (cluster size, total clusters) and
        $Bitmap for the carving phase.

        Returns flat list of (original_path, RecoveredEntry) tuples.
        Requires admin/elevated privileges for raw volume access.
        """
        import win32file

        MFT_RECORD_SIZE = 1024
        MFT_SIGNATURE = b"FILE"
        ATTR_FILENAME = 0x30
        ATTR_STANDARD_INFO = 0x10
        ATTR_DATA = 0x80
        FLAG_IN_USE = 0x01
        FLAG_DIRECTORY = 0x02

        volume_path = f"\\\\.\\{drive_letter}"
        try:
            handle = win32file.CreateFile(
                volume_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        except Exception as exc:
            logger.debug("Cannot open volume %s for MFT scan: %s", drive_letter, exc)
            return []

        try:
            return self._mft_two_pass(handle, MFT_RECORD_SIZE, MFT_SIGNATURE,
                                       ATTR_FILENAME, ATTR_STANDARD_INFO, ATTR_DATA,
                                       FLAG_IN_USE, FLAG_DIRECTORY)
        finally:
            win32file.CloseHandle(handle)

    def _mft_two_pass(self, handle, MFT_RECORD_SIZE, MFT_SIGNATURE,
                       ATTR_FILENAME, ATTR_STANDARD_INFO, ATTR_DATA,
                       FLAG_IN_USE, FLAG_DIRECTORY) -> list[tuple[str, RecoveredEntry]]:
        """Execute the two-pass MFT scan."""
        import win32file

        # Read boot sector to find MFT location
        boot_sector = win32file.ReadFile(handle, 512)[1]
        if len(boot_sector) < 512:
            return []

        bytes_per_sector = struct.unpack_from("<H", boot_sector, 0x0B)[0]
        sectors_per_cluster = boot_sector[0x0D]
        mft_cluster = struct.unpack_from("<Q", boot_sector, 0x30)[0]
        bytes_per_cluster = bytes_per_sector * sectors_per_cluster
        mft_offset = mft_cluster * bytes_per_cluster
        total_sectors = struct.unpack_from("<Q", boot_sector, 0x28)[0]
        total_clusters = total_sectors // sectors_per_cluster if sectors_per_cluster else 0

        # Store geometry for carver phase
        self._vol_bytes_per_cluster = bytes_per_cluster
        self._vol_total_clusters = total_clusters

        logger.info("MFT at cluster %d (offset %d), cluster size %d, total clusters %d",
                     mft_cluster, mft_offset, bytes_per_cluster, total_clusters)

        # ── Pass 1: Read ALL MFT records into lookup maps ──
        # Maps: record_num -> (name, parent_ref, is_dir, in_use, size, created, modified)
        record_map: dict[int, tuple[str, int, bool, bool, int, datetime | None, datetime | None]] = {}

        CHUNK_RECORDS = 4096  # Read 4096 records (4MB) at a time
        chunk_size = CHUNK_RECORDS * MFT_RECORD_SIZE
        # Scan up to 1 GB of MFT (~1M records, covers most volumes)
        MAX_MFT_BYTES = 1024 * 1024 * 1024

        current_offset = mft_offset
        record_index = 0
        total_deleted = 0
        consecutive_empty = 0
        last_progress_index = 0

        self._report_progress("[MFT] Pass 1: Reading MFT records...")

        while record_index * MFT_RECORD_SIZE < MAX_MFT_BYTES:
            self._check_cancelled()

            try:
                win32file.SetFilePointer(handle, current_offset, 0)
                _, data = win32file.ReadFile(handle, chunk_size)
            except Exception:
                break

            if not data or len(data) < MFT_RECORD_SIZE:
                break

            chunk_has_records = False

            for i in range(0, len(data) - MFT_RECORD_SIZE + 1, MFT_RECORD_SIZE):
                rec_num = record_index + (i // MFT_RECORD_SIZE)
                record = data[i:i + MFT_RECORD_SIZE]

                if record[:4] != MFT_SIGNATURE:
                    continue

                chunk_has_records = True

                flags = struct.unpack_from("<H", record, 0x16)[0]
                in_use = bool(flags & FLAG_IN_USE)
                is_dir = bool(flags & FLAG_DIRECTORY)

                # Apply fixup
                try:
                    record = self._apply_mft_fixup(record)
                except Exception:
                    continue

                # Parse attributes
                name = None
                parent_ref = 0
                file_size = 0
                created_time = None
                modified_time = None
                best_namespace = -1  # Track best namespace (Win32 > POSIX > DOS)

                attr_offset = struct.unpack_from("<H", record, 0x14)[0]
                pos = attr_offset

                while pos + 8 < MFT_RECORD_SIZE:
                    attr_type = struct.unpack_from("<I", record, pos)[0]
                    if attr_type == 0xFFFFFFFF or attr_type == 0:
                        break

                    attr_len = struct.unpack_from("<I", record, pos + 4)[0]
                    if attr_len < 8 or pos + attr_len > MFT_RECORD_SIZE:
                        break

                    non_resident = record[pos + 8]

                    if attr_type == ATTR_STANDARD_INFO and non_resident == 0:
                        content_off = struct.unpack_from("<H", record, pos + 0x14)[0]
                        abs_off = pos + content_off
                        if abs_off + 32 <= MFT_RECORD_SIZE:
                            created_ft = struct.unpack_from("<Q", record, abs_off)[0]
                            modified_ft = struct.unpack_from("<Q", record, abs_off + 8)[0]
                            created_time = _filetime_to_datetime(created_ft)
                            modified_time = _filetime_to_datetime(modified_ft)

                    elif attr_type == ATTR_FILENAME and non_resident == 0:
                        content_off = struct.unpack_from("<H", record, pos + 0x14)[0]
                        abs_off = pos + content_off
                        if abs_off + 0x42 <= MFT_RECORD_SIZE:
                            fn_parent = struct.unpack_from("<Q", record, abs_off)[0] & 0xFFFFFFFFFFFF
                            fn_alloc_size = struct.unpack_from("<Q", record, abs_off + 0x28)[0]
                            fn_real_size = struct.unpack_from("<Q", record, abs_off + 0x30)[0]
                            fn_name_len = record[abs_off + 0x40]
                            fn_namespace = record[abs_off + 0x41]

                            # Namespace priority: Win32+DOS(3) > Win32(1) > POSIX(0) > DOS(2)
                            ns_priority = {3: 4, 1: 3, 0: 2, 2: 1}.get(fn_namespace, 0)

                            name_start = abs_off + 0x42
                            if name_start + fn_name_len * 2 <= MFT_RECORD_SIZE and ns_priority > best_namespace:
                                try:
                                    fn_name = record[name_start:name_start + fn_name_len * 2].decode("utf-16-le")
                                    if fn_name and fn_namespace != 2:  # Skip pure DOS names
                                        name = fn_name
                                        parent_ref = fn_parent
                                        file_size = fn_real_size if fn_real_size > 0 else fn_alloc_size
                                        best_namespace = ns_priority
                                except Exception:
                                    pass

                    elif attr_type == ATTR_DATA and non_resident == 0:
                        # Resident data — get actual size
                        content_size = struct.unpack_from("<I", record, pos + 0x10)[0]
                        if content_size > 0 and not is_dir:
                            file_size = max(file_size, content_size)

                    pos += attr_len

                if name and not name.startswith("$"):
                    record_map[rec_num] = (name, parent_ref, is_dir, in_use, file_size, created_time, modified_time)
                    if not in_use:
                        total_deleted += 1

            record_index += len(data) // MFT_RECORD_SIZE
            current_offset += len(data)

            if not chunk_has_records:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break  # No more MFT records
            else:
                consecutive_empty = 0

            if record_index - last_progress_index >= 10000:
                last_progress_index = record_index
                self._files_found = len(record_map)
                self._deleted_found = total_deleted
                scanned_gb = current_offset / (1024 * 1024 * 1024)
                self._report_progress(
                    f"[MFT] Pass 1: {scanned_gb:.2f} GB scanned — {record_index:,} records, {len(record_map):,} files, {total_deleted:,} deleted"
                )

        logger.info("MFT Pass 1 complete: %d records mapped, %d deleted",
                     len(record_map), total_deleted)

        if not record_map:
            return []

        # ── Pass 2: Reconstruct directory paths for ALL entries ──
        self._report_progress("[MFT] Pass 2: Rebuilding directory paths...")

        # Build path cache using parent references
        path_cache: dict[int, str] = {}

        def _resolve_path(rec_num: int, depth: int = 0) -> str:
            """Resolve full path for an MFT record by following parent refs."""
            if rec_num in path_cache:
                return path_cache[rec_num]
            if depth > 50:  # Prevent infinite loops
                return "?"
            if rec_num == 5:  # Root directory in NTFS is always record 5
                path_cache[rec_num] = ""
                return ""
            if rec_num not in record_map:
                path_cache[rec_num] = "?"
                return "?"

            entry_name, parent, _, _, _, _, _ = record_map[rec_num]
            parent_path = _resolve_path(parent, depth + 1)

            if parent_path == "?":
                full_path = f"?/{entry_name}"
            elif parent_path == "":
                full_path = entry_name
            else:
                full_path = f"{parent_path}/{entry_name}"

            path_cache[rec_num] = full_path
            return full_path

        # Collect flat (path, entry) tuples for ALL records
        results: list[tuple[str, RecoveredEntry]] = []
        processed = 0
        total_records = len(record_map)

        for rec_num, (name, parent_ref, is_dir, in_use, file_size, created_time, modified_time) in record_map.items():
            self._check_cancelled()
            processed += 1

            if processed % 10000 == 0:
                self._report_progress(
                    f"[MFT] Pass 2: {processed:,}/{total_records:,} paths resolved"
                )

            full_path = _resolve_path(rec_num)

            # Skip active (in-use) files — we only recover deleted files
            if in_use:
                continue

            status = FileStatus.DELETED
            confidence = 0.5 if file_size > 0 else 0.2

            entry = RecoveredEntry(
                name=name,
                path=f"/{full_path}",
                is_directory=is_dir,
                size_bytes=file_size if not is_dir else 0,
                status=status,
                confidence=confidence,
                created=created_time,
                modified=modified_time,
                inode=rec_num,
            )

            results.append((full_path, entry))

        logger.info("MFT Pass 2 complete: %d total entries (%d deleted) with paths",
                     len(results), total_deleted)
        return results

    @staticmethod
    def _apply_mft_fixup(record: bytes) -> bytes:
        """Apply the MFT fixup array to validate and correct a record."""
        record = bytearray(record)
        fixup_offset = struct.unpack_from("<H", record, 0x04)[0]
        fixup_count = struct.unpack_from("<H", record, 0x06)[0]

        if fixup_count < 2 or fixup_offset + fixup_count * 2 > len(record):
            return bytes(record)

        signature = struct.unpack_from("<H", record, fixup_offset)[0]

        for i in range(1, fixup_count):
            sector_end = i * 512 - 2
            if sector_end + 2 > len(record):
                break
            stored = struct.unpack_from("<H", record, sector_end)[0]
            if stored != signature:
                raise ValueError(f"MFT fixup mismatch at sector {i}")
            replacement = struct.unpack_from("<H", record, fixup_offset + i * 2)[0]
            struct.pack_into("<H", record, sector_end, replacement)

        return bytes(record)

    def _read_volume_geometry(self, drive_letter: str) -> None:
        """Read NTFS boot sector to populate _vol_bytes_per_cluster and _vol_total_clusters."""
        import win32file

        volume_path = f"\\\\.\\{drive_letter}"
        handle = win32file.CreateFile(
            volume_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        try:
            boot_sector = win32file.ReadFile(handle, 512)[1]
            if len(boot_sector) < 512:
                return
            bytes_per_sector = struct.unpack_from("<H", boot_sector, 0x0B)[0]
            sectors_per_cluster = boot_sector[0x0D]
            total_sectors = struct.unpack_from("<Q", boot_sector, 0x28)[0]
            bytes_per_cluster = bytes_per_sector * sectors_per_cluster
            total_clusters = total_sectors // sectors_per_cluster if sectors_per_cluster else 0
            self._vol_bytes_per_cluster = bytes_per_cluster
            self._vol_total_clusters = total_clusters
        finally:
            win32file.CloseHandle(handle)

    def _read_bitmap(self, drive_letter: str) -> bytes | None:
        """Read NTFS $Bitmap as raw bytes (1 bit per cluster).

        Uses FSCTL_GET_VOLUME_BITMAP to get the cluster allocation bitmap.
        Returns raw bitmap bytes where bit=1 means allocated, or None on failure.
        Memory-efficient: a 500GB drive with 4KB clusters needs only ~15 MB.
        """
        import win32file

        volume_path = f"\\\\.\\{drive_letter}"
        try:
            handle = win32file.CreateFile(
                volume_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        except Exception as exc:
            logger.debug("Cannot open volume for bitmap: %s", exc)
            return None

        FSCTL_GET_VOLUME_BITMAP = 0x0009006F

        try:
            # Request bitmap starting from cluster 0
            input_buf = struct.pack("<Q", 0)  # StartingLcn
            # Request enough for the full bitmap — for a 2TB drive with
            # 4KB clusters that is ~64 MB of bitmap data.
            needed_bytes = (self._vol_total_clusters + 7) // 8
            out_size = needed_bytes + 24  # header is 16 bytes, add margin
            # Cap the buffer at 128 MB to be safe
            out_size = min(out_size, 128 * 1024 * 1024)
            try:
                result = win32file.DeviceIoControl(
                    handle, FSCTL_GET_VOLUME_BITMAP, input_buf, out_size
                )
            except Exception as exc:
                logger.debug("FSCTL_GET_VOLUME_BITMAP failed: %s", exc)
                return None

            if len(result) < 24:
                return None

            # VOLUME_BITMAP_BUFFER: StartingLcn (8), BitmapSize (8), Buffer[]
            bitmap_data = result[16:]
            return bytes(bitmap_data)

        finally:
            win32file.CloseHandle(handle)

    def _sum_sizes(self, entries: list[RecoveredEntry]) -> int:
        total = 0
        for e in entries:
            total += e.size_bytes
            if e.children:
                total += self._sum_sizes(e.children)
        return total


def _safe_datetime(ts: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _filetime_to_datetime(filetime: int) -> datetime | None:
    """Convert Windows FILETIME to datetime."""
    if filetime <= 0:
        return None
    try:
        seconds = (filetime / 10_000_000) - 11644473600
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
