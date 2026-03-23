"""File and folder recovery engine."""

from __future__ import annotations

import enum
import logging
import os
import shutil
from pathlib import Path
from typing import Callable

try:
    import pytsk3
    HAS_PYTSK3 = True
except ImportError:
    pytsk3 = None  # type: ignore[assignment]
    HAS_PYTSK3 = False

from .models import FileStatus, RecoveredEntry, RecoveryTask

logger = logging.getLogger(__name__)

# Buffer size for streaming file data from disk (1 MB)
READ_BUFFER_SIZE = 1024 * 1024


class ConflictAction(enum.Enum):
    """User response when a destination file already exists."""
    REPLACE = "replace"
    SKIP = "skip"
    DUPLICATE = "duplicate"
    REPLACE_ALL = "replace_all"
    SKIP_ALL = "skip_all"
    DUPLICATE_ALL = "duplicate_all"


def _deduplicate_path(target: Path) -> Path:
    """Generate a unique path by appending (1), (2), etc."""
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# Type for the callback the UI provides to resolve conflicts.
# Receives the target Path, returns a ConflictAction.
ConflictHandler = Callable[[Path], ConflictAction]


class RecoveryCancelled(Exception):
    """Raised when recovery is cancelled by the user."""


class RecoveryEngine:
    """Recovers files and folders from disk images."""

    def __init__(self, fs_info: pytsk3.FS_Info):
        self._fs_info = fs_info
        self._cancelled = False
        self._progress_callback: Callable[[str, int, int, int], None] | None = None
        self._conflict_handler: ConflictHandler | None = None
        self._conflict_policy: ConflictAction | None = None  # sticky "All" choice
        self._files_recovered = 0
        self._files_failed = 0
        self._files_skipped = 0
        self._bytes_recovered = 0

    def cancel(self) -> None:
        """Request recovery cancellation."""
        self._cancelled = True

    def set_progress_callback(
        self, callback: Callable[[str, int, int, int], None]
    ) -> None:
        """Set progress callback.

        Callback signature: (current_file, recovered_count, failed_count, bytes_written)
        """
        self._progress_callback = callback

    def set_conflict_handler(self, handler: ConflictHandler) -> None:
        """Set a callback to ask the user what to do on file conflicts."""
        self._conflict_handler = handler

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise RecoveryCancelled("Recovery cancelled by user")

    def _report_progress(self, path: str) -> None:
        if self._progress_callback:
            self._progress_callback(
                path,
                self._files_recovered,
                self._files_failed,
                self._bytes_recovered,
            )

    def recover(self, task: RecoveryTask) -> dict:
        """Execute a recovery task.

        Args:
            task: RecoveryTask with entries to recover and destination path

        Returns:
            dict with recovery statistics
        """
        self._cancelled = False
        self._files_recovered = 0
        self._files_failed = 0
        self._files_skipped = 0
        self._bytes_recovered = 0
        self._conflict_policy = None

        # If overwrite_existing is set, pre-populate the sticky policy
        if task.overwrite_existing:
            self._conflict_policy = ConflictAction.REPLACE_ALL

        dest = Path(task.destination)
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)

        for entry in task.entries:
            self._check_cancelled()
            self._recover_entry(entry, dest, task.preserve_structure)

        return {
            "files_recovered": self._files_recovered,
            "files_failed": self._files_failed,
            "files_skipped": self._files_skipped,
            "bytes_recovered": self._bytes_recovered,
        }

    def _recover_entry(
        self,
        entry: RecoveredEntry,
        dest_root: Path,
        preserve_structure: bool,
    ) -> None:
        """Recover a single file or folder entry."""
        self._check_cancelled()

        if preserve_structure:
            # Strip leading / from the original path
            relative = entry.path.lstrip("/")
            target = dest_root / relative
        else:
            target = dest_root / entry.name

        if entry.is_directory:
            self._recover_directory(entry, target, preserve_structure)
        else:
            self._recover_file(entry, target)

    def _recover_directory(
        self,
        entry: RecoveredEntry,
        target: Path,
        preserve_structure: bool,
    ) -> None:
        """Recover a directory and its children."""
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create directory %s: %s", target, exc)
        logger.debug("Created directory: %s", target)

        for child in entry.children:
            self._check_cancelled()
            child_target = target / child.name

            if child.is_directory:
                self._recover_directory(child, child_target, preserve_structure)
            else:
                self._recover_file(child, child_target)

    def _ask_conflict(self, target: Path) -> ConflictAction:
        """Determine what to do when *target* already exists."""
        # A sticky "All" policy overrides per-file prompts
        if self._conflict_policy in (
            ConflictAction.REPLACE_ALL, ConflictAction.SKIP_ALL,
            ConflictAction.DUPLICATE_ALL,
        ):
            return self._conflict_policy

        if self._conflict_handler:
            action = self._conflict_handler(target)
            # Promote "All" choices to sticky policy
            if action == ConflictAction.REPLACE_ALL:
                self._conflict_policy = ConflictAction.REPLACE_ALL
            elif action == ConflictAction.SKIP_ALL:
                self._conflict_policy = ConflictAction.SKIP_ALL
            elif action == ConflictAction.DUPLICATE_ALL:
                self._conflict_policy = ConflictAction.DUPLICATE_ALL
            return action

        # No handler configured — default to skip
        return ConflictAction.SKIP

    def _recover_file(
        self,
        entry: RecoveredEntry,
        target: Path,
    ) -> None:
        """Recover a single file by reading its data from the disk image."""
        if entry.status == FileStatus.METADATA_ONLY:
            logger.warning("Skipping metadata-only file: %s", entry.path)
            self._files_failed += 1
            return

        # Handle name conflicts
        if target.exists():
            action = self._ask_conflict(target)
            if action in (ConflictAction.SKIP, ConflictAction.SKIP_ALL):
                logger.info("Skipped (exists): %s", entry.path)
                self._files_skipped += 1
                self._report_progress(entry.path)
                return
            if action in (ConflictAction.DUPLICATE, ConflictAction.DUPLICATE_ALL):
                target = _deduplicate_path(target)
            # REPLACE / REPLACE_ALL — overwrite by proceeding normally

        # Ensure parent directory exists
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create parent dir for %s: %s", target, exc)
            self._files_failed += 1
            self._report_progress(entry.path)
            return

        try:
            # Open the file via its inode
            file_obj = self._fs_info.open_meta(inode=entry.inode)
        except Exception as exc:
            logger.error("Cannot open inode %d for %s: %s", entry.inode, entry.path, exc)
            self._files_failed += 1
            return

        try:
            self._stream_file_data(file_obj, entry, target)
            self._files_recovered += 1
            logger.info("Recovered: %s -> %s", entry.path, target)
        except RecoveryCancelled:
            # Clean up partial file
            if target.exists():
                target.unlink()
            raise
        except Exception as exc:
            logger.error("Failed to recover %s: %s", entry.path, exc)
            self._files_failed += 1
            # Clean up partial file on error
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass

        self._report_progress(entry.path)

    def _stream_file_data(
        self,
        file_obj,
        entry: RecoveredEntry,
        target: Path,
    ) -> None:
        """Stream file data from disk image to destination file."""
        offset = 0
        size = entry.size_bytes

        with open(target, "wb") as out:
            while offset < size:
                self._check_cancelled()

                read_size = min(READ_BUFFER_SIZE, size - offset)
                try:
                    data = file_obj.read_random(offset, read_size)
                except Exception as exc:
                    # Bad sector — write zeros and continue
                    logger.warning(
                        "Read error at offset %d in %s: %s (padding with zeros)",
                        offset, entry.path, exc,
                    )
                    data = b"\x00" * read_size

                if not data:
                    break

                out.write(data)
                offset += len(data)
                self._bytes_recovered += len(data)

        # Restore timestamps if available
        self._restore_timestamps(target, entry)

    def _restore_timestamps(self, target: Path, entry: RecoveredEntry) -> None:
        """Restore file modification and access timestamps."""
        try:
            mtime = entry.modified.timestamp() if entry.modified else None
            atime = entry.accessed.timestamp() if entry.accessed else None

            if mtime is not None:
                a = atime if atime is not None else mtime
                os.utime(target, (a, mtime))
        except Exception as exc:
            logger.debug("Could not restore timestamps for %s: %s", target, exc)




