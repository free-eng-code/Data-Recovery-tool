"""Disk and partition selector page."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot, QThread
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.disk import enumerate_physical_drives, get_logical_volumes
from ..core.models import DiskInfo, PartitionInfo
from ..core.session import list_sessions, load_session, _session_id
from ..utils.formatting import format_size

logger = logging.getLogger(__name__)


class DiskEnumWorker(QThread):
    """Background worker for disk enumeration."""
    finished = Signal(list, list)  # disks, volumes
    error = Signal(str)

    def run(self):
        try:
            disks = enumerate_physical_drives()
            volumes = get_logical_volumes()
            self.finished.emit(disks, volumes)
        except Exception as exc:
            self.error.emit(str(exc))


class DiskSelectorPage(QWidget):
    """Page for selecting a disk or partition to scan."""

    disk_selected = Signal(bool)  # has_selection

    def __init__(self):
        super().__init__()
        self._disks: list[DiskInfo] = []
        self._volumes: list[dict] = []
        self._worker: DiskEnumWorker | None = None
        self._setup_ui()
        self._start_enumeration()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Title
        title = QLabel("Select Drive or Partition")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(title)

        desc = QLabel(
            "Choose a physical drive or partition to scan for deleted and lost files. "
            "The scan is read-only and will not modify the source.\n"
            "Optionally specify a directory path to scan only that folder instead of the entire partition."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aab4c0;")
        layout.addWidget(desc)

        # Optional: directory scope
        dir_layout = QHBoxLayout()
        dir_label = QLabel("Scan directory (optional):")
        dir_label.setStyleSheet("color: #e0e0e0;")
        dir_layout.addWidget(dir_label)

        self._dir_input = QLineEdit()
        self._dir_input.setPlaceholderText("/ (entire partition) — or type e.g. /Users/Documents")
        self._dir_input.setStyleSheet("""
            QLineEdit {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 6px;
            }
        """)
        dir_layout.addWidget(self._dir_input, 1)

        self._browse_btn = QPushButton("Browse...")
        self._browse_btn.setStyleSheet("""
            QPushButton {
                background: #4fc3f7; color: #1e1e2e; border: none;
                padding: 6px 16px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #81d4fa; }
        """)
        self._browse_btn.clicked.connect(self._browse_scan_directory)
        dir_layout.addWidget(self._browse_btn)
        layout.addLayout(dir_layout)

        # Scan size limit
        size_layout = QHBoxLayout()
        size_label = QLabel("Scan size limit (GB):")
        size_label.setStyleSheet("color: #e0e0e0;")
        size_layout.addWidget(size_label)

        self._size_input = QSpinBox()
        self._size_input.setRange(0, 999999)
        self._size_input.setValue(0)
        self._size_input.setSuffix(" GB")
        self._size_input.setSpecialValueText("Entire disk (no limit)")
        self._size_input.setToolTip(
            "Set a limit on how much of the disk to scan (in GB).\n"
            "0 = scan the entire disk/partition. Useful for very large drives."
        )
        self._size_input.setFixedWidth(220)
        self._size_input.setStyleSheet("""
            QSpinBox {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 6px;
            }
        """)
        size_layout.addWidget(self._size_input)
        size_layout.addStretch()
        layout.addLayout(size_layout)

        # File extension filter
        ext_layout = QHBoxLayout()
        ext_label = QLabel("File extensions (optional):")
        ext_label.setStyleSheet("color: #e0e0e0;")
        ext_layout.addWidget(ext_label)

        self._ext_input = QLineEdit()
        self._ext_input.setPlaceholderText(
            "e.g.  .jpg .png .docx  — leave empty to recover all file types"
        )
        self._ext_input.setToolTip(
            "Space-separated list of file extensions to recover.\n"
            "Examples: .jpg .png .pdf\n"
            "Leave empty to recover all file types."
        )
        self._ext_input.setStyleSheet("""
            QLineEdit {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 6px;
            }
        """)
        ext_layout.addWidget(self._ext_input, 1)
        layout.addLayout(ext_layout)

        # Loading indicator
        self._loading = QLabel("Enumerating drives...")
        self._loading.setStyleSheet("color: #4fc3f7;")
        layout.addWidget(self._loading)

        # Physical drives tree
        drives_group = QGroupBox("Physical Drives")
        drives_group.setStyleSheet("""
            QGroupBox {
                color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; margin-top: 8px; padding-top: 16px;
            }
            QGroupBox::title { subcontrol-position: top left; padding: 4px 8px; }
        """)
        drives_layout = QVBoxLayout(drives_group)

        self._drives_tree = QTreeWidget()
        self._drives_tree.setHeaderLabels([
            "Drive", "Model", "Size", "Partition Table", "Partitions",
        ])
        self._drives_tree.setRootIsDecorated(True)
        self._drives_tree.setAlternatingRowColors(True)
        self._drives_tree.setStyleSheet("""
            QTreeWidget {
                background: #2d2d44; color: #e0e0e0; border: none;
                alternate-background-color: #32324d;
            }
            QTreeWidget::item:selected { background: #4a4a6a; }
            QHeaderView::section {
                background: #252540; color: #aab4c0; border: none;
                padding: 6px; font-weight: bold;
            }
        """)
        header = self._drives_tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._drives_tree.itemSelectionChanged.connect(self._on_selection_changed)
        drives_layout.addWidget(self._drives_tree)
        layout.addWidget(drives_group, 1)

        # Saved sessions group
        sessions_group = QGroupBox("Saved Scan Sessions (click to load without re-scanning)")
        sessions_group.setStyleSheet(drives_group.styleSheet())
        sessions_layout = QVBoxLayout(sessions_group)

        self._sessions_tree = QTreeWidget()
        self._sessions_tree.setHeaderLabels([
            "Disk", "Partition", "File System", "Files", "Deleted", "Saved At",
        ])
        self._sessions_tree.setRootIsDecorated(False)
        self._sessions_tree.setAlternatingRowColors(True)
        self._sessions_tree.setStyleSheet(self._drives_tree.styleSheet())
        self._sessions_tree.itemSelectionChanged.connect(self._on_session_selection_changed)
        sessions_layout.addWidget(self._sessions_tree)
        layout.addWidget(sessions_group)

        # Logical volumes (selectable for native scanning)
        volumes_group = QGroupBox("Mounted Volumes (select for quick scan)")
        volumes_group.setStyleSheet(drives_group.styleSheet())
        vol_layout = QVBoxLayout(volumes_group)

        self._volumes_tree = QTreeWidget()
        self._volumes_tree.setHeaderLabels([
            "Drive", "Label", "File System", "Size", "Free", "Type",
        ])
        self._volumes_tree.setRootIsDecorated(False)
        self._volumes_tree.setAlternatingRowColors(True)
        self._volumes_tree.setStyleSheet(self._drives_tree.styleSheet())
        self._volumes_tree.itemSelectionChanged.connect(self._on_volume_selection_changed)
        self._volumes_tree.itemChanged.connect(self._on_volume_check_changed)
        vol_layout.addWidget(self._volumes_tree)
        layout.addWidget(volumes_group)

    def _start_enumeration(self) -> None:
        self._worker = DiskEnumWorker()
        self._worker.finished.connect(self._on_enum_complete)
        self._worker.error.connect(self._on_enum_error)
        self._worker.start()

    @Slot(list, list)
    def _on_enum_complete(self, disks: list[DiskInfo], volumes: list[dict]) -> None:
        self._disks = disks
        self._volumes = volumes
        self._loading.hide()
        self._populate_drives()
        self._populate_volumes()
        self._populate_sessions()

    @Slot(str)
    def _on_enum_error(self, error: str) -> None:
        self._loading.setText(f"Error: {error}")
        self._loading.setStyleSheet("color: #ef5350;")

    def _populate_drives(self) -> None:
        self._drives_tree.clear()
        for disk in self._disks:
            item = QTreeWidgetItem([
                f"PhysicalDrive{disk.index}",
                disk.model or "Unknown",
                disk.size_display,
                disk.partition_scheme.value,
                str(len(disk.partitions)),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, ("disk", disk))

            for part in disk.partitions:
                child = QTreeWidgetItem([
                    f"  Partition {part.index}",
                    part.fs_type.value,
                    part.size_display,
                    part.drive_letter or "",
                    "",
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, ("partition", disk, part))
                item.addChild(child)

            self._drives_tree.addTopLevelItem(item)
            item.setExpanded(True)

    def _populate_volumes(self) -> None:
        self._volumes_tree.blockSignals(True)
        self._volumes_tree.clear()
        for vol in self._volumes:
            total_b = vol.get("total_bytes", 0)
            free_b = vol.get("free_bytes", 0)
            item = QTreeWidgetItem(self._volumes_tree, [
                vol["letter"],
                vol.get("label", ""),
                vol.get("fs_type", ""),
                format_size(total_b) if total_b else "",
                format_size(free_b) if free_b else "",
                vol.get("type", ""),
            ])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, ("volume", vol))
        self._volumes_tree.blockSignals(False)

    def _on_selection_changed(self) -> None:
        # Clear other selections
        self._sessions_tree.clearSelection()
        self._volumes_tree.clearSelection()
        items = self._drives_tree.selectedItems()
        self.disk_selected.emit(len(items) > 0)
        # Auto-fill scan size from selected item
        if items:
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "partition":
                gb = data[2].size_bytes / (1024 ** 3)
                self._size_input.setValue(0)  # default to full
                self._size_input.setToolTip(
                    f"Partition size: {gb:.1f} GB. Set 0 to scan all."
                )
            elif data and data[0] == "disk":
                gb = data[1].size_bytes / (1024 ** 3)
                self._size_input.setValue(0)
                self._size_input.setToolTip(
                    f"Disk size: {gb:.1f} GB. Set 0 to scan all."
                )

    def _on_session_selection_changed(self) -> None:
        # Clear other selections
        self._drives_tree.clearSelection()
        self._volumes_tree.clearSelection()
        items = self._sessions_tree.selectedItems()
        self.disk_selected.emit(len(items) > 0)

    def _on_volume_selection_changed(self) -> None:
        # Clear other selections
        self._drives_tree.clearSelection()
        self._sessions_tree.clearSelection()
        items = self._volumes_tree.selectedItems()
        has = len(items) > 0 or self._has_checked_volume()
        self.disk_selected.emit(has)
        # Auto-fill scan size tooltip
        if items:
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "volume":
                total_b = data[1].get("total_bytes", 0)
                gb = total_b / (1024 ** 3) if total_b else 0
                if gb > 0:
                    self._size_input.setToolTip(
                        f"Volume size: {gb:.1f} GB. Set 0 to scan all."
                    )

    def _on_volume_check_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle volume checkbox toggle — enforce single-check."""
        if column != 0:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            # Uncheck all other volumes
            self._volumes_tree.blockSignals(True)
            for i in range(self._volumes_tree.topLevelItemCount()):
                other = self._volumes_tree.topLevelItem(i)
                if other is not item:
                    other.setCheckState(0, Qt.CheckState.Unchecked)
            self._volumes_tree.blockSignals(False)
            # Clear other selections
            self._drives_tree.clearSelection()
            self._sessions_tree.clearSelection()
        self.disk_selected.emit(self._has_checked_volume())

    def _has_checked_volume(self) -> bool:
        for i in range(self._volumes_tree.topLevelItemCount()):
            if self._volumes_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked:
                return True
        return False

    def _get_checked_volume(self) -> QTreeWidgetItem | None:
        for i in range(self._volumes_tree.topLevelItemCount()):
            item = self._volumes_tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                return item
        return None

    def _browse_scan_directory(self) -> None:
        """Open a folder picker to choose the scan directory."""
        # Pre-fill starting dir from checked volume if available
        start_dir = ""
        checked = self._get_checked_volume()
        if checked:
            data = checked.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "volume":
                start_dir = data[1]["letter"] + "\\"

        path = QFileDialog.getExistingDirectory(
            self,
            "Select Directory to Scan",
            start_dir,
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            # Convert Windows path to virtual path relative to volume root
            path = path.replace("\\", "/")
            # If the path starts with a volume letter like D:/, strip it
            if len(path) >= 2 and path[1] == ":":
                drive_part = path[:2]  # e.g. "D:"
                remainder = path[2:]   # e.g. "/Users/Docs"
                # Auto-check the matching volume
                self._auto_check_volume(drive_part)
                self._dir_input.setText(remainder if remainder else "/")
            else:
                self._dir_input.setText(path)

    def _auto_check_volume(self, drive_letter: str) -> None:
        """Check the volume matching the given drive letter."""
        self._volumes_tree.blockSignals(True)
        for i in range(self._volumes_tree.topLevelItemCount()):
            item = self._volumes_tree.topLevelItem(i)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "volume" and data[1]["letter"] == drive_letter:
                item.setCheckState(0, Qt.CheckState.Checked)
            else:
                item.setCheckState(0, Qt.CheckState.Unchecked)
        self._volumes_tree.blockSignals(False)
        self._drives_tree.clearSelection()
        self._sessions_tree.clearSelection()
        self.disk_selected.emit(True)

    def _populate_sessions(self) -> None:
        """Populate the saved sessions tree."""
        self._sessions_tree.clear()
        sessions = list_sessions()
        for s in sessions:
            saved = s.get("saved_at", "")
            if saved:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(saved)
                    saved = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            item = QTreeWidgetItem([
                f"Drive {s.get('disk_index', '?')} — {s.get('disk_model', 'Unknown')}",
                f"Partition {s.get('partition_index', '?')}",
                s.get("fs_type", ""),
                str(s.get("total_files", 0)),
                str(s.get("total_deleted", 0)),
                saved,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, ("session", s))
            # Color session items in a distinct way
            for col in range(6):
                item.setForeground(col, QBrush(QColor("#81d4fa")))
            self._sessions_tree.addTopLevelItem(item)

    def get_selection(self) -> tuple | None:
        """Get the selected disk/partition, volume, or session.

        Returns:
            ("session", session_dict) for saved sessions,
            ("volume", volume_dict) for mounted volumes,
            (DiskInfo, PartitionInfo | None) for physical drives,
            or None.
        """
        # Check sessions first
        session_items = self._sessions_tree.selectedItems()
        if session_items:
            data = session_items[0].data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "session":
                return data  # ("session", session_dict)

        # Check volumes — prefer checked item over selected
        checked_vol = self._get_checked_volume()
        if checked_vol:
            data = checked_vol.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "volume":
                return data  # ("volume", volume_dict)

        volume_items = self._volumes_tree.selectedItems()
        if volume_items:
            data = volume_items[0].data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "volume":
                return data  # ("volume", volume_dict)

        # Then check drives
        items = self._drives_tree.selectedItems()
        if not items:
            return None

        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return None

        if data[0] == "disk":
            return (data[1], None)
        elif data[0] == "partition":
            return (data[1], data[2])
        return None

    def get_scan_size_limit_gb(self) -> int:
        """Get the scan size limit in GB (0 = no limit)."""
        return self._size_input.value()

    def get_extension_filter(self) -> list[str]:
        """Get the list of file extensions to filter by.

        Returns:
            List of lowercase extensions like [".jpg", ".png"], or empty list for no filter.
        """
        text = self._ext_input.text().strip()
        if not text:
            return []
        parts = text.replace(",", " ").split()
        exts: list[str] = []
        for p in parts:
            p = p.strip().lower()
            if p and not p.startswith("."):
                p = "." + p
            if p:
                exts.append(p)
        return exts

    def get_target_path(self) -> str:
        """Get the optional directory path to scope the scan.

        Returns:
            \"/\" for full partition scan, or a directory path like \"/Users/Documents\".
        """
        text = self._dir_input.text().strip()
        if not text:
            return "/"
        # Normalize: ensure leading /, use forward slashes
        text = text.replace("\\", "/")
        if not text.startswith("/"):
            text = "/" + text
        return text
