"""Main application window — wizard-style data recovery flow."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .disk_selector import DiskSelectorPage
from .scan_progress import ScanProgressPage
from .tree_view import TreeViewPage
from .recovery_dialog import RecoveryPage
from ..core.session import save_session, load_session

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window with wizard-style navigation."""

    WINDOW_TITLE = "DataForge Recovery"
    MIN_WIDTH = 1100
    MIN_HEIGHT = 700

    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)

        self._scan_result = None
        self._img_info = None
        self._fs_info = None
        self._drive_letter = ""

        self._setup_ui()
        self._setup_styles()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = self._create_header()
        layout.addWidget(header)

        # Stacked pages
        self._stack = QStackedWidget()

        self._disk_page = DiskSelectorPage()
        self._scan_page = ScanProgressPage()
        self._tree_page = TreeViewPage()
        self._recovery_page = RecoveryPage()

        self._stack.addWidget(self._disk_page)     # 0
        self._stack.addWidget(self._scan_page)      # 1
        self._stack.addWidget(self._tree_page)       # 2
        self._stack.addWidget(self._recovery_page)   # 3

        layout.addWidget(self._stack, 1)

        # Navigation bar
        nav = self._create_nav_bar()
        layout.addWidget(nav)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Select a disk or partition to scan")

        # Connect signals
        self._disk_page.disk_selected.connect(self._on_disk_selected)
        self._scan_page.scan_complete.connect(self._on_scan_complete)
        self._scan_page.scan_error.connect(self._on_scan_error)
        self._tree_page.recover_requested.connect(self._on_recover_requested)
        self._recovery_page.recovery_complete.connect(self._on_recovery_complete)

    def _create_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 12, 20, 12)

        title = QLabel("DataForge Recovery")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        layout.addWidget(title)

        layout.addStretch()

        subtitle = QLabel("Bit-level Data Recovery Tool")
        subtitle.setStyleSheet("color: #aab4c0;")
        layout.addWidget(subtitle)

        return header

    def _create_nav_bar(self) -> QWidget:
        nav = QWidget()
        nav.setObjectName("navbar")
        layout = QHBoxLayout(nav)
        layout.setContentsMargins(20, 8, 20, 8)

        self._btn_back = QPushButton("← Back")
        self._btn_back.setEnabled(False)
        self._btn_back.clicked.connect(self._go_back)
        layout.addWidget(self._btn_back)

        layout.addStretch()

        # Step indicators
        self._step_labels = []
        steps = ["1. Select Disk", "2. Scan", "3. Browse Files", "4. Recover"]
        for text in steps:
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #666; padding: 0 10px;")
            layout.addWidget(lbl)
            self._step_labels.append(lbl)
        self._update_step_indicators(0)

        layout.addStretch()

        self._btn_next = QPushButton("Scan →")
        self._btn_next.setEnabled(False)
        self._btn_next.clicked.connect(self._go_next)
        layout.addWidget(self._btn_next)

        return nav

    def _update_step_indicators(self, current: int) -> None:
        for i, lbl in enumerate(self._step_labels):
            if i == current:
                lbl.setStyleSheet("color: #4fc3f7; font-weight: bold; padding: 0 10px;")
            elif i < current:
                lbl.setStyleSheet("color: #81c784; padding: 0 10px;")
            else:
                lbl.setStyleSheet("color: #666; padding: 0 10px;")

    def _setup_styles(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #1e1e2e; }
            #header { background: #2d2d44; border-bottom: 1px solid #3d3d5c; }
            #navbar { background: #252540; border-top: 1px solid #3d3d5c; }
            QPushButton {
                background: #4fc3f7; color: #1e1e2e; border: none;
                padding: 8px 20px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #81d4fa; }
            QPushButton:disabled { background: #3d3d5c; color: #666; }
            QStatusBar { background: #252540; color: #aab4c0; }
        """)

    # --- Navigation ---

    def _go_back(self) -> None:
        idx = self._stack.currentIndex()
        if idx > 0:
            if idx == 1 and self._scan_page.is_scanning:
                reply = QMessageBox.question(
                    self, "Cancel Scan",
                    "A scan is in progress. Cancel it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    return
                self._scan_page.cancel_scan()

            self._stack.setCurrentIndex(idx - 1)
            self._update_nav(idx - 1)

    def _go_next(self) -> None:
        idx = self._stack.currentIndex()
        if idx == 0:
            selection = self._disk_page.get_selection()
            target_path = self._disk_page.get_target_path()
            scan_size_limit_gb = self._disk_page.get_scan_size_limit_gb()
            extension_filter = self._disk_page.get_extension_filter()
            if selection and selection[0] == "session":
                # Load saved session — skip scanning entirely
                self._load_from_session(selection[1])
            elif selection and selection[0] == "volume":
                # Volume selected — use native scanner with drive letter
                vol = selection[1]
                drive_letter = vol["letter"]
                self._drive_letter = drive_letter
                from ..core.models import DiskInfo, PartitionInfo, FileSystemType
                disk = DiskInfo(
                    index=-1,
                    model=f"Volume {drive_letter}",
                    serial=str(vol.get("serial", "")),
                )
                fs = vol.get("fs_type", "")
                try:
                    fs_type = FileSystemType(fs) if fs else FileSystemType.UNKNOWN
                except ValueError:
                    fs_type = FileSystemType.UNKNOWN
                partition = PartitionInfo(
                    index=0, offset_bytes=0,
                    size_bytes=vol.get("total_bytes", 0),
                    fs_type=fs_type,
                    label=vol.get("label", ""),
                    drive_letter=drive_letter,
                )
                disk.size_bytes = vol.get("total_bytes", 0)
                self._stack.setCurrentIndex(1)
                self._update_nav(1)
                self._scan_page.start_scan(
                    (disk, partition),
                    target_path=target_path,
                    drive_letter=drive_letter,
                    scan_size_limit_gb=scan_size_limit_gb,
                    extension_filter=extension_filter,
                )
            elif selection:
                # Physical disk/partition selected
                disk, partition = selection
                self._drive_letter = (
                    partition.drive_letter if partition and partition.drive_letter else ""
                )
                cached = load_session(disk, partition)
                if cached and cached.target_path == target_path:
                    reply = QMessageBox.question(
                        self, "Saved Session Found",
                        f"A previous scan session exists for this drive "
                        f"({cached.total_files} files found, path: {cached.target_path}).\n\n"
                        f"Load the saved session instead of re-scanning?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply == QMessageBox.StandardButton.Yes:
                        self._scan_result = cached
                        self._reopen_fs_from_result(cached)
                        self._tree_page.load_scan_result(cached)
                        self._stack.setCurrentIndex(2)
                        self._update_nav(2)
                        self._status.showMessage(
                            f"Session loaded: {cached.total_files} files "
                            f"({cached.total_deleted} deleted)"
                        )
                        return
                # No session or user chose to rescan
                self._stack.setCurrentIndex(1)
                self._update_nav(1)
                self._scan_page.start_scan(
                    (disk, partition),
                    target_path=target_path,
                    drive_letter=self._drive_letter,
                    scan_size_limit_gb=scan_size_limit_gb,
                    extension_filter=extension_filter,
                )
        elif idx == 2:
            # Go to recovery
            entries = self._tree_page.get_selected_entries()
            if entries:
                self._stack.setCurrentIndex(3)
                self._update_nav(3)
                self._recovery_page.set_entries(
                    entries, self._fs_info, self._drive_letter
                )

    def _load_from_session(self, session_dict: dict) -> None:
        """Load a scan result from a saved session dict."""
        from ..core.session import _result_from_dict
        import json
        from pathlib import Path

        file_path = session_dict.get("file_path")
        if not file_path or not Path(file_path).exists():
            QMessageBox.warning(self, "Session Error", "Session file not found.")
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = _result_from_dict(data["scan_result"])
            self._scan_result = result
            self._reopen_fs_from_result(result)
            self._tree_page.load_scan_result(result)
            self._stack.setCurrentIndex(2)
            self._update_nav(2)
            self._status.showMessage(
                f"Session loaded: {result.total_files} files "
                f"({result.total_deleted} deleted)"
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Session Error", f"Failed to load session: {exc}"
            )

    def _update_nav(self, idx: int) -> None:
        self._update_step_indicators(idx)
        self._btn_back.setEnabled(idx > 0)

        if idx == 0:
            self._btn_next.setText("Scan →")
            self._btn_next.setEnabled(self._disk_page.get_selection() is not None)
        elif idx == 1:
            self._btn_next.setText("Scan →")
            self._btn_next.setEnabled(False)
        elif idx == 2:
            self._btn_next.setText("Recover →")
            self._btn_next.setEnabled(True)
        elif idx == 3:
            self._btn_next.setText("Recover →")
            self._btn_next.setEnabled(False)

    # --- Signal handlers ---

    def _on_disk_selected(self, has_selection: bool) -> None:
        self._btn_next.setEnabled(has_selection)

    def _on_scan_complete(self, scan_result, img_info, fs_info) -> None:
        self._scan_result = scan_result
        self._img_info = img_info
        self._fs_info = fs_info

        # Auto-save session so this scan can be reloaded later
        try:
            session_path = save_session(scan_result)
            logger.info("Scan session auto-saved: %s", session_path)
        except Exception as exc:
            logger.warning("Failed to auto-save session: %s", exc)

        self._tree_page.load_scan_result(scan_result)
        self._stack.setCurrentIndex(2)
        self._update_nav(2)
        self._status.showMessage(
            f"Scan complete: {scan_result.total_files} files found "
            f"({scan_result.total_deleted} deleted) — Session saved"
        )

    def _on_scan_error(self, error_msg: str) -> None:
        QMessageBox.critical(self, "Scan Error", error_msg)
        self._stack.setCurrentIndex(0)
        self._update_nav(0)

    def _reopen_fs_from_result(self, result) -> None:
        """Re-open pytsk3 img_info/fs_info from a loaded ScanResult.

        This restores the file-system handles that are needed for recovery
        but cannot be serialised to JSON.
        """
        # Derive drive letter
        dl = ""
        if result.partition and result.partition.drive_letter:
            dl = result.partition.drive_letter
        self._drive_letter = dl

        if not dl:
            return  # nothing we can reconnect to

        try:
            import pytsk3
            from ..core.disk import open_disk_image
            device_path = f"\\\\.\\{dl}"
            img_info = open_disk_image(device_path)
            fs_info = pytsk3.FS_Info(img_info, offset=0)
            self._img_info = img_info
            self._fs_info = fs_info
            logger.info("Reopened FS handles for %s from saved session", dl)
        except Exception as exc:
            logger.warning("Could not reopen FS handles for %s: %s", dl, exc)
            self._img_info = None
            self._fs_info = None

    def _on_recover_requested(self, entries) -> None:
        self._stack.setCurrentIndex(3)
        self._update_nav(3)
        self._recovery_page.set_entries(entries, self._fs_info, self._drive_letter)

    def _on_recovery_complete(self, stats: dict) -> None:
        skipped = stats.get('files_skipped', 0)
        QMessageBox.information(
            self, "Recovery Complete",
            f"Files recovered: {stats['files_recovered']}\n"
            f"Files skipped: {skipped}\n"
            f"Files failed: {stats['files_failed']}\n"
            f"Bytes recovered: {stats['bytes_recovered']:,}",
        )
        self._status.showMessage("Recovery complete")
