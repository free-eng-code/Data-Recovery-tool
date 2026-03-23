---
description: "Use when: writing tests, adding test coverage, creating pytest fixtures, testing disk I/O, mocking pytsk3, testing recovery logic, testing GUI components"
applyTo: "tests/**"
---

# Testing Guidelines for DataForge

## Framework
- Use **pytest** for all tests
- Use **pytest-qt** for PySide6 GUI tests

## Structure
```
tests/
├── test_models.py        # Data model serialization, format_size, etc.
├── test_scanner.py       # Scanner with mocked pytsk3 objects
├── test_recovery.py      # Recovery engine with temp directories
├── test_session.py       # Session save/load round-trip
└── test_disk.py          # Disk enumeration (requires mocking win32file)
```

## Rules
- NEVER access real physical drives in tests — always mock `pytsk3.Img_Info` and `win32file`
- Use `tmp_path` fixture for recovery destination tests
- Test bad-sector handling by making mocked `read_random` raise `IOError`
- Test cancellation by setting `scanner.cancel()` mid-walk
- Every model must round-trip through JSON serialization (session persistence)
- Test `format_size` with edge cases: 0, negative, TB-scale values

## Coverage Targets
- `src/core/models.py` — 100%
- `src/core/scanner.py` — 80%+ (mock pytsk3 objects)
- `src/core/recovery.py` — 80%+ (use real temp files)
- `src/core/session.py` — 100%
