"""Runtime errors. Operational failures may be retried or caught by `else`; security errors never are."""
from __future__ import annotations

from typing import Optional

from .errors import Span


class RemitRuntimeError(Exception):
    code = "R0000"
    operational = False  # True: may be retried / caught by `else`

    def __init__(self, message: str, span: Optional[Span] = None, data: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.span = span
        self.data = data or {}

    def render(self) -> str:
        loc = f"{self.span}: " if self.span else ""
        return f"{loc}runtime error[{self.code}]: {self.message}"


class EvalError(RemitRuntimeError):
    code = "R0101"


class ListBoundExceeded(RemitRuntimeError):
    code = "R0113"


class PolicyViolation(RemitRuntimeError):
    code = "R0301"


class ApprovalRequired(RemitRuntimeError):
    code = "R0302"


class NotGranted(RemitRuntimeError):
    code = "R0303"


class BudgetExceeded(RemitRuntimeError):
    code = "R0401"


class CallLimitExceeded(RemitRuntimeError):
    code = "R0402"


class CapabilityFailed(RemitRuntimeError):
    code = "R0501"
    operational = True


class ModelOutputInvalid(RemitRuntimeError):
    code = "R0502"
    operational = True


class Timeout(RemitRuntimeError):
    code = "R0503"
    operational = True


class ReplayDivergence(RemitRuntimeError):
    code = "R0601"


class NonIdempotentRetry(RemitRuntimeError):
    code = "R0405"
