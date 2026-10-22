"""Per-case log capture that stays correct when eval cases run concurrently.

Several checks read back what the shared "clinical_copilot" logger emitted
during ONE case (no_phi_in_logs, the routing_decision assertions). With
cases running on parallel threads, a plain logging.Handler attached to that
logger would also receive every other in-flight case's lines: a PHI leak
would be blamed on the wrong case, and "expected exactly 1 routing_decision
line" would see one per concurrent case.

run_evals sets `current_case` to a unique tag inside each case's worker
thread. A ScopedLogCapture remembers the tag active when it was built and
only keeps records emitted under that same tag. A record with no tag (it
came from a thread that didn't inherit the context) is kept by every
active capture: over-attribution can only fail a check, never let a leak
slip past one. Outside any tagged context (serial runs, unit tests) it
captures everything, exactly like the plain handler it replaces.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

current_case: ContextVar[str | None] = ContextVar("eval_current_case", default=None)


class ScopedLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self._owner = current_case.get()

    def emit(self, record: logging.LogRecord) -> None:
        tag = current_case.get()
        if self._owner is None or tag is None or tag == self._owner:
            self.messages.append(record.getMessage())