class NativeRecoveryEngine:
    """Recovery engine using standard file operations (no pytsk3).

    Can recover intact files by copying them from the source volume.
    Deleted files cannot be recovered without pytsk3/raw disk access.
    """

    def __init__(self, drive_letter: str):
        self._drive_letter = drive_letter.rstrip(":\\")
        self._cancelled = False
        self._progress_callback: Callable[[str, int, int, int], None] | None = None
        self._conflict_handler: ConflictHandler | None = None
        self._conflict_policy: ConflictAction | None = None
        self._files_recovered = 0
        self._files_failed = 0
        self._files_skipped = 0
        self._bytes_recovered = 0

    def cancel(self) -> None:
        self._cancelled = True

    def set_progress_callback(
        self, callback: Callable[[str, int, int, int], None]
    ) -> None:
        self._progress_callback = callback

    def set_conflict_handler(self, handler: ConflictHandler) -> None:
        self._conflict_handler = handler

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise RecoveryCancelled("Recovery cancelled by user")

    def _report_progress(self, path: str) -> None:
        if self._progress_callback:
            self._progress_callback(
                path,
                self._files_recovered,
                self._files_failed,
                self._bytes_recovered,
            )

    def _ask_conflict(self, target: Path) -> ConflictAction:
        if self._conflict_policy in (
            ConflictAction.REPLACE_ALL, ConflictAction.SKIP_ALL,
            ConflictAction.DUPLICATE_ALL,
        ):
            return self._conflict_policy
        if self._conflict_handler:
            action = self._conflict_handler(target)
            if action == ConflictAction.REPLACE_ALL:
                self._conflict_policy = ConflictAction.REPLACE_ALL
            elif action == ConflictAction.SKIP_ALL:
                self._conflict_policy = ConflictAction.SKIP_ALL
            elif action == ConflictAction.DUPLICATE_ALL:
                self._conflict_policy = ConflictAction.DUPLICATE_ALL
            return action
        return ConflictAction.SKIP

    def recover(self, task: RecoveryTask) -> dict:
        self._cancelled = False
        self._files_recovered = 0
        self._files_failed = 0
        self._files_skipped = 0
        self._bytes_recovered = 0
        self._conflict_policy = None

        if task.overwrite_existing:
            self._conflict_policy = ConflictAction.REPLACE_ALL

        dest = Path(task.destination)
        dest.mkdir(parents=True, exist_ok=True)

        for entry in task.entries:
            self._check_cancelled()
            self._recover_entry(
                entry, dest, task.preserve_structure
            )

        return {
            "files_recovered": self._files_recovered,
            "files_failed": self._files_failed,
            "files_skipped": self._files_skipped,
            "bytes_recovered": self._bytes_recovered,
        }

    def _source_path(self, entry: RecoveredEntry) -> Path:
        """Build the real filesystem path from drive letter + virtual path."""
        rel = entry.path.lstrip("/").replace("/", os.sep)
        return Path(f"{self._drive_letter}:\\{rel}")

    def _recover_entry(
        self,
        entry: RecoveredEntry,
        dest_root: Path,
        preserve_structure: bool,
    ) -> None:
        self._check_cancelled()

        if entry.status == FileStatus.DELETED:
            logger.warning(
                "Cannot recover deleted file without pytsk3: %s", entry.path
            )
            self._files_failed += 1
            self._report_progress(entry.path)
            return

        if entry.status == FileStatus.METADATA_ONLY:
            self._files_failed += 1
            return

        if preserve_structure:
            relative = entry.path.lstrip("/")
            target = dest_root / relative
        else:
            target = dest_root / entry.name

        if entry.is_directory:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Cannot create directory %s: %s", target, exc)
            for child in entry.children:
                self._check_cancelled()
                child_target_root = dest_root
                self._recover_entry(
                    child, child_target_root, preserve_structure
                )
        else:
            self._copy_file(entry, target)

    def _copy_file(
        self, entry: RecoveredEntry, target: Path
    ) -> None:
        source = self._source_path(entry)

        if not source.exists():
            logger.error("Source file not found: %s", source)
            self._files_failed += 1
            return

        if target.exists():
            action = self._ask_conflict(target)
            if action in (ConflictAction.SKIP, ConflictAction.SKIP_ALL):
                logger.info("Skipped (exists): %s", entry.path)
                self._files_skipped += 1
                self._report_progress(entry.path)
                return
            if action in (ConflictAction.DUPLICATE, ConflictAction.DUPLICATE_ALL):
                target = _deduplicate_path(target)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create parent dir for %s: %s", target, exc)
            self._files_failed += 1
            self._report_progress(entry.path)
            return

        try:
            shutil.copy2(str(source), str(target))
            size = target.stat().st_size
            self._files_recovered += 1
            self._bytes_recovered += size
            logger.info("Recovered: %s -> %s", source, target)
        except Exception as exc:
            logger.error("Failed to copy %s: %s", source, exc)
            self._files_failed += 1

        self._report_progress(entry.path)
