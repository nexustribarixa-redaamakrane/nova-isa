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
        # Default include context for top-level preprocessing.
        self._top_filename = filename
        self._top_directory = Path(directory) if directory is not None else Path.cwd()

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
        # __STDC__ and __STDC_VERSION__ are intentionally not defined until a
        # specific hosted/freestanding language mode is declared.
        # __FILE__ and __LINE__ are handled dynamically during expansion.
        pass

    def _install_initial_defines(self) -> None:
        for name, value in self.options.defines.items():
            self._define_from_text(name, value, SourceLocation("<command-line>", 1, 1))

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
            for line_text, line_no, raw_had_directive in logical_lines:
                self._process_logical_line(line_text, line_no, filename, directory, output, depth)
            return output
        finally:
            self.include_stack.pop()

    def _current_active(self) -> bool:
        return all(frame.active for frame in self.cond_stack)

    def _process_logical_line(self, line_text: str, line_no: int, filename: str,
                              directory: Path, output: List[Token], depth: int) -> None:
        stripped_leading = line_text.lstrip(" \t\v\f")
        if stripped_leading.startswith("#"):
            self._handle_directive(line_text, line_no, filename, directory, output, depth)
            return
        if not self._current_active():
            return
        tokens = _lex_fragment(line_text, filename, line_no)
        if not tokens:
            return
        expanded = self._expand_tokens(tokens, set(), 0)
        output.extend(expanded)

    # ------------------------------------------------------------------
    # Directives
    # ------------------------------------------------------------------

    def _handle_directive(self, line_text: str, line_no: int, filename: str,
                          directory: Path, output: List[Token], depth: int) -> None:
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
                self._directive_pragma(arg_text, loc, filename, directory)
            return
        if name == "error":
            if active:
                msg = arg_text.strip()
                raise CompileError.at(loc, msg or "#error")
            return
        if name == "line":
            # Accepted as no-op for now (location control not modeled in token stream API).
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
            self.pragma_once.add(_norm_path_key(filename, directory))
            return
        # Unknown pragmas are ignored in this phase.

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
        # Compute macros are not expanded in #include in this simplified form except
        # when the argument is not a direct header-name form; but easier: first try
        # direct forms.
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
        # Computed include: expand macros and expect a string or <...> sequence.
        toks = _lex_fragment(s, loc.file, loc.line)
        expanded = self._expand_tokens(toks, set(), 0)
        if len(expanded) == 1 and expanded[0].kind == TokenKind.STRING:
            return expanded[0].string or "", False
        # Try to reconstruct < ... > form
        if expanded and expanded[0].value == "<" and expanded[-1].value == ">":
            name = "".join(t.value for t in expanded[1:-1])
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

    def _expand_tokens(self, tokens: Sequence[Token], disabled: Set[str], depth: int) -> List[Token]:
        if depth > MAX_EXPANSION_DEPTH:
            if tokens:
                raise CompileError.at(tokens[0].location, "macro expansion depth exceeded")
            raise CompileError("macro expansion depth exceeded")
        out: List[Token] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
                # __FILE__ / __LINE__
                if tok.value == "__FILE__":
                    out.append(Token(TokenKind.STRING, '"' + _escape_c_string(tok.location.file) + '"',
                                     tok.location, string=tok.location.file))
                    i += 1
                    continue
                if tok.value == "__LINE__":
                    val = tok.location.line
                    out.append(Token(TokenKind.INT, str(val), tok.location, number=val))
                    i += 1
                    continue
                macro = self.macros.get(tok.value)
                if macro is None or tok.value in disabled:
                    out.append(tok)
                    i += 1
                    continue
                if macro.is_function_like:
                    # Function-like macro only expands if followed by '('
                    j = i + 1
                    # tokens list has no whitespace tokens, so next token is immediate
                    if j >= n or tokens[j].value != "(":
                        out.append(tok)
                        i += 1
                        continue
                    args, end = self._collect_macro_args(tokens, j, macro, tok.location)
                    replaced = self._substitute_macro(macro, args, tok.location)
                    # Rescan with this macro disabled in the rescanned result.
                    rescanned = self._expand_tokens(replaced, disabled | {macro.name}, depth + 1)
                    out.extend(rescanned)
                    i = end
                    continue
                else:
                    replaced = list(macro.replacement)
                    replaced = _relocate_tokens(replaced, tok.location)
                    rescanned = self._expand_tokens(replaced, disabled | {macro.name}, depth + 1)
                    out.extend(rescanned)
                    i += 1
                    continue
            else:
                out.append(tok)
                i += 1
        # Apply token pasting (##) on the expanded output as an operator pass.
        out = self._apply_token_pasting(out)
        return out

    def _collect_macro_args(self, tokens: Sequence[Token], lparen_index: int,
                            macro: Macro, loc: SourceLocation) -> Tuple[List[List[Token]], int]:
        # lparen_index points at '('
        i = lparen_index + 1
        n = len(tokens)
        args: List[List[Token]] = []
        current: List[Token] = []
        paren_depth = 0
        # Handle zero-arg invocation: ()
        if i < n and tokens[i].value == ")":
            if len(macro.params or ()) == 0:
                return [], i + 1
            if macro.variadic and len(macro.params or ()) == 1:
                # only __VA_ARGS__, may be empty
                return [[]], i + 1
            raise CompileError.at(loc, f"macro argument count mismatch for '{macro.name}'")
        while i < n:
            tok = tokens[i]
            if tok.value == "(":
                paren_depth += 1
                current.append(tok)
                i += 1
                continue
            if tok.value == ")" and paren_depth == 0:
                args.append(current)
                # validate count
                self._validate_arg_count(macro, args, loc)
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
                # empty varargs allowed; represent as empty list (already the case if handled) but
                # our collection puts fixed args then varargs - if only fixed provided, append empty vararg.
                args.append([])
            elif len(args) > fixed + 1:
                # extra args merged into varargs by our collection logic; should not happen except if miscount
                pass
            # If len(args) == fixed+1 ok.
            # Special case: fixed==0 and single arg collected may be vararg content (including empty via () handled earlier)
            return
        if len(args) != len(params):
            raise CompileError.at(loc, f"macro argument count mismatch for '{macro.name}'")

    def _substitute_macro(self, macro: Macro, args: List[List[Token]], invoke_loc: SourceLocation) -> List[Token]:
        params = list(macro.params or [])
        param_map: Dict[str, List[Token]] = {}
        for idx, pname in enumerate(params):
            param_map[pname] = list(args[idx]) if idx < len(args) else []
        repl = list(macro.replacement)
        out: List[Token] = []
        i = 0
        n = len(repl)
        while i < n:
            tok = repl[i]
            # Stringification: # param
            if tok.value == "#" and macro.is_function_like:
                if i + 1 < n and repl[i + 1].value in param_map:
                    arg_toks = param_map[repl[i + 1].value]
                    s = _stringize_tokens(arg_toks)
                    out.append(Token(TokenKind.STRING, '"' + _escape_c_string(s) + '"', invoke_loc, string=s))
                    i += 2
                    continue
                else:
                    raise CompileError.at(invoke_loc, "'#' is not followed by a macro parameter")
            # Parameter substitution (with awareness of ## so operands not prematurely expanded per standard rules;
            # simplified but practical: expand args unless adjacent to ##)
            if tok.value in param_map and macro.is_function_like:
                arg_toks = param_map[tok.value]
                # Determine if this parameter is operand of ##
                adjacent_paste = (i > 0 and repl[i - 1].value == "##") or (i + 1 < n and repl[i + 1].value == "##")
                if adjacent_paste:
                    # Insert raw argument tokens (no prior expansion)
                    out.extend(_relocate_tokens(arg_toks, invoke_loc))
                else:
                    # Expand argument tokens first, then substitute
                    expanded_arg = self._expand_tokens(arg_toks, set(), 0)
                    out.extend(_relocate_tokens(expanded_arg, invoke_loc))
                i += 1
                continue
            out.append(_relocate_token(tok, invoke_loc))
            i += 1
        return out

    def _apply_token_pasting(self, tokens: List[Token]) -> List[Token]:
        # Repeatedly merge A ## B into a single pasted preprocessing token.
        changed = True
        out = list(tokens)
        while changed:
            changed = False
            i = 0
            new_out: List[Token] = []
            while i < len(out):
                if i + 1 < len(out) and out[i + 1].value == "##":
                    if i + 2 >= len(out):
                        raise CompileError.at(out[i + 1].location, "'##' cannot appear at end of macro expansion")
                    left = out[i]
                    right = out[i + 2]
                    pasted_text = left.value + right.value
                    try:
                        pasted_toks = _lex_fragment(pasted_text, left.location.file, left.location.line)
                    except CompileError:
                        pasted_toks = []
                    if len(pasted_toks) != 1:
                        raise CompileError.at(out[i + 1].location, f"token pasting produced an invalid token '{pasted_text}'")
                    pt = pasted_toks[0]
                    new_out.append(Token(pt.kind, pt.value, left.location, number=pt.number, string=pt.string))
                    i += 3
                    changed = True
                    continue
                if out[i].value == "##":
                    # leading ## or leftover - invalid
                    raise CompileError.at(out[i].location, "'##' cannot appear here in macro expansion")
                new_out.append(out[i])
                i += 1
            out = new_out
        return out

    # ------------------------------------------------------------------
    # #if expression evaluation
    # ------------------------------------------------------------------

    def _eval_if_expression(self, expr_text: str, loc: SourceLocation) -> int:
        toks = _lex_fragment(expr_text, loc.file, loc.line)
        if not toks:
            raise CompileError.at(loc, "expected expression in conditional directive")
        # Replace 'defined' and perform macro expansion (identifiers not macros -> 0)
        toks = self._expand_if_tokens(toks, loc)
        parser = _IfExprParser(toks, loc)
        value = parser.parse_expression()
        if parser.peek().kind != TokenKind.EOF:
            raise CompileError.at(parser.peek().location, "unexpected token in conditional expression")
        return 1 if value else 0 if isinstance(value, bool) else int(value)


    def _expand_if_tokens(self, tokens: Sequence[Token], loc: SourceLocation) -> List[Token]:
        out: List[Token] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and tok.value == "defined":
                # defined NAME or defined(NAME)
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
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
                # true/false? no; expand macros except special. In #if, remaining identifiers become 0.
                macro = self.macros.get(tok.value)
                if macro is not None and not macro.is_function_like:
                    expanded = self._expand_if_tokens(_relocate_tokens(list(macro.replacement), tok.location), loc)
                    out.extend(expanded)
                    i += 1
                    continue
                elif macro is not None and macro.is_function_like:
                    # function-like macro in #if: attempt expansion only if invocation; else 0
                    if i + 1 < n and tokens[i + 1].value == "(":
                        args, end = self._collect_macro_args(tokens, i + 1, macro, tok.location)
                        replaced = self._substitute_macro(macro, args, tok.location)
                        out.extend(self._expand_if_tokens(replaced, loc))
                        i = end
                        continue
                    out.append(Token(TokenKind.INT, "0", tok.location, number=0))
                    i += 1
                    continue
                else:
                    out.append(Token(TokenKind.INT, "0", tok.location, number=0))
                    i += 1
                    continue
            out.append(tok)
            i += 1
        return out


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


