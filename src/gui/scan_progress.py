"""Scan progress page with real-time stats."""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, Signal, Slot, QThread, QMutex, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.disk import open_disk_image
from ..core.models import DiskInfo, FileStatus, PartitionInfo, ScanResult
from ..core.scanner import DiskScanner, ScanCancelled, HAS_PYTSK3
from ..utils.formatting import format_size, format_duration

logger = logging.getLogger(__name__)


class ScanWorker(QThread):
    """Background worker for scanning."""
    progress = Signal(str, int, int)       # path, files_found, deleted_found
    finished = Signal(object, object, object)  # ScanResult, img_info, fs_info
    error = Signal(str)

    def __init__(
        self,
        disk: DiskInfo,
        partition: PartitionInfo | None,
        target_path: str = "/",
        drive_letter: str = "",
        scan_size_limit_gb: int = 0,
        extension_filter: list[str] | None = None,
    ):
        super().__init__()
        self.disk = disk
        self.partition = partition
        self.target_path = target_path
        self.drive_letter = drive_letter
        self.scan_size_limit_gb = scan_size_limit_gb
        self.extension_filter = extension_filter or []
        self.scanner = DiskScanner()
        self._native_scanner = None

    def run(self):
        if HAS_PYTSK3:
            self._run_pytsk3()
        else:
            self._run_native()

    def _run_pytsk3(self):
        try:
            import pytsk3
            from ..core.win_scanner import ScanCancelled as NativeScanCancelled

            # Determine volume path for pytsk3
            # If a drive letter is provided (volume), open \\.<letter>:
            # Otherwise open the physical disk device
            if self.drive_letter:
                device_path = f"\\\\.\\{self.drive_letter}"
            else:
                device_path = self.disk.device_path

            img_info = open_disk_image(device_path)

            self.scanner.set_progress_callback(self._on_progress)
            if self.extension_filter:
                self.scanner.set_extension_filter(self.extension_filter)
            all_results = []
            fs_info_last = None

            if self.drive_letter:
                # Volume — open FS at offset 0 (the volume IS the FS)
                partitions_to_scan = [
                    self.partition or PartitionInfo(
                        index=0, offset_bytes=0, size_bytes=0,
                        drive_letter=self.drive_letter,
                    )
                ]
                # Force offset to 0 for volume handles
                for p in partitions_to_scan:
                    p.offset_bytes = 0
            else:
                # Physical disk — scan each partition
                partitions_to_scan = (
                    [self.partition] if self.partition
                    else self.disk.partitions
                )

            if not partitions_to_scan:
                self.error.emit("No partitions found on this disk.")
                return

            for part in partitions_to_scan:
                try:
                    fs_info = pytsk3.FS_Info(
                        img_info, offset=part.offset_bytes
                    )
                    fs_info_last = fs_info
                except Exception as exc:
                    logger.warning(
                        "Cannot open FS on partition %d: %s", part.index, exc
                    )
                    continue

                result = self.scanner.scan_partition(
                    img_info, part, self.disk, target_path=self.target_path
                )
                all_results.append(result)

                try:
                    orphan_result = self.scanner.scan_unallocated(
                        img_info, part, self.disk
                    )
                    if orphan_result.root_entries:
                        # scan_unallocated now returns a proper tree with
                        # reconstructed paths plus an "Orphan Files" node
                        # for truly unresolvable entries.  Merge into main.
                        result.root_entries.extend(orphan_result.root_entries)
                        result.total_files += orphan_result.total_files
                        result.total_deleted += orphan_result.total_deleted
                        result.total_size_bytes += orphan_result.total_size_bytes
                except Exception as exc:
                    logger.warning("Orphan scan failed: %s", exc)

            if not all_results:
                self.error.emit("No readable file systems found.")
                return

            merged = all_results[0]
            for r in all_results[1:]:
                merged.root_entries.extend(r.root_entries)
                merged.total_files += r.total_files
                merged.total_deleted += r.total_deleted
                merged.total_size_bytes += r.total_size_bytes
                merged.scan_duration_seconds += r.scan_duration_seconds

            # Phase: File carving from unallocated space
            drive = self.drive_letter
            if not drive and self.partition and self.partition.drive_letter:
                drive = self.partition.drive_letter
            if drive:
                try:
                    self._on_progress("[Carving] Scanning unallocated space...", merged.total_files, merged.total_deleted)
                    from ..core.carver import FileCarver
                    from ..core.win_scanner import WindowsScanner, ScanCancelled as NativeScanCancelled
                    # Read bitmap and volume geometry via native API
                    ws = WindowsScanner()
                    ws.set_progress_callback(self._on_progress)
                    ws._vol_bytes_per_cluster = 4096
                    ws._vol_total_clusters = 0
                    try:
                        ws._read_volume_geometry(drive)
                    except Exception:
                        pass
                    bitmap_data = None
                    if ws._vol_total_clusters > 0:
                        try:
                            bitmap_data = ws._read_bitmap(drive)
                        except Exception:
                            pass

                    carver = FileCarver()
                    base_entries = self.scanner._entries_scanned
                    base_deleted = merged.total_deleted

                    def _carver_progress(msg: str, carved: int, _d: int) -> None:
                        self._on_progress(msg, base_entries + carved, base_deleted + carved)

                    carver.set_progress_callback(_carver_progress)
                    carver._parent_scanner = self.scanner

                    # Apply scan size limit to carving
                    carve_total_clusters = ws._vol_total_clusters
                    if self.scan_size_limit_gb > 0 and ws._vol_bytes_per_cluster > 0:
                        limit_bytes = self.scan_size_limit_gb * (1024 ** 3)
                        limit_clusters = limit_bytes // ws._vol_bytes_per_cluster
                        if limit_clusters < carve_total_clusters:
                            carve_total_clusters = limit_clusters

                    carved_results = carver.carve_volume(
                        drive,
                        bytes_per_cluster=ws._vol_bytes_per_cluster,
                        total_clusters=carve_total_clusters,
                        bitmap_data=bitmap_data,
                    )
                    # Add carved entries to results
                    from ..core.models import RecoveredEntry as RE
                    if carved_results:
                        children = [e for _, e in carved_results]
                        # Apply extension filter to carved results
                        if self.extension_filter:
                            ext_set = set(self.extension_filter)
                            children = [
                                e for e in children
                                if any(e.name.lower().endswith(ext) for ext in ext_set)
                            ]
                        if children:
                            reconstructed = RE(
                                name=f"Reconstructed ({len(children)})",
                                path="/Reconstructed",
                                is_directory=True,
                                status=FileStatus.PARTIAL,
                                confidence=0.5,
                                children=children,
                            )
                            merged.root_entries.append(reconstructed)
                            merged.total_files += len(children)
                            merged.total_deleted += len(children)
                except (ScanCancelled, NativeScanCancelled):
                    raise
                except Exception as exc:
                    logger.warning("File carving failed: %s", exc)

            self.finished.emit(merged, img_info, fs_info_last)

        except (ScanCancelled, NativeScanCancelled):
            self.error.emit("Scan cancelled.")
        except Exception as exc:
            logger.exception("Scan failed")
            self.error.emit(f"Scan failed: {exc}")

    def _run_native(self):
        """Scan using the Windows-native scanner (no pytsk3)."""
        from ..core.win_scanner import WindowsScanner
        from ..core.win_scanner import ScanCancelled as NativeScanCancelled

        drive = self.drive_letter
        if not drive:
            # Try to get drive letter from partition
            if self.partition and self.partition.drive_letter:
                drive = self.partition.drive_letter
            else:
                self.error.emit(
                    "No drive letter available for native scanning. "
                    "Select a mounted volume, or install pytsk3 for raw disk access."
                )
                return

        scanner = WindowsScanner()
        scanner.set_progress_callback(self._on_progress)
        if self.scan_size_limit_gb > 0:
            scanner._scan_size_limit_gb = self.scan_size_limit_gb
        self._native_scanner = scanner

        partition = self.partition or PartitionInfo(
            index=0, offset_bytes=0, size_bytes=0, drive_letter=drive,
        )

        try:
            result = scanner.scan_volume(
                drive, self.disk, partition, self.target_path
            )
            self.finished.emit(result, None, None)
        except NativeScanCancelled:
            self.error.emit("Scan cancelled.")
        except Exception as exc:
            logger.exception("Native scan failed")
            self.error.emit(f"Scan failed: {exc}")

    def _on_progress(self, path: str, files: int, deleted: int) -> None:
        self.progress.emit(path, files, deleted)

    def cancel(self) -> None:
        self.scanner.cancel()
        if self._native_scanner:
            self._native_scanner.cancel()


