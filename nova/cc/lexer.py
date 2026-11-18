"""C lexer for :mod:`nova.cc`.

The lexer tokenizes a translation unit into C tokens with precise source
locations. It handles comments, string and character literals with escapes, and
the punctuators needed by the Phase 1 C subset. Preprocessing directives are
returned as ordinary tokens (``#`` and identifiers) so the preprocessor can
operate on the token stream in a later step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Iterable, List, Optional

from .diagnostics import CompileError, SourceLocation


class TokenKind(Enum):
    EOF = auto()
    IDENT = auto()
    KEYWORD = auto()
    INT = auto()
    CHAR = auto()
    STRING = auto()
    PUNCT = auto()


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    location: SourceLocation
    # For INT/CHAR tokens, numeric value when known.
    number: Optional[int] = None
    # For STRING, decoded bytes/text payload without surrounding quotes.
    string: Optional[str] = None

    def __repr__(self) -> str:
        extra = ""
        if self.number is not None:
            extra += f", number={self.number}"
        if self.string is not None:
            extra += f", string={self.string!r}"
        return f"Token({self.kind.name}, {self.value!r}, {self.location}{extra})"


# Keywords required by the Phase 1 subset plus common C keywords so they are not
# misclassified as identifiers.
KEYWORDS = {
    "_Bool",
    "_Complex",
    "_Generic",
    "_Noreturn",
    "break",
    "case",
    "char",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extern",
    "float",
    "for",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "register",
    "restrict",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "struct",
    "switch",
    "typedef",
    "union",
    "unsigned",
    "void",
    "volatile",
    "while",
}

# Punctuators, longest first so multi-character operators match greedily.
PUNCTUATORS = (
    "...",
    ">>=",
    "<<=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "^=",
    "|=",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "++",
    "--",
    "->",
    "<<",
    ">>",
    "##",
    "(", ")", "[", "]", "{", "}",
    ".", ",", ";", ":",
    "?",
    "~",
    "!",
    "+", "-", "*", "/", "%",
    "<", ">",
    "=", "&", "^", "|",
    "#",
)

_SIMPLE_ESCAPES: Dict[str, int] = {
    "'": ord("'"),
    '"': ord('"'),
    "?": ord("?"),
    "\\": ord("\\"),
    "a": 7,
    "b": 8,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
}


def lex(source: str, filename: str = "<stdin>") -> List[Token]:
    """Tokenize *source* into a list of C tokens terminated by EOF."""
    if not isinstance(source, str):
        raise TypeError("C source must be a string.")
    lexer = _Lexer(source, filename)
    return lexer.lex()


class _Lexer:
    def __init__(self, source: str, filename: str):
        # Normalize newlines for line tracking but keep indices simple.
        self.source = source.replace("\r\n", "\n").replace("\r", "\n")
        self.filename = filename
        self.i = 0
        self.line = 1
        self.column = 1
        self.length = len(self.source)

    def lex(self) -> List[Token]:
        tokens: List[Token] = []
        while True:
            self._skip_space_and_comments()
            if self.i >= self.length:
                tokens.append(Token(TokenKind.EOF, "", self._loc()))
                return tokens
            ch = self.source[self.i]
            if _is_ident_start(ch):
                tokens.append(self._ident_or_keyword())
            elif ch.isdigit() or (ch == "." and self._peek_digit()):
                tokens.append(self._number())
            elif ch == "'":
                tokens.append(self._char())
            elif ch == '"':
                tokens.append(self._string())
            else:
                punct = self._punctuator()
                if punct is None:
                    raise CompileError.at(self._loc(), f"unexpected character {ch!r}")
                tokens.append(punct)

    def _loc(self, line: Optional[int] = None, column: Optional[int] = None) -> SourceLocation:
        return SourceLocation(self.filename, line or self.line, column or self.column)

    def _peek(self, offset: int = 0) -> str:
        j = self.i + offset
        if j >= self.length:
            return ""
        return self.source[j]

    def _peek_digit(self) -> bool:
        return self._peek(1).isdigit()

    def _advance(self, count: int = 1) -> str:
        out = []
        for _ in range(count):
            if self.i >= self.length:
                break
            ch = self.source[self.i]
            self.i += 1
            out.append(ch)
            if ch == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
        return "".join(out)

    def _skip_space_and_comments(self) -> None:
        while self.i < self.length:
            ch = self.source[self.i]
            if ch in " \t\n\v\f":
                self._advance()
                continue
            if ch == "/" and self._peek(1) == "/":
                self._advance(2)
                while self.i < self.length and self.source[self.i] != "\n":
                    self._advance()
                continue
            if ch == "/" and self._peek(1) == "*":
                start = self._loc()
                self._advance(2)
                closed = False
                while self.i < self.length:
                    if self.source[self.i] == "*" and self._peek(1) == "/":
                        self._advance(2)
                        closed = True
                        break
                    self._advance()
                if not closed:
                    raise CompileError.at(start, "unterminated block comment")
                continue
            break

    def _ident_or_keyword(self) -> Token:
        start = self._loc()
        chars = [self._advance()]
        while self.i < self.length and _is_ident_continue(self.source[self.i]):
            chars.append(self._advance())
        text = "".join(chars)
        kind = TokenKind.KEYWORD if text in KEYWORDS else TokenKind.IDENT
        return Token(kind, text, start)

    def _number(self) -> Token:
        # Integer constants for Phase 1 (decimal, octal, hex). A leading '.'
        # followed by digit is reserved for floats and diagnosed clearly.
        start = self._loc()
        if self.source[self.i] == ".":
            raise CompileError.at(start, "floating-point constants are not supported")
        if self.source[self.i] == "0" and self._peek(1) in ("x", "X"):
            text = self._advance(2)
            if not _is_hex_digit(self._peek()):
                raise CompileError.at(start, "invalid hexadecimal constant")
            while self.i < self.length and (_is_hex_digit(self.source[self.i]) or self.source[self.i] == "'"):
                if self.source[self.i] != "'":
                    text += self.source[self.i]
                self._advance()
            base = 16
            digits = text[2:]
        elif self.source[self.i] == "0" and self._peek(1) in ("b", "B"):
            # Accept 0b/0B as an extension; value is still a C integer token.
            text = self._advance(2)
            if self._peek() not in ("0", "1"):
                raise CompileError.at(start, "invalid binary constant")
            while self.i < self.length and self.source[self.i] in ("0", "1", "'"):
                if self.source[self.i] != "'":
                    text += self.source[self.i]
                self._advance()
            base = 2
            digits = text[2:]
        elif self.source[self.i] == "0":
            text = self._advance()
            while self.i < self.length and (self.source[self.i] in "01234567'" ):
                if self.source[self.i] != "'":
                    text += self.source[self.i]
                self._advance()
            base = 8
            digits = text
        else:
            text_chars = []
            while self.i < self.length and (self.source[self.i].isdigit() or self.source[self.i] == "'"):
                if self.source[self.i] != "'":
                    text_chars.append(self.source[self.i])
                self._advance()
            text = "".join(text_chars)
            base = 10
            digits = text
        suffix = self._read_int_suffix()
        spelling = self.source[self._index_of(start):self.i] if False else None
        # Reconstruct spelling from consumed region is awkward with normalization;
        # keep a canonical spelling.
        full = ("0x" + digits if base == 16 else
                "0b" + digits if base == 2 else
                digits) + suffix
        try:
            number = int(digits, base)
        except ValueError as exc:
            raise CompileError.at(start, f"invalid integer constant: {exc}") from exc
        # Reject float suffixes/exponents encountered after an integer.
        if self.i < self.length and self.source[self.i] in ".eEpP":
            raise CompileError.at(start, "floating-point constants are not supported")
        return Token(TokenKind.INT, full, start, number=number)

    def _index_of(self, loc: SourceLocation) -> int:
        # Not used; retained only to avoid accidental attribute errors.
        return 0

    def _read_int_suffix(self) -> str:
        allowed = set("uUlL")
        suffix = []
        while self.i < self.length and self.source[self.i] in allowed:
            suffix.append(self._advance())
        # Validate suffix shape roughly: u, l, ll, ul, ull, lu, llu and case variants.
        s = "".join(suffix)
        canon = s.lower().replace("ll", "L").replace("l", "l").replace("u", "u")
        # Accept common forms only.
        valid = {"", "u", "l", "L", "ul", "uL", "lu", "Lu"}
        if canon not in valid:
            raise CompileError.at(self._loc(self.line, self.column - len(s) if len(s) <= self.column - 1 else 1),
                                  f"invalid integer suffix {s!r}")
        return s

    def _char(self) -> Token:
        start = self._loc()
        self._advance()  # opening '
        if self.i >= self.length or self.source[self.i] == "\n":
            raise CompileError.at(start, "unterminated character constant")
        if self.source[self.i] == "'":
            raise CompileError.at(start, "empty character constant")
        value, raw = self._read_c_char(stop="'", context="character constant", start=start)
        # Multi-char constants: consume remaining chars but keep first value as C does in common cases.
        while self.i < self.length and self.source[self.i] != "'":
            _, more = self._read_c_char(stop="'", context="character constant", start=start)
            raw += more
            # value remains implementation-defined; keep last/read accumulation simple.
            value = ((value << 8) | value) & 0xFFFFFFFFFFFFFFFF  # not meaningful; prefer first below
        if self.i >= self.length:
            raise CompileError.at(start, "unterminated character constant")
        self._advance()  # closing '
        # Use the first character's numeric value.
        first_value = self._first_char_value(raw, start)
        return Token(TokenKind.CHAR, "'" + raw + "'", start, number=first_value)

    def _first_char_value(self, raw: str, start: SourceLocation) -> int:
        # Decode just the first character/escape of a raw char payload.
        sub = _StringDecoder(raw, start)
        val = sub.read_one()
        return val

    def _string(self) -> Token:
        start = self._loc()
        self._advance()  # opening "
        chars: List[str] = []
        raw_parts: List[str] = []
        while self.i < self.length:
            ch = self.source[self.i]
            if ch == '"':
                self._advance()
                decoded = "".join(chars)
                return Token(TokenKind.STRING, '"' + "".join(raw_parts) + '"', start, string=decoded)
            if ch == "\n":
                raise CompileError.at(start, "unterminated string literal")
            val, raw = self._read_c_char(stop='"', context="string literal", start=start)
            raw_parts.append(raw)
            chars.append(chr(val & 0xFF))
        raise CompileError.at(start, "unterminated string literal")

    def _read_c_char(self, stop: str, context: str, start: SourceLocation):
        ch = self.source[self.i]
        if ch == "\\":
            raw_start = self.i
            self._advance()
            if self.i >= self.length:
                raise CompileError.at(start, f"unterminated escape in {context}")
            esc = self.source[self.i]
            if esc in _SIMPLE_ESCAPES:
                self._advance()
                return _SIMPLE_ESCAPES[esc], "\\" + esc
            if esc in "01234567":
                digits = []
                for _ in range(3):
                    if self.i < self.length and self.source[self.i] in "01234567":
                        digits.append(self._advance())
                    else:
                        break
                raw = "\\" + "".join(digits)
                return int("".join(digits), 8), raw
            if esc == "x":
                self._advance()
                hex_digits = []
                while self.i < self.length and _is_hex_digit(self.source[self.i]):
                    hex_digits.append(self._advance())
                if not hex_digits:
                    raise CompileError.at(start, f"invalid \\x escape in {context}")
                raw = "\\x" + "".join(hex_digits)
                return int("".join(hex_digits), 16), raw
            raise CompileError.at(start, f"unsupported escape \\{esc} in {context}")
        else:
            self._advance()
            return ord(ch), ch

    def _punctuator(self) -> Optional[Token]:
        start = self._loc()
        for punct in PUNCTUATORS:
            if self.source.startswith(punct, self.i):
                self._advance(len(punct))
                return Token(TokenKind.PUNCT, punct, start)
        return None


class _StringDecoder:
    """Helper to decode the first character from a raw char-literal payload."""

    def __init__(self, raw: str, start: SourceLocation):
        self.raw = raw
        self.i = 0
        self.start = start

    def read_one(self) -> int:
        if self.i >= len(self.raw):
            return 0
        ch = self.raw[self.i]
        if ch != "\\":
            self.i += 1
            return ord(ch)
        self.i += 1
        esc = self.raw[self.i]
        if esc in _SIMPLE_ESCAPES:
            self.i += 1
            return _SIMPLE_ESCAPES[esc]
        if esc in "01234567":
            digits = []
            for _ in range(3):
                if self.i < len(self.raw) and self.raw[self.i] in "01234567":
                    digits.append(self.raw[self.i])
                    self.i += 1
                else:
                    break
            return int("".join(digits), 8)
        if esc == "x":
            self.i += 1
            digits = []
            while self.i < len(self.raw) and _is_hex_digit(self.raw[self.i]):
                digits.append(self.raw[self.i])
                self.i += 1
            return int("".join(digits), 16)
        raise CompileError.at(self.start, f"unsupported escape \\{esc} in character constant")


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_continue(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _is_hex_digit(ch: str) -> bool:
    return len(ch) == 1 and ch in "0123456789abcdefABCDEF"