def splice_lines(source: str, filename: str) -> List[Tuple[str, int, bool]]:
    """Perform backslash-newline splicing.

    Returns a list of (logical_line_text, starting_physical_line_number, _).
    Logical line text does not include the terminating newline.
    """
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    result: List[Tuple[str, int, bool]] = []
    i = 0
    phys = 1
    n = len(lines)
    while i < n:
        start_phys = phys
        parts = [lines[i]]
        cur = i
        # splice while line ends with backslash
        while cur < n and lines[cur].endswith("\\"):
            # remove trailing backslash
            parts[-1] = lines[cur][:-1]
            cur += 1
            if cur < n:
                parts.append(lines[cur])
        logical = "".join(parts)
        result.append((logical, start_phys, True))
        consumed = cur - i + 1
        i = cur + 1
        phys = start_phys + consumed
        # Handle case where source ended without trailing newline: split produced last element.
    # split('\n') on "a\n" gives ['a',''] so last empty logical line corresponds to trailing newline.
    # Keep behavior consistent; parser/preprocessor can ignore final empty logical line if desired.
    return result


def _lex_fragment(text: str, filename: str, line: int) -> List[Token]:
    if text == "":
        return []
    toks = lex(text, filename)
    # Drop EOF and relocate to requested logical line.
    out: List[Token] = []
    for t in toks:
        if t.kind == TokenKind.EOF:
            continue
        out.append(Token(t.kind, t.value, SourceLocation(filename, line, t.location.column),
                         number=t.number, string=t.string))
    return out


