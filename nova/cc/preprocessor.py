"""C preprocessor for :mod:`nova.cc`.

This module implements the Phase 1 preprocessing pipeline required by the Nova
C compiler front end: line splicing, directive handling, object/function-like
macro expansion (including ``#`` and ``##``), conditional compilation, include
resolution with cycle/guard handling, and source-location tracking.

The public API is designed for future compiler stages and for ``nova-cc -E``:
``preprocess(...)`` returns preprocessed text, while ``preprocess_to_tokens(...)``
returns a token stream suitable for the parser.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .diagnostics import CompileError, SourceLocation
from .lexer import Token, TokenKind, lex

MAX_INCLUDE_DEPTH = 200
MAX_EXPANSION_DEPTH = 256
MAX_COND_DEPTH = 256
_PLACEMARKER = "\x00PM\x00"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")
_INT_TOKEN_RE = re.compile(r"^(0[xX][0-9A-Fa-f]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)([uUlL]*)$")


@dataclass(frozen=True)
class Macro:
    name: str
    params: Optional[Tuple[str, ...]]
    replacement: Tuple[Token, ...]
    variadic: bool = False
    definition_location: Optional[SourceLocation] = None

    @property
    def is_function_like(self) -> bool:
        return self.params is not None


@dataclass
class _CondFrame:
    parent_active: bool
    taken: bool
    active: bool
    location: SourceLocation
    else_seen: bool = False


@dataclass
class PreprocessorOptions:
    include_paths: List[Path] = field(default_factory=list)
    system_include_paths: List[Path] = field(default_factory=list)
    defines: Dict[str, str] = field(default_factory=dict)
    undefines: List[str] = field(default_factory=list)
    language: str = "nova-c11"  # documented initial mode; see _install_builtin_macros


class Preprocessor:
    """Stateful C preprocessor."""

    def __init__(self, *, filename: str = "<stdin>", directory: Optional[Path] = None,
                 options: Optional[PreprocessorOptions] = None):
        self.options = options or PreprocessorOptions()
        self.macros: Dict[str, Macro] = {}
        self.cond_stack: List[_CondFrame] = []
        self.pragma_once: Set[str] = set()
        self.include_stack: List[str] = []
        self._install_builtin_macros()
        self._install_initial_defines()
        self._install_initial_undefines()
        # Default include context for top-level preprocessing.
        self._top_filename = filename
        self._top_directory = Path(directory) if directory is not None else Path.cwd()
        # Logical source mapping stack for #line: list of (physical_line_base, logical_line_base, logical_file)
        # managed per processed source in _process_source. Dynamic builtins use token locations.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preprocess(self, source: str, filename: Optional[str] = None,
                   directory: Optional[str | Path] = None) -> str:
        tokens = self.preprocess_to_tokens(source, filename=filename, directory=directory)
        return tokens_to_text(tokens)

    def preprocess_to_tokens(self, source: str, filename: Optional[str] = None,
                            directory: Optional[str | Path] = None) -> List[Token]:
        name = filename or self._top_filename
        base_dir = Path(directory) if directory is not None else (
            Path(name).resolve().parent if name not in ("<stdin>", "") and Path(name).exists() else self._top_directory
        )
        # If filename is a real path but directory not explicitly given, prefer its parent.
        if directory is None and name not in ("<stdin>", ""):
            try:
                base_dir = Path(name).resolve().parent
            except Exception:
                base_dir = self._top_directory
        out = self._process_source(source, name, base_dir, depth=0)
        if self.cond_stack:
            loc = self.cond_stack[-1].location
            raise CompileError.at(loc, "unterminated conditional directive")
        out.append(Token(TokenKind.EOF, "", SourceLocation(name, 1, 1)))
        return out

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _install_builtin_macros(self) -> None:
        # __FILE__ and __LINE__ are handled dynamically during expansion.
        # Built-in object-like macros depend on the selected documented language mode.
        lang = getattr(self.options, "language", "nova-c11") or "nova-c11"
        # Initial documented mode for Nova freestanding C frontend. We define
        # __STDC__ to indicate a conforming implementation mode, but do not claim
        # full ISO C library/hosted conformance. __STDC_VERSION__ is defined only
        # for modes that explicitly select a C standard level.
        if lang in ("nova-c11", "c11", "gnu11"):
            self._define_builtin_int("__STDC__", 1)
            self._define_builtin_int("__STDC_HOSTED__", 0)
            if lang == "c11":
                self._define_builtin_int("__STDC_VERSION__", 201112)
        elif lang in ("c99", "gnu99"):
            self._define_builtin_int("__STDC__", 1)
            self._define_builtin_int("__STDC_HOSTED__", 0)
            self._define_builtin_int("__STDC_VERSION__", 199901)
        elif lang in ("c89", "c90"):
            self._define_builtin_int("__STDC__", 1)
        # Other/custom modes may choose not to predefine these macros.

    def _define_builtin_int(self, name: str, value: int) -> None:
        loc = SourceLocation("<builtin>", 1, 1)
        tok = Token(TokenKind.INT, str(value), loc, number=value)
        self.macros[name] = Macro(name=name, params=None, replacement=(tok,), definition_location=loc)

    def _install_initial_defines(self) -> None:
        for name, value in self.options.defines.items():
            self._define_from_text(name, value, SourceLocation("<command-line>", 1, 1))

    def _install_initial_undefines(self) -> None:
        # -U applies after builtin and -D definitions, so it can remove either.
        for name in self.options.undefines:
            self.macros.pop(name, None)

    def _define_from_text(self, name: str, value: str, loc: SourceLocation) -> None:
        toks = _lex_fragment(value, loc.file, loc.line)
        self.macros[name] = Macro(name=name, params=None, replacement=tuple(toks), definition_location=loc)

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def _process_source(self, source: str, filename: str, directory: Path, depth: int) -> List[Token]:
        if depth > MAX_INCLUDE_DEPTH:
            raise CompileError.at(SourceLocation(filename, 1, 1), "maximum include depth exceeded")
        norm_name = _norm_path_key(filename, directory)
        if norm_name in self.include_stack:
            raise CompileError.at(SourceLocation(filename, 1, 1), f"include cycle detected: {filename}")
        if norm_name in self.pragma_once and depth > 0:
            return []
        self.include_stack.append(norm_name)
        try:
            logical_lines = splice_lines(source, filename)
            output: List[Token] = []
            # Logical location state for this source: can be changed by #line.
            logical_file = filename
            line_override: Optional[Tuple[int, int]] = None  # (physical_line, logical_line)
            for logical in logical_lines:
                line_text = logical.text
                phys_line_no = logical.start_phys_line
                logical_line_no = phys_line_no
                if line_override is not None:
                    base_phys, base_logical = line_override
                    logical_line_no = base_logical + (phys_line_no - base_phys)
                self._process_logical_line(line_text, logical_line_no, logical_file, directory, output, depth,
                                           phys_filename=filename, splice_segments=logical.segments)
                # If directive changed mapping, update state
                if hasattr(self, "_pending_line_override"):
                    override = getattr(self, "_pending_line_override")
                    delattr(self, "_pending_line_override")
                    if override is None:
                        line_override = None
                    else:
                        # override applies starting at next line
                        next_phys = phys_line_no + 1
                        if len(override) == 3:
                            line_override = (next_phys, override[0])
                            logical_file = override[1]
                        else:
                            line_override = (next_phys, override[0])
                            logical_file = override[1]
                if hasattr(self, "_pending_logical_file"):
                    logical_file = getattr(self, "_pending_logical_file")
                    delattr(self, "_pending_logical_file")
            return output
        finally:
            self.include_stack.pop()

    def _current_active(self) -> bool:
        return all(frame.active for frame in self.cond_stack)

    def _process_logical_line(self, line_text: str, line_no: int, filename: str,
                              directory: Path, output: List[Token], depth: int,
                              phys_filename: Optional[str] = None,
                              splice_segments: Optional[List[SpliceSegment]] = None) -> None:
        stripped_leading = line_text.lstrip(" \t\v\f")
        if stripped_leading.startswith("#"):
            self._handle_directive(line_text, line_no, filename, directory, output, depth,
                                   phys_filename=phys_filename, splice_segments=splice_segments)
            return
        if not self._current_active():
            return
        tokens = _lex_fragment(line_text, filename, line_no, splice_segments=splice_segments)
        if not tokens:
            return
        expanded = self._expand_tokens(tokens, set(), 0)
        output.extend(expanded)

    # ------------------------------------------------------------------
    # Directives
    # ------------------------------------------------------------------

    def _handle_directive(self, line_text: str, line_no: int, filename: str,
                          directory: Path, output: List[Token], depth: int,
                          phys_filename: Optional[str] = None,
                          splice_segments: Optional[List[SpliceSegment]] = None) -> None:
        # Locate '#' after leading whitespace in the logical line.
        idx = 0
        while idx < len(line_text) and line_text[idx] in " \t\v\f":
            idx += 1
        # idx points at '#'
        after_hash = idx + 1
        rest = line_text[after_hash:]
        loc = SourceLocation(filename, line_no, idx + 1)
        # Parse directive name and argument text.
        m = re.match(r"[ \t\v\f]*([A-Za-z_]\w*)?(.*)$", rest, re.DOTALL)
        assert m is not None
        name = m.group(1)
        arg_text = m.group(2) or ""
        # Null directive
        if name is None:
            if arg_text.strip(" \t\v\f"):
                raise CompileError.at(loc, "invalid preprocessing directive")
            return

        active = self._current_active()

        if name == "define":
            if active:
                self._directive_define(arg_text, loc)
            return
        if name == "undef":
            if active:
                self._directive_undef(arg_text, loc)
            return
        if name == "include":
            if active:
                self._directive_include(arg_text, loc, directory, output, depth)
            return
        if name == "ifdef":
            self._directive_ifdef(arg_text, loc, negated=False)
            return
        if name == "ifndef":
            self._directive_ifdef(arg_text, loc, negated=True)
            return
        if name == "if":
            self._directive_if(arg_text, loc)
            return
        if name == "elif":
            self._directive_elif(arg_text, loc)
            return
        if name == "else":
            self._directive_else(arg_text, loc)
            return
        if name == "endif":
            self._directive_endif(loc)
            return
        if name == "pragma":
            if active:
                self._directive_pragma(arg_text, loc, phys_filename or filename, directory)
            return
        if name == "error":
            if active:
                msg = arg_text.strip()
                raise CompileError.at(loc, msg or "#error")
            return
        if name == "line":
            if active:
                self._directive_line(arg_text, loc)
            return

        if active:
            raise CompileError.at(loc, f"unsupported preprocessing directive #{name}")
        # In inactive branch, unknown directives are ignored except structural ones handled above.

    def _directive_define(self, arg_text: str, loc: SourceLocation) -> None:
        # arg_text begins after 'define' keyword.
        s = arg_text.lstrip(" \t\v\f")
        if not s:
            raise CompileError.at(loc, "macro name missing in #define")
        m = re.match(r"([A-Za-z_]\w*)", s)
        if not m:
            raise CompileError.at(loc, "macro name missing in #define")
        name = m.group(1)
        rest = s[m.end():]
        params: Optional[Tuple[str, ...]] = None
        variadic = False
        # Function-like: '(' must immediately follow name (no whitespace) in the
        # replacement-list sense; but easier detect: if rest startswith '(' treat
        # as function-like parameter list. Standard requires no space before '('
        # for function-like macro; if space, it's object-like and '(' is part of body.
        if rest.startswith("("):
            params, variadic, rest = self._parse_macro_params(rest, loc)
            replacement_text = rest.lstrip(" \t\v\f")
        else:
            replacement_text = rest.lstrip(" \t\v\f")
        replacement = tuple(_lex_fragment(replacement_text, loc.file, loc.line))
        new_macro = Macro(name=name, params=params, replacement=replacement,
                          variadic=variadic, definition_location=loc)
        if name in self.macros:
            old = self.macros[name]
            if not _macros_identical(old, new_macro):
                raise CompileError.at(loc, f"redefinition of macro '{name}'")
            # Identical redefinition is allowed.
            return
        self.macros[name] = new_macro

    def _parse_macro_params(self, text: str, loc: SourceLocation) -> Tuple[Tuple[str, ...], bool, str]:
        # text starts at '('
        assert text.startswith("(")
        i = 1
        params: List[str] = []
        variadic = False
        length = len(text)
        while i < length:
            # skip whitespace
            while i < length and text[i] in " \t\v\f":
                i += 1
            if i >= length:
                raise CompileError.at(loc, "unterminated macro parameter list")
            if text[i] == ")":
                i += 1
                return tuple(params), variadic, text[i:]
            if text.startswith("...", i):
                variadic = True
                params.append("__VA_ARGS__")
                i += 3
                while i < length and text[i] in " \t\v\f":
                    i += 1
                if i >= length or text[i] != ")":
                    raise CompileError.at(loc, "expected ')' after variadic macro parameter")
                i += 1
                return tuple(params), variadic, text[i:]
            m = re.match(r"[A-Za-z_]\w*", text[i:])
            if not m:
                raise CompileError.at(loc, "invalid macro parameter")
            pname = m.group(0)
            if pname in params:
                raise CompileError.at(loc, f"duplicate macro parameter '{pname}'")
            params.append(pname)
            i += len(pname)
            while i < length and text[i] in " \t\v\f":
                i += 1
            if i < length and text[i] == ",":
                i += 1
                continue
            if i < length and text[i] == ")":
                i += 1
                return tuple(params), variadic, text[i:]
            raise CompileError.at(loc, "invalid macro parameter list")
        raise CompileError.at(loc, "unterminated macro parameter list")

    def _directive_undef(self, arg_text: str, loc: SourceLocation) -> None:
        toks = _lex_fragment(arg_text, loc.file, loc.line)
        if len(toks) != 1 or toks[0].kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
            # Some implementations allow keywords/idents; require exactly one name.
            name = arg_text.strip()
            if not _IDENTIFIER_RE.match(name):
                raise CompileError.at(loc, "macro name missing or invalid in #undef")
            self.macros.pop(name, None)
            return
        self.macros.pop(toks[0].value, None)

    def _directive_ifdef(self, arg_text: str, loc: SourceLocation, negated: bool) -> None:
        if len(self.cond_stack) >= MAX_COND_DEPTH:
            raise CompileError.at(loc, "conditional nesting too deep")
        parent_active = self._current_active()
        name = self._parse_single_macro_name(arg_text, loc, directive="ifdef" if not negated else "ifndef")
        cond = name in self.macros
        if negated:
            cond = not cond
        active = parent_active and cond
        self.cond_stack.append(_CondFrame(parent_active=parent_active, taken=cond and parent_active, active=active, location=loc))

    def _directive_if(self, arg_text: str, loc: SourceLocation) -> None:
        if len(self.cond_stack) >= MAX_COND_DEPTH:
            raise CompileError.at(loc, "conditional nesting too deep")
        parent_active = self._current_active()
        cond = False
        if parent_active:
            cond = self._eval_if_expression(arg_text, loc) != 0
        active = parent_active and cond
        self.cond_stack.append(_CondFrame(parent_active=parent_active, taken=cond and parent_active, active=active, location=loc))

    def _directive_elif(self, arg_text: str, loc: SourceLocation) -> None:
        if not self.cond_stack:
            raise CompileError.at(loc, "#elif without matching #if")
        frame = self.cond_stack[-1]
        if frame.else_seen:
            raise CompileError.at(loc, "#elif after #else")
        cond = False
        if frame.parent_active and not frame.taken:
            cond = self._eval_if_expression(arg_text, loc) != 0
        frame.active = frame.parent_active and (not frame.taken) and cond
        if frame.active:
            frame.taken = True
        else:
            # if already taken, remain inactive; if parent inactive, remain inactive
            frame.active = False

    def _directive_else(self, arg_text: str, loc: SourceLocation) -> None:
        if not self.cond_stack:
            raise CompileError.at(loc, "#else without matching #if")
        frame = self.cond_stack[-1]
        if frame.else_seen:
            raise CompileError.at(loc, "duplicate #else")
        if arg_text.strip():
            raise CompileError.at(loc, "unexpected tokens after #else")
        frame.active = frame.parent_active and not frame.taken
        frame.taken = True
        frame.else_seen = True

    def _directive_endif(self, loc: SourceLocation) -> None:
        if not self.cond_stack:
            raise CompileError.at(loc, "#endif without matching #if")
        self.cond_stack.pop()

    def _directive_pragma(self, arg_text: str, loc: SourceLocation, filename: str, directory: Path) -> None:
        toks = _lex_fragment(arg_text, loc.file, loc.line)
        if len(toks) == 1 and toks[0].value == "once":
            # pragma once applies to the physical file identity
            self.pragma_once.add(_norm_path_key(filename, directory))
            return
        # Unknown pragmas are ignored in this phase.

    def _directive_line(self, arg_text: str, loc: SourceLocation) -> None:
        toks = _lex_fragment(arg_text, loc.file, loc.line)
        # Allow macro-expanded #line forms? preprocess arg tokens except in some cases;
        # simple approach: expand non-directive macros in argument where appropriate.
        # For now, try expanding if first token not INT/STRING forms that are obvious.
        if toks and not (toks[0].kind == TokenKind.INT):
            # attempt expansion
            expanded = self._expand_tokens(toks, set(), 0)
            if expanded:
                toks = expanded
        if not toks:
            raise CompileError.at(loc, "invalid #line directive")
        if toks[0].kind != TokenKind.INT or toks[0].number is None:
            raise CompileError.at(loc, "line number expected in #line directive")
        line_no = int(toks[0].number)
        if line_no < 1:
            raise CompileError.at(loc, "line number in #line directive must be positive")
        new_file = loc.file
        idx = 1
        if idx < len(toks):
            if toks[idx].kind != TokenKind.STRING:
                # GNU may have extra flags; tolerate trailing integer flags after filename.
                raise CompileError.at(loc, "filename or end of line expected in #line directive")
            new_file = toks[idx].string or ""
            idx += 1
        # Ignore trailing flags (common extension) if they are integers/identifiers.
        if idx < len(toks):
            for t in toks[idx:]:
                if t.kind not in (TokenKind.INT, TokenKind.IDENT, TokenKind.KEYWORD):
                    raise CompileError.at(loc, "unexpected token in #line directive")
        # Set override to apply starting at next logical line.
        self._pending_line_override = (line_no, new_file)

    def _directive_include(self, arg_text: str, loc: SourceLocation, directory: Path,
                           output: List[Token], depth: int) -> None:
        name, is_system = self._parse_include_name(arg_text, loc)
        resolved = self._resolve_include(name, is_system, directory)
        if resolved is None:
            raise CompileError.at(loc, f"include file not found: {name}")
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            raise CompileError.at(loc, f"cannot read include file '{name}': {exc}") from exc
        included = self._process_source(text, str(resolved), resolved.parent, depth + 1)
        output.extend(included)

    def _parse_include_name(self, arg_text: str, loc: SourceLocation) -> Tuple[str, bool]:
        s = arg_text.lstrip(" \t\v\f")
        # Direct header-name forms
        if s.startswith('"'):
            end = s.find('"', 1)
            if end == -1:
                raise CompileError.at(loc, "unterminated include file name")
            name = s[1:end]
            tail = s[end + 1:].strip()
            if tail:
                raise CompileError.at(loc, "unexpected tokens after #include")
            return name, False
        if s.startswith("<"):
            end = s.find(">", 1)
            if end == -1:
                raise CompileError.at(loc, "unterminated include file name")
            name = s[1:end]
            tail = s[end + 1:].strip()
            if tail:
                raise CompileError.at(loc, "unexpected tokens after #include")
            return name, True
        # Computed include: expand macros and reconstruct header names from token spellings.
        toks = _lex_fragment(s, loc.file, loc.line)
        if not toks:
            raise CompileError.at(loc, "empty #include directive")
        expanded = self._expand_tokens(toks, set(), 0)
        if len(expanded) == 1 and expanded[0].kind == TokenKind.STRING:
            return expanded[0].string or "", False
        # Reconstruct < ... > form. Header-name tokens include punctuation like '.' and '/'
        # which the ordinary lexer represents as separate tokens; joining spellings is
        # deterministic and handles nested macro expansion and whitespace variants.
        if expanded[0].value == "<":
            close = next((i for i, t in enumerate(expanded) if t.value == ">"), None)
            if close is None:
                raise CompileError.at(loc, "unterminated include file name")
            if any(t.kind == TokenKind.STRING for t in expanded[1:close]):
                raise CompileError.at(loc, "invalid token in include file name")
            if close + 1 != len(expanded):
                raise CompileError.at(loc, "unexpected tokens after #include")
            name = "".join(t.value for t in expanded[1:close])
            if not name:
                raise CompileError.at(loc, "empty include file name")
            return name, True
        raise CompileError.at(loc, "invalid #include directive")

    def _resolve_include(self, name: str, is_system: bool, directory: Path) -> Optional[Path]:
        # Deterministic resolution; reject absolute and parent-traversal escapes in name.
        if os.path.isabs(name):
            candidate = Path(name)
            return candidate.resolve() if candidate.exists() else None
        norm_name = Path(name)
        if any(part == ".." for part in norm_name.parts):
            # Disallow uncontrolled traversal via include name.
            return None
        search: List[Path] = []
        if not is_system:
            search.append(directory)
        search.extend(self.options.include_paths)
        search.extend(self.options.system_include_paths)
        if is_system:
            # Also allow local paths? No, quoted includes already searched local.
            pass
        else:
            # Quoted includes: local first, then include paths (already added).
            pass
        for base in search:
            try:
                candidate = (base / norm_name).resolve()
                base_res = base.resolve()
            except Exception:
                continue
            # Ensure candidate is within base to avoid traversal through symlinks etc. best-effort.
            try:
                candidate.relative_to(base_res)
            except Exception:
                # Allow it only if it's exactly a normal joined path; but traversal was filtered.
                pass
            if candidate.is_file():
                return candidate
        return None

    def _parse_single_macro_name(self, arg_text: str, loc: SourceLocation, directive: str) -> str:
        toks = _lex_fragment(arg_text, loc.file, loc.line)
        if len(toks) != 1 or toks[0].kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
            raise CompileError.at(loc, f"macro name expected in #{directive}")
        return toks[0].value

    # ------------------------------------------------------------------
    # Macro expansion
    # ------------------------------------------------------------------

    def _expand_tokens(self, tokens: Sequence[Token], disabled: Optional[Set[str]] = None, depth: int = 0) -> List[Token]:
        """Fully macro-expand a token sequence.

        Uses a hide-set (blue-paint) algorithm so that macros are not re-expanded
        recursively and already-disabled names remain disabled during argument
        prescan and rescanning. Rescanned replacement tokens are reinserted ahead
        of the remaining input so function-like invocations formed across expansion
        boundaries are recognized.
        """
        # Internal stream elements are (Token, hide_frozenset)
        stream: List[Tuple[Token, frozenset]] = [(t, frozenset(disabled or ())) for t in tokens]
        output: List[Token] = []
        steps = 0
        while stream:
            steps += 1
            if steps > 1000000:  # safety valve
                raise CompileError.at(stream[0][0].location, "macro expansion did not terminate")
            tok, hide = stream.pop(0)
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
                # __FILE__ / __LINE__ are dynamic built-ins
                if tok.value == "__FILE__":
                    s = tok.location.file
                    output.append(Token(TokenKind.STRING, '"' + _escape_c_string(s) + '"', tok.location, string=s))
                    continue
                if tok.value == "__LINE__":
                    val = tok.location.line
                    output.append(Token(TokenKind.INT, str(val), tok.location, number=val))
                    continue
                if tok.value == "_Pragma":
                    # _Pragma operator (C99): _Pragma("...") acts like a #pragma directive.
                    if stream and stream[0][0].value == "(":
                        prag = self._collect_pragma_operand(stream, tok.location)
                        # Apply pragma at expansion point using token location file.
                        self._directive_pragma(" " + prag, tok.location, tok.location.file, Path(tok.location.file).parent if tok.location.file not in ("<stdin>", "<builtin>") else Path.cwd())
                        continue
                macro = self.macros.get(tok.value)
                if macro is None or tok.value in hide:
                    output.append(tok)
                    continue
                if macro.is_function_like:
                    # Only expand if next token in the (possibly rescanned) stream is '('
                    if not stream or stream[0][0].value != "(":
                        output.append(tok)
                        continue
                    args, consumed = self._collect_macro_args_stream(stream, macro, tok.location)
                    # Prescan arguments with the invocation hide set (excluding macro name itself)
                    prescanned_args = [self._prescan_argument(arg, hide) for arg in args]
                    replaced = self._substitute_macro(macro, args, prescanned_args, tok.location)
                    # Perform ## in substituted replacement before rescanning
                    replaced = self._apply_token_pasting(replaced, tok.location)
                    new_hide = hide | frozenset({macro.name})
                    tagged = [(self._retag_token(t, new_hide), new_hide) for t in replaced]
                    # Rescan by prepending; remaining stream already had '('..')' consumed
                    stream = tagged + stream[consumed:]
                    continue
                else:
                    replaced = [_relocate_token(t, tok.location) for t in macro.replacement]
                    replaced = self._apply_token_pasting(replaced, tok.location)
                    new_hide = hide | frozenset({macro.name})
                    tagged = [(self._retag_token(t, new_hide), new_hide) for t in replaced]
                    stream = tagged + stream
                    continue
            else:
                output.append(tok)
        return output

    def _prescan_argument(self, arg_tokens: List[Token], hide: frozenset) -> List[Token]:
        if not arg_tokens:
            return []
        # Fully macro-expand argument tokens as if they formed the rest of the file,
        # but in the hide-set context of the invocation site.
        # Use a fresh expansion context carrying the invocation hide set.
        stream: List[Tuple[Token, frozenset]] = [(t, hide) for t in arg_tokens]
        # Reuse the same expansion engine logic by calling a helper on the stream.
        return self._expand_stream(stream)

    def _expand_stream(self, stream: List[Tuple[Token, frozenset]]) -> List[Token]:
        output: List[Token] = []
        steps = 0
        while stream:
            steps += 1
            if steps > 1000000:
                raise CompileError.at(stream[0][0].location, "macro expansion did not terminate")
            tok, hide = stream.pop(0)
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
                if tok.value == "__FILE__":
                    s = tok.location.file
                    output.append(Token(TokenKind.STRING, '"' + _escape_c_string(s) + '"', tok.location, string=s))
                    continue
                if tok.value == "__LINE__":
                    val = tok.location.line
                    output.append(Token(TokenKind.INT, str(val), tok.location, number=val))
                    continue
                if tok.value == "_Pragma":
                    if stream and stream[0][0].value == "(":
                        prag = self._collect_pragma_operand(stream, tok.location)
                        self._directive_pragma(" " + prag, tok.location, tok.location.file, Path(tok.location.file).parent if tok.location.file not in ("<stdin>", "<builtin>") else Path.cwd())
                        continue
                macro = self.macros.get(tok.value)
                if macro is None or tok.value in hide:
                    output.append(tok)
                    continue
                if macro.is_function_like:
                    if not stream or stream[0][0].value != "(":
                        output.append(tok)
                        continue
                    args, consumed = self._collect_macro_args_stream(stream, macro, tok.location)
                    prescanned_args = [self._prescan_argument(arg, hide) for arg in args]
                    replaced = self._substitute_macro(macro, args, prescanned_args, tok.location)
                    replaced = self._apply_token_pasting(replaced, tok.location)
                    new_hide = hide | frozenset({macro.name})
                    tagged = [(self._retag_token(t, new_hide), new_hide) for t in replaced]
                    stream = tagged + stream[consumed:]
                    continue
                else:
                    replaced = [_relocate_token(t, tok.location) for t in macro.replacement]
                    replaced = self._apply_token_pasting(replaced, tok.location)
                    new_hide = hide | frozenset({macro.name})
                    tagged = [(self._retag_token(t, new_hide), new_hide) for t in replaced]
                    stream = tagged + stream
                    continue
            else:
                output.append(tok)
        return output

    def _collect_pragma_operand(self, stream: List[Tuple[Token, frozenset]], loc: SourceLocation) -> str:
        """Consume a _Pragma("...") operand from stream and return its decoded payload."""
        if not stream or stream[0][0].value != "(":
            raise CompileError.at(loc, "expected '(' after '_Pragma'")
        stream.pop(0)  # '('
        parts: List[str] = []
        while stream:
            tok = stream[0][0]
            if tok.value == ")":
                stream.pop(0)
                if not parts:
                    raise CompileError.at(loc, "_Pragma requires a string literal operand")
                return "".join(parts)
            if tok.kind != TokenKind.STRING:
                raise CompileError.at(tok.location, "_Pragma operand must be a string literal")
            parts.append(tok.string or "")
            stream.pop(0)
        raise CompileError.at(loc, "unterminated _Pragma operator")

    def _retag_token(self, tok: Token, hide: frozenset) -> Token:
        # Hide set is tracked in the stream, not on Token objects for API compatibility.
        return tok

    @staticmethod
    def _retag_token_static(tok: Token, hide: frozenset) -> Token:
        return tok

    def _collect_macro_args_stream(self, stream: List[Tuple[Token, frozenset]],
                                   macro: Macro, loc: SourceLocation) -> Tuple[List[List[Token]], int]:
        # stream[0] is expected to be '('
        if not stream or stream[0][0].value != "(":
            raise CompileError.at(loc, f"expected '(' in macro invocation of '{macro.name}'")
        i = 1
        n = len(stream)
        args: List[List[Token]] = []
        current: List[Token] = []
        paren_depth = 0
        # Handle zero-arg invocation: ()
        if i < n and stream[i][0].value == ")":
            consumed = i + 1
            if len(macro.params or ()) == 0:
                return [], consumed
            if macro.variadic and len(macro.params or ()) == 1:
                return [[]], consumed
            raise CompileError.at(loc, f"macro argument count mismatch for '{macro.name}'")
        while i < n:
            tok = stream[i][0]
            if tok.value == "(":
                paren_depth += 1
                current.append(tok)
                i += 1
                continue
            if tok.value == ")" and paren_depth == 0:
                args.append(current)
                self._validate_arg_count(macro, args, loc)
                # Ensure varargs placeholder exists when needed
                params = macro.params or ()
                if macro.variadic:
                    fixed = len(params) - 1
                    if len(args) == fixed:
                        args.append([])
                return args, i + 1
            if tok.value == ")" and paren_depth > 0:
                paren_depth -= 1
                current.append(tok)
                i += 1
                continue
            if tok.value == "," and paren_depth == 0:
                if macro.variadic and len(args) == len(macro.params or ()) - 1:
                    # comma belongs to variadic argument
                    current.append(tok)
                else:
                    args.append(current)
                    current = []
                i += 1
                continue
            current.append(tok)
            i += 1
        raise CompileError.at(loc, f"unterminated macro invocation of '{macro.name}'")

    def _validate_arg_count(self, macro: Macro, args: List[List[Token]], loc: SourceLocation) -> None:
        params = macro.params or ()
        if macro.variadic:
            fixed = len(params) - 1
            if len(args) < fixed:
                raise CompileError.at(loc, f"macro argument count mismatch for '{macro.name}'")
            if len(args) == fixed:
                args.append([])
            return
        if len(args) != len(params):
            raise CompileError.at(loc, f"macro argument count mismatch for '{macro.name}'")

    def _substitute_macro(self, macro: Macro, raw_args: List[List[Token]],
                          prescanned_args: List[List[Token]], invoke_loc: SourceLocation) -> List[Token]:
        params = list(macro.params or ())
        raw_map: Dict[str, List[Token]] = {}
        expanded_map: Dict[str, List[Token]] = {}
        for idx, pname in enumerate(params):
            raw_map[pname] = list(raw_args[idx]) if idx < len(raw_args) else []
            expanded_map[pname] = list(prescanned_args[idx]) if idx < len(prescanned_args) else []
        repl = list(macro.replacement)
        out: List[Token] = []
        i = 0
        n = len(repl)
        while i < n:
            tok = repl[i]
            # Stringification: # param uses raw (unexpanded) argument tokens.
            if tok.value == "#" and macro.is_function_like:
                if i + 1 < n and repl[i + 1].value in raw_map:
                    escaped, decoded = _stringize_tokens(raw_map[repl[i + 1].value])
                    out.append(Token(TokenKind.STRING, '"' + escaped + '"', invoke_loc, string=decoded))
                    i += 2
                    continue
                raise CompileError.at(invoke_loc, "'#' is not followed by a macro parameter")

            # GNU extension: , ## __VA_ARGS__ (or named variadic parameter).
            # If varargs is empty, elide comma and ##. If non-empty, keep comma and
            # insert varargs tokens (do not token-paste comma with first vararg token).
            if (tok.value == "," and i + 2 < n and repl[i + 1].value == "##"
                    and macro.is_function_like and macro.variadic):
                va_name = repl[i + 2].value
                if va_name in raw_map and va_name == params[-1]:
                    va_raw = raw_map[va_name]
                    if len(va_raw) == 0:
                        i += 3
                        continue
                    else:
                        out.append(_relocate_token(tok, invoke_loc))  # comma
                        out.extend(_relocate_tokens(va_raw, invoke_loc))
                        i += 3
                        continue

            if tok.value in raw_map and macro.is_function_like:
                adjacent_paste = (i > 0 and repl[i - 1].value == "##") or (i + 1 < n and repl[i + 1].value == "##")
                if adjacent_paste:
                    raw = raw_map[tok.value]
                    if len(raw) == 0:
                        out.append(Token(TokenKind.PUNCT, _PLACEMARKER, invoke_loc))
                    else:
                        out.extend(_relocate_tokens(raw, invoke_loc))
                else:
                    out.extend(_relocate_tokens(expanded_map[tok.value], invoke_loc))
                i += 1
                continue
            out.append(_relocate_token(tok, invoke_loc))
            i += 1
        return out

    def _apply_token_pasting(self, tokens: List[Token], loc: Optional[SourceLocation] = None) -> List[Token]:
        """Apply ## in a substituted replacement list before rescanning."""
        def is_pm(t: Token) -> bool:
            return t.value == _PLACEMARKER

        out = list(tokens)
        i = 0
        while i < len(out):
            # Look for 'left ## right' starting at i
            if i + 1 < len(out) and out[i + 1].value == "##":
                if i + 2 >= len(out):
                    lloc = out[i + 1].location if i + 1 < len(out) else (loc or (out[i].location if out else SourceLocation()))
                    raise CompileError.at(lloc, "'##' cannot appear at end of macro expansion")
                left = out[i]
                right = out[i + 2]
                if is_pm(left) and is_pm(right):
                    result = left
                elif is_pm(left):
                    result = right
                elif is_pm(right):
                    result = left
                else:
                    pasted_text = left.value + right.value
                    try:
                        pasted_toks = _lex_fragment(pasted_text, left.location.file, left.location.line)
                    except CompileError:
                        pasted_toks = []
                    if len(pasted_toks) != 1:
                        raise CompileError.at(out[i + 1].location, f"token pasting produced an invalid token '{pasted_text}'")
                    pt = pasted_toks[0]
                    result = Token(pt.kind, pt.value, left.location, number=pt.number, string=pt.string, leading_space=left.leading_space)
                # Replace left,##,right with result and recheck at i for chained ##.
                out[i:i + 3] = [result]
                continue
            if out[i].value == "##":
                raise CompileError.at(out[i].location, "'##' cannot appear here in macro expansion")
            i += 1
        # Remove any placemarkers that remain (not adjacent to ##)
        out = [t for t in out if not is_pm(t)]
        return out

    # ------------------------------------------------------------------
    # #if expression evaluation
    # ------------------------------------------------------------------

    def _eval_if_expression(self, expr_text: str, loc: SourceLocation) -> int:
        toks = _lex_fragment(expr_text, loc.file, loc.line)
        if not toks:
            raise CompileError.at(loc, "expected expression in conditional directive")
        toks = self._expand_if_tokens(toks, loc)
        parser = _IfExprParser(toks, loc)
        value = parser.parse_expression()
        if parser.peek().kind != TokenKind.EOF:
            raise CompileError.at(parser.peek().location, "unexpected token in conditional expression")
        return 1 if value else 0 if isinstance(value, bool) else int(value)

    def _expand_if_tokens(self, tokens: Sequence[Token], loc: SourceLocation) -> List[Token]:
        # Process 'defined', then macro-expand, then convert remaining identifiers to 0.
        out: List[Token] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and tok.value == "defined":
                j = i + 1
                has_paren = False
                if j < n and tokens[j].value == "(":
                    has_paren = True
                    j += 1
                if j >= n or tokens[j].kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise CompileError.at(tok.location, "expected macro name after 'defined'")
                name = tokens[j].value
                j += 1
                if has_paren:
                    if j >= n or tokens[j].value != ")":
                        raise CompileError.at(tok.location, "expected ')' after 'defined'")
                    j += 1
                val = 1 if name in self.macros else 0
                out.append(Token(TokenKind.INT, str(val), tok.location, number=val))
                i = j
                continue
            out.append(tok)
            i += 1
        try:
            expanded = self._expand_tokens(out, set(), 0)
        except CompileError:
            expanded = out
        final: List[Token] = []
        for t in expanded:
            if t.kind == TokenKind.IDENT:
                final.append(Token(TokenKind.INT, "0", t.location, number=0))
            elif t.kind == TokenKind.KEYWORD:
                final.append(Token(TokenKind.INT, "0", t.location, number=0))
            else:
                final.append(t)
        return final

