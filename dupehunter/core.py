"""DupeHunter core engine.

Find real duplicates (same bytes, not same name), then either move the extra
copies to a quarantine folder with a journal - fully reversible - or delete
them permanently when you ask for it explicitly.

Safety rules baked in:
  * nothing is touched during a scan
  * hard links are not reported as duplicates (they take no extra space)
  * empty files are ignored by default
  * every action verifies the content with a full hash first
  * quarantine moves are journaled before the first file is touched
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

APP_NAME = "DupeHunter"
VERSION = "1.0.0"
JOURNAL_VERSION = 1
PROJECT_URL = "https://github.com/hassanelsayed91/dupehunter"

CHUNK = 1024 * 1024          # 1 MiB read buffer for full hashing
SAMPLE = 64 * 1024           # bytes read from the start and end in fast mode

DEFAULT_EXCLUDE_DIRS = [
    "node_modules", ".git", "__pycache__", "$RECYCLE.BIN",
    "System Volume Information", ".venv", "venv",
]

DEFAULT_EXCLUDE_GLOBS = [
    "desktop.ini", "thumbs.db", ".ds_store", "*.tmp", "*.part", "*.crdownload",
]

KEEP_POLICIES = ("oldest", "newest", "shortest", "first")

SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3,
}


def parse_size(text: str | int) -> int:
    """'500kb' / '2 MB' / '1024' -> bytes."""
    if isinstance(text, int):
        return text
    raw = str(text).strip().lower().replace("_", "").replace(" ", "")
    number = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    unit = "".join(ch for ch in raw if ch.isalpha())
    if not number:
        raise ValueError(f"invalid size: {text}")
    if unit not in SIZE_UNITS:
        raise ValueError(f"unknown unit in {text!r} (use B, KB, MB or GB)")
    return int(float(number) * SIZE_UNITS[unit])


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def journal_dir() -> Path:
    return data_dir() / "journal"


def default_quarantine_dir() -> Path:
    return Path.home() / "DupeHunter Quarantine"


def config_path() -> Path:
    return data_dir() / "config.json"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    inode: tuple[int, int] | None = None
    digest: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def folder(self) -> str:
        return str(self.path.parent)

    @property
    def modified(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": self.mtime,
            "digest": self.digest,
        }


@dataclass
class DupGroup:
    size: int
    digest: str
    files: list[FileInfo] = field(default_factory=list)

    @property
    def wasted(self) -> int:
        """Bytes that would be freed by keeping a single copy."""
        return self.size * max(0, len(self.files) - 1)

    def survivor(self, policy: str = "oldest") -> FileInfo:
        return choose_survivor(self.files, policy)

    def removals(self, policy: str = "oldest") -> list[FileInfo]:
        keep = self.survivor(policy)
        return [f for f in self.files if f.path != keep.path]

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "digest": self.digest,
            "wasted": self.wasted,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass
class ScanOptions:
    min_size: int = 1
    fast: bool = False
    recursive: bool = True
    include_hidden: bool = False
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    def to_dict(self) -> dict:
        return {
            "min_size": self.min_size,
            "fast": self.fast,
            "recursive": self.recursive,
            "include_hidden": self.include_hidden,
            "exclude_dirs": self.exclude_dirs,
            "exclude_globs": self.exclude_globs,
        }


@dataclass
class ScanResult:
    groups: list[DupGroup] = field(default_factory=list)
    files_scanned: int = 0
    candidates: int = 0
    errors: int = 0
    elapsed: float = 0.0
    roots: list[Path] = field(default_factory=list)

    @property
    def duplicate_files(self) -> int:
        return sum(len(g.files) - 1 for g in self.groups)

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups)

    @property
    def total_files(self) -> int:
        return sum(len(g.files) for g in self.groups)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _is_hidden(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        attrs = path.stat().st_file_attributes
    except (OSError, AttributeError):
        return False
    import stat as _stat

    return bool(attrs & (_stat.FILE_ATTRIBUTE_HIDDEN | _stat.FILE_ATTRIBUTE_SYSTEM))


def _excluded(path: Path, options: ScanOptions) -> bool:
    import fnmatch

    lowered = path.name.lower()
    if any(fnmatch.fnmatch(lowered, pat.lower()) for pat in options.exclude_globs):
        return True
    excluded = {d.lower() for d in options.exclude_dirs}
    return bool({p.name.lower() for p in path.parents} & excluded)


def _iter_files(roots: Sequence[Path], options: ScanOptions) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        walker = root.rglob("*") if options.recursive else root.glob("*")
        for path in walker:
            try:
                if path.is_dir() or path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            if not options.include_hidden and _is_hidden(path):
                continue
            if _excluded(path, options):
                continue
            yield path


def file_digest(path: Path, fast: bool = False) -> str:
    """BLAKE2b hash of the whole file, or of its start+end in fast mode."""
    digest = hashlib.blake2b(digest_size=16)
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if fast and size > SAMPLE * 2:
            digest.update(handle.read(SAMPLE))
            handle.seek(-SAMPLE, os.SEEK_END)
            digest.update(handle.read(SAMPLE))
        else:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    return digest.hexdigest()


def choose_survivor(files: Sequence[FileInfo], policy: str = "oldest") -> FileInfo:
    """Which copy should be kept. The others are the duplicates."""
    if not files:
        raise ValueError("empty group")
    if policy == "newest":
        return max(files, key=lambda f: (f.mtime, -len(str(f.path))))
    if policy == "shortest":
        return min(files, key=lambda f: (len(str(f.path)), str(f.path)))
    if policy == "first":
        return min(files, key=lambda f: str(f.path).lower())
    # oldest (default) - and the shortest path breaks ties
    return min(files, key=lambda f: (f.mtime, len(str(f.path)), str(f.path).lower()))


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
def find_duplicates(
    roots: Sequence[str | os.PathLike],
    options: ScanOptions | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ScanResult:
    """Scan the given folders and return groups of files with identical content."""
    options = options or ScanOptions()
    started = time.perf_counter()
    root_paths = [Path(r).expanduser() for r in roots]
    for root in root_paths:
        if not root.exists():
            raise FileNotFoundError(f"no such folder: {root}")

    result = ScanResult(roots=root_paths)
    by_inode: dict[tuple[int, int], Path] = {}
    by_size: dict[int, list[FileInfo]] = defaultdict(list)

    for path in _iter_files(root_paths, options):
        if cancel and cancel():
            return result
        try:
            stat = path.stat()
        except OSError:
            result.errors += 1
            continue
        if stat.st_size < options.min_size:
            continue
        result.files_scanned += 1

        inode = (stat.st_dev, stat.st_ino)
        if stat.st_ino and inode in by_inode:
            # hard link / already seen through another path: not a duplicate
            continue
        if stat.st_ino:
            by_inode[inode] = path

        by_size[stat.st_size].append(
            FileInfo(path=path, size=stat.st_size, mtime=stat.st_mtime, inode=inode)
        )

    candidates = [info for bucket in by_size.values() if len(bucket) > 1 for info in bucket]
    result.candidates = len(candidates)
    if progress:
        progress("hashing", 0, len(candidates))

    # hash pass: only files that share a size with at least one other file
    buckets: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
    done = 0
    for info in candidates:
        if cancel and cancel():
            return result
        try:
            info.digest = file_digest(info.path, fast=options.fast)
        except OSError:
            result.errors += 1
            done += 1
            if progress:
                progress("hashing", done, len(candidates))
            continue
        buckets[(info.size, info.digest)].append(info)
        done += 1
        if progress and (done % 25 == 0 or done == len(candidates)):
            progress("hashing", done, len(candidates))

    groups = [
        DupGroup(size=size, digest=digest, files=sorted(files, key=lambda f: str(f.path).lower()))
        for (size, digest), files in buckets.items()
        if len(files) > 1
    ]
    groups.sort(key=lambda g: (-g.wasted, -g.size))
    result.groups = groups
    result.elapsed = time.perf_counter() - started
    if progress:
        progress("done", len(candidates), len(candidates))
    return result


def verify_groups(groups: Iterable[DupGroup], progress=None, cancel=None) -> list[DupGroup]:
    """Re-hash every file in full and split groups that differ.

    Run before deleting or quarantining, so a fast scan can never make you
    remove a file that is not really a duplicate.
    """
    verified: list[DupGroup] = []
    for group in groups:
        buckets: dict[str, list[FileInfo]] = defaultdict(list)
        for info in group.files:
            if cancel and cancel():
                return verified
            try:
                info.digest = file_digest(info.path, fast=False)
            except OSError:
                continue
            buckets[info.digest].append(info)
        for digest, files in buckets.items():
            if len(files) > 1:
                files.sort(key=lambda f: str(f.path).lower())
                verified.append(DupGroup(size=files[0].size, digest=digest, files=files))
    if progress:
        progress("verified", len(verified), len(verified))
    return verified


# --------------------------------------------------------------------------- #
# journals
# --------------------------------------------------------------------------- #
def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _new_journal(action: str, extra: dict | None = None) -> Path:
    journal_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = journal_dir() / f"{action}-{stamp}-{os.getpid()}.json"
    payload = {
        "app": APP_NAME,
        "version": VERSION,
        "journal_version": JOURNAL_VERSION,
        "action": action,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "moves": [],
    }
    if extra:
        payload.update(extra)
    _write_json(path, payload)
    return path


def latest_journal(action: str | None = None) -> Path | None:
    folder = journal_dir()
    if not folder.is_dir():
        return None
    pattern = f"{action}-*.json" if action else "*.json"
    journals = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return journals[-1] if journals else None


def list_journals() -> list[Path]:
    folder = journal_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


# --------------------------------------------------------------------------- #
# quarantine (reversible)
# --------------------------------------------------------------------------- #
def quarantine(
    groups: Sequence[DupGroup],
    policy: str = "oldest",
    dest: str | os.PathLike | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    dry_run: bool = False,
    verify: bool = True,
    cancel: Callable[[], bool] | None = None,
) -> tuple[Counter, Path | None]:
    """Move the extra copies of every group into a quarantine folder."""
    if policy not in KEEP_POLICIES:
        raise ValueError(f"unknown keep policy: {policy}")
    pool = verify_groups(groups, progress=progress, cancel=cancel) if verify else list(groups)
    targets = [info.path for group in pool for info in group.removals(policy)]
    return quarantine_files(targets, dest, progress=progress, dry_run=dry_run, cancel=cancel)


def quarantine_files(
    paths: Iterable[str | os.PathLike],
    dest: str | os.PathLike | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    dry_run: bool = False,
    cancel: Callable[[], bool] | None = None,
) -> tuple[Counter, Path | None]:
    """Move exactly these files into a quarantine folder. Returns (summary, journal)."""
    entries: list[dict] = []
    for path in paths:
        entries.append({"src": str(path), "dst": "", "size": 0, "status": "pending"})
    if not entries:
        return Counter(), None
    if dry_run:
        total = 0
        for entry in entries:
            try:
                total += Path(entry["src"]).stat().st_size
            except OSError:
                pass
        return Counter({"would_move": len(entries), "bytes": total}), None

    root = Path(dest).expanduser() if dest else default_quarantine_dir()
    target = root / datetime.now().strftime("%Y-%m-%d %H%M%S")
    summary: Counter = Counter()
    journal = _new_journal("quarantine", {"quarantine_dir": str(target)})

    for index, entry in enumerate(entries, start=1):
        if cancel and cancel():
            break
        src = Path(entry["src"])
        try:
            if not src.is_file():
                entry["status"] = "skipped"
                summary["skipped"] += 1
                continue
            entry["size"] = src.stat().st_size
            destination = _unique_path(target / src.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(destination))
            entry["dst"] = str(destination)
            entry["status"] = "moved"
            summary["moved"] += 1
            summary["bytes"] += entry["size"]
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            summary["error"] += 1
        if progress and (index % 10 == 0 or index == len(entries)):
            progress("moving", index, len(entries))
        _write_json(journal, {
            "app": APP_NAME, "version": VERSION, "journal_version": JOURNAL_VERSION,
            "action": "quarantine", "created_at": datetime.now().isoformat(timespec="seconds"),
            "quarantine_dir": str(target), "moves": entries,
        })

    _write_json(journal, {
        "app": APP_NAME, "version": VERSION, "journal_version": JOURNAL_VERSION,
        "action": "quarantine", "created_at": datetime.now().isoformat(timespec="seconds"),
        "quarantine_dir": str(target), "moves": entries,
    })
    if summary.get("moved", 0) == 0:
        journal.unlink(missing_ok=True)
        return summary, None
    return summary, journal


def restore(
    journal: str | os.PathLike | None = None,
    dry_run: bool = False,
) -> tuple[Counter, Path | None]:
    """Put quarantined files back where they came from."""
    path = Path(journal) if journal else latest_journal("quarantine")
    if not path or not path.exists():
        return Counter({"missing": 1}), None

    data = json.loads(path.read_text(encoding="utf-8"))
    summary: Counter = Counter()
    for move in reversed(data.get("moves", [])):
        if move.get("status") != "moved":
            continue
        source = Path(move["dst"])
        destination = _unique_path(Path(move["src"]))
        try:
            if not source.exists():
                summary["missing"] += 1
                continue
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            move["status"] = "restored"
            move["restored_to"] = str(destination)
            summary["restored"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["error"] += 1
            print(f"[{APP_NAME}] could not restore {source}: {exc}", file=sys.stderr)

    if not dry_run:
        data["restored_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(path, data)
        folder = data.get("quarantine_dir")
        if folder:
            try:
                candidate = Path(folder)
                if candidate.is_dir() and not any(candidate.iterdir()):
                    candidate.rmdir()
            except OSError:
                pass
    return summary, path


# --------------------------------------------------------------------------- #
# permanent delete (explicit, verified, journaled)
# --------------------------------------------------------------------------- #
def delete_duplicates(
    groups: Sequence[DupGroup],
    policy: str = "oldest",
    dry_run: bool = False,
    verify: bool = True,
) -> tuple[Counter, Path | None]:
    """Delete the extra copies for real. The journal is a record, not an undo."""
    if policy not in KEEP_POLICIES:
        raise ValueError(f"unknown keep policy: {policy}")
    pool = verify_groups(groups) if verify else list(groups)
    targets = [info.path for group in pool for info in group.removals(policy)]
    return delete_files(targets, dry_run=dry_run)


def delete_files(
    paths: Iterable[str | os.PathLike],
    dry_run: bool = False,
) -> tuple[Counter, Path | None]:
    """Permanently delete exactly these files. Returns (summary, journal)."""
    entries = [{"path": str(path), "size": 0, "status": "pending"} for path in paths]
    if not entries:
        return Counter(), None
    if dry_run:
        total = 0
        for entry in entries:
            try:
                total += Path(entry["path"]).stat().st_size
            except OSError:
                pass
        return Counter({"would_delete": len(entries), "bytes": total}), None

    summary: Counter = Counter()
    for entry in entries:
        path = Path(entry["path"])
        try:
            entry["size"] = path.stat().st_size
            path.unlink()
            entry["status"] = "deleted"
            summary["deleted"] += 1
            summary["bytes"] += entry["size"]
        except OSError as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            summary["error"] += 1

    journal = _new_journal("delete", {"policy": "manual"})
    _write_json(journal, {
        "app": APP_NAME, "version": VERSION, "journal_version": JOURNAL_VERSION,
        "action": "delete", "created_at": datetime.now().isoformat(timespec="seconds"),
        "moves": entries,
    })
    if summary.get("deleted", 0) == 0:
        journal.unlink(missing_ok=True)
        return summary, None
    return summary, journal


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def scan_summary(result: ScanResult) -> list[tuple[str, int, int]]:
    """[(group_label, files, wasted_bytes)] for a quick console/CLI view."""
    rows = []
    for group in result.groups:
        rows.append((f"{human_size(group.size)} each", len(group.files), group.wasted))
    return rows


def export_json(result: ScanResult, path: str | os.PathLike) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "app": APP_NAME,
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "roots": [str(r) for r in result.roots],
        "files_scanned": result.files_scanned,
        "errors": result.errors,
        "groups": [g.to_dict() for g in result.groups],
        "totals": {
            "groups": len(result.groups),
            "duplicate_files": result.duplicate_files,
            "wasted_bytes": result.wasted,
        },
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def export_csv(result: ScanResult, path: str | os.PathLike) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["group", "size", "wasted", "digest", "path", "modified", "keep"])
        for index, group in enumerate(result.groups, start=1):
            survivor = group.survivor("oldest")
            for info in group.files:
                writer.writerow([
                    index, group.size, group.wasted, group.digest,
                    str(info.path), info.modified,
                    "yes" if info.path == survivor.path else "no",
                ])
    return out
