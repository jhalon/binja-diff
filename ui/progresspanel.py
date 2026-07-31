# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Centered progress panel shown while a diff runs.

This replaces the drop prompt rather than sitting beside it. Loading and
matching two binaries takes tens of seconds, and leaving an enabled "Drop a
binary here" panel on screen throughout invites a second drop that is then
refused.
"""

from __future__ import annotations

import os
import time

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import format_duration
from .dropzone import dimmed

_BAR_WIDTH = 440

#: The elapsed times tick on their own rather than being refreshed from
#: progress updates: matching can spend minutes between two iterations on a
#: large pair, and a clock that freezes there reads as a hung plugin.
_TICK_MS = 1000


def _phase_key(label: str) -> str:
    """Phase identity, ignoring a trailing percentage.

    ``load_secondary`` folds a database's own load progress into the label
    ("Opening database x (37%)"), so comparing the raw text would restart the
    phase clock on every percent of the slowest phase there is.
    """

    head, separator, tail = label.rpartition(" (")
    return head if separator and tail.endswith("%)") else label


class ProgressPanel(QWidget):
    """Phase, percentage and a cancel button, centered in the tab."""

    cancelled = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.addStretch(1)

        self.title = QLabel(self)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setTextFormat(Qt.PlainText)
        font = self.title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.title.setFont(font)
        outer.addWidget(self.title)

        self.pair = QLabel(self)
        self.pair.setAlignment(Qt.AlignCenter)
        self.pair.setTextFormat(Qt.PlainText)
        self.pair.setWordWrap(True)
        outer.addWidget(dimmed(self.pair))

        outer.addSpacing(16)

        bar_row = QHBoxLayout()
        bar_row.addStretch(1)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setFixedWidth(_BAR_WIDTH)
        bar_row.addWidget(self.bar)
        bar_row.addStretch(1)
        outer.addLayout(bar_row)

        self.phase = QLabel(self)
        self.phase.setAlignment(Qt.AlignCenter)
        self.phase.setTextFormat(Qt.PlainText)
        outer.addWidget(dimmed(self.phase))

        self.elapsed = QLabel(self)
        self.elapsed.setAlignment(Qt.AlignCenter)
        self.elapsed.setTextFormat(Qt.PlainText)
        outer.addWidget(dimmed(self.elapsed))

        self._label = ""
        self._started = time.monotonic()
        self._phase_started = self._started
        self._clock = QTimer(self)
        self._clock.setInterval(_TICK_MS)
        self._clock.timeout.connect(self._render_elapsed)

        outer.addSpacing(16)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setMinimumWidth(160)
        self.cancel_button.clicked.connect(self._on_cancel)
        outer.addWidget(self.cancel_button, alignment=Qt.AlignCenter)

        outer.addStretch(1)

    def begin(self, primary: str | None, secondary: str | None, title: str = "Diffing") -> None:
        self.title.setText(title)
        self.pair.setText(
            f"{os.path.basename(primary or '?')}  \u2194  {os.path.basename(secondary or '?')}"
        )
        self.phase.setText("Starting...")
        self._set_bar(0)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")

        self._label = ""
        self._started = self._phase_started = time.monotonic()
        self._render_elapsed()
        self._clock.start()

    def finish(self) -> None:
        """Stop the clock. Whatever the panel last showed stays on screen."""

        self._clock.stop()

    def set_progress(self, label: str, percent: int) -> None:
        if _phase_key(label) != _phase_key(self._label):
            self._phase_started = time.monotonic()
        self._label = label
        self.phase.setText(label or "")
        self._set_bar(percent)
        self._render_elapsed()

    def _set_bar(self, percent: int) -> None:
        """Show a busy indicator for phases that cannot report a fraction.

        A bar frozen at 100% while the work continues is worse than no bar:
        it says the step is finished when it is not.
        """

        if percent < 0:
            self.bar.setRange(0, 0)
            return
        self.bar.setRange(0, 100)
        self.bar.setValue(percent)

    def _render_elapsed(self) -> None:
        now = time.monotonic()
        phase = format_duration(now - self._phase_started)
        total = format_duration(now - self._started)
        # The phase time is the one being watched; the total is context for it.
        self.elapsed.setText(f"{phase}   (total {total})" if self._label else f"total {total}")

    def _on_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")
        self.phase.setText("Cancelling...")
        self.cancelled.emit()
