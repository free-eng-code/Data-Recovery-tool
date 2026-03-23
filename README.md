# DataForge Recovery

Bit-level data recovery tool with deep disk forensics support.

## Features

- Deep scan of physical drives and partitions at the bit level
- Supports NTFS, FAT12/16/32, exFAT, ext2/3/4, HFS+, UFS, ISO9660
- Displays full folder structure of deleted/lost files with dates and sizes
- Selective recovery — choose files, folders, or entire trees
- Destination directory picker with folder structure preservation
- Real-time scan progress and recovery statistics
- File extension filtering — recover only specific file types (e.g. `.jpg .docx`)
- Directory-scoped scanning — scan a specific folder instead of the full partition
- Scan size limit — cap how much of a large drive to scan
- File carving — recovers files from unallocated space using 146 signature patterns across 82 file types
- Conflict resolution — Replace, Skip, or Duplicate when recovered files already exist at the destination
- Session save/restore — resume from a previous scan without re-scanning

---

## Installation (EXE — recommended for most users)

No Python or dependencies needed. Just download and run.

The pre-built EXE is included in this repository under [`dist/DataForge Recovery/`](dist/DataForge%20Recovery/).

1. Clone or download this repository
2. Navigate to the `dist/DataForge Recovery/` folder
3. Double-click **`DataForge Recovery.exe`**
4. Windows will ask for Administrator permission — click **Yes** (required for raw disk access)

> **Important:** Do not move the `.exe` out of its folder. It needs the `_internal/` directory next to it to run.

> **Tip:** To create a desktop shortcut, right-click `DataForge Recovery.exe` → **Send to** → **Desktop (create shortcut)**.

---

## Development Setup (for contributors)

If you want to enhance, modify, or contribute to this project, follow these steps.

### Prerequisites

- **Windows 10/11**
- **Python 3.10+** (3.12 recommended)
- **Visual Studio 2022 Build Tools** — required to compile `pytsk3` from source
  ```powershell
  choco install visualstudio2022-workload-vctools
  ```

### 1. Clone the repository

```bash
git clone https://github.com/your-username/Data-Recovery-tool.git
cd Data-Recovery-tool
```

### 2. Create a virtual environment

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -e ".[dev]"
```

This installs all runtime dependencies (`pytsk3`, `PySide6`, `pywin32`) plus dev tools (`pytest`, `pyinstaller`).

### 4. Run the application

```powershell
# Option A: Run with auto-elevation (requests admin)
python -m src.main

# Option B: Run without elevation prompt (volume scanning still works)
python -m src.main --no-elevate

# Option C: Run elevated directly via PowerShell
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m src.main --no-elevate" -Verb RunAs
```

### 5. Build the EXE

```bash
.venv\Scripts\pyinstaller.exe dataforge.spec --noconfirm
```

The output will be in `dist\DataForge Recovery\`. You can zip this folder for distribution.

---

## Project Structure

```
src/
├── main.py                  # Application entry point
├── core/
│   ├── disk.py              # Drive enumeration, volume listing
│   ├── models.py            # Data models (DiskInfo, RecoveredEntry, etc.)
│   ├── scanner.py           # pytsk3-based MFT/inode scanner
│   ├── win_scanner.py       # Native Windows volume scanner
│   ├── recovery.py          # File recovery engine with conflict handling
│   ├── carver.py            # File carver for unallocated space
│   ├── signatures.py        # 146 file signatures for carving
│   └── session.py           # Scan session save/load
├── gui/
│   ├── main_window.py       # Main window with navigation
│   ├── disk_selector.py     # Drive/partition selection page
│   ├── scan_progress.py     # Scan progress with phase indicators
│   ├── tree_view.py         # File tree browser with checkboxes
│   └── recovery_dialog.py   # Recovery destination and progress
└── utils/
    ├── admin.py             # Admin privilege helpers
    ├── formatting.py        # Size/duration formatting
    └── logging_setup.py     # Logging configuration
```

## Tech Stack

- **pytsk3** — The Sleuth Kit Python bindings (forensic engine)
- **PySide6** — Qt 6 GUI framework
- **pywin32** — Windows raw disk access
- **PyInstaller** — EXE packaging

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests: `pytest`
5. Commit and push
6. Open a Pull Request

## License

This project is provided as-is for educational and data recovery purposes.
