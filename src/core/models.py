"""Data models for the recovery engine."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath


class FileStatus(enum.Enum):
    """Recovery status of a file or folder."""
    INTACT = "intact"
    DELETED = "deleted"
    PARTIAL = "partial"
    METADATA_ONLY = "metadata_only"


class FileSystemType(enum.Enum):
    """Supported file system types."""
    NTFS = "NTFS"
    FAT12 = "FAT12"
    FAT16 = "FAT16"
    FAT32 = "FAT32"
    EXFAT = "exFAT"
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    HFS_PLUS = "HFS+"
    UFS = "UFS"
    ISO9660 = "ISO9660"
    UNKNOWN = "Unknown"


class PartitionScheme(enum.Enum):
    """Partition table type."""
    MBR = "MBR"
    GPT = "GPT"
    UNKNOWN = "Unknown"


@dataclass
class DiskInfo:
    """Represents a physical disk."""
    index: int                          # PhysicalDrive index
    model: str = ""
    serial: str = ""
    size_bytes: int = 0
    sector_size: int = 512
    partition_scheme: PartitionScheme = PartitionScheme.UNKNOWN
    partitions: list[PartitionInfo] = field(default_factory=list)

    @property
    def device_path(self) -> str:
        return f"\\\\.\\PhysicalDrive{self.index}"

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)


@dataclass
class PartitionInfo:
    """Represents a partition on a disk."""
    index: int
    offset_bytes: int
    size_bytes: int
    fs_type: FileSystemType = FileSystemType.UNKNOWN
    label: str = ""
    drive_letter: str = ""             # e.g. "C:" if mounted
    is_active: bool = False

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)


@dataclass
class RecoveredEntry:
    """A file or folder found during scanning."""
    name: str
    path: str                           # Full original path
    is_directory: bool
    size_bytes: int = 0
    status: FileStatus = FileStatus.INTACT
    confidence: float = 1.0             # 0.0 - 1.0 recovery confidence
    created: datetime | None = None
    modified: datetime | None = None
    accessed: datetime | None = None
    inode: int = 0
    # For files: list of (offset, length) cluster runs on disk
    data_runs: list[tuple[int, int]] = field(default_factory=list)
    children: list[RecoveredEntry] = field(default_factory=list)

    @property
    def size_display(self) -> str:
        return format_size(self.size_bytes)

    @property
    def date_display(self) -> str:
        if self.modified:
            return self.modified.strftime("%Y-%m-%d %H:%M:%S")
        return ""


@dataclass
class ScanResult:
    """Result of a deep scan operation."""
    disk: DiskInfo
    partition: PartitionInfo
    root_entries: list[RecoveredEntry] = field(default_factory=list)
    total_files: int = 0
    total_deleted: int = 0
    total_size_bytes: int = 0
    scan_duration_seconds: float = 0.0
    target_path: str = "/"               # Directory path scanned (/ = full partition)


@dataclass
class RecoveryTask:
    """A recovery operation to perform."""
    entries: list[RecoveredEntry]
    destination: str                    # Destination directory path
    preserve_structure: bool = True
    overwrite_existing: bool = False


def format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} EB"
