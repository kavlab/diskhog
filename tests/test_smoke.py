"""Smoke tests for diskhog: scanner engine + TUI via Textual's pilot harness."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from textual.widgets import Tree

from diskhog.app import ConfirmScreen, DiskHogApp, LargestFilesScreen
from diskhog.scanner import fmt_size, scan_root


def make_fixtures(base: Path) -> None:
    (base / "big").mkdir()
    (base / "big" / "data.bin").write_bytes(b"x" * 100_000)
    (base / "big" / "sub").mkdir()
    (base / "big" / "sub" / "small.txt").write_bytes(b"y" * 1_000)
    (base / "empty").mkdir()
    (base / "many").mkdir()
    for i in range(5):
        (base / "many" / f"f{i}.txt").write_bytes(b"z" * (100 * (i + 1)))


def test_fmt() -> None:
    assert fmt_size(0) == "0 B"
    assert fmt_size(1024) == "1.0 KiB"
    assert fmt_size(1024 * 1024) == "1.0 MiB"
    assert fmt_size(123_456_789) == "118 MiB"
    assert fmt_size(3 * 1024**3) == "3.0 GiB"


def test_scanner(base: Path) -> None:
    result = scan_root(str(base))
    root = result.root
    assert root.is_dir and root.scanned
    assert root.files == 7, root.files
    assert root.dirs == 4, root.dirs
    assert root.size_logical == 102_500, root.size_logical
    assert root.size_disk >= 102_500, root.size_disk
    assert result.files == 7 and result.dirs == 4
    assert result.cancelled is False

    big = next(c for c in root.children if c.name == "big")
    assert big.is_dir and big.parent is root
    assert big.files == 2 and big.dirs == 1
    assert big.size_logical == 101_000
    assert big.size_disk >= 101_000

    data_bin = next(c for c in big.children if c.name == "data.bin")
    assert not data_bin.is_dir and data_bin.files == 1
    assert data_bin.size_logical == 100_000
    assert data_bin.size_disk >= 100_000

    empty = next(c for c in root.children if c.name == "empty")
    assert empty.children == []

    # aggregation is consistent: parent total == own allocation + sum of children
    total_disk = sum(c.size_disk for c in root.children)
    own = root.size_disk - total_disk
    assert 0 <= own <= 16384, (own, total_disk, root.size_disk)  # dir entry itself
    total_logical = sum(c.size_logical for c in root.children)
    assert total_logical == root.size_logical


async def run_app(base: Path) -> None:
    app = DiskHogApp(str(base))
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        tree = app.query_one("#tree", Tree)
        assert app.result is not None
        root_entry = app.result.root
        assert tree.root.data is root_entry
        assert str(tree.root.label).startswith("diskhog-test")

        names = [n.data.name for n in tree.root.children if n.data is not None]
        assert names == ["big", "many", "empty"], names  # sorted by size

        # Expand "big" via keyboard: down moves to first child, right expands.
        await pilot.press("down")
        await pilot.press("right")
        await pilot.pause()
        big_node = app._tnodes[os.path.join(str(base), "big")]
        child_names = [n.data.name for n in big_node.children if n.data is not None]
        assert child_names == ["data.bin", "sub"], child_names

        # Largest files screen: 7 files, biggest first.
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, LargestFilesScreen)
        entries = app.screen._entries
        assert len(entries) == 7
        assert entries[0].name == "data.bin"

        # Reveal in the tree: enter selects the first row, pops the screen.
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, LargestFilesScreen)
        assert os.path.join(str(base), "big", "data.bin") in app._tnodes
        assert app._tnodes[os.path.join(str(base), "big", "data.bin")].parent is big_node

        # Go back to "big" (cursor is on data.bin now, one line below "big").
        await pilot.press("up")
        await pilot.pause()

        # Permanent delete confirmation can be cancelled.
        await pilot.press("D")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert (base / "big").exists()

        # Delete "big" to the recycle bin (cursor is still on "big").
        size_before = root_entry.size_disk
        big_entry = next(c for c in root_entry.children if c.name == "big")
        big_size = big_entry.size_disk
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not (base / "big").exists()
        assert root_entry.files == 5, root_entry.files
        assert root_entry.dirs == 2, root_entry.dirs
        assert root_entry.size_disk == size_before - big_size, (
            root_entry.size_disk, size_before, big_size)
        assert app._freed == big_size
        names = [n.data.name for n in tree.root.children if n.data is not None]
        assert names == ["many", "empty"], names

        # Rescan reflects the deletion.
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.result.files == 5
        assert app.result.dirs == 2
        assert app.result.root.size_logical == 1_500


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="diskhog-test-"))
    make_fixtures(base)
    test_fmt()
    test_scanner(base)
    asyncio.run(run_app(base))
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
