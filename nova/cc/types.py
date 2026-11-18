"""C type system for :mod:`nova.cc`.

This module provides the semantic type representation used during semantic
analysis. It is intentionally target-aware so Nova64 ABI decisions (LP64 data
model, alignment, integer widths/ranks) are explicit and do not depend on the
host Python environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Qualifiers:
    const: bool = False
    volatile: bool = False
    restrict: bool = False
    atomic: bool = False

    @property
    def empty(self) -> bool:
        return not (self.const or self.volatile or self.restrict or self.atomic)

    def union(self, other: "Qualifiers") -> "Qualifiers":
        return Qualifiers(
            const=self.const or other.const,
            volatile=self.volatile or other.volatile,
            restrict=self.restrict or other.restrict,
            atomic=self.atomic or other.atomic,
        )

    def without(self, names: Iterable[str]) -> "Qualifiers":
        s = set(names)
        return Qualifiers(
            const=self.const and "const" not in s,
            volatile=self.volatile and "volatile" not in s,
            restrict=self.restrict and "restrict" not in s,
            atomic=self.atomic and "_Atomic" not in s,
        )

    def __str__(self) -> str:
        parts = []
        if self.const:
            parts.append("const")
        if self.volatile:
            parts.append("volatile")
        if self.restrict:
            parts.append("restrict")
        if self.atomic:
            parts.append("_Atomic")
        return " ".join(parts)


NO_QUALS = Qualifiers()


class Type:
    """Base semantic type."""

    qualifiers: Qualifiers

    def __init__(self, qualifiers: Qualifiers = NO_QUALS):
        self.qualifiers = qualifiers

    @property
    def unqualified(self) -> "Type":
        return self.with_qualifiers(NO_QUALS)

    def with_qualifiers(self, qualifiers: Qualifiers) -> "Type":  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def add_qualifiers(self, qualifiers: Qualifiers) -> "Type":
        return self.with_qualifiers(self.qualifiers.union(qualifiers))

    def is_same_unqualified(self, other: "Type") -> bool:
        return type_eq(strip_qualifiers(self), strip_qualifiers(other))

    def is_void(self) -> bool:
        return False

    def is_bool(self) -> bool:
        return False

    def is_integer(self) -> bool:
        return False

    def is_floating(self) -> bool:
        return False

    def is_arithmetic(self) -> bool:
        return self.is_integer() or self.is_floating()

    def is_scalar(self) -> bool:
        return self.is_arithmetic() or isinstance(self, PointerType)

    def is_pointer(self) -> bool:
        return isinstance(self, PointerType)

    def is_array(self) -> bool:
        return isinstance(self, ArrayType)

    def is_function(self) -> bool:
        return isinstance(self, FunctionType)

    def is_struct(self) -> bool:
        return isinstance(self, StructType)

    def is_union(self) -> bool:
        return isinstance(self, UnionType)

    def is_enum(self) -> bool:
        return isinstance(self, EnumType)

    def is_record(self) -> bool:
        return isinstance(self, (StructType, UnionType))

    def is_incomplete(self) -> bool:
        return False

    def is_object(self) -> bool:
        return not self.is_function() and not self.is_void() and not self.is_incomplete()

    def sizeof(self) -> Optional[int]:
        return None

    def alignof(self) -> Optional[int]:
        return None

    def __repr__(self) -> str:
        return self.describe()

    def describe(self) -> str:
        return type(self).__name__


@dataclass
class VoidType(Type):
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "VoidType":
        return VoidType(qualifiers)

    def is_void(self) -> bool:
        return True

    def is_incomplete(self) -> bool:
        return True

    def describe(self) -> str:
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}void".strip()


@dataclass
class BoolType(Type):
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "BoolType":
        return BoolType(qualifiers)

    def is_bool(self) -> bool:
        return True

    def is_integer(self) -> bool:
        return True

    def sizeof(self) -> Optional[int]:
        return 1

    def alignof(self) -> Optional[int]:
        return 1

    def describe(self) -> str:
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}_Bool".strip()


@dataclass
class IntegerType(Type):
    name: str
    size: int
    align: int
    signed: bool
    rank: int
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "IntegerType":
        return IntegerType(self.name, self.size, self.align, self.signed, self.rank, qualifiers)

    def is_integer(self) -> bool:
        return True

    def sizeof(self) -> Optional[int]:
        return self.size

    def alignof(self) -> Optional[int]:
        return self.align

    def describe(self) -> str:
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}{self.name}".strip()


@dataclass
class FloatingType(Type):
    name: str
    size: int
    align: int
    rank: int
    qualifiers: Qualifiers = NO_QUALS
    lowering_supported: bool = False

    def with_qualifiers(self, qualifiers: Qualifiers) -> "FloatingType":
        return FloatingType(self.name, self.size, self.align, self.rank, qualifiers, self.lowering_supported)

    def is_floating(self) -> bool:
        return True

    def sizeof(self) -> Optional[int]:
        return self.size

    def alignof(self) -> Optional[int]:
        return self.align

    def describe(self) -> str:
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}{self.name}".strip()


@dataclass
class PointerType(Type):
    pointee: Type
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "PointerType":
        return PointerType(self.pointee, qualifiers)

    def is_incomplete(self) -> bool:
        return False

    def sizeof(self) -> Optional[int]:
        return 8

    def alignof(self) -> Optional[int]:
        return 8

    def describe(self) -> str:
        q = (" " + str(self.qualifiers)) if not self.qualifiers.empty else ""
        return f"pointer to {self.pointee.describe()}{q}".strip()


@dataclass
class ArrayType(Type):
    element: Type
    length: Optional[int]  # None for incomplete array type T[]
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "ArrayType":
        return ArrayType(self.element, self.length, qualifiers)

    def is_incomplete(self) -> bool:
        if self.length is None:
            return True
        return self.element.is_incomplete()

    def sizeof(self) -> Optional[int]:
        if self.length is None:
            return None
        esz = self.element.sizeof()
        if esz is None:
            return None
        return self.length * esz

    def alignof(self) -> Optional[int]:
        return self.element.alignof()

    def describe(self) -> str:
        n = "" if self.length is None else str(self.length)
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}array[{n}] of {self.element.describe()}"


@dataclass
class ParameterType:
    name: Optional[str]
    type: Type


@dataclass
class FunctionType(Type):
    return_type: Type
    params: List[ParameterType] = field(default_factory=list)
    variadic: bool = False
    no_prototype: bool = False  # old-style/unspecified params (limited)
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "FunctionType":
        return FunctionType(self.return_type, list(self.params), self.variadic, self.no_prototype, qualifiers)

    def is_incomplete(self) -> bool:
        return False

    def describe(self) -> str:
        if self.no_prototype:
            params = ""
        else:
            plist = [p.type.describe() for p in self.params]
            if self.variadic:
                plist.append("...")
            params = ", ".join(plist)
        return f"function({params}) returning {self.return_type.describe()}"


@dataclass
class FieldInfo:
    name: Optional[str]
    type: Type
    offset: int = 0
    bit_width: Optional[int] = None
    bit_offset: Optional[int] = None


@dataclass
class StructType(Type):
    name: Optional[str]
    tag_id: int
    fields: List[FieldInfo] = field(default_factory=list)
    complete: bool = False
    size: Optional[int] = None
    align: Optional[int] = None
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "StructType":
        t = StructType(self.name, self.tag_id, self.fields, self.complete, self.size, self.align, qualifiers)
        return t

    def is_incomplete(self) -> bool:
        return not self.complete

    def sizeof(self) -> Optional[int]:
        return self.size if self.complete else None

    def alignof(self) -> Optional[int]:
        return self.align if self.complete else None

    def field_map(self) -> Dict[str, FieldInfo]:
        return {f.name: f for f in self.fields if f.name}

    def describe(self) -> str:
        tag = f"struct {self.name}" if self.name else f"struct <anon#{self.tag_id}>"
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}{tag}".strip()


@dataclass
class UnionType(Type):
    name: Optional[str]
    tag_id: int
    fields: List[FieldInfo] = field(default_factory=list)
    complete: bool = False
    size: Optional[int] = None
    align: Optional[int] = None
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "UnionType":
        return UnionType(self.name, self.tag_id, self.fields, self.complete, self.size, self.align, qualifiers)

    def is_incomplete(self) -> bool:
        return not self.complete

    def sizeof(self) -> Optional[int]:
        return self.size if self.complete else None

    def alignof(self) -> Optional[int]:
        return self.align if self.complete else None

    def field_map(self) -> Dict[str, FieldInfo]:
        return {f.name: f for f in self.fields if f.name}

    def describe(self) -> str:
        tag = f"union {self.name}" if self.name else f"union <anon#{self.tag_id}>"
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}{tag}".strip()


@dataclass
class EnumConst:
    name: str
    value: int
    type: "EnumType" | None = None


@dataclass
class EnumType(Type):
    name: Optional[str]
    tag_id: int
    underlying: "IntegerType"
    enumerators: List[EnumConst] = field(default_factory=list)
    complete: bool = False
    qualifiers: Qualifiers = NO_QUALS

    def with_qualifiers(self, qualifiers: Qualifiers) -> "EnumType":
        return EnumType(self.name, self.tag_id, self.underlying, self.enumerators, self.complete, qualifiers)

    def is_integer(self) -> bool:
        # Enums have integer-compatible values; semantic checks often treat them specially,
        # but for arithmetic classification they behave like their compatible integer type.
        return True

    def is_enum(self) -> bool:
        return True

    def is_incomplete(self) -> bool:
        return not self.complete

    def sizeof(self) -> Optional[int]:
        return self.underlying.sizeof()

    def alignof(self) -> Optional[int]:
        return self.underlying.alignof()

    def describe(self) -> str:
        tag = f"enum {self.name}" if self.name else f"enum <anon#{self.tag_id}>"
        q = (str(self.qualifiers) + " ") if not self.qualifiers.empty else ""
        return f"{q}{tag}".strip()


class TypeContext:
    """Target type context (Nova64 LP64)."""

    def __init__(self, target: str = "nova64"):
        self.target = target
        self._tag_counter = 0
        # Nova64 ABI (docs/NOVA_C_ABI.md): LP64, little-endian, char unsigned.
        self.void_t = VoidType()
        self.bool_t = BoolType()
        self.char_t = IntegerType("char", 1, 1, signed=False, rank=1)
        self.schar_t = IntegerType("signed char", 1, 1, signed=True, rank=1)
        self.uchar_t = IntegerType("unsigned char", 1, 1, signed=False, rank=1)
        self.short_t = IntegerType("short", 2, 2, signed=True, rank=2)
        self.ushort_t = IntegerType("unsigned short", 2, 2, signed=False, rank=2)
        self.int_t = IntegerType("int", 4, 4, signed=True, rank=3)
        self.uint_t = IntegerType("unsigned int", 4, 4, signed=False, rank=3)
        self.long_t = IntegerType("long", 8, 8, signed=True, rank=4)
        self.ulong_t = IntegerType("unsigned long", 8, 8, signed=False, rank=4)
        self.llong_t = IntegerType("long long", 8, 8, signed=True, rank=5)
        self.ullong_t = IntegerType("unsigned long long", 8, 8, signed=False, rank=5)
        # Floating types represented semantically; Nova base ISA has no FP registers/instructions.
        self.float_t = FloatingType("float", 4, 4, rank=1, lowering_supported=False)
        self.double_t = FloatingType("double", 8, 8, rank=2, lowering_supported=False)
        self.ldouble_t = FloatingType("long double", 8, 8, rank=3, lowering_supported=False)
        self.ptrdiff_t = self.long_t
        self.size_t = self.ulong_t
        self.intptr_t = self.long_t
        self.uintptr_t = self.ulong_t

    def new_tag_id(self) -> int:
        self._tag_counter += 1
        return self._tag_counter

    def pointer_to(self, t: Type) -> PointerType:
        return PointerType(t)

    def array_of(self, t: Type, length: Optional[int]) -> ArrayType:
        return ArrayType(t, length)

    def function(self, ret: Type, params: List[ParameterType], variadic: bool = False, no_prototype: bool = False) -> FunctionType:
        return FunctionType(ret, params, variadic, no_prototype)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def strip_qualifiers(t: Type) -> Type:
    return t.unqualified


def is_qualified(t: Type) -> bool:
    return not t.qualifiers.empty


def type_eq(a: Type, b: Type) -> bool:
    if type(a) is not type(b):
        # Enum may compare specially with underlying in some contexts, but strict type equality here.
        return False
    if a.qualifiers != b.qualifiers:
        return False
    if isinstance(a, (VoidType, BoolType)):
        return True
    if isinstance(a, IntegerType) and isinstance(b, IntegerType):
        return (a.name, a.size, a.signed, a.rank) == (b.name, b.size, b.signed, b.rank)
    if isinstance(a, FloatingType) and isinstance(b, FloatingType):
        return (a.name, a.size, a.rank) == (b.name, b.size, b.rank)
    if isinstance(a, PointerType) and isinstance(b, PointerType):
        return type_eq(a.pointee, b.pointee)
    if isinstance(a, ArrayType) and isinstance(b, ArrayType):
        return a.length == b.length and type_eq(a.element, b.element)
    if isinstance(a, FunctionType) and isinstance(b, FunctionType):
        if a.variadic != b.variadic or a.no_prototype != b.no_prototype:
            return False
        if not type_eq(a.return_type, b.return_type):
            return False
        if a.no_prototype or b.no_prototype:
            return a.no_prototype == b.no_prototype
        if len(a.params) != len(b.params):
            return False
        return all(type_eq(pa.type, pb.type) for pa, pb in zip(a.params, b.params))
    if isinstance(a, StructType) and isinstance(b, StructType):
        return a.tag_id == b.tag_id
    if isinstance(a, UnionType) and isinstance(b, UnionType):
        return a.tag_id == b.tag_id
    if isinstance(a, EnumType) and isinstance(b, EnumType):
        return a.tag_id == b.tag_id
    return False


def compatible_types(a: Type, b: Type) -> bool:
    a0, b0 = strip_qualifiers(a), strip_qualifiers(b)
    if type(a0) is not type(b0):
        # enums compatible with their implementation/underlying in some uses? C has specific rules;
        # keep conservative: same enum tag only.
        return False
    if isinstance(a0, (VoidType, BoolType)):
        return True
    if isinstance(a0, IntegerType) and isinstance(b0, IntegerType):
        return a0.signed == b0.signed and a0.size == b0.size and a0.rank == b0.rank and a0.name == b0.name
    if isinstance(a0, FloatingType) and isinstance(b0, FloatingType):
        return a0.name == b0.name and a0.size == b0.size
    if isinstance(a0, PointerType) and isinstance(b0, PointerType):
        # compatible if pointed-to types are compatible (ignoring top-level quals of pointee for base check here?)
        # C: pointers to compatible types are compatible. Qualifiers on pointed-to compared as compatible.
        return compatible_qualified(a0.pointee, b0.pointee)
    if isinstance(a0, ArrayType) and isinstance(b0, ArrayType):
        if not compatible_types(a0.element, b0.element):
            return False
        if a0.length is None or b0.length is None:
            return True
        return a0.length == b0.length
    if isinstance(a0, FunctionType) and isinstance(b0, FunctionType):
        if not compatible_types(a0.return_type, b0.return_type):
            return False
        # prototype compatibility (simplified)
        if a0.no_prototype and b0.no_prototype:
            return True
        if a0.no_prototype or b0.no_prototype:
            # old-style vs prototype: compatible if return compatible and params compatible after promotion in simple cases
            proto = b0 if a0.no_prototype else a0
            # accept for now
            return True
        if a0.variadic != b0.variadic or len(a0.params) != len(b0.params):
            return False
        return all(compatible_types(pa.type, pb.type) for pa, pb in zip(a0.params, b0.params))
    if isinstance(a0, StructType) and isinstance(b0, StructType):
        return a0.tag_id == b0.tag_id
    if isinstance(a0, UnionType) and isinstance(b0, UnionType):
        return a0.tag_id == b0.tag_id
    if isinstance(a0, EnumType) and isinstance(b0, EnumType):
        return a0.tag_id == b0.tag_id
    return False


def compatible_qualified(a: Type, b: Type) -> bool:
    # For pointer-target compatibility, qualifiers must be compatible in C (a can have superset in some contexts),
    # but "compatible types" requires same qualifiers in the compared position.
    return a.qualifiers == b.qualifiers and compatible_types(a, b)


def is_integer_type(t: Type) -> bool:
    t = strip_qualifiers(t)
    return isinstance(t, (IntegerType, BoolType, EnumType))


def is_floating_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), FloatingType)


def is_arithmetic_type(t: Type) -> bool:
    t = strip_qualifiers(t)
    return is_integer_type(t) or is_floating_type(t)


def is_scalar_type(t: Type) -> bool:
    t = strip_qualifiers(t)
    return is_arithmetic_type(t) or isinstance(t, PointerType)


def is_pointer_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), PointerType)


def is_array_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), ArrayType)


def is_function_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), FunctionType)


def is_record_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), (StructType, UnionType))


def is_void_type(t: Type) -> bool:
    return isinstance(strip_qualifiers(t), VoidType)


def is_complete_type(t: Type) -> bool:
    return not strip_qualifiers(t).is_incomplete()


def integer_rank(t: Type, ctx: TypeContext) -> int:
    t = strip_qualifiers(t)
    if isinstance(t, BoolType):
        return 0
    if isinstance(t, EnumType):
        return t.underlying.rank
    if isinstance(t, IntegerType):
        # char and signed/unsigned char have same rank as char in C; we store rank 1 for all
        return t.rank
    return -1


def is_signed_integer(t: Type, ctx: TypeContext) -> bool:
    t = strip_qualifiers(t)
    if isinstance(t, BoolType):
        return False
    if isinstance(t, EnumType):
        return t.underlying.signed
    if isinstance(t, IntegerType):
        return t.signed
    return False


def corresponding_unsigned(t: IntegerType, ctx: TypeContext) -> IntegerType:
    mapping = {
        ctx.char_t.name: ctx.uchar_t,  # plain char unsigned in this ABI, but keep mapping
        ctx.schar_t.name: ctx.uchar_t,
        ctx.short_t.name: ctx.ushort_t,
        ctx.int_t.name: ctx.uint_t,
        ctx.long_t.name: ctx.ulong_t,
        ctx.llong_t.name: ctx.ullong_t,
    }
    return mapping.get(t.name, ctx.uint_t)


def corresponding_signed(t: IntegerType, ctx: TypeContext) -> IntegerType:
    mapping = {
        ctx.uchar_t.name: ctx.schar_t,
        ctx.ushort_t.name: ctx.short_t,
        ctx.uint_t.name: ctx.int_t,
        ctx.ulong_t.name: ctx.long_t,
        ctx.ullong_t.name: ctx.llong_t,
        ctx.char_t.name: ctx.char_t,  # plain char unsigned here
    }
    return mapping.get(t.name, ctx.int_t)


def integer_promotions(t: Type, ctx: TypeContext) -> Type:
    """Integer promotions (C11 6.3.1.1)."""
    t0 = strip_qualifiers(t)
    if isinstance(t0, EnumType):
        t0 = t0.underlying
    if isinstance(t0, BoolType):
        return ctx.int_t
    if isinstance(t0, IntegerType):
        # If int can represent all values, promote to int; else unsigned int.
        if t0.rank < ctx.int_t.rank or (t0.rank == ctx.int_t.rank and t0.size < ctx.int_t.size):
            # all small integer types fit in int on Nova64 LP64
            return ctx.int_t
        if t0.rank == ctx.int_t.rank and t0.size == ctx.int_t.size and not t0.signed and t0.name != ctx.int_t.name:
            # unsigned short same rank? short rank 2 < int rank 3 so promoted to int
            return ctx.uint_t if t0.signed is False and t0.size == ctx.int_t.size and t0.rank >= ctx.int_t.rank else ctx.int_t
        return t0
    return t


def usual_arithmetic_conversions(lhs: Type, rhs: Type, ctx: TypeContext) -> Type:
    l = strip_qualifiers(lhs)
    r = strip_qualifiers(rhs)
    # Floating
    if isinstance(l, FloatingType) or isinstance(r, FloatingType):
        if isinstance(l, FloatingType) and isinstance(r, FloatingType):
            return l if l.rank >= r.rank else r
        return l if isinstance(l, FloatingType) else r
    # Integer promotions
    l = integer_promotions(l, ctx)
    r = integer_promotions(r, ctx)
    if not (isinstance(l, IntegerType) and isinstance(r, IntegerType)):
        # fallback
        return l
    if l.name == r.name and l.signed == r.signed and l.size == r.size:
        return l
    if l.signed == r.signed:
        return l if integer_rank(l, ctx) >= integer_rank(r, ctx) else r
    # one signed, one unsigned
    if not l.signed:
        u, s = l, r
    else:
        u, s = r, l
    if integer_rank(u, ctx) >= integer_rank(s, ctx):
        return u
    if s.size > u.size:
        return s
    # convert to unsigned type corresponding to signed type
    return corresponding_unsigned(s, ctx)


def pointer_to_compatible(a: Type, b: Type) -> bool:
    a0, b0 = strip_qualifiers(a), strip_qualifiers(b)
    if not (isinstance(a0, PointerType) and isinstance(b0, PointerType)):
        return False
    return compatible_types(a0.pointee, b0.pointee)


def composite_type(a: Type, b: Type) -> Type:
    # Simplified composite type: prefer completed/more specific where compatible.
    if compatible_types(a, b):
        a0, b0 = strip_qualifiers(a), strip_qualifiers(b)
        if isinstance(a0, ArrayType) and isinstance(b0, ArrayType):
            length = a0.length if a0.length is not None else b0.length
            elem = composite_type(a0.element, b0.element)
            t = ArrayType(elem, length)
            return t
        if isinstance(a0, FunctionType) and isinstance(b0, FunctionType):
            proto = a0 if not a0.no_prototype else b0
            return proto
        # if one is complete record and other same tag incomplete, return complete
        if isinstance(a0, StructType) and a0.complete:
            return a0
        if isinstance(b0, StructType) and b0.complete:
            return b0
        if isinstance(a0, UnionType) and a0.complete:
            return a0
        if isinstance(b0, UnionType) and b0.complete:
            return b0
        if isinstance(a0, EnumType) and a0.complete:
            return a0
        if isinstance(b0, EnumType) and b0.complete:
            return b0
        return a
    return a


def can_decay_to_pointer(t: Type) -> bool:
    t = strip_qualifiers(t)
    return isinstance(t, (ArrayType, FunctionType))


def decay(t: Type, ctx: TypeContext) -> Type:
    t0 = strip_qualifiers(t)
    if isinstance(t0, ArrayType):
        return ctx.pointer_to(t0.element)
    if isinstance(t0, FunctionType):
        return ctx.pointer_to(t0)
    return t