class _IfExprParser:
    """Integer constant expression parser for #if/#elif."""

    def __init__(self, tokens: Sequence[Token], loc: SourceLocation):
        self.tokens = list(tokens)
        if not self.tokens or self.tokens[-1].kind != TokenKind.EOF:
            end_loc = self.tokens[-1].location if self.tokens else loc
            self.tokens.append(Token(TokenKind.EOF, "", end_loc))
        self.i = 0
        self.loc = loc

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != TokenKind.EOF:
            self.i += 1
        return tok

    def match(self, *values: str) -> bool:
        if self.peek().value in values:
            self.take()
            return True
        return False

    def parse_expression(self) -> int:
        return self.parse_conditional()

    def parse_conditional(self) -> int:
        cond = self.parse_logical_or()
        if self.match("?"):
            then_v = self.parse_expression()
            if not self.match(":"):
                raise CompileError.at(self.peek().location, "expected ':' in conditional expression")
            else_v = self.parse_conditional()
            return then_v if cond != 0 else else_v
        return cond

    def parse_logical_or(self) -> int:
        val = self.parse_logical_and()
        while self.match("||"):
            rhs = self.parse_logical_and()
            val = 1 if (val != 0 or rhs != 0) else 0
        return val

    def parse_logical_and(self) -> int:
        val = self.parse_bit_or()
        while self.match("&&"):
            rhs = self.parse_bit_or()
            val = 1 if (val != 0 and rhs != 0) else 0
        return val

    def parse_bit_or(self) -> int:
        val = self.parse_bit_xor()
        while self.match("|"):
            rhs = self.parse_bit_xor()
            val = _to_int64(int(val) | int(rhs))
        return val

    def parse_bit_xor(self) -> int:
        val = self.parse_bit_and()
        while self.match("^"):
            rhs = self.parse_bit_and()
            val = _to_int64(int(val) ^ int(rhs))
        return val

    def parse_bit_and(self) -> int:
        val = self.parse_equality()
        while self.match("&"):
            rhs = self.parse_equality()
            val = _to_int64(int(val) & int(rhs))
        return val

    def parse_equality(self) -> int:
        val = self.parse_relational()
        while True:
            if self.match("=="):
                val = 1 if int(val) == int(self.parse_relational()) else 0
            elif self.match("!="):
                val = 1 if int(val) != int(self.parse_relational()) else 0
            else:
                break
        return val

    def parse_relational(self) -> int:
        val = self.parse_shift()
        while True:
            if self.match("<="):
                val = 1 if int(val) <= int(self.parse_shift()) else 0
            elif self.match(">="):
                val = 1 if int(val) >= int(self.parse_shift()) else 0
            elif self.match("<"):
                val = 1 if int(val) < int(self.parse_shift()) else 0
            elif self.match(">"):
                val = 1 if int(val) > int(self.parse_shift()) else 0
            else:
                break
        return val

    def parse_shift(self) -> int:
        val = self.parse_additive()
        while True:
            if self.match("<<"):
                rhs = int(self.parse_additive())
                val = _to_int64(int(val) << (rhs & 63))
            elif self.match(">>"):
                rhs = int(self.parse_additive())
                val = _to_int64(int(val) >> (rhs & 63))
            else:
                break
        return val

    def parse_additive(self) -> int:
        val = self.parse_multiplicative()
        while True:
            if self.match("+"):
                val = _to_int64(int(val) + int(self.parse_multiplicative()))
            elif self.match("-"):
                val = _to_int64(int(val) - int(self.parse_multiplicative()))
            else:
                break
        return val

    def parse_multiplicative(self) -> int:
        val = self.parse_unary()
        while True:
            if self.match("*"):
                val = _to_int64(int(val) * int(self.parse_unary()))
            elif self.match("/"):
                rhs = int(self.parse_unary())
                if rhs == 0:
                    raise CompileError.at(self.peek().location, "division by zero in conditional expression")
                val = _to_int64(int(int(val) / rhs))
            elif self.match("%"):
                rhs = int(self.parse_unary())
                if rhs == 0:
                    raise CompileError.at(self.peek().location, "division by zero in conditional expression")
                val = _to_int64(int(val) % rhs)
            else:
                break
        return val

    def parse_unary(self) -> int:
        if self.match("+"):
            return +self.parse_unary()
        if self.match("-"):
            return _to_int64(-int(self.parse_unary()))
        if self.match("!"):
            return 0 if int(self.parse_unary()) != 0 else 1
        if self.match("~"):
            return _to_int64(~int(self.parse_unary()))
        return self.parse_primary()

    def parse_primary(self) -> int:
        tok = self.take()
        if tok.value == "(":
            val = self.parse_expression()
            if not self.match(")"):
                raise CompileError.at(self.peek().location, "expected ')' in conditional expression")
            return val
        if tok.kind == TokenKind.INT:
            if tok.number is None:
                raise CompileError.at(tok.location, "invalid integer constant in conditional expression")
            return int(tok.number)
        if tok.kind == TokenKind.CHAR:
            return int(tok.number or 0)
        raise CompileError.at(tok.location, "invalid token in conditional expression")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _to_int64(value: int) -> int:
    value &= (1 << 64) - 1
    if value & (1 << 63):
        value -= (1 << 64)
    return value


