"""Cover saving a diff and restoring it without re-running QBinDiff.

The point of the format is that a result survives a Binary Ninja restart, so
what matters here is that a round trip through JSON is lossless, that a payload
which is not ours is rejected rather than half-read, and that a binary which
changed underneath the saved diff is noticed — a restore that silently pairs
the wrong functions is worse than no restore at all.

    .venv-qbindiff-312/bin/python binja_diff/tests/test_persist.py
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

from binja_diff.core import persist  # noqa: E402
from binja_diff.core.engine import DiffOptions, DiffResult, FunctionRef, MatchRecord  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def make_view(name: str, size: int = 64, functions: int = 3):
    stubs = _bootstrap.stubs()
    bv = stubs.BinaryView(name, b"\x00" * size)
    bv.functions = [object()] * functions
    return bv


def make_result():
    primary, secondary = make_view("/tmp/a.bin"), make_view("/tmp/b.bin", size=72)
    return DiffResult(
        primary_bv=primary,
        secondary_bv=secondary,
        similarity=0.875,
        matches=[
            MatchRecord(FunctionRef(0x1000, "main"), FunctionRef(0x2000, "main"), 1.0, 0.9),
            MatchRecord(FunctionRef(0x1100, "sub_1100"), FunctionRef(0x2100, "check"), 0.5, 0.25),
        ],
        primary_unmatched=[FunctionRef(0x1200, "gone")],
        secondary_unmatched=[FunctionRef(0x2200, "added"), FunctionRef(0x2300, "also_added")],
    )


def test_round_trip():
    print("JSON round trip")
    result = make_result()
    saved = persist.SavedDiff.from_result(result, DiffOptions())
    back = persist.SavedDiff.from_json(saved.to_json())

    check("similarity preserved", back.similarity == result.similarity)
    check("matches preserved", back.matches == result.matches, f"got {back.matches}")
    check("primary-only preserved", back.primary_unmatched == result.primary_unmatched)
    check("secondary-only preserved", back.secondary_unmatched == result.secondary_unmatched)
    check("secondary path preserved", back.secondary.filename == "/tmp/b.bin")
    check("options recorded", back.options.get("distance") == "haussmann", f"{back.options}")
    check("creation time recorded", bool(back.created))

    restored = back.to_result(result.primary_bv, result.secondary_bv)
    check("indexes rebuilt", 0x1000 in restored.by_primary and 0x2100 in restored.by_secondary)
    check(
        "counts match the original",
        (restored.nb_match, restored.nb_unmatched_primary, restored.nb_unmatched_secondary)
        == (2, 1, 2),
    )


def test_rejects_foreign_payloads():
    print("foreign payloads are rejected")
    for label, text in (
        ("empty string", ""),
        ("not json", "not json at all"),
        ("json but not ours", '{"hello": "world"}'),
        ("a bare list", "[1, 2, 3]"),
    ):
        try:
            persist.SavedDiff.from_json(text)
        except ValueError:
            check(label, True)
        except Exception as exc:
            check(label, False, f"raised {exc!r} instead of ValueError")
        else:
            check(label, False, "no exception")


def test_rejects_newer_version():
    print("a newer format version is refused, not misread")
    saved = persist.SavedDiff.from_result(make_result())
    payload = saved.to_dict()
    payload["version"] = persist.VERSION + 1
    try:
        persist.SavedDiff.from_dict(payload)
    except ValueError as exc:
        check("raises ValueError", True)
        check("names the version", str(persist.VERSION + 1) in str(exc), str(exc))
    else:
        check("raises ValueError", False, "no exception")


def test_truncated_payload():
    print("a malformed match row is a ValueError, not a crash")
    payload = persist.SavedDiff.from_result(make_result()).to_dict()
    payload["matches"][0] = [0x1000, "main"]
    try:
        persist.SavedDiff.from_dict(payload)
    except ValueError:
        check("raises ValueError", True)
    except Exception as exc:
        check("raises ValueError", False, f"raised {exc!r}")
    else:
        check("raises ValueError", False, "no exception")


def test_database_sink():
    print("database sink")
    result = make_result()
    bv = result.primary_bv

    check("nothing stored yet", persist.load_from_database(bv) is None)

    persist.store_in_database(bv, persist.SavedDiff.from_result(result))
    saved = persist.load_from_database(bv)
    check("round trips through metadata", saved is not None and len(saved.matches) == 2)

    persist.remove_from_database(bv)
    check("removal works", persist.load_from_database(bv) is None)

    bv.store_metadata(persist.METADATA_KEY, "{}")
    check("junk metadata is ignored, not raised", persist.load_from_database(bv) is None)


def test_file_sink():
    print("file sink")
    result = make_result()
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / persist.default_filename(result))
        check("suggested name mentions both sides", "a.bin-vs-b.bin" in path, path)

        persist.write_file(persist.SavedDiff.from_result(result), path)
        saved = persist.read_file(path)
        check("matches survive the file", len(saved.matches) == 2)
        check("stored as plain JSON", isinstance(json.loads(Path(path).read_text()), dict))

        try:
            persist.read_file(str(Path(tmp) / "missing.json"))
        except RuntimeError:
            check("missing file raises RuntimeError", True)
        except Exception as exc:
            check("missing file raises RuntimeError", False, repr(exc))
        else:
            check("missing file raises RuntimeError", False, "no exception")


def test_drift_detection():
    print("drift detection")
    result = make_result()
    saved = persist.SavedDiff.from_result(result)

    check("same view has no drift", saved.primary.differences(result.primary_bv) == [])

    rebuilt = make_view("/tmp/a.bin", size=64, functions=5)
    check("new functions noticed", len(saved.primary.differences(rebuilt)) == 1)

    resized = make_view("/tmp/a.bin", size=128, functions=3)
    check("size change noticed", len(saved.primary.differences(resized)) == 1)

    renamed = make_view("/tmp/elsewhere/other.bin", size=64, functions=3)
    check("different file noticed", len(saved.primary.differences(renamed)) == 1)

    # A file that merely moved is the same file; only the directory changed.
    moved = make_view("/elsewhere/a.bin", size=64, functions=3)
    check("a moved file is not drift", saved.primary.differences(moved) == [])


def test_summary():
    print("summary line")
    saved = persist.SavedDiff.from_result(make_result())
    summary = saved.summary
    check("names the secondary", "b.bin" in summary, summary)
    check("counts the matches", "2 matches" in summary, summary)


def main() -> int:
    for test in (
        test_round_trip,
        test_rejects_foreign_payloads,
        test_rejects_newer_version,
        test_truncated_payload,
        test_database_sink,
        test_file_sink,
        test_drift_detection,
        test_summary,
    ):
        test()
    print()
    if check.failures:
        print(f"{check.failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
