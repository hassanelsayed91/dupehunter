"""DupeHunter - find files with identical content, clean up safely."""

from .core import (
    VERSION,
    DupGroup,
    FileInfo,
    ScanOptions,
    ScanResult,
    delete_files,
    find_duplicates,
    quarantine,
    quarantine_files,
    restore,
)

__all__ = [
    "VERSION",
    "DupGroup",
    "FileInfo",
    "ScanOptions",
    "ScanResult",
    "find_duplicates",
    "quarantine",
    "quarantine_files",
    "delete_files",
    "restore",
]
__version__ = VERSION
