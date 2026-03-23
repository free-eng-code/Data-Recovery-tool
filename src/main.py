"""DataForge Recovery — Entry point."""

from __future__ import annotations

import sys

from src.utils.admin import is_admin, request_elevation
from src.utils.logging_setup import setup_logging


def main() -> None:
    """Application entry point."""
    setup_logging(log_file="dataforge.log", debug=True)

    # Check admin privileges — warn but allow running for volume-level scanning
    if not is_admin():
        print("Note: Running without Administrator privileges.")
        print("Volume scanning works, but raw disk access requires elevation.")
        if "--no-elevate" not in sys.argv:
            print("Requesting elevation... (use --no-elevate to skip)")
            request_elevation()
            return

    # Import PySide6 after admin check to avoid loading GUI unnecessarily
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    from src.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("DataForge Recovery")
    app.setOrganizationName("DataForge")

    # Dark theme palette
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
