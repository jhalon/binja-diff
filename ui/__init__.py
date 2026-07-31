# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Qt widgets for the diff view.

Every module in this package imports ``binaryninjaui`` before ``PySide6``.
Binary Ninja ships a custom PySide6 build ABI-matched to its own
``libbinaryninjaui``; importing the two in the wrong order loads the wrong
PySide6 and crashes the process rather than raising.
"""