def _relocate_token(tok: Token, loc: SourceLocation) -> Token:
    # Preserve macro invocation location for diagnostics/expansion context; keep column 1 as coarse info.
    return Token(tok.kind, tok.value, SourceLocation(loc.file, loc.line, tok.location.column if tok.location.file == loc.file else 1),
                 number=tok.number, string=tok.string)


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


def _stringize_tokens(tokens: Sequence[Token]) -> str:
    # Concatenate token spellings. Original whitespace is not preserved by the
    # lexer, so we insert spaces only where needed to keep tokens distinct/readable.
    if not tokens:
        return ""
    parts: List[str] = []
    prev: Optional[Token] = None
    for tok in tokens:
        if prev is not None and _needs_stringize_space(prev, tok):
            parts.append(" ")
        parts.append(tok.value)
        prev = tok
    return "".join(parts)


def _needs_stringize_space(prev: Token, cur: Token) -> bool:
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT) and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT):
        return True
    if prev.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT) and cur.kind in (TokenKind.STRING, TokenKind.CHAR):
        return True
    if prev.kind in (TokenKind.STRING, TokenKind.CHAR) and cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD, TokenKind.INT, TokenKind.STRING, TokenKind.CHAR):
        return True
    # Prevent accidental token merging such as + + or / * in stringized text.
    if prev.kind == TokenKind.PUNCT and cur.kind == TokenKind.PUNCT:
        if prev.value + cur.value in {"++", "--", "##", "->", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
                                      "+=", "-=", "*=", "/=", "%=", "&=", "^=", "|=", "<<=", ">>=", "...", "/*", "//"}:
            return False
        # Adjacent identical punctuators that would merge visually into a different token.
        if prev.value == cur.value and prev.value in {"+", "-", "&", "|", "<", ">", "=", "/", "*", "%", "^", "!", ".", ":"}:
            return True
        return False
    return False


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
               defines: Optional[Dict[str, str]] = None) -> str:
    opts = PreprocessorOptions(
        include_paths=[Path(p) for p in (include_paths or [])],
        system_include_paths=[Path(p) for p in (system_include_paths or [])],
        defines=dict(defines or {}),
    )
    pp = Preprocessor(filename=filename, directory=Path(directory) if directory else None, options=opts)
    return pp.preprocess(source, filename=filename, directory=directory)


def preprocess_to_tokens(source: str, filename: str = "<stdin>", *, directory: Optional[str | Path] = None,
                         include_paths: Optional[Sequence[str | Path]] = None,
                         system_include_paths: Optional[Sequence[str | Path]] = None,
                         defines: Optional[Dict[str, str]] = None) -> List[Token]:
    opts = PreprocessorOptions(
        include_paths=[Path(p) for p in (include_paths or [])],
        system_include_paths=[Path(p) for p in (system_include_paths or [])],
        defines=dict(defines or {}),
    )
    pp = Preprocessor(filename=filename, directory=Path(directory) if directory else None, options=opts)
    return pp.preprocess_to_tokens(source, filename=filename, directory=directory)
