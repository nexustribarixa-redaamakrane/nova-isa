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

from collections import Counter
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

    # -- semantic annotations / backend gate -------------------------------

    def note_conversion(
        self,
        expr: Optional[ast.Expression],
        target: Type,
        kind: str,
        source: Optional[Type] = None,
    ) -> None:
        """Record an implicit conversion for the lowering pass.

        ``resolved_type`` remains the expression's natural semantic type. The
        lowering pass consults ``implicit_cast_to`` when materialising the value
        so it need not redo C's promotion/conversion rules.
        """
        if expr is None:
            return
        if source is None:
            source = getattr(expr, "resolved_type", None)
        target_u = types.strip_qualifiers(target)
        if source is not None and types.type_eq(types.strip_qualifiers(source), target_u):
            return
        expr.implicit_cast_to = target_u  # type: ignore[attr-defined]
        expr.implicit_cast_kind = kind  # type: ignore[attr-defined]

    def require_lowering_supported(self, t: Type, loc: SourceLocation, what: str) -> None:
        """Reject types the Nova64 backend cannot lower, with a clear diagnostic."""
        st = types.strip_qualifiers(t)
        if isinstance(st, FloatingType):
            raise self.error(loc, f"floating-point {what} are not supported by the Nova64 backend")
        if isinstance(st, (ArrayType, FunctionType)):
            # Arrays/functions are valid only through decay or explicit
            # sizeof/address contexts; callers handle those before reaching here.
            return

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
        """Map a C specifier sequence to a Nova64 semantic type.

        A multiset is required: ``long long`` must not collapse to ``long`` just
        because Python sets remove the duplicate keyword.
        """
        ctx = self.ctx
        counts = Counter(names)
        s = set(counts)
        is_signed = "signed" in s
        is_unsigned = "unsigned" in s
        if is_signed and is_unsigned:
            raise self.error(loc, "both 'signed' and 'unsigned' specified")
        if counts["void"] == 1 and len(names) == 1:
            return ctx.void_t
        if counts["_Bool"] == 1 and len(names) == 1:
            return ctx.bool_t
        if counts["char"] == 1:
            if is_signed and not is_unsigned:
                return ctx.schar_t
            if is_unsigned:
                return ctx.uchar_t
            if not is_signed:
                return ctx.char_t
        if counts["short"] == 1 and "long" not in s and "double" not in s:
            if counts["int"] <= 1 and "char" not in s:
                if is_unsigned:
                    return ctx.ushort_t
                return ctx.short_t
        if counts["long"] == 2 and "double" not in s and "short" not in s:
            if counts["int"] <= 1 and "char" not in s:
                if is_unsigned:
                    return ctx.ullong_t
                return ctx.llong_t
        if counts["long"] == 1 and "double" not in s and "short" not in s:
            if counts["int"] <= 1 and "char" not in s:
                if is_unsigned:
                    return ctx.ulong_t
                return ctx.long_t
        if counts["int"] == 1 and not (s & {"char", "short", "long", "double", "float", "void", "_Bool"}):
            return ctx.uint_t if is_unsigned else ctx.int_t
        if counts["int"] == 0 and len(names) == 1 and s <= {"signed", "unsigned"}:
            # Plain `signed` is `signed int`; plain `unsigned` is `unsigned int`.
            return ctx.uint_t if is_unsigned else ctx.int_t
        if counts["float"] == 1 and len(names) == 1:
            return ctx.float_t
        if counts["double"] == 1 and "long" not in s and len(names) == 1:
            return ctx.double_t
        if counts["double"] == 1 and counts["long"] == 1 and len(names) == 2:
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
                        if fname is not None:
                            raise self.error(sd.location, "named bit-field cannot have zero width")
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
        # FAM must be last, and a struct containing one needs another named
        # member (C11 6.7.2.1p18). Anonymous bit-fields do not count.
        if not is_union and rec.fields:
            fam_count = sum(1 for f in rec.fields if isinstance(types.strip_qualifiers(f.type), ArrayType) and types.strip_qualifiers(f.type).length is None)
            if fam_count:
                last = types.strip_qualifiers(rec.fields[-1].type)
                if fam_count > 1 or not (isinstance(last, ArrayType) and last.length is None):
                    raise self.error(loc, "flexible array member must be the last member of a struct")
                named_count = sum(1 for f in rec.fields if f.name is not None and f.bit_width is None)
                if named_count < 2:
                    raise self.error(loc, "struct with a flexible array member must have more than one named member")
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
                    if not (-(1 << 31) <= int(val) <= (1 << 31) - 1):
                        raise self.error(
                            e.location if hasattr(e, 'location') else node.location,
                            "enumerator value is outside the range of the enum's int underlying type",
                        )
                    next_val = int(val)
                ev = next_val
                if not (-(1 << 31) <= ev <= (1 << 31) - 1):
                    raise self.error(
                        e.location if hasattr(e, 'location') else node.location,
                        "enumerator value is outside the range of the enum's int underlying type",
                    )
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
                    # A non-constant bound is a variable length array. Nova64
                    # lowering has no runtime-sized stack allocation, so reject
                    # it explicitly instead of reporting an incomplete type later.
                    self.analyze_expression(node.size)
                    raise self.error(
                        node.location,
                        "variable length arrays are not supported by the Nova64 backend",
                    )
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
            if stripped.is_incomplete() and not isinstance(stripped, VoidType):
                raise self.error(node.location, "function cannot return an incomplete type")
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
            if types.strip_qualifiers(ptype).is_incomplete():
                raise self.error(p.location, "parameter has incomplete type")
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
                if init_decl.initializer is not None:
                    dtype = self.complete_array_type_from_initializer(dtype, init_decl.initializer, init_decl.location)
                    init_decl.resolved_type = dtype  # type: ignore[attr-defined]
                    init_decl.declarator.resolved_type = dtype  # type: ignore[attr-defined]
                self.declare_object(name, dtype, init_decl.location, storage_names, init_decl.initializer, is_file_scope)
                init_decl.symbol = self.scope.lookup_ordinary_local(name)  # type: ignore[attr-defined]

    def complete_array_type_from_initializer(self, dtype: Type, initializer, loc: SourceLocation) -> Type:
        """Give an incomplete array type a length derived from its initializer."""
        t = types.strip_qualifiers(dtype)
        if not isinstance(t, ArrayType) or t.length is not None:
            return dtype
        length: Optional[int] = None
        if isinstance(initializer, ast.StringLiteral):
            length = len(initializer.value.encode("utf-8")) + 1
        elif isinstance(initializer, ast.InitializerList):
            next_index = 0
            max_index = -1
            for item in initializer.items:
                index = next_index
                if isinstance(item, ast.DesignatedInitializer):
                    for desig in item.designators:
                        if desig.index is not None:
                            val = self.eval_integer_constant(desig.index, require_constant=True)
                            if val is None:
                                raise self.error(desig.index.location, "array designator must be an integer constant expression")
                            index = int(val)
                            break
                if index < 0:
                    raise self.error(loc, "array designator index must be non-negative")
                max_index = max(max_index, index)
                next_index = index + 1
            length = max_index + 1 if max_index >= 0 else 0
        if length is None:
            return dtype
        return ArrayType(t.element, length, t.qualifiers)

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
        storage_names = [s.name for s in fn.specifiers.storage]
        # declare in file scope (or current scope)
        existing = self.file_scope.lookup_ordinary_local(name)
        if existing is not None:
            if existing.kind != "function":
                raise self.error(fn.location, f"'{name}' redeclared as different kind of symbol")
            if not types.compatible_types(existing.type, ftype):
                raise self.error(fn.location, f"conflicting types for function '{name}'")
            if existing.defined:
                raise self.error(fn.location, f"redefinition of function '{name}'")
            if storage_names and existing.storage and existing.storage != storage_names[0]:
                raise self.error(
                    fn.location,
                    f"conflicting linkage for function '{name}'",
                )
            existing.type = types.composite_type(existing.type, ftype)
            sym = existing
        else:
            sym = Symbol(name, "function", ftype, fn.location, defined=False,
                         storage=(storage_names[0] if storage_names else None))
            self.file_scope.ordinary[name] = sym
        if storage_names:
            sym.storage = storage_names[0]
        sym.defined = True
        self.current_function = sym
        ft = types.strip_qualifiers(ftype)
        assert isinstance(ft, FunctionType)
        self.current_return_type = ft.return_type
        # function scope for labels/params/body
        fn_scope = self.push_scope("function")
        sym.params_scope = fn_scope  # type: ignore[attr-defined]
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
            self.require_lowering_supported(cond_t, stmt.condition.location, "condition types")
            self.analyze_statement(stmt.then_branch)
            if stmt.else_branch:
                self.analyze_statement(stmt.else_branch)
            return
        if isinstance(stmt, ast.WhileStatement):
            cond_t = self.analyze_expression(stmt.condition)
            if not types.is_scalar_type(cond_t):
                raise self.error(stmt.location, "controlling expression of while statement must have scalar type")
            self.require_lowering_supported(cond_t, stmt.condition.location, "condition types")
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
            self.require_lowering_supported(cond_t, stmt.condition.location, "condition types")
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
                self.require_lowering_supported(ct, stmt.condition.location, "condition types")
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
                self.require_lowering_supported(rt, stmt.value.location, "return types")
                self.require_lowering_supported(vt, stmt.value.location, "return expressions")
                if not self.is_assignable(rt, vt, stmt.value, check_const=False):
                    raise self.error(stmt.location, "incompatible return type")
                self.note_conversion(stmt.value, rt, "return conversion", source=vt)
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
            self.require_lowering_supported(ct, stmt.condition.location, "switch condition types")
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
        """Validate an initializer and record placement/conversion metadata."""
        t = types.strip_qualifiers(target_type)
        init.resolved_type = target_type  # type: ignore[attr-defined]
        if isinstance(init, ast.InitializerList):
            if isinstance(t, ArrayType):
                next_index = 0
                for item in init.items:
                    index = next_index
                    if isinstance(item, ast.DesignatedInitializer):
                        for desig in item.designators:
                            if desig.index is not None:
                                val = self.eval_integer_constant(desig.index, require_constant=True)
                                if val is None:
                                    raise self.error(desig.index.location, "array designator must be an integer constant expression")
                                index = int(val)
                                break
                            if desig.field:
                                raise self.error(item.location, "field designator used for array initializer")
                        if index < 0:
                            raise self.error(item.location, "array designator index must be non-negative")
                        item.array_index = index  # type: ignore[attr-defined]
                        self.analyze_initializer(t.element, item.initializer, item.location)
                    else:
                        item.array_index = index  # type: ignore[attr-defined]
                        self.analyze_initializer(t.element, item, item.location)
                    next_index = index + 1
                if t.length is not None and next_index > t.length:
                    raise self.error(loc, "excess elements in array initializer")
                return
            if isinstance(t, (StructType, UnionType)):
                if not t.complete:
                    raise self.error(loc, "initializer for incomplete aggregate type")
                fields = t.fields
                idx = 0
                for item in init.items:
                    designated_field: Optional[str] = None
                    if isinstance(item, ast.DesignatedInitializer):
                        if not item.designators:
                            raise self.error(item.location, "empty designator in aggregate initializer")
                        desig = item.designators[0]
                        if len(item.designators) > 1:
                            raise self.error(item.location, "nested designators are not supported by this backend")
                        if desig.field:
                            designated_field = desig.field
                            fmap = t.field_map()
                            if designated_field not in fmap:
                                raise self.error(item.location, f"field '{designated_field}' not found in aggregate")
                            for i, f in enumerate(fields):
                                if f.name == designated_field:
                                    idx = i
                                    break
                        elif desig.index is not None:
                            val = self.eval_integer_constant(desig.index, require_constant=True)
                            if val is None:
                                raise self.error(desig.index.location, "aggregate designator must be an integer constant expression")
                            idx = int(val)
                        else:
                            raise self.error(item.location, "invalid designator in aggregate initializer")
                    if idx < len(fields):
                        field = fields[idx]
                        item.field_index = idx  # type: ignore[attr-defined]
                        if designated_field:
                            item.designated_field = designated_field  # type: ignore[attr-defined]
                        child_init = item.initializer if isinstance(item, ast.DesignatedInitializer) else item
                        self.analyze_initializer(field.type, child_init, item.location)
                        idx += 1
                    else:
                        raise self.error(item.location, f"excess elements in {'union' if isinstance(t, UnionType) else 'struct'} initializer")
                return
            # scalar with braces: allow single element
            if len(init.items) == 1:
                item = init.items[0]
                if isinstance(item, ast.DesignatedInitializer):
                    self.analyze_initializer(target_type, item.initializer, item.location)
                else:
                    self.analyze_initializer(target_type, item, item.location)
                return
            if len(init.items) == 0:
                return
            raise self.error(loc, "invalid brace-enclosed initializer for scalar type")
        # expression initializer
        expr = init
        et = self.analyze_expression(expr)
        if not self.is_assignable(target_type, et, expr, check_const=False):
            # string literal to integer array is handled by array completion above
            if isinstance(t, ArrayType) and isinstance(types.strip_qualifiers(t.element), IntegerType) and isinstance(expr, ast.StringLiteral):
                return
            raise self.error(loc, "incompatible initializer type")
        self.require_lowering_supported(et, expr.location, "initializer expressions")
        self.require_lowering_supported(target_type, expr.location, "initialized object types")
        self.note_conversion(expr, target_type, "initializer")

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
        if isinstance(expr, ast.VaArgExpression):
            self.analyze_expression(expr.ap)
            ttype = self.analyze_type_name(expr.type_name)
            target = types.strip_qualifiers(ttype)
            if isinstance(target, (VoidType, FunctionType, ArrayType)):
                raise self.error(expr.location,
                                 f"invalid va_arg type '{ttype.describe()}'")
            if target.is_incomplete():
                raise self.error(expr.location,
                                 f"va_arg type '{ttype.describe()}' is incomplete")
            self.require_lowering_supported(ttype, expr.location,
                                            "va_arg types")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ttype
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
            self.require_lowering_supported(ot, expr.location, "increment/decrement operands")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ot
        op = expr.op
        ot = self.analyze_expression(expr.operand)
        if op in ("++", "--"):
            if not self.is_modifiable_lvalue(expr.operand, ot):
                raise self.error(expr.location, "operand of increment/decrement must be a modifiable lvalue")
            if not types.is_scalar_type(ot):
                raise self.error(expr.location, "invalid operand type for increment/decrement")
            self.require_lowering_supported(ot, expr.location, "increment/decrement operands")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ot
        if op in ("+", "-", "~"):
            if op == "~":
                if not types.is_integer_type(ot):
                    raise self.error(expr.location, "bitwise not requires integer operand")
            elif not types.is_arithmetic_type(ot):
                raise self.error(expr.location, f"unary '{op}' requires arithmetic operand")
            self.require_lowering_supported(ot, expr.location, "unary operands")
            result = types.integer_promotions(ot, ctx) if types.is_integer_type(ot) else ot
            self.note_conversion(expr.operand, result, f"unary '{op}' promotion", source=ot)
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return result
        if op == "!":
            if not types.is_scalar_type(ot):
                raise self.error(expr.location, "logical not requires scalar operand")
            self.require_lowering_supported(ot, expr.location, "logical operands")
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
            self.require_lowering_supported(p.pointee, expr.location, "dereferenced types")
            expr.value_category = "lvalue"  # type: ignore[attr-defined]
            return p.pointee
        raise self.error(expr.location, f"unsupported unary operator '{op}'")

    def analyze_binary(self, expr: ast.BinaryExpression) -> Type:
        ctx = self.ctx
        op = expr.op
        lt = self.analyze_expression(expr.left)
        rt = self.analyze_expression(expr.right)
        ldt = self.decay_expr_type(expr.left, lt)
        rdt = self.decay_expr_type(expr.right, rt)
        lds, rds = types.strip_qualifiers(ldt), types.strip_qualifiers(rdt)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]

        if op in ("*", "/", "%", "&", "^", "|"):
            if not (types.is_arithmetic_type(lds) and types.is_arithmetic_type(rds)):
                raise self.error(expr.location, f"invalid operands to binary '{op}'")
            if op == "%" and not (types.is_integer_type(lds) and types.is_integer_type(rds)):
                raise self.error(expr.location, "operands of '%' must have integer type")
            if op in ("&", "^", "|") and not (types.is_integer_type(lds) and types.is_integer_type(rds)):
                raise self.error(expr.location, f"bitwise operator '{op}' requires integer operands")
            self.require_lowering_supported(lds, expr.location, "binary operands")
            self.require_lowering_supported(rds, expr.location, "binary operands")
            result = types.usual_arithmetic_conversions(lds, rds, ctx)
            self.note_conversion(expr.left, result, f"binary '{op}' conversion", source=ldt)
            self.note_conversion(expr.right, result, f"binary '{op}' conversion", source=rdt)
            return result
        if op in ("+", "-"):
            if types.is_arithmetic_type(lds) and types.is_arithmetic_type(rds):
                self.require_lowering_supported(lds, expr.location, "binary operands")
                self.require_lowering_supported(rds, expr.location, "binary operands")
                result = types.usual_arithmetic_conversions(lds, rds, ctx)
                self.note_conversion(expr.left, result, f"binary '{op}' conversion", source=ldt)
                self.note_conversion(expr.right, result, f"binary '{op}' conversion", source=rdt)
                return result
            if op == "+" and isinstance(lds, PointerType) and types.is_integer_type(rds):
                self.require_lowering_supported(lds.pointee, expr.location, "pointer arithmetic pointee types")
                return ldt
            if op == "+" and isinstance(rds, PointerType) and types.is_integer_type(lds):
                self.require_lowering_supported(rds.pointee, expr.location, "pointer arithmetic pointee types")
                return rdt
            if op == "-" and isinstance(lds, PointerType) and types.is_integer_type(rds):
                self.require_lowering_supported(lds.pointee, expr.location, "pointer arithmetic pointee types")
                return ldt
            if op == "-" and isinstance(lds, PointerType) and isinstance(rds, PointerType):
                self.require_lowering_supported(lds.pointee, expr.location, "pointer difference pointee types")
                return ctx.ptrdiff_t
            raise self.error(expr.location, f"invalid operands to binary '{op}'")
        if op in ("<<", ">>"):
            if not (types.is_integer_type(lds) and types.is_integer_type(rds)):
                raise self.error(expr.location, "shift operands must have integer type")
            result = types.integer_promotions(lds, ctx)
            self.note_conversion(expr.left, result, f"binary '{op}' promotion", source=ldt)
            self.note_conversion(expr.right, ctx.int_t, "shift count promotion", source=rdt)
            return result
        if op in ("<", "<=", ">", ">=", "==", "!="):
            if types.is_arithmetic_type(lds) and types.is_arithmetic_type(rds):
                self.require_lowering_supported(lds, expr.location, "comparison operands")
                self.require_lowering_supported(rds, expr.location, "comparison operands")
                common = types.usual_arithmetic_conversions(lds, rds, ctx)
                self.note_conversion(expr.left, common, "comparison conversion", source=ldt)
                self.note_conversion(expr.right, common, "comparison conversion", source=rdt)
                return ctx.int_t
            if isinstance(lds, PointerType) and isinstance(rds, PointerType):
                if not (
                    types.compatible_types(types.strip_qualifiers(lds.pointee), types.strip_qualifiers(rds.pointee))
                    or isinstance(types.strip_qualifiers(lds.pointee), VoidType)
                    or isinstance(types.strip_qualifiers(rds.pointee), VoidType)
                ):
                    raise self.error(expr.location, f"incompatible pointer operands to '{op}'")
                return ctx.int_t
            if isinstance(lds, PointerType) and self.is_null_pointer_constant(expr.right, rt):
                return ctx.int_t
            if isinstance(rds, PointerType) and self.is_null_pointer_constant(expr.left, lt):
                return ctx.int_t
            raise self.error(expr.location, f"invalid operands to relational operator '{op}'")
        if op in ("&&", "||"):
            if not (types.is_scalar_type(lds) and types.is_scalar_type(rds)):
                raise self.error(expr.location, f"logical operator '{op}' requires scalar operands")
            self.require_lowering_supported(lds, expr.location, "logical operands")
            self.require_lowering_supported(rds, expr.location, "logical operands")
            return ctx.int_t
        raise self.error(expr.location, f"unsupported binary operator '{op}'")

    def analyze_assignment(self, expr: ast.AssignmentExpression) -> Type:
        ctx = self.ctx
        lt = self.analyze_expression(expr.left)
        rt = self.analyze_expression(expr.right)
        if not self.is_modifiable_lvalue(expr.left, lt):
            raise self.error(expr.location, "left operand of assignment must be a modifiable lvalue")
        self.require_lowering_supported(lt, expr.location, "assignment target types")
        self.require_lowering_supported(rt, expr.location, "assignment source types")
        if expr.op == "=":
            if not self.is_assignable(lt, rt, expr.right, check_const=False):
                raise self.error(expr.location, "incompatible types in assignment")
            self.note_conversion(expr.right, lt, "assignment conversion", source=rt)
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return lt
        binop = expr.op[:-1]
        lts, rts = types.strip_qualifiers(lt), types.strip_qualifiers(rt)
        if binop in ("+", "-") and isinstance(lts, PointerType) and types.is_integer_type(rts):
            self.require_lowering_supported(lts.pointee, expr.location, "pointer arithmetic pointee types")
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return lt
        if binop in ("&", "^", "|", "<<", ">>", "%") and not (
            types.is_integer_type(lts) and types.is_integer_type(rts)
        ):
            raise self.error(expr.location, f"invalid operands to compound assignment '{expr.op}'")
        if not (types.is_arithmetic_type(lts) and types.is_arithmetic_type(rts)):
            raise self.error(expr.location, f"invalid operands to compound assignment '{expr.op}'")
        common = types.usual_arithmetic_conversions(lts, rts, ctx)
        self.note_conversion(expr.left, common, f"compound '{expr.op}' conversion", source=lt)
        self.note_conversion(expr.right, common, f"compound '{expr.op}' conversion", source=rt)
        self.note_conversion(expr.right, lt, "assignment conversion", source=common)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        return lt

    def analyze_conditional(self, expr: ast.ConditionalExpression) -> Type:
        ctx = self.ctx
        ct = self.analyze_expression(expr.condition)
        if not types.is_scalar_type(ct):
            raise self.error(expr.location, "conditional expression condition must have scalar type")
        self.require_lowering_supported(ct, expr.location, "conditional conditions")
        tt = self.analyze_expression(expr.then_expr) if expr.then_expr is not None else ct
        et = self.analyze_expression(expr.else_expr)
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        ttd = self.decay_expr_type(expr.then_expr, tt) if expr.then_expr else tt
        etd = self.decay_expr_type(expr.else_expr, et)
        result: Type
        if types.is_arithmetic_type(ttd) and types.is_arithmetic_type(etd):
            self.require_lowering_supported(ttd, expr.location, "conditional operands")
            self.require_lowering_supported(etd, expr.location, "conditional operands")
            result = types.usual_arithmetic_conversions(ttd, etd, ctx)
        elif types.compatible_types(ttd, etd):
            result = ttd
        elif isinstance(types.strip_qualifiers(ttd), PointerType) and isinstance(types.strip_qualifiers(etd), PointerType):
            result = ttd
        elif isinstance(types.strip_qualifiers(ttd), VoidType) and isinstance(types.strip_qualifiers(etd), VoidType):
            result = ctx.void_t
        else:
            raise self.error(expr.location, "incompatible operands to conditional expression")
        if expr.then_expr is not None:
            self.note_conversion(expr.then_expr, result, "conditional conversion", source=ttd)
        self.note_conversion(expr.else_expr, result, "conditional conversion", source=etd)
        return result

    def analyze_cast(self, expr: ast.CastExpression) -> Type:
        ttype = self.analyze_type_name(expr.type_name)
        ot = self.analyze_expression(expr.operand)
        target = types.strip_qualifiers(ttype)
        source = types.strip_qualifiers(ot)
        if isinstance(target, ArrayType) or isinstance(target, FunctionType):
            raise self.error(expr.location, "cast to array or function type is invalid")
        if isinstance(source, (StructType, UnionType)) or isinstance(target, (StructType, UnionType)):
            raise self.error(expr.location, "casts involving struct or union types are not supported")
        if isinstance(target, VoidType):
            expr.value_category = "rvalue"  # type: ignore[attr-defined]
            return ttype
        if not (types.is_scalar_type(target) and types.is_scalar_type(source)):
            raise self.error(expr.location, "invalid cast operand or target type")
        self.require_lowering_supported(source, expr.location, "cast operands")
        self.require_lowering_supported(target, expr.location, "cast targets")
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
        for arg, at in zip(expr.arguments, args_t):
            self.require_lowering_supported(at, arg.location, "call arguments")
        if not ft.no_prototype:
            fixed = len(ft.params)
            if not ft.variadic and len(args_t) != fixed:
                raise self.error(expr.location, f"incorrect number of arguments to function call (expected {fixed}, got {len(args_t)})")
            if ft.variadic and len(args_t) < fixed:
                raise self.error(expr.location, f"too few arguments to variadic function call (expected at least {fixed}, got {len(args_t)})")
            for i, pt in enumerate(ft.params):
                at = args_t[i]
                if not self.is_assignable(pt.type, at, expr.arguments[i], check_const=False):
                    raise self.error(
                        expr.arguments[i].location,
                        f"incompatible argument {i + 1}: expected '{pt.type.describe()}', got '{at.describe()}'",
                    )
                self.note_conversion(expr.arguments[i], pt.type, "argument conversion", source=at)
            if ft.variadic:
                for arg, at in zip(expr.arguments[fixed:], args_t[fixed:]):
                    if types.is_integer_type(at):
                        promoted = types.integer_promotions(at, self.ctx)
                        self.note_conversion(arg, promoted, "default argument promotion", source=at)
                    else:
                        # Array and function designators decay to pointers in
                        # the default argument promotions too; without this the
                        # lowerer would try to pass the whole object as an
                        # aggregate variadic argument.
                        decayed = self.decay_expr_type(arg, at)
                        if decayed is not at:
                            self.note_conversion(arg, decayed,
                                                 "default argument promotion",
                                                 source=at)
        self.require_lowering_supported(ft.return_type, expr.location, "call return types")
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
            # The operand is unevaluated: analyze it for its semantic type but do
            # not apply array/function decay. `analyze_expression` does not decay
            # on its own, so the declared type is preserved here.
            t = self.analyze_expression(expr.operand)
        else:
            raise self.error(expr.location, "invalid sizeof operand")
        st = types.strip_qualifiers(t)
        if isinstance(st, VoidType):
            raise self.error(expr.location, "sizeof(void) is invalid")
        if isinstance(st, FunctionType):
            raise self.error(expr.location, "sizeof(function type) is invalid")
        if isinstance(st, ArrayType) and (
            st.length is None or st.element.is_incomplete() or st.element.sizeof() is None
        ):
            raise self.error(expr.location, "sizeof(incomplete array type) is invalid")
        if st.is_incomplete() or st.sizeof() is None:
            raise self.error(expr.location, f"sizeof incomplete type '{t.describe()}' is invalid")
        size = st.sizeof()
        assert size is not None
        # sizeof result type is size_t (unsigned long on Nova64). The value is a
        # compile-time constant consumed by array bounds, enum values and codegen.
        expr.value_category = "rvalue"  # type: ignore[attr-defined]
        expr.const_value = size  # type: ignore[attr-defined]
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
        # Arrays and function designators decay in value contexts. Record the
        # decayed type so codegen does not need to infer it again.
        st = types.strip_qualifiers(t)
        decayed: Type = t
        if isinstance(st, ArrayType):
            decayed = PointerType(st.element)
        elif isinstance(st, FunctionType):
            decayed = PointerType(st)
        if expr is not None:
            expr.decayed_type = decayed  # type: ignore[attr-defined]
            if decayed is not t:
                expr.decay_kind = "array-to-pointer" if isinstance(st, ArrayType) else "function-to-pointer"  # type: ignore[attr-defined]
        return decayed

    def is_null_pointer_constant(self, expr: Optional[ast.Expression], t: Type) -> bool:
        """C's null pointer constant: an integer constant expression of value 0."""
        if expr is None or not types.is_integer_type(t):
            return False
        value = self.eval_integer_constant(expr)
        return value is not None and value == 0

    @staticmethod
    def pointer_qualifiers_compatible(target: Type, source: Type) -> bool:
        """Allow adding qualifiers, never discarding them, in pointer assignments."""
        tq, sq = target.qualifiers, source.qualifiers
        return (
            (not sq.const or tq.const)
            and (not sq.volatile or tq.volatile)
            and (not sq.restrict or tq.restrict)
            and (not sq.atomic or tq.atomic)
        )

    def pointer_assignment_ok(self, target: PointerType, source: PointerType) -> bool:
        tp, sp = target.pointee, source.pointee
        if not self.pointer_qualifiers_compatible(tp, sp):
            return False
        tu, su = types.strip_qualifiers(tp), types.strip_qualifiers(sp)
        if isinstance(tu, VoidType) or isinstance(su, VoidType):
            return True
        return types.compatible_types(tu, su)

    def is_assignable(self, lhs_type: Type, rhs_type: Type, rhs_expr: Optional[ast.Expression], check_const: bool = False) -> bool:
        lt = types.strip_qualifiers(lhs_type)
        rtd = types.strip_qualifiers(self.decay_expr_type(rhs_expr, rhs_type))
        if isinstance(lt, (ArrayType, FunctionType)):
            return False
        if isinstance(lt, BoolType) and types.is_scalar_type(rtd):
            return True
        if types.is_arithmetic_type(lt) and types.is_arithmetic_type(rtd):
            return True
        if isinstance(lt, (StructType, UnionType)):
            return types.compatible_types(lt, rtd)
        if isinstance(lt, PointerType) and isinstance(rtd, PointerType):
            return self.pointer_assignment_ok(lt, rtd)
        if isinstance(lt, PointerType) and self.is_null_pointer_constant(rhs_expr, rhs_type):
            return True
        return types.compatible_types(lhs_type, rhs_type)

    def eval_integer_constant(self, expr: ast.Expression, require_constant: bool = False) -> Optional[int]:
        """Evaluate an integer constant expression, normalized to its C type.

        The raw evaluator works with Python integers; this wrapper applies the
        expression's Nova64 width and signedness so folded values used by array
        bounds, case labels and codegen match the target model.
        """
        value = self._eval_integer_constant_raw(expr, require_constant=require_constant)
        if value is not None:
            value = self._normalize_integer_constant(value, getattr(expr, "resolved_type", None))
            expr.const_value = value  # type: ignore[attr-defined]
        return value

    def _normalize_integer_constant(self, value: int, typ: Optional[Type]) -> int:
        if typ is None:
            return value
        t = types.strip_qualifiers(typ)
        size = t.sizeof()
        if size is None or not isinstance(t, (IntegerType, BoolType, EnumType)):
            return value
        bits = size * 8
        mask = (1 << bits) - 1
        value &= mask
        signed = t.signed if isinstance(t, IntegerType) else True
        if isinstance(t, EnumType):
            signed = t.underlying.signed
        if signed and value & (1 << (bits - 1)):
            value -= 1 << bits
        return value

    def _eval_integer_constant_raw(self, expr: ast.Expression, require_constant: bool = False) -> Optional[int]:
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
            # analyze_sizeof() stores the computed size; the wrapper returns it.
            return getattr(expr, "const_value", None)
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
