"""Admin privilege checking and elevation."""

from __future__ import annotations

import ctypes
import sys


def is_admin() -> bool:
    """Check if the current process has administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except (AttributeError, OSError):
        return False


def request_elevation() -> None:
    """Re-launch the current script as administrator.

    This will trigger a UAC prompt on Windows.
    """
    if is_admin():
        return

    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        " ".join(sys.argv),
        None,
        1,  # SW_SHOWNORMAL
    )
    sys.exit(0)
