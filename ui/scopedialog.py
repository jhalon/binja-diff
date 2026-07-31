# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Choosing which part of a container to diff: a kext, a SEP module."""

from __future__ import annotations

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.scope import Region
from .dropzone import dimmed


class ScopeDialog(QDialog):
    """Pick one region, or the whole binary.

    The list comes from the *primary* alone, before the second binary has been
    opened: a kext and a SEP module keep their name between builds, so a name
    is enough to find the same part on the other side, and asking first means
    never analyzing a 268 MB kernelcache to offer the choice.
    """

    def __init__(self, parent: QWidget, regions: list[Region], container: str):
        super().__init__(parent)
        self.setWindowTitle("Choose what to diff")
        self.setModal(True)
        self.resize(420, 460)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"This is a {container}. Diff one part of it, or all of it:", self))

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Filter...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list = QListWidget(self)
        whole = QListWidgetItem("Everything loaded in the primary")
        whole.setData(Qt.UserRole, "")
        self.list.addItem(whole)
        for region in regions:
            label = region.name if region.loaded else f"{region.name}   (not loaded yet)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, region.name)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)

        note = QLabel(
            "Matching is quadratic, so one part diffs in a fraction of the time the "
            "whole container would take. A kernelcache loads the kext it needs; a SEP "
            "module has to have been loaded in sep-binja first, and only loaded ones "
            "are listed.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(dimmed(note))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def region_name(self) -> str | None:
        """The chosen region, or ``None`` for the whole binary."""

        item = self.list.currentItem()
        if item is None:
            return None
        # Item data survives QVariant as a plain string on purpose; a str enum
        # would not (see MatchTable's filter and PortDirection).
        return item.data(Qt.UserRole) or None
