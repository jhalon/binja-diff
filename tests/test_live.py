"""Smoke-test the backend against the real Binary Ninja API and real binaries.

Unlike the other test modules, this one needs a working Binary Ninja
installation and a license that permits headless use. It is skipped
automatically when ``binaryninja`` cannot be imported.

    .venv-qbindiff-312/bin/python binja_diff/tests/test_live.py [primary] [secondary]

Defaults to diffing two system binaries against each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: Added by the Binary Ninja installer for GUI use; not always on sys.path
#: for an arbitrary interpreter.
_BN_PYTHON = Path.home() / "Documents" / "binaryninja" / "python"
if _BN_PYTHON.is_dir() and str(_BN_PYTHON) not in sys.path:
    sys.path.append(str(_BN_PYTHON))

try:
    import binaryninja
except Exception as exc:  # pragma: no cover - depends on the host
    print(f"SKIP: Binary Ninja is not importable here ({exc.__class__.__name__}: {exc})")
    raise SystemExit(0) from None


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def pick_binaries() -> tuple[str, str]:
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]
    candidates = [
        p
        for p in ("/bin/true", "/bin/false", "/bin/echo", "/bin/cat", "/bin/ls")
        if Path(p).is_file()
    ]
    if len(candidates) < 2:
        print("SKIP: could not find two system binaries to diff")
        raise SystemExit(0)
    return candidates[0], candidates[1]


def test_backend_against_real_view(bv):
    print(f"backend over {bv.file.filename}")
    from qbindiff.loader import Program

    from binja_diff.core.backend import ProgramBackendBinja

    program = Program.from_backend(ProgramBackendBinja(bv))

    check("function count matches Binary Ninja", len(program) == len(bv.functions))
    check("program is non-empty", len(program) > 0, f"got {len(program)}")

    addrs = {addr for addr, _f in program.items()}
    bn_addrs = {f.start for f in bv.functions}
    check("addresses match Binary Ninja", addrs == bn_addrs, f"diff {addrs ^ bn_addrs}")

    total_blocks = 0
    total_instrs = 0
    checked = 0
    for addr, func in program.items():
        bn_func = bv.get_function_at(addr)
        if bn_func is None:
            continue
        with func:
            # Like Program, Function.__iter__ yields values rather than keys,
            # so .values() from the Mapping mixin raises.
            blocks = list(func)
            total_blocks += len(blocks)
            if not func.is_import():
                check_once = len(blocks) == len(bn_func.basic_blocks)
                if not check_once and checked < 3:
                    check(
                        f"block count for {func.name}",
                        False,
                        f"{len(blocks)} vs {len(bn_func.basic_blocks)}",
                    )
                    checked += 1
            for block in blocks:
                instrs = list(block)
                total_instrs += len(instrs)
                for instr in instrs:
                    if instr.mnemonic == "":
                        check(f"empty mnemonic at {instr.addr:#x}", False)
                        return

    check("basic blocks recovered", total_blocks > 0, f"got {total_blocks}")
    check("instructions recovered", total_instrs > 0, f"got {total_instrs}")
    check("callgraph populated", len(program.callgraph) == len(program))
    print(f"       {len(program)} functions, {total_blocks} blocks, {total_instrs} instructions")
    return program


def test_real_diff(primary_bv, secondary_bv):
    print("end-to-end diff on real binaries")
    from binja_diff.core.engine import run_diff

    steps: list[tuple[str, float]] = []
    result = run_diff(
        primary_bv,
        secondary_bv,
        progress=lambda label, fraction: steps.append((label, fraction)),
    )

    check("diff produced a result", result is not None)
    if result is None:
        return
    check("progress was reported", len(steps) > 0, f"got {len(steps)}")
    phases = {label for label, _fraction in steps}
    check("both phases reported", len(phases) >= 2, f"got {phases}")
    check("similarity in range", 0.0 <= result.similarity <= 1.0, f"got {result.similarity}")
    check("primary index built", len(result.by_primary) == result.nb_match)
    check("secondary index built", len(result.by_secondary) == result.nb_match)
    print(
        f"       {result.nb_match} matches, "
        f"{result.nb_unmatched_primary} primary-only, "
        f"{result.nb_unmatched_secondary} secondary-only, "
        f"similarity {result.similarity:.3f}"
    )
    return result


def test_alignment_on_real_functions(result):
    print("alignment on real matched functions")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches to align)")
        return

    aligned_any = False
    for match in list(result.matches)[:5]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        blocks = align.align_blocks(primary, secondary, "Disassembly")
        check(
            f"blocks classified for {primary.name}",
            len(blocks.left_status) == len(primary.basic_blocks),
            f"{len(blocks.left_status)} vs {len(primary.basic_blocks)}",
        )

        for level in align.IL_LEVELS:
            rows = align.align_function_text(
                result.primary_bv, primary, result.secondary_bv, secondary, level
            )
            check(f"{level} produced rows for {primary.name}", len(rows) > 0, "empty")
            check(
                f"{level} rows are well formed",
                all(r.left is not None or r.right is not None for r in rows),
            )
        aligned_any = True
        break

    check("aligned at least one pair", aligned_any)


def test_il_renders_on_the_first_try(result):
    """The IL panes must be readable without being visited twice.

    Rendering IL that has not been generated yet yields a "Loading..."
    placeholder, filled in asynchronously — which a pane drawn once never
    sees. Only real Binary Ninja generates IL lazily, so only this test can
    catch it coming back.
    """

    print("IL renders on the first pass")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches)")
        return

    pair = None
    for match in list(result.matches)[:20]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is not None and secondary is not None and primary.basic_blocks:
            pair = (primary, secondary)
            break
    if pair is None:
        print("  (no resolvable pair)")
        return

    primary, secondary = pair
    for level in ("LLIL", "MLIL", "HLIL"):
        # Deliberately without touching the graph path first: that is what used
        # to generate the IL as a side effect and mask this.
        lines = align.function_lines(result.primary_bv, primary, level)
        text = [str(line) for line in lines]
        check(f"{level} produced lines", bool(text), "empty")
        check(
            f"{level} is not still loading",
            not any("Loading" in line for line in text),
            f"got {text[:3]}",
        )


def test_status_agrees_with_the_panes(result):
    """The table's status must describe the rows the panes actually show.

    It used to be read off QBinDiff's similarity, which says nothing about the
    text: a pair scored 1.0 came up "identical" while the pane marked every
    line '~'. Only real disassembly can catch that disagreeing again.
    """

    print("match table status matches the rendered diff")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches)")
        return

    checked = 0
    for match in list(result.matches)[:40]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        status, rows = align.classify_pair(primary, secondary)
        markers = {row.status for row in rows if row.status.is_difference}
        if status is align.FunctionStatus.IDENTICAL:
            check(f"{primary.name}: identical means no markers", not markers, f"got {markers}")
        elif status is align.FunctionStatus.MINOR:
            check(
                f"{primary.name}: offsets only means only '~'",
                markers == {align.LineStatus.MINOR},
                f"got {markers}",
            )
        else:
            check(f"{primary.name}: changed means real differences", bool(markers))
        checked += 1
        if checked >= 5:
            break

    check("classified at least one pair", checked > 0)


def test_changes_are_visible(result):
    """Non-identical matched functions must produce visible differences.

    This is the regression guard for changes being silently classified as
    equal: it holds the whole pipeline to the promise that a function the
    matcher scored below 1.0 actually shows something in the text panes.
    """

    print("changes surface in the text views")
    from binja_diff.core import align

    if result is None:
        print("  (no result)")
        return

    imperfect = [m for m in result.matches if m.similarity < 0.99]
    if not imperfect:
        print("  (no imperfect matches in this pair; skipping)")
        return

    imperfect.sort(key=lambda m: m.similarity)
    inspected = 0
    silent = []
    for match in imperfect[:8]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        rows = align.align_function_text(
            result.primary_bv, primary, result.secondary_bv, secondary, "Disassembly"
        )
        differing = [r for r in rows if r.status.is_difference]
        inspected += 1
        if not differing:
            silent.append((primary.name, match.similarity))
        else:
            kinds = sorted({r.status.value for r in differing})
            print(
                f"       {primary.name} (sim {match.similarity:.3f}): "
                f"{len(differing)}/{len(rows)} rows differ {kinds}"
            )

    check("inspected some imperfect matches", inspected > 0)
    check(
        "no imperfect match renders as fully identical",
        not silent,
        f"silent: {silent}",
    )


def test_graph_line_highlighting(result):
    """Per-instruction highlights in the flow graph must actually stick.

    The graph pane zips per-line statuses onto a node's own ``lines``, so two
    things have to hold and neither is obvious: a node's line count must match
    its basic block's rendered text, and a highlight written through the
    ``lines`` setter must survive the round trip into the core. If either fails
    the pane silently falls back to no highlighting at all.
    """

    print("flow graph per-instruction highlighting")
    from binaryninja import HighlightColor
    from binaryninja.enums import HighlightColorStyle

    from binja_diff.core import align

    if result is None:
        print("  (no result)")
        return

    # Least similar first: in a near-identical binary pair only a handful of
    # functions have a changed block at all, and mapping order will not find them.
    candidates = sorted(result.matches, key=lambda m: m.similarity)

    pair = None
    for match in candidates[:40]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue
        alignment = align.align_blocks(primary, secondary, "Disassembly")
        changed = [
            p
            for p in alignment.pairs
            if p.status is align.BlockStatus.CHANGED and p.left_addr is not None
        ]
        if changed:
            pair = (primary, secondary, alignment, changed[0])
            break

    if pair is None:
        print("  (no changed block pair in this binary pair; skipping)")
        return

    primary, secondary, alignment, block_pair = pair

    left_graph = primary.create_graph()
    left_graph.layout_and_wait()
    right_graph = secondary.create_graph()
    right_graph.layout_and_wait()

    def node_for(graph, addr):
        return next(
            (n for n in graph.nodes if n.basic_block is not None and n.basic_block.start == addr),
            None,
        )

    left_node = node_for(left_graph, block_pair.left_addr)
    right_node = node_for(right_graph, block_pair.right_addr)
    check("found both graph nodes", left_node is not None and right_node is not None)
    if left_node is None or right_node is None:
        return

    # A node prepends a symbol label its basic block's text does not have, so the
    # statuses have to come from the node lines themselves.
    lines = left_node.lines
    right_lines = right_node.lines
    left_status, right_status = align.align_line_statuses(lines, right_lines)

    check(
        "statuses match the left node's line count",
        len(left_status) == len(lines),
        f"{len(left_status)} vs {len(lines)}",
    )
    check(
        "statuses match the right node's line count",
        len(right_status) == len(right_lines),
        f"{len(right_status)} vs {len(right_lines)}",
    )
    check(
        "a changed block has something to highlight",
        any(s.is_difference for s in left_status) or any(s.is_difference for s in right_status),
        f"left {[s.value for s in left_status]}",
    )
    check(
        "unchanged instructions stay unmarked",
        any(not s.is_difference for s in left_status),
        "every line flagged, which would be the old whole-block behavior",
    )

    marked = [i for i, s in enumerate(left_status) if s.is_difference]
    for index in marked:
        lines[index].highlight = HighlightColor(red=210, green=170, blue=40)
    left_node.lines = lines
    node = left_node

    read_back = node.lines
    check("line count survives the write", len(read_back) == len(lines))
    persisted = [
        i
        for i, line in enumerate(read_back)
        if line.highlight is not None
        and line.highlight.style == HighlightColorStyle.CustomHighlightColor
    ]
    check(
        "highlights persisted on exactly the changed lines",
        persisted == marked,
        f"marked {marked}, persisted {persisted}",
    )
    print(f"       {primary.name}: {len(marked)}/{len(lines)} lines highlighted in one block")


def test_saved_diff_round_trip(result, primary_path: str):
    """A saved diff must survive the real metadata store and a real .bndb.

    The stubbed tests prove the JSON round trips; only here can it be shown
    that Binary Ninja accepts a payload that size as metadata and hands it back
    intact after the database has been written and reopened.
    """

    print("saving and restoring a diff")
    import tempfile

    from binja_diff.core import persist

    if result is None:
        print("  (no result)")
        return

    saved = persist.SavedDiff.from_result(result)
    persist.store_in_database(result.primary_bv, saved)
    reloaded = persist.load_from_database(result.primary_bv)
    check("stored diff reads back", reloaded is not None)
    if reloaded is None:
        return
    check(
        "matches survive the metadata store",
        [(m.primary.addr, m.secondary.addr) for m in reloaded.matches]
        == [(m.primary.addr, m.secondary.addr) for m in result.matches],
    )
    check("similarity survives", abs(reloaded.similarity - result.similarity) < 1e-6)
    check(
        "no drift against the binary it came from",
        reloaded.primary.differences(result.primary_bv) == [],
    )

    restored = reloaded.to_result(result.primary_bv, result.secondary_bv)
    check("restored result is usable", restored.nb_match == result.nb_match)
    check(
        "restored functions resolve in the view",
        all(
            result.primary_bv.get_function_at(m.primary.addr) is not None
            for m in list(restored.matches)[:20]
        ),
    )

    # A separate view of the same file: creating a database rebinds the view to
    # it, and this one's directory is about to be deleted.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "with_diff.bndb")
        fresh = binaryninja.load(primary_path)
        try:
            persist.store_in_database(fresh, saved)
            created = fresh.create_database(db_path)
        finally:
            fresh.file.close()
        check("database with a saved diff created", created)
        if not created:
            return

        reopened = binaryninja.load(db_path)
        try:
            persisted = persist.load_from_database(reopened)
            check("diff survives closing and reopening the database", persisted is not None)
            if persisted is not None:
                check("match count unchanged", len(persisted.matches) == result.nb_match)
        finally:
            reopened.file.close()

    persist.remove_from_database(result.primary_bv)


def test_kernelcache_scoping():
    """Diffing one kext out of a kernelcache, if one is to hand.

    A kernelcache is the case scoping exists for: matching is quadratic, so the
    whole container never finishes while one kext takes under a minute. Only
    Binary Ninja's own loader can enumerate and map a kext, so this cannot be
    checked against the stub.
    """

    print("kernelcache scoping")
    from binja_diff.core import scope

    caches = sorted(Path.home().glob("dev/kcache/kernelcache*"))
    if len(caches) < 2:
        print("  (no kernelcache pair to hand; skipping)")
        return

    bv = binaryninja.load(str(caches[0]), update_analysis=False)
    try:
        check("recognised as a kernelcache", bv.view_type == scope.KERNELCACHE_VIEW, bv.view_type)
        regions = scope.available_regions(bv)
        check("kexts enumerated", len(regions) > 10, f"got {len(regions)}")
        check("none loaded yet", not any(r.loaded for r in regions))
        check("and no functions yet", len(bv.functions) == 0, f"got {len(bv.functions)}")

        wanted = next((r for r in regions if "AppleSEPManager" in r.name), regions[0])
        check("found by name", scope.find_region(bv, wanted.name) is not None)
        check("loads on demand", scope.ensure_loaded(bv, wanted))
        check("which is what creates functions", len(bv.functions) > 0)

        scoped = scope.functions_in(bv, scope.find_region(bv, wanted.name))
        check(
            "and they all belong to that kext",
            0 < len(scoped) <= len(bv.functions),
            f"{len(scoped)} of {len(bv.functions)}",
        )
        print(f"       {wanted.name}: {len(scoped)} functions of {len(regions)} kexts")
    finally:
        bv.file.close()


def test_database_round_trip(source_path: str):
    """A .bndb must load with its saved analysis intact and diff normally."""

    print("database (.bndb) support")
    import tempfile

    from binja_diff.core.engine import load_secondary, run_diff

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "saved.bndb")

        bv = binaryninja.load(source_path)
        target = next((f for f in bv.functions if f.basic_blocks), None)
        if target is None:
            print("  (no function to annotate; skipping)")
            bv.file.close()
            return
        original_addr = target.start
        target.name = "renamed_by_test"
        target.set_comment_at(original_addr, "comment from the database")
        bv.update_analysis_and_wait()
        created = bv.create_database(db_path)
        bv.file.close()

        check("database created", created)
        if not created:
            return

        loaded = load_secondary(db_path)
        check("database loads", loaded is not None)
        if loaded is None:
            return
        try:
            names = {f.name for f in loaded.functions}
            check("renamed function preserved", "renamed_by_test" in names)
            restored = loaded.get_function_at(original_addr)
            check(
                "comment preserved",
                restored is not None
                and restored.get_comment_at(original_addr) == "comment from the database",
            )

            fresh = binaryninja.load(source_path)
            try:
                result = run_diff(fresh, loaded)
                check("diff against a database succeeds", result is not None)
                if result is not None:
                    check(
                        "matches the identical content",
                        result.nb_match > 0,
                        f"got {result.nb_match}",
                    )
                    print(f"       {result.nb_match} matches, similarity {result.similarity:.3f}")
            finally:
                fresh.file.close()
        finally:
            loaded.file.close()

    print("  cancelling a load")
    # Aborting via the progress callback makes binaryninja.load() report a
    # generic failure; that must surface as a clean cancellation, never as an
    # error dialog.
    try:
        cancelled_view = load_secondary(source_path, cancelled=lambda: True)
        check("cancelled load raises nothing", True)
        if cancelled_view is not None:
            cancelled_view.file.close()
    except Exception as exc:
        check("cancelled load raises nothing", False, repr(exc))


def main() -> int:
    primary_path, secondary_path = pick_binaries()
    print(f"primary={primary_path} secondary={secondary_path}\n")

    primary_bv = binaryninja.load(primary_path)
    secondary_bv = binaryninja.load(secondary_path)
    try:
        test_backend_against_real_view(primary_bv)
        result = test_real_diff(primary_bv, secondary_bv)
        test_alignment_on_real_functions(result)
        test_il_renders_on_the_first_try(result)
        test_status_agrees_with_the_panes(result)
        test_changes_are_visible(result)
        test_graph_line_highlighting(result)
        test_saved_diff_round_trip(result, primary_path)
        test_kernelcache_scoping()
        test_database_round_trip(primary_path)
    finally:
        primary_bv.file.close()
        secondary_bv.file.close()

    print()
    if check.failures:
        print(f"{check.failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
