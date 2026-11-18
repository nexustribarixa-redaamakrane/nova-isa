"""C parser for :mod:`nova.cc`.

This module implements a recursive-descent parser for the Phase 1 C subset.
It consumes a preprocessed token stream and produces a syntax-level AST with
source locations, including structural declarators suitable for later semantic
analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

from . import ast
from .diagnostics import CompileError, SourceLocation
from .lexer import Token, TokenKind
from .preprocessor import preprocess_to_tokens

ASSIGNMENT_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}
TYPE_QUALIFIERS = {"const", "volatile", "restrict", "_Atomic"}
STORAGE_CLASSES = {"typedef", "extern", "static", "auto", "register"}
FUNCTION_SPECIFIERS = {"inline", "_Noreturn"}
TYPE_KEYWORDS = {"void", "char", "short", "int", "long", "signed", "unsigned", "_Bool", "float", "double"}
STRUCT_UNION = {"struct", "union"}


class ParseError(CompileError):
    """Syntax/parsing error (alias of CompileError for clarity)."""


@dataclass
class _Scope:
    typedefs: set


class Parser:
    def __init__(self, tokens: Sequence[Token], filename: str = "<stdin>"):
        self.tokens = list(tokens)
        if not self.tokens or self.tokens[-1].kind != TokenKind.EOF:
            end_loc = self.tokens[-1].location if self.tokens else SourceLocation(filename, 1, 1)
            self.tokens.append(Token(TokenKind.EOF, "", end_loc))
        self.i = 0
        self.filename = filename
        self.scopes: List[_Scope] = [_Scope(set())]
        self.errors: List[CompileError] = []

    # ------------------------------------------------------------------
    # Token utilities
    # ------------------------------------------------------------------

    def peek(self, offset: int = 0) -> Token:
        j = self.i + offset
        if j >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[j]

    def current(self) -> Token:
        return self.peek(0)

    def at_end(self) -> bool:
        return self.current().kind == TokenKind.EOF

    def take(self) -> Token:
        tok = self.current()
        if tok.kind != TokenKind.EOF:
            self.i += 1
        return tok

    def check(self, kind: TokenKind, value: Optional[str] = None, offset: int = 0) -> bool:
        tok = self.peek(offset)
        if tok.kind != kind:
            return False
        if value is not None and tok.value != value:
            return False
        return True

    def match(self, kind: TokenKind, value: Optional[str] = None) -> Optional[Token]:
        if self.check(kind, value):
            return self.take()
        return None

    def match_punct(self, *values: str) -> Optional[Token]:
        tok = self.current()
        if tok.kind == TokenKind.PUNCT and tok.value in values:
            return self.take()
        return None

    def match_keyword(self, *values: str) -> Optional[Token]:
        tok = self.current()
        if tok.kind == TokenKind.KEYWORD and tok.value in values:
            return self.take()
        return None

    def expect_punct(self, value: str, message: Optional[str] = None) -> Token:
        tok = self.match_punct(value)
        if tok is None:
            msg = message or f"expected '{value}'"
            raise self.error_here(msg)
        return tok

    def expect_kind(self, kind: TokenKind, message: str) -> Token:
        tok = self.current()
        if tok.kind != kind:
            raise self.error_here(message)
        return self.take()

    def error_here(self, message: str) -> ParseError:
        tok = self.current()
        return ParseError.at(tok.location, message)

    def push_scope(self) -> None:
        self.scopes.append(_Scope(set()))

    def pop_scope(self) -> None:
        if len(self.scopes) > 1:
            self.scopes.pop()

    def declare_typedef(self, name: str) -> None:
        self.scopes[-1].typedefs.add(name)

    def is_typedef_name(self, name: str) -> bool:
        for scope in reversed(self.scopes):
            if name in scope.typedefs:
                return True
        return False

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def parse_translation_unit(self) -> ast.TranslationUnit:
        loc = self.current().location
        decls: List[ast.ExternalDeclaration] = []
        while not self.at_end():
            try:
                decls.extend(self.parse_external_declaration())
            except CompileError as exc:
                # Basic synchronization at top level
                self.errors.append(exc)
                self._synchronize_top_level()
                if self.errors:
                    # Fail fast with first useful diagnostic for now
                    raise self.errors[0]
        return ast.TranslationUnit(location=loc, declarations=decls)

    # ------------------------------------------------------------------
    # Declarations / top-level
    # ------------------------------------------------------------------

    def parse_external_declaration(self) -> List[ast.ExternalDeclaration]:
        if self.at_end():
            return []
        # Empty declaration ';'
        if self.match_punct(";"):
            return []
        spec_loc = self.current().location
        specifiers = self.parse_declaration_specifiers(allow_storage=True)
        # Handle struct/union/enum declaration with no declarator: 'struct S { ... };'
        if self.check(TokenKind.PUNCT, ";"):
            self.take()
            decl = ast.Declaration(location=spec_loc, specifiers=specifiers, init_declarators=[])
            self._maybe_register_typedefs(specifiers, [])
            return [decl]
        # Function definition? parse first declarator and look for '{' (or K&R decls)
        declarator = self.parse_declarator(allow_abstract=False)
        if self.check(TokenKind.PUNCT, "{") or (self._looks_like_function_definition_after_declarator()):
            # Function definition
            old_style = []
            while not self.check(TokenKind.PUNCT, "{") and not self.at_end():
                # K&R old-style parameter declarations
                if self._is_declaration_start():
                    old_style.append(self.parse_declaration())
                else:
                    break
            body = self.parse_compound_statement()
            self._maybe_register_typedefs(specifiers, [])
            func = ast.FunctionDefinition(location=spec_loc, specifiers=specifiers, declarator=declarator,
                                          body=body, old_style_declarations=old_style)
            return [func]
        # Otherwise it's a declaration (possibly multiple declarators)
        init_decls = [self._parse_init_declarator_after_declarator(declarator)]
        while self.match_punct(","):
            init_decls.append(self.parse_init_declarator())
        self.expect_punct(";", "expected ';' after declaration")
        decl = ast.Declaration(location=spec_loc, specifiers=specifiers, init_declarators=init_decls)
        self._maybe_register_typedefs(specifiers, init_decls)
        return [decl]

    def _looks_like_function_definition_after_declarator(self) -> bool:
        # Conservative: K&R style would have declarations before '{'; handled in caller loop.
        return False

    def parse_declaration(self) -> ast.Declaration:
        loc = self.current().location
        specifiers = self.parse_declaration_specifiers(allow_storage=True)
        init_decls: List[ast.InitDeclarator] = []
        if not self.check(TokenKind.PUNCT, ";"):
            init_decls.append(self.parse_init_declarator())
            while self.match_punct(","):
                init_decls.append(self.parse_init_declarator())
        self.expect_punct(";", "expected ';' after declaration")
        decl = ast.Declaration(location=loc, specifiers=specifiers, init_declarators=init_decls)
        self._maybe_register_typedefs(specifiers, init_decls)
        return decl

    def _maybe_register_typedefs(self, specifiers: ast.DeclarationSpecifiers, init_decls: List[ast.InitDeclarator]) -> None:
        if any(s.name == "typedef" for s in specifiers.storage):
            for init in init_decls:
                name = self._declarator_name(init.declarator)
                if name:
                    self.declare_typedef(name)

    def _declarator_name(self, decl: Optional[ast.Declarator]) -> Optional[str]:
        if decl is None or decl.direct is None:
            return None
        d = decl.direct
        while True:
            if isinstance(d, ast.IdentifierDeclarator):
                return d.name
            if isinstance(d, ast.ParenthesizedDeclarator):
                return self._declarator_name(d.inner)
            if isinstance(d, (ast.ArrayDeclarator, ast.FunctionDeclarator)):
                d = d.base
                continue
            if isinstance(d, ast.AbstractDeclaratorNode):
                return None
            return None

    def parse_declaration_specifiers(self, allow_storage: bool = True) -> ast.DeclarationSpecifiers:
        loc = self.current().location
        storage: List[ast.StorageClassSpecifier] = []
        qualifiers: List[ast.TypeQualifier] = []
        type_specs: List[ast.TypeSpecifier] = []
        func_specs: List[str] = []
        seen_type = False
        while True:
            tok = self.current()
            if tok.kind == TokenKind.KEYWORD and tok.value in STORAGE_CLASSES:
                if not allow_storage:
                    raise self.error_here(f"storage class specifier '{tok.value}' not allowed here")
                self.take()
                storage.append(ast.StorageClassSpecifier(location=tok.location, name=tok.value))
                continue
            if tok.kind == TokenKind.KEYWORD and tok.value in TYPE_QUALIFIERS:
                self.take()
                qualifiers.append(ast.TypeQualifier(location=tok.location, name=tok.value))
                continue
            if tok.kind == TokenKind.KEYWORD and tok.value in FUNCTION_SPECIFIERS:
                self.take()
                func_specs.append(tok.value)
                continue
            if tok.kind == TokenKind.KEYWORD and tok.value in STRUCT_UNION:
                type_specs.append(self.parse_struct_or_union_specifier())
                seen_type = True
                continue
            if tok.kind == TokenKind.KEYWORD and tok.value == "enum":
                type_specs.append(self.parse_enum_specifier())
                seen_type = True
                continue
            if tok.kind == TokenKind.KEYWORD and tok.value in TYPE_KEYWORDS:
                # collect primitive type keyword sequence (int, long, unsigned, etc.)
                names = [self.take().value]
                while self.current().kind == TokenKind.KEYWORD and self.current().value in TYPE_KEYWORDS:
                    names.append(self.take().value)
                type_specs.append(ast.PrimitiveTypeSpecifier(location=loc, names=names))
                seen_type = True
                continue
            if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and self.is_typedef_name(tok.value) and not seen_type:
                name = self.take().value
                type_specs.append(ast.TypedefName(location=tok.location, name=name))
                seen_type = True
                continue
            break
        if not storage and not qualifiers and not type_specs and not func_specs:
            raise self.error_here("expected declaration specifiers")
        return ast.DeclarationSpecifiers(location=loc, storage=storage, qualifiers=qualifiers,
                                         type_specifiers=type_specs, function_specifiers=func_specs)

    def parse_struct_or_union_specifier(self) -> ast.StructOrUnionSpecifier:
        kw = self.take()  # struct/union
        loc = kw.location
        name: Optional[str] = None
        if self.current().kind in (TokenKind.IDENT, TokenKind.KEYWORD):
            # tag name
            if not self.check(TokenKind.PUNCT, "{"):
                name = self.take().value
        members = None
        if self.match_punct("{"):
            members = []
            self.push_scope()  # members don't create typedef scope, but harmless
            while not self.check(TokenKind.PUNCT, "}") and not self.at_end():
                members.append(self.parse_struct_member())
            self.pop_scope()
            self.expect_punct("}", "expected '}' after struct/union body")
        if name is None and members is None:
            raise self.error_here("expected struct/union tag or body")
        return ast.StructOrUnionSpecifier(location=loc, keyword=kw.value, name=name, members=members)

    def parse_struct_member(self) -> ast.StructMember:
        loc = self.current().location
        spec = self.parse_declaration_specifiers(allow_storage=False)
        declarators: List[ast.StructDeclarator] = []
        if self.check(TokenKind.PUNCT, ";"):
            self.take()
            return ast.StructMember(location=loc, specifiers=spec, declarators=declarators)
        while True:
            d = None
            if not self.check(TokenKind.PUNCT, ":"):
                d = self.parse_declarator(allow_abstract=False)
            bit_width = None
            if self.match_punct(":"):
                bit_width = self.parse_assignment_expression()
            declarators.append(ast.StructDeclarator(location=(d.location if d else loc), declarator=d, bit_width=bit_width))
            if not self.match_punct(","):
                break
        self.expect_punct(";", "expected ';' after struct member")
        return ast.StructMember(location=loc, specifiers=spec, declarators=declarators)

    def parse_enum_specifier(self) -> ast.EnumSpecifier:
        kw = self.take()  # enum
        loc = kw.location
        name = None
        if self.current().kind in (TokenKind.IDENT, TokenKind.KEYWORD) and not self.check(TokenKind.PUNCT, "{"):
            name = self.take().value
        enumerators = None
        if self.match_punct("{"):
            enumerators = []
            while not self.check(TokenKind.PUNCT, "}") and not self.at_end():
                etok = self.current()
                if etok.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise self.error_here("expected enumerator name")
                ename = self.take().value
                value = None
                if self.match_punct("="):
                    value = self.parse_assignment_expression()
                enumerators.append(ast.Enumerator(location=etok.location, name=ename, value=value))
                if not self.match_punct(","):
                    break
            self.expect_punct("}", "expected '}' after enumerator list")
        if name is None and enumerators is None:
            raise self.error_here("expected enum tag or body")
        return ast.EnumSpecifier(location=loc, name=name, enumerators=enumerators)

    # ------------------------------------------------------------------
    # Declarators
    # ------------------------------------------------------------------

    def parse_init_declarator(self) -> ast.InitDeclarator:
        d = self.parse_declarator(allow_abstract=False)
        return self._parse_init_declarator_after_declarator(d)

    def _parse_init_declarator_after_declarator(self, declarator: ast.Declarator) -> ast.InitDeclarator:
        init = None
        if self.match_punct("="):
            init = self.parse_initializer()
        return ast.InitDeclarator(location=declarator.location, declarator=declarator, initializer=init)

    def parse_initializer(self) -> ast.Initializer:
        if self.match_punct("{"):
            items = self.parse_initializer_list_items()
            self.expect_punct("}", "expected '}' after initializer list")
            return ast.InitializerList(location=self.current().location, items=items)
        return self.parse_assignment_expression()

    def parse_initializer_list_items(self) -> List[Union[ast.Initializer, ast.DesignatedInitializer]]:
        items: List[Union[ast.Initializer, ast.DesignatedInitializer]] = []
        while not self.check(TokenKind.PUNCT, "}") and not self.at_end():
            if self.check(TokenKind.PUNCT, ".") or self.check(TokenKind.PUNCT, "["):
                items.append(self.parse_designated_initializer())
            else:
                items.append(self.parse_initializer())
            if not self.match_punct(","):
                break
        return items

    def parse_designated_initializer(self) -> ast.DesignatedInitializer:
        loc = self.current().location
        designators: List[ast.Designator] = []
        while True:
            if self.match_punct("."):
                name_tok = self.current()
                if name_tok.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise self.error_here("expected field name in designator")
                name = self.take().value
                designators.append(ast.Designator(location=name_tok.location, field=name))
                continue
            if self.match_punct("["):
                idx = self.parse_assignment_expression()
                self.expect_punct("]", "expected ']' in designator")
                designators.append(ast.Designator(location=loc, index=idx))
                continue
            break
        self.expect_punct("=", "expected '=' after designator")
        init = self.parse_initializer()
        return ast.DesignatedInitializer(location=loc, designators=designators, initializer=init)

    def parse_declarator(self, allow_abstract: bool = False) -> ast.Declarator:
        loc = self.current().location
        pointers = self.parse_pointer_prefix()
        direct = self.parse_direct_declarator(allow_abstract=allow_abstract)
        if direct is None:
            if allow_abstract:
                return ast.Declarator(location=loc, pointers=pointers, direct=ast.AbstractDeclaratorNode(location=loc))
            raise self.error_here("expected declarator")
        return ast.Declarator(location=loc, pointers=pointers, direct=direct)

    def parse_pointer_prefix(self) -> List[ast.Pointer]:
        pointers: List[ast.Pointer] = []
        while self.match_punct("*"):
            quals: List[ast.TypeQualifier] = []
            while self.current().kind == TokenKind.KEYWORD and self.current().value in TYPE_QUALIFIERS:
                tok = self.take()
                quals.append(ast.TypeQualifier(location=tok.location, name=tok.value))
            pointers.append(ast.Pointer(location=self.current().location, qualifiers=quals))
        return pointers

    def parse_direct_declarator(self, allow_abstract: bool) -> Optional[ast.DirectDeclarator]:
        loc = self.current().location
        base: Optional[ast.DirectDeclarator] = None
        if self.match_punct("("):
            # Could be parenthesized declarator or function parameter list if this is abstract base in some forms;
            # distinguish: if next tokens can start a declarator/pointer or ')', parse as declarator inside parens
            # unless it's clearly parameter list in abstract context? But function declarator suffix handles params.
            # Heuristic: if we see declaration specifier or ')' or '...' at start inside parens and allow_abstract,
            # treat as function params of abstract declarator with empty base.
            if allow_abstract and (self._is_declaration_specifier_start() or self.check(TokenKind.PUNCT, ")") or self.check(TokenKind.PUNCT, "...")):
                # function declarator with omitted base: (params) in abstract declarator context is uncommon, but
                # interpret as params list: backtrack-like by parsing params from current pos-? easier: treat '(' as
                # start of function parameter list on abstract base.
                # Actually, in C, abstract declarator can be (*)(int) where inner is (* ) etc. We need better disambiguation.
                # Simpler approach: try parse declarator; if that fails in a way that suggests params, treat as params.
                params = self.parse_parameter_list_after_lparen()
                base = ast.FunctionDeclarator(location=loc, base=ast.AbstractDeclaratorNode(location=loc), params=params)
            else:
                inner = self.parse_declarator(allow_abstract=allow_abstract)
                self.expect_punct(")", "expected ')' in declarator")
                base = ast.ParenthesizedDeclarator(location=loc, inner=inner)
        elif self.current().kind in (TokenKind.IDENT, TokenKind.KEYWORD) and not self.is_typedef_name(self.current().value):
            # identifier declarator (typedef name is not allowed as declarator name in same scope declaration in this simple model)
            name = self.take().value
            base = ast.IdentifierDeclarator(location=loc, name=name)
        elif allow_abstract:
            base = None
        else:
            return None

        # Suffixes: arrays and function parameter lists (left-to-right)
        while True:
            if self.match_punct("["):
                if base is None:
                    base = ast.AbstractDeclaratorNode(location=loc)
                quals = []
                static = False
                # consume simple qualifiers/static if present (limited)
                while self.current().kind == TokenKind.KEYWORD and (self.current().value in TYPE_QUALIFIERS or self.current().value == "static"):
                    if self.current().value == "static":
                        static = True
                        self.take()
                    else:
                        t = self.take()
                        quals.append(ast.TypeQualifier(location=t.location, name=t.value))
                size = None
                if not self.check(TokenKind.PUNCT, "]"):
                    size = self.parse_assignment_expression()
                self.expect_punct("]", "expected ']' in array declarator")
                base = ast.ArrayDeclarator(location=loc, base=base, size=size, qualifiers=quals, static=static)
                continue
            if self.match_punct("("):
                if base is None:
                    base = ast.AbstractDeclaratorNode(location=loc)
                params = self.parse_parameter_list_after_lparen()
                base = ast.FunctionDeclarator(location=loc, base=base, params=params)
                continue
            break
        return base

    def parse_parameter_list_after_lparen(self) -> ast.ParameterList:
        loc = self.current().location
        params: List[ast.ParameterDeclaration] = []
        variadic = False
        if self.match_punct(")"):
            return ast.ParameterList(location=loc, params=params, variadic=False)
        while True:
            if self.match_punct("..."):
                variadic = True
                self.expect_punct(")", "expected ')' after variadic parameter")
                break
            param = self.parse_parameter_declaration()
            params.append(param)
            if self.match_punct(")"):
                break
            self.expect_punct(",", "expected ',' or ')' in parameter list")
        return ast.ParameterList(location=loc, params=params, variadic=variadic)

    def parse_parameter_declaration(self) -> ast.ParameterDeclaration:
        loc = self.current().location
        spec = self.parse_declaration_specifiers(allow_storage=True)
        # declarator may be abstract or omitted in some cases
        declarator = None
        abstract = None
        # Try to parse declarator if possible
        save = self.i
        try:
            # if next is ',' or ')' or '...', no declarator
            if self.current().value in (",", ")", "..."):
                declarator = None
            else:
                d = self.parse_declarator(allow_abstract=True)
                # If it has a name, it's a declarator; if only abstract, store as abstract
                if self._declarator_name(d):
                    declarator = d
                else:
                    abstract = d
        except CompileError:
            self.i = save
            declarator = None
        return ast.ParameterDeclaration(location=loc, specifiers=spec, declarator=declarator, abstract_declarator=abstract)

    # ------------------------------------------------------------------
    # Type names (for casts / sizeof)
    # ------------------------------------------------------------------

    def parse_type_name(self) -> ast.TypeName:
        loc = self.current().location
        spec = self.parse_declaration_specifiers(allow_storage=False)
        decl = None
        if not self.check(TokenKind.PUNCT, ")"):
            save = self.i
            try:
                decl = self.parse_declarator(allow_abstract=True)
            except CompileError:
                self.i = save
                decl = None
        return ast.TypeName(location=loc, specifiers=spec, declarator=decl)

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def parse_expression(self) -> ast.Expression:
        loc = self.current().location
        exprs = [self.parse_assignment_expression()]
        while self.match_punct(","):
            exprs.append(self.parse_assignment_expression())
        if len(exprs) == 1:
            return exprs[0]
        return ast.CommaExpression(location=loc, expressions=exprs)

    def parse_assignment_expression(self) -> ast.Expression:
        left = self.parse_conditional_expression()
        tok = self.current()
        if tok.kind == TokenKind.PUNCT and tok.value in ASSIGNMENT_OPS:
            op = self.take().value
            right = self.parse_assignment_expression()  # right-associative
            return ast.AssignmentExpression(location=left.location, op=op, left=left, right=right)
        return left

    def parse_conditional_expression(self) -> ast.Expression:
        cond = self.parse_logical_or_expression()
        if self.match_punct("?"):
            then_expr = self.parse_expression() if not self.check(TokenKind.PUNCT, ":") else None
            self.expect_punct(":", "expected ':' in conditional expression")
            else_expr = self.parse_conditional_expression()
            return ast.ConditionalExpression(location=cond.location, condition=cond, then_expr=then_expr, else_expr=else_expr)
        return cond

    def parse_logical_or_expression(self) -> ast.Expression:
        left = self.parse_logical_and_expression()
        while self.match_punct("||"):
            op = "||"
            right = self.parse_logical_and_expression()
            left = ast.BinaryExpression(location=left.location, op=op, left=left, right=right)
        return left

    def parse_logical_and_expression(self) -> ast.Expression:
        left = self.parse_bit_or_expression()
        while self.match_punct("&&"):
            right = self.parse_bit_or_expression()
            left = ast.BinaryExpression(location=left.location, op="&&", left=left, right=right)
        return left

    def parse_bit_or_expression(self) -> ast.Expression:
        left = self.parse_bit_xor_expression()
        while self.match_punct("|"):
            right = self.parse_bit_xor_expression()
            left = ast.BinaryExpression(location=left.location, op="|", left=left, right=right)
        return left

    def parse_bit_xor_expression(self) -> ast.Expression:
        left = self.parse_bit_and_expression()
        while self.match_punct("^"):
            right = self.parse_bit_and_expression()
            left = ast.BinaryExpression(location=left.location, op="^", left=left, right=right)
        return left

    def parse_bit_and_expression(self) -> ast.Expression:
        left = self.parse_equality_expression()
        while self.match_punct("&"):
            right = self.parse_equality_expression()
            left = ast.BinaryExpression(location=left.location, op="&", left=left, right=right)
        return left

    def parse_equality_expression(self) -> ast.Expression:
        left = self.parse_relational_expression()
        while True:
            if self.match_punct("=="):
                right = self.parse_relational_expression()
                left = ast.BinaryExpression(location=left.location, op="==", left=left, right=right)
            elif self.match_punct("!="):
                right = self.parse_relational_expression()
                left = ast.BinaryExpression(location=left.location, op="!=", left=left, right=right)
            else:
                break
        return left

    def parse_relational_expression(self) -> ast.Expression:
        left = self.parse_shift_expression()
        while True:
            if self.match_punct("<="):
                right = self.parse_shift_expression()
                left = ast.BinaryExpression(location=left.location, op="<=", left=left, right=right)
            elif self.match_punct(">="):
                right = self.parse_shift_expression()
                left = ast.BinaryExpression(location=left.location, op=">=", left=left, right=right)
            elif self.match_punct("<"):
                right = self.parse_shift_expression()
                left = ast.BinaryExpression(location=left.location, op="<", left=left, right=right)
            elif self.match_punct(">"):
                right = self.parse_shift_expression()
                left = ast.BinaryExpression(location=left.location, op=">", left=left, right=right)
            else:
                break
        return left

    def parse_shift_expression(self) -> ast.Expression:
        left = self.parse_additive_expression()
        while True:
            if self.match_punct("<<"):
                right = self.parse_additive_expression()
                left = ast.BinaryExpression(location=left.location, op="<<", left=left, right=right)
            elif self.match_punct(">>"):
                right = self.parse_additive_expression()
                left = ast.BinaryExpression(location=left.location, op=">>", left=left, right=right)
            else:
                break
        return left

    def parse_additive_expression(self) -> ast.Expression:
        left = self.parse_multiplicative_expression()
        while True:
            if self.match_punct("+"):
                right = self.parse_multiplicative_expression()
                left = ast.BinaryExpression(location=left.location, op="+", left=left, right=right)
            elif self.match_punct("-"):
                right = self.parse_multiplicative_expression()
                left = ast.BinaryExpression(location=left.location, op="-", left=left, right=right)
            else:
                break
        return left

    def parse_multiplicative_expression(self) -> ast.Expression:
        left = self.parse_cast_expression()
        while True:
            if self.match_punct("*"):
                right = self.parse_cast_expression()
                left = ast.BinaryExpression(location=left.location, op="*", left=left, right=right)
            elif self.match_punct("/"):
                right = self.parse_cast_expression()
                left = ast.BinaryExpression(location=left.location, op="/", left=left, right=right)
            elif self.match_punct("%"):
                right = self.parse_cast_expression()
                left = ast.BinaryExpression(location=left.location, op="%", left=left, right=right)
            else:
                break
        return left

    def parse_cast_expression(self) -> ast.Expression:
        # Cast or compound literal: '(' type-name ')' cast-expression | '{' initializer-list '}'
        if self.check(TokenKind.PUNCT, "(") and self._looks_like_cast():
            loc = self.take().location  # '('
            tname = self.parse_type_name()
            self.expect_punct(")", "expected ')' after cast type")
            if self.check(TokenKind.PUNCT, "{"):
                init = self.parse_initializer()
                if not isinstance(init, ast.InitializerList):
                    # parse_initializer returns InitializerList when '{' seen
                    init = ast.InitializerList(location=init.location, items=[init])
                return ast.CompoundLiteral(location=loc, type_name=tname, initializer=init)
            operand = self.parse_cast_expression()
            return ast.CastExpression(location=loc, type_name=tname, operand=operand)
        return self.parse_unary_expression()

    def _looks_like_cast(self) -> bool:
        # Lookahead: '(' followed by type specifier/qualifier/typedef name ... ')' and then
        # a token that can start a cast-expression (not just binary op in ambiguous cases).
        if not self.check(TokenKind.PUNCT, "("):
            return False
        # scan ahead conservatively
        j = self.i + 1
        depth = 0
        # must start with declaration specifier token
        tok = self.peek(1)
        if not self._token_starts_type_name(tok):
            return False
        # find matching ')' for this outer paren at depth 0 relative
        k = self.i
        paren = 0
        tokens = self.tokens
        while k < len(tokens):
            t = tokens[k]
            if t.value == "(":
                paren += 1
            elif t.value == ")":
                paren -= 1
                if paren == 0:
                    # token after cast close
                    nxt = tokens[k + 1] if k + 1 < len(tokens) else None
                    if nxt is None:
                        return False
                    # if next is '{' it might be compound literal (not handled now) - treat as cast-like
                    return True
            k += 1
        return False

    def _token_starts_type_name(self, tok: Token) -> bool:
        if tok.kind == TokenKind.KEYWORD and (tok.value in TYPE_KEYWORDS or tok.value in STRUCT_UNION or tok.value == "enum" or tok.value in TYPE_QUALIFIERS):
            return True
        if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and self.is_typedef_name(tok.value):
            return True
        return False

    def parse_unary_expression(self) -> ast.Expression:
        tok = self.current()
        if tok.kind == TokenKind.PUNCT and tok.value in {"++", "--", "+", "-", "!", "~", "*", "&"}:
            op = self.take().value
            operand = self.parse_cast_expression()
            return ast.UnaryExpression(location=tok.location, op=op, operand=operand, postfix=False)
        if tok.kind == TokenKind.KEYWORD and tok.value == "sizeof":
            loc = self.take().location
            if self.match_punct("("):
                # sizeof(type-name) or sizeof(expr) - disambiguate
                if self._looks_like_type_name_inside_sizeof():
                    tname = self.parse_type_name()
                    self.expect_punct(")", "expected ')' after sizeof type")
                    return ast.SizeofExpression(location=loc, operand=tname)
                else:
                    expr = self.parse_expression()
                    self.expect_punct(")", "expected ')' after sizeof expression")
                    return ast.SizeofExpression(location=loc, operand=expr)
            else:
                operand = self.parse_unary_expression()
                return ast.SizeofExpression(location=loc, operand=operand)
        return self.parse_postfix_expression()

    def _looks_like_type_name_inside_sizeof(self) -> bool:
        tok = self.current()
        return self._token_starts_type_name(tok)

    def parse_postfix_expression(self) -> ast.Expression:
        expr = self.parse_primary_expression()
        while True:
            if self.match_punct("("):
                args = self.parse_argument_expression_list()
                expr = ast.CallExpression(location=expr.location, callee=expr, arguments=args)
                continue
            if self.match_punct("["):
                idx = self.parse_expression()
                self.expect_punct("]", "expected ']' after array index")
                expr = ast.SubscriptExpression(location=expr.location, array=expr, index=idx)
                continue
            if self.match_punct("."):
                member = self.current()
                if member.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise self.error_here("expected member name after '.'")
                self.take()
                expr = ast.MemberAccessExpression(location=expr.location, base=expr, member=member.value, pointer=False)
                continue
            if self.match_punct("->"):
                member = self.current()
                if member.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise self.error_here("expected member name after '->'")
                self.take()
                expr = ast.MemberAccessExpression(location=expr.location, base=expr, member=member.value, pointer=True)
                continue
            if self.match_punct("++"):
                expr = ast.UnaryExpression(location=expr.location, op="++", operand=expr, postfix=True)
                continue
            if self.match_punct("--"):
                expr = ast.UnaryExpression(location=expr.location, op="--", operand=expr, postfix=True)
                continue
            break
        return expr

    def parse_argument_expression_list(self) -> List[ast.Expression]:
        args: List[ast.Expression] = []
        if self.match_punct(")"):
            return args
        while True:
            args.append(self.parse_assignment_expression())
            if self.match_punct(")"):
                break
            self.expect_punct(",", "expected ',' or ')' in argument list")
        return args

    def parse_primary_expression(self) -> ast.Expression:
        tok = self.current()
        if tok.kind == TokenKind.INT:
            self.take()
            return ast.IntegerLiteral(location=tok.location, value=tok.value, number=tok.number)
        if tok.kind == TokenKind.CHAR:
            self.take()
            return ast.CharacterLiteral(location=tok.location, value=tok.value, number=tok.number)
        if tok.kind == TokenKind.STRING:
            # Adjacent string literal concatenation
            parts = [tok.string or ""]
            loc = tok.location
            self.take()
            while self.current().kind == TokenKind.STRING:
                parts.append(self.current().string or "")
                self.take()
            value = "".join(parts)
            return ast.StringLiteral(location=loc, value=value, parts=parts)
        if tok.kind == TokenKind.KEYWORD and tok.value == "_Generic":
            return self.parse_generic_selection()
        if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
            # identifier (including keywords used as identifiers in some contexts? usually not; but take value)
            if tok.kind == TokenKind.KEYWORD and tok.value in {"sizeof"}:
                # should have been handled in unary
                raise self.error_here(f"unexpected keyword '{tok.value}' in expression")
            self.take()
            return ast.IdentifierExpression(location=tok.location, name=tok.value)
        if self.match_punct("("):
            expr = self.parse_expression()
            self.expect_punct(")", "expected ')' after expression")
            return expr
        raise self.error_here("expected expression")

    def parse_generic_selection(self) -> ast.GenericSelection:
        loc = self.expect_kind(TokenKind.KEYWORD, "expected '_Generic'").location
        self.expect_punct("(", "expected '(' after '_Generic'")
        control = self.parse_assignment_expression()
        self.expect_punct(",", "expected ',' after controlling expression in _Generic")
        associations: List[ast.GenericAssociation] = []
        while True:
            if self.current().kind == TokenKind.KEYWORD and self.current().value == "default" and self.check(TokenKind.PUNCT, ":", offset=1):
                self.take()  # default
                self.expect_punct(":", "expected ':' in generic association")
                expr = self.parse_assignment_expression()
                associations.append(ast.GenericAssociation(location=loc, type_name=None, expression=expr))
            else:
                tname = self.parse_type_name()
                self.expect_punct(":", "expected ':' after type name in generic association")
                expr = self.parse_assignment_expression()
                associations.append(ast.GenericAssociation(location=tname.location, type_name=tname, expression=expr))
            if self.match_punct(")"):
                break
            self.expect_punct(",", "expected ',' or ')' in generic association list")
        return ast.GenericSelection(location=loc, controlling=control, associations=associations)

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def parse_statement(self) -> ast.Statement:
        tok = self.current()
        if tok.kind == TokenKind.PUNCT and tok.value == "{":
            return self.parse_compound_statement()
        if tok.kind == TokenKind.KEYWORD:
            if tok.value == "if":
                return self.parse_if_statement()
            if tok.value == "switch":
                return self.parse_switch_statement()
            if tok.value == "while":
                return self.parse_while_statement()
            if tok.value == "do":
                return self.parse_do_while_statement()
            if tok.value == "for":
                return self.parse_for_statement()
            if tok.value == "return":
                return self.parse_return_statement()
            if tok.value == "break":
                loc = self.take().location
                self.expect_punct(";", "expected ';' after 'break'")
                return ast.BreakStatement(location=loc)
            if tok.value == "continue":
                loc = self.take().location
                self.expect_punct(";", "expected ';' after 'continue'")
                return ast.ContinueStatement(location=loc)
            if tok.value == "goto":
                loc = self.take().location
                label = self.current()
                if label.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
                    raise self.error_here("expected label name after 'goto'")
                self.take()
                self.expect_punct(";", "expected ';' after goto label")
                return ast.GotoStatement(location=loc, label=label.value)
            if tok.value == "case":
                return self.parse_case_statement()
            if tok.value == "default":
                return self.parse_default_statement()
        # Label: identifier ':'
        if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and self.check(TokenKind.PUNCT, ":", offset=1):
            name = self.take().value
            self.take()  # ':'
            body = self.parse_statement()
            return ast.LabelStatement(location=tok.location, label=name, body=body)
        # Expression statement or null statement
        if self.match_punct(";"):
            return ast.ExpressionStatement(location=tok.location, expression=None)
        expr = self.parse_expression()
        self.expect_punct(";", "expected ';' after expression")
        return ast.ExpressionStatement(location=expr.location, expression=expr)

    def parse_compound_statement(self) -> ast.CompoundStatement:
        loc = self.expect_punct("{", "expected '{'").location
        self.push_scope()
        items: List[Union[ast.Statement, ast.Declaration]] = []
        while not self.check(TokenKind.PUNCT, "}") and not self.at_end():
            try:
                if self._is_declaration_start():
                    items.append(self.parse_declaration())
                else:
                    items.append(self.parse_statement())
            except CompileError as exc:
                self.errors.append(exc)
                self._synchronize_block()
                if self.errors:
                    raise self.errors[0]
        self.expect_punct("}", "expected '}' after compound statement")
        self.pop_scope()
        return ast.CompoundStatement(location=loc, items=items)

    def parse_if_statement(self) -> ast.IfStatement:
        loc = self.take().location  # if
        self.expect_punct("(", "expected '(' after 'if'")
        cond = self.parse_expression()
        self.expect_punct(")", "expected ')' after if condition")
        then_b = self.parse_statement()
        else_b = None
        if self.match_keyword("else"):
            else_b = self.parse_statement()
        return ast.IfStatement(location=loc, condition=cond, then_branch=then_b, else_branch=else_b)

    def parse_switch_statement(self) -> ast.SwitchStatement:
        loc = self.take().location
        self.expect_punct("(", "expected '(' after 'switch'")
        cond = self.parse_expression()
        self.expect_punct(")", "expected ')' after switch condition")
        body = self.parse_statement()
        return ast.SwitchStatement(location=loc, condition=cond, body=body)

    def parse_case_statement(self) -> ast.CaseStatement:
        loc = self.take().location
        value = self.parse_assignment_expression()
        self.expect_punct(":", "expected ':' after case value")
        # In C, case can be followed by statements; parse one statement (often a labeled/compound/body)
        body = self.parse_statement() if not self.check(TokenKind.PUNCT, "}") else None
        return ast.CaseStatement(location=loc, value=value, body=body)

    def parse_default_statement(self) -> ast.DefaultStatement:
        loc = self.take().location
        self.expect_punct(":", "expected ':' after 'default'")
        body = self.parse_statement() if not self.check(TokenKind.PUNCT, "}") else None
        return ast.DefaultStatement(location=loc, body=body)

    def parse_while_statement(self) -> ast.WhileStatement:
        loc = self.take().location
        self.expect_punct("(", "expected '(' after 'while'")
        cond = self.parse_expression()
        self.expect_punct(")", "expected ')' after while condition")
        body = self.parse_statement()
        return ast.WhileStatement(location=loc, condition=cond, body=body)

    def parse_do_while_statement(self) -> ast.DoWhileStatement:
        loc = self.take().location
        body = self.parse_statement()
        if not self.match_keyword("while"):
            raise self.error_here("expected 'while' after do body")
        self.expect_punct("(", "expected '(' after 'while'")
        cond = self.parse_expression()
        self.expect_punct(")", "expected ')' after do-while condition")
        self.expect_punct(";", "expected ';' after do-while statement")
        return ast.DoWhileStatement(location=loc, body=body, condition=cond)

    def parse_for_statement(self) -> ast.ForStatement:
        loc = self.take().location
        self.expect_punct("(", "expected '(' after 'for'")
        self.push_scope()
        init = None
        if not self.check(TokenKind.PUNCT, ";"):
            if self._is_declaration_start():
                init = self.parse_declaration()
            else:
                expr = self.parse_expression()
                self.expect_punct(";", "expected ';' in for statement")
                init = expr
        else:
            self.take()  # ';'
        cond = None
        if not self.check(TokenKind.PUNCT, ";"):
            cond = self.parse_expression()
        self.expect_punct(";", "expected ';' in for statement")
        step = None
        if not self.check(TokenKind.PUNCT, ")"):
            step = self.parse_expression()
        self.expect_punct(")", "expected ')' after for clauses")
        body = self.parse_statement()
        self.pop_scope()
        return ast.ForStatement(location=loc, init=init, condition=cond, step=step, body=body)

    def parse_return_statement(self) -> ast.ReturnStatement:
        loc = self.take().location
        value = None
        if not self.check(TokenKind.PUNCT, ";"):
            value = self.parse_expression()
        self.expect_punct(";", "expected ';' after return value")
        return ast.ReturnStatement(location=loc, value=value)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_declaration_start(self) -> bool:
        tok = self.current()
        if tok.kind == TokenKind.KEYWORD and (tok.value in STORAGE_CLASSES or tok.value in TYPE_KEYWORDS or tok.value in STRUCT_UNION or tok.value == "enum" or tok.value in TYPE_QUALIFIERS or tok.value in FUNCTION_SPECIFIERS):
            return True
        if tok.kind in (TokenKind.IDENT, TokenKind.KEYWORD) and self.is_typedef_name(tok.value):
            return True
        return False

    def _is_declaration_specifier_start(self) -> bool:
        return self._is_declaration_start()

    def _synchronize_top_level(self) -> None:
        # Skip until a likely top-level boundary.
        while not self.at_end():
            tok = self.current()
            if tok.value == ";":
                self.take()
                return
            if tok.value == "}":
                self.take()
                return
            # start of another declaration/function
            if self._is_declaration_start():
                return
            self.take()

    def _synchronize_block(self) -> None:
        while not self.at_end():
            tok = self.current()
            if tok.value == ";":
                self.take()
                return
            if tok.value == "}":
                return
            if tok.value == "{":
                # consume nested block quickly to avoid cascading
                depth = 0
                while not self.at_end():
                    t = self.take()
                    if t.value == "{":
                        depth += 1
                    elif t.value == "}":
                        depth -= 1
                        if depth <= 0:
                            return
                return
            self.take()


# Convenience API

def parse(tokens: Sequence[Token], filename: str = "<stdin>") -> ast.TranslationUnit:
    return Parser(tokens, filename=filename).parse_translation_unit()


def parse_source(source: str, filename: str = "<stdin>", **preprocess_kwargs) -> ast.TranslationUnit:
    tokens = preprocess_to_tokens(source, filename=filename, **preprocess_kwargs)
    return parse(tokens, filename=filename)
