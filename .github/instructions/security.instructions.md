---
description: "Use when: writing disk I/O code, handling raw device access, opening physical drives, writing recovery output, validating paths, preventing accidental writes to source drives"
applyTo: "src/core/**"
---

# Security Rules for Core Engine

## Absolute Rules
- **NEVER write to the source disk or partition** — all source access is strictly READ-ONLY
- **NEVER use `os.system()` or `subprocess`** for disk operations — use `win32file` or `pytsk3` APIs
- **ALWAYS validate** that the recovery destination is NOT on the source drive before writing
- **NEVER store file contents in memory** beyond the streaming buffer — stream directly to disk
- **NEVER open a device path constructed from user input** without sanitizing it against the pattern `\\.\PhysicalDriveN` or `\\.\X:`

## Validation Checks
- Before recovery: confirm `destination_drive != source_drive`
- Before opening a device: verify path matches `^\\\\\.\\\\(PhysicalDrive\d+|[A-Z]:)$`
- Before writing: verify destination directory exists and is writable

## Error Handling
- All `win32file` and `pytsk3` calls MUST be wrapped in try/except
- Bad sectors: log the error, pad with zeros, continue — never crash
- Permission errors: surface immediately to the user with clear message
