"""Command line interface for DupeHunter."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import core
from .core import DupGroup, ScanOptions

BANNER = f"DupeHunter {core.VERSION} - find real duplicates, remove them safely"

SIZE_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def parse_size(text: str) -> int:
    """argparse wrapper around core.parse_size."""
    try:
        return core.parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _options(args: argparse.Namespace) -> ScanOptions:
    options = ScanOptions()
    options.min_size = args.min_size
    options.fast = args.fast
    options.recursive = not args.no_recursive
    options.include_hidden = args.include_hidden
    options.exclude_dirs = list(core.DEFAULT_EXCLUDE_DIRS) + list(args.exclude or [])
    return options


def _progress_printer():
    if not sys.stdout.isatty():
        return None

    def progress(stage: str, done: int, total: int) -> None:
        if total <= 0:
            return
        percent = int(done * 100 / total)
        sys.stdout.write(f"\r  {stage:<9} {done:>6}/{total}  {percent:>3}%")
        sys.stdout.flush()
        if done >= total:
            sys.stdout.write("\n")

    return progress


def _print_groups(result: core.ScanResult, limit: int) -> None:
    groups = result.groups[:limit]
    for index, group in enumerate(groups, start=1):
        print(f"\n  [{index}] {len(group.files)} copies · {core.human_size(group.size)} each "
              f"· {core.human_size(group.wasted)} wasted")
        keep = group.survivor("oldest")
        for info in group.files:
            marker = "keep" if info.path == keep.path else "dup "
            print(f"      {marker}  {info.modified}  {info.path}")
    if len(result.groups) > limit:
        print(f"\n  ... and {len(result.groups) - limit} more groups (use --limit)")


def _summary_line(result: core.ScanResult) -> str:
    return (
        f"{len(result.groups)} duplicate group(s) · "
        f"{result.duplicate_files} extra file(s) · "
        f"{core.human_size(result.wasted)} wasted"
    )


def cmd_scan(args: argparse.Namespace) -> int:
    options = _options(args)
    print(f"\n{BANNER}\n")
    for root in args.paths:
        print(f"  folder: {root}")
    print()
    try:
        result = core.find_duplicates(args.paths, options, progress=_progress_printer())
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n  scanned {result.files_scanned} file(s) in {result.elapsed:.1f}s "
          f"({result.candidates} compared, {result.errors} unreadable)")
    if not result.groups:
        print("\n  No duplicates found. Nothing to clean.\n")
        return 0

    print(f"\n  {_summary_line(result)}")
    _print_groups(result, args.limit)

    if args.json:
        print(f"\n  JSON report: {core.export_json(result, args.json)}")
    if args.csv:
        print(f"  CSV report:  {core.export_csv(result, args.csv)}")
    print(f"\n  Nothing was changed. To clean up:\n"
          f"    dupehunter clean \"{args.paths[0]}\" --dry-run\n")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    options = _options(args)
    print(f"\n{BANNER}\n")
    try:
        result = core.find_duplicates(args.paths, options, progress=_progress_printer())
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not result.groups:
        print("\n  No duplicates found. Nothing to do.\n")
        return 0

    action = "delete permanently" if args.delete else "move to quarantine"
    print(f"\n  {_summary_line(result)}")
    print(f"  keep policy: {args.keep}   action: {action}\n")
    _print_groups(result, args.limit)

    if args.dry_run:
        summary, _ = core.delete_duplicates(result.groups, args.keep, dry_run=True) \
            if args.delete else core.quarantine(result.groups, args.keep, args.quarantine, dry_run=True)
        count = summary.get("would_delete") or summary.get("would_move") or 0
        print(f"\n  dry run: would {action} {count} file(s), "
              f"freeing {core.human_size(summary.get('bytes', 0))}. Nothing changed.\n")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("  refusing to change files without --yes in a non-interactive shell.\n")
            return 2
        answer = input(f"  {action.capitalize()} now? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "نعم", "ن"):
            print("  cancelled.\n")
            return 0

    if args.delete:
        summary, journal = core.delete_duplicates(result.groups, args.keep)
        print(f"\n  deleted: {summary.get('deleted', 0)}   errors: {summary.get('error', 0)}   "
              f"freed: {core.human_size(summary.get('bytes', 0))}")
        if journal:
            print(f"  record: {journal}")
        print("  (permanent delete cannot be undone)\n")
        return 0 if summary.get("error", 0) == 0 else 1

    summary, journal = core.quarantine(result.groups, args.keep, args.quarantine,
                                       progress=_progress_printer())
    print(f"\n  quarantined: {summary.get('moved', 0)}   skipped: {summary.get('skipped', 0)}   "
          f"errors: {summary.get('error', 0)}   freed: {core.human_size(summary.get('bytes', 0))}")
    if journal:
        print(f"  undo with:  dupehunter restore \"{journal}\"")
    print()
    return 0 if summary.get("error", 0) == 0 else 1


def cmd_restore(args: argparse.Namespace) -> int:
    journal = Path(args.journal).expanduser() if args.journal else None
    summary, used = core.restore(journal, dry_run=args.dry_run)
    if not used:
        print("No quarantine journal found - nothing to restore.")
        return 1
    prefix = "would restore" if args.dry_run else "restored"
    print(f"\n{BANNER}\n  journal: {used}\n")
    print(f"  {prefix}: {summary.get('restored', 0)}   missing: {summary.get('missing', 0)}   "
          f"errors: {summary.get('error', 0)}\n")
    return 0


def cmd_journals(args: argparse.Namespace) -> int:
    journals = core.list_journals()
    if not journals:
        print(f"No journals yet in {core.journal_dir()}")
        return 0
    print(f"\n  {len(journals)} journal(s) in {core.journal_dir()}\n")
    for path in journals[: args.limit]:
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {stamp}   {path.name}")
    print()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(f"Tkinter is not available: {exc}", file=sys.stderr)
        return 1
    gui_main(args.paths)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dupehunter",
        description="Find files with identical content and free space safely.",
    )
    parser.add_argument("--version", action="version", version=f"DupeHunter {core.VERSION}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("paths", nargs="+", help="folders (or files) to scan")
    common.add_argument("--min-size", type=parse_size, default=1, metavar="SIZE",
                        help="ignore files smaller than this, e.g. 1MB (default: 1 byte)")
    common.add_argument("--fast", action="store_true",
                        help="hash only the start+end of each file (faster, verified again before acting)")
    common.add_argument("--exclude", action="append", metavar="DIR",
                        help="extra folder name to skip (repeatable)")
    common.add_argument("--no-recursive", action="store_true", help="do not enter subfolders")
    common.add_argument("--include-hidden", action="store_true", help="include hidden/system files")
    common.add_argument("-n", "--limit", type=int, default=10, help="how many groups to print")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="report duplicates, change nothing")
    scan.add_argument("--json", metavar="FILE", help="write a JSON report")
    scan.add_argument("--csv", metavar="FILE", help="write a CSV report")
    scan.set_defaults(func=cmd_scan)

    clean = sub.add_parser("clean", parents=[common], help="remove the extra copies")
    clean.add_argument("--keep", choices=core.KEEP_POLICIES, default="oldest",
                       help="which copy to keep (default: oldest)")
    clean.add_argument("--delete", action="store_true",
                       help="delete for real instead of moving to quarantine")
    clean.add_argument("--quarantine", metavar="DIR",
                       help=f"where to move duplicates (default: {core.default_quarantine_dir()})")
    clean.add_argument("--dry-run", action="store_true", help="show what would happen")
    clean.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    clean.set_defaults(func=cmd_clean)

    restore = sub.add_parser("restore", help="put quarantined files back")
    restore.add_argument("journal", nargs="?", help="journal file (default: most recent)")
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(func=cmd_restore)

    journals = sub.add_parser("journals", help="list quarantine/delete journals")
    journals.add_argument("-n", "--limit", type=int, default=20)
    journals.set_defaults(func=cmd_journals)

    gui = sub.add_parser("gui", help="open the desktop app")
    gui.add_argument("paths", nargs="*", help="folders to preload")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  cancelled.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
