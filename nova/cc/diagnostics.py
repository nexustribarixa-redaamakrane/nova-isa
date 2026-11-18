"""Diagnostics and source locations for :mod:`nova.cc`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceLocation:
    """A human-readable source position."""

    file: str = "<stdin>"
    line: int = 1
    column: int = 1

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column}"


@dataclass(frozen=True)
class Diagnostic:
    """A compiler diagnostic anchored to a source location."""

    location: SourceLocation
    message: str
    level: str = "error"

    def __str__(self) -> str:
        return f"{self.location}: {self.level}: {self.message}"


class CompileError(Exception):
    """Raised when compilation cannot continue."""

    def __init__(self, message: str, location: Optional[SourceLocation] = None):
        self.message = message
        self.location = location or SourceLocation()
        self.diagnostic = Diagnostic(self.location, message)
        super().__init__(str(self.diagnostic))

    @classmethod
    def at(cls, location: SourceLocation, message: str) -> "CompileError":
        return cls(message, location)
