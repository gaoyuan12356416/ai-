"""Isolated control-plane primitives for X automatic publishing templates.

The package owns only its independent template, run, task, reservation, and
metric state.  Existing X credentials, queues, history, validation, and final
publishing remain behind explicitly injected adapters.
"""

from .core import XAutoPostStore, XAutoPostStoreError

__all__ = ["XAutoPostStore", "XAutoPostStoreError"]
