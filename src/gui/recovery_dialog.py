"""Recovery dialog — destination selection and recovery execution."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QThread, QMetaObject, Q_ARG
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.models import RecoveredEntry, RecoveryTask
from ..core.recovery import ConflictAction, RecoveryEngine, RecoveryCancelled
from ..utils.formatting import format_size, format_duration

logger = logging.getLogger(__name__)


class RecoveryWorker(QThread):
    """Background worker for file recovery."""
    progress = Signal(str, int, int, int)  # path, recovered, failed, bytes
    finished = Signal(dict)
    error = Signal(str)
    conflict = Signal(str)  # absolute path of conflicting file

    def __init__(self, engine: RecoveryEngine, task: RecoveryTask):
        super().__init__()
        self._engine = engine
        self._task = task
        # Thread-safe mechanism for conflict resolution
        self._conflict_event = threading.Event()
        self._conflict_response: ConflictAction = ConflictAction.SKIP

    def run(self):
        try:
            self._engine.set_progress_callback(self._on_progress)
            self._engine.set_conflict_handler(self._on_conflict)
            stats = self._engine.recover(self._task)
            self.finished.emit(stats)
        except RecoveryCancelled:
            self.error.emit("Recovery cancelled by user.")
        except Exception as exc:
            logger.exception("Recovery failed")
            self.error.emit(f"Recovery failed: {exc}")

    def _on_progress(self, path: str, recovered: int, failed: int, bytes_written: int) -> None:
        self.progress.emit(path, recovered, failed, bytes_written)

    def _on_conflict(self, target: Path) -> ConflictAction:
        """Called from the engine thread when a file conflict occurs.

        Emits the conflict signal and blocks until the UI responds.
        """
        self._conflict_event.clear()
        self.conflict.emit(str(target))
        self._conflict_event.wait()  # blocks until resolve_conflict is called
        return self._conflict_response

    def resolve_conflict(self, action: ConflictAction) -> None:
        """Called from the UI thread to unblock the engine."""
        self._conflict_response = action
        self._conflict_event.set()

    def cancel(self) -> None:
        self._engine.cancel()
        # Unblock if waiting on a conflict dialog
        self._conflict_event.set()


class RecoveryPage(QWidget):
    """Page for configuring and executing recovery."""

    recovery_complete = Signal(dict)

    def __init__(self):
        super().__init__()
        self._entries: list[RecoveredEntry] = []
        self._fs_info = None
        self._drive_letter = ""
        self._worker: RecoveryWorker | None = None
        self._start_time = 0.0
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        # Title
        title = QLabel("Recover Files")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(title)

        # Summary
        self._summary = QLabel("No files selected")
        self._summary.setStyleSheet("color: #aab4c0; font-size: 13px;")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        # Destination selector
        dest_layout = QHBoxLayout()
        dest_label = QLabel("Destination:")
        dest_label.setStyleSheet("color: #e0e0e0; font-weight: bold;")
        dest_layout.addWidget(dest_label)

        self._dest_input = QLineEdit()
        self._dest_input.setPlaceholderText("Select destination folder...")
        self._dest_input.setReadOnly(True)
        self._dest_input.setStyleSheet("""
            QLineEdit {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 8px; font-size: 13px;
            }
        """)
        dest_layout.addWidget(self._dest_input, 1)

        browse_btn = QPushButton("Browse...")
        browse_btn.setStyleSheet("""
            QPushButton {
                background: #4fc3f7; color: #1e1e2e; border: none;
                padding: 8px 20px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #81d4fa; }
        """)
        browse_btn.clicked.connect(self._browse_destination)
        dest_layout.addWidget(browse_btn)
        layout.addLayout(dest_layout)

        # Options
        options_layout = QHBoxLayout()

        self._preserve_check = QCheckBox("Preserve folder structure")
        self._preserve_check.setChecked(True)
        self._preserve_check.setStyleSheet("color: #e0e0e0;")
        options_layout.addWidget(self._preserve_check)

        conflict_label = QLabel("If file exists:")
        conflict_label.setStyleSheet("color: #e0e0e0; font-weight: bold;")
        options_layout.addWidget(conflict_label)

        self._conflict_combo = QComboBox()
        self._conflict_combo.addItems(["Ask me", "Replace all", "Skip all", "Duplicate all"])
        self._conflict_combo.setCurrentIndex(0)
        self._conflict_combo.setStyleSheet("""
            QComboBox {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 4px 8px; min-width: 110px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #2d2d44; color: #e0e0e0;
                selection-background-color: #4fc3f7;
            }
        """)
        options_layout.addWidget(self._conflict_combo)

        options_layout.addStretch()
        layout.addLayout(options_layout)

        # Start button
        self._start_btn = QPushButton("Start Recovery")
        self._start_btn.setFixedWidth(200)
        self._start_btn.setStyleSheet("""
            QPushButton {
                background: #66bb6a; color: white; border: none;
                padding: 10px 30px; border-radius: 4px; font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background: #81c784; }
            QPushButton:disabled { background: #3d3d5c; color: #666; }
        """)
        self._start_btn.clicked.connect(self._start_recovery)
        layout.addWidget(self._start_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # Progress section (hidden initially)
        self._progress_widget = QWidget()
        progress_layout = QVBoxLayout(self._progress_widget)
        progress_layout.setContentsMargins(0, 0, 0, 0)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                background: #2d2d44; border: none; border-radius: 4px;
            }
            QProgressBar::chunk {
                background: #66bb6a; border-radius: 4px;
            }
        """)
        progress_layout.addWidget(self._progress_bar)

        self._current_file_label = QLabel("")
        self._current_file_label.setStyleSheet("color: #aab4c0;")
        progress_layout.addWidget(self._current_file_label)

        stats_layout = QHBoxLayout()
        self._recovered_label = QLabel("Recovered: 0")
        self._recovered_label.setStyleSheet("color: #81c784;")
        stats_layout.addWidget(self._recovered_label)

        self._failed_label = QLabel("Failed: 0")
        self._failed_label.setStyleSheet("color: #ef5350;")
        stats_layout.addWidget(self._failed_label)

        self._bytes_label = QLabel("Data: 0 B")
        self._bytes_label.setStyleSheet("color: #e0e0e0;")
        stats_layout.addWidget(self._bytes_label)

        self._time_label = QLabel("Time: 0s")
        self._time_label.setStyleSheet("color: #e0e0e0;")
        stats_layout.addWidget(self._time_label)

        stats_layout.addStretch()
        progress_layout.addLayout(stats_layout)

        self._progress_widget.hide()
        layout.addWidget(self._progress_widget)

        # Cancel button
        self._cancel_btn = QPushButton("Cancel Recovery")
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background: #ef5350; color: white; border: none;
                padding: 8px 20px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #f44336; }
        """)
        self._cancel_btn.clicked.connect(self._cancel_recovery)
        self._cancel_btn.hide()
        layout.addWidget(self._cancel_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # Log output
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(150)
        self._log.setStyleSheet("""
            QTextEdit {
                background: #1a1a2e; color: #aab4c0; border: 1px solid #3d3d5c;
                border-radius: 4px; font-family: Consolas, monospace; font-size: 11px;
            }
        """)
        layout.addWidget(self._log)

        layout.addStretch()

    def set_entries(
        self, entries: list[RecoveredEntry], fs_info, drive_letter: str = ""
    ) -> None:
        """Set the entries to recover and the file system reference."""
        self._entries = entries
        self._fs_info = fs_info
        self._drive_letter = drive_letter

        total_size = sum(e.size_bytes for e in entries if not e.is_directory)
        dirs = sum(1 for e in entries if e.is_directory)
        files = len(entries) - dirs

        self._summary.setText(
            f"Selected: {files} files, {dirs} folders | "
            f"Total size: {format_size(total_size)}"
        )

    def _browse_destination(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Destination Folder",
            "",
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self._dest_input.setText(path)

    def _start_recovery(self) -> None:
        dest = self._dest_input.text().strip()
        if not dest:
            QMessageBox.warning(
                self, "No Destination",
                "Please select a destination folder first.",
            )
            return

        if not self._entries:
            QMessageBox.warning(self, "No Files", "No files selected for recovery.")
            return

        if self._fs_info is None and not self._drive_letter:
            QMessageBox.critical(
                self, "Error",
                "File system reference is not available. Please re-scan.",
            )
            return

        # Confirm
        reply = QMessageBox.question(
            self, "Start Recovery",
            f"Recover {len(self._entries)} items to:\n{dest}\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Determine conflict policy from dropdown
        conflict_idx = self._conflict_combo.currentIndex()
        overwrite_all = (conflict_idx == 1)  # "Replace all"

        task = RecoveryTask(
            entries=self._entries,
            destination=dest,
            preserve_structure=self._preserve_check.isChecked(),
            overwrite_existing=overwrite_all,
        )

        if self._fs_info is not None:
            engine = RecoveryEngine(self._fs_info)
        else:
            from ..core.recovery import NativeRecoveryEngine
            engine = NativeRecoveryEngine(self._drive_letter)

        # Pre-set conflict policy based on dropdown
        if conflict_idx == 2:
            engine._conflict_policy = ConflictAction.SKIP_ALL
        elif conflict_idx == 3:
            engine._conflict_policy = ConflictAction.DUPLICATE_ALL

        self._start_time = time.monotonic()
        self._start_btn.setEnabled(False)
        self._progress_widget.show()
        self._cancel_btn.show()
        self._progress_bar.setRange(0, len(self._entries))
        self._progress_bar.setValue(0)
        self._log.clear()
        self._log.append("Starting recovery...")

        self._worker = RecoveryWorker(engine, task)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        # Only wire conflict dialog when "Ask me" is selected
        if conflict_idx == 0:
            self._worker.conflict.connect(self._on_conflict)
        self._worker.start()

    def _cancel_recovery(self) -> None:
        if self._worker:
            self._worker.cancel()

    @Slot(str, int, int, int)
    def _on_progress(self, path: str, recovered: int, failed: int, bytes_written: int) -> None:
        self._progress_bar.setValue(recovered + failed)
        display = path if len(path) < 60 else "..." + path[-57:]
        self._current_file_label.setText(display)
        self._recovered_label.setText(f"Recovered: {recovered}")
        self._failed_label.setText(f"Failed: {failed}")
        self._bytes_label.setText(f"Data: {format_size(bytes_written)}")

        elapsed = time.monotonic() - self._start_time
        self._time_label.setText(f"Time: {format_duration(elapsed)}")

        self._log.append(f"✓ {path}")

    @Slot(str)
    def _on_conflict(self, target_path: str) -> None:
        """Show a dialog asking the user what to do with a conflicting file."""
        short = target_path if len(target_path) < 80 else "..." + target_path[-77:]
        msg = QMessageBox(self)
        msg.setWindowTitle("File Already Exists")
        msg.setText(f"The following file already exists:\n\n{short}")
        msg.setInformativeText("What would you like to do?")

        replace_btn = msg.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        duplicate_btn = msg.addButton("Duplicate", QMessageBox.ButtonRole.ActionRole)
        skip_btn = msg.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
        replace_all_btn = msg.addButton("Replace All", QMessageBox.ButtonRole.YesRole)
        duplicate_all_btn = msg.addButton("Duplicate All", QMessageBox.ButtonRole.ActionRole)
        skip_all_btn = msg.addButton("Skip All", QMessageBox.ButtonRole.NoRole)

        msg.setDefaultButton(duplicate_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == replace_btn:
            action = ConflictAction.REPLACE
        elif clicked == duplicate_btn:
            action = ConflictAction.DUPLICATE
        elif clicked == replace_all_btn:
            action = ConflictAction.REPLACE_ALL
        elif clicked == duplicate_all_btn:
            action = ConflictAction.DUPLICATE_ALL
        elif clicked == skip_all_btn:
            action = ConflictAction.SKIP_ALL
        else:
            action = ConflictAction.SKIP

        if self._worker:
            self._worker.resolve_conflict(action)

    @Slot(dict)
    def _on_finished(self, stats: dict) -> None:
        self._start_btn.setEnabled(True)
        self._cancel_btn.hide()
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._current_file_label.setText("Recovery complete!")
        skipped = stats.get('files_skipped', 0)
        summary = (
            f"\n--- Recovery Complete ---\n"
            f"Recovered: {stats['files_recovered']}\n"
            f"Skipped: {skipped}\n"
            f"Failed: {stats['files_failed']}\n"
            f"Data: {format_size(stats['bytes_recovered'])}"
        )
        self._log.append(summary)
        self.recovery_complete.emit(stats)

    @Slot(str)
    def _on_error(self, error: str) -> None:
        self._start_btn.setEnabled(True)
        self._cancel_btn.hide()
        self._log.append(f"\n--- Error ---\n{error}")
        QMessageBox.critical(self, "Recovery Error", error)
