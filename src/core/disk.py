"""Disk enumeration and raw access for Windows."""

from __future__ import annotations

import ctypes
import logging
import struct
from typing import Generator

from .models import DiskInfo, FileSystemType, PartitionInfo, PartitionScheme

logger = logging.getLogger(__name__)

# Windows constants
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_DRIVE_LAYOUT_EX = 0x00070050

# Partition GUIDs
PARTITION_GUID_TYPES = {
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft Basic Data",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft Reserved",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows Recovery",
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI System",
}


def is_admin() -> bool:
    """Check if the current process has administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except (AttributeError, OSError):
        return False


def enumerate_physical_drives(max_drives: int = 16) -> list[DiskInfo]:
    """Enumerate all physical drives on the system.

    Tries to open each PhysicalDriveN from 0 to max_drives-1.
    """
    import win32file
    import win32api

    drives: list[DiskInfo] = []

    for i in range(max_drives):
        device_path = f"\\\\.\\PhysicalDrive{i}"
        try:
            handle = win32file.CreateFile(
                device_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        except Exception:
            continue

        try:
            disk = DiskInfo(index=i)
            _fill_drive_geometry(handle, disk)
            _fill_storage_property(handle, disk)
            _fill_partition_layout(handle, disk)
            drives.append(disk)
            logger.info(
                "Found drive %d: %s (%s)",
                i, disk.model, disk.size_display,
            )
        except Exception as exc:
            logger.warning("Error reading drive %d: %s", i, exc)
        finally:
            win32file.CloseHandle(handle)

    return drives


def _fill_drive_geometry(handle, disk: DiskInfo) -> None:
    """Fill disk size and sector size from IOCTL_DISK_GET_DRIVE_GEOMETRY_EX."""
    import win32file
    import winioctlcon

    try:
        result = win32file.DeviceIoControl(
            handle,
            IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
            None,
            256,
        )
        if len(result) >= 24:
            # DISK_GEOMETRY_EX: Cylinders(8) + MediaType(4) + TracksPerCylinder(4)
            # + SectorsPerTrack(4) + BytesPerSector(4) + DiskSize(8)
            cylinders = struct.unpack_from("<q", result, 0)[0]
            media_type = struct.unpack_from("<I", result, 8)[0]
            tracks_per_cyl = struct.unpack_from("<I", result, 12)[0]
            sectors_per_track = struct.unpack_from("<I", result, 16)[0]
            bytes_per_sector = struct.unpack_from("<I", result, 20)[0]
            disk_size = struct.unpack_from("<q", result, 24)[0]

            disk.sector_size = bytes_per_sector
            disk.size_bytes = disk_size
    except Exception as exc:
        logger.debug("GEOMETRY_EX failed: %s", exc)


def _fill_storage_property(handle, disk: DiskInfo) -> None:
    """Fill model and serial from IOCTL_STORAGE_QUERY_PROPERTY."""
    import win32file

    # STORAGE_PROPERTY_QUERY: PropertyId=0, QueryType=0
    query = struct.pack("<III", 0, 0, 0)
    try:
        result = win32file.DeviceIoControl(
            handle,
            IOCTL_STORAGE_QUERY_PROPERTY,
            query,
            4096,
        )
        if len(result) >= 48:
            vendor_offset = struct.unpack_from("<I", result, 28)[0]
            product_offset = struct.unpack_from("<I", result, 32)[0]
            serial_offset = struct.unpack_from("<I", result, 40)[0]

            def _read_string(data: bytes, offset: int) -> str:
                if offset == 0 or offset >= len(data):
                    return ""
                end = data.index(b"\x00", offset) if b"\x00" in data[offset:] else len(data)
                return data[offset:end].decode("ascii", errors="replace").strip()

            vendor = _read_string(result, vendor_offset)
            product = _read_string(result, product_offset)
            disk.model = f"{vendor} {product}".strip()
            disk.serial = _read_string(result, serial_offset)
    except Exception as exc:
        logger.debug("STORAGE_QUERY_PROPERTY failed: %s", exc)


def _fill_partition_layout(handle, disk: DiskInfo) -> None:
    """Fill partition info from IOCTL_DISK_GET_DRIVE_LAYOUT_EX."""
    import win32file

    try:
        result = win32file.DeviceIoControl(
            handle,
            IOCTL_DISK_GET_DRIVE_LAYOUT_EX,
            None,
            65536,
        )
        if len(result) < 8:
            return

        partition_style = struct.unpack_from("<I", result, 0)[0]
        partition_count = struct.unpack_from("<I", result, 4)[0]

        if partition_style == 0:
            disk.partition_scheme = PartitionScheme.MBR
        elif partition_style == 1:
            disk.partition_scheme = PartitionScheme.GPT
        else:
            disk.partition_scheme = PartitionScheme.UNKNOWN

        # Parse partition entries (offset depends on partition style)
        # MBR: 8 + 40 bytes header; GPT: 8 + 40 bytes header
        # Each PARTITION_INFORMATION_EX is 144 bytes
        header_size = 48  # PartitionStyle(4) + PartitionCount(4) + union(40)
        entry_size = 144

        for i in range(partition_count):
            offset = header_size + i * entry_size
            if offset + entry_size > len(result):
                break

            part_style = struct.unpack_from("<I", result, offset)[0]
            part_offset = struct.unpack_from("<q", result, offset + 8)[0]
            part_size = struct.unpack_from("<q", result, offset + 16)[0]
            part_number = struct.unpack_from("<I", result, offset + 24)[0]

            if part_size <= 0:
                continue

            part = PartitionInfo(
                index=part_number,
                offset_bytes=part_offset,
                size_bytes=part_size,
            )
            disk.partitions.append(part)
            logger.debug(
                "Partition %d: offset=%d size=%s",
                part_number, part_offset, part.size_display,
            )

    except Exception as exc:
        logger.debug("DRIVE_LAYOUT_EX failed: %s", exc)


def get_logical_volumes() -> list[dict]:
    """Get mounted logical volumes with drive letters."""
    import win32api
    import win32file

    volumes = []
    drive_bits = win32api.GetLogicalDrives()

    for i in range(26):
        if drive_bits & (1 << i):
            letter = chr(ord('A') + i)
            drive_root = f"{letter}:\\"
            try:
                drive_type = win32file.GetDriveType(drive_root)
                type_names = {
                    0: "Unknown", 1: "No Root", 2: "Removable",
                    3: "Fixed", 4: "Network", 5: "CD-ROM", 6: "RAM Disk",
                }
                vol_info = None
                try:
                    vol_info = win32api.GetVolumeInformation(drive_root)
                except Exception:
                    pass

                # Get disk space
                total_bytes = 0
                free_bytes = 0
                try:
                    import ctypes
                    free_user = ctypes.c_ulonglong(0)
                    total = ctypes.c_ulonglong(0)
                    free_total = ctypes.c_ulonglong(0)
                    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                        drive_root,
                        ctypes.byref(free_user),
                        ctypes.byref(total),
                        ctypes.byref(free_total),
                    )
                    total_bytes = total.value
                    free_bytes = free_total.value
                except Exception:
                    pass

                volumes.append({
                    "letter": f"{letter}:",
                    "type": type_names.get(drive_type, "Unknown"),
                    "label": vol_info[0] if vol_info else "",
                    "fs_type": vol_info[4] if vol_info else "",
                    "serial": vol_info[1] if vol_info else 0,
                    "total_bytes": total_bytes,
                    "free_bytes": free_bytes,
                })
            except Exception:
                continue

    return volumes


def open_disk_image(device_path: str):
    """Open a disk or partition for reading via pytsk3.

    Args:
        device_path: Path like '\\\\.\\PhysicalDrive0' or '\\\\.\\C:'

    Returns:
        pytsk3.Img_Info object
    """
    import pytsk3

    logger.info("Opening disk image: %s", device_path)
    return pytsk3.Img_Info(device_path)
