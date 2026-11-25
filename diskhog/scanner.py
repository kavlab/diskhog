"""Scanning engine for diskhog.

Builds an in-memory tree of files and directories with "size on disk"
(allocated bytes) and logical size for every entry.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Windows file attributes (available via os.stat_result.st_file_attributes)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_COMPRESSED = 0x800
_FILE_ATTRIBUTE_SPARSE_FILE = 0x200

if IS_WINDOWS:
    import ctypes.wintypes as _wt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GetCompressedFileSizeW = _kernel32.GetCompressedFileSizeW
    _GetCompressedFileSizeW.restype = _wt.DWORD
    _GetCompressedFileSizeW.argtypes = (_wt.LPCWSTR, ctypes.POINTER(_wt.DWORD))

    _GetDiskFreeSpaceW = _kernel32.GetDiskFreeSpaceW
    _GetDiskFreeSpaceW.restype = _wt.BOOL
    _GetDiskFreeSpaceW.argtypes = (
        _wt.LPCWSTR,
        ctypes.POINTER(_wt.DWORD),
        ctypes.POINTER(_wt.DWORD),
        ctypes.POINTER(_wt.DWORD),
        ctypes.POINTER(_wt.DWORD),
    )

    def _win_allocated_size(path: str) -> int | None:
        """Actual allocated size of a compressed/sparse file; None on error."""
        high = _wt.DWORD()
        low = _GetCompressedFileSizeW(path, ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
            return None
        return (high.value << 32) | low

    def _win_cluster_size(path: str) -> int:
        """Allocation granularity (cluster size) of the volume holding *path*."""
        drive, _ = os.path.splitdrive(path)
        root = (drive + "\\") if drive else path
        spc = _wt.DWORD()
        bps = _wt.DWORD()
        if _GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps), None, None):
            return spc.value * bps.value
        return 4096

else:

    def _win_allocated_size(path: str) -> int | None:  # pragma: no cover
        return None

    def _win_cluster_size(path: str) -> int:  # pragma: no cover
        return 0


def _posix_allocated(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else blocks * 512


@dataclass(slots=True)
class Entry:
    """A file or directory in the scanned tree."""

    name: str
    path: str
    is_dir: bool
    parent: "Entry | None" = None
    size_disk: int = 0  # bytes allocated on disk
    size_logical: int = 0  # logical file size
    files: int = 0  # number of files in subtree (1 for a file)
    dirs: int = 0  # number of subdirectories in subtree
    mtime: float = 0.0
    error: str | None = None
    link: bool = False  # symlink/junction: not traversed, not counted
    scanned: bool = False  # for dirs: scandir() was attempted
    children: "list[Entry] | None" = None
    pad: int = 0  # label column width used by the UI


@dataclass(slots=True)
class ProgressInfo:
    path: str
    files: int
    dirs: int
    bytes_disk: int
    elapsed: float
    errors: int


@dataclass(slots=True)
class ScanResult:
    root: Entry
    cancelled: bool = False
    duration: float = 0.0
    files: int = 0
    dirs: int = 0
    errors: int = 0


def _err_str(exc: OSError) -> str:
    msg = exc.strerror or str(exc)
    code = getattr(exc, "winerror", None) or exc.errno
    return f"{msg} (error {code})" if code else msg


def _file_size_disk(path: str, st: os.stat_result, cluster: int) -> int:
    """Bytes allocated on disk for a regular file."""
    if IS_WINDOWS:
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & (_FILE_ATTRIBUTE_COMPRESSED | _FILE_ATTRIBUTE_SPARSE_FILE):
            allocated = _win_allocated_size(path)
            if allocated is not None:
                return allocated
        if cluster > 1 and st.st_size:
            return (st.st_size + cluster - 1) // cluster * cluster
        return st.st_size
    return _posix_allocated(st)


def scan_root(
    root_path: str,
    stop: "callable | None" = None,
    progress: "callable | None" = None,
) -> ScanResult:
    """Scan *root_path* recursively and return the sized tree.

    stop: optional () -> bool, polled between directories (cancellation).
    progress: optional callback taking ProgressInfo, called ~7x/second.
    Symlinks and junctions are recorded as zero-size leaves (no traversal,
    no double counting).
    """
    root_path = os.fspath(root_path)
    cluster = _win_cluster_size(root_path) if IS_WINDOWS else 0
    st = os.stat(root_path)
    name = Path(root_path).name or root_path
    root = Entry(name=name, path=root_path, is_dir=True, mtime=st.st_mtime, scanned=True)
    root.size_disk = cluster if IS_WINDOWS else _posix_allocated(st)

    files = 0
    dirs = 0
    errors = 0
    bytes_disk = 0
    cancelled = False
    started = time.monotonic()
    last_report = 0.0

    stack: list[Entry] = [root]
    while stack:
        if stop is not None and stop():
            cancelled = True
            break
        node = stack.pop()
        node.children = []
        node.scanned = True
        try:
            dir_iter = os.scandir(node.path)
        except OSError as exc:
            node.error = _err_str(exc)
            errors += 1
            continue
        try:
            with dir_iter:
                for de in dir_iter:
                    try:
                        cst = de.stat(follow_symlinks=False)
                        is_link = de.is_symlink()
                        if not is_link and IS_WINDOWS:
                            attrs = getattr(cst, "st_file_attributes", 0)
                            is_link = bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
                        is_dir = de.is_dir(follow_symlinks=False)
                    except OSError:
                        node.children.append(
                            Entry(name=de.name, path=de.path, is_dir=False, parent=node,
                                  error="could not stat")
                        )
                        errors += 1
                        continue
                    if is_link:
                        child = Entry(name=de.name, path=de.path, is_dir=False, parent=node,
                                      link=True, mtime=cst.st_mtime)
                        files += 1
                    elif is_dir:
                        child = Entry(
                            name=de.name, path=de.path, is_dir=True, parent=node,
                            mtime=cst.st_mtime,
                            size_disk=cluster if IS_WINDOWS else _posix_allocated(cst),
                        )
                        dirs += 1
                        stack.append(child)
                    else:
                        size_disk = _file_size_disk(de.path, cst, cluster)
                        child = Entry(
                            name=de.name, path=de.path, is_dir=False, parent=node,
                            size_disk=size_disk, size_logical=cst.st_size,
                            files=1, mtime=cst.st_mtime,
                        )
                        files += 1
                        bytes_disk += size_disk
                    node.children.append(child)
        except OSError as exc:
            node.error = _err_str(exc)
            errors += 1

        now = time.monotonic()
        if progress is not None and now - last_report >= 0.15:
            last_report = now
            progress(ProgressInfo(node.path, files, dirs, bytes_disk, now - started, errors))

    # Aggregate child totals into parents (descendants are finalized first:
    # the reversed pop-order below guarantees every child precedes its parent).
    order: list[Entry] = []
    collect = [root]
    while collect:
        n = collect.pop()
        order.append(n)
        if n.children:
            collect.extend(n.children)
    for n in reversed(order):
        if n.children:
            for c in n.children:
                n.size_disk += c.size_disk
                n.size_logical += c.size_logical
                n.files += c.files
                n.dirs += c.dirs + (1 if c.is_dir else 0)

    duration = time.monotonic() - started
    if progress is not None:
        progress(ProgressInfo(root_path, files, dirs, root.size_disk, duration, errors))
    return ScanResult(
        root=root, cancelled=cancelled, duration=duration,
        files=files, dirs=dirs, errors=errors,
    )


def fmt_size(n: int) -> str:
    """Human-readable byte size, binary units (KiB, MiB, ...)."""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if x < 1024 or unit == "PiB":
            if unit == "B" or x >= 100:
                return f"{x:,.0f} {unit}"
            return f"{x:,.1f} {unit}"
        x /= 1024
    return f"{x:,.0f} PiB"


def fmt_count(n: int) -> str:
    return f"{n:,}"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_mtime(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
