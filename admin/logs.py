"""관리 페이지용 IPC 로그 조회. 파일 끝부분만 읽는다."""

from __future__ import annotations

import re
from pathlib import Path

_MAX_BYTES = 256_000
_LINE = 400
_NAME = re.compile(r"^[\w.\-]+$", re.UNICODE)


def list_devices(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    names = []
    for path in root.iterdir():
        if path.is_dir() and not path.name.startswith(".") and _NAME.fullmatch(path.name):
            names.append(path.name)
    return sorted(names)


def tail_device(root: Path, device: str, lines: int) -> dict:
    if not device or _NAME.fullmatch(device) is None or device in (".", ".."):
        raise ValueError("device_key 가 올바르지 않습니다")
    lines = max(1, min(200, lines))
    root_resolved = root.resolve()
    directory = (root_resolved / device).resolve()
    if directory.parent != root_resolved:
        raise ValueError("device_key 가 올바르지 않습니다")
    empty = {"device": device, "file": None, "lines": [], "cut": False}
    if not directory.is_dir():
        return empty
    files = []
    for path in directory.glob("*.log"):
        resolved = path.resolve()
        if resolved.is_file() and resolved.parent == directory:
            files.append(resolved)
    files.sort()
    if not files:
        return empty
    path = files[-1]
    rows, cut = _tail(path, lines)
    return {"device": device, "file": path.name, "lines": rows, "cut": cut}


def _tail(path: Path, lines: int) -> tuple[list[str], bool]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > _MAX_BYTES:
            handle.seek(size - _MAX_BYTES)
        data = handle.read()
    rows = data.decode("utf-8", errors="replace").splitlines()
    cut = size > _MAX_BYTES
    if cut and rows:
        rows = rows[1:]
    out: list[str] = []
    for row in rows[-lines:]:
        if len(row) > _LINE:
            out.append(row[:_LINE] + "…")
            cut = True
        else:
            out.append(row)
    return out, cut
