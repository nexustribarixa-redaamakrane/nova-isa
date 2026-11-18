"""Nova64 freestanding C compiler (nova-cc).

Phase 1 provides a small, inspectable C compiler that lowers C to Nova
assembly and uses the existing :mod:`nova.assembler` and :mod:`nova.link`
toolchain to produce real ``.nobj`` and ``.nob`` artifacts.
"""

from __future__ import annotations

from .diagnostics import CompileError, Diagnostic, SourceLocation
from .lexer import KEYWORDS, Token, TokenKind, lex

__all__ = [
    "CompileError",
    "Diagnostic",
    "SourceLocation",
    "KEYWORDS",
    "Token",
    "TokenKind",
    "lex",
]
