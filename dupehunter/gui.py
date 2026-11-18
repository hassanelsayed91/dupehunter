"""Tkinter desktop app for DupeHunter - review every duplicate before it moves."""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import core
from .core import DupGroup, FileInfo, ScanOptions

BACKGROUND = "#f6f7fb"
CARD = "#ffffff"
MUTED = "#64748b"
DANGER = "#dc2626"
TEAL = "#0d9488"

CHECKED = "\u2611"   # ballot box with check
UNCHECKED = "\u2610"  # ballot box


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


class DupeHunterApp(tk.Tk):
    def __init__(self, initial_paths: list[str] | None = None) -> None:
        _enable_dpi_awareness()
        super().__init__()
        self.title(f"DupeHunter {core.VERSION}")
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1080, max(820, screen_w - 160))
        height = min(760, max(560, screen_h - 180))
        self.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{max(20, (screen_h - height) // 3)}")
        self.minsize(800, 520)
        self.configure(bg=BACKGROUND)

        self.result: core.ScanResult | None = None
        self.items: dict[str, FileInfo] = {}
        self.keep_items: set[str] = set()
        self.selected: set[str] = set()
        self.group_items: dict[str, DupGroup] = {}
        self.cancel_flag = threading.Event()
        self.scanning = False
        self.last_journal: Path | None = None

        self.min_size_var = tk.StringVar(value="1 KB")
        self.fast_var = tk.BooleanVar(value=False)
        self.keep_var = tk.StringVar(value="oldest")
        self.status_var = tk.StringVar(value="Add a folder, then press Scan.")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_styles()
        self._build_layout()
        self._set_icon()

        for path in initial_paths or []:
            self._add_path(path)
        if not initial_paths:
            downloads = Path.home() / "Downloads"
            if downloads.is_dir():
                self._add_path(str(downloads))
        self.after(300, self.start_scan)

    # ------------------------------------------------------------------ ui
    def _build_styles(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BACKGROUND, foreground="#0f172a")
        style.configure("Muted.TLabel", background=BACKGROUND, foreground=MUTED)
        style.configure("H1.TLabel", background=CARD, foreground="#0f172a",
                        font=("Segoe UI Semibold", 18))
        style.configure("Sub.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background="#0f172a", foreground="#e2e8f0",
                        font=("Segoe UI", 9), padding=(12, 6))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10))
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 10), foreground=DANGER)
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("Group.Treeview", font=("Segoe UI Semibold", 9))

    def _build_layout(self) -> None:
        header = ttk.Frame(self, style="Card.TFrame", padding=(20, 14))
        header.pack(fill="x")
        ttk.Label(header, text="DupeHunter", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Finds files with identical content. Nothing moves until you choose it.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self, padding=(20, 12, 20, 6))
        body.pack(fill="both", expand=True)

        # folders
        folders = ttk.Frame(body)
        folders.pack(fill="x")
        ttk.Label(folders, text="Folders", width=9).pack(side="left", anchor="n", pady=4)
        list_wrap = ttk.Frame(folders)
        list_wrap.pack(side="left", fill="x", expand=True)
        self.folder_list = tk.Listbox(list_wrap, height=3, font=("Segoe UI", 9),
                                      activestyle="none", selectmode="extended",
                                      borderwidth=1, relief="solid")
        self.folder_list.pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(folders)
        buttons.pack(side="left", padx=(8, 0), fill="y")
        ttk.Button(buttons, text="Add folder", command=self.add_folder).pack(fill="x")
        ttk.Button(buttons, text="Add file", command=self.add_file).pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="Remove", command=self.remove_selected_folder).pack(fill="x", pady=(4, 0))

        # options
        options = ttk.Frame(body)
        options.pack(fill="x", pady=(10, 6))
        ttk.Label(options, text="Options", width=9).pack(side="left")
        ttk.Label(options, text="Ignore files under").pack(side="left")
        size_box = ttk.Combobox(options, textvariable=self.min_size_var, width=9,
                                values=("0", "1 KB", "10 KB", "100 KB", "1 MB", "10 MB", "100 MB"))
        size_box.pack(side="left", padx=(6, 14))
        ttk.Checkbutton(options, text="Fast scan (start+end hash)", variable=self.fast_var).pack(side="left")
        ttk.Label(options, text="Keep").pack(side="left", padx=(18, 4))
        keep_box = ttk.Combobox(options, textvariable=self.keep_var, width=9, state="readonly",
                                values=core.KEEP_POLICIES)
        keep_box.pack(side="left")
        keep_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_keep_policy())

        # actions
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 8))
        self.scan_btn = ttk.Button(actions, text="Scan for duplicates", style="Accent.TButton",
                                   command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Export report", command=self.export_report).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open quarantine", command=self.open_quarantine).pack(side="right")
        self.progress = ttk.Progressbar(actions, variable=self.progress_var, length=180)
        self.progress.pack(side="right", padx=(8, 12))

        # results
        table_frame = ttk.Frame(body, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=("size", "modified", "folder"),
                                 show="tree headings", selectmode="none")
        self.tree.heading("#0", text="File")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("folder", text="Folder")
        self.tree.column("#0", width=380, stretch=True, anchor="w")
        self.tree.column("size", width=90, stretch=False, anchor="e")
        self.tree.column("modified", width=135, stretch=False, anchor="w")
        self.tree.column("folder", width=340, stretch=True, anchor="w")
        self.tree.tag_configure("group", background="#eef2ff", font=("Segoe UI Semibold", 9))
        self.tree.tag_configure("dup", background="#ffffff")
        self.tree.tag_configure("keep", background="#ecfdf5", foreground="#065f46")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _e: self.toggle_selected_rows())

        # bottom bar
        bar = ttk.Frame(self, padding=(20, 6, 20, 8))
        bar.pack(fill="x")
        self.quarantine_btn = ttk.Button(bar, text="Move selected to quarantine",
                                         style="Accent.TButton", command=self.quarantine_selected)
        self.quarantine_btn.pack(side="left")
        self.delete_btn = ttk.Button(bar, text="Delete selected permanently",
                                     style="Danger.TButton", command=self.delete_selected)
        self.delete_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Restore last quarantine", command=self.restore_last).pack(side="left", padx=(8, 0))

        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(
            fill="x", side="bottom")

    # -------------------------------------------------------------- helpers
    def _set_icon(self) -> None:
        """Use the project icon when running from source (the exe carries it)."""
        icon = Path(__file__).resolve().parent.parent / "assets" / "dupehunter.ico"
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))
            except Exception:  # noqa: BLE001
                pass

    def _add_path(self, path: str) -> None:
        existing = list(self.folder_list.get(0, "end"))
        if path not in existing:
            self.folder_list.insert("end", path)

    def _paths(self) -> list[str]:
        return list(self.folder_list.get(0, "end"))

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Folder to scan")
        if chosen:
            self._add_path(chosen)

    def add_file(self) -> None:
        chosen = filedialog.askopenfilename(title="Add a single file")
        if chosen:
            self._add_path(chosen)

    def remove_selected_folder(self) -> None:
        for index in reversed(self.folder_list.curselection()):
            self.folder_list.delete(index)

    # --------------------------------------------------------------- scanning
    def _scan_options(self) -> ScanOptions:
        options = ScanOptions()
        try:
            options.min_size = core.parse_size(self.min_size_var.get())
        except ValueError:
            options.min_size = 1
        options.fast = self.fast_var.get()
        return options

    def start_scan(self) -> None:
        if self.scanning:
            return
        paths = self._paths()
        if not paths:
            messagebox.showwarning("DupeHunter", "Add at least one folder to scan.")
            return
        self.scanning = True
        self.cancel_flag.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress_var.set(0)
        self.status_var.set("Scanning...")
        threading.Thread(target=self._scan_worker, args=(paths, self._scan_options()), daemon=True).start()

    def _scan_worker(self, paths: list[str], options: ScanOptions) -> None:
        try:
            result = core.find_duplicates(
                paths, options,
                progress=lambda stage, done, total: self.after(0, self._on_progress, stage, done, total),
                cancel=self.cancel_flag.is_set,
            )
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._scan_failed, str(exc))
            return
        self.after(0, self._scan_done, result)

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        if total:
            self.progress_var.set(done * 100 / total)
            self.status_var.set(f"{stage.capitalize()} {done:,} / {total:,}")

    def _scan_failed(self, message: str) -> None:
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Scan failed.")
        messagebox.showerror("DupeHunter", message)

    def _scan_done(self, result: core.ScanResult) -> None:
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress_var.set(0)
        self.result = result
        self._populate()
        if not result.groups:
            self.status_var.set(
                f"No duplicates in {result.files_scanned:,} files "
                f"({result.elapsed:.1f}s). Nothing to clean."
            )
        else:
            self.status_var.set(
                f"{len(result.groups)} groups · {result.duplicate_files} extra files · "
                f"{core.human_size(result.wasted)} wasted · all duplicates pre-selected"
            )

    def stop_scan(self) -> None:
        self.cancel_flag.set()
        self.status_var.set("Stopping...")

    # ----------------------------------------------------------------- table
    def _populate(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.keep_items.clear()
        self.selected.clear()
        self.group_items.clear()
        if not self.result:
            return
        policy = self.keep_var.get()
        for index, group in enumerate(self.result.groups, start=1):
            survivor = group.survivor(policy)
            parent = self.tree.insert(
                "", "end", open=index <= 8,
                text=f"{len(group.files)} copies · {core.human_size(group.size)} each · "
                     f"{core.human_size(group.wasted)} wasted",
                values=("", "", ""), tags=("group",),
            )
            self.group_items[parent] = group
            for info in group.files:
                is_keep = info.path == survivor.path
                label = f"{'KEEP' if is_keep else CHECKED}  {info.name}"
                item = self.tree.insert(
                    parent, "end", text=label,
                    values=(core.human_size(info.size), info.modified, info.folder),
                    tags=("keep",) if is_keep else ("dup",),
                )
                self.items[item] = info
                if is_keep:
                    self.keep_items.add(item)
                else:
                    self.selected.add(item)
        self._update_selection_status()

    def _apply_keep_policy(self) -> None:
        if self.result and self.result.groups:
            self._populate()

    def _on_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row in self.items and column == "#0":
            self._toggle(row)
            return "break"
        return None

    def _toggle(self, item: str) -> None:
        info = self.items[item]
        if item in self.keep_items:
            return  # the survivor is never a candidate for removal
        if item in self.selected:
            self.selected.discard(item)
            label = f"{UNCHECKED}  {info.name}"
        else:
            self.selected.add(item)
            label = f"{CHECKED}  {info.name}"
        self.tree.item(item, text=label)
        self._update_selection_status()

    def toggle_selected_rows(self) -> None:
        focus = self.tree.focus()
        if focus in self.items:
            self._toggle(focus)

    def _selected_files(self) -> list[FileInfo]:
        return [self.items[item] for item in self.selected if item in self.items]

    def _update_selection_status(self) -> None:
        if not self.items:
            self.status_var.set("Nothing selected.")
            return
        files = self._selected_files()
        total = sum(f.size for f in files)
        groups = len({self.tree.parent(item) for item in self.items})
        self.status_var.set(
            f"{groups} group(s) · {len(files)} file(s) selected · "
            f"{core.human_size(total)} to free"
        )

    # --------------------------------------------------------------- actions
    def _remove_rows(self, removed: list[FileInfo]) -> None:
        gone = {str(f.path) for f in removed}
        for item, info in list(self.items.items()):
            if str(info.path) in gone:
                self.tree.delete(item)
                self.items.pop(item, None)
                self.selected.discard(item)
        for parent, group in list(self.group_items.items()):
            if not self.tree.exists(parent):
                self.group_items.pop(parent, None)
            elif not self.tree.get_children(parent):
                self.tree.delete(parent)
                self.group_items.pop(parent, None)

    def quarantine_selected(self) -> None:
        files = self._selected_files()
        if not files:
            messagebox.showinfo("DupeHunter", "Select the copies you want to move first.")
            return
        total = sum(f.size for f in files)
        if not messagebox.askyesno(
            "Move to quarantine",
            f"Move {len(files)} file(s) ({core.human_size(total)}) to the quarantine folder?\n\n"
            "You can restore them with one click.",
        ):
            return
        self.config(cursor="watch")
        self.update_idletasks()
        summary, journal = core.quarantine_files([f.path for f in files])
        self.config(cursor="")
        self.last_journal = journal
        moved = summary.get("moved", 0)
        if moved:
            self._remove_rows(files)
            self._update_selection_status()
        messagebox.showinfo(
            "Quarantine",
            f"Moved: {summary.get('moved', 0)}\n"
            f"Errors: {summary.get('error', 0)}\n"
            f"Freed: {core.human_size(summary.get('bytes', 0))}"
            + (f"\n\nRestore from:\n{journal}" if journal else ""),
        )

    def delete_selected(self) -> None:
        files = self._selected_files()
        if not files:
            messagebox.showinfo("DupeHunter", "Select the copies you want to delete first.")
            return
        total = sum(f.size for f in files)
        if not messagebox.askyesno(
            "Delete permanently",
            f"Delete {len(files)} file(s) ({core.human_size(total)})?\n\n"
            "This cannot be undone. Quarantine is the safe option.",
            icon="warning",
        ):
            return
        self.config(cursor="watch")
        self.update_idletasks()
        summary, _journal = core.delete_files([f.path for f in files])
        self.config(cursor="")
        if summary.get("deleted", 0):
            self._remove_rows(files)
            self._update_selection_status()
        messagebox.showinfo(
            "Deleted",
            f"Deleted: {summary.get('deleted', 0)}\n"
            f"Errors: {summary.get('error', 0)}\n"
            f"Freed: {core.human_size(summary.get('bytes', 0))}",
        )

    def restore_last(self) -> None:
        journal = self.last_journal or core.latest_journal("quarantine")
        if not journal:
            messagebox.showinfo("DupeHunter", "No quarantine journal yet.")
            return
        if not messagebox.askyesno("Restore", f"Put the files back from:\n{journal.name}?"):
            return
        summary, _ = core.restore(journal)
        messagebox.showinfo(
            "Restored",
            f"Restored: {summary.get('restored', 0)}\n"
            f"Missing: {summary.get('missing', 0)}\n"
            f"Errors: {summary.get('error', 0)}",
        )
        self.start_scan()

    def export_report(self) -> None:
        if not self.result or not self.result.groups:
            messagebox.showinfo("DupeHunter", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save duplicate report", defaultextension=".json",
            filetypes=[("JSON report", "*.json"), ("CSV report", "*.csv")],
            initialfile="dupehunter-report.json",
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() == ".csv":
            core.export_csv(self.result, target)
        else:
            core.export_json(self.result, target)
        messagebox.showinfo("Saved", f"Report saved to:\n{target}")

    def open_quarantine(self) -> None:
        folder = core.default_quarantine_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            subprocess.Popen(["explorer", str(folder)])


def main(initial_paths: list[str] | None = None) -> None:
    app = DupeHunterApp(initial_paths)
    app.mainloop()


if __name__ == "__main__":
    main()