@dataclass
class SpliceSegment:
    phys_line: int
    phys_col_start: int  # 1-based column in physical line corresponding to start of this segment in logical text
    logical_start: int   # 0-based index in logical line text


@dataclass
class LogicalLine:
    text: str
    start_phys_line: int
    segments: List[SpliceSegment]


def splice_lines(source: str, filename: str) -> List[LogicalLine]:
    """Perform backslash-newline splicing.

    Returns logical lines with segment mapping so token columns can be mapped to
    physical source locations when a logical line spans multiple physical lines.
    Logical line text does not include the terminating newline.
    """
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    result: List[LogicalLine] = []
    i = 0
    phys = 1
    n = len(lines)
    while i < n:
        start_phys = phys
        parts: List[str] = []
        segments: List[SpliceSegment] = []
        cur = i
        logical_pos = 0
        # first physical segment
        first_line = lines[cur]
        if cur < n and first_line.endswith("\\"):
            seg_text = first_line[:-1]
        else:
            seg_text = first_line
        parts.append(seg_text)
        segments.append(SpliceSegment(phys_line=start_phys, phys_col_start=1, logical_start=logical_pos))
        logical_pos += len(seg_text)
        # splice while line ends with backslash
        while cur < n and lines[cur].endswith("\\"):
            cur += 1
            if cur < n:
                next_phys = start_phys + (cur - i)
                next_line = lines[cur]
                if next_line.endswith("\\"):
                    seg_text = next_line[:-1]
                else:
                    seg_text = next_line
                parts.append(seg_text)
                segments.append(SpliceSegment(phys_line=next_phys, phys_col_start=1, logical_start=logical_pos))
                logical_pos += len(seg_text)
        logical = "".join(parts)
        result.append(LogicalLine(text=logical, start_phys_line=start_phys, segments=segments))
        consumed = cur - i + 1
        i = cur + 1
        phys = start_phys + consumed
    return result


