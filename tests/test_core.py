"""Tests for the DupeHunter core. Run with: python -m pytest -q"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dupehunter import core


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.root / "_appdata")
        self.quarantine = self.root / "_quarantine"

    def tearDown(self) -> None:
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        self._tmp.cleanup()

    def write(self, name: str, data: bytes = b"same content", mtime: float | None = None) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def scan(self, *roots, **kwargs) -> core.ScanResult:
        options = core.ScanOptions(**kwargs)
        return core.find_duplicates(list(roots) or [self.root], options)


class FindTests(BaseCase):
    def test_finds_same_content_with_different_names(self) -> None:
        self.write("report.pdf", b"hello world")
        self.write("copy/report-final.pdf", b"hello world")
        result = self.scan()
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].files), 2)
        self.assertEqual(result.wasted, len(b"hello world"))

    def test_same_size_different_content_is_not_duplicate(self) -> None:
        self.write("a.bin", b"AAAA")
        self.write("b.bin", b"BBBB")
        self.assertEqual(self.scan().groups, [])

    def test_unique_files_are_not_reported(self) -> None:
        self.write("only.txt", b"nothing else like me")
        self.assertEqual(self.scan().groups, [])

    def test_three_copies_waste_two_copies(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name, b"x" * 100)
        result = self.scan()
        self.assertEqual(result.wasted, 200)
        self.assertEqual(result.duplicate_files, 2)

    def test_min_size_filters_small_files(self) -> None:
        self.write("a.txt", b"tiny")
        self.write("b.txt", b"tiny")
        self.assertEqual(self.scan(min_size=10).groups, [])

    def test_empty_files_are_ignored_by_default(self) -> None:
        self.write("a.empty", b"")
        self.write("b.empty", b"")
        self.assertEqual(self.scan().groups, [])
        self.assertEqual(len(self.scan(min_size=0).groups), 1)

    def test_dot_hidden_files_are_skipped(self) -> None:
        self.write(".hidden.cache", b"same")
        self.write("visible.cache", b"same")
        self.assertEqual(self.scan().groups, [])
        self.assertEqual(len(self.scan(include_hidden=True).groups), 1)

    def test_excluded_folder_is_skipped(self) -> None:
        self.write("photo.jpg", b"duplicate bytes")
        self.write("node_modules/photo.jpg", b"duplicate bytes")
        self.assertEqual(self.scan().groups, [])
        result = self.scan(exclude_dirs=[])
        self.assertEqual(len(result.groups), 1)

    def test_excluded_glob_is_skipped(self) -> None:
        self.write("a.tmp", b"temp bytes")
        self.write("b.tmp", b"temp bytes")
        self.assertEqual(self.scan().groups, [])
        self.assertEqual(len(self.scan(exclude_globs=[]).groups), 1)

    def test_multiple_roots(self) -> None:
        first = self.root / "one"
        second = self.root / "two"
        self.write("one/song.mp3", b"music")
        self.write("two/song.mp3", b"music")
        result = core.find_duplicates([first, second])
        self.assertEqual(len(result.groups), 1)

    def test_single_file_root(self) -> None:
        target = self.write("solo.txt", b"unique")
        result = core.find_duplicates([target])
        self.assertEqual(result.files_scanned, 1)
        self.assertEqual(result.groups, [])

    def test_hardlinks_are_not_duplicates(self) -> None:
        original = self.write("original.bin", b"hard link content")
        link = self.root / "link.bin"
        try:
            os.link(original, link)
        except (OSError, NotImplementedError):
            self.skipTest("hard links not supported here")
        self.assertEqual(self.scan().groups, [])

    def test_fast_scan_samples_can_false_positive_and_verify_catches_it(self) -> None:
        head = b"H" * 100_000
        tail = b"T" * 100_000
        self.write("a.bin", head + b"1111" + tail)
        self.write("b.bin", head + b"2222" + tail)
        self.assertEqual(self.scan().groups, [])               # full hash: different files
        fast = self.scan(fast=True)                            # start+end look identical
        self.assertEqual(len(fast.groups), 1)
        self.assertEqual(core.verify_groups(fast.groups), [])   # verification saves the day

    def test_report_properties(self) -> None:
        for name in ("a.txt", "b.txt"):
            self.write(name, b"12345")
        result = self.scan()
        self.assertGreaterEqual(result.elapsed, 0)
        self.assertEqual(result.total_files, 2)
        self.assertEqual(result.duplicate_files, 1)
        self.assertEqual(result.errors, 0)


class PolicyTests(BaseCase):
    def group_of_three(self) -> core.DupGroup:
        oldest = self.write("old/older.txt", b"same", mtime=1_000_000)
        middle = self.write("mid.txt", b"same", mtime=2_000_000)
        newest = self.write("newest-very-long-name.txt", b"same", mtime=3_000_000)
        infos = [core.FileInfo(p, 4, p.stat().st_mtime) for p in (oldest, newest, middle)]
        return core.DupGroup(size=4, digest="abc", files=infos)

    def test_oldest_policy(self) -> None:
        group = self.group_of_three()
        self.assertEqual(group.survivor("oldest").path, self.root / "old/older.txt")

    def test_newest_policy(self) -> None:
        group = self.group_of_three()
        self.assertEqual(group.survivor("newest").path, self.root / "newest-very-long-name.txt")

    def test_shortest_policy(self) -> None:
        group = self.group_of_three()
        self.assertEqual(group.survivor("shortest").path, self.root / "mid.txt")

    def test_first_policy_is_alphabetical(self) -> None:
        group = self.group_of_three()
        self.assertEqual(group.survivor("first").path, self.root / "mid.txt")

    def test_removals_are_the_other_copies(self) -> None:
        group = self.group_of_three()
        self.assertEqual(len(group.removals("oldest")), 2)

    def test_empty_group_raises(self) -> None:
        with self.assertRaises(ValueError):
            core.choose_survivor([], "oldest")


class QuarantineTests(BaseCase):
    def three_copies(self) -> core.ScanResult:
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name, b"same bytes here")
        return self.scan()

    def test_quarantine_moves_extras_and_keeps_one(self) -> None:
        result = self.three_copies()
        summary, journal = core.quarantine(result.groups, "oldest", dest=self.quarantine)
        self.assertEqual(summary["moved"], 2)
        self.assertTrue(journal and journal.exists())
        survivors = [p for p in self.root.glob("*.txt")]
        self.assertEqual(len(survivors), 1)
        quarantined = list(self.quarantine.rglob("*.txt"))
        self.assertEqual(len(quarantined), 2)

    def test_quarantine_preserves_content(self) -> None:
        result = self.three_copies()
        core.quarantine(result.groups, "oldest", dest=self.quarantine)
        for path in self.quarantine.rglob("*.txt"):
            self.assertEqual(path.read_bytes(), b"same bytes here")

    def test_quarantine_is_reversible(self) -> None:
        result = self.three_copies()
        _summary, journal = core.quarantine(result.groups, "oldest", dest=self.quarantine)
        restored, _ = core.restore(journal)
        self.assertEqual(restored["restored"], 2)
        self.assertEqual(len(list(self.root.glob("*.txt"))), 3)

    def test_restore_twice_is_harmless(self) -> None:
        result = self.three_copies()
        _s, journal = core.quarantine(result.groups, "oldest", dest=self.quarantine)
        core.restore(journal)
        again, _ = core.restore(journal)
        self.assertEqual(again["restored"], 0)

    def test_restore_without_journal(self) -> None:
        summary, used = core.restore()
        self.assertIsNone(used)
        self.assertEqual(summary["missing"], 1)

    def test_quarantine_dry_run_changes_nothing(self) -> None:
        result = self.three_copies()
        summary, journal = core.quarantine(result.groups, "oldest", dest=self.quarantine, dry_run=True)
        self.assertEqual(summary["would_move"], 2)
        self.assertIsNone(journal)
        self.assertEqual(len(list(self.root.glob("*.txt"))), 3)

    def test_unique_path_never_overwrites(self) -> None:
        target = self.write("dup.txt", b"payload")
        unique = core._unique_path(target)
        self.assertEqual(unique.name, "dup (1).txt")

    def test_quarantine_keeps_both_copies_with_same_name(self) -> None:
        first = self.root / "keep" / "same.txt"
        second = self.root / "other" / "same.txt"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"payload")
        second.write_bytes(b"payload")
        _s1, journal1 = core.quarantine_files([second], dest=self.quarantine)
        _s2, journal2 = core.quarantine_files([first], dest=self.quarantine)
        self.assertTrue(journal1 and journal1.exists())
        self.assertTrue(journal2 and journal2.exists())
        quarantined = list(self.quarantine.rglob("*.txt"))
        self.assertEqual(len(quarantined), 2)
        self.assertEqual({p.read_bytes() for p in quarantined}, {b"payload"})

    def test_quarantine_files_moves_exact_list(self) -> None:
        one = self.write("one.txt", b"1")
        two = self.write("two.txt", b"2")
        summary, journal = core.quarantine_files([two], dest=self.quarantine)
        self.assertEqual(summary["moved"], 1)
        self.assertTrue(one.exists())
        self.assertFalse(two.exists())
        self.assertTrue(journal.exists())

    def test_quarantine_of_nothing(self) -> None:
        summary, journal = core.quarantine_files([], dest=self.quarantine)
        self.assertEqual(summary, {})
        self.assertIsNone(journal)

    def test_false_group_is_not_moved_after_verification(self) -> None:
        first = self.write("x.bin", b"content one")
        second = self.write("y.bin", b"content two")
        fake = core.DupGroup(size=11, digest="fake", files=[
            core.FileInfo(first, 11, 0, digest="fake"),
            core.FileInfo(second, 11, 0, digest="fake"),
        ])
        summary, journal = core.quarantine([fake], dest=self.quarantine)
        self.assertEqual(summary.get("moved", 0), 0)
        self.assertIsNone(journal)
        self.assertTrue(first.exists() and second.exists())


class DeleteTests(BaseCase):
    def test_delete_removes_only_extras(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name, b"triple")
        result = self.scan()
        summary, journal = core.delete_duplicates(result.groups, "oldest")
        self.assertEqual(summary["deleted"], 2)
        self.assertEqual(len(list(self.root.glob("*.txt"))), 1)
        self.assertTrue(journal and journal.exists())

    def test_delete_dry_run(self) -> None:
        for name in ("a.txt", "b.txt"):
            self.write(name, b"pair")
        result = self.scan()
        summary, journal = core.delete_duplicates(result.groups, "oldest", dry_run=True)
        self.assertEqual(summary["would_delete"], 1)
        self.assertIsNone(journal)
        self.assertEqual(len(list(self.root.glob("*.txt"))), 2)

    def test_delete_files_exact_list(self) -> None:
        first = self.write("keep.txt", b"keep")
        second = self.write("drop.txt", b"drop")
        summary, _ = core.delete_files([second])
        self.assertEqual(summary["deleted"], 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())

    def test_delete_journal_records_paths(self) -> None:
        target = self.write("gone.txt", b"bye")
        _summary, journal = core.delete_files([target])
        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(data["action"], "delete")
        self.assertEqual(data["moves"][0]["path"], str(target))
        self.assertEqual(data["moves"][0]["status"], "deleted")


class ReportTests(BaseCase):
    def test_export_json(self) -> None:
        for name in ("a.txt", "b.txt"):
            self.write(name, b"report me")
        result = self.scan()
        out = core.export_json(result, self.root / "out" / "report.json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["totals"]["groups"], 1)
        self.assertEqual(data["totals"]["duplicate_files"], 1)
        self.assertEqual(data["groups"][0]["files"][0]["path"].endswith(".txt"), True)

    def test_export_csv(self) -> None:
        for name in ("a.txt", "b.txt"):
            self.write(name, b"csv me")
        result = self.scan()
        out = core.export_csv(result, self.root / "report.csv")
        lines = out.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(lines[0].split(",")[:2], ["group", "size"])
        self.assertEqual(len(lines), 3)


class HelperTests(unittest.TestCase):
    def test_parse_size(self) -> None:
        self.assertEqual(core.parse_size("1"), 1)
        self.assertEqual(core.parse_size("1 KB"), 1024)
        self.assertEqual(core.parse_size("2mb"), 2 * 1024 ** 2)
        self.assertEqual(core.parse_size("1.5 GB"), int(1.5 * 1024 ** 3))
        with self.assertRaises(ValueError):
            core.parse_size("banana")

    def test_human_size(self) -> None:
        self.assertEqual(core.human_size(0), "0 B")
        self.assertEqual(core.human_size(2048), "2.0 KB")
        self.assertEqual(core.human_size(1024 ** 3), "1.0 GB")

    def test_keep_policies_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            core.quarantine([], policy="bogus")


if __name__ == "__main__":
    unittest.main()
