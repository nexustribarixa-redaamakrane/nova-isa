"""Nova64 assembly lowering for semantically analysed :mod:`nova.cc` trees.

Milestone 5 uses direct AST lowering: there is no second IR and no register
allocator.  Every local object and compiler temporary receives a slot in the
current stack frame, so values survive calls and the generated code is easy to
inspect.  A scalar lvalue's slot holds its address; a scalar rvalue's slot holds
its value.  Aggregate values are represented by their address and are copied
explicitly when assigned or passed.

The fixed frame layout follows ``docs/NOVA_C_ABI.md`` and
``docs/PHASE1_PLAN.md``:

* ``[sp, sp + 128)`` -- outgoing stack-argument area (16 eight-byte slots)
* ``sp + 128`` -- saved ``ra``
* ``sp + 136`` onwards -- parameters, locals and temporaries
* incoming stack arguments -- ``sp + frame_size + 8 * (i - 6)``

Every function allocates a 16-byte-aligned frame and saves/restores ``ra``.
Generated code uses only ``r0``, ``a0``-``a5``, ``t0``-``t3``, ``sp`` and
``ra``.  It never uses ``s0``-``s3``, ``gp`` or ``fp``.  Constants use the
assembler's ``LI`` pseudo, symbol addresses use ``LA``, and direct calls use
``CALL``.  Function-pointer calls use ``LA ra, .Lreturn`` followed by ``JMP``;
no new instruction, register, relocation or ABI convention is introduced.

``generate`` refuses trees that did not pass semantic analysis, because the
lowering pass relies on semantic symbols, types, conversions and layout
annotations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..pseudo import MAX_LI_LENGTH, expand_li
from . import ast, types
from .diagnostics import CompileError, SourceLocation
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
    StructType,
    Type,
    TypeContext,
    UnionType,
    VoidType,
)

__all__ = ["generate", "CodeGenerator"]

# -- Frozen Nova64 software conventions -------------------------------------

WORD_BYTES = 8
WORD_BITS = 64
WORD_MASK = (1 << WORD_BITS) - 1
ARG_REGISTERS = ("a0", "a1", "a2", "a3", "a4", "a5")
OUTGOING_ARG_BYTES = 128
MAX_OUTGOING_CHUNKS = OUTGOING_ARG_BYTES // WORD_BYTES + len(ARG_REGISTERS)
RA_SAVE_OFFSET = OUTGOING_ARG_BYTES
LOCAL_AREA_START = OUTGOING_ARG_BYTES + WORD_BYTES
FRAME_ALIGNMENT = 16
SIMM_MIN = -(1 << 13)
SIMM_MAX = (1 << 13) - 1
UIMM_MAX = (1 << 14) - 1
AGGREGATE_REGISTER_RETURN_BYTES = 16


# -- Small helpers -----------------------------------------------------------


def _strip(t: Type) -> Type:
    return types.strip_qualifiers(t)


def _location(node: object) -> SourceLocation:
    return getattr(node, "location", SourceLocation())


def _unsupported(node: object, what: str) -> CompileError:
    return CompileError.at(_location(node),
                           f"Nova64 code generation does not support {what}")


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def _fits_simm(value: int) -> bool:
    return SIMM_MIN <= value <= SIMM_MAX


def _type_size(t: Type, node: object) -> int:
    size = _strip(t).sizeof()
    if size is None:
        raise _unsupported(node, f"type '{t.describe()}' with no static size")
    return int(size)


def _type_align(t: Type) -> int:
    return max(1, int(_strip(t).alignof() or 1))


def _is_record(t: Type) -> bool:
    return isinstance(_strip(t), (StructType, UnionType))


def _is_aggregate(t: Type) -> bool:
    return isinstance(_strip(t), (StructType, UnionType, ArrayType))


def _is_integer(t: Type) -> bool:
    return types.is_integer_type(t)


def _is_signed(t: Type) -> bool:
    return types.is_signed_integer(t, TypeContext())


def _is_pointer(t: Type) -> bool:
    return isinstance(_strip(t), PointerType)


def _const_int(expr: object) -> Optional[int]:
    value = getattr(expr, "const_value", None)
    if value is None:
        value = getattr(expr, "number", None)
    return None if value is None else int(value)


def _as_asm_bytes(value: str) -> str:
    """Encode an already-decoded C string for the assembler's ``.byte``."""
    raw = value.encode("utf-8") + b"\x00"
    return ", ".join(f"0x{byte:02x}" for byte in raw)


def _instruction_length(line: str) -> int:
    """Number of real instructions a generated assembly line expands to."""
    text = line.strip()
    if not text or text.endswith(":") or text.startswith("."):
        return 0
    parts = text.split(None, 1)
    mnemonic = parts[0].upper()
    if mnemonic == "LA":
        # assemble_object gives every symbolic LA a maximal fixed-width block.
        return MAX_LI_LENGTH
    if mnemonic == "BL":
        # assemble_object gives every symbolic BL a maximal LI plus one JMP.
        return MAX_LI_LENGTH + 1
    if mnemonic == "LI":
        operands = parts[1].split(",") if len(parts) == 2 else []
        if len(operands) == 2:
            return len(expand_li(operands[0].strip(), operands[1].strip()))
    return 1


# -- Data containers ---------------------------------------------------------


@dataclass
class Slot:
    """A fixed eight-byte-oriented offset in the current stack frame."""

    offset: int
    type: Type
    location: SourceLocation
    name: Optional[str] = None


@dataclass
class ExprResult:
    """The slot produced by an expression.

    ``address=True`` means the slot holds the object's address (an lvalue or an
    aggregate value).  Otherwise the slot holds the scalar value itself.
    """

    slot: Slot
    type: Type
    address: bool = False


@dataclass
class Frame:
    next_offset: int = LOCAL_AREA_START
    symbols: Dict[int, Slot] = field(default_factory=dict)
    temporaries: List[Slot] = field(default_factory=list)

    def allocate(self, t: Type, location: SourceLocation,
                 name: Optional[str] = None) -> Slot:
        alignment = min(FRAME_ALIGNMENT, max(WORD_BYTES, _type_align(t)))
        size = max(WORD_BYTES, _type_size(t, None))
        self.next_offset = _align_up(self.next_offset, alignment)
        slot = Slot(self.next_offset, t, location, name)
        self.next_offset += size
        return slot

    def temp(self, t: Type, location: SourceLocation) -> Slot:
        slot = self.allocate(t, location)
        self.temporaries.append(slot)
        return slot

    def size(self) -> int:
        return _align_up(self.next_offset, FRAME_ALIGNMENT)


@dataclass
class _Arg:
    slot: Slot
    type: Type
    aggregate: bool


@dataclass
class _DataObject:
    label: str
    type: Type
    initializer: Optional[ast.Initializer]
    location: SourceLocation
    global_name: Optional[str] = None
    relocations: List[Tuple[int, str]] = field(default_factory=list)