def _map_splice_location(segments: Optional[List[SpliceSegment]], logical_col: int, fallback_line: int) -> Tuple[int, int]:
    """Map a 1-based logical column in a spliced logical line to (physical_line, physical_col)."""
    if not segments:
        return fallback_line, logical_col
    idx = max(logical_col - 1, 0)
    seg = segments[0]
    for s in segments:
        if s.logical_start <= idx:
            seg = s
        else:
            break
    phys_col = seg.phys_col_start + (idx - seg.logical_start)
    return seg.phys_line, phys_col


def _lex_fragment(text: str, filename: str, line: int,
                  splice_segments: Optional[List[SpliceSegment]] = None) -> List[Token]:
    if text == "":
        return []
    toks = lex(text, filename)
    # Drop EOF and relocate to requested logical line. When splice segment mapping
    # is available, preserve physical line/column across splices for diagnostics.
    out: List[Token] = []
    for t in toks:
        if t.kind == TokenKind.EOF:
            continue
        if splice_segments and len(splice_segments) > 1:
            phys_line, phys_col = _map_splice_location(splice_segments, t.location.column, line)
            loc = SourceLocation(filename, phys_line, phys_col)
        else:
            # For non-spliced lines, keep logical line number provided by caller and
            # lexical column from the fragment.
            loc = SourceLocation(filename, line, t.location.column)
        out.append(Token(t.kind, t.value, loc,
                         number=t.number, string=t.string, leading_space=t.leading_space))
    return out


