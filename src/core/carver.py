"""File carver — scan unallocated disk clusters for file signatures.

Recovers files whose MFT entries have been overwritten by scanning
raw disk bytes for known magic headers (JPEG, PNG, PDF, DOCX, etc.).
This is how Disk Drill / EaseUS find their "Reconstructed" and
"File Path Lost" entries.
"""

from __future__ import annotations

import logging
import struct
import time
from datetime import datetime, timezone
from typing import Callable

from .models import FileStatus, RecoveredEntry
from .signatures import SIGNATURES, FileSignature, MAX_HEADER_LEN

logger = logging.getLogger(__name__)

# Windows constants
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3


class FileCarver:
    """Scan unallocated clusters for file signatures."""

    def __init__(self) -> None:
        self._cancelled = False
        self._progress_cb: Callable[[str, int, int], None] | None = None

    def cancel(self) -> None:
        self._cancelled = True

    def set_progress_callback(self, cb: Callable[[str, int, int], None]) -> None:
        self._progress_cb = cb

    def _check_cancelled(self) -> None:
        # Check own flag AND parent scanner's flag (if linked)
        if self._cancelled:
            from .win_scanner import ScanCancelled
            raise ScanCancelled()
        parent = getattr(self, '_parent_scanner', None)
        if parent and getattr(parent, '_cancelled', False):
            from .win_scanner import ScanCancelled
            raise ScanCancelled()

    def _report(self, msg: str, found: int) -> None:
        if self._progress_cb:
            self._progress_cb(msg, found, found)

    # ------------------------------------------------------------------ API
    def carve_volume(
        self,
        drive_letter: str,
        bytes_per_cluster: int,
        total_clusters: int,
        bitmap_data: bytes | None = None,
    ) -> list[tuple[str, RecoveredEntry]]:
        """Scan unallocated space on a volume for file signatures.

        Args:
            drive_letter: e.g. "C:"
            bytes_per_cluster: cluster size in bytes (usually 4096)
            total_clusters: total number of clusters on the volume
            bitmap_data: raw bitmap bytes (1 bit per cluster, bit=1 allocated).
                         If None, scans ALL clusters (slower but works
                         without bitmap).

        Returns:
            List of (virtual_path, RecoveredEntry) for each carved file.
        """
        import win32file

        # Don't reset _cancelled — it may have been set by cancel() already
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
            logger.warning("Cannot open volume for carving: %s", exc)
            return []

        results: list[tuple[str, RecoveredEntry]] = []
        found_count = 0

        # Read buffer — 4 MB gives good sequential throughput
        READ_SIZE = 4 * 1024 * 1024
        scan_offset = 0
        # Skip first cluster (boot sector area)
        scan_offset = bytes_per_cluster

        # Calculate total volume size
        volume_size = total_clusters * bytes_per_cluster
        # Scan the entire volume — no cap
        MAX_SCAN = volume_size

        scanned = 0
        last_report = 0
        last_report_time = time.monotonic()
        total_gb = MAX_SCAN / (1024 * 1024 * 1024)

        try:
            # Fast pre-scan: find all unallocated regions from bitmap
            # This avoids slow per-cluster Python loops during carving
            free_regions: list[tuple[int, int]] = []  # (start_offset, length)
            if bitmap_data is not None:
                self._report(
                    f"[Carving] Analyzing bitmap for free space on {total_gb:.1f} GB volume...",
                    0,
                )
                bitmap_len = len(bitmap_data)
                cluster = 0
                byte_idx = 0
                report_interval = max(1, bitmap_len // 50)  # ~50 progress updates

                while byte_idx < bitmap_len:
                    # Skip fully allocated bytes (0xFF) in bulk
                    if bitmap_data[byte_idx] == 0xFF:
                        byte_idx += 1
                        cluster += 8
                        if byte_idx % report_interval == 0:
                            now = time.monotonic()
                            if now - last_report_time > 1.0:
                                last_report_time = now
                                scanned_gb = (cluster * bytes_per_cluster) / (1024 * 1024 * 1024)
                                pct = int(scanned_gb * 100 / total_gb) if total_gb else 0
                                self._report(
                                    f"[Carving] Analyzing bitmap... {scanned_gb:.1f} / {total_gb:.1f} GB ({pct}%)",
                                    found_count,
                                )
                                self._check_cancelled()
                        continue

                    # This byte has at least one free cluster — check bits
                    bval = bitmap_data[byte_idx]
                    for bit in range(8):
                        c = cluster + bit
                        if c >= total_clusters:
                            break
                        if not (bval & (1 << bit)):
                            # Free cluster — extend current region or start new one
                            offset = c * bytes_per_cluster
                            if free_regions and free_regions[-1][0] + free_regions[-1][1] == offset:
                                free_regions[-1] = (free_regions[-1][0], free_regions[-1][1] + bytes_per_cluster)
                            else:
                                free_regions.append((offset, bytes_per_cluster))
                    byte_idx += 1
                    cluster += 8

                total_free = sum(length for _, length in free_regions)
                free_gb = total_free / (1024 * 1024 * 1024)
                self._report(
                    f"[Carving] Found {free_gb:.1f} GB free space in {len(free_regions):,} regions. Scanning for files...",
                    found_count,
                )
                last_report_time = time.monotonic()
            else:
                # No bitmap — scan entire volume
                free_regions = [(bytes_per_cluster, MAX_SCAN - bytes_per_cluster)]

            # Now scan only free regions for file signatures
            # First, merge nearby regions to reduce random seeks
            # (many free regions are single 4KB clusters — scanning each
            # individually causes extreme seek storms)
            MIN_SCAN_SIZE = 512  # skip regions smaller than one sector
            MERGE_GAP = 64 * 1024  # merge regions within 64KB of each other
            merged_regions: list[tuple[int, int]] = []
            for start, length in free_regions:
                if length < MIN_SCAN_SIZE:
                    continue
                if merged_regions:
                    prev_start, prev_length = merged_regions[-1]
                    prev_end = prev_start + prev_length
                    if start - prev_end <= MERGE_GAP:
                        # Merge with previous region (include the gap)
                        merged_regions[-1] = (prev_start, start + length - prev_start)
                        continue
                merged_regions.append((start, length))

            total_free_bytes = sum(length for _, length in merged_regions)
            bytes_scanned_free = 0
            regions_done = 0
            total_regions = len(merged_regions)
            free_gb = total_free_bytes / (1024 * 1024 * 1024)

            self._report(
                f"[Carving] Scanning {free_gb:.1f} GB free of {total_gb:.1f} GB drive ({total_regions:,} regions)...",
                found_count,
            )
            last_report_time = time.monotonic()

            for region_start, region_length in merged_regions:
                self._check_cancelled()
                regions_done += 1
                region_offset = region_start
                region_end = region_start + region_length

                while region_offset < region_end:
                    self._check_cancelled()

                    read_size = min(READ_SIZE, region_end - region_offset)

                    # Read a chunk
                    try:
                        win32file.SetFilePointer(handle, region_offset, 0)
                        _, data = win32file.ReadFile(handle, read_size)
                    except Exception:
                        region_offset += read_size
                        bytes_scanned_free += read_size
                        continue

                    if not data:
                        break

                    # ----- Fast signature scan using C-level bytes.find() -----
                    # Instead of stepping every 512 bytes in Python (slow,
                    # ~8 K iterations per 4 MB chunk), find all signature
                    # header positions at once with the C-optimised find().
                    hits: list[tuple[int, "FileSignature"]] = []
                    data_len = len(data)
                    for sig in SIGNATURES:
                        hdr = sig.header
                        hlen = len(hdr)
                        search_from = 0
                        while True:
                            idx = data.find(hdr, search_from)
                            if idx < 0 or idx >= data_len - MAX_HEADER_LEN:
                                break
                            hits.append((idx, sig))
                            search_from = idx + 512  # next sector at minimum

                    # Process hits in offset order, skipping past carved files
                    hits.sort(key=lambda h: h[0])
                    skip_to = 0

                    for hit_pos, sig in hits:
                        if hit_pos < skip_to:
                            continue

                        self._check_cancelled()

                        # Heartbeat every 0.5 s
                        now = time.monotonic()
                        if now - last_report_time > 0.5:
                            last_report_time = now
                            done_bytes = bytes_scanned_free + hit_pos
                            done_gb = done_bytes / (1024 * 1024 * 1024)
                            pct = int(done_bytes * 100 / total_free_bytes) if total_free_bytes else 0
                            self._report(
                                f"[Carving] {done_gb:.1f} / {free_gb:.1f} GB free of {total_gb:.1f} GB ({pct}%) — {found_count} files carved — region {regions_done:,}/{total_regions:,}",
                                found_count,
                            )

                        abs_offset = region_offset + hit_pos

                        # Tick callback keeps UI alive during size estimation
                        _tick_bs = bytes_scanned_free
                        _tick_hp = hit_pos
                        _tick_sn = sig.name

                        def _estimation_tick(
                            _bs=_tick_bs, _hp=_tick_hp, _sn=_tick_sn
                        ) -> None:
                            nonlocal last_report_time
                            now2 = time.monotonic()
                            if now2 - last_report_time > 0.5:
                                last_report_time = now2
                                pb = _bs + _hp
                                dg = pb / (1024 * 1024 * 1024)
                                pc = int(pb * 100 / total_free_bytes) if total_free_bytes else 0
                                self._report(
                                    f"[Carving] {dg:.1f} / {free_gb:.1f} GB free of {total_gb:.1f} GB ({pc}%) — probing {_sn} — region {regions_done:,}/{total_regions:,}",
                                    found_count,
                                )

                        carved_size = self._estimate_file_size(
                            handle, abs_offset, sig, bytes_per_cluster,
                            on_tick=_estimation_tick,
                        )
                        if carved_size < 16:
                            continue  # false positive — move to next hit

                        found_count += 1
                        name = f"carved_{found_count:06d}{sig.extension}"
                        confidence = 0.7 if sig.footer else 0.4

                        entry = RecoveredEntry(
                            name=name,
                            path=f"/Reconstructed/{sig.name}/{name}",
                            is_directory=False,
                            size_bytes=carved_size,
                            status=FileStatus.PARTIAL,
                            confidence=confidence,
                            inode=0,
                        )
                        entry.data_runs = [(abs_offset, carved_size)]
                        results.append(
                            (f"Reconstructed/{sig.name}/{name}", entry)
                        )

                        skip_to = hit_pos + max(carved_size, bytes_per_cluster)

                    # ----- end of chunk scan -----
                    bytes_scanned_free += len(data)
                    region_offset += len(data)

                    # Report progress between chunks
                    now = time.monotonic()
                    if now - last_report_time > 0.5:
                        last_report_time = now
                        done_gb = bytes_scanned_free / (1024 * 1024 * 1024)
                        pct = int(bytes_scanned_free * 100 / total_free_bytes) if total_free_bytes else 0
                        self._report(
                            f"[Carving] {done_gb:.1f} / {free_gb:.1f} GB free of {total_gb:.1f} GB ({pct}%) — {found_count} files carved — region {regions_done:,}/{total_regions:,}",
                            found_count,
                        )

        except Exception as exc:
            logger.warning("Carving stopped: %s", exc)
        finally:
            win32file.CloseHandle(handle)

        logger.info("File carving complete: %d files carved", found_count)
        return results

    def _estimate_file_size(
        self,
        handle,
        offset: int,
        sig: FileSignature,
        cluster_size: int,
        on_tick: Callable[[], None] | None = None,
    ) -> int:
        """Estimate the size of a carved file.

        If the signature has a footer, search for it.
        Otherwise use heuristics (zeroed clusters, max_size cap).
        """
        import win32file

        # Keep estimation bounded to avoid long stalls on false positives.
        max_size = min(sig.max_size, 16 * 1024 * 1024)
        read_chunk = 64 * 1024  # 64 KB reads for footer search

        if sig.footer:
            # Search for footer
            search_offset = offset + len(sig.header)
            searched = len(sig.header)
            footer = sig.footer

            while searched < max_size:
                self._check_cancelled()
                if on_tick:
                    on_tick()
                try:
                    win32file.SetFilePointer(handle, search_offset, 0)
                    _, block = win32file.ReadFile(handle, read_chunk)
                except Exception:
                    break
                if not block:
                    break

                idx = block.find(footer)
                if idx >= 0:
                    return searched + idx + len(footer)

                searched += len(block)
                search_offset += len(block)

            # Footer not found within max_size — return max_size
            return min(searched, max_size)

        else:
            # No footer — estimate size by reading until empty/zeroed
            # For structured formats (ZIP, 7z, etc.), try to read size from header
            if sig.extension == ".bmp":
                return self._read_bmp_size(handle, offset)
            if sig.extension == ".exe":
                return self._read_pe_size(handle, offset)

            # Generic: read up to 2 clusters and check if data looks valid
            # Use max_size capped at a reasonable scan for estimation
            return min(max_size, 10 * 1024 * 1024)  # Default 10 MB estimate

    def _read_bmp_size(self, handle, offset: int) -> int:
        """Read file size from BMP header."""
        import win32file
        try:
            win32file.SetFilePointer(handle, offset, 0)
            _, hdr = win32file.ReadFile(handle, 14)
            if len(hdr) >= 6:
                return struct.unpack_from("<I", hdr, 2)[0]
        except Exception:
            pass
        return 0

    def _read_pe_size(self, handle, offset: int) -> int:
        """Estimate PE (exe/dll) size from headers."""
        import win32file
        try:
            win32file.SetFilePointer(handle, offset, 0)
            _, data = win32file.ReadFile(handle, 4096)
            if len(data) < 64:
                return 0
            pe_off = struct.unpack_from("<I", data, 0x3C)[0]
            if pe_off + 0x58 > len(data):
                return 0
            size_of_image = struct.unpack_from("<I", data, pe_off + 0x50)[0]
            return min(size_of_image, 500 * 1024 * 1024)
        except Exception:
            return 0
