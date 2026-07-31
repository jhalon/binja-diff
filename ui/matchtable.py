# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The function match table driving the rest of the diff view."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core import align
from ..core.align import BlockStatus, FunctionStatus, LineStatus
from ..core.engine import DiffResult
from . import theme


class RowKind(str, Enum):
    MATCHED = "matched"
    PRIMARY_ONLY = "primary only"
    SECONDARY_ONLY = "secondary only"


@dataclass
class MatchRow:
    kind: RowKind
    primary_addr: int | None
    primary_name: str
    secondary_addr: int | None
    secondary_name: str
    similarity: float
    confidence: float

    @property
    def is_matched(self) -> bool:
        return self.kind is RowKind.MATCHED


#: The IL level the Status column describes, and the one its tooltip explains.
_LEVEL = "Disassembly"

#: Differing lines shown in a status tooltip before it is truncated.
_EXPLAIN_LINES = 6


_COLUMNS = (
    ("Primary", 200),
    ("Address", 100),
    ("Secondary", 200),
    ("Address", 100),
    ("Similarity", 90),
    ("Confidence", 90),
    ("Status", 110),
)


#: What the numeric columns actually measure. The similarity in particular
#: invites a reading it does not support: QBinDiff computes it as a MinHash
#: over basic blocks, one shingle per block holding that block's mnemonics, so
#: a single-block function scores 1.0 or 0.0 and nothing in between. A function
#: whose one block gained an instruction reads 0.000 while being the same code
#: — which is what the Status column is for.
_HEADER_TOOLTIPS = {
    4: (
        "How much of the function is the same code, counted per line: lines that\n"
        "are identical or differ only in an operand's spelling, over the longer\n"
        "side. Computed from the same comparison the panes draw."
    ),
    5: "How sure the matcher is of the pairing, from belief propagation over the call graph.",
    6: "How the code compares, line by line. Hover a cell for the differing lines.",
}


def _status_color(status: FunctionStatus | None):
    """Row tint. Matches the colour the text panes give the same distinction."""

    if status is FunctionStatus.IDENTICAL:
        return theme.block_color(BlockStatus.IDENTICAL)
    if status is FunctionStatus.MINOR:
        return theme.line_color(LineStatus.MINOR)
    if status is FunctionStatus.CHANGED:
        return theme.block_color(BlockStatus.CHANGED)
    # Unclassified or still unknown: leave the row alone rather than guess.
    return None


class MatchTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[MatchRow] = []
        self._result: DiffResult | None = None
        #: Function statuses, computed the first time a row is painted and kept
        #: for as long as the result stands. Classifying every pair up front
        #: would mean disassembling both binaries in full before showing
        #: anything; Qt only asks about the rows on screen.
        self._status: dict[tuple[int, int], FunctionStatus] = {}
        #: Per-pair line similarity, filled by the same pass as the status.
        self._same: dict[tuple[int, int], float] = {}
        #: Tooltip text per pair, filled on hover. See explain().
        self._explained: dict[tuple[int, int], str] = {}

    def _classify(self, row: MatchRow):
        """(status, line similarity) for a pair, both from one comparison.

        Computed on first paint and cached for as long as the result stands.
        Classifying every pair up front would mean rendering both binaries
        before the table could appear; Qt only asks about the rows on screen.
        """

        if not row.is_matched or self._result is None:
            return None, None
        if row.primary_addr is None or row.secondary_addr is None:
            return None, None
        key = (row.primary_addr, row.secondary_addr)
        cached = self._status.get(key)
        if cached is not None:
            return cached, self._same.get(key)

        primary = self._result.primary_bv.get_function_at(row.primary_addr)
        secondary = self._result.secondary_bv.get_function_at(row.secondary_addr)
        if primary is None or secondary is None:
            return None, None
        try:
            status, rows = align.classify_pair(primary, secondary, _LEVEL)
        except Exception:
            # Painting must not fail over a function that will not render.
            status, rows = align.FunctionStatus.UNKNOWN, []
        if status is None:
            # Not drawn yet. Leave the cell blank and ask again on the next
            # repaint rather than record a verdict taken from half a function.
            return None, None
        self._status[key] = status
        if rows:
            self._same[key] = align.text_similarity(rows)
        return status, self._same.get(key)

    def status_of(self, row: MatchRow) -> FunctionStatus | None:
        """How the pair actually compares, or None if it cannot be worked out."""

        return self._classify(row)[0]

    def line_similarity_of(self, row: MatchRow) -> float | None:
        """How much of the pair is the same code. None while unclassified.

        Deliberately not QBinDiff's similarity, which is a MinHash over whole
        basic blocks: a one-block function that gained an instruction shares no
        shingle with its own previous build and scores 0.000. That number is
        kept in the result (and in a saved diff) but is not what the table
        shows, because it answers a question nobody asked of this column.
        """

        return self._classify(row)[1]

    def explain(self, row: MatchRow) -> str:
        """The lines behind a row's status, for its tooltip.

        Computed only when the user hovers, and cached. A status that
        disagrees with what the panes show is otherwise impossible to
        investigate without a debugger.
        """

        if not row.is_matched or self._result is None:
            return ""
        if row.primary_addr is None or row.secondary_addr is None:
            return ""
        key = (row.primary_addr, row.secondary_addr)
        cached = self._explained.get(key)
        if cached is not None:
            return cached

        primary = self._result.primary_bv.get_function_at(row.primary_addr)
        secondary = self._result.secondary_bv.get_function_at(row.secondary_addr)
        if primary is None or secondary is None:
            return ""
        try:
            _status, rows = align.classify_pair(primary, secondary, _LEVEL)
        except Exception as exc:
            return f"could not render this pair: {exc}"

        differing = [aligned for aligned in rows if aligned.status.is_difference]
        if not differing:
            text = "no differing instructions"
        else:
            lines = [f"{len(differing)} differing line(s):"]
            for aligned in differing[:_EXPLAIN_LINES]:
                lines.append(f"{aligned.status.marker} {aligned.status.value}")
                lines.append(f"    {aligned.left if aligned.left is not None else '-'}")
                lines.append(f"    {aligned.right if aligned.right is not None else '-'}")
            if len(differing) > _EXPLAIN_LINES:
                lines.append(f"... and {len(differing) - _EXPLAIN_LINES} more")
            text = "\n".join(lines)
        self._explained[key] = text
        return text

    def set_result(self, result: DiffResult | None) -> None:
        self.beginResetModel()
        self._rows = []
        self._result = result
        self._status.clear()
        self._explained.clear()
        if result is not None:
            for match in result.matches:
                self._rows.append(
                    MatchRow(
                        RowKind.MATCHED,
                        match.primary.addr,
                        match.primary.name,
                        match.secondary.addr,
                        match.secondary.name,
                        float(match.similarity),
                        float(match.confidence),
                    )
                )
            for func in result.primary_unmatched:
                self._rows.append(
                    MatchRow(RowKind.PRIMARY_ONLY, func.addr, func.name, None, "", 0.0, 0.0)
                )
            for func in result.secondary_unmatched:
                self._rows.append(
                    MatchRow(RowKind.SECONDARY_ONLY, None, "", func.addr, func.name, 0.0, 0.0)
                )
            self._rows.sort(key=lambda r: (-r.similarity, r.primary_addr or r.secondary_addr or 0))
        self.endResetModel()

    def row_at(self, index: int) -> MatchRow | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.ToolTipRole:
            return _HEADER_TOOLTIPS.get(section)
        if role != Qt.DisplayRole:
            return None
        return _COLUMNS[section][0]

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return row.primary_name or "-"
            if column == 1:
                return f"{row.primary_addr:#x}" if row.primary_addr is not None else "-"
            if column == 2:
                return row.secondary_name or "-"
            if column == 3:
                return f"{row.secondary_addr:#x}" if row.secondary_addr is not None else "-"
            if column == 4:
                if not row.is_matched:
                    return "-"
                same = self.line_similarity_of(row)
                return f"{same * 100:.0f}%" if same is not None else ""
            if column == 5:
                return f"{row.confidence:.3f}" if row.kind is RowKind.MATCHED else "-"
            if column == 6:
                if not row.is_matched:
                    return row.kind.value
                status = self.status_of(row)
                return status.value if status is not None else RowKind.MATCHED.value

        # Sort on the raw values so numeric columns order correctly. The status
        # column deliberately sorts on similarity rather than on the classified
        # status: sorting asks every row at once, and classifying the whole
        # table on the UI thread is exactly what the laziness avoids.
        if role == Qt.UserRole:
            # The Similarity column sorts on whatever has been classified already,
            # falling back to QBinDiff's score for rows nobody has looked at:
            # sorting asks every row at once, and classifying the whole table
            # on the UI thread is exactly what the laziness above avoids.
            same = self._same.get((row.primary_addr or 0, row.secondary_addr or 0))
            return (
                row.primary_name,
                row.primary_addr or 0,
                row.secondary_name,
                row.secondary_addr or 0,
                row.similarity if same is None else same,
                row.confidence,
                (row.kind.value, -row.similarity),
            )[column]

        if role == Qt.BackgroundRole:
            if not row.is_matched:
                return theme.block_color(BlockStatus.UNMATCHED)
            return _status_color(self.status_of(row))

        if role == Qt.ToolTipRole:
            if column == 6:
                return self.explain(row)
            if column == 4 and row.is_matched:
                same = self.line_similarity_of(row)
                lines = [_HEADER_TOOLTIPS[4]]
                if same is not None:
                    lines.append(f"\nThis pair: {same * 100:.1f}% of its lines.")
                lines.append(f"QBinDiff's own score for it: {row.similarity:.3f}")
                return "\n".join(lines)

        if role == Qt.TextAlignmentRole and column in (1, 3, 4, 5):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None


