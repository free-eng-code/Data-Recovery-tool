---
name: file-carver
description: "Carve files from unallocated disk space using magic byte signatures. Use when: adding file type recovery by signature, implementing header/footer carving, scanning raw clusters for JPEG/PNG/PDF/DOCX/ZIP/MP4 headers, extending supported carved formats."
---

# File Signature Carving

Recover files from unallocated space by scanning for known file headers (magic bytes) without relying on file system metadata.

## When to Use
- Adding recovery for files whose metadata is completely gone
- Scanning unallocated clusters or raw disk regions
- Extending the list of recognized file signatures
- Building a carving engine that operates independently of the file system

## Procedure

### 1. Define Signatures
Add file signatures to a registry in `src/core/signatures.py`:

```python
@dataclass
class FileSignature:
    name: str           # e.g. "JPEG"
    extension: str      # e.g. ".jpg"
    header: bytes       # Magic bytes at file start
    footer: bytes | None  # Optional end marker
    max_size: int       # Maximum expected file size (prevents runaway reads)

SIGNATURES = [
    FileSignature("JPEG", ".jpg", b"\xff\xd8\xff", b"\xff\xd9", 50 * 1024 * 1024),
    FileSignature("PNG", ".png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 50 * 1024 * 1024),
    FileSignature("PDF", ".pdf", b"%PDF-", b"%%EOF", 500 * 1024 * 1024),
    FileSignature("ZIP/DOCX", ".zip", b"PK\x03\x04", None, 500 * 1024 * 1024),
    FileSignature("MP4", ".mp4", b"\x00\x00\x00\x18ftypmp4", None, 4 * 1024 * 1024 * 1024),
    FileSignature("BMP", ".bmp", b"BM", None, 50 * 1024 * 1024),
    FileSignature("GIF", ".gif", b"GIF89a", b"\x00\x3b", 50 * 1024 * 1024),
    FileSignature("7ZIP", ".7z", b"7z\xbc\xaf\x27\x1c", None, 500 * 1024 * 1024),
    FileSignature("RAR", ".rar", b"Rar!\x1a\x07", None, 500 * 1024 * 1024),
    FileSignature("SQLite", ".db", b"SQLite format 3\x00", None, 100 * 1024 * 1024),
]
```

### 2. Build the Carver
Create `src/core/carver.py` with a `FileCarver` class:

- Read unallocated regions in 512-byte aligned chunks
- Slide a window checking for header matches
- When a header is found, read until footer or max_size
- Stream carved output directly to the destination — never buffer the whole file
- Track found files with offset, size, and signature type

### 3. Integrate with Scanner
- After the normal file-system walk completes, run the carver on unallocated space
- Add carved files as `RecoveredEntry` items with `status=PARTIAL` and `confidence` based on whether a footer was found

### 4. Safety
- NEVER write carved data back to the source disk
- Validate that each carved region doesn't overlap with allocated clusters
- Cap individual file size to `max_size` to prevent runaway reads
- Support cancellation between chunk reads
