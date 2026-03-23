"""Tree view page — browse recovered files and folders."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import FileStatus, RecoveredEntry, ScanResult
from ..utils.formatting import format_size, format_datetime

logger = logging.getLogger(__name__)

# Color scheme for file status
STATUS_COLORS = {
    FileStatus.INTACT: QColor("#81c784"),       # Green
    FileStatus.DELETED: QColor("#ef5350"),       # Red
    FileStatus.PARTIAL: QColor("#ffb74d"),       # Orange
    FileStatus.METADATA_ONLY: QColor("#9e9e9e"), # Gray
}

STATUS_LABELS = {
    FileStatus.INTACT: "Intact",
    FileStatus.DELETED: "Deleted",
    FileStatus.PARTIAL: "Partial",
    FileStatus.METADATA_ONLY: "Metadata Only",
}

# Sentinel child used when a directory has children but hasn't been expanded yet
_PLACEHOLDER = "__lazy_placeholder__"


def _count_tree(entries: list[RecoveredEntry]) -> tuple[int, int, int]:
    """Return (total_files, total_dirs, total_size) recursively."""
    files = dirs = size = 0
    for e in entries:
        if e.is_directory:
            dirs += 1
            cf, cd, cs = _count_tree(e.children)
            files += cf
            dirs += cd
            size += cs
        else:
            files += 1
            size += e.size_bytes
    return files, dirs, size


class TreeViewPage(QWidget):
    """Page displaying the folder tree of recovered files."""

    recover_requested = Signal(list)  # list of RecoveredEntry

    def __init__(self):
        super().__init__()
        self._scan_result: ScanResult | None = None
        self._all_items: list[QTreeWidgetItem] = []
        self._setup_ui()

    # ------------------------------------------------------------------ UI
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # ---- Top toolbar ----
        toolbar = QHBoxLayout()

        filter_label = QLabel("Filter:")
        filter_label.setStyleSheet("color: #e0e0e0;")
        toolbar.addWidget(filter_label)

        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("Type to filter by name...")
        self._filter_input.setFixedWidth(250)
        self._filter_input.setStyleSheet("""
            QLineEdit {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 6px;
            }
        """)
        self._filter_input.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter_input)

        self._status_filter = QComboBox()
        self._status_filter.addItems(["All Files", "Deleted Only", "Intact Only", "Partial / Carved"])
        self._status_filter.setCurrentIndex(1)  # Default to deleted — recovery tool
        self._status_filter.setStyleSheet("""
            QComboBox {
                background: #2d2d44; color: #e0e0e0; border: 1px solid #3d3d5c;
                border-radius: 4px; padding: 6px;
            }
            QComboBox QAbstractItemView {
                background: #2d2d44; color: #e0e0e0; selection-background-color: #4a4a6a;
            }
        """)
        self._status_filter.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self._status_filter)

        toolbar.addStretch()

        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #aab4c0;")
        toolbar.addWidget(self._stats_label)

        # Expand / Collapse
        _btn_style = """
            QPushButton {
                background: #3d3d5c; color: #e0e0e0; border: none;
                padding: 6px 12px; border-radius: 4px;
            }
            QPushButton:hover { background: #4a4a6a; }
        """
        expand_btn = QPushButton("Expand All")
        expand_btn.setStyleSheet(_btn_style)
        expand_btn.clicked.connect(lambda: self._dir_tree.expandAll())
        toolbar.addWidget(expand_btn)

        collapse_btn = QPushButton("Collapse All")
        collapse_btn.setStyleSheet(_btn_style)
        collapse_btn.clicked.connect(lambda: self._dir_tree.collapseAll())
        toolbar.addWidget(collapse_btn)

        self._select_all = QCheckBox("Select All")
        self._select_all.setStyleSheet("color: #e0e0e0;")
        self._select_all.stateChanged.connect(self._toggle_select_all)
        toolbar.addWidget(self._select_all)

        self._recover_btn = QPushButton("Recover Selected")
        self._recover_btn.setStyleSheet("""
            QPushButton {
                background: #66bb6a; color: white; border: none;
                padding: 8px 20px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #81c784; }
        """)
        self._recover_btn.clicked.connect(self._on_recover_clicked)
        toolbar.addWidget(self._recover_btn)

        layout.addLayout(toolbar)

        # ---- Breadcrumb path bar ----
        self._path_label = QLabel("Path: /")
        self._path_label.setStyleSheet(
            "color: #4fc3f7; background: #252540; padding: 6px 12px; "
            "border-radius: 4px; font-family: Consolas, monospace;"
        )
        layout.addWidget(self._path_label)

        # ---- Splitter: directory tree (left) + file list (right) ----
        splitter = QSplitter(Qt.Orientation.Horizontal)

        tree_style = """
            QTreeWidget {
                background: #2d2d44; color: #e0e0e0; border: none;
                alternate-background-color: #32324d;
            }
            QTreeWidget::item:selected { background: #4a4a6a; }
            QTreeWidget::item { padding: 2px 0; }
            QHeaderView::section {
                background: #252540; color: #aab4c0; border: none;
                padding: 6px; font-weight: bold;
            }
        """

        # -- Left: directory tree --
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        # Path / Type toggle (like EaseUS)
        tab_bar = QHBoxLayout()
        tab_style_active = (
            "background: #3d3d5c; color: #4fc3f7; border: none; "
            "padding: 6px 18px; font-weight: bold; border-bottom: 2px solid #4fc3f7;"
        )
        tab_style_inactive = (
            "background: transparent; color: #aab4c0; border: none; "
            "padding: 6px 18px;"
        )
        self._tab_path = QPushButton("Path")
        self._tab_path.setStyleSheet(tab_style_active)
        self._tab_path.setCheckable(True)
        self._tab_path.setChecked(True)
        self._tab_path.clicked.connect(lambda: self._switch_tree_mode("path"))
        tab_bar.addWidget(self._tab_path)

        self._tab_type = QPushButton("Type")
        self._tab_type.setStyleSheet(tab_style_inactive)
        self._tab_type.setCheckable(True)
        self._tab_type.clicked.connect(lambda: self._switch_tree_mode("type"))
        tab_bar.addWidget(self._tab_type)

        self._tab_style_active = tab_style_active
        self._tab_style_inactive = tab_style_inactive
        tab_bar.addStretch()
        left_layout.addLayout(tab_bar)

        self._dir_tree = QTreeWidget()
        self._dir_tree.setHeaderLabels(["Name", "Items", "Size", "Status"])
        self._dir_tree.setRootIsDecorated(True)
        self._dir_tree.setAlternatingRowColors(True)
        self._dir_tree.setStyleSheet(tree_style)
        dir_header = self._dir_tree.header()
        dir_header.setStretchLastSection(False)
        dir_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        dir_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dir_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dir_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._dir_tree.itemExpanded.connect(self._on_dir_expanded)
        self._dir_tree.currentItemChanged.connect(self._on_dir_selected)
        left_layout.addWidget(self._dir_tree)
        splitter.addWidget(left)

        # -- Right: file contents of selected directory --
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._contents_label = QLabel("Directory Contents")
        self._contents_label.setStyleSheet(
            "color: #aab4c0; font-weight: bold; padding: 4px;"
        )
        right_layout.addWidget(self._contents_label)

        self._file_tree = QTreeWidget()
        self._file_tree.setHeaderLabels([
            "", "Name", "Size", "Modified", "Created", "Status", "Confidence",
        ])
        self._file_tree.setRootIsDecorated(False)
        self._file_tree.setAlternatingRowColors(True)
        self._file_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._file_tree.setStyleSheet(tree_style)
        file_header = self._file_tree.header()
        file_header.setStretchLastSection(False)
        file_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        file_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        file_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        file_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        file_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        file_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        file_header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._file_tree.setColumnWidth(0, 30)
        right_layout.addWidget(self._file_tree)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)  # left  ~35%
        splitter.setStretchFactor(1, 2)  # right ~65%
        splitter.setSizes([350, 700])
        layout.addWidget(splitter, 1)

        # ---- Legend ----
        legend = QHBoxLayout()
        for status, color in STATUS_COLORS.items():
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color.name()};")
            legend.addWidget(dot)
            lbl = QLabel(STATUS_LABELS[status])
            lbl.setStyleSheet("color: #aab4c0; margin-right: 15px;")
            legend.addWidget(lbl)
        legend.addStretch()
        layout.addLayout(legend)

    # --------------------------------------------------------- Load results
    def load_scan_result(self, result: ScanResult) -> None:
        """Load scan results into the tree."""
        self._scan_result = result
        self._dir_tree.clear()
        self._file_tree.clear()
        self._all_items = []
        self._path_label.setText(f"Path: {result.target_path}")

        # Build only the top-level directory nodes (lazy load the rest)
        # Default to Path view: skip type category folders (starting with "/[")
        for entry in result.root_entries:
            if entry.is_directory and not entry.path.startswith("/["):
                item = self._create_dir_item(entry)
                self._dir_tree.addTopLevelItem(item)
            else:
                # Top-level files go into a synthetic root shown later
                pass

        # If there are top-level files, show them automatically
        self._show_dir_contents(result.root_entries, result.target_path)

        self._dir_tree.expandToDepth(0)
        self._update_stats()
        # Default to path view
        self._tree_mode = "path"

    # ------------------------------------------------ Directory tree helpers
    @staticmethod
    def _format_count(n: int) -> str:
        """Format a count like Disk Drill: 1.6K, 147.5K, 1.9M."""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    def _create_dir_item(self, entry: RecoveredEntry) -> QTreeWidgetItem:
        """Create a left-pane directory node with summary info."""
        files, dirs, size = _count_tree(entry.children)
        item_count = files + dirs

        # Show count in the name like Disk Drill: "Users (1065427)"
        if item_count:
            display_name = f"📁 {entry.name} ({self._format_count(item_count)})"
        else:
            display_name = f"📁 {entry.name}"

        item = QTreeWidgetItem([
            display_name,
            f"{item_count:,}" if item_count else "",
            format_size(size) if size else "",
            STATUS_LABELS.get(entry.status, ""),
        ])
        item.setData(0, Qt.ItemDataRole.UserRole, entry)

        color = STATUS_COLORS.get(entry.status, QColor("#e0e0e0"))
        for col in range(4):
            item.setForeground(col, QBrush(color))
        # Make folder names bold
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)

        # Add a placeholder child so Qt shows the expand arrow
        if entry.children:
            placeholder = QTreeWidgetItem([_PLACEHOLDER])
            item.addChild(placeholder)

        return item

    @Slot(QTreeWidgetItem)
    def _on_dir_expanded(self, item: QTreeWidgetItem) -> None:
        """Lazy-load subdirectories when a node is expanded."""
        # If the first child is the placeholder, replace with real children
        if (
            item.childCount() == 1
            and item.child(0).text(0) == _PLACEHOLDER
        ):
            item.removeChild(item.child(0))
            entry: RecoveredEntry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry:
                for child in entry.children:
                    if child.is_directory:
                        child_item = self._create_dir_item(child)
                        item.addChild(child_item)

    @Slot(QTreeWidgetItem, QTreeWidgetItem)
    def _on_dir_selected(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        """When a directory is clicked, show its contents in the right pane."""
        if current is None:
            return
        entry: RecoveredEntry | None = current.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:
            return

        self._path_label.setText(f"Path: {entry.path}")
        self._show_dir_contents(entry.children, entry.path)

    def _switch_tree_mode(self, mode: str) -> None:
        """Toggle left panel between Path view and Type view (like EaseUS)."""
        if mode == self._tree_mode:
            return
        self._tree_mode = mode

        # Update tab styles
        if mode == "path":
            self._tab_path.setStyleSheet(self._tab_style_active)
            self._tab_path.setChecked(True)
            self._tab_type.setStyleSheet(self._tab_style_inactive)
            self._tab_type.setChecked(False)
        else:
            self._tab_type.setStyleSheet(self._tab_style_active)
            self._tab_type.setChecked(True)
            self._tab_path.setStyleSheet(self._tab_style_inactive)
            self._tab_path.setChecked(False)

        # Rebuild the tree
        if not self._scan_result:
            return

        self._dir_tree.clear()
        self._file_tree.clear()

        if mode == "type":
            # Type view: show file type categories at top level
            for entry in self._scan_result.root_entries:
                if entry.is_directory and entry.path.startswith("/["):
                    item = self._create_dir_item(entry)
                    self._dir_tree.addTopLevelItem(item)
        else:
            # Path view: show directory structure (skip type categories)
            for entry in self._scan_result.root_entries:
                if entry.is_directory and not entry.path.startswith("/["):
                    item = self._create_dir_item(entry)
                    self._dir_tree.addTopLevelItem(item)

        self._dir_tree.expandToDepth(0)

    # ------------------------------------------------- Right-pane file list
    def _show_dir_contents(
        self, entries: list[RecoveredEntry], parent_path: str
    ) -> None:
        """Populate the right-pane file list with contents of a directory."""
        self._file_tree.clear()
        self._all_items = []

        # Sort: directories first, then files, both alphabetical
        sorted_entries = sorted(
            entries, key=lambda e: (not e.is_directory, e.name.lower())
        )

        dir_count = 0
        file_count = 0

        for entry in sorted_entries:
            item = self._create_file_item(entry)
            self._file_tree.addTopLevelItem(item)
            self._all_items.append(item)
            if entry.is_directory:
                dir_count += 1
            else:
                file_count += 1

        self._contents_label.setText(
            f"Directory Contents — {dir_count} folders, {file_count} files"
        )

    def _create_file_item(self, entry: RecoveredEntry) -> QTreeWidgetItem:
        """Create a right-pane item for a file or subdirectory."""
        if entry.is_directory:
            files, dirs, size = _count_tree(entry.children)
            icon_text = "📁"
            size_text = format_size(size) if size else f"({files + dirs} items)"
            confidence_text = ""
        else:
            icon_text = "📄"
            size_text = format_size(entry.size_bytes)
            confidence_text = f"{entry.confidence * 100:.0f}%"

        item = QTreeWidgetItem([
            "",  # Checkbox column
            f"{icon_text}  {entry.name}",
            size_text,
            format_datetime(entry.modified),
            format_datetime(entry.created),
            STATUS_LABELS.get(entry.status, ""),
            confidence_text,
        ])

        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        item.setCheckState(0, Qt.CheckState.Unchecked)

        # Bold name for directories
        if entry.is_directory:
            font = item.font(1)
            font.setBold(True)
            item.setFont(1, font)

        # Color-code by status
        color = STATUS_COLORS.get(entry.status, QColor("#e0e0e0"))
        for col in range(1, 7):
            item.setForeground(col, QBrush(color))

        return item

    # ------------------------------------------------------------ Filtering
    def _apply_filter(self) -> None:
        """Filter right-pane items by name and status."""
        text = self._filter_input.text().lower()
        status_idx = self._status_filter.currentIndex()

        for item in self._all_items:
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry is None:
                continue

            name_match = not text or text in entry.name.lower()
            status_match = True
            if status_idx == 1:
                status_match = entry.status in (
                    FileStatus.DELETED, FileStatus.PARTIAL, FileStatus.METADATA_ONLY
                )
            elif status_idx == 2:
                status_match = entry.status == FileStatus.INTACT
            elif status_idx == 3:
                status_match = entry.status == FileStatus.PARTIAL

            item.setHidden(not (name_match and status_match))

    # ------------------------------------------------------------ Selection
    def _toggle_select_all(self, state: int) -> None:
        check = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for item in self._all_items:
            if not item.isHidden():
                item.setCheckState(0, check)

    def _on_recover_clicked(self) -> None:
        entries = self.get_selected_entries()
        if entries:
            self.recover_requested.emit(entries)

    def get_selected_entries(self) -> list[RecoveredEntry]:
        """Get all checked entries from the file list."""
        entries = []
        for item in self._all_items:
            if item.checkState(0) == Qt.CheckState.Checked:
                entry = item.data(0, Qt.ItemDataRole.UserRole)
                if entry:
                    entries.append(entry)
        return entries

    # ---------------------------------------------------------- Stats
    def _update_stats(self) -> None:
        if self._scan_result:
            r = self._scan_result
            self._stats_label.setText(
                f"{r.total_files:,} files / {format_size(r.total_size_bytes)} "
                f"({r.total_deleted:,} recoverable)"
            )
