"""Nova64 freestanding C compiler (nova-cc).

Phase 1 provides a small, inspectable C compiler that lowers C to Nova
assembly and uses the existing :mod:`nova.assembler` and :mod:`nova.link`
toolchain to produce real ``.nobj`` and ``.nob`` artifacts.
"""

from __future__ import annotations

from .diagnostics import CompileError, Diagnostic, SourceLocation
from .lexer import KEYWORDS, Token, TokenKind, lex
from .preprocessor import Preprocessor, preprocess, preprocess_to_tokens, tokens_to_text

__all__ = [
    "CompileError",
    "Diagnostic",
    "SourceLocation",
    "KEYWORDS",
    "Token",
    "TokenKind",
    "lex",
    "Preprocessor",
    "preprocess",
    "preprocess_to_tokens",
    "tokens_to_text",
]
