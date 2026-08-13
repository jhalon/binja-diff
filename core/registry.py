"""Module-level registry of active DiffResults.

Both producers (binja-diff's DiffView) and consumers (binary_ninja_mcp's HTTP
server) run in the same Binary Ninja process.  The DiffView registers a result
from the main thread when a diff completes; the HTTP server reads it from a
daemon thread.  A threading.Lock serializes access to the dict.

Keyed by the primary BinaryView's file path so the MCP server can look up the
diff for the binary it is currently viewing.

Published as ``sys.modules["binja_diff_registry"]`` so consumers can import it
by that well-known name regardless of which directory the plugin was installed
under.  Same pattern as sep-binja's ``sep_binja_api``.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import DiffResult

@dataclass(frozen=True)
class DiffEntry:
    primary_path: str
    secondary_path: str
    result: DiffResult


_lock = threading.Lock()
_diffs: dict[str, DiffEntry] = {}


def register(result: DiffResult) -> None:
    primary_path = result.primary_bv.file.filename
    secondary_path = result.secondary_bv.file.filename
    entry = DiffEntry(primary_path, secondary_path, result)
    with _lock:
        _diffs[primary_path] = entry


def unregister(primary_path: str) -> None:
    with _lock:
        _diffs.pop(primary_path, None)


def get(primary_path: str) -> DiffEntry | None:
    with _lock:
        return _diffs.get(primary_path)


def get_any() -> DiffEntry | None:
    with _lock:
        if len(_diffs) == 1:
            return next(iter(_diffs.values()))
        return None


def list_all() -> list[DiffEntry]:
    with _lock:
        return list(_diffs.values())


def _resolve_align_module():
    from . import align
    return align


sys.modules.setdefault("binja_diff_registry", sys.modules[__name__])