def _relocate_token(tok: Token, loc: SourceLocation) -> Token:
    # Preserve macro invocation location for diagnostics/expansion context; keep column 1 as coarse info.
    return Token(tok.kind, tok.value, SourceLocation(loc.file, loc.line, tok.location.column if tok.location.file == loc.file else 1),
                 number=tok.number, string=tok.string, leading_space=tok.leading_space)


def _relocate_tokens(tokens: Sequence[Token], loc: SourceLocation) -> List[Token]:
    return [_relocate_token(t, loc) for t in tokens]


def _escape_c_string(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\0":
            out.append("\\0")
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append("\\x{:02x}".format(o & 0xFF))
    return "".join(out)


def _stringize_tokens(tokens: Sequence[Token]) -> Tuple[str, str]:
    """Stringize argument tokens per C rules.

    Returns (escaped_content, decoded_content):
      - escaped_content: suitable for embedding inside a C string literal token
        (quotes and backslashes escaped as \\\" and \\\\).
      - decoded_content: the actual string value (what the program would see).
    """
    if not tokens:
        return "", ""
    escaped_parts: List[str] = []
    decoded_parts: List[str] = []
    for i, tok in enumerate(tokens):
        space = " " if (i > 0 and getattr(tok, "leading_space", False)) else ""
        if space:
            escaped_parts.append(" ")
            decoded_parts.append(" ")
        spelling = tok.value
        if tok.kind == TokenKind.STRING:
            # Argument token spelling includes surrounding quotes, e.g. "hi".
            # In the stringized result, the quote characters and any backslashes
            # in the token must be escaped.
            escaped_parts.append(spelling.replace("\\", "\\\\").replace('"', '\\"'))
            decoded_parts.append(spelling)
        elif tok.kind == TokenKind.CHAR:
            escaped_parts.append(spelling.replace("\\", "\\\\").replace('"', '\\"'))
            decoded_parts.append(spelling)
        else:
            # Identifiers, keywords, numbers, punctuators: no " or \ in spellings.
            escaped_parts.append(spelling)
            decoded_parts.append(spelling)
    return "".join(escaped_parts), "".join(decoded_parts)


def _macros_identical(a: Macro, b: Macro) -> bool:
    if a.name != b.name or a.params != b.params or a.variadic != b.variadic:
        return False
    if len(a.replacement) != len(b.replacement):
        return False
    for x, y in zip(a.replacement, b.replacement):
        if x.kind != y.kind or x.value != y.value or x.number != y.number or x.string != y.string:
            return False
    return True


def _norm_path_key(filename: str, directory: Path) -> str:
    try:
        p = Path(filename)
        if not p.is_absolute():
            p = directory / p
        return str(p.resolve())
    except Exception:
        return os.path.normcase(os.path.normpath(filename))


def tokens_to_text(tokens: Sequence[Token]) -> str:
    """Render tokens as C-like source text (suitable for ``-E`` style output)."""
    parts: List[str] = []
    prev: Optional[Token] = None
    for tok in tokens:
        if tok.kind == TokenKind.EOF:
            break
        if prev is not None and _needs_space(prev, tok):
            parts.append(" ")
        parts.append(tok.value)
        prev = tok
    text = "".join(parts)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _needs_space(prev: Token, cur: Token) -> bool:
    # Conservative spacing to preserve token boundaries and keep -E output readable.
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT) and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT):
        return True
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and cur.kind == TokenKind.INT:
        return True
    if prev.kind == TokenKind.INT and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
        return True
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT) and cur.kind in (TokenKind.CHAR, TokenKind.STRING):
        return True
    if prev.kind in (TokenKind.CHAR, TokenKind.STRING) and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT, TokenKind.CHAR, TokenKind.STRING):
        return True
    # Space around/between most punctuation is safe and improves readability.
    if prev.kind == TokenKind.PUNCT and cur.kind == TokenKind.PUNCT:
        pair = prev.value + cur.value
        # Keep common multi-character operators and adjacent open/close tight only in simple cases.
        tight_pairs = {"()", "[]", "{}", "->", "++", "--", "##", "::"}
        if pair in tight_pairs:
            return False
        # Avoid accidentally forming comment introducers or ambiguous repeats not intended.
        if pair in {"/*", "//", "*/"}:
            return True
        return False
    if prev.kind == TokenKind.PUNCT and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT, TokenKind.CHAR, TokenKind.STRING):
        if prev.value in (")", "]", "}"):
            return True
        return False
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT, TokenKind.CHAR, TokenKind.STRING) and cur.kind == TokenKind.PUNCT:
        if cur.value in ("(", "["):
            return False
        return False
    if prev.value == "/" and cur.value in {"/", "*", "="}:
        return True
    return False


