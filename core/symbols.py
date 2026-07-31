# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Porting function names across a diff.

The point of diffing two builds is usually that one of them is understood. This
carries that understanding over: for every matched pair, the name from the
symbolicated side is applied to the other, so a stripped image arrives with the
names you already worked out on the old one.

Names are read from the two live BinaryViews rather than from the diff result,
because renaming a function and *then* porting is the normal order of events —
the result only supplies which addresses pair up.

Everything here is a rename of somebody's database, so two properties matter
more than features: the whole batch is one undo step, and nothing is written
without the caller having seen a count first.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum

from binaryninja import BackgroundTaskThread, BinaryView, log_error, log_info, log_warn

from .engine import DiffResult, FunctionRef, is_generated_name

#: Rename this many functions between progress reports. Each rename defines a
#: user symbol, which is fast, but a firmware pair has thousands of them.
_PROGRESS_EVERY = 64


class PortDirection(str, Enum):
    """Which side of the diff receives the names."""

    TO_PRIMARY = "secondary to primary"
    TO_SECONDARY = "primary to secondary"


class SkipReason(str, Enum):
    """Why a matched pair contributed no rename. Reported, never silent."""

    BELOW_THRESHOLD = "match too weak"
    NO_SYMBOL = "source function has no real name"
    ALREADY_NAMED = "target already has a name"
    SAME_NAME = "names already agree"
    MISSING = "function not found in the view"


@dataclass
class PortOptions:
    direction: PortDirection = PortDirection.TO_PRIMARY
    #: Below this similarity a match is more likely to be noise than a pairing,
    #: and a wrong name is worse than no name — it is indistinguishable from one
    #: you established yourself.
    min_similarity: float = 0.9
    #: Replace names the target side already has. Off by default: those are
    #: usually yours, and the diff is not evidence that they are wrong.
    overwrite: bool = False


@dataclass(frozen=True)
class Rename:
    addr: int
    old_name: str
    new_name: str
    similarity: float


@dataclass
class PortPlan:
    """What porting would do, before anything is written."""

    renames: list[Rename] = field(default_factory=list)
    skipped: dict[SkipReason, int] = field(default_factory=dict)

    def skip(self, reason: SkipReason) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def count(self) -> int:
        return len(self.renames)

    def summary(self) -> str:
        """One line per outcome, most important first."""

        lines = [f"{self.count} function(s) to rename"]
        lines += [
            f"{count} skipped: {reason.value}"
            for reason, count in sorted(self.skipped.items(), key=lambda kv: -kv[1])
        ]
        return "\n".join(lines)


def target_view(result: DiffResult, direction: PortDirection) -> BinaryView:
    """The view that would be modified."""

    # Compared by value, not identity: PortDirection is a str enum, and Qt
    # hands one back through QVariant as a plain str, which `is` silently gets
    # wrong -- and getting it wrong here means renaming the other binary.
    if direction == PortDirection.TO_PRIMARY:
        return result.primary_bv
    return result.secondary_bv


def plan_port(
    result: DiffResult,
    options: PortOptions | None = None,
    only: Iterable[int] | None = None,
) -> PortPlan:
    """Work out every rename, without performing any of them.

    ``only`` restricts the plan to those primary addresses — the rows somebody
    selected in the table. Keyed on the primary side whichever way the names
    travel, because that is what identifies a pair on screen.
    """

    options = options or PortOptions()
    wanted = None if only is None else set(only)
    to_primary = options.direction == PortDirection.TO_PRIMARY
    source_bv = result.secondary_bv if to_primary else result.primary_bv
    dest_bv = result.primary_bv if to_primary else result.secondary_bv

    plan = PortPlan()
    for match in result.matches:
        if wanted is not None and match.primary.addr not in wanted:
            continue
        if match.similarity < options.min_similarity:
            plan.skip(SkipReason.BELOW_THRESHOLD)
            continue

        source_ref = match.secondary if to_primary else match.primary
        dest_ref = match.primary if to_primary else match.secondary
        source_func = source_bv.get_function_at(source_ref.addr)
        dest_func = dest_bv.get_function_at(dest_ref.addr)
        if source_func is None or dest_func is None:
            plan.skip(SkipReason.MISSING)
            continue

        name = source_func.name
        if is_generated_name(name):
            plan.skip(SkipReason.NO_SYMBOL)
            continue
        if dest_func.name == name:
            plan.skip(SkipReason.SAME_NAME)
            continue
        if not options.overwrite and not is_generated_name(dest_func.name):
            plan.skip(SkipReason.ALREADY_NAMED)
            continue

        plan.renames.append(
            Rename(
                addr=dest_ref.addr,
                old_name=dest_func.name,
                new_name=name,
                similarity=match.similarity,
            )
        )
    return plan


