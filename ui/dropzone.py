# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Drag-and-drop landing area for the second binary.

Binary Ninja's UI API has no drag-and-drop support, so this is plain Qt 6.
Note ``QDropEvent.pos()`` no longer exists in Qt 6; use ``position()``.
"""

from __future__ import annotations

import os

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


#: Databases are listed first because diffing against saved analysis (renamed
#: functions, applied types, comments) is usually more useful than re-analyzing
#: the raw file.
FILE_FILTER = (
    "Binaries and databases (*.bndb *.exe *.dll *.so *.dylib *.o *.bin);;"
    "Binary Ninja databases (*.bndb);;"
    "All files (*)"
)

#: The prompt reads as a single card rather than filling the tab. Selectors are
#: id-based on purpose: a bare ``QFrame`` selector also matches QLabel, QSplitter
#: and friends, which draws the border around every child too.
_CARD_STYLE = "#binjaDiffCard { border: 2px dashed %s; border-radius: 8px; }"

#: Wide enough for the explanation to wrap into a readable column, narrow enough
#: that it does not stretch across an ultrawide monitor.
_CARD_WIDTH = 560

#: Below this the card stops shrinking and is allowed to clip instead.
_CARD_MIN_WIDTH = 320


def local_files(mime) -> list[str]:
    """Extract droppable local file paths from mime data."""

    if not mime.hasUrls():
        return []
    return [
        url.toLocalFile()
        for url in mime.urls()
        if url.isLocalFile() and os.path.isfile(url.toLocalFile())
    ]


def dimmed(label: QLabel) -> QLabel:
    """Mute secondary text so it recedes without disappearing.

    The colour is resolved once, from the palette in force when the label is
    built; switching theme mid-session leaves it stale until the view is
    rebuilt. That is the price of not using ``palette(mid)``, which follows the
    theme and is unreadable in Binary Ninja's dark ones (see theme.muted_text).
    """

    label.setStyleSheet(f"color: {theme.muted_text(label).name()};")
    return label


class DropZone(QWidget):
    """Full-panel prompt shown until a secondary binary is chosen."""

    fileChosen = Signal(str)
    restoreRequested = Signal()

    def __init__(self, parent: QWidget, primary_name: str | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)

        # Drops land anywhere in the panel; the card is only the visual target.
        outer = QVBoxLayout(self)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._build_card(primary_name))
        row.addStretch(1)
        outer.addLayout(row)

        outer.addStretch(1)
        self._set_active(False)

    def _build_card(self, primary_name: str | None) -> QFrame:
        self.card = QFrame(self)
        self.card.setObjectName("binjaDiffCard")
        # Width is driven from resizeEvent. Centering the card between two
        # stretches would otherwise squeeze it down to its size hint, which for
        # a word-wrapped label is a narrow column.
        self.card.setFixedWidth(_CARD_WIDTH)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(10)

        title = QLabel("Drop a binary or .bndb here", self.card)
        title.setAlignment(Qt.AlignCenter)
        font = title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        self.subtitle = QLabel(self.card)
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.set_primary_name(primary_name)
        layout.addWidget(dimmed(self.subtitle))

        layout.addSpacing(6)

        hint = QLabel("or", self.card)
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(dimmed(hint))

        button = QPushButton("Choose file...", self.card)
        button.setMinimumWidth(160)
        button.clicked.connect(self.browse)
        layout.addWidget(button, alignment=Qt.AlignCenter)

        layout.addSpacing(6)

        note = QLabel(
            "The dropped file is loaded and analyzed in the background. Dropping a "
            "Binary Ninja database (.bndb) reuses its saved analysis, so renamed "
            "functions, applied types and comments all carry into the diff.",
            self.card,
        )
        note.setAlignment(Qt.AlignCenter)
        note.setWordWrap(True)
        layout.addWidget(dimmed(note))

        # Only ever shown when the database already holds a diff. It sits below
        # the drop prompt because re-running a diff stays the primary action;
        # restoring is the shortcut for the session you left behind.
        self.restore_note = QLabel(self.card)
        self.restore_note.setAlignment(Qt.AlignCenter)
        self.restore_note.setWordWrap(True)
        self.restore_button = QPushButton("Restore saved diff", self.card)
        self.restore_button.setMinimumWidth(160)
        self.restore_button.clicked.connect(self.restoreRequested)
        layout.addSpacing(6)
        layout.addWidget(dimmed(self.restore_note))
        layout.addWidget(self.restore_button, alignment=Qt.AlignCenter)
        self.set_saved_diff(None)

        return self.card

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        available = self.width() - 64
        self.card.setFixedWidth(max(min(_CARD_WIDTH, available), _CARD_MIN_WIDTH))

    def set_saved_diff(self, summary: str | None) -> None:
        """Offer to restore a diff found in the database, or hide the offer."""

        self.restore_note.setText(
            f"This database already holds a diff: {summary}" if summary else ""
        )
        self.restore_note.setVisible(summary is not None)
        self.restore_button.setVisible(summary is not None)

    def set_primary_name(self, name: str | None) -> None:
        target = os.path.basename(name) if name else "the current binary"
        self.subtitle.setText(f"to diff it against {target}")

    def _set_active(self, active: bool) -> None:
        # palette(highlight) is a real accent colour in every theme; the idle
        # border needs the same treatment as dimmed text to stay visible.
        border = "palette(highlight)" if active else theme.muted_text(self.card).name()
        self.card.setStyleSheet(_CARD_STYLE % border)

    def browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select a binary or database to diff against",
            "",
            FILE_FILTER,
        )
        if path:
            self.fileChosen.emit(path)

    def dragEnterEvent(self, event) -> None:
        if local_files(event.mimeData()):
            self._set_active(True)
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if local_files(event.mimeData()):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._set_active(False)

    def dropEvent(self, event) -> None:
        self._set_active(False)
        paths = local_files(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self.fileChosen.emit(paths[0])
