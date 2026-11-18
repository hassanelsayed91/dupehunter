# Changelog

All notable changes to DupeHunter are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-18

First public release - day 2 of "30 Windows tools in 30 days".

### Added
- Content-based duplicate detection with BLAKE2b, grouped by size first so unique files are never hashed.
- Full-hash and fast (head+tail sampling) scan modes, with full verification before any action.
- Hard link detection: the same physical file is never reported as a duplicate.
- Safe cleanup with quarantine, timestamped folders and a journal per run.
- `dupehunter restore` rebuilds the original folder layout, skips files you removed yourself and cleans up emptied quarantine folders.
- Explicit permanent delete (`--delete`) with its own journal and confirmation.
- Keep policies: `oldest`, `newest`, `shortest`, `first` - plus per-file checkboxes in the GUI.
- Desktop app (Tkinter): folder list, size filter, review tree, selection totals, quarantine/delete/restore, report export.
- CLI: `scan`, `clean`, `restore`, `journals`, `gui`; JSON and CSV reports.
- 40 unit tests, GitHub Actions CI on Windows and Linux (Python 3.10 and 3.13).
- MIT license, bilingual documentation (English + Arabic).