def apply_port(
    bv: BinaryView,
    plan: PortPlan,
    progress: Callable[[str, float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    """Apply a plan to ``bv``. Returns how many functions were renamed.

    The batch is one undoable transaction, so a mistake is one Ctrl+Z rather
    than a few thousand, and an exception part-way through leaves the database
    exactly as it was instead of half-renamed. Cancelling is different from
    failing: what has already been applied is kept, and stays undoable.
    """

    report = progress or (lambda label, fraction: None)
    is_cancelled = cancelled or (lambda: False)
    total = len(plan.renames)
    if total == 0:
        return 0

    applied = 0
    with bv.undoable_transaction():
        for index, rename in enumerate(plan.renames):
            if is_cancelled():
                break
            func = bv.get_function_at(rename.addr)
            if func is None:
                continue
            func.name = rename.new_name
            applied += 1
            if index % _PROGRESS_EVERY == 0:
                report("Porting symbols", index / total)
    report("Porting symbols", 1.0)
    return applied


def refresh_names(result: DiffResult) -> None:
    """Re-read both sides' function names into the result, in place.

    The records hold the names as they were when the diff ran. Porting symbols
    invalidates half of them by design, and the match table reads them, so
    without this the table still shows the ``sub_...`` names it just replaced.
    """

    def current(bv: BinaryView, ref: FunctionRef) -> FunctionRef:
        func = bv.get_function_at(ref.addr)
        if func is None or func.name == ref.name:
            return ref
        return replace(ref, name=func.name)

    result.matches = [
        replace(
            match,
            primary=current(result.primary_bv, match.primary),
            secondary=current(result.secondary_bv, match.secondary),
        )
        for match in result.matches
    ]
    result.primary_unmatched = [current(result.primary_bv, f) for f in result.primary_unmatched]
    result.secondary_unmatched = [
        current(result.secondary_bv, f) for f in result.secondary_unmatched
    ]
    result.reindex()


def is_persistent(bv: BinaryView) -> bool:
    """Whether renames in ``bv`` have a database to be saved into."""

    try:
        return bool(bv.file.has_database)
    except Exception:
        return False


def save_database(bv: BinaryView) -> bool:
    """Write the view's database back to disk, if it has one.

    The secondary is not open in the UI, so nothing else will ever offer to
    save it; without this, porting into it would be lost on closing the tab.
    """

    if not is_persistent(bv):
        return False
    try:
        return bool(bv.file.save_auto_snapshot())
    except Exception:
        log_warn("Could not save the secondary database", "QBinDiff")
        return False


class PortSymbolsTask(BackgroundTaskThread):
    """Apply a symbol port off the UI thread.

    Planning is read-only and cheap enough to run inline for a preview; each
    rename, on the other hand, defines a user symbol and schedules analysis, so
    a few thousand of them are not something to do on the main thread.
    """

    def __init__(
        self,
        result: DiffResult,
        options: PortOptions,
        on_done: Callable[[PortPlan, int], None],
        on_error: Callable[[str], None],
        save: bool = False,
        on_progress: Callable[[str, float], None] | None = None,
        only: Iterable[int] | None = None,
    ):
        super().__init__("Binary diff: porting symbols", can_cancel=True)
        self._result = result
        self._options = options
        self._on_done = on_done
        self._on_error = on_error
        self._save = save
        self._on_progress = on_progress
        self._only = None if only is None else list(only)

    def _report(self, label: str, fraction: float) -> None:
        self.progress = f"Binary diff: {label} ({fraction * 100:.0f}%)"
        if self._on_progress is not None:
            self._on_progress(label, fraction)

    def run(self) -> None:
        try:
            bv = target_view(self._result, self._options.direction)
            self._report("Planning the symbol port", 0.0)
            plan = plan_port(self._result, self._options, self._only)
            applied = apply_port(bv, plan, progress=self._report, cancelled=lambda: self.cancelled)
            log_info(
                f"Ported {applied} symbol(s), {PortDirection(self._options.direction).value}",
                "QBinDiff",
            )
            if self._save and applied:
                if save_database(bv):
                    log_info("Saved the receiving database", "QBinDiff")
            self._on_done(plan, applied)
        except Exception as exc:
            log_error(traceback.format_exc(), "QBinDiff")
            self._on_error(str(exc))