class ScanProgressPage(QWidget):
    """Page showing scan progress and stats."""

    scan_complete = Signal(object, object, object)  # ScanResult, img_info, fs_info
    scan_error = Signal(str)

    def __init__(self):
        super().__init__()
        self._worker: ScanWorker | None = None
        self._start_time = 0.0
        self._setup_ui()

        # Timer to keep elapsed clock ticking every second
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick_elapsed)

    @property
    def is_scanning(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Title
        self._title = QLabel("Scanning...")
        self._title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        self._title.setStyleSheet("color: #e0e0e0;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # Indeterminate
        self._progress.setFixedHeight(8)
        self._progress.setStyleSheet("""
            QProgressBar {
                background: #2d2d44; border: none; border-radius: 4px;
            }
            QProgressBar::chunk {
                background: #4fc3f7; border-radius: 4px;
            }
        """)
        layout.addWidget(self._progress)

        # Disk size info
        self._size_label = QLabel("")
        self._size_label.setStyleSheet("color: #4fc3f7; font-size: 13px;")
        self._size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._size_label)

        # Current path
        self._path_label = QLabel("")
        self._path_label.setStyleSheet("color: #aab4c0;")
        self._path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        # Stats
        stats_style = "color: #e0e0e0; font-size: 14px;"

        self._files_label = QLabel("Entries scanned: 0")
        self._files_label.setStyleSheet(stats_style)
        self._files_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._files_label)

        self._deleted_label = QLabel("Deleted files found: 0")
        self._deleted_label.setStyleSheet("color: #ef5350; font-size: 14px; font-weight: bold;")
        self._deleted_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._deleted_label)

        # Phase label — shows current scan phase
        self._phase_label = QLabel("")
        self._phase_label.setStyleSheet("color: #81c784; font-size: 13px;")
        self._phase_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._phase_label)

        self._time_label = QLabel("Elapsed: 0s")
        self._time_label.setStyleSheet(stats_style)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._time_label)

        # Cancel button
        self._cancel_btn = QPushButton("Cancel Scan")
        self._cancel_btn.setFixedWidth(150)
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background: #ef5350; color: white; border: none;
                padding: 8px 20px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #f44336; }
        """)
        self._cancel_btn.clicked.connect(self.cancel_scan)
        layout.addWidget(self._cancel_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch()

    def start_scan(
        self, selection: tuple, target_path: str = "/", drive_letter: str = "",
        scan_size_limit_gb: int = 0, extension_filter: list[str] | None = None,
    ) -> None:
        """Start scanning the selected disk/partition.

        Args:
            selection: (DiskInfo, PartitionInfo | None)
            target_path: Directory path to scan ("/" = full partition)
            drive_letter: Volume letter for native scanning (e.g. "C:")
            scan_size_limit_gb: Max GB to scan (0 = no limit)
            extension_filter: List of extensions to filter (e.g. [".jpg", ".png"])
        """
        disk, partition = selection
        self._start_time = time.monotonic()

        scope = f" → {target_path}" if target_path != "/" else ""
        label = drive_letter or disk.model or f"Drive {disk.index}"
        self._title.setText(f"Scanning {label}{scope}...")

        # Show total disk/partition size
        total_bytes = 0
        if partition and partition.size_bytes:
            total_bytes = partition.size_bytes
        elif disk and disk.size_bytes:
            total_bytes = disk.size_bytes
        if total_bytes > 0:
            size_text = f"Disk size: {format_size(total_bytes)}"
            if scan_size_limit_gb > 0:
                size_text += f"  ·  Scan limit: {scan_size_limit_gb} GB"
            self._size_label.setText(size_text)
        elif scan_size_limit_gb > 0:
            self._size_label.setText(f"Scan limit: {scan_size_limit_gb} GB")
        else:
            self._size_label.setText("")

        self._worker = ScanWorker(
            disk, partition, target_path=target_path, drive_letter=drive_letter,
            scan_size_limit_gb=scan_size_limit_gb,
            extension_filter=extension_filter,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        self._tick_timer.start()

    def cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _tick_elapsed(self) -> None:
        """Update elapsed time every second independent of scan callbacks."""
        elapsed = time.monotonic() - self._start_time
        self._time_label.setText(f"Elapsed: {format_duration(elapsed)}")

    @Slot(str, int, int)
    def _on_progress(self, path: str, files: int, deleted: int) -> None:
        import re

        # ── Parse phase from bracketed prefix ──
        phase_match = re.match(r"\[([^\]]+)\]\s*(.*)", path)
        if phase_match:
            phase = phase_match.group(1)
            detail = phase_match.group(2)
        else:
            phase = ""
            detail = path

        # Truncate long paths for display
        display = detail if len(detail) < 80 else "..." + detail[-77:]
        self._path_label.setText(display)

        # ── Phase label & progress bar ──
        if phase == "Directory Scan":
            self._phase_label.setText("Phase 1/3: Scanning directory tree...")
            # Indeterminate bar during directory walk
            if self._progress.maximum() != 0:
                self._progress.setRange(0, 0)

        elif phase == "Deep Scan":
            self._phase_label.setText("Phase 2/3: Deep scan for orphan deleted files...")
            pct_m = re.search(r"(\d+)%", detail)
            if pct_m:
                pct = int(pct_m.group(1))
                self._progress.setRange(0, 100)
                self._progress.setValue(pct)

        elif phase == "Carving":
            self._phase_label.setText("Phase 3/3: Carving unallocated space...")
            # Try to extract GB-based percentage
            m = re.search(
                r"(\d+\.\d+)\s*/\s*(\d+\.\d+)\s*GB\s*free\s*of\s*(\d+\.\d+)\s*GB.*?(\d+)%",
                detail,
            )
            if m:
                pct = int(m.group(4))
                self._progress.setRange(0, 100)
                self._progress.setValue(pct)
                display = f"Scanning {m.group(1)}/{m.group(2)} GB free of {m.group(3)} GB ({pct}%)"
                self._path_label.setText(display)

        elif phase in ("MFT", "Bitmap", "Recycle Bin", "USN"):
            # Native scanner phases
            self._phase_label.setText(f"Scanning: {phase}...")
            if self._progress.maximum() != 0:
                self._progress.setRange(0, 0)

        else:
            # Unknown / no-phase progress
            if phase:
                self._phase_label.setText(f"{phase}...")
            else:
                self._phase_label.setText("")

        self._files_label.setText(f"Entries scanned: {files:,}")
        self._deleted_label.setText(f"Deleted files found: {deleted:,}")

        elapsed = time.monotonic() - self._start_time
        self._time_label.setText(f"Elapsed: {format_duration(elapsed)}")

    @Slot(object, object, object)
    def _on_finished(self, result, img_info, fs_info) -> None:
        self._tick_timer.stop()
        self._title.setText("Scan Complete!")
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self.scan_complete.emit(result, img_info, fs_info)

    @Slot(str)
    def _on_error(self, error: str) -> None:
        self._tick_timer.stop()
        self.scan_error.emit(error)