class _FilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self._kind: RowKind | None = None
        self._status: FunctionStatus | None = None
        self._text = ""

    def set_filter(self, kind: RowKind | None, status: FunctionStatus | None) -> None:
        self._kind = kind
        self._status = status
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        row = model.row_at(source_row)
        if row is None:
            return False
        # Compared by value: these are str enums, and Qt hands one back from
        # itemData() as a plain str, which `is` silently never matches — the
        # filter simply emptied the table. Same trap as PortDirection.
        if self._kind is not None and row.kind != self._kind:
            return False
        if self._status is not None:
            if not row.is_matched:
                return False
            if model.status_of(row) != self._status:
                return False
        if self._text:
            haystack = f"{row.primary_name} {row.secondary_name}".lower()
            if self._text not in haystack:
                return False
        return True


class MatchTable(QWidget):
    """Table of function matches. Emits the current row, and menu requests."""

    selectionChanged = Signal(object)
    #: Right-click, with the global position to pop a menu at. The rows it
    #: applies to come from selected_rows(); what may be done with them depends
    #: on the two views, which the diff view owns rather than this widget.
    contextMenuRequested = Signal(object)

    def __init__(self, parent: QWidget):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        # Item data is a plain string rather than the enum itself: Qt converts
        # a str enum to str on the way through QVariant, so storing the member
        # only invites the identity comparison that broke this filter before.
        self.filter_combo = QComboBox(self)
        self.filter_combo.addItem("All", None)
        for kind in RowKind:
            self.filter_combo.addItem(kind.value.title(), f"kind:{kind.value}")
        self.filter_combo.insertSeparator(self.filter_combo.count())
        for status in (FunctionStatus.IDENTICAL, FunctionStatus.MINOR, FunctionStatus.CHANGED):
            self.filter_combo.addItem(status.value.title(), f"status:{status.value}")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(QLabel("Show:", self))
        controls.addWidget(self.filter_combo)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Filter by function name...")
        self.search.textChanged.connect(lambda text: self.proxy.set_text(text))
        controls.addWidget(self.search, 1)

        self.stats = QLabel("", self)
        controls.addWidget(self.stats)
        layout.addLayout(controls)

        self.model = MatchTableModel(self)
        self.proxy = _FilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Extended rather than single: porting a name is worth doing to a
        # hundred functions at once, and the panes follow the *current* row
        # regardless of how many are selected.
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._request_menu)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        header = self.table.horizontalHeader()
        for column, (_name, width) in enumerate(_COLUMNS):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.selectionModel().selectionChanged.connect(self._emit_selection)
        layout.addWidget(self.table, 1)

    def _apply_filter(self, index: int) -> None:
        """Split the chosen entry into a pairing filter and a status filter.

        Filtering by status classifies every row it is asked about, so picking
        one costs a pass over the table — unavoidable, since a status is a
        property of the code rather than of the match.
        """

        selected = self.filter_combo.itemData(index)
        kind = status = None
        if isinstance(selected, str):
            group, _, value = selected.partition(":")
            if group == "kind":
                kind = RowKind(value)
            elif group == "status":
                status = FunctionStatus(value)
        self.proxy.set_filter(kind, status)

    def _emit_selection(self, *_args) -> None:
        # The panes show one pair, so they follow the current row: with several
        # selected, "the one the user last touched" is the only sensible pick.
        index = self.table.currentIndex()
        if not index.isValid():
            self.selectionChanged.emit(None)
            return
        self.selectionChanged.emit(self.model.row_at(self.proxy.mapToSource(index).row()))

    def _request_menu(self, pos) -> None:
        """Ask for a menu, having first made sure the click is on a selection.

        Right-clicking a row outside the selection selects it, which is what
        every other table does; without it the menu would silently act on rows
        the user cannot see any more.
        """

        index = self.table.indexAt(pos)
        if index.isValid() and index not in self.table.selectionModel().selectedRows(
            index.column()
        ):
            self.table.setCurrentIndex(index)
            self.table.selectRow(index.row())
        if self.selected_rows():
            self.contextMenuRequested.emit(self.table.viewport().mapToGlobal(pos))

    def selected_rows(self) -> list[MatchRow]:
        """Every selected row, in the order the table shows them."""

        rows = []
        for index in self.table.selectionModel().selectedRows():
            row = self.model.row_at(self.proxy.mapToSource(index).row())
            if row is not None:
                rows.append(row)
        return rows

    def set_result(self, result: DiffResult | None) -> None:
        self.model.set_result(result)
        if result is None:
            self.stats.setText("")
            return
        self.stats.setText(
            f"{result.nb_match} matched   "
            f"{result.nb_unmatched_primary} primary-only   "
            f"{result.nb_unmatched_secondary} secondary-only   "
            f"similarity {result.similarity:.3f}"
        )
        if self.proxy.rowCount() > 0:
            self.table.selectRow(0)