# Convenience functions

def preprocess(source: str, filename: str = "<stdin>", *, directory: Optional[str | Path] = None,
               include_paths: Optional[Sequence[str | Path]] = None,
               system_include_paths: Optional[Sequence[str | Path]] = None,
               defines: Optional[Dict[str, str]] = None,
               undefines: Optional[Sequence[str]] = None,
               language: str = "nova-c11") -> str:
    opts = PreprocessorOptions(
        include_paths=[Path(p) for p in (include_paths or [])],
        system_include_paths=[Path(p) for p in (system_include_paths or [])],
        defines=dict(defines or {}),
        undefines=list(undefines or []),
        language=language,
    )
    pp = Preprocessor(filename=filename, directory=Path(directory) if directory else None, options=opts)
    return pp.preprocess(source, filename=filename, directory=directory)


def preprocess_to_tokens(source: str, filename: str = "<stdin>", *, directory: Optional[str | Path] = None,
                         include_paths: Optional[Sequence[str | Path]] = None,
                         system_include_paths: Optional[Sequence[str | Path]] = None,
                         defines: Optional[Dict[str, str]] = None,
                         undefines: Optional[Sequence[str]] = None,
                         language: str = "nova-c11") -> List[Token]:
    opts = PreprocessorOptions(
        include_paths=[Path(p) for p in (include_paths or [])],
        system_include_paths=[Path(p) for p in (system_include_paths or [])],
        defines=dict(defines or {}),
        undefines=list(undefines or []),
        language=language,
    )
    pp = Preprocessor(filename=filename, directory=Path(directory) if directory else None, options=opts)
    return pp.preprocess_to_tokens(source, filename=filename, directory=directory)