class CodeGenerator:
    """Direct AST-to-Nova64-assembly lowering pass."""

    def __init__(self, tree: ast.TranslationUnit, target: str = "nova64",
                 long_calls: bool = False, long_branches: bool = False):
        self.tree = tree
        self.target = target
        self.long_calls = long_calls
        self.long_branches = long_branches
        # Resolved lazily so ``generate`` can diagnose an unanalysed tree with
        # a source-located compile error instead of an AttributeError.
        self.ctx: Optional[TypeContext] = getattr(tree, "type_context", None)
        self.out: List[str] = []
        self.text: List[str] = []
        self.rodata: List[str] = []
        self.data: List[str] = []
        self.bss: List[str] = []
        self.preamble: List[str] = []

        self.symbol_labels: Dict[int, str] = {}
        self.global_labels: List[str] = []
        self._global_label_set: set[str] = set()
        self.strings: List[Tuple[str, str]] = []
        self.data_objects: Dict[str, _DataObject] = {}
        self.emitted_data: set[str] = set()

        self.frame: Optional[Frame] = None
        self.function: Optional[ast.FunctionDefinition] = None
        self.function_type: Optional[FunctionType] = None
        self.hidden_return_slot: Optional[Slot] = None
        self.va_reg_save: Optional[Slot] = None
        self.va_base: Optional[Slot] = None
        self.epilogue_label: Optional[str] = None
        self.unique = 0
        self.function_counter = 0
        self.labels: Dict[str, str] = {}
        self.break_stack: List[str] = []
        self.continue_stack: List[str] = []
        self.switch_stack: List[dict] = []

    # -- public entry --------------------------------------------------------

    def generate(self) -> str:
        if self.target != "nova64":
            raise CompileError(f"unsupported code generation target '{self.target}'")
        if getattr(self.tree, "semantic_ok", False) is not True:
            raise CompileError.at(
                _location(self.tree),
                "code generation requires a translation unit that passed semantic analysis",
            )
        if not isinstance(getattr(self.tree, "type_context", None), TypeContext):
            raise CompileError("translation unit has no Nova64 type context")

        self._collect_symbols()
        for label in self.global_labels:
            self.preamble.append(f".global {label}")

        self.out = self.text
        self._emit(".section .text")
        for declaration in self.tree.declarations:
            if isinstance(declaration, ast.FunctionDefinition):
                self._emit_function(declaration)
        self._emit_data_sections()

        lines = self.preamble + self.text + self.rodata + self.data + self.bss
        return "\n".join(line for line in lines if line != "") + "\n"

    # -- top-level symbol collection ----------------------------------------

    def _new_label(self, hint: str) -> str:
        self.unique += 1
        return f".Lcgu_{hint}_{self.unique}"

    def _make_global(self, label: str) -> None:
        if label not in self._global_label_set:
            self._global_label_set.add(label)
            self.global_labels.append(label)

    def _collect_symbols(self) -> None:
        for declaration in self.tree.declarations:
            if isinstance(declaration, ast.FunctionDefinition):
                sym = getattr(declaration, "symbol", None)
                if sym is None:
                    raise _unsupported(declaration, "function without semantic symbol")
                if sym.storage == "static":
                    label = self._new_label("fn")
                else:
                    label = sym.name
                    self._make_global(label)
                self.symbol_labels[id(sym)] = label
                continue
            if not isinstance(declaration, ast.Declaration):
                continue
            for init_decl in declaration.init_declarators:
                sym = getattr(init_decl, "symbol", None)
                if sym is None or sym.kind != "object":
                    continue
                if sym.storage == "extern" and not sym.defined:
                    self.symbol_labels.setdefault(id(sym), sym.name)
                    continue
                if sym.storage == "static":
                    label = self._new_label("static")
                else:
                    label = sym.name
                    self._make_global(label)
                self.symbol_labels.setdefault(id(sym), label)
                existing = self.data_objects.get(label)
                if existing is None:
                    self.data_objects[label] = _DataObject(
                        label=label,
                        type=init_decl.resolved_type,
                        initializer=init_decl.initializer,
                        location=init_decl.location,
                        global_name=None if sym.storage == "static" else sym.name,
                    )
                elif init_decl.initializer is not None and existing.initializer is None:
                    existing.initializer = init_decl.initializer
                    existing.type = init_decl.resolved_type

    def _label_for(self, sym: object) -> str:
        label = self.symbol_labels.get(id(sym))
        return label if label is not None else str(getattr(sym, "name", ""))

    # -- emission helpers ----------------------------------------------------

    def _emit(self, text: str) -> None:
        self.out.append(text)

    def _emit_label(self, label: str) -> None:
        self.out.append(label + ":")

    def _emit_jump(self, label: str) -> None:
        """Emit an unconditional intra-function branch without new syntax.

        ``JMP`` takes a register, so a guaranteed-true ``BEQ`` against ``r0``
        is the canonical real-instruction branch to an assembly label.  It uses
        the existing ``PCREL_DISP14`` relocation and needs no extra scratch
        register.  In long-branch mode the documented ``BL`` pseudo removes the
        signed 14-bit displacement limit.
        """
        if self.long_branches:
            self._emit(f"BL {label}")
        else:
            self._emit(f"BEQ r0, r0, {label}")

    _BRANCH_INVERSIONS = {
        "BEQ": "BNE",
        "BNE": "BEQ",
        "BLT": "BGE",
        "BGE": "BLT",
        "BLE": "BGT",
        "BGT": "BLE",
        "BLTU": "BGEU",
        "BGEU": "BLTU",
        "BLEU": "BGTU",
        "BGTU": "BLEU",
    }

    def _emit_branch(self, mnemonic: str, operands: str, label: str) -> None:
        """Emit a conditional branch, optionally without a range limit."""
        if not self.long_branches:
            self._emit(f"{mnemonic} {operands}, {label}")
            return
        skip = self._new_label("branch_skip")
        inverse = self._BRANCH_INVERSIONS[mnemonic]
        self._emit(f"{inverse} {operands}, {skip}")
        self._emit(f"BL {label}")
        self._emit_label(skip)

    def _emit_li(self, reg: str, value: int) -> None:
        self._emit(f"LI {reg}, {int(value) & WORD_MASK}")

    def _emit_addi(self, rd: str, rs: str, imm: int) -> None:
        if imm == 0:
            self._emit(f"MOV {rd}, {rs}")
        elif _fits_simm(imm):
            self._emit(f"ADDI {rd}, {rs}, {imm}")
        else:
            self._emit_li("t3", imm)
            self._emit(f"ADD {rd}, {rs}, t3")

    def _slot_address(self, slot: Slot, reg: str) -> None:
        self._emit_addi(reg, "sp", slot.offset)

    def _load_slot(self, slot: Slot, reg: str) -> None:
        if _fits_simm(slot.offset):
            self._emit(f"LDD {reg}, [sp + {slot.offset}]")
        else:
            self._emit_addi("t3", "sp", slot.offset)
            self._emit(f"LDD {reg}, [t3]")

    def _store_slot(self, slot: Slot, reg: str) -> None:
        if _fits_simm(slot.offset):
            self._emit(f"STD {reg}, [sp + {slot.offset}]")
        else:
            self._emit_addi("t3", "sp", slot.offset)
            self._emit(f"STD {reg}, [t3]")

    def _temp(self, t: Type, node: object) -> Slot:
        assert self.frame is not None
        return self.frame.temp(t, _location(node))

    # -- typed memory access -------------------------------------------------

    def _load_op(self, t: Type, node: object) -> str:
        t0 = _strip(t)
        if isinstance(t0, FloatingType):
            raise _unsupported(node, "floating-point values")
        if isinstance(t0, (StructType, UnionType, ArrayType, FunctionType)):
            raise _unsupported(node, f"direct load of '{t.describe()}'")
        size = _type_size(t0, node)
        signed = _is_signed(t0)
        op = {
            (1, True): "LDB", (1, False): "LDBU",
            (2, True): "LDH", (2, False): "LDHU",
            (4, True): "LDW", (4, False): "LDWU",
            (8, True): "LDD", (8, False): "LDD",
        }.get((size, signed))
        if op is None:
            raise _unsupported(node, f"load of type '{t.describe()}'")
        return op

    def _store_op(self, t: Type, node: object) -> str:
        t0 = _strip(t)
        if isinstance(t0, FloatingType):
            raise _unsupported(node, "floating-point values")
        if isinstance(t0, (StructType, UnionType, ArrayType, FunctionType)):
            raise _unsupported(node, f"direct store of '{t.describe()}'")
        op = {1: "STB", 2: "STH", 4: "STW", 8: "STD"}.get(_type_size(t0, node))
        if op is None:
            raise _unsupported(node, f"store of type '{t.describe()}'")
        return op

    def _load_from_slot_typed(self, slot: Slot, dest: str, t: Type,
                              node: object) -> None:
        op = self._load_op(t, node)
        if _fits_simm(slot.offset):
            self._emit(f"{op} {dest}, [sp + {slot.offset}]")
        else:
            self._slot_address(slot, "t2")
            self._emit(f"{op} {dest}, [t2]")

    def _store_to_slot_typed(self, slot: Slot, src: str, t: Type,
                             node: object) -> None:
        op = self._store_op(t, node)
        if _fits_simm(slot.offset):
            self._emit(f"{op} {src}, [sp + {slot.offset}]")
        else:
            self._slot_address(slot, "t2")
            self._emit(f"{op} {src}, [t2]")

    def _load_through_slot(self, slot: Slot, dest: str, t: Type,
                           node: object) -> None:
        """Load an object whose address is stored in ``slot``."""
        op = self._load_op(t, node)
        self._load_slot(slot, "t2")
        self._emit(f"{op} {dest}, [t2]")

    def _store_through_slot(self, slot: Slot, src: str, t: Type,
                            node: object) -> None:
        """Store to an object whose address is stored in ``slot``."""
        op = self._store_op(t, node)
        self._load_slot(slot, "t2")
        self._emit(f"{op} {src}, [t2]")

    def _normalize_scalar(self, reg: str, t: Type) -> None:
        t0 = _strip(t)
        if isinstance(t0, PointerType):
            return
        size = int(t0.sizeof() or WORD_BYTES)
        bits = size * 8
        if bits >= WORD_BITS:
            return
        shift = WORD_BITS - bits
        self._emit(f"SHLI {reg}, {reg}, {shift}")
        if _is_signed(t0) and not isinstance(t0, BoolType):
            self._emit(f"SARI {reg}, {reg}, {shift}")
        else:
            self._emit(f"SHRI {reg}, {reg}, {shift}")

    def _materialize(self, reg: str, t: Type, node: object) -> ExprResult:
        slot = self._temp(t, node)
        self._normalize_scalar(reg, t)
        self._store_slot(slot, reg)
        return ExprResult(slot, t, False)

    # -- aggregate helpers ---------------------------------------------------

    def _zero_slot(self, slot: Slot, size: int, node: object) -> None:
        self._emit("MOVI t0, 0")
        offset = 0
        while offset + WORD_BYTES <= size:
            self._slot_address(slot, "t2")
            if offset:
                self._emit_addi("t2", "t2", offset)
            self._emit("STD t0, [t2]")
            offset += WORD_BYTES
        for width, op in ((4, "STW"), (2, "STH"), (1, "STB")):
            if offset + width <= size:
                self._slot_address(slot, "t2")
                if offset:
                    self._emit_addi("t2", "t2", offset)
                self._emit(f"{op} t0, [t2]")
                offset += width

    def _copy_object(self, dest: Slot, src: Slot, size: int,
                     node: object) -> None:
        offset = 0
        while offset + WORD_BYTES <= size:
            self._load_slot(src, "t0")
            self._emit_addi("t0", "t0", offset)
            self._load_slot(dest, "t1")
            self._emit_addi("t1", "t1", offset)
            self._emit("LDD t2, [t0]")
            self._emit("STD t2, [t1]")
            offset += WORD_BYTES
        for width, load_op, store_op in (
            (4, "LDWU", "STW"), (2, "LDHU", "STH"), (1, "LDBU", "STB")
        ):
            if offset + width <= size:
                self._load_slot(src, "t0")
                self._emit_addi("t0", "t0", offset)
                self._load_slot(dest, "t1")
                self._emit_addi("t1", "t1", offset)
                self._emit(f"{load_op} t2, [t0]")
                self._emit(f"{store_op} t2, [t1]")
                offset += width

    def _load_chunk(self, addr: Slot, offset: int, size: int,
                    dest: str, node: object) -> None:
        self._load_slot(addr, "t2")
        if offset:
            self._emit_addi("t2", "t2", offset)
        if size >= 8:
            self._emit(f"LDD {dest}, [t2]")
            return
        op = {4: "LDWU", 2: "LDHU", 1: "LDBU"}.get(size)
        if op is not None:
            self._emit(f"{op} {dest}, [t2]")
            return
        if size <= 0:
            raise _unsupported(node, "zero-sized ABI chunk")
        # The ABI passes a short final chunk as a full eight-byte slot.  Build
        # the value from legal widths instead of reading past the object.
        if size >= 4:
            self._emit("LDWU t0, [t2]")
            remaining = size - 4
            position = 4
        else:
            self._emit("MOVI t0, 0")
            remaining = size
            position = 0
        while remaining > 0:
            width = 2 if remaining >= 2 else 1
            op = "LDHU" if width == 2 else "LDBU"
            self._emit(f"{op} t3, [t2 + {position}]")
            self._emit(f"SHLI t3, t3, {position * 8}")
            self._emit("OR t0, t0, t3")
            remaining -= width
            position += width
        if dest != "t0":
            self._emit(f"MOV {dest}, t0")

    def _store_chunk(self, addr: Slot, offset: int, size: int,
                     src: str, node: object) -> None:
        self._load_slot(addr, "t2")
        self._store_chunk_at("t2", offset, size, src, node)

    def _store_chunk_at(self, base: str, offset: int, size: int,
                        src: str, node: object) -> None:
        if offset:
            self._emit_addi(base, base, offset)
        if size >= 8:
            self._emit(f"STD {src}, [{base}]")
            return
        op = {4: "STW", 2: "STH", 1: "STB"}.get(size)
        if op is not None:
            self._emit(f"{op} {src}, [{base}]")
            return
        if size <= 0:
            raise _unsupported(node, "zero-sized ABI chunk")
        # Store a short final chunk without overwriting neighbouring bytes.
        if size >= 4:
            self._emit(f"STW {src}, [t2]")
            remaining = size - 4
            position = 4
            self._emit(f"MOV t1, {src}")
            self._emit("SHRI t1, t1, 32")
        else:
            remaining = size
            position = 0
            self._emit(f"MOV t1, {src}")
        while remaining > 0:
            width = 2 if remaining >= 2 else 1
            op = "STH" if width == 2 else "STB"
            self._emit(f"{op} t1, [t2 + {position}]")
            if remaining > width:
                self._emit(f"SHRI t1, t1, {width * 8}")
            remaining -= width
            position += width

    def _chunk_count(self, t: Type, node: object) -> int:
        if _is_record(t):
            size = _type_size(t, node)
            return max(1, (size + WORD_BYTES - 1) // WORD_BYTES)
        return 1

    # -- conversions and expression values ----------------------------------

    def _convert(self, src: Slot, source: Type, target: Type,
                 dest: Slot, node: object) -> None:
        source0, target0 = _strip(source), _strip(target)
        if isinstance(source0, FloatingType) or isinstance(target0, FloatingType):
            raise _unsupported(node, "floating-point conversion")
        if isinstance(target0, VoidType):
            return
        if not (types.is_scalar_type(source0) or isinstance(source0, PointerType)):
            raise _unsupported(node, f"conversion from '{source.describe()}'")
        if not (types.is_scalar_type(target0) or isinstance(target0, PointerType)):
            raise _unsupported(node, f"conversion to '{target.describe()}'")

        self._load_slot(src, "t0")
        if isinstance(target0, BoolType):
            true_label, end_label = self._new_label("bool_true"), self._new_label("bool_end")
            self._emit_branch("BNE", "t0, r0", true_label)
            self._emit("MOVI t0, 0")
            self._emit_jump(end_label)
            self._emit_label(true_label)
            self._emit("MOVI t0, 1")
            self._emit_label(end_label)
        else:
            self._normalize_scalar("t0", target0)
        self._store_slot(dest, "t0")

    def _to_rvalue(self, result: ExprResult, expr: ast.Expression) -> ExprResult:
        t0 = _strip(result.type)
        if isinstance(t0, ArrayType):
            # The slot already contains the array's address.
            return ExprResult(result.slot, PointerType(t0.element), False)
        if isinstance(t0, FunctionType):
            return ExprResult(result.slot, PointerType(t0), False)
        if isinstance(t0, (StructType, UnionType)):
            return result
        if not result.address:
            return result
        dest = self._temp(result.type, expr)
        self._load_through_slot(result.slot, "t0", result.type, expr)
        self._store_slot(dest, "t0")
        return ExprResult(dest, t0, False)

    def _apply_implicit(self, expr: ast.Expression,
                        result: ExprResult) -> ExprResult:
        target = getattr(expr, "implicit_cast_to", None)
        if target is None:
            return result
        target0 = _strip(target)
        if isinstance(target0, (VoidType, StructType, UnionType, ArrayType, FunctionType)):
            return result
        value = self._to_rvalue(result, expr)
        if types.type_eq(_strip(value.type), target0):
            return value
        dest = self._temp(target, expr)
        self._convert(value.slot, value.type, target, dest, expr)
        return ExprResult(dest, target0, False)

    def _gen_expr(self, expr: ast.Expression) -> ExprResult:
        return self._apply_implicit(expr, self._gen_expr_raw(expr))

    def _gen_rvalue(self, expr: ast.Expression) -> ExprResult:
        return self._to_rvalue(self._gen_expr(expr), expr)

    # -- address generation --------------------------------------------------

    def _string_label(self, expr: ast.StringLiteral) -> str:
        for label, value in self.strings:
            if value == expr.value:
                return label
        label = self._new_label("str")
        self.strings.append((label, expr.value))
        return label

    def _gen_addr(self, expr: ast.Expression) -> ExprResult:
        if isinstance(expr, ast.IdentifierExpression):
            sym = getattr(expr, "symbol", None)
            if sym is None or sym.kind not in ("object", "function"):
                raise _unsupported(expr, f"address of '{expr.name}'")
            slot = self._temp(PointerType(expr.resolved_type), expr)
            if id(sym) in self.symbol_labels:
                self._emit(f"LA t0, {self._label_for(sym)}")
                self._store_slot(slot, "t0")
            elif self.frame is not None and id(sym) in self.frame.symbols:
                self._slot_address(self.frame.symbols[id(sym)], "t0")
                self._store_slot(slot, "t0")
            else:
                raise _unsupported(expr, f"object '{expr.name}' without storage")
            return ExprResult(slot, expr.resolved_type, True)

        if isinstance(expr, ast.StringLiteral):
            slot = self._temp(PointerType(expr.resolved_type), expr)
            self._emit(f"LA t0, {self._string_label(expr)}")
            self._store_slot(slot, "t0")
            return ExprResult(slot, expr.resolved_type, True)

        if isinstance(expr, ast.UnaryExpression) and expr.op == "*" and not expr.postfix:
            pointer = self._gen_rvalue(expr.operand)
            p = _strip(pointer.type)
            if not isinstance(p, PointerType):
                raise _unsupported(expr, "indirection on a non-pointer")
            return ExprResult(pointer.slot, p.pointee, True)

        if isinstance(expr, ast.SubscriptExpression):
            return self._subscript_address(expr)

        if isinstance(expr, ast.MemberAccessExpression):
            return self._member_address(expr)

        if isinstance(expr, ast.CompoundLiteral):
            t = self._type_of_name(expr.type_name)
            obj_slot = self._temp(t, expr)
            self._zero_slot(obj_slot, _type_size(t, expr), expr)
            self._emit_initializer_at(obj_slot, t, expr.initializer, 0, expr)
            slot = self._temp(PointerType(t), expr)
            self._slot_address(obj_slot, "t0")
            self._store_slot(slot, "t0")
            return ExprResult(slot, t, True)

        if isinstance(expr, ast.CallExpression):
            result = self._gen_expr_raw(expr)
            if not _is_aggregate(result.type) or not result.address:
                raise _unsupported(expr, "address of a non-aggregate call result")
            return result

        if isinstance(expr, (ast.ConditionalExpression, ast.CommaExpression,
                             ast.GenericSelection)):
            result = self._gen_expr(expr)
            if not result.address:
                raise _unsupported(expr, "address of a non-lvalue expression")
            return result

        raise _unsupported(expr, f"address of {type(expr).__name__}")

    def _type_of_name(self, type_name: ast.TypeName) -> Type:
        t = getattr(type_name, "resolved_type", None)
        if t is None:
            raise _unsupported(type_name, "type name without semantic analysis")
        return t

    def _subscript_address(self, expr: ast.SubscriptExpression) -> ExprResult:
        base = self._gen_rvalue(expr.array)
        index = self._gen_rvalue(expr.index)
        base_t = _strip(base.type)
        if not isinstance(base_t, PointerType):
            raise _unsupported(expr, "subscript of a non-pointer")
        elem = base_t.pointee
        size = _type_size(elem, expr)
        slot = self._temp(PointerType(elem), expr)
        self._load_slot(base.slot, "t0")
        self._load_slot(index.slot, "t1")
        if size != 1:
            self._emit_li("t2", size)
            self._emit("MUL t1, t1, t2")
        self._emit("ADD t0, t0, t1")
        self._store_slot(slot, "t0")
        return ExprResult(slot, elem, True)

    def _member_address(self, expr: ast.MemberAccessExpression) -> ExprResult:
        field = getattr(expr, "symbol", None)
        if not isinstance(field, FieldInfo):
            raise _unsupported(expr, "member access without semantic field information")
        if field.bit_width is not None:
            raise _unsupported(expr, "bit-field access")
        if isinstance(_strip(field.type), FloatingType):
            raise _unsupported(expr, "floating-point struct members")

        if expr.pointer:
            base = self._gen_rvalue(expr.base)
            base_t = _strip(base.type)
            if not isinstance(base_t, PointerType):
                raise _unsupported(expr, "'->' on a non-pointer")
            base_slot = base.slot
        else:
            base = self._gen_addr(expr.base)
            base_slot = base.slot

        slot = self._temp(PointerType(field.type), expr)
        self._load_slot(base_slot, "t0")
        if field.offset:
            self._emit_addi("t0", "t0", field.offset)
        self._store_slot(slot, "t0")
        return ExprResult(slot, field.type, True)

    # -- expression lowering -------------------------------------------------

    def _gen_expr_raw(self, expr: ast.Expression) -> ExprResult:
        if isinstance(expr, (ast.IntegerLiteral, ast.CharacterLiteral)):
            value = _const_int(expr)
            if value is None:
                raise _unsupported(expr, "integer literal without a folded value")
            slot = self._temp(expr.resolved_type, expr)
            self._emit_li("t0", value)
            self._store_slot(slot, "t0")
            return ExprResult(slot, expr.resolved_type, False)

        if isinstance(expr, ast.StringLiteral):
            return self._gen_addr(expr)

        if isinstance(expr, ast.IdentifierExpression):
            sym = getattr(expr, "symbol", None)
            if sym is None:
                raise _unsupported(expr, f"unresolved identifier '{expr.name}'")
            if sym.kind == "enum_const":
                slot = self._temp(self.ctx.int_t, expr)
                self._emit_li("t0", int(sym.enum_value or 0))
                self._store_slot(slot, "t0")
                return ExprResult(slot, self.ctx.int_t, False)
            return self._gen_addr(expr)

        if isinstance(expr, ast.UnaryExpression):
            return self._gen_unary(expr)

        if isinstance(expr, ast.BinaryExpression):
            return self._gen_binary(expr)

        if isinstance(expr, ast.AssignmentExpression):
            return self._gen_assignment(expr)

        if isinstance(expr, ast.ConditionalExpression):
            return self._gen_conditional(expr)

        if isinstance(expr, ast.CastExpression):
            target = self._type_of_name(expr.type_name)
            if isinstance(_strip(target), VoidType):
                value = self._gen_rvalue(expr.operand)
                return ExprResult(value.slot, target, False)
            value = self._gen_rvalue(expr.operand)
            dest = self._temp(target, expr)
            self._convert(value.slot, value.type, target, dest, expr)
            return ExprResult(dest, target, False)

        if isinstance(expr, ast.CallExpression):
            return self._gen_call(expr)

        if isinstance(expr, ast.VaArgExpression):
            return self._gen_va_arg(expr)

        if isinstance(expr, ast.SubscriptExpression):
            return self._gen_addr(expr)

        if isinstance(expr, ast.MemberAccessExpression):
            return self._gen_addr(expr)

        if isinstance(expr, ast.SizeofExpression):
            value = _const_int(expr)
            if value is None:
                raise _unsupported(expr, "sizeof without a folded value")
            slot = self._temp(expr.resolved_type, expr)
            self._emit_li("t0", value)
            self._store_slot(slot, "t0")
            return ExprResult(slot, expr.resolved_type, False)

        if isinstance(expr, ast.CommaExpression):
            result: Optional[ExprResult] = None
            for item in expr.expressions:
                result = self._gen_expr(item)
            if result is None:
                raise _unsupported(expr, "empty comma expression")
            return result

        if isinstance(expr, ast.CompoundLiteral):
            return self._gen_addr(expr)

        if isinstance(expr, ast.GenericSelection):
            selected = getattr(expr, "selected", None)
            if not isinstance(selected, ast.GenericAssociation):
                raise _unsupported(expr, "_Generic selection without semantic result")
            return self._gen_expr(selected.expression)

        if isinstance(expr, ast.PostfixExpression):
            return self._gen_expr(expr.operand)

        raise _unsupported(expr, f"expression {type(expr).__name__}")

    def _gen_unary(self, expr: ast.UnaryExpression) -> ExprResult:
        op = expr.op
        if op in ("++", "--"):
            return self._gen_incdec(expr)
        if op == "&":
            addr = self._gen_addr(expr.operand)
            pointer_type = PointerType(expr.operand.resolved_type)
            slot = self._temp(pointer_type, expr)
            self._load_slot(addr.slot, "t0")
            self._store_slot(slot, "t0")
            return ExprResult(slot, pointer_type, False)
        if op == "*":
            return self._gen_addr(expr)

        operand = self._gen_rvalue(expr.operand)
        result_type = expr.resolved_type
        if op == "+":
            return operand
        if op == "-":
            self._load_slot(operand.slot, "t0")
            self._emit("NEG t0, t0")
            return self._materialize("t0", result_type, expr)
        if op == "~":
            self._load_slot(operand.slot, "t0")
            self._emit("NOT t0, t0")
            return self._materialize("t0", result_type, expr)
        if op == "!":
            slot = self._temp(result_type, expr)
            true_label, end_label = (self._new_label("not_true"),
                                     self._new_label("not_end"))
            self._load_slot(operand.slot, "t0")
            self._emit_branch("BNE", "t0, r0", true_label)
            self._emit("MOVI t0, 1")
            self._emit_jump(end_label)
            self._emit_label(true_label)
            self._emit("MOVI t0, 0")
            self._emit_label(end_label)
            self._store_slot(slot, "t0")
            return ExprResult(slot, result_type, False)
        raise _unsupported(expr, f"unary operator '{op}'")

    def _gen_incdec(self, expr: ast.UnaryExpression) -> ExprResult:
        addr = self._gen_addr(expr.operand)
        t = _strip(expr.operand.resolved_type)
        old_slot = self._temp(t, expr)
        new_slot = self._temp(t, expr)
        self._load_through_slot(addr.slot, "t0", t, expr)
        self._store_slot(old_slot, "t0")

        delta = 1
        pointer = _strip(t)
        if isinstance(pointer, PointerType):
            delta = _type_size(pointer.pointee, expr)
            self._emit_li("t2", delta)
            self._emit("ADD t1, t0, t2")
        elif _is_integer(t):
            self._emit("SUBI t1, t0, 1" if expr.op == "--" else "ADDI t1, t0, 1")
        else:
            raise _unsupported(expr, f"increment/decrement of '{t.describe()}'")

        if expr.op == "--" and isinstance(pointer, PointerType):
            # The pointer step was computed as a positive size; subtract it.
            self._load_slot(old_slot, "t0")
            self._emit("SUB t1, t0, t2")
        self._normalize_scalar("t1", t)
        self._store_slot(new_slot, "t1")
        self._store_through_slot(addr.slot, "t1", t, expr)
        return ExprResult(new_slot if not expr.postfix else old_slot,
                          expr.resolved_type, False)

    def _comparison_unsigned(self, expr: ast.BinaryExpression) -> bool:
        for operand, fallback in ((expr.left, expr.left.resolved_type),
                                  (expr.right, expr.right.resolved_type)):
            target = getattr(operand, "implicit_cast_to", None)
            t = _strip(target) if target is not None else _strip(fallback)
            if isinstance(t, PointerType):
                return True
            if _is_integer(t) and not _is_signed(t):
                return True
        return False

    def _emit_branch_compare(self, op: str, left: Slot, right: Slot,
                             true_label: str, unsigned: bool,
                             node: object) -> None:
        table = {
            "==": "BEQ", "!=": "BNE",
            "<": "BLTU" if unsigned else "BLT",
            "<=": "BLEU" if unsigned else "BLE",
            ">": "BGTU" if unsigned else "BGT",
            ">=": "BGEU" if unsigned else "BGE",
        }
        mnemonic = table.get(op)
        if mnemonic is None:
            raise _unsupported(node, f"comparison operator '{op}'")
        self._load_slot(left, "t0")
        self._load_slot(right, "t1")
        self._emit_branch(mnemonic, "t0, t1", true_label)

    def _gen_binary(self, expr: ast.BinaryExpression) -> ExprResult:
        op = expr.op
        if op in ("&&", "||"):
            return self._gen_logical(expr)
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return self._gen_compare(expr)
        return self._gen_arithmetic(expr)

    def _gen_logical(self, expr: ast.BinaryExpression) -> ExprResult:
        slot = self._temp(expr.resolved_type, expr)
        true_label = self._new_label("logic_true")
        false_label = self._new_label("logic_false")
        end_label = self._new_label("logic_end")
        self._gen_cond(expr, true_label, false_label)
        self._emit_label(true_label)
        self._emit("MOVI t0, 1")
        self._store_slot(slot, "t0")
        self._emit_jump(end_label)
        self._emit_label(false_label)
        self._emit("MOVI t0, 0")
        self._store_slot(slot, "t0")
        self._emit_label(end_label)
        return ExprResult(slot, expr.resolved_type, False)

    def _gen_compare(self, expr: ast.BinaryExpression) -> ExprResult:
        left = self._gen_rvalue(expr.left)
        right = self._gen_rvalue(expr.right)
        slot = self._temp(expr.resolved_type, expr)
        true_label = self._new_label("cmp_true")
        end_label = self._new_label("cmp_end")
        self._emit_branch_compare(expr.op, left.slot, right.slot, true_label,
                                  self._comparison_unsigned(expr), expr)
        self._emit("MOVI t0, 0")
        self._store_slot(slot, "t0")
        self._emit_jump(end_label)
        self._emit_label(true_label)
        self._emit("MOVI t0, 1")
        self._store_slot(slot, "t0")
        self._emit_label(end_label)
        return ExprResult(slot, expr.resolved_type, False)

    def _gen_arithmetic(self, expr: ast.BinaryExpression) -> ExprResult:
        op = expr.op
        left = self._gen_rvalue(expr.left)
        right = self._gen_rvalue(expr.right)
        lt, rt = _strip(left.type), _strip(right.type)
        result_type = expr.resolved_type

        if isinstance(lt, PointerType) and _is_integer(rt) and op in ("+", "-"):
            step = _type_size(lt.pointee, expr)
            self._load_slot(right.slot, "t1")
            if step != 1:
                self._emit_li("t2", step)
                self._emit("MUL t1, t1, t2")
            self._load_slot(left.slot, "t0")
            self._emit(("SUB t0, t0, t1" if op == "-" else "ADD t0, t0, t1"))
            slot = self._temp(result_type, expr)
            self._store_slot(slot, "t0")
            return ExprResult(slot, result_type, False)

        if isinstance(rt, PointerType) and _is_integer(lt) and op == "+":
            step = _type_size(rt.pointee, expr)
            self._load_slot(left.slot, "t1")
            if step != 1:
                self._emit_li("t2", step)
                self._emit("MUL t1, t1, t2")
            self._load_slot(right.slot, "t0")
            self._emit("ADD t0, t0, t1")
            slot = self._temp(result_type, expr)
            self._store_slot(slot, "t0")
            return ExprResult(slot, result_type, False)

        if isinstance(lt, PointerType) and isinstance(rt, PointerType) and op == "-":
            step = _type_size(lt.pointee, expr)
            self._load_slot(left.slot, "t0")
            self._load_slot(right.slot, "t1")
            self._emit("SUB t0, t0, t1")
            if step != 1:
                self._emit_li("t1", step)
                self._emit("DIV t0, t0, t1")
            slot = self._temp(result_type, expr)
            self._store_slot(slot, "t0")
            return ExprResult(slot, result_type, False)

        if not ((_is_integer(lt) or isinstance(lt, PointerType)) and
                (_is_integer(rt) or isinstance(rt, PointerType))):
            raise _unsupported(expr, f"binary operator '{op}' on non-scalar values")

        self._load_slot(left.slot, "t0")
        self._load_slot(right.slot, "t1")
        if op == "+":
            self._emit("ADD t0, t0, t1")
        elif op == "-":
            self._emit("SUB t0, t0, t1")
        elif op == "*":
            self._emit("MUL t0, t0, t1")
        elif op == "/":
            self._emit("UDIV t0, t0, t1" if not _is_signed(result_type)
                       else "DIV t0, t0, t1")
        elif op == "%":
            self._emit("UREM t0, t0, t1" if not _is_signed(result_type)
                       else "REM t0, t0, t1")
        elif op == "&":
            self._emit("AND t0, t0, t1")
        elif op == "|":
            self._emit("OR t0, t0, t1")
        elif op == "^":
            self._emit("XOR t0, t0, t1")
        elif op == "<<":
            self._emit("SHL t0, t0, t1")
        elif op == ">>":
            self._emit("SAR t0, t0, t1" if _is_signed(lt) else "SHR t0, t0, t1")
        else:
            raise _unsupported(expr, f"binary operator '{op}'")
        return self._materialize("t0", result_type, expr)

    def _gen_assignment(self, expr: ast.AssignmentExpression) -> ExprResult:
        addr = self._gen_addr(expr.left)
        target = _strip(addr.type)

        if expr.op == "=":
            if _is_record(target):
                value = self._gen_expr(expr.right)
                if not (value.address and _is_aggregate(value.type)):
                    raise _unsupported(expr, "record assignment without an address")
                self._copy_object(addr.slot, value.slot, _type_size(target, expr), expr)
                return value

            value = self._gen_rvalue(expr.right)
            result = self._coerce_value(value, target, expr)
            self._store_through_slot(addr.slot, "t0", target, expr)
            self._load_slot(result.slot, "t0")
            return ExprResult(result.slot, expr.resolved_type, False)

        # Compound assignment: use the LHS's converted value, apply the
        # operation, then convert the result back to the declared LHS type.
        current = self._temp(target, expr)
        self._load_through_slot(addr.slot, "t0", target, expr)
        self._store_slot(current, "t0")
        raw_rhs = self._gen_rvalue(expr.right)
        binop = expr.op[:-1]
        target_stripped = _strip(target)
        pointer_op = (isinstance(target_stripped, PointerType) and
                      binop in ("+", "-") and _is_integer(raw_rhs.type))
        rhs = (raw_rhs if pointer_op
               else self._coerce_value(raw_rhs, target, expr))

        if pointer_op:
            step = _type_size(target_stripped.pointee, expr)
            self._load_slot(rhs.slot, "t1")
            if step != 1:
                self._emit_li("t2", step)
                self._emit("MUL t1, t1, t2")
            self._load_slot(current, "t0")
            self._emit("SUB t0, t0, t1" if binop == "-" else "ADD t0, t0, t1")
            slot = self._temp(expr.resolved_type, expr)
            self._store_slot(slot, "t0")
            self._store_through_slot(addr.slot, "t0", target, expr)
            return ExprResult(slot, expr.resolved_type, False)

        self._load_slot(current, "t0")
        self._load_slot(rhs.slot, "t1")
        if binop == "+":
            self._emit("ADD t0, t0, t1")
        elif binop == "-":
            self._emit("SUB t0, t0, t1")
        elif binop == "*":
            self._emit("MUL t0, t0, t1")
        elif binop == "/":
            self._emit("UDIV t0, t0, t1" if not _is_signed(target) else "DIV t0, t0, t1")
        elif binop == "%":
            self._emit("UREM t0, t0, t1" if not _is_signed(target) else "REM t0, t0, t1")
        elif binop == "&":
            self._emit("AND t0, t0, t1")
        elif binop == "|":
            self._emit("OR t0, t0, t1")
        elif binop == "^":
            self._emit("XOR t0, t0, t1")
        elif binop == "<<":
            self._emit("SHL t0, t0, t1")
        elif binop == ">>":
            self._emit("SAR t0, t0, t1" if _is_signed(target) else "SHR t0, t0, t1")
        else:
            raise _unsupported(expr, f"compound assignment '{expr.op}'")
        result = self._materialize("t0", target, expr)
        self._store_through_slot(addr.slot, "t0", target, expr)
        return ExprResult(result.slot, expr.resolved_type, False)

    def _coerce_value(self, value: ExprResult, target: Type,
                      node: object) -> ExprResult:
        """Apply a missing narrowing/same-width conversion after sema passes."""
        target0 = _strip(target)
        if isinstance(target0, (VoidType, StructType, UnionType, ArrayType, FunctionType)):
            return value
        if types.type_eq(_strip(value.type), target0):
            return value
        dest = self._temp(target, node)
        self._convert(value.slot, value.type, target, dest, node)
        return ExprResult(dest, target0, False)

    def _store_result(self, dest: Slot, value: ExprResult, target: Type,
                      node: object) -> None:
        target0 = _strip(target)
        if isinstance(target0, VoidType):
            return
        if _is_record(target0):
            if not (value.address and _is_aggregate(value.type)):
                raise _unsupported(node, "aggregate value without an address")
            self._copy_object(dest, value.slot, _type_size(target0, node), node)
            return
        value = self._coerce_value(value, target0, node)
        self._load_slot(value.slot, "t0")
        self._store_slot(dest, "t0")

    def _gen_conditional(self, expr: ast.ConditionalExpression) -> ExprResult:
        result_type = expr.resolved_type
        aggregate = _is_aggregate(result_type)
        obj_slot = self._temp(result_type, expr) if aggregate else None
        dest = (self._temp(PointerType(result_type), expr) if aggregate
                else self._temp(result_type, expr))
        if aggregate:
            assert obj_slot is not None
            self._slot_address(obj_slot, "t0")
            self._store_slot(dest, "t0")

        true_label = self._new_label("cond_true")
        false_label = self._new_label("cond_false")
        end_label = self._new_label("cond_end")
        self._gen_cond(expr.condition, true_label, false_label)

        self._emit_label(true_label)
        if expr.then_expr is not None:
            then_value = self._gen_rvalue(expr.then_expr)
        else:
            then_value = self._gen_rvalue(expr.condition)
        self._store_result(dest, then_value, result_type, expr)
        self._emit_jump(end_label)

        self._emit_label(false_label)
        else_value = self._gen_rvalue(expr.else_expr)
        self._store_result(dest, else_value, result_type, expr)
        self._emit_label(end_label)
        return ExprResult(dest, result_type, aggregate)

    # -- calls ---------------------------------------------------------------

    def _function_type(self, callee: ast.Expression) -> FunctionType:
        t = getattr(callee, "decayed_type", None)
        if t is None:
            t = getattr(callee, "resolved_type", None)
        t0 = _strip(t) if t is not None else None
        if isinstance(t0, FunctionType):
            return t0
        if isinstance(t0, PointerType) and isinstance(_strip(t0.pointee), FunctionType):
            return _strip(t0.pointee)  # type: ignore[return-value]
        raise _unsupported(callee, "call of an unknown function type")

    def _place_chunk(self, reg_index: int, value: str, node: object) -> None:
        if reg_index < len(ARG_REGISTERS):
            self._emit(f"MOV {ARG_REGISTERS[reg_index]}, {value}")
            return
        stack_index = reg_index - len(ARG_REGISTERS)
        if stack_index >= MAX_OUTGOING_CHUNKS:
            raise _unsupported(node, "call with too many argument chunks")
        self._emit(f"STD {value}, [sp + {stack_index * WORD_BYTES}]")

    def _gen_call(self, expr: ast.CallExpression) -> ExprResult:
        if isinstance(expr.callee, ast.IdentifierExpression):
            builtin = expr.callee.name
            if builtin in ("__builtin_va_start", "__builtin_va_end",
                           "__builtin_va_copy"):
                return self._gen_va_builtin(builtin, expr)
        ft = self._function_type(expr.callee)
        return_type = _strip(ft.return_type)
        is_void = isinstance(return_type, VoidType)
        is_aggregate = _is_aggregate(return_type)
        hidden = (is_aggregate and
                  _type_size(return_type, expr) > AGGREGATE_REGISTER_RETURN_BYTES)
        result_obj: Optional[Slot] = None
        result_slot: Optional[Slot] = None
        if not is_void:
            # Aggregates need an object slot plus a pointer-valued result slot;
            # scalar values live directly in their result slot.
            result_obj = self._temp(return_type, expr) if is_aggregate else None
            result_slot = (self._temp(PointerType(return_type), expr)
                           if is_aggregate else self._temp(return_type, expr))

        # Evaluate all arguments before loading call-chunk registers.
        args: List[_Arg] = []
        for arg in expr.arguments:
            result = self._gen_expr(arg)
            if _is_aggregate(result.type):
                if not result.address:
                    raise _unsupported(arg, "aggregate argument without an address")
                args.append(_Arg(result.slot, result.type, True))
            else:
                value = self._to_rvalue(result, arg)
                args.append(_Arg(value.slot, value.type, False))

        reg_index = 1 if hidden else 0
        if hidden:
            assert result_obj is not None
            self._slot_address(result_obj, "t0")
            self._place_chunk(0, "t0", expr)

        for arg in args:
            if arg.aggregate:
                size = _type_size(arg.type, expr)
                offset = 0
                while offset < size:
                    chunk = min(WORD_BYTES, size - offset)
                    self._load_chunk(arg.slot, offset, chunk, "t0", expr)
                    self._place_chunk(reg_index, "t0", expr)
                    offset += WORD_BYTES
                    reg_index += 1
            else:
                self._load_slot(arg.slot, "t0")
                self._place_chunk(reg_index, "t0", expr)
                reg_index += 1

        sym = getattr(expr.callee, "symbol", None)
        if isinstance(expr.callee, ast.IdentifierExpression) and sym is not None and sym.kind == "function":
            target = self._label_for(sym)
            if self.long_calls:
                # ``CALL`` encodes a signed 14-bit PC-relative displacement.
                # Materialise the return address and use the assembler's
                # documented ``BL`` long-jump pseudo so the call is independent
                # of the final linked distance. No ISA surface is added: ``BL``
                # expands to LI r15/JMP r15 and ``LA`` to LI.
                return_label = self._new_label("longcall")
                self._emit(f"LA ra, {return_label}")
                self._emit(f"BL {target}")
                self._emit_label(return_label)
            else:
                self._emit(f"CALL {target}")
        else:
            callee = self._gen_rvalue(expr.callee)
            return_label = self._new_label("funcret")
            self._emit(f"LA ra, {return_label}")
            self._load_slot(callee.slot, "t0")
            self._emit("JMP t0")
            self._emit_label(return_label)

        if result_slot is None:
            # ``void`` has no value; the scratch slot keeps the expression-result
            # convention uniform without allocating a zero-sized object.
            return ExprResult(self._temp(self.ctx.int_t, expr), self.ctx.void_t,
                              False)

        if is_aggregate:
            assert result_obj is not None
            self._slot_address(result_obj, "t0")
            self._store_slot(result_slot, "t0")
            if not hidden and _is_record(return_type):
                size = _type_size(return_type, expr)
                offset = 0
                reg = 0
                while offset < size and reg < len(ARG_REGISTERS):
                    chunk = min(WORD_BYTES, size - offset)
                    self._store_chunk(result_slot, offset, chunk,
                                      ARG_REGISTERS[reg], expr)
                    offset += WORD_BYTES
                    reg += 1
            return ExprResult(result_slot, return_type, True)

        self._store_slot(result_slot, "a0")
        return ExprResult(result_slot, return_type, False)

    # -- variadic argument access -------------------------------------------

    def _void_result(self, expr: ast.Expression) -> ExprResult:
        return ExprResult(self._temp(self.ctx.int_t, expr), self.ctx.void_t,
                          False)

    def _fixed_argument_chunks(self, expr: ast.Expression) -> int:
        """Number of eight-byte chunks consumed by fixed parameters."""
        ft = self.function_type
        if ft is None:
            raise _unsupported(expr, "va_start outside a function definition")
        total = 1 if self.hidden_return_slot is not None else 0
        for param in ft.params:
            total += self._chunk_count(param.type, expr)
        return total

    def _gen_va_builtin(self, name: str,
                        expr: ast.CallExpression) -> ExprResult:
        if name == "__builtin_va_start":
            if self.va_reg_save is None or self.va_base is None:
                raise _unsupported(
                    expr, "va_start in a function that is not variadic")
            if not expr.arguments:
                raise _unsupported(expr, "va_start without a va_list argument")
            ap = self._gen_rvalue(expr.arguments[0])
            fixed = self._fixed_argument_chunks(expr)
            gp_offset = min(fixed, len(ARG_REGISTERS)) * WORD_BYTES
            stack_chunks = max(0, fixed - len(ARG_REGISTERS))
            self._load_slot(ap.slot, "t0")
            self._emit_li("t1", gp_offset)
            self._emit("STW t1, [t0 + 0]")
            self._emit("MOVI t1, 0")
            self._emit("STW t1, [t0 + 4]")
            self._load_slot(self.va_base, "t1")
            if stack_chunks:
                self._emit_addi("t1", "t1", stack_chunks * WORD_BYTES)
            self._emit("STD t1, [t0 + 8]")
            self._emit_addi("t1", "sp", self.va_reg_save.offset)
            self._emit("STD t1, [t0 + 16]")
            return self._void_result(expr)

        if name == "__builtin_va_end":
            if expr.arguments:
                self._gen_rvalue(expr.arguments[0])
            return self._void_result(expr)

        # __builtin_va_copy(destination, source)
        if len(expr.arguments) != 2:
            raise _unsupported(expr, "va_copy without two va_list arguments")
        dest = self._gen_rvalue(expr.arguments[0])
        source = self._gen_rvalue(expr.arguments[1])
        self._load_slot(dest.slot, "t0")
        self._load_slot(source.slot, "t1")
        for offset in (0, 8, 16):
            self._emit(f"LDD t2, [t1 + {offset}]")
            self._emit(f"STD t2, [t0 + {offset}]")
        return self._void_result(expr)

    def _gen_va_arg(self, expr: ast.VaArgExpression) -> ExprResult:
        if self.frame is None:
            raise _unsupported(expr, "va_arg outside a function definition")
        target = self._type_of_name(expr.type_name)
        target0 = _strip(target)
        if isinstance(target0, (VoidType, FunctionType, ArrayType)):
            raise _unsupported(expr, f"va_arg of type '{target.describe()}'")
        chunks = self._chunk_count(target0, expr)
        size = _type_size(target0, expr)

        ap = self._gen_rvalue(expr.ap)
        obj = self._temp(target0, expr)
        obj_ptr = self._temp(PointerType(target0), expr)
        self._slot_address(obj, "a1")
        self._store_slot(obj_ptr, "a1")
        self._load_slot(ap.slot, "t0")
        self._emit("LDWU t1, [t0 + 0]")       # gp_offset
        self._emit("LDD t3, [t0 + 8]")        # overflow_arg_area

        for chunk_index in range(chunks):
            offset = chunk_index * WORD_BYTES
            use_reg = self._new_label("va_reg")
            done = self._new_label("va_done")
            self._emit_li("t2", len(ARG_REGISTERS) * WORD_BYTES)
            self._emit_branch("BLTU", "t1, t2", use_reg)
            # No argument registers left: read and advance overflow area.
            self._emit("LDD a0, [t3]")
            self._emit("ADDI t3, t3, 8")
            self._emit_jump(done)
            self._emit_label(use_reg)
            self._emit("LDD t2, [t0 + 16]")    # reg_save_area
            self._emit("ADD t2, t2, t1")
            self._emit("LDD a0, [t2]")
            self._emit("ADDI t1, t1, 8")
            self._emit_label(done)
            width = min(WORD_BYTES, size - offset)
            self._store_chunk(obj_ptr, offset, width, "a0", expr)

        self._emit("STW t1, [t0 + 0]")
        self._emit("STD t3, [t0 + 8]")

        if _is_aggregate(target0):
            return ExprResult(obj_ptr, target0, True)
        value = self._temp(target0, expr)
        self._load_from_slot_typed(obj, "t0", target0, expr)
        self._store_slot(value, "t0")
        return ExprResult(value, target0, False)

    # -- conditions ----------------------------------------------------------

    def _gen_cond(self, expr: ast.Expression, true_label: str,
                  false_label: str) -> None:
        if isinstance(expr, ast.UnaryExpression) and expr.op == "!" and not expr.postfix:
            self._gen_cond(expr.operand, false_label, true_label)
            return
        if isinstance(expr, ast.BinaryExpression):
            op = expr.op
            if op == "&&":
                middle = self._new_label("and_rhs")
                self._gen_cond(expr.left, middle, false_label)
                self._emit_label(middle)
                self._gen_cond(expr.right, true_label, false_label)
                return
            if op == "||":
                middle = self._new_label("or_rhs")
                self._gen_cond(expr.left, true_label, middle)
                self._emit_label(middle)
                self._gen_cond(expr.right, true_label, false_label)
                return
            if op in ("==", "!=", "<", "<=", ">", ">="):
                left = self._gen_rvalue(expr.left)
                right = self._gen_rvalue(expr.right)
                self._emit_branch_compare(op, left.slot, right.slot, true_label,
                                          self._comparison_unsigned(expr), expr)
                self._emit_jump(false_label)
                return
        value = self._gen_rvalue(expr)
        if not (types.is_scalar_type(value.type) or _is_pointer(value.type)):
            raise _unsupported(expr, "non-scalar condition")
        self._load_slot(value.slot, "t0")
        self._emit_branch("BNE", "t0, r0", true_label)
        self._emit_jump(false_label)

    # -- function bodies -----------------------------------------------------

    def _emit_function(self, fn: ast.FunctionDefinition) -> None:
        sym = fn.symbol
        if sym is None:
            raise _unsupported(fn, "function definition without semantic symbol")
        ftype = _strip(fn.resolved_type)
        if not isinstance(ftype, FunctionType):
            raise _unsupported(fn, "function definition without function type")
        if fn.old_style_declarations:
            raise _unsupported(fn, "K&R old-style function definitions")
        if isinstance(ftype.return_type, FloatingType):
            raise _unsupported(fn, "floating-point return types")
        for param in ftype.params:
            if isinstance(_strip(param.type), FloatingType):
                raise _unsupported(fn, "floating-point parameters")

        self.function = fn
        self.function_type = ftype
        self.function_counter += 1
        self.unique = self.function_counter * 100000
        self.frame = Frame()
        self.labels = {}
        self.break_stack = []
        self.continue_stack = []
        self.switch_stack = []
        self.epilogue_label = self._new_label("return_epilogue")
        self.hidden_return_slot = None
        self.va_reg_save = None
        self.va_base = None
        self._collect_goto_labels(fn.body)

        return_type = _strip(ftype.return_type)
        hidden = (_is_aggregate(return_type) and
                  _type_size(return_type, fn) > AGGREGATE_REGISTER_RETURN_BYTES)
        if hidden:
            self.hidden_return_slot = self.frame.temp(PointerType(return_type), fn.location)

        if ftype.variadic:
            # ABI section 7: a callee using va_start spills a0-a5 into a
            # 48-byte register save area.  The base slot records the incoming
            # stack pointer once the final frame size is known.
            self.va_reg_save = self.frame.allocate(
                ArrayType(self.ctx.ulong_t, len(ARG_REGISTERS)),
                fn.location, name="__nova_va_reg_save")
            self.va_base = self.frame.allocate(
                PointerType(self.ctx.void_t), fn.location,
                name="__nova_va_base")

        params = self._allocate_parameters(ftype, fn)
        body: List[str] = []
        saved_out = self.out
        self.out = body
        self._emit_statement(fn.body)
        self._emit_label(self.epilogue_label)
        self._emit(f"LDD ra, [sp + {RA_SAVE_OFFSET}]")
        frame_size = self.frame.size()
        self._emit_addi("sp", "sp", frame_size)
        self._emit("RET")
        self.out = saved_out

        label = self._label_for(sym)
        prologue: List[str] = []
        if _fits_simm(-frame_size):
            prologue.append(f"ADDI sp, sp, {-frame_size}")
        else:
            prologue.append(f"LI t3, {frame_size}")
            prologue.append("SUB sp, sp, t3")
        prologue.append(f"STD ra, [sp + {RA_SAVE_OFFSET}]")
        if hidden and self.hidden_return_slot is not None:
            prologue.append(f"STD a0, [sp + {self.hidden_return_slot.offset}]")
        if (ftype.variadic and self.va_reg_save is not None
                and self.va_base is not None):
            for index, register in enumerate(ARG_REGISTERS):
                prologue.append(
                    f"STD {register}, [sp + "
                    f"{self.va_reg_save.offset + index * WORD_BYTES}]"
                )
            if _fits_simm(frame_size):
                prologue.append(f"ADDI t0, sp, {frame_size}")
            else:
                prologue.append(f"LI t0, {frame_size}")
                prologue.append("ADD t0, sp, t0")
            prologue.append(f"STD t0, [sp + {self.va_base.offset}]")
        prologue.extend(
            self._load_incoming_parameters(ftype, params, frame_size, hidden, fn)
        )

        function_lines = prologue + body
        self._emit(f".type {label}, function")
        self._emit_label(label)
        for line in function_lines:
            self._emit(line)
        size = sum(_instruction_length(line) * 4 for line in function_lines)
        self._emit(f".size {label}, {size}")

        self.function = None
        self.function_type = None
        self.frame = None
        self.hidden_return_slot = None
        self.va_reg_save = None
        self.va_base = None
        self.epilogue_label = None

    def _allocate_parameters(self, ft: FunctionType,
                             fn: ast.FunctionDefinition
                             ) -> List[Tuple[ParameterType, Slot]]:
        assert self.frame is not None
        scope = getattr(fn.symbol, "params_scope", None)
        params: List[Tuple[ParameterType, Slot]] = []
        for param in ft.params:
            slot = self.frame.allocate(param.type, fn.location, name=param.name or None)
            if param.name and scope is not None:
                sym = scope.lookup_ordinary_local(param.name)
                if sym is not None:
                    self.frame.symbols[id(sym)] = slot
            params.append((param, slot))
        return params

    def _load_incoming_parameters(self, ft: FunctionType,
                                  params: List[Tuple[ParameterType, Slot]],
                                  frame_size: int, hidden: bool,
                                  node: object) -> List[str]:
        lines: List[str] = []
        saved_out = self.out
        self.out = lines
        reg_index = 1 if hidden else 0
        for param, slot in params:
            size = _type_size(param.type, node)
            chunk_offset = 0
            for _ in range(self._chunk_count(param.type, node)):
                if reg_index < len(ARG_REGISTERS):
                    value = ARG_REGISTERS[reg_index]
                else:
                    stack_index = reg_index - len(ARG_REGISTERS)
                    if stack_index >= MAX_OUTGOING_CHUNKS:
                        raise _unsupported(node, "function with too many stack parameters")
                    self._emit_addi("t1", "sp", frame_size + stack_index * WORD_BYTES)
                    self._emit("LDD t0, [t1]")
                    value = "t0"
                if value != "t0":
                    self._emit(f"MOV t0, {value}")
                width = min(WORD_BYTES, size - chunk_offset)
                self._slot_address(slot, "t2")
                self._store_chunk_at("t2", chunk_offset, width, "t0", node)
                chunk_offset += WORD_BYTES
                reg_index += 1
        self.out = saved_out
        return lines

    def _collect_goto_labels(self, stmt: ast.Statement) -> None:
        if isinstance(stmt, ast.LabelStatement):
            self.labels[stmt.label] = self._new_label("goto")
            self._collect_goto_labels(stmt.body)
        elif isinstance(stmt, ast.CompoundStatement):
            for item in stmt.items:
                if isinstance(item, ast.Statement):
                    self._collect_goto_labels(item)
        elif isinstance(stmt, ast.IfStatement):
            self._collect_goto_labels(stmt.then_branch)
            if stmt.else_branch is not None:
                self._collect_goto_labels(stmt.else_branch)
        elif isinstance(stmt, (ast.WhileStatement, ast.DoWhileStatement,
                               ast.ForStatement, ast.SwitchStatement)):
            self._collect_goto_labels(stmt.body)
        elif isinstance(stmt, (ast.CaseStatement, ast.DefaultStatement)) and stmt.body is not None:
            self._collect_goto_labels(stmt.body)

    # -- statements ----------------------------------------------------------

    def _emit_statement(self, stmt: ast.Statement) -> None:
        if isinstance(stmt, ast.CompoundStatement):
            for item in stmt.items:
                if isinstance(item, ast.Declaration):
                    self._emit_declaration(item)
                else:
                    self._emit_statement(item)
            return

        if isinstance(stmt, ast.DeclarationStatement):
            self._emit_declaration(stmt.declaration)
            return

        if isinstance(stmt, ast.ExpressionStatement):
            if stmt.expression is not None:
                self._gen_expr(stmt.expression)
            return

        if isinstance(stmt, ast.IfStatement):
            true_label = self._new_label("if_true")
            false_label = self._new_label("if_false")
            end_label = self._new_label("if_end")
            self._gen_cond(stmt.condition, true_label, false_label)
            self._emit_label(true_label)
            self._emit_statement(stmt.then_branch)
            self._emit_jump(end_label)
            self._emit_label(false_label)
            if stmt.else_branch is not None:
                self._emit_statement(stmt.else_branch)
            self._emit_label(end_label)
            return

        if isinstance(stmt, ast.WhileStatement):
            top = self._new_label("while_top")
            body = self._new_label("while_body")
            end = self._new_label("while_end")
            self.break_stack.append(end)
            self.continue_stack.append(top)
            self._emit_jump(top)
            self._emit_label(top)
            self._gen_cond(stmt.condition, body, end)
            self._emit_label(body)
            self._emit_statement(stmt.body)
            self._emit_jump(top)
            self._emit_label(end)
            self.break_stack.pop()
            self.continue_stack.pop()
            return

        if isinstance(stmt, ast.DoWhileStatement):
            top = self._new_label("do_top")
            cond = self._new_label("do_cond")
            end = self._new_label("do_end")
            self.break_stack.append(end)
            self.continue_stack.append(cond)
            self._emit_label(top)
            self._emit_statement(stmt.body)
            self._emit_label(cond)
            self._gen_cond(stmt.condition, top, end)
            self._emit_label(end)
            self.break_stack.pop()
            self.continue_stack.pop()
            return

        if isinstance(stmt, ast.ForStatement):
            top = self._new_label("for_top")
            cond = self._new_label("for_cond")
            step = self._new_label("for_step")
            end = self._new_label("for_end")
            if isinstance(stmt.init, ast.Declaration):
                self._emit_declaration(stmt.init)
            elif stmt.init is not None:
                self._gen_expr(stmt.init)
            self.break_stack.append(end)
            self.continue_stack.append(step)
            self._emit_jump(cond)
            self._emit_label(top)
            self._emit_statement(stmt.body)
            self._emit_label(step)
            if stmt.step is not None:
                self._gen_expr(stmt.step)
            self._emit_label(cond)
            if stmt.condition is not None:
                self._gen_cond(stmt.condition, top, end)
            else:
                self._emit_jump(top)
            self._emit_label(end)
            self.break_stack.pop()
            self.continue_stack.pop()
            return

        if isinstance(stmt, ast.BreakStatement):
            if not self.break_stack:
                raise _unsupported(stmt, "'break' outside loop or switch")
            self._emit_jump(self.break_stack[-1])
            return

        if isinstance(stmt, ast.ContinueStatement):
            if not self.continue_stack:
                raise _unsupported(stmt, "'continue' outside loop")
            self._emit_jump(self.continue_stack[-1])
            return

        if isinstance(stmt, ast.ReturnStatement):
            self._emit_return(stmt)
            return

        if isinstance(stmt, ast.SwitchStatement):
            self._emit_switch(stmt)
            return

        if isinstance(stmt, ast.CaseStatement):
            label = self.switch_stack[-1]["case_labels"].get(id(stmt)) if self.switch_stack else None
            if label is None:
                raise _unsupported(stmt, "case label outside switch")
            self._emit_label(label)
            if stmt.body is not None:
                self._emit_statement(stmt.body)
            return

        if isinstance(stmt, ast.DefaultStatement):
            if not self.switch_stack:
                raise _unsupported(stmt, "default label outside switch")
            label = self.switch_stack[-1]["default_label"]
            self.switch_stack[-1]["default_emitted"] = True
            self._emit_label(label)
            if stmt.body is not None:
                self._emit_statement(stmt.body)
            return

        if isinstance(stmt, ast.GotoStatement):
            label = self.labels.get(stmt.label)
            if label is None:
                raise _unsupported(stmt, f"undeclared label '{stmt.label}'")
            self._emit_jump(label)
            return

        if isinstance(stmt, ast.LabelStatement):
            label = self.labels.get(stmt.label)
            if label is None:
                raise _unsupported(stmt, f"undeclared label '{stmt.label}'")
            self._emit_label(label)
            self._emit_statement(stmt.body)
            return

        raise _unsupported(stmt, f"statement {type(stmt).__name__}")

    def _emit_return(self, stmt: ast.ReturnStatement) -> None:
        assert self.function_type is not None
        assert self.epilogue_label is not None
        return_type = _strip(self.function_type.return_type)
        if stmt.value is not None:
            value = self._gen_rvalue(stmt.value)
            if isinstance(return_type, VoidType):
                raise _unsupported(stmt, "return value from void function")
            if _is_record(return_type):
                if not (value.address and _is_aggregate(value.type)):
                    raise _unsupported(stmt, "aggregate return without an address")
                size = _type_size(return_type, stmt)
                if size > AGGREGATE_REGISTER_RETURN_BYTES:
                    assert self.hidden_return_slot is not None
                    self._copy_object(self.hidden_return_slot, value.slot,
                                      size, stmt)
                    self._load_slot(self.hidden_return_slot, "a0")
                else:
                    offset = 0
                    reg = 0
                    while offset < size and reg < 2:
                        chunk = min(WORD_BYTES, size - offset)
                        self._load_chunk(value.slot, offset, chunk,
                                         ARG_REGISTERS[reg], stmt)
                        offset += WORD_BYTES
                        reg += 1
            else:
                value = self._coerce_value(value, return_type, stmt)
                self._load_slot(value.slot, "a0")
        self._emit_jump(self.epilogue_label)

    def _collect_switch_labels(self, stmt: ast.Statement,
                               labels: Dict[int, Tuple[ast.Expression, str]]) -> None:
        if isinstance(stmt, ast.SwitchStatement):
            return  # nested switch owns its own labels
        if isinstance(stmt, ast.CaseStatement):
            labels[id(stmt)] = (stmt.value, self._new_label("case"))
            if stmt.body is not None:
                self._collect_switch_labels(stmt.body, labels)
        elif isinstance(stmt, ast.CompoundStatement):
            for item in stmt.items:
                if isinstance(item, ast.Statement):
                    self._collect_switch_labels(item, labels)
        elif isinstance(stmt, ast.IfStatement):
            self._collect_switch_labels(stmt.then_branch, labels)
            if stmt.else_branch is not None:
                self._collect_switch_labels(stmt.else_branch, labels)
        elif isinstance(stmt, (ast.WhileStatement, ast.DoWhileStatement, ast.ForStatement)):
            self._collect_switch_labels(stmt.body, labels)
        elif isinstance(stmt, ast.LabelStatement):
            self._collect_switch_labels(stmt.body, labels)

    def _emit_switch(self, stmt: ast.SwitchStatement) -> None:
        condition = self._gen_rvalue(stmt.condition)
        case_labels: Dict[int, Tuple[ast.Expression, str]] = {}
        self._collect_switch_labels(stmt.body, case_labels)
        default_label = self._new_label("switch_default")
        end_label = self._new_label("switch_end")

        for value_expr, label in case_labels.values():
            value = _const_int(value_expr)
            if value is None:
                raise _unsupported(value_expr, "non-constant case label")
            self._load_slot(condition.slot, "t0")
            self._emit_li("t1", value)
            self._emit_branch("BEQ", "t0, t1", label)
        self._emit_jump(default_label)

        self.break_stack.append(end_label)
        switch_info = {
            "case_labels": {key: label for key, (_, label) in case_labels.items()},
            "default_label": default_label,
            "default_emitted": False,
        }
        self.switch_stack.append(switch_info)
        self._emit_statement(stmt.body)
        if not switch_info["default_emitted"]:
            self._emit_label(default_label)
        self._emit_label(end_label)
        self.switch_stack.pop()
        self.break_stack.pop()

    # -- declarations and initializers --------------------------------------

    def _emit_declaration(self, decl: ast.Declaration) -> None:
        for init_decl in decl.init_declarators:
            sym = getattr(init_decl, "symbol", None)
            if sym is None or sym.kind in ("typedef", "function"):
                continue
            t = init_decl.resolved_type
            if isinstance(_strip(t), FloatingType):
                raise _unsupported(init_decl, "floating-point objects")
            if sym.storage == "extern" and init_decl.initializer is None:
                continue
            if sym.storage == "static":
                self._emit_local_static(init_decl, sym, t)
                continue
            assert self.frame is not None
            slot = self.frame.allocate(t, init_decl.location, name=sym.name)
            self.frame.symbols[id(sym)] = slot
            if init_decl.initializer is not None:
                self._zero_slot(slot, _type_size(t, init_decl), init_decl)
                self._emit_initializer_at(slot, t, init_decl.initializer, 0, init_decl)

    def _emit_local_static(self, init_decl: ast.InitDeclarator, sym: object,
                           t: Type) -> None:
        if id(sym) in self.symbol_labels:
            return
        label = self._new_label("static")
        self.symbol_labels[id(sym)] = label
        self.data_objects[label] = _DataObject(
            label=label,
            type=t,
            initializer=init_decl.initializer,
            location=init_decl.location,
        )

    def _emit_initializer_at(self, addr: Slot, target: Type,
                             initializer: ast.Initializer, offset: int,
                             node: object) -> None:
        t0 = _strip(target)
        if isinstance(initializer, ast.DesignatedInitializer):
            self._emit_initializer_at(addr, t0, initializer.initializer,
                                      offset, initializer)
            return

        if _is_record(t0):
            if not isinstance(initializer, ast.InitializerList):
                value = self._gen_rvalue(initializer)
                if not (value.address and _is_aggregate(value.type)):
                    raise _unsupported(node, f"non-list initializer for '{t0.describe()}'")
                dest = self._temp(PointerType(t0), node)
                self._slot_address(addr, "t0")
                if offset:
                    self._emit_addi("t0", "t0", offset)
                self._store_slot(dest, "t0")
                self._copy_object(dest, value.slot, _type_size(t0, node), node)
                return
            fields = t0.fields
            for item in initializer.items:
                index = getattr(item, "field_index", None)
                if index is None or not (0 <= int(index) < len(fields)):
                    raise _unsupported(item, "record initializer without field index")
                field = fields[int(index)]
                if field.bit_width is not None:
                    raise _unsupported(item, "bit-field initializer")
                child = item.initializer if isinstance(item, ast.DesignatedInitializer) else item
                self._emit_initializer_at(addr, field.type, child,
                                          offset + field.offset, item)
            return

        if isinstance(t0, ArrayType):
            element_size = _type_size(t0.element, node)
            if isinstance(initializer, ast.StringLiteral) and _is_integer(t0.element):
                raw = initializer.value.encode("utf-8") + b"\x00"
                limit = t0.length if t0.length is not None else len(raw)
                for index, byte in enumerate(raw[:limit]):
                    self._emit_li("t0", byte)
                    self._slot_address(addr, "t1")
                    self._emit_addi("t1", "t1", offset + index)
                    self._emit("STB t0, [t1]")
                return
            if not isinstance(initializer, ast.InitializerList):
                raise _unsupported(node, f"non-list initializer for array '{t0.describe()}'")
            for item in initializer.items:
                index = getattr(item, "array_index", None)
                if index is None:
                    raise _unsupported(item, "array initializer without element index")
                child = item.initializer if isinstance(item, ast.DesignatedInitializer) else item
                self._emit_initializer_at(addr, t0.element, child,
                                          offset + int(index) * element_size, item)
            return

        if isinstance(initializer, ast.InitializerList):
            if not initializer.items:
                return
            first = initializer.items[0]
            child = first.initializer if isinstance(first, ast.DesignatedInitializer) else first
            self._emit_initializer_at(addr, t0, child, offset, first)
            return

        value = self._gen_rvalue(initializer)
        value = self._coerce_value(value, t0, node)
        self._load_slot(value.slot, "t0")
        self._slot_address(addr, "t1")
        self._emit_addi("t1", "t1", offset)
        self._emit(f"{self._store_op(t0, node)} t0, [t1]")

    # -- data sections -------------------------------------------------------

    def _emit_data_sections(self) -> None:
        # Data initializers may intern new string literals (for example a
        # global ``char *p = "text"``), so materialise data objects first and
        # emit the complete .rodata string pool afterwards.
        for label, obj in self.data_objects.items():
            if label in self.emitted_data:
                continue
            self.emitted_data.add(label)
            self._emit_data_object(obj)

        if self.strings:
            self.out = self.rodata
            self._emit(".section .rodata")
            for label, value in self.strings:
                self._emit(f".align {FRAME_ALIGNMENT}")
                self._emit_label(label)
                self._emit(".byte " + _as_asm_bytes(value))
        self.out = self.text

    def _emit_data_object(self, obj: _DataObject) -> None:
        t = _strip(obj.type)
        size = _type_size(t, obj)
        data = bytearray(size)
        relocations: List[Tuple[int, str]] = []
        if obj.initializer is not None:
            self._fill_data(t, obj.initializer, data, 0, obj, relocations)

        section = self.data if relocations or any(data) else self.bss
        self.out = section
        self._emit(".section " + (".data" if section is self.data else ".bss"))
        self._emit(f".align {min(FRAME_ALIGNMENT, _type_align(t))}")
        self._emit_label(obj.label)

        if section is self.bss:
            self._emit(f".space {size}")
        else:
            cursor = 0
            for offset, symbol in sorted(relocations):
                if offset % WORD_BYTES:
                    raise _unsupported(obj, "misaligned pointer initializer")
                if offset < cursor or offset + WORD_BYTES > size:
                    raise _unsupported(obj, "overlapping pointer initializer")
                if offset > cursor:
                    self._emit_bytes(data[cursor:offset])
                self._emit(f".quad {symbol}")
                cursor = offset + WORD_BYTES
            if cursor < size:
                self._emit_bytes(data[cursor:size])
        if obj.global_name is not None:
            self._emit(f".type {obj.label}, object")
            self._emit(f".size {obj.label}, {size}")

    def _emit_bytes(self, chunk: bytes) -> None:
        for start in range(0, len(chunk), 16):
            part = chunk[start:start + 16]
            self._emit(".byte " + ", ".join(f"0x{byte:02x}" for byte in part))

    def _fill_data(self, target: Type, initializer: ast.Initializer,
                   data: bytearray, offset: int, node: object,
                   relocations: List[Tuple[int, str]]) -> None:
        t = _strip(target)
        if isinstance(initializer, ast.DesignatedInitializer):
            self._fill_data(t, initializer.initializer, data, offset, node, relocations)
            return

        if _is_record(t):
            fields = t.fields
            items = (initializer.items if isinstance(initializer, ast.InitializerList)
                     else [initializer])
            for item in items:
                index = getattr(item, "field_index", None)
                if index is None or not (0 <= int(index) < len(fields)):
                    raise _unsupported(item, "record initializer without field index")
                field = fields[int(index)]
                if field.bit_width is not None:
                    raise _unsupported(item, "bit-field initializer")
                child = item.initializer if isinstance(item, ast.DesignatedInitializer) else item
                self._fill_data(field.type, child, data, offset + field.offset,
                                item, relocations)
            return

        if isinstance(t, ArrayType):
            element_size = _type_size(t.element, node)
            if isinstance(initializer, ast.StringLiteral) and _is_integer(t.element):
                raw = initializer.value.encode("utf-8") + b"\x00"
                limit = t.length if t.length is not None else len(raw)
                data[offset:offset + min(len(raw), limit)] = raw[:limit]
                return
            if not isinstance(initializer, ast.InitializerList):
                raise _unsupported(node, "non-list array initializer")
            for item in initializer.items:
                index = getattr(item, "array_index", None)
                if index is None:
                    raise _unsupported(item, "array initializer without element index")
                child = item.initializer if isinstance(item, ast.DesignatedInitializer) else item
                self._fill_data(t.element, child, data,
                                offset + int(index) * element_size, item, relocations)
            return

        if isinstance(initializer, ast.InitializerList):
            if initializer.items:
                first = initializer.items[0]
                child = first.initializer if isinstance(first, ast.DesignatedInitializer) else first
                self._fill_data(t, child, data, offset, first, relocations)
            return

        if isinstance(t, FloatingType):
            raise _unsupported(initializer, "floating-point data initializer")
        size = _type_size(t, initializer)
        if isinstance(initializer, ast.StringLiteral) and isinstance(t, PointerType):
            if size != WORD_BYTES:
                raise _unsupported(initializer, "string initializer for a non-pointer object")
            relocations.append((offset, self._string_label(initializer)))
            return
        symbol = self._constant_symbol(initializer)
        if symbol is not None:
            if size != WORD_BYTES:
                raise _unsupported(initializer, "symbol initializer for a non-pointer object")
            relocations.append((offset, symbol))
            return
        value = _const_int(initializer)
        if value is None:
            raise _unsupported(initializer, "non-constant data initializer")
        encoded = (value & ((1 << (size * 8)) - 1)).to_bytes(size, "little")
        data[offset:offset + size] = encoded

    def _constant_symbol(self, expr: ast.Expression) -> Optional[str]:
        if (isinstance(expr, ast.UnaryExpression) and expr.op == "&" and
                isinstance(expr.operand, ast.IdentifierExpression)):
            sym = getattr(expr.operand, "symbol", None)
            return self._label_for(sym) if sym is not None else None
        if isinstance(expr, ast.IdentifierExpression):
            sym = getattr(expr, "symbol", None)
            if sym is not None and sym.kind == "function":
                return self._label_for(sym)
        return None


def generate(tree: ast.TranslationUnit, target: str = "nova64",
             long_calls: bool = False, long_branches: bool = False) -> str:
    """Lower a semantically analysed translation unit to Nova64 assembly.

    ``long_calls`` replaces direct ``CALL`` instructions with a return-address
    ``LA`` plus the assembler's documented ``BL`` pseudo-instruction. The
    resulting sequence is position-independent of the final linked layout and
    removes the signed 14-bit call displacement limit at the cost of code size.

    ``long_branches`` replaces direct intra-function branches with an inverted
    short branch around the same documented ``BL`` pseudo-instruction, so no
    single function is limited by the signed 14-bit ``PCREL_DISP14`` range.
    Both options are opt-in: default code stays compact and range-limited.
    """
    return CodeGenerator(tree, target, long_calls=long_calls,
                         long_branches=long_branches).generate()
