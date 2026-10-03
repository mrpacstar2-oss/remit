"""Diagnostics with source locations."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Span:
    file: str
    line: int
    col: int
    end_line: int = 0
    end_col: int = 0

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


@dataclass
class Diagnostic:
    code: str
    message: str
    span: Span | None
    severity: str = "error"  # "error" | "warning"
    hint: str | None = None
    notes: list[str] = field(default_factory=list)

    def render(self, sources: dict[str, str] | None = None) -> str:
        loc = str(self.span) if self.span else "<unknown>"
        out = [f"{loc}: {self.severity}[{self.code}]: {self.message}"]
        if self.span and sources and self.span.file in sources:
            lines = sources[self.span.file].splitlines()
            if 0 < self.span.line <= len(lines):
                text = lines[self.span.line - 1]
                gutter = f"{self.span.line:>4} | "
                out.append(gutter + text)
                width = 1
                if self.span.end_line == self.span.line and self.span.end_col > self.span.col:
                    width = self.span.end_col - self.span.col
                out.append(" " * (len(gutter) + self.span.col - 1) + "^" * max(1, width))
        for n in self.notes:
            out.append(f"  note: {n}")
        if self.hint:
            out.append(f"  hint: {self.hint}")
        return "\n".join(out)

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "file": self.span.file if self.span else None,
            "line": self.span.line if self.span else None,
            "col": self.span.col if self.span else None,
            "hint": self.hint,
            "notes": self.notes,
        }


class RemitError(Exception):
    """Raised for fatal front-end errors (lexing/parsing)."""

    def __init__(self, diag: Diagnostic):
        super().__init__(diag.message)
        self.diag = diag
