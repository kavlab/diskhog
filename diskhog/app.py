"""Interactive Textual TUI for diskhog."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from typing import Callable, cast

from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from .scanner import (
    IS_WINDOWS,
    Entry,
    ProgressInfo,
    ScanResult,
    fmt_count,
    fmt_duration,
    fmt_mtime,
    fmt_size,
    scan_root,
)

MAX_CHILD_NODES = 500  # children shown under one tree node
BAR_WIDTH = 20         # width of the size bar in labels
LARGEST_N = 100        # rows on the "largest files" screen


def _make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        _make_writable(path)
        os.remove(path)


def _rmtree_retry(func, path, _exc) -> None:
    """shutil.rmtree onerror hook: clear read-only flag and retry."""
    _make_writable(path)
    func(path)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation dialog; dismisses with True or False."""

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 76;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No", show=False),
    ]

    def __init__(self, message: Text) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(self._message, id="dialog", markup=False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class LargestFilesScreen(Screen):
    """Table of the largest files found by the scan."""

    CSS = "DataTable { height: 1fr; }"

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("d", "delete_trash", "Delete (recycle bin)"),
        Binding("D", "delete_perm", "Delete FOREVER"),
    ]

    def __init__(self, entries: list[Entry]) -> None:
        super().__init__()
        self._entries = list(entries)

    def compose(self) -> ComposeResult:
        yield DataTable(id="tbl")

    def on_mount(self) -> None:
        table = self.query_one("#tbl", DataTable)
        table.cursor_type = "row"
        table.add_columns("Size", "Share", "Modified", "Path")
        total = self._hog().result.root.size_disk
        for entry in self._entries:
            share = entry.size_disk / total * 100 if total else 0.0
            table.add_row(
                fmt_size(entry.size_disk),
                f"{share:5.1f}%",
                fmt_mtime(entry.mtime),
                entry.path,
                key=entry.path,
            )

    def _hog(self) -> "DiskHogApp":
        return cast("DiskHogApp", self.app)

    def _selected(self) -> Entry | None:
        table = self.query_one("#tbl", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        entry = next((e for e in self._entries if e.path == event.row_key.value), None)
        if entry is not None:
            self._reveal(entry)

    def _reveal(self, entry: Entry) -> None:
        app = self._hog()
        self.dismiss()
        app.call_after_refresh(app.reveal_entry, entry)

    def action_back(self) -> None:
        self.dismiss()

    def action_delete_trash(self) -> None:
        self._delete(False)

    def action_delete_perm(self) -> None:
        self._delete(True)

    def _delete(self, permanent: bool) -> None:
        entry = self._selected()
        if entry is None:
            return
        self._hog().confirm_and_delete(entry, permanent, on_done=self._on_deleted)

    def _on_deleted(self, ok: bool, entry: Entry) -> None:
        if not ok:
            return
        try:
            self.query_one("#tbl", DataTable).remove_row(entry.path)
        except Exception:
            pass
        if entry in self._entries:
            self._entries.remove(entry)


class DiskHogApp(App):
    """Scan a directory tree, explore sizes, delete the space hogs."""

    TITLE = "diskhog"

    CSS = """
    #tree {
        height: 1fr;
        padding: 0 1;
    }
    #details {
        height: 9;
        border-top: heavy $accent;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("f", "largest", "Largest files"),
        Binding("d", "delete_trash", "Delete (recycle bin)"),
        Binding("D", "delete_perm", "Delete FOREVER"),
        Binding("h", "toggle_files", "Show/hide files"),
        Binding("o", "open_location", "Open in file manager"),
        Binding("r", "rescan", "Rescan"),
        Binding("s", "stop_scan", "Stop scan"),
        Binding("right", "expand_cursor", "Expand", show=False),
        Binding("left", "collapse_cursor", "Collapse", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, root_path: str) -> None:
        super().__init__()
        self.root_path = root_path
        self.result: ScanResult | None = None
        self.scanning = False
        self._stop_flag = False
        self._scan_state: ProgressInfo | None = None
        self._tree: Tree = None  # type: ignore[assignment]
        self._details: Static = None  # type: ignore[assignment]
        self._tnodes: dict[str, TreeNode] = {}
        self._loaded: set[str] = set()
        self._show_files = True
        self._freed = 0
        self._largest: list[Entry] | None = None

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("", id="tree")
        yield Static("", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._tree = self.query_one("#tree", Tree)
        self._details = self.query_one("#details", Static)
        self._tree.focus()
        self.set_interval(0.25, self._tick)
        self._start_scan()

    # -------------------------------------------------------------- scan

    def _start_scan(self) -> None:
        self.scanning = True
        self._stop_flag = False
        self._scan_state = None
        self.result = None
        self._largest = None
        self._freed = 0
        self._clear_tree()
        self._tree.root.data = None
        self._tree.root.set_label(Text("scanning…", style="dim"))
        self._details.update(Text("Scanning…", style="dim"))
        self.sub_title = f"scanning {self.root_path}"
        self.run_worker(self._scan_worker, thread=True, exclusive=True, group="scan")

    def _scan_worker(self) -> None:
        try:
            result = scan_root(
                self.root_path,
                stop=lambda: self._stop_flag,
                progress=self._on_progress,
            )
        except Exception as exc:
            self.call_from_thread(self._scan_failed, exc)
            return
        self.call_from_thread(self._scan_done, result)

    def _on_progress(self, info: ProgressInfo) -> None:
        self._scan_state = info

    def _scan_done(self, result: ScanResult) -> None:
        self.result = result
        self.scanning = False
        self._scan_state = None
        self._build_root()
        self._update_header()
        if result.cancelled:
            self.notify(
                "Scan stopped — partial results shown (unscanned folders marked with ?)",
                severity="warning", timeout=10,
            )
        if result.errors:
            self.notify(
                f"{fmt_count(result.errors)} item(s) could not be read (marked with ⚠)",
                severity="warning", timeout=10,
            )

    def _scan_failed(self, exc: Exception) -> None:
        self.scanning = False
        self.notify(f"Scan failed: {exc}", severity="error", timeout=10)

    def _tick(self) -> None:
        if not self.scanning:
            return
        info = self._scan_state
        if info is None:
            return
        text = Text()
        text.append("Scanning  ")
        text.append(
            f"{fmt_count(info.files)} files · {fmt_count(info.dirs)} dirs · "
            f"{fmt_size(info.bytes_disk)} · {fmt_duration(info.elapsed)}"
        )
        if info.errors:
            text.append(f" · {fmt_count(info.errors)} errors", style="yellow")
        text.append("\n" + info.path, style="dim")
        self._details.update(text)

    # -------------------------------------------------------------- tree

    def _clear_tree(self) -> None:
        root_node = self._tree.root
        root_node.collapse()
        for child in list(root_node.children):
            try:
                child.remove()
            except Exception:
                pass
        self._tnodes.clear()
        self._loaded.clear()

    def _build_root(self) -> None:
        root_node = self._tree.root
        root = self.result.root
        root_node.data = root
        root_node.set_label(self._root_label())
        root_node.expand()
        self._populate(root_node)
        self._update_details(root)

    def _rebuild_tree(self) -> None:
        self._clear_tree()
        self._build_root()

    def _populate(self, node: TreeNode) -> None:
        if self.result is None:
            return
        entry = node.data
        if not isinstance(entry, Entry) or not entry.is_dir or entry.link:
            return
        if entry.path in self._loaded:
            return
        if entry.children is None:
            # scan was cancelled before this folder was reached
            self._loaded.add(entry.path)
            extra = node.add(
                Text("(not scanned — press r to rescan)", style="dim italic"), data=None
            )
            extra.allow_expand = False
            return
        self._loaded.add(entry.path)
        kids = [c for c in entry.children if c.is_dir or self._show_files]
        kids.sort(key=lambda c: (-c.size_disk, c.name.lower()))
        hidden_count = 0
        if len(kids) > MAX_CHILD_NODES:
            hidden_count = len(kids) - MAX_CHILD_NODES
            kids = kids[:MAX_CHILD_NODES]
        if not kids:
            node.allow_expand = False
            return
        pad = min(max(len(c.name) for c in kids), 48)
        entry.pad = pad
        for child_entry in kids:
            child_node = node.add(self._label(child_entry, pad), data=child_entry)
            child_node.allow_expand = child_entry.is_dir and not child_entry.link
            self._tnodes[child_entry.path] = child_node
        if hidden_count:
            extra = node.add(
                Text(f"… {fmt_count(hidden_count)} more item(s) not shown", style="dim italic"),
                data=None,
            )
            extra.allow_expand = False

    def _label(self, entry: Entry, pad: int) -> Text:
        total = self.result.root.size_disk if self.result is not None else 0
        name = entry.name
        if len(name) > pad:
            name = name[: pad - 1] + "…"
        if entry.is_dir:
            name_style = "bold cyan"
        elif entry.link:
            name_style = "dim"
        else:
            name_style = ""
        label = Text()
        label.append(name.ljust(pad), name_style)
        label.append("  ")
        if total > 0 and entry.size_disk > 0:
            frac = min(entry.size_disk / total, 1.0)
            filled = round(frac * BAR_WIDTH)
            if filled >= 1:
                label.append("█" * filled + "░" * (BAR_WIDTH - filled),
                             "cyan" if entry.is_dir else "green")
            else:
                label.append(" " * BAR_WIDTH)
        else:
            label.append(" " * BAR_WIDTH)
        label.append(" ")
        label.append(fmt_size(entry.size_disk).rjust(11))
        label.append("  ")
        share = entry.size_disk / total * 100 if total > 0 else 0.0
        label.append(f"{share:5.1f}%", "dim")
        if entry.error is not None:
            label.append(" ⚠", "bold red")
        elif entry.is_dir and not entry.scanned and entry.children is None:
            label.append(" ?", "yellow")
        return label

    def _root_label(self) -> Text:
        r = self.result
        label = Text()
        label.append(r.root.name or r.root.path, "bold cyan")
        label.append(f"  {fmt_size(r.root.size_disk)}", "bold")
        label.append(f"  ·  {fmt_count(r.files)} files, {fmt_count(r.dirs)} folders", "dim")
        if r.cancelled:
            label.append("  ·  partial", "yellow")
        return label

    def _refresh_labels(self) -> None:
        if self.result is None:
            return
        self._tree.root.set_label(self._root_label())
        root_entry = self.result.root
        for child_node in self._tree.root.children:
            child_entry = child_node.data
            if isinstance(child_entry, Entry):
                child_node.set_label(self._label(child_entry, root_entry.pad or 16))
        for path in list(self._loaded):
            if path == root_entry.path:
                continue
            parent_node = self._tnodes.get(path)
            if parent_node is None:
                continue
            parent_entry = parent_node.data
            if not isinstance(parent_entry, Entry) or not parent_entry.children:
                continue
            pad = parent_entry.pad or 16
            for child_node in parent_node.children:
                child_entry = child_node.data
                if isinstance(child_entry, Entry):
                    child_node.set_label(self._label(child_entry, pad))

    # ----------------------------------------------------------- details

    def _update_details(self, entry: Entry | None) -> None:
        if entry is None:
            self._details.update(Text("Nothing selected", style="dim"))
            return
        total = self.result.root.size_disk if self.result is not None else 0
        t = Text()
        t.append(entry.path, "bold")
        t.append("\n")
        t.append("on disk: ", "dim")
        t.append(fmt_size(entry.size_disk), "bold")
        if entry.size_logical:
            t.append("   logical: ", "dim")
            t.append(fmt_size(entry.size_logical))
        if total:
            t.append("   share: ", "dim")
            t.append(f"{entry.size_disk / total * 100:.1f}%")
        if entry.is_dir and not entry.link:
            t.append("   files: ", "dim")
            t.append(fmt_count(entry.files))
            t.append("   dirs: ", "dim")
            t.append(fmt_count(entry.dirs))
        t.append("   modified: ", "dim")
        t.append(fmt_mtime(entry.mtime))
        if entry.link:
            t.append("\nsymlink / junction — contents not counted", "yellow")
        if entry.is_dir and not entry.scanned and entry.children is None:
            t.append("\nnot scanned (scan was stopped)", "yellow")
        if entry.error:
            t.append(f"\nerror: {entry.error}", "red")
        self._details.update(t)

    def _update_header(self) -> None:
        r = self.result
        if r is None:
            self.sub_title = "scanning…"
            return
        parts = [
            self.root_path,
            fmt_size(r.root.size_disk),
            f"{fmt_count(r.files)} files",
            f"scanned in {fmt_duration(r.duration)}",
        ]
        if self._freed:
            parts.append(f"freed {fmt_size(self._freed)}")
        if r.cancelled:
            parts.append("partial")
        try:
            du = shutil.disk_usage(self.root_path)
            parts.append(f"volume: {fmt_size(du.total)}, free {fmt_size(du.free)}")
        except OSError:
            pass
        self.sub_title = "  ·  ".join(parts)

    # ------------------------------------------------------------ events

    @on(Tree.NodeExpanded)
    def _on_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._populate(event.node)

    @on(Tree.NodeHighlighted)
    def _on_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if not self.scanning:
            self._update_details(event.node.data if event.node is not None else None)

    # ----------------------------------------------------------- actions

    def action_expand_cursor(self) -> None:
        if self.scanning:
            return
        node = self._tree.cursor_node
        if node is not None and node.data is not None and not node.is_expanded:
            node.expand()
            self._populate(node)

    def action_collapse_cursor(self) -> None:
        if self.scanning:
            return
        node = self._tree.cursor_node
        if node is not None and node.is_expanded:
            node.collapse()

    def action_largest(self) -> None:
        if self.scanning or self.result is None:
            self.notify("Nothing scanned yet", severity="warning")
            return
        self.push_screen(LargestFilesScreen(self.collect_largest_files()))

    def action_toggle_files(self) -> None:
        if self.scanning or self.result is None:
            return
        self._show_files = not self._show_files
        self._rebuild_tree()
        self.notify("Files are now " + ("shown" if self._show_files else "hidden (directories only)"))

    def action_rescan(self) -> None:
        if self.scanning:
            self.notify("Already scanning — press s to stop it first", severity="warning")
            return
        self._start_scan()

    def action_stop_scan(self) -> None:
        if self.scanning:
            self._stop_flag = True
            self.notify("Stopping scan…")

    def action_open_location(self) -> None:
        if self.result is None:
            return
        node = self._tree.cursor_node
        entry = node.data if node is not None else None
        if not isinstance(entry, Entry):
            return
        path = entry.path if entry.is_dir else (os.path.dirname(entry.path) or entry.path)
        try:
            if IS_WINDOWS:
                starter = getattr(os, "startfile", None)
                if starter is not None:
                    starter(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self.notify(f"Could not open: {exc}", severity="error")

    def action_delete_trash(self) -> None:
        self._delete_from_tree(permanent=False)

    def action_delete_perm(self) -> None:
        self._delete_from_tree(permanent=True)

    def _delete_from_tree(self, permanent: bool) -> None:
        if self.scanning or self.result is None:
            return
        node = self._tree.cursor_node
        entry = node.data if node is not None else None
        if not isinstance(entry, Entry):
            return
        self.confirm_and_delete(entry, permanent)

    # -------------------------------------------------------- largest N

    def collect_largest_files(self) -> list[Entry]:
        if self._largest is not None:
            return self._largest
        files: list[Entry] = []
        stack = [self.result.root]
        while stack:
            node = stack.pop()
            if node.children:
                for child in node.children:
                    if child.is_dir:
                        stack.append(child)
                    else:
                        files.append(child)
        files.sort(key=lambda e: (-e.size_disk, e.path.lower()))
        self._largest = files[:LARGEST_N]
        return self._largest

    # ------------------------------------------------------------ reveal

    def reveal_entry(self, entry: Entry) -> None:
        """Expand parents of *entry* in the tree and select it."""
        if self.result is None:
            return
        chain: list[Entry] = []
        node_entry: Entry | None = entry
        while node_entry is not None and node_entry.parent is not None:
            chain.append(node_entry)
            node_entry = node_entry.parent
        chain.reverse()
        tree_node: TreeNode = self._tree.root
        for target in chain:
            tree_node.expand()
            self._populate(tree_node)
            nxt = self._tnodes.get(target.path)
            if nxt is None:
                self.notify("Could not reveal this item in the tree", severity="warning")
                return
            tree_node = nxt
        if entry.is_dir:
            tree_node.expand()
            self._populate(tree_node)
        try:
            self._tree.scroll_to_node(tree_node)
        except Exception:
            pass
        if hasattr(self._tree, "select_node"):
            try:
                self._tree.select_node(tree_node)
            except Exception:
                pass
        self._tree.focus()
        self._update_details(entry)

    # ------------------------------------------------------------ delete

    def confirm_and_delete(
        self,
        entry: Entry,
        permanent: bool,
        on_done: Callable[[bool, Entry], None] | None = None,
    ) -> None:
        if self.result is None or self.scanning:
            self.notify("Scan is not finished yet", severity="warning")
            return
        if entry is self.result.root:
            self.notify("The scan root cannot be deleted from here", severity="warning")
            return
        verb = ("PERMANENTLY DELETE" if permanent
                else ("Move to Recycle Bin" if IS_WINDOWS else "Move to Trash"))
        msg = Text()
        msg.append(verb + "\n\n", "bold red" if permanent else "bold yellow")
        msg.append(entry.path + "\n", "bold")
        if entry.is_dir:
            msg.append(f"{fmt_count(entry.files)} file(s), {fmt_count(entry.dirs)} subfolder(s)\n")
        msg.append(f"size on disk: {fmt_size(entry.size_disk)}\n\n")
        msg.append("y = confirm      n / Esc = cancel", "dim")

        def finished(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(
                    lambda: self._delete_worker(entry, permanent, on_done),
                    thread=True,
                    group="delete",
                )

        self.push_screen(ConfirmScreen(msg), finished)

    def _delete_worker(
        self,
        entry: Entry,
        permanent: bool,
        on_done: Callable[[bool, Entry], None] | None,
    ) -> None:
        ok = False
        err: str | None = None
        try:
            if permanent:
                if entry.is_dir:
                    shutil.rmtree(entry.path, onerror=_rmtree_retry)
                else:
                    _remove_file(entry.path)
            else:
                from send2trash import send2trash
                try:
                    send2trash(entry.path)
                except PermissionError:
                    _make_writable(entry.path)
                    send2trash(entry.path)
            ok = True
        except Exception as exc:
            err = str(exc) or repr(exc)
        self.call_from_thread(self._delete_finished, entry, ok, err, on_done)

    def _delete_finished(
        self,
        entry: Entry,
        ok: bool,
        err: str | None,
        on_done: Callable[[bool, Entry], None] | None,
    ) -> None:
        if not ok:
            self.notify(f"Could not delete:\n{err}", severity="error", timeout=10)
            if on_done is not None:
                on_done(False, entry)
            return
        self._apply_delete(entry)
        self.notify(f"Deleted · freed {fmt_size(entry.size_disk)}")
        if on_done is not None:
            on_done(True, entry)

    def _apply_delete(self, entry: Entry) -> None:
        """Remove *entry* from the model and the tree, fix up all totals."""
        self._largest = None
        self._freed += entry.size_disk
        parent = entry.parent
        if parent is not None:
            if parent.children and entry in parent.children:
                parent.children.remove(entry)
            node = parent
            while node is not None:
                node.size_disk -= entry.size_disk
                node.size_logical -= entry.size_logical
                node.files -= entry.files
                node.dirs -= entry.dirs + (1 if entry.is_dir else 0)
                node = node.parent
        widget_node = self._tnodes.pop(entry.path, None)
        prefix = entry.path + os.sep
        for path in [p for p in self._tnodes if p.startswith(prefix)]:
            del self._tnodes[path]
        self._loaded.discard(entry.path)
        for path in [p for p in self._loaded if p.startswith(prefix)]:
            self._loaded.discard(path)
        if widget_node is not None:
            try:
                widget_node.remove()
            except Exception:
                pass
        self._refresh_labels()
        self._update_header()
        node = self._tree.cursor_node
        self._update_details(node.data if node is not None else None)
