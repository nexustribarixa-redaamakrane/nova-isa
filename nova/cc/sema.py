"""Semantic analysis for :mod:`nova.cc`.

This module performs name resolution, type construction/checking, and basic
semantic validation over the syntax-level AST produced by :mod:`nova.cc.parser`.

It annotates AST nodes with semantic information for later code generation:
- ``resolved_type``: semantic :class:`nova.cc.types.Type` (for expressions and
  some declarators/declarations where useful)
- ``symbol`` / ``resolved_symbol``: resolved symbol object
- ``value_category``: ``"lvalue"``, ``"rvalue"``, or ``"function"``
- ``const_value``: integer constant value when safely known
- ``implicit_cast_to``: target type for implicit conversions (where applicable)

The analysis is intentionally conservative and targets the Nova64 ABI/type model.
It validates a useful Phase 1 subset and reports source-located diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from . import ast
from .diagnostics import CompileError, SourceLocation
from . import types
from .types import (
    ArrayType,
    BoolType,
    EnumType,
    FieldInfo,
    FloatingType,
    FunctionType,
    IntegerType,
    ParameterType,
    PointerType,
    Qualifiers,
    StructType,
    Type,
    TypeContext,
    UnionType,
    VoidType,
    NO_QUALS,
)


# ---------------------------------------------------------------------------
# Symbols and scopes
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: str  # 'object', 'function', 'typedef', 'enum_const', 'label', 'tag'
    type: Type
    location: SourceLocation
    storage: Optional[str] = None
    defined: bool = False
    enum_value: Optional[int] = None
    params_scope: Optional["Scope"] = None


@dataclass
class Scope:
    kind: str  # 'file', 'function', 'block', 'prototype'
    parent: Optional["Scope"] = None
    ordinary: Dict[str, Symbol] = field(default_factory=dict)  # objects/functions/typedefs/enum consts
    tags: Dict[str, Symbol] = field(default_factory=dict)      # struct/union/enum tags
    labels: Dict[str, Symbol] = field(default_factory=dict)    # function scope labels
    function: Optional[Symbol] = None

    def lookup_ordinary_local(self, name: str) -> Optional[Symbol]:
        return self.ordinary.get(name)

    def lookup_ordinary(self, name: str) -> Optional[Symbol]:
        s = self.ordinary.get(name)
        if s is not None:
            return s
        return self.parent.lookup_ordinary(name) if self.parent else None

    def lookup_tag_local(self, name: str) -> Optional[Symbol]:
        return self.tags.get(name)

    def lookup_tag(self, name: str) -> Optional[Symbol]:
        s = self.tags.get(name)
        if s is not None:
            return s
        return self.parent.lookup_tag(name) if self.parent else None


class SemanticError(CompileError):
    pass


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    def __init__(self, target: str = "nova64"):
        self.ctx = TypeContext(target=target)
        self.file_scope = Scope(kind="file")
        self.scope = self.file_scope
        self.current_function: Optional[Symbol] = None
        self.current_return_type: Optional[Type] = None
        self.switch_depth = 0
        self.loop_depth = 0
        self.label_defs: Dict[str, Symbol] = {}
        self.label_refs: List[Tuple[str, SourceLocation]] = []
        self.switch_stack: List[Dict[str, object]] = []

    # -- scopes -------------------------------------------------------------

    def push_scope(self, kind: str) -> Scope:
        self.scope = Scope(kind=kind, parent=self.scope, function=self.current_function)
        return self.scope

    def pop_scope(self) -> None:
        if self.scope.parent is not None:
            self.scope = self.scope.parent

    def function_scope(self) -> Scope:
        s = self.scope
        while s is not None:
            if s.kind == "function":
                return s
            s = s.parent
        return self.file_scope

    # -- diagnostics --------------------------------------------------------

    def error(self, loc: SourceLocation, msg: str) -> SemanticError:
        return SemanticError.at(loc, msg)

    # -- entry --------------------------------------------------------------

    def analyze_translation_unit(self, tu: ast.TranslationUnit) -> ast.TranslationUnit:
        tu.scope = self.file_scope  # type: ignore[attr-defined]
        for decl in tu.declarations:
            if isinstance(decl, ast.FunctionDefinition):
                self.analyze_function_definition(decl)
            else:
                self.analyze_declaration(decl, is_file_scope=True)
        # resolve goto labels
        for name, loc in self.label_refs:
            if name not in self.label_defs:
                raise self.error(loc, f"use of undeclared label '{name}'")
        tu.semantic_ok = True  # type: ignore[attr-defined]
        tu.type_context = self.ctx  # type: ignore[attr-defined]
        return tu

    # ------------------------------------------------------------------
    # Declaration specifiers / type construction
    # ------------------------------------------------------------------

    def qualifiers_from_ast(self, quals: Sequence[ast.TypeQualifier]) -> Qualifiers:
        q = Qualifiers()
        for item in quals:
            n = item.name
            if n == "const":
                q = Qualifiers(q.const or True, q.volatile, q.restrict, q.atomic)
            elif n == "volatile":
                q = Qualifiers(q.const, True, q.restrict, q.atomic)
            elif n == "restrict":
                q = Qualifiers(q.const, q.volatile, True, q.atomic)
            elif n == "_Atomic":
                q = Qualifiers(q.const, q.volatile, q.restrict, True)
        return q

    def analyze_type_specifiers(self, spec: ast.DeclarationSpecifiers, loc: SourceLocation) -> Type:
        ctx = self.ctx
        ts = list(spec.type_specifiers)
        if not ts:
            # implicit int not supported in C11 mode except maybe none; diagnose clearly
            raise self.error(loc, "missing type specifier in declaration")
        if len(ts) == 1:
            tnode = ts[0]
            if isinstance(tnode, ast.PrimitiveTypeSpecifier):
                return self.map_primitive_type(tnode.names, loc)
            if isinstance(tnode, ast.StructOrUnionSpecifier):
                return self.analyze_struct_union_specifier(tnode)
            if isinstance(tnode, ast.EnumSpecifier):
                return self.analyze_enum_specifier(tnode)
            if isinstance(tnode, ast.TypedefName):
                sym = self.scope.lookup_ordinary(tnode.name)
                if sym is None or sym.kind != "typedef":
                    raise self.error(tnode.location, f"unknown typedef name '{tnode.name}'")
                return sym.type
        # multiple specifiers - only primitive sequences like unsigned long long int
        if all(isinstance(t, ast.PrimitiveTypeSpecifier) for t in ts):
            names: List[str] = []
            for t in ts:
                names.extend(t.names)  # type: ignore[union-attr]
            return self.map_primitive_type(names, loc)
        # typedef name cannot combine with other type specifiers in standard C
        if any(isinstance(t, ast.TypedefName) for t in ts):
            raise self.error(loc, "typedef name cannot be combined with other type specifiers")
        raise self.error(loc, "invalid combination of type specifiers")

    def map_primitive_type(self, names: Sequence[str], loc: SourceLocation) -> Type:
        ctx = self.ctx
        key = tuple(sorted(names))
        # normalize common forms
        s = set(names)
        if s == {"void"}:
            return ctx.void_t
        if s == {"_Bool"}:
            return ctx.bool_t
        if s == {"char"}:
            return ctx.char_t
        if s == {"signed", "char"}:
            return ctx.schar_t
        if s == {"unsigned", "char"}:
            return ctx.uchar_t
        if s in ({"short"}, {"signed", "short"}, {"short", "int"}, {"signed", "short", "int"}):
            return ctx.short_t
        if s in ({"unsigned", "short"}, {"unsigned", "short", "int"}):
            return ctx.ushort_t
        if s in ({"int"}, {"signed"}, {"signed", "int"}):
            return ctx.int_t
        if s in ({"unsigned"}, {"unsigned", "int"}):
            return ctx.uint_t
        if s in ({"long"}, {"signed", "long"}, {"long", "int"}, {"signed", "long", "int"}):
            return ctx.long_t
        if s in ({"unsigned", "long"}, {"unsigned", "long", "int"}):
            return ctx.ulong_t
        if s in ({"long", "long"}, {"signed", "long", "long"}, {"long", "long", "int"}, {"signed", "long", "long", "int"}):
            return ctx.llong_t
        if s in ({"unsigned", "long", "long"}, {"unsigned", "long", "long", "int"}):
            return ctx.ullong_t
        if s == {"float"}:
            return ctx.float_t
        if s == {"double"}:
            return ctx.double_t
        if s in ({"long", "double"}):
            return ctx.ldouble_t
        raise self.error(loc, f"unsupported or invalid type specifier combination: {' '.join(names)}")

    # ------------------------------------------------------------------
    # Struct/union/enum
    # ------------------------------------------------------------------

    def analyze_struct_union_specifier(self, node: ast.StructOrUnionSpecifier) -> Type:
        ctx = self.ctx
        is_union = node.keyword == "union"
        tag_name = node.name
        sym = None
        if tag_name:
            if node.members is None:
                # elaborated type specifier / forward reference: find any visible tag
                sym = self.scope.lookup_tag(tag_name)
                if sym is None:
                    if is_union:
                        t: Type = UnionType(tag_name, ctx.new_tag_id(), complete=False)
                    else:
                        t = StructType(tag_name, ctx.new_tag_id(), complete=False)
                    sym = Symbol(tag_name, "tag", t, node.location)
                    self.scope.tags[tag_name] = sym
                else:
                    t = sym.type
                    if is_union and not isinstance(types.strip_qualifiers(t), UnionType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
                    if not is_union and not isinstance(types.strip_qualifiers(t), StructType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
            else:
                # definition: declare/complete in current scope; if an incomplete tag of same kind
                # is already visible, prefer completing that visible tag (common file-scope pattern)
                sym = self.scope.lookup_tag_local(tag_name)
                if sym is None:
                    visible = self.scope.lookup_tag(tag_name)
                    if visible is not None:
                        vt = types.strip_qualifiers(visible.type)
                        if (is_union and isinstance(vt, UnionType)) or ((not is_union) and isinstance(vt, StructType)):
                            sym = visible
                        else:
                            raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
                    if sym is None:
                        if is_union:
                            t = UnionType(tag_name, ctx.new_tag_id(), complete=False)
                        else:
                            t = StructType(tag_name, ctx.new_tag_id(), complete=False)
                        sym = Symbol(tag_name, "tag", t, node.location)
                        self.scope.tags[tag_name] = sym
                else:
                    t = sym.type
                    if is_union and not isinstance(types.strip_qualifiers(t), UnionType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
                    if not is_union and not isinstance(types.strip_qualifiers(t), StructType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
        else:
            if is_union:
                t = UnionType(None, ctx.new_tag_id(), complete=False)
            else:
                t = StructType(None, ctx.new_tag_id(), complete=False)
            sym = Symbol(f"<anon-{t.tag_id}>", "tag", t, node.location)  # type: ignore[attr-defined]

        rec = types.strip_qualifiers(sym.type)
        if node.members is not None:
            if rec.complete:  # type: ignore[attr-defined]
                raise self.error(node.location, f"redefinition of {'union' if is_union else 'struct'} '{tag_name or '<anonymous>'}'")
            self.complete_record(rec, node.members, node.location, is_union=is_union)  # type: ignore[arg-type]
            sym.type = rec
        node.resolved_type = sym.type  # type: ignore[attr-defined]
        node.symbol = sym  # type: ignore[attr-defined]
        return sym.type

    def complete_record(self, rec: Union[StructType, UnionType], members: Sequence[ast.StructMember], loc: SourceLocation, is_union: bool) -> None:
        ctx = self.ctx
        field_map: Dict[str, FieldInfo] = {}
        offset = 0
        max_align = 1
        max_size = 0
        # simple layout (no bit-field packing complexity beyond basic placement/alignment by unit)
        current_bit_unit_offset = 0
        current_bit_unit_size = 0
        current_bit_offset = 0
        for m in members:
            base_type = self.analyze_type_specifiers(m.specifiers, m.location)
            base_type = base_type.add_qualifiers(self.qualifiers_from_ast(m.specifiers.qualifiers))
            if not m.declarators:
                # anonymous member or type-only member? allow struct/union field without declarator only in extensions; diagnose for now unless bitfield? keep simple
                raise self.error(m.location, "member declaration must have a declarator")
            for sd in m.declarators:
                ftype = base_type
                fname = None
                bit_width = None
                if sd.bit_width is not None:
                    bw = self.eval_integer_constant(sd.bit_width, require_constant=True)
                    if bw is None or bw < 0:
                        raise self.error(sd.location, "bit-field width must be a non-negative integer constant")
                    bit_width = int(bw)
                if sd.declarator is not None:
                    ftype, fname = self.analyze_declarator(base_type, sd.declarator)
                else:
                    if bit_width is None:
                        raise self.error(sd.location, "member declarator required")
                    fname = None
                # Flexible array member check: incomplete array as last member in struct
                is_fam = isinstance(types.strip_qualifiers(ftype), ArrayType) and types.strip_qualifiers(ftype).length is None
                if is_fam:
                    if is_union:
                        raise self.error(sd.location, "flexible array member not allowed in union")
                    # must be last member - validated after loop? easier track index
                if fname and fname in field_map:
                    raise self.error(sd.location, f"duplicate member '{fname}'")
                if bit_width is not None:
                    if not types.is_integer_type(ftype):
                        raise self.error(sd.location, "bit-field has invalid type")
                    unit_size = ftype.sizeof() or 4
                    unit_align = ftype.alignof() or 4
                    max_align = max(max_align, unit_align)
                    if bit_width == 0:
                        # unnamed zero-width bit-field forces alignment to next unit boundary (simplified)
                        if current_bit_unit_size:
                            if is_union:
                                max_size = max(max_size, current_bit_unit_offset + current_bit_unit_size)
                            else:
                                offset = current_bit_unit_offset + current_bit_unit_size
                            current_bit_unit_size = 0
                            current_bit_offset = 0
                        continue
                    if bit_width > unit_size * 8:
                        raise self.error(sd.location, "bit-field width exceeds type width")
                    if current_bit_unit_size == 0 or current_bit_offset + bit_width > current_bit_unit_size * 8:
                        # start new unit
                        if not is_union:
                            # align offset
                            offset = _align_up(offset, unit_align)
                            current_bit_unit_offset = offset
                            offset += unit_size
                        else:
                            current_bit_unit_offset = 0
                        current_bit_unit_size = unit_size
                        current_bit_offset = 0
                        max_size = max(max_size, current_bit_unit_offset + current_bit_unit_size)
                    finfo = FieldInfo(fname, ftype, current_bit_unit_offset, bit_width, current_bit_offset)
                    current_bit_offset += bit_width
                else:
                    # flush pending bit-field unit alignment in struct layout (units already advanced)
                    if isinstance(types.strip_qualifiers(ftype), FunctionType):
                        raise self.error(sd.location, "field declared as a function")
                    if not is_union and types.strip_qualifiers(ftype).is_incomplete() and not is_fam:
                        raise self.error(sd.location, "field has incomplete type")
                    if is_union and types.strip_qualifiers(ftype).is_incomplete():
                        raise self.error(sd.location, "union field has incomplete type")
                    fsz = ftype.sizeof() if not is_fam else 0
                    fal = ftype.alignof() or 1
                    if fsz is None and not is_fam:
                        raise self.error(sd.location, "cannot determine size of member")
                    max_align = max(max_align, fal)
                    if not is_union:
                        offset = _align_up(offset, fal)
                        finfo = FieldInfo(fname, ftype, offset, None, None)
                        if not is_fam:
                            offset += int(fsz or 0)
                    else:
                        finfo = FieldInfo(fname, ftype, 0, None, None)
                    if not is_fam:
                        max_size = max(max_size, (finfo.offset if not is_union else 0) + int(fsz or 0))
                    else:
                        max_size = max(max_size, finfo.offset)
                if fname:
                    field_map[fname] = finfo
                rec.fields.append(finfo)
        # FAM must be last (struct only)
        if not is_union and rec.fields:
            fam_count = sum(1 for f in rec.fields if isinstance(types.strip_qualifiers(f.type), ArrayType) and types.strip_qualifiers(f.type).length is None)
            if fam_count:
                last = types.strip_qualifiers(rec.fields[-1].type)
                if fam_count > 1 or not (isinstance(last, ArrayType) and last.length is None):
                    raise self.error(loc, "flexible array member must be the last member of a struct")
        if is_union:
            size = _align_up(max_size, max_align)
        else:
            size = _align_up(offset if offset else max_size, max_align)
        rec.complete = True
        rec.size = size
        rec.align = max_align

    def analyze_enum_specifier(self, node: ast.EnumSpecifier) -> Type:
        ctx = self.ctx
        tag_name = node.name
        sym = None
        if tag_name:
            if node.enumerators is None:
                sym = self.scope.lookup_tag(tag_name)
                if sym is None:
                    et = EnumType(tag_name, ctx.new_tag_id(), ctx.int_t, complete=False)
                    sym = Symbol(tag_name, "tag", et, node.location)
                    self.scope.tags[tag_name] = sym
                else:
                    et = types.strip_qualifiers(sym.type)
                    if not isinstance(et, EnumType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
            else:
                sym = self.scope.lookup_tag_local(tag_name)
                if sym is None:
                    visible = self.scope.lookup_tag(tag_name)
                    if visible is not None and isinstance(types.strip_qualifiers(visible.type), EnumType):
                        sym = visible
                    elif visible is not None:
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
                    if sym is None:
                        et = EnumType(tag_name, ctx.new_tag_id(), ctx.int_t, complete=False)
                        sym = Symbol(tag_name, "tag", et, node.location)
                        self.scope.tags[tag_name] = sym
                else:
                    et = types.strip_qualifiers(sym.type)
                    if not isinstance(et, EnumType):
                        raise self.error(node.location, f"tag '{tag_name}' previously declared as different kind")
        else:
            et = EnumType(None, ctx.new_tag_id(), ctx.int_t, complete=False)
            sym = Symbol(f"<enum-{et.tag_id}>", "tag", et, node.location)

        et = types.strip_qualifiers(sym.type)
        if node.enumerators is not None:
            if not isinstance(et, EnumType):
                raise self.error(node.location, "invalid enum type")
            if et.complete:
                raise self.error(node.location, f"redefinition of enum '{tag_name or '<anonymous>'}'")
            next_val = 0
            consts = []
            for e in node.enumerators:
                if e.value is not None:
                    val = self.eval_integer_constant(e.value, require_constant=True)
                    if val is None:
                        raise self.error(e.location if hasattr(e, 'location') else node.location, "enum constant must be an integer constant expression")
                    next_val = int(val)
                ev = next_val
                ec = Symbol(e.name, "enum_const", et, e.location if hasattr(e, 'location') else node.location, enum_value=ev, defined=True)
                existing = self.scope.lookup_ordinary_local(e.name)
                if existing is not None:
                    raise self.error(e.location if hasattr(e, 'location') else node.location, f"redeclaration of enumerator '{e.name}'")
                self.scope.ordinary[e.name] = ec
                consts.append(types.EnumConst(e.name, ev, et))
                next_val = ev + 1
            et.enumerators = consts
            et.complete = True
            sym.type = et
        node.resolved_type = sym.type  # type: ignore[attr-defined]
        node.symbol = sym  # type: ignore[attr-defined]
        return sym.type

    # ------------------------------------------------------------------
    # Declarators
    # ------------------------------------------------------------------

    def analyze_declarator(self, base_type: Type, declarator: ast.Declarator, in_typedef: bool = False) -> Tuple[Type, Optional[str]]:
        """Build the semantic type of a declarator.

        C declarators are read from the identifier outward: suffixes such as
        ``[N]`` and ``(params)`` bind before a pointer prefix, while parentheses
        regroup that order. We collect type constructors in that reading order,
        then apply them in reverse to the declaration-specifier type. This keeps
        pointer-to-array distinct from array-of-pointer and pointer-to-function
        distinct from function-returning-pointer.
        """
        chain = self._collect_declarator_constructors(declarator)
        t = base_type
        for kind, payload in reversed(chain):
            t = self._apply_type_constructor(t, kind, payload)
        declarator.resolved_type = t  # type: ignore[attr-defined]
        name = self._find_declarator_name(declarator.direct)
        if name:
            declarator.name = name  # type: ignore[attr-defined]
        return t, name

    def _collect_declarator_constructors(self, declarator: ast.Declarator) -> List[Tuple[str, object]]:
        """Return type constructors ordered from the identifier outward."""
        chain: List[Tuple[str, object]] = []
        if declarator.direct is not None:
            chain.extend(self._collect_direct_constructors(declarator.direct))
        # Pointer prefixes are written to the left of the direct declarator; the
        # rightmost star is closest to the identifier, so reverse the parsed list.
        for ptr in reversed(declarator.pointers):
            chain.append(("pointer", ptr))
        return chain

    def _collect_direct_constructors(self, direct: Optional[ast.DirectDeclarator]) -> List[Tuple[str, object]]:
        if direct is None:
            return []
        if isinstance(direct, (ast.IdentifierDeclarator, ast.AbstractDeclaratorNode)):
            return []
        if isinstance(direct, ast.ParenthesizedDeclarator):
            # Parentheses regroup an inner declarator; its constructors are the
            # next ones encountered when reading outward from the identifier.
            return self._collect_declarator_constructors(direct.inner)
        if isinstance(direct, ast.ArrayDeclarator):
            return self._collect_direct_constructors(direct.base) + [("array", direct)]
        if isinstance(direct, ast.FunctionDeclarator):
            return self._collect_direct_constructors(direct.base) + [("function", direct)]
        return []

    def _apply_type_constructor(self, base: Type, kind: str, payload: object) -> Type:
        if kind == "pointer":
            ptr = payload
            assert isinstance(ptr, ast.Pointer)
            return PointerType(base, qualifiers=self.qualifiers_from_ast(ptr.qualifiers))
        if kind == "array":
            node = payload
            assert isinstance(node, ast.ArrayDeclarator)
            length = None
            if node.size is not None:
                val = self.eval_integer_constant(node.size, require_constant=False)
                if val is not None:
                    if val < 0:
                        raise self.error(node.location, "array size must be non-negative")
                    length = int(val)
                else:
                    # VLA or non-constant bound: analyze the expression but keep the
                    # static layout incomplete (runtime-sized bounds are not lowered).
                    self.analyze_expression(node.size)
            stripped = types.strip_qualifiers(base)
            if isinstance(stripped, FunctionType):
                raise self.error(node.location, "array element cannot be a function type")
            if isinstance(stripped, VoidType):
                raise self.error(node.location, "array element cannot have void type")
            return ArrayType(base, length)
        if kind == "function":
            node = payload
            assert isinstance(node, ast.FunctionDeclarator)
            stripped = types.strip_qualifiers(base)
            if isinstance(stripped, ArrayType):
                raise self.error(node.location, "function cannot return array type")
            if isinstance(stripped, FunctionType):
                raise self.error(node.location, "function cannot return function type")
            params, variadic, no_proto = self.analyze_parameter_list(node.params, node.location)
            return FunctionType(base, params, variadic=variadic, no_prototype=no_proto)
        raise AssertionError(f"unknown type constructor {kind}")

    def _find_declarator_name(self, direct: object) -> Optional[str]:
        if direct is None:
            return None
        if isinstance(direct, ast.Declarator):
            return self._find_declarator_name(direct.direct)
        if isinstance(direct, ast.IdentifierDeclarator):
            return direct.name
        if isinstance(direct, ast.ParenthesizedDeclarator):
            return self._find_declarator_name(direct.inner)
        if isinstance(direct, (ast.ArrayDeclarator, ast.FunctionDeclarator)):
            return self._find_declarator_name(direct.base)
        return None

    def analyze_parameter_list(self, params_node: Optional[ast.ParameterList], loc: SourceLocation) -> Tuple[List[ParameterType], bool, bool]:
        if params_node is None:
            return [], False, True
        params: List[ParameterType] = []
        variadic = params_node.variadic
        for p in params_node.params:
            ptype = self.analyze_type_specifiers(p.specifiers, p.location)
            ptype = ptype.add_qualifiers(self.qualifiers_from_ast(p.specifiers.qualifiers))
            name = None
            if p.declarator is not None:
                ptype, name = self.analyze_declarator(ptype, p.declarator)
            elif p.abstract_declarator is not None:
                ptype, _ = self.analyze_declarator(ptype, p.abstract_declarator)
            # adjust parameters: array -> pointer, function -> pointer
            ptype_stripped = types.strip_qualifiers(ptype)
            if isinstance(ptype_stripped, ArrayType):
                ptype = PointerType(ptype_stripped.element, qualifiers=ptype.qualifiers)
            elif isinstance(ptype_stripped, FunctionType):
                ptype = PointerType(ptype_stripped, qualifiers=ptype.qualifiers)
            if isinstance(types.strip_qualifiers(ptype), VoidType):
                # void parameter only allowed as sole parameter without name
                if len(params_node.params) != 1 or variadic or name is not None:
                    raise self.error(p.location, "'void' must be the only parameter")
                return [], False, False
            params.append(ParameterType(name, ptype))
        return params, variadic, False

    # ------------------------------------------------------------------
    # Declarations and definitions
    # ------------------------------------------------------------------

    def analyze_declaration(self, decl: ast.Declaration, is_file_scope: bool = False) -> None:
        loc = decl.location
        spec = decl.specifiers
        base_type = self.analyze_type_specifiers(spec, loc)
        base_type = base_type.add_qualifiers(self.qualifiers_from_ast(spec.qualifiers))
        storage_names = [s.name for s in spec.storage]
        is_typedef = "typedef" in storage_names
        if is_typedef and len(storage_names) > 1:
            # typedef with other storage classes not supported
            pass
        if not decl.init_declarators:
            # struct/union/enum tag declaration or empty declaration
            decl.resolved_type = base_type  # type: ignore[attr-defined]
            return
        for init_decl in decl.init_declarators:
            dtype, name = self.analyze_declarator(base_type, init_decl.declarator, in_typedef=is_typedef)
            init_decl.resolved_type = dtype  # type: ignore[attr-defined]
            if name is None:
                # abstract declarator in declaration context is unusual except maybe typedef? diagnose
                raise self.error(init_decl.location, "declarator has no identifier")
            if is_typedef:
                existing = self.scope.lookup_ordinary_local(name)
                if existing is not None:
                    if existing.kind != "typedef" or not types.compatible_types(existing.type, dtype):
                        raise self.error(init_decl.location, f"conflicting typedef '{name}'")
                else:
                    self.scope.ordinary[name] = Symbol(name, "typedef", dtype, init_decl.location, storage="typedef", defined=True)
                continue
            # object/function declaration
            if isinstance(types.strip_qualifiers(dtype), FunctionType):
                self.declare_function(name, dtype, init_decl.location, storage_names, is_file_scope)
                if init_decl.initializer is not None:
                    raise self.error(init_decl.location, "function declaration cannot have an initializer")
            else:
                self.declare_object(name, dtype, init_decl.location, storage_names, init_decl.initializer, is_file_scope)

    def declare_object(self, name: str, dtype: Type, loc: SourceLocation, storage_names: Sequence[str], initializer, is_file_scope: bool) -> None:
        if isinstance(types.strip_qualifiers(dtype), VoidType):
            raise self.error(loc, "variable has incomplete type 'void'")
        if types.strip_qualifiers(dtype).is_incomplete() and initializer is None:
            # Incomplete object types are only valid for `extern` declarations (or
            # tentative file-scope declarations of e.g. arrays that may complete later).
            if "extern" not in storage_names and not (is_file_scope and isinstance(types.strip_qualifiers(dtype), ArrayType)):
                raise self.error(loc, f"object '{name}' has incomplete type")
        existing = self.scope.lookup_ordinary_local(name)
        sym = existing
        if existing is not None:
            if existing.kind == "typedef":
                raise self.error(loc, f"'{name}' redeclared as different kind of symbol")
            if not types.compatible_types(existing.type, dtype):
                raise self.error(loc, f"conflicting types for '{name}'")
            existing.type = types.composite_type(existing.type, dtype)
            sym = existing
        else:
            sym = Symbol(name, "object", dtype, loc, storage=(storage_names[0] if storage_names else None))
            self.scope.ordinary[name] = sym
        if initializer is not None:
            if sym.defined and self.scope.kind == "file":
                # tentative vs definition rules simplified
                pass
            self.analyze_initializer(sym.type, initializer, loc)
            sym.defined = True

    def declare_function(self, name: str, ftype: Type, loc: SourceLocation, storage_names: Sequence[str], is_file_scope: bool) -> None:
        existing = self.scope.lookup_ordinary_local(name)
        if existing is not None:
            if existing.kind not in ("function",):
                raise self.error(loc, f"'{name}' redeclared as different kind of symbol")
            if not types.compatible_types(existing.type, ftype):
                raise self.error(loc, f"conflicting types for function '{name}'")
            existing.type = types.composite_type(existing.type, ftype)
        else:
            sym = Symbol(name, "function", ftype, loc, storage=(storage_names[0] if storage_names else None))
            self.scope.ordinary[name] = sym

    def analyze_function_definition(self, fn: ast.FunctionDefinition) -> None:
        base_type = self.analyze_type_specifiers(fn.specifiers, fn.location)
        base_type = base_type.add_qualifiers(self.qualifiers_from_ast(fn.specifiers.qualifiers))
        ftype, name = self.analyze_declarator(base_type, fn.declarator)
        if name is None or not isinstance(types.strip_qualifiers(ftype), FunctionType):
            raise self.error(fn.location, "function definition requires a function declarator with a name")
        # declare in file scope (or current scope)
        existing = self.file_scope.lookup_ordinary_local(name)
        if existing is not None:
            if existing.kind != "function":
                raise self.error(fn.location, f"'{name}' redeclared as different kind of symbol")
            if not types.compatible_types(existing.type, ftype):
                raise self.error(fn.location, f"conflicting types for function '{name}'")
            if existing.defined:
                raise self.error(fn.location, f"redefinition of function '{name}'")
            existing.type = types.composite_type(existing.type, ftype)
            sym = existing
        else:
            sym = Symbol(name, "function", ftype, fn.location, defined=False)
            self.file_scope.ordinary[name] = sym
        sym.defined = True
        self.current_function = sym
        ft = types.strip_qualifiers(ftype)
        assert isinstance(ft, FunctionType)
        self.current_return_type = ft.return_type
        # function scope for labels/params/body
        fn_scope = self.push_scope("function")
        self.label_defs = {}
        self.label_refs = []
        # parameters
        for p in ft.params:
            if p.name:
                if self.scope.lookup_ordinary_local(p.name):
                    raise self.error(fn.location, f"redefinition of parameter '{p.name}'")
                self.scope.ordinary[p.name] = Symbol(p.name, "object", p.type, fn.location, defined=True)
        # analyze body
        self.analyze_compound_statement(fn.body, new_scope=False)
        # check gotos
        for lname, lloc in self.label_refs:
            if lname not in self.label_defs:
                raise self.error(lloc, f"use of undeclared label '{lname}'")
        self.pop_scope()  # function scope
        self.current_function = None
        self.current_return_type = None
        fn.resolved_type = ftype  # type: ignore[attr-defined]
        fn.symbol = sym  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def analyze_compound_statement(self, stmt: ast.CompoundStatement, new_scope: bool = True) -> None:
        if new_scope:
            self.push_scope("block")
        for item in stmt.items:
            if isinstance(item, ast.Declaration):
                self.analyze_declaration(item, is_file_scope=False)
            else:
                self.analyze_statement(item)
        if new_scope:
            self.pop_scope()

    def analyze_statement(self, stmt: ast.Statement) -> None:
        if isinstance(stmt, ast.CompoundStatement):
            self.analyze_compound_statement(stmt, new_scope=True)
            return
        if isinstance(stmt, ast.ExpressionStatement):
            if stmt.expression is not None:
                self.analyze_expression(stmt.expression)
            return
        if isinstance(stmt, ast.IfStatement):
            cond_t = self.analyze_expression(stmt.condition)
            if not types.is_scalar_type(cond_t):
                raise self.error(stmt.location, "controlling expression of if statement must have scalar type")
            self.analyze_statement(stmt.then_branch)
            if stmt.else_branch:
                self.analyze_statement(stmt.else_branch)
            return
        if isinstance(stmt, ast.WhileStatement):
            cond_t = self.analyze_expression(stmt.condition)
            if not types.is_scalar_type(cond_t):
                raise self.error(stmt.location, "controlling expression of while statement must have scalar type")
            self.loop_depth += 1
            self.analyze_statement(stmt.body)
            self.loop_depth -= 1
            return
        if isinstance(stmt, ast.DoWhileStatement):
            self.loop_depth += 1
            self.analyze_statement(stmt.body)
            self.loop_depth -= 1
            cond_t = self.analyze_expression(stmt.condition)
            if not types.is_scalar_type(cond_t):
                raise self.error(stmt.location, "controlling expression of do-while statement must have scalar type")
            return
        if isinstance(stmt, ast.ForStatement):
            self.push_scope("block")
            if stmt.init is not None:
                if isinstance(stmt.init, ast.Declaration):
                    self.analyze_declaration(stmt.init, is_file_scope=False)
                else:
                    self.analyze_expression(stmt.init)
            if stmt.condition is not None:
                ct = self.analyze_expression(stmt.condition)
                if not types.is_scalar_type(ct):
                    raise self.error(stmt.location, "controlling expression of for statement must have scalar type")
            if stmt.step is not None:
                self.analyze_expression(stmt.step)
            self.loop_depth += 1
            self.analyze_statement(stmt.body)
            self.loop_depth -= 1
            self.pop_scope()
            return
        if isinstance(stmt, ast.ReturnStatement):
            if self.current_return_type is None:
                raise self.error(stmt.location, "return statement not within a function")
            rt = types.strip_qualifiers(self.current_return_type)
            if stmt.value is None:
                if not isinstance(rt, VoidType):
                    # allow in some C modes? be lenient-ish? better to diagnose non-void missing value
                    raise self.error(stmt.location, "non-void function should return a value")
            else:
                vt = self.analyze_expression(stmt.value)
                if isinstance(rt, VoidType):
                    raise self.error(stmt.location, "void function should not return a value")
                if not self.is_assignable(rt, vt, stmt.value, check_const=False):
                    raise self.error(stmt.location, "incompatible return type")
            return
        if isinstance(stmt, ast.BreakStatement):
            if self.loop_depth <= 0 and self.switch_depth <= 0:
                raise self.error(stmt.location, "'break' statement not in loop or switch statement")
            return
        if isinstance(stmt, ast.ContinueStatement):
            if self.loop_depth <= 0:
                raise self.error(stmt.location, "'continue' statement not in loop statement")
            return
        if isinstance(stmt, ast.SwitchStatement):
            ct = self.analyze_expression(stmt.condition)
            if not types.is_integer_type(ct):
                raise self.error(stmt.location, "controlling expression of switch statement must have integer type")
            self.switch_depth += 1
            self.switch_stack.append({"cases": set(), "has_default": False})
            self.analyze_statement(stmt.body)
            self.switch_stack.pop()
            self.switch_depth -= 1
            return
        if isinstance(stmt, ast.CaseStatement):
            if self.switch_depth <= 0 or not self.switch_stack:
                raise self.error(stmt.location, "'case' statement not in switch statement")
            val = self.eval_integer_constant(stmt.value, require_constant=True)
            if val is None:
                raise self.error(stmt.location, "case label must be an integer constant expression")
            ival = int(val)
            cases = self.switch_stack[-1]["cases"]
            assert isinstance(cases, set)
            if ival in cases:
                raise self.error(stmt.location, "duplicate case value in switch statement")
            cases.add(ival)
            if stmt.body is not None:
                self.analyze_statement(stmt.body)
            return
        if isinstance(stmt, ast.DefaultStatement):
            if self.switch_depth <= 0 or not self.switch_stack:
                raise self.error(stmt.location, "'default' statement not in switch statement")
            if self.switch_stack[-1]["has_default"]:
                raise self.error(stmt.location, "multiple default labels in switch statement")
            self.switch_stack[-1]["has_default"] = True
            if stmt.body is not None:
                self.analyze_statement(stmt.body)
            return
        if isinstance(stmt, ast.GotoStatement):
            self.label_refs.append((stmt.label, stmt.location))
            return
        if isinstance(stmt, ast.LabelStatement):
            if stmt.label in self.label_defs:
                raise self.error(stmt.location, f"duplicate label '{stmt.label}'")
            sym = Symbol(stmt.label, "label", self.ctx.void_t, stmt.location, defined=True)
            self.label_defs[stmt.label] = sym
            self.function_scope().labels[stmt.label] = sym
            self.analyze_statement(stmt.body)
            return
        # DeclarationStatement wrapper not used by parser currently, but handle if present
        if hasattr(stmt, 'declaration') and isinstance(getattr(stmt, 'declaration', None), ast.Declaration):
            self.analyze_declaration(stmt.declaration)  # type: ignore[arg-type]
            return
        # unknown statement kinds ignored/diagnosed
        raise self.error(getattr(stmt, 'location', SourceLocation()), "unsupported statement")

    # ------------------------------------------------------------------
    # Initializers
    # ------------------------------------------------------------------

    def analyze_initializer(self, target_type: Type, init: ast.Initializer, loc: SourceLocation) -> None:
        t = types.strip_qualifiers(target_type)
        if isinstance(init, ast.InitializerList):
            if isinstance(t, ArrayType):
                # basic validation
                count = 0
                for item in init.items:
                    if isinstance(item, ast.DesignatedInitializer):
                        # limited support: accept and analyze initializer expression
                        self.analyze_initializer(t.element, item.initializer, loc)
                        # index designator may extend array
                    else:
                        self.analyze_initializer(t.element, item, loc)
                    count += 1
                if t.length is not None and count > t.length:
                    raise self.error(loc, "excess elements in array initializer")
                return
            if isinstance(t, (StructType, UnionType)):
                if not t.complete:
                    raise self.error(loc, "initializer for incomplete aggregate type")
                fields = t.fields
                idx = 0
                for item in init.items:
                    if isinstance(item, ast.DesignatedInitializer):
                        # find field by designator .name if present
                        if item.designators and item.designators[0].field:
                            fname = item.designators[0].field
                            fmap = t.field_map() if isinstance(t, StructType) else t.field_map()
                            if fname not in fmap:
                                raise self.error(loc, f"field '{fname}' not found in aggregate")
                            ftype = fmap[fname].type
                            self.analyze_initializer(ftype, item.initializer, loc)
                            # reset idx naively
                            # compute idx position
                            for i, f in enumerate(fields):
                                if f.name == fname:
                                    idx = i + 1
                                    break
                        else:
                            if idx < len(fields):
                                self.analyze_initializer(fields[idx].type, item.initializer, loc)
                                idx += 1
                            else:
                                raise self.error(loc, "excess elements in struct/union initializer")
                    else:
                        if isinstance(t, UnionType):
                            if idx >= 1 and len(fields) >= 1:
                                # union initializes first member unless designated; extra is excess
                                if idx >= 1:
                                    raise self.error(loc, "excess elements in union initializer")
                            if fields:
                                self.analyze_initializer(fields[0].type, item, loc)
                                idx = 1
                            continue
                        if idx >= len(fields):
                            raise self.error(loc, "excess elements in struct initializer")
                        self.analyze_initializer(fields[idx].type, item, loc)
                        idx += 1
                return
            # scalar with braces: allow single element
            if len(init.items) == 1:
                item = init.items[0]
                if isinstance(item, ast.DesignatedInitializer):
                    self.analyze_initializer(target_type, item.initializer, loc)
                else:
                    self.analyze_initializer(target_type, item, loc)
                return
            if len(init.items) == 0:
                return
            raise self.error(loc, "invalid brace-enclosed initializer for scalar type")
        else:
            # expression initializer
            expr = init
            et = self.analyze_expression(expr)
            if not self.is_assignable(target_type, et, expr, check_const=False):
                # string literal to char array special case
                if isinstance(t, ArrayType) and isinstance(types.strip_qualifiers(t.element), IntegerType):
                    if isinstance(expr, ast.StringLiteral):
                        return
                raise self.error(loc, "incompatible initializer type")

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def analyze_expression(self, expr: ast.Expression) -> Type:
        t = self._analyze_expression_inner(expr)
        expr.resolved_type = t  # type: ignore[attr-defined]
        # Annotate safely computable integer constant expressions (not just
        # literals) so later stages can use folded values for case labels, array
        # bounds, and enum initializers. Non-constant expressions are left alone.
        cv = self.eval_integer_constant(expr)
        if cv is not None:
            expr.const_value = cv  # type: ignore[attr-defined]
        return t

    def _analyze_expression_inner(self, expr: ast.Expression) -> Type:
        ctx = self.ctx
        if isinstance(expr, ast.IntegerLiteral):
            # determine type by value/suffix (simplified)
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            expr.const_value = expr.number  # type: ignore[attr-defined]
            # default int/long/long long based on value
            v = expr.number if expr.number is not None else 0
            suf = expr.value.lower()
            unsigned = "u" in suf
            long_count = suf.count("l")
            if long_count >= 2:
                return ctx.ullong_t if unsigned else ctx.llong_t
            if long_count == 1:
                return ctx.ulong_t if unsigned else ctx.long_t
            if unsigned:
                return ctx.uint_t
            # signed int if fits, else long
            if -2147483648 <= v <= 2147483647:
                return ctx.int_t
            return ctx.long_t
        if isinstance(expr, ast.CharacterLiteral):
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            expr.const_value = expr.number  # type: ignore[attr-defined]
            return ctx.int_t
        if isinstance(expr, ast.StringLiteral):
            expr.value_category = "lvalue"  # type: ignore[attr-defined]
            # array of char including null terminator
            n = len(expr.value.encode("utf-8")) + 1
            return ArrayType(ctx.char_t, n)
        if isinstance(expr, ast.IdentifierExpression):
            sym = self.scope.lookup_ordinary(expr.name)
            if sym is None:
                raise self.error(expr.location, f"undeclared identifier '{expr.name}'")
            expr.symbol = sym  # type: ignore[attr-defined]
            expr.resolved_symbol = sym  # type: ignore[attr-defined]
            if sym.kind == "enum_const":
                expr.value_category = "rvalue"  # type: ignore[attr-defined]
                expr.const_value = sym.enum_value  # type: ignore[attr-defined]
                return ctx.int_t
            if isinstance(types.strip_qualifiers(sym.type), FunctionType):
                expr.value_category = "function"  # type: ignore[attr-defined]
            else:
                expr.value_category = "lvalue"  # type: ignore[attr-defined]
            return sym.type
        if isinstance(expr, ast.UnaryExpression):
            return self.analyze_unary(expr)
        if isinstance(expr, ast.BinaryExpression):
            return self.analyze_binary(expr)
        if isinstance(expr, ast.AssignmentExpression):
            return self.analyze_assignment(expr)
        if isinstance(expr, ast.ConditionalExpression):
            return self.analyze_conditional(expr)
        if isinstance(expr, ast.CastExpression):
            return self.analyze_cast(expr)
        if isinstance(expr, ast.CallExpression):
            return self.analyze_call(expr)
        if isinstance(expr, ast.SubscriptExpression):
            return self.analyze_subscript(expr)
        if isinstance(expr, ast.MemberAccessExpression):
            return self.analyze_member_access(expr)
        if isinstance(expr, ast.SizeofExpression):
            return self.analyze_sizeof(expr)
        if isinstance(expr, ast.CommaExpression):
            t = ctx.int_t
            for e in expr.expressions:
                t = self.analyze_expression(e)
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return t
        if isinstance(expr, ast.CompoundLiteral):
            ttype = self.analyze_type_name(expr.type_name)
            self.analyze_initializer(ttype, expr.initializer, expr.location)
            expr.value_category = "lvalue"  # type: ignore[attr-defined]
            return ttype
        if isinstance(expr, ast.GenericSelection):
            ct = self.analyze_expression(expr.controlling)
            ctd = types.strip_qualifiers(self.decay_expr_type(expr.controlling, ct))
            chosen: Optional[ast.GenericAssociation] = None
            default_assoc: Optional[ast.GenericAssociation] = None
            for assoc in expr.associations:
                if assoc.type_name is None:
                    if default_assoc is not None:
                        raise self.error(expr.location, "multiple default associations in generic selection")
                    default_assoc = assoc
                    continue
                at = self.analyze_type_name(assoc.type_name)
                if types.compatible_types(types.strip_qualifiers(at), ctd):
                    if chosen is not None:
                        # multiple compatible associations
                        raise self.error(expr.location, "multiple compatible associations in generic selection")
                    chosen = assoc
            sel = chosen or default_assoc
            if sel is None:
                raise self.error(expr.location, "no compatible association in generic selection")
            # analyze selected expression for type/result; in full C this is unevaluated for others, but keep simple
            rt = self.analyze_expression(sel.expression)
            expr.value_category = getattr(sel.expression, "value_category", "rvalue")  # type: ignore[attr-defined]
            expr.selected = sel  # type: ignore[attr-defined]
            return rt
        raise self.error(expr.location, f"unsupported expression node {type(expr).__name__}")

    def analyze_unary(self, expr: ast.UnaryExpression) -> Type:
        ctx = self.ctx
        if expr.postfix:
            ot = self.analyze_expression(expr.operand)
            if not self.is_modifiable_lvalue(expr.operand, ot):
                raise self.error(expr.location, "operand of postfix increment/decrement must be a modifiable lvalue")
            if not (types.is_scalar_type(ot)):
                raise self.error(expr.location, "invalid operand type for postfix increment/decrement")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ot
        op = expr.op
        ot = self.analyze_expression(expr.operand)
        if op in ("++", "--"):
            if not self.is_modifiable_lvalue(expr.operand, ot):
                raise self.error(expr.location, "operand of increment/decrement must be a modifiable lvalue")
            if not types.is_scalar_type(ot):
                raise self.error(expr.location, "invalid operand type for increment/decrement")
            expr.value_category = "lvalue" if False else "rvalue"  # C: prefix result is lvalue in C++? in C it's rvalue
            return ot
        if op == "+":
            if not types.is_arithmetic_type(ot):
                raise self.error(expr.location, "unary '+' requires arithmetic operand")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return types.integer_promotions(ot, ctx) if types.is_integer_type(ot) else ot
        if op == "-":
            if not types.is_arithmetic_type(ot):
                raise self.error(expr.location, "unary '-' requires arithmetic operand")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return types.integer_promotions(ot, ctx) if types.is_integer_type(ot) else ot
        if op == "~":
            if not types.is_integer_type(ot):
                raise self.error(expr.location, "bitwise not requires integer operand")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return types.integer_promotions(ot, ctx)
        if op == "!":
            if not types.is_scalar_type(ot):
                raise self.error(expr.location, "logical not requires scalar operand")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ctx.int_t
        if op == "&":
            # address-of: operand must be lvalue or function designator (or *expr in some cases)
            if not self.is_lvalue(expr.operand, ot) and not isinstance(types.strip_qualifiers(ot), FunctionType):
                # allow &*p
                if isinstance(expr.operand, ast.UnaryExpression) and expr.operand.op == "*" and not expr.operand.postfix:
                    pass
                else:
                    raise self.error(expr.location, "cannot take address of non-lvalue")
            base_t = ot
            if isinstance(types.strip_qualifiers(ot), FunctionType):
                base_t = ot
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return PointerType(ot)
        if op == "*":
            pt = self.decay_expr_type(expr.operand, ot)
            p = types.strip_qualifiers(pt)
            if not isinstance(p, PointerType):
                raise self.error(expr.location, "indirection requires pointer operand")
            if isinstance(types.strip_qualifiers(p.pointee), VoidType):
                raise self.error(expr.location, "dereferencing pointer to void")
            expr.value_category = "lvalue"  # type: ignore[attr-defined]
            return p.pointee
        raise self.error(expr.location, f"unsupported unary operator '{op}'")

    def analyze_binary(self, expr: ast.BinaryExpression) -> Type:
        ctx = self.ctx
        op = expr.op
        lt = self.analyze_expression(expr.left)
        rt = self.analyze_expression(expr.right)
        # apply usual conversions for arithmetic/logical comparisons as needed
        if op in ("*", "/", "%", "+", "-", "<<", ">>", "&", "^", "|"):
            pass
        expr.value_category = "rvalue"  # type: ignore[attr-defined]

        if op in ("*", "/", "%"):
            if not (types.is_arithmetic_type(lt) and types.is_arithmetic_type(rt)):
                raise self.error(expr.location, f"invalid operands to binary '{op}'")
            if op == "%" and not (types.is_integer_type(lt) and types.is_integer_type(rt)):
                raise self.error(expr.location, "operands of '%' must have integer type")
            return types.usual_arithmetic_conversions(lt, rt, ctx)
        if op == "+":
            ldt = self.decay_expr_type(expr.left, lt)
            rdt = self.decay_expr_type(expr.right, rt)
            if types.is_arithmetic_type(ldt) and types.is_arithmetic_type(rdt):
                return types.usual_arithmetic_conversions(ldt, rdt, ctx)
            if isinstance(types.strip_qualifiers(ldt), PointerType) and types.is_integer_type(rdt):
                return ldt
            if isinstance(types.strip_qualifiers(rdt), PointerType) and types.is_integer_type(ldt):
                return rdt
            raise self.error(expr.location, "invalid operands to binary '+'")
        if op == "-":
            ldt = self.decay_expr_type(expr.left, lt)
            rdt = self.decay_expr_type(expr.right, rt)
            if types.is_arithmetic_type(ldt) and types.is_arithmetic_type(rdt):
                return types.usual_arithmetic_conversions(ldt, rdt, ctx)
            if isinstance(types.strip_qualifiers(ldt), PointerType) and types.is_integer_type(rdt):
                return ldt
            if isinstance(types.strip_qualifiers(ldt), PointerType) and isinstance(types.strip_qualifiers(rdt), PointerType):
                # pointer difference -> ptrdiff_t
                return ctx.ptrdiff_t
            raise self.error(expr.location, "invalid operands to binary '-'")
        if op in ("<<", ">>"):
            if not (types.is_integer_type(lt) and types.is_integer_type(rt)):
                raise self.error(expr.location, "shift operands must have integer type")
            return types.integer_promotions(lt, ctx)
        if op in ("<", "<=", ">", ">="):
            ldt = self.decay_expr_type(expr.left, lt)
            rdt = self.decay_expr_type(expr.right, rt)
            if types.is_arithmetic_type(ldt) and types.is_arithmetic_type(rdt):
                return ctx.int_t
            if isinstance(types.strip_qualifiers(ldt), PointerType) and isinstance(types.strip_qualifiers(rdt), PointerType):
                return ctx.int_t
            raise self.error(expr.location, f"invalid operands to relational operator '{op}'")
        if op in ("==", "!="):
            ldt = self.decay_expr_type(expr.left, lt)
            rdt = self.decay_expr_type(expr.right, rt)
            if types.is_arithmetic_type(ldt) and types.is_arithmetic_type(rdt):
                return ctx.int_t
            if isinstance(types.strip_qualifiers(ldt), PointerType) or isinstance(types.strip_qualifiers(rdt), PointerType):
                # allow pointer-pointer, pointer-int0 in general? simplified
                return ctx.int_t
            raise self.error(expr.location, f"invalid operands to equality operator '{op}'")
        if op in ("&", "^", "|"):
            if not (types.is_integer_type(lt) and types.is_integer_type(rt)):
                raise self.error(expr.location, f"bitwise operator '{op}' requires integer operands")
            return types.usual_arithmetic_conversions(lt, rt, ctx)
        if op in ("&&", "||"):
            if not (types.is_scalar_type(lt) and types.is_scalar_type(rt)):
                raise self.error(expr.location, f"logical operator '{op}' requires scalar operands")
            return ctx.int_t
        raise self.error(expr.location, f"unsupported binary operator '{op}'")

    def analyze_assignment(self, expr: ast.AssignmentExpression) -> Type:
        ctx = self.ctx
        lt = self.analyze_expression(expr.left)
        rt = self.analyze_expression(expr.right)
        if not self.is_modifiable_lvalue(expr.left, lt):
            raise self.error(expr.location, "left operand of assignment must be a modifiable lvalue")
        if expr.op == "=":
            if not self.is_assignable(lt, rt, expr.right, check_const=False):
                raise self.error(expr.location, "incompatible types in assignment")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return lt
        # compound assignments: basic check
        binop = expr.op[:-1]
        # synthesize check by analyzing as binary in effect? simplified: require compatible arithmetic/pointer forms
        if binop in ("+", "-") and isinstance(types.strip_qualifiers(lt), PointerType) and types.is_integer_type(rt):
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return lt
        if not (types.is_arithmetic_type(lt) and types.is_arithmetic_type(rt)):
            raise self.error(expr.location, f"invalid operands to compound assignment '{expr.op}'")
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        return lt

    def analyze_conditional(self, expr: ast.ConditionalExpression) -> Type:
        ctx = self.ctx
        ct = self.analyze_expression(expr.condition)
        if not types.is_scalar_type(ct):
            raise self.error(expr.location, "conditional expression condition must have scalar type")
        tt = self.analyze_expression(expr.then_expr) if expr.then_expr is not None else ct
        et = self.analyze_expression(expr.else_expr)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        if types.is_arithmetic_type(tt) and types.is_arithmetic_type(et):
            return types.usual_arithmetic_conversions(tt, et, ctx)
        ttd = self.decay_expr_type(expr.then_expr, tt) if expr.then_expr else tt
        etd = self.decay_expr_type(expr.else_expr, et)
        if types.compatible_types(ttd, etd):
            return ttd
        if isinstance(types.strip_qualifiers(ttd), PointerType) and isinstance(types.strip_qualifiers(etd), PointerType):
            return ttd
        # void cases
        if isinstance(types.strip_qualifiers(tt), VoidType) and isinstance(types.strip_qualifiers(et), VoidType):
            return ctx.void_t
        return tt

    def analyze_cast(self, expr: ast.CastExpression) -> Type:
        ttype = self.analyze_type_name(expr.type_name)
        self.analyze_expression(expr.operand)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        return ttype

    def analyze_type_name(self, tn: ast.TypeName) -> Type:
        base = self.analyze_type_specifiers(tn.specifiers, tn.location)
        base = base.add_qualifiers(self.qualifiers_from_ast(tn.specifiers.qualifiers))
        if tn.declarator is not None:
            t, _ = self.analyze_declarator(base, tn.declarator)
            tn.resolved_type = t  # type: ignore[attr-defined]
            return t
        tn.resolved_type = base  # type: ignore[attr-defined]
        return base

    def analyze_call(self, expr: ast.CallExpression) -> Type:
        callee_t = self.analyze_expression(expr.callee)
        callee_t = self.decay_expr_type(expr.callee, callee_t)
        ct = types.strip_qualifiers(callee_t)
        if isinstance(ct, PointerType) and isinstance(types.strip_qualifiers(ct.pointee), FunctionType):
            ft = types.strip_qualifiers(ct.pointee)
        elif isinstance(ct, FunctionType):
            ft = ct
        else:
            raise self.error(expr.location, "called object is not a function or function pointer")
        assert isinstance(ft, FunctionType)
        args_t = [self.analyze_expression(a) for a in expr.arguments]
        if not ft.no_prototype:
            fixed = len(ft.params)
            if not ft.variadic and len(args_t) != fixed:
                raise self.error(expr.location, f"incorrect number of arguments to function call (expected {fixed}, got {len(args_t)})")
            if ft.variadic and len(args_t) < fixed:
                raise self.error(expr.location, f"too few arguments to variadic function call (expected at least {fixed}, got {len(args_t)})")
            for i, pt in enumerate(ft.params):
                at = args_t[i]
                if not self.is_assignable(pt.type, at, expr.arguments[i], check_const=False):
                    # allow basic pointer/integer cases leniently? keep diagnostic useful
                    pass
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        return ft.return_type

    def analyze_subscript(self, expr: ast.SubscriptExpression) -> Type:
        at = self.analyze_expression(expr.array)
        it = self.analyze_expression(expr.index)
        ad = self.decay_expr_type(expr.array, at)
        idd = self.decay_expr_type(expr.index, it)
        ap = types.strip_qualifiers(ad)
        ip = types.strip_qualifiers(idd)
        if isinstance(ap, PointerType) and types.is_integer_type(ip):
            elem = ap.pointee
        elif isinstance(ip, PointerType) and types.is_integer_type(ap):
            elem = ip.pointee
        else:
            raise self.error(expr.location, "subscripted value is not an array or pointer")
        if isinstance(types.strip_qualifiers(elem), VoidType):
            raise self.error(expr.location, "subscript of pointer to void")
        expr.value_category = "lvalue"  # type: ignore[attr-defined]
        return elem

    def analyze_member_access(self, expr: ast.MemberAccessExpression) -> Type:
        bt = self.analyze_expression(expr.base)
        if expr.pointer:
            bd = self.decay_expr_type(expr.base, bt)
            bp = types.strip_qualifiers(bd)
            if not isinstance(bp, PointerType):
                raise self.error(expr.location, "member access '->' requires pointer to struct/union")
            rec = types.strip_qualifiers(bp.pointee)
        else:
            rec = types.strip_qualifiers(bt)
        if not isinstance(rec, (StructType, UnionType)):
            raise self.error(expr.location, "member access requires struct or union type")
        if not rec.complete:
            raise self.error(expr.location, "member access on incomplete struct/union type")
        fmap = rec.field_map()
        if expr.member not in fmap:
            raise self.error(expr.location, f"no member named '{expr.member}'")
        f = fmap[expr.member]
        expr.symbol = f  # type: ignore[attr-defined]
        expr.value_category = "lvalue"  # type: ignore[attr-defined]
        # member type may inherit qualifiers from base in some cases; simplified keep field type
        return f.type

    def analyze_sizeof(self, expr: ast.SizeofExpression) -> Type:
        if isinstance(expr.operand, ast.TypeName):
            t = self.analyze_type_name(expr.operand)
        elif isinstance(expr.operand, ast.Expression):
            t = self.analyze_expression(expr.operand)
        else:
            raise self.error(expr.location, "invalid sizeof operand")
        # sizeof result type is size_t (unsigned long on Nova64)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        # do not require complete type in all unevaluated contexts in this simplified model, but usually required
        return self.ctx.size_t

    # ------------------------------------------------------------------
    # Helpers / checks
    # ------------------------------------------------------------------

    def is_lvalue(self, expr: ast.Expression, etype: Optional[Type] = None) -> bool:
        vc = getattr(expr, "value_category", None)
        if vc == "lvalue":
            return True
        # string literal is lvalue (array), identifier of object is lvalue (set above)
        if isinstance(expr, ast.IdentifierExpression):
            sym = getattr(expr, "symbol", None)
            if sym and sym.kind == "object":
                return True
        if isinstance(expr, ast.UnaryExpression) and expr.op == "*" and not expr.postfix:
            return True
        if isinstance(expr, ast.MemberAccessExpression):
            return True
        if isinstance(expr, ast.SubscriptExpression):
            return True
        if isinstance(expr, ast.StringLiteral):
            return True
        return False

    def is_modifiable_lvalue(self, expr: ast.Expression, etype: Type) -> bool:
        if not self.is_lvalue(expr, etype):
            return False
        t = etype
        if t.qualifiers.const:
            return False
        st = types.strip_qualifiers(t)
        if isinstance(st, ArrayType):
            return False
        if isinstance(st, VoidType):
            return False
        if isinstance(st, (StructType, UnionType)) and st.is_incomplete():
            return False
        # const-qualified fields not fully tracked here; basic check
        return True

    def decay_expr_type(self, expr: Optional[ast.Expression], t: Type) -> Type:
        # arrays/functions decay in many expression contexts; lvalue category not strictly required here
        st = types.strip_qualifiers(t)
        if isinstance(st, ArrayType):
            return PointerType(st.element)
        if isinstance(st, FunctionType):
            return PointerType(st)
        return t

    def is_assignable(self, lhs_type: Type, rhs_type: Type, rhs_expr: Optional[ast.Expression], check_const: bool = False) -> bool:
        lt = types.strip_qualifiers(lhs_type)
        rt = types.strip_qualifiers(rhs_type)
        rtd = self.decay_expr_type(rhs_expr, rhs_type)
        rtds = types.strip_qualifiers(rtd)
        if types.is_arithmetic_type(lt) and types.is_arithmetic_type(rtds):
            return True
        if isinstance(lt, (StructType, UnionType)) and types.compatible_types(lt, rtds):
            return True
        if isinstance(lt, PointerType) and isinstance(rtds, PointerType):
            # allow void* compatibility and compatible pointee types (simplified qualifiers)
            lp = lt.pointee
            rp = rtds.pointee
            if isinstance(types.strip_qualifiers(lp), VoidType) or isinstance(types.strip_qualifiers(rp), VoidType):
                return True
            if types.compatible_types(lp, rp):
                return True
            # allow char* special cases? conservative
            return False
        if isinstance(lt, PointerType) and isinstance(rhs_expr, ast.IntegerLiteral) and rhs_expr.number == 0:
            return True
        if isinstance(lt, BoolType) and types.is_scalar_type(rtds):
            return True
        return types.compatible_types(lhs_type, rhs_type)

    def eval_integer_constant(self, expr: ast.Expression, require_constant: bool = False) -> Optional[int]:
        # Try to evaluate constant integer expressions in a limited but useful form.
        if isinstance(expr, ast.IntegerLiteral):
            return int(expr.number or 0)
        if isinstance(expr, ast.CharacterLiteral):
            return int(expr.number or 0)
        if isinstance(expr, ast.IdentifierExpression):
            sym = self.scope.lookup_ordinary(expr.name) if hasattr(self, 'scope') else None
            if sym and sym.kind == "enum_const" and sym.enum_value is not None:
                return int(sym.enum_value)
            return None
        if isinstance(expr, ast.UnaryExpression) and not expr.postfix:
            v = self.eval_integer_constant(expr.operand, require_constant=False)
            if v is None:
                return None
            if expr.op == "+":
                return +v
            if expr.op == "-":
                return -v
            if expr.op == "~":
                return ~v
            if expr.op == "!":
                return 0 if v != 0 else 1
            return None
        if isinstance(expr, ast.BinaryExpression):
            l = self.eval_integer_constant(expr.left, False)
            r = self.eval_integer_constant(expr.right, False)
            if l is None or r is None:
                # logical ops can short-circuit in general, but for constant expr require both in simple cases
                if expr.op == "&&":
                    if l == 0:
                        return 0
                    if l is not None and r is not None:
                        return 1 if (l != 0 and r != 0) else 0
                    return None
                if expr.op == "||":
                    if l == 1 or (l is not None and l != 0):
                        return 1
                    if l is not None and r is not None:
                        return 1 if (l != 0 or r != 0) else 0
                    return None
                return None
            try:
                if expr.op == "+":
                    return l + r
                if expr.op == "-":
                    return l - r
                if expr.op == "*":
                    return l * r
                if expr.op == "/":
                    return l // r if r != 0 else None
                if expr.op == "%":
                    return l % r if r != 0 else None
                if expr.op == "<<":
                    return l << r
                if expr.op == ">>":
                    return l >> r
                if expr.op == "&":
                    return l & r
                if expr.op == "|":
                    return l | r
                if expr.op == "^":
                    return l ^ r
                if expr.op == "<":
                    return 1 if l < r else 0
                if expr.op == "<=":
                    return 1 if l <= r else 0
                if expr.op == ">":
                    return 1 if l > r else 0
                if expr.op == ">=":
                    return 1 if l >= r else 0
                if expr.op == "==":
                    return 1 if l == r else 0
                if expr.op == "!=":
                    return 1 if l != r else 0
                if expr.op == "&&":
                    return 1 if (l != 0 and r != 0) else 0
                if expr.op == "||":
                    return 1 if (l != 0 or r != 0) else 0
            except Exception:
                return None
            return None
        if isinstance(expr, ast.ConditionalExpression):
            c = self.eval_integer_constant(expr.condition, False)
            if c is None:
                return None
            if c != 0:
                te = expr.then_expr if expr.then_expr is not None else expr.condition
                return self.eval_integer_constant(te, False)
            return self.eval_integer_constant(expr.else_expr, False)
        if isinstance(expr, ast.CastExpression):
            v = self.eval_integer_constant(expr.operand, False)
            if v is not None:
                # model as 64-bit integer cast crudely
                return int(v)
            return None
        if isinstance(expr, ast.SizeofExpression):
            # try to compute sizeof for constant-known types
            if isinstance(expr.operand, ast.TypeName):
                # analyze type name in a temporary way? not safe without scope context in all cases
                return None
            return None
        return None


def _align_up(value: int, align: int) -> int:
    if align <= 0:
        return value
    rem = value % align
    return value if rem == 0 else value + (align - rem)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(tree: ast.TranslationUnit, target: str = "nova64") -> ast.TranslationUnit:
    analyzer = Analyzer(target=target)
    return analyzer.analyze_translation_unit(tree)
