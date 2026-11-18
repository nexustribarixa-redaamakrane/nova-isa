"""Reference emulator for the Nova ISA.

The emulator models the architectural contract only: general-purpose
registers, the program counter, control/status registers, privilege level,
exception state, sparse byte-addressable memory, the virtual-memory
translator, interrupts, and the capability-based object table. It does not
model caches, pipelines, branch predictors, or any other microarchitectural
structure.

Execution follows the architectural sequence::

    fetch -> decode -> validate -> execute -> update state -> raise events

Fetch, decode, and validation are shared with the assembler and the
disassembler through :mod:`nova.isa`; per-instruction behaviour lives in the
:data:`HANDLERS` table, which the module checks against the instruction table
at import time so that no instruction can be silently unimplemented.

Control/status registers are the single source of truth for ``psr``, ``epc``,
``ecause``, ``ecode``, ``priv``, and ``vbar``. Host-side attribute access
bypasses privilege and WARL checks for test setup; only instruction execution
enforces them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import objfmt
from .isa import (
    ARITHMETIC_DETAIL_DIVIDE,
    ARITHMETIC_DETAIL_REMAINDER,
    COREID,
    CSR_BY_NAME,
    CSR_COUNT,
    CSR_TABLE,
    ECAUSE,
    ECODE,
    EPC,
    EXCEPTION_CODES,
    EXCEPTION_STACK_DEPTH,
    FLAG_CARRY,
    FLAG_NEGATIVE,
    FLAG_OVERFLOW,
    FLAG_ZERO,
    GPR_COUNT,
    HSTATUS,
    IE,
    INT_EXTERNAL,
    INT_SOFTWARE,
    INT_TIMER,
    INTERRUPT_SOURCES,
    INSTRUCTION_ALIGNMENT,
    INSTRUCTION_BYTES,
    INSTRUCTION_TABLE,
    INTERRUPT_IE_MASK,
    IP,
    IVEC,
    MMU_DETAIL_MISALIGNED_PTE,
    MMU_DETAIL_MISSING,
    MMU_DETAIL_NONCANONICAL,
    MMU_DETAIL_PERMISSION,
    MMU_DETAIL_RESERVED_PTE,
    OBJECT_DETAIL_INVALID,
    OBJECT_DETAIL_NULL,
    OBJECT_DETAIL_PERMISSION,
    OBJECT_FLAG_EXTERNAL,
    OBJECT_FORMAT_VERSION,
    OBJECT_PERM_MASK,
    OBJECT_GENERATION_MASK,
    OBJECT_INDEX_MASK,
    OBJECT_PERM_DROP,
    OBJECT_PERM_EXEC,
    OBJECT_PERM_GRANT,
    OBJECT_PERM_META,
    OBJECT_PERM_READ,
    OBJECT_PERM_WRITE,
    ObjectFormatError,
    make_object_reference,
    split_object_reference,
    PAGE_BITS,
    PAGE_SIZE,
    PHYSICAL_ADDRESS_BITS,
    PTE_PPN_MASK,
    PT_ENTRIES,
    PT_INDEX_BITS,
    PT_LEVELS,
    PRIV,
    PRIVILEGE_LEVELS,
    PRIV_MACHINE,
    PRIV_USER,
    PRIV_RESERVED,
    PRIVILEGE_NAMES,
    PSR,
    PTBR,
    PTE_BITS,
    REGISTERS,
    RESET_ENTRY,
    S2PTBR,
    TIME,
    TIMECMP,
    TVAL,
    VBAR,
    VEXIT,
    VIRTUAL_ADDRESS_BITS,
    VPRIV,
    VPTBR,
    VPC,
    VM_EXIT_EXPLICIT,
    VM_EXIT_TRAP,
    WORD_BITS,
    WORD_MASK,
    DecodedInstruction,
    decode_instruction,
    sign_extend,
    to_signed64,
    to_u64,
)

__all__ = ["NovaMachine", "NovaCluster", "HANDLERS", "NovaTrap"]


class NovaTrap(Exception):
    """Internal control-flow exception used to unwind to exception entry."""

    __slots__ = ("code", "detail", "return_pc")

    def __init__(self, code: int, detail: int = 0,
                 return_pc: Optional[int] = None) -> None:
        self.code = code
        self.detail = detail
        self.return_pc = return_pc
        super().__init__(f"Nova trap {code} (detail {detail:#x})")


Handler = Callable[["NovaMachine", DecodedInstruction], Optional[int]]

#: Privilege levels ordered numerically. The reserved level 3 sorts above
#: machine so that a corrupted ``priv`` can never pass a privilege check.
_PRIVILEGE_RANKS: Mapping[int, int] = MappingProxyType(
    {
        PRIVILEGE_LEVELS["user"]: 0,
        PRIVILEGE_LEVELS["supervisor"]: 1,
        PRIVILEGE_LEVELS["machine"]: 2,
    }
)


# --------------------------------------------------------------------------
# Architectural helpers used by the instruction handlers
# --------------------------------------------------------------------------


def _data_address(machine: "NovaMachine", base: int, displacement: int,
                  size: int) -> int:
    """Compute and validate a data effective address."""
    address = base + displacement
    if address < 0 or address + size - 1 > WORD_MASK:
        machine._trap(EXCEPTION_CODES["MEMORY_FAULT"], to_u64(address))
    if address % size:
        machine._trap(EXCEPTION_CODES["ALIGNMENT_FAULT"], address)
    return address


def _load(machine: "NovaMachine", d: DecodedInstruction, size: int,
          extend: Callable[[int, int], int]) -> None:
    base = machine._read_reg(d.memory_base)
    address = _data_address(machine, base, d.memory_displacement, size)
    physical = machine._translate(address, size, "r")
    value = machine._read_mem(physical, size)
    machine._write_reg(d.register("rd"), extend(value, size * 8))


def _store(machine: "NovaMachine", d: DecodedInstruction, size: int) -> None:
    base = machine._read_reg(d.memory_base)
    address = _data_address(machine, base, d.memory_displacement, size)
    physical = machine._translate(address, size, "w")
    machine._write_mem(physical, machine._read_reg(d.register("rd")), size)


def _branch_handler(predicate: Callable[[int, int], bool]) -> Handler:
    """Build a conditional-branch handler.

    Branch conditions read registers directly; they never consume PSR flags,
    so a flag-setting instruction cannot change branch behaviour.
    """
    def handler(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
        lhs = machine._read_reg(d.register("reg_a"))
        rhs = machine._read_reg(d.register("reg_b"))
        if predicate(lhs, rhs):
            return machine._pc_target(machine.pc + d.target_displacement)
        return None

    handler.__name__ = f"branch_{getattr(predicate, '__doc__', None) or 'cond'}"
    return handler


def _quotient(lhs: int, rhs: int) -> int:
    """Signed quotient truncated toward zero."""
    quotient = abs(lhs) // abs(rhs)
    return -quotient if (lhs < 0) != (rhs < 0) else quotient


# --------------------------------------------------------------------------
# Instruction handlers
# --------------------------------------------------------------------------


def _op_nop(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return None


def _op_add(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) + machine._read_reg(d.register("rs2")),
    )
    return None


def _op_sub(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) - machine._read_reg(d.register("rs2")),
    )
    return None


def _op_mul(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) * machine._read_reg(d.register("rs2")),
    )
    return None


def _op_div(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    lhs = to_signed64(machine._read_reg(d.register("rs1")))
    rhs = to_signed64(machine._read_reg(d.register("rs2")))
    if rhs == 0:
        machine._trap(EXCEPTION_CODES["ARITHMETIC"], ARITHMETIC_DETAIL_DIVIDE)
    machine._write_reg(d.register("rd"), _quotient(lhs, rhs))
    return None


def _op_rem(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    lhs = to_signed64(machine._read_reg(d.register("rs1")))
    rhs = to_signed64(machine._read_reg(d.register("rs2")))
    if rhs == 0:
        machine._trap(EXCEPTION_CODES["ARITHMETIC"], ARITHMETIC_DETAIL_REMAINDER)
    machine._write_reg(
        d.register("rd"), lhs - _quotient(lhs, rhs) * rhs
    )
    return None


def _op_and(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) & machine._read_reg(d.register("rs2")),
    )
    return None


def _op_or(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) | machine._read_reg(d.register("rs2")),
    )
    return None


def _op_xor(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) ^ machine._read_reg(d.register("rs2")),
    )
    return None


def _op_shl(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    shift = machine._read_reg(d.register("rs2")) & 0x3F
    machine._write_reg(d.register("rd"), machine._read_reg(d.register("rs1")) << shift)
    return None


def _op_shr(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    shift = machine._read_reg(d.register("rs2")) & 0x3F
    machine._write_reg(d.register("rd"), machine._read_reg(d.register("rs1")) >> shift)
    return None


def _op_sar(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    shift = machine._read_reg(d.register("rs2")) & 0x3F
    machine._write_reg(
        d.register("rd"),
        to_signed64(machine._read_reg(d.register("rs1"))) >> shift,
    )
    return None


def _op_cmp(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._set_compare_flags(
        machine._read_reg(d.register("rs1")),
        machine._read_reg(d.register("rs2")),
    )
    return None


def _op_cmpi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._set_compare_flags(
        machine._read_reg(d.register("rs1")),
        to_u64(d.signed_immediate("simm")),
    )
    return None


def _op_mov(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(d.register("rd"), machine._read_reg(d.register("rs1")))
    return None


def _op_not(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(d.register("rd"), ~machine._read_reg(d.register("rs1")))
    return None


def _op_neg(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(d.register("rd"), -machine._read_reg(d.register("rs1")))
    return None


def _op_addi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) + d.signed_immediate("simm"),
    )
    return None


def _op_subi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) - d.signed_immediate("simm"),
    )
    return None


def _op_muli(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) * d.signed_immediate("simm"),
    )
    return None


def _op_andi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) & to_u64(d.signed_immediate("simm")),
    )
    return None


def _op_ori(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) | to_u64(d.signed_immediate("simm")),
    )
    return None


def _op_xori(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) ^ to_u64(d.signed_immediate("simm")),
    )
    return None


def _op_shli(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) << (d.signed_immediate("simm") & 0x3F),
    )
    return None


def _op_shri(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        machine._read_reg(d.register("rs1")) >> (d.signed_immediate("simm") & 0x3F),
    )
    return None


def _op_sari(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(
        d.register("rd"),
        to_signed64(machine._read_reg(d.register("rs1")))
        >> (d.signed_immediate("simm") & 0x3F),
    )
    return None


def _op_movi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(d.register("rd"), d.unsigned_immediate("uimm"))
    return None


def _op_ldb(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 1, sign_extend)
    return None


def _op_ldbu(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 1, lambda value, width: value)
    return None


def _op_ldh(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 2, sign_extend)
    return None


def _op_ldhu(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 2, lambda value, width: value)
    return None


def _op_ldw(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 4, sign_extend)
    return None


def _op_ldwu(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 4, lambda value, width: value)
    return None


def _op_ldd(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _load(machine, d, 8, lambda value, width: to_u64(value))
    return None


def _op_stb(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _store(machine, d, 1)
    return None


def _op_sth(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _store(machine, d, 2)
    return None


def _op_stw(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _store(machine, d, 4)
    return None


def _op_std(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    _store(machine, d, 8)
    return None


def _op_jmp(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return machine._pc_target(machine._read_reg(d.register("reg_a")))


def _op_call(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    target = machine._pc_target(machine.pc + d.target_displacement)
    machine._write_reg(31, machine.pc + INSTRUCTION_BYTES)
    return target


def _op_ret(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return machine._pc_target(machine._read_reg(31))


def _op_scall(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._trap(
        EXCEPTION_CODES["SYSTEM_CALL"],
        d.unsigned_immediate("uimm"),
        return_pc=machine.pc + INSTRUCTION_BYTES,
    )
    return None  # pragma: no cover - _trap always raises


def _op_trap(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._trap(
        EXCEPTION_CODES["SOFTWARE_TRAP"],
        d.unsigned_immediate("uimm"),
        return_pc=machine.pc + INSTRUCTION_BYTES,
    )
    return None  # pragma: no cover - _trap always raises


def _op_mret(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return _exception_return(machine)


def _op_fence(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return None


def _op_halt(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine.halted = True
    return machine.pc


def _op_wait(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    """WAIT retires when an interrupt is pending, otherwise it stops."""
    if machine._pending_interrupt() is None:
        machine.halted = True
        return machine.pc
    return None


def _op_obj_create(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    reference = machine._create_object(machine._read_reg(d.register("rs1")))
    machine._write_reg(d.register("rd"), reference)
    return None


def _op_obj_get(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    reference = machine._read_reg(d.register("rs1"))
    obj = machine._object_field(reference, OBJECT_PERM_READ)
    machine._write_reg(d.register("rd"), obj["size"])
    return None


def _op_obj_set(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    reference = machine._read_reg(d.register("rs1"))
    obj = machine._object_field(reference, OBJECT_PERM_WRITE)
    obj["size"] = to_u64(machine._read_reg(d.register("rd")))
    return None


def _op_obj_drop(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._drop_object(machine._read_reg(d.register("rd")))
    return None


def _op_csrr(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_reg(d.register("rd"), machine._read_csr(d.csr_index))
    return None


def _op_csrw(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine._write_csr(d.csr_index, machine._read_reg(d.register("rs1")))
    return None


# --------------------------------------------------------------------------
# Nova v1.0 handlers: rotates, unsigned division, atomics, MMU, interrupts,
# virtualization, and finalized object capabilities.
# --------------------------------------------------------------------------


def _op_rotl(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    value = machine._read_reg(d.register("rs1"))
    amount = machine._read_reg(d.register("rs2")) & (WORD_BITS - 1)
    rotated = ((value << amount) | (value >> (WORD_BITS - amount))) & WORD_MASK
    machine._write_reg(d.register("rd"), rotated)
    return None


def _op_rotr(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    value = machine._read_reg(d.register("rs1"))
    amount = machine._read_reg(d.register("rs2")) & (WORD_BITS - 1)
    rotated = ((value >> amount) | (value << (WORD_BITS - amount))) & WORD_MASK
    machine._write_reg(d.register("rd"), rotated)
    return None


def _rotl_immediate(machine: "NovaMachine", d: DecodedInstruction,
                    right: bool) -> Optional[int]:
    value = machine._read_reg(d.register("rs1"))
    amount = d.unsigned_immediate("simm") & (WORD_BITS - 1)
    if right:
        result = ((value >> amount) | (value << (WORD_BITS - amount))) & WORD_MASK
    else:
        result = ((value << amount) | (value >> (WORD_BITS - amount))) & WORD_MASK
    machine._write_reg(d.register("rd"), result)
    return None


def _op_rotli(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return _rotl_immediate(machine, d, False)


def _op_rotri(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return _rotl_immediate(machine, d, True)


def _op_udiv(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    lhs = machine._read_reg(d.register("rs1"))
    rhs = machine._read_reg(d.register("rs2"))
    if rhs == 0:
        machine._trap(EXCEPTION_CODES["ARITHMETIC"], ARITHMETIC_DETAIL_DIVIDE)
    machine._write_reg(d.register("rd"), lhs // rhs)
    return None


def _op_urem(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    lhs = machine._read_reg(d.register("rs1"))
    rhs = machine._read_reg(d.register("rs2"))
    if rhs == 0:
        machine._trap(EXCEPTION_CODES["ARITHMETIC"], ARITHMETIC_DETAIL_REMAINDER)
    machine._write_reg(d.register("rd"), lhs % rhs)
    return None


def _amo(machine: "NovaMachine", d: DecodedInstruction, kind: str) -> Optional[int]:
    address = _data_address(machine, machine._read_reg(d.register("rs1")), 0, 8)
    physical = machine._translate(address, 8, "w")
    old = machine._read_mem(physical, 8)
    operand = machine._read_reg(d.register("rs2"))
    expected = machine._read_reg(d.register("rd"))
    if kind == "add":
        new = to_u64(old + operand)
    elif kind == "swap":
        new = operand
    elif kind == "cas":
        new = operand if old == expected else old
    elif kind == "and":
        new = old & operand
    elif kind == "or":
        new = old | operand
    else:  # xor
        new = old ^ operand
    machine._write_mem(physical, new, 8)
    machine._write_reg(d.register("rd"), old)
    machine.last_amo_ordering = (d.fields["aq"], d.fields["rl"])
    return None


def _amo_handler(kind: str) -> Handler:
    def handler(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
        return _amo(machine, d, kind)
    handler.__name__ = f"amo_{kind}"
    return handler


def _op_tlbi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    address = machine._read_reg(d.register("rs1"))
    machine.flush_tlb(None if address == 0 else address)
    return None


def _exception_return(machine: "NovaMachine") -> int:
    """Shared MRET/IRET exception-return semantics."""
    if machine._csr[EPC] % INSTRUCTION_ALIGNMENT:
        machine._trap(EXCEPTION_CODES["ALIGNMENT_FAULT"], machine._csr[EPC])
    if not machine._exception_stack:
        machine._trap(EXCEPTION_CODES["ILLEGAL_INSTRUCTION"], 0)
    epc, _ecode, _ecause, priv = machine._exception_stack.pop()
    machine._csr[ECODE] = EXCEPTION_CODES["NONE"]
    machine._csr[EPC] = epc
    machine._csr[PRIV] = priv
    return machine._pc_target(epc)


def _op_iret(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return _exception_return(machine)


def _op_sipi(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    machine.send_ipi(d.unsigned_immediate("uimm"))
    return None


def _vm_enter(machine: "NovaMachine", save_host: bool) -> int:
    if machine._csr[HSTATUS] & 1:
        machine._trap(EXCEPTION_CODES["VM_FAULT"], VM_EXIT_EXPLICIT)
    if save_host:
        machine._host_state = (machine.pc + INSTRUCTION_BYTES, machine.priv,
                               machine._csr[PTBR])
    machine._csr[PTBR] = machine._csr[VPTBR]
    machine._csr[PRIV] = machine._csr[VPRIV]
    machine._csr[HSTATUS] = 1
    machine._csr[VEXIT] = 0
    machine.flush_tlb(None)
    return machine._pc_target(machine._csr[VPC])


def _op_vmlaunch(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    return _vm_enter(machine, True)


def _op_vmresume(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    if not (machine._csr[HSTATUS] & 1) and machine._host_state is None:
        machine._trap(EXCEPTION_CODES["VM_FAULT"], VM_EXIT_EXPLICIT)
    return _vm_enter(machine, False)


def _op_vmexit(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    if not (machine._csr[HSTATUS] & 1) or machine._host_state is None:
        machine._trap(EXCEPTION_CODES["VM_FAULT"], VM_EXIT_EXPLICIT)
    reason = d.unsigned_immediate("uimm")
    machine._csr[VEXIT] = reason
    machine._vm_leave()
    return machine.pc


def _op_obj_perm(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    reference = machine._read_reg(d.register("rs1"))
    obj = machine._object_field(reference, None)
    machine._write_reg(
        d.register("rd"),
        obj["read"] | obj["write"] | obj["exec"]
        | obj["grant"] | obj["drop"] | obj["meta"],
    )
    return None


def _op_obj_cmp(machine: "NovaMachine", d: DecodedInstruction) -> Optional[int]:
    first = machine._read_reg(d.register("rs1"))
    second = machine._read_reg(d.register("rs2"))
    same = first != 0 and first == second and first in machine.objects
    machine._write_reg(d.register("rd"), 1 if same else 0)
    return None


#: Mnemonic -> execution handler. A handler returns the next program counter,
#: or ``None`` to retire the instruction at ``PC + 4``. Handlers signal
#: architectural exceptions by calling ``machine._trap``.
HANDLERS: Mapping[str, Handler] = MappingProxyType(
    {
        "NOP": _op_nop,
        "ROTL": _op_rotl,
        "ROTR": _op_rotr,
        "UDIV": _op_udiv,
        "UREM": _op_urem,
        "AMOADD": _amo_handler("add"),
        "AMOSWAP": _amo_handler("swap"),
        "AMOCAS": _amo_handler("cas"),
        "AMOAND": _amo_handler("and"),
        "AMOOR": _amo_handler("or"),
        "AMOXOR": _amo_handler("xor"),
        "ADD": _op_add,
        "SUB": _op_sub,
        "MUL": _op_mul,
        "DIV": _op_div,
        "REM": _op_rem,
        "AND": _op_and,
        "OR": _op_or,
        "XOR": _op_xor,
        "SHL": _op_shl,
        "SHR": _op_shr,
        "SAR": _op_sar,
        "CMP": _op_cmp,
        "MOV": _op_mov,
        "NOT": _op_not,
        "NEG": _op_neg,
        "ADDI": _op_addi,
        "SUBI": _op_subi,
        "MULI": _op_muli,
        "ANDI": _op_andi,
        "ORI": _op_ori,
        "XORI": _op_xori,
        "SHLI": _op_shli,
        "SHRI": _op_shri,
        "SARI": _op_sari,
        "CMPI": _op_cmpi,
        "MOVI": _op_movi,
        "ROTLI": _op_rotli,
        "ROTRI": _op_rotri,
        "TLBI": _op_tlbi,
        "LDB": _op_ldb,
        "LDH": _op_ldh,
        "LDW": _op_ldw,
        "LDD": _op_ldd,
        "LDBU": _op_ldbu,
        "LDHU": _op_ldhu,
        "LDWU": _op_ldwu,
        "STB": _op_stb,
        "STH": _op_sth,
        "STW": _op_stw,
        "STD": _op_std,
        "JMP": _op_jmp,
        "BEQ": _branch_handler(lambda a, b: a == b),
        "BNE": _branch_handler(lambda a, b: a != b),
        "BLT": _branch_handler(lambda a, b: to_signed64(a) < to_signed64(b)),
        "BLE": _branch_handler(lambda a, b: to_signed64(a) <= to_signed64(b)),
        "BGT": _branch_handler(lambda a, b: to_signed64(a) > to_signed64(b)),
        "BGE": _branch_handler(lambda a, b: to_signed64(a) >= to_signed64(b)),
        "BLTU": _branch_handler(lambda a, b: a < b),
        "BLEU": _branch_handler(lambda a, b: a <= b),
        "BGTU": _branch_handler(lambda a, b: a > b),
        "BGEU": _branch_handler(lambda a, b: a >= b),
        "CALL": _op_call,
        "RET": _op_ret,
        "TRAP": _op_trap,
        "SCALL": _op_scall,
        "MRET": _op_mret,
        "IRET": _op_iret,
        "FENCE": _op_fence,
        "WAIT": _op_wait,
        "HALT": _op_halt,
        "SIPI": _op_sipi,
        "VMLAUNCH": _op_vmlaunch,
        "VMRESUME": _op_vmresume,
        "VMEXIT": _op_vmexit,
        "OBJ.CREATE": _op_obj_create,
        "OBJ.GET": _op_obj_get,
        "OBJ.SET": _op_obj_set,
        "OBJ.DROP": _op_obj_drop,
        "OBJ.PERM": _op_obj_perm,
        "OBJ.CMP": _op_obj_cmp,
        "CSRR": _op_csrr,
        "CSRW": _op_csrw,
    }
)

_missing = set(INSTRUCTION_TABLE) - set(HANDLERS)
if _missing:  # pragma: no cover - guards against an incomplete table
    raise RuntimeError(f"Nova instructions without an emulator handler: {sorted(_missing)}")
_extra = set(HANDLERS) - set(INSTRUCTION_TABLE)
if _extra:  # pragma: no cover - guards against a stale table
    raise RuntimeError(f"Emulator handlers without an ISA definition: {sorted(_extra)}")


# --------------------------------------------------------------------------
# The machine
# --------------------------------------------------------------------------


class NovaMachine:
    """Architectural-state emulator for the Nova ISA."""

    def __init__(self, code: bytes = b"", *, entry: int = RESET_ENTRY,
                 page_size: int = 4096, object_generation_seed: int = 1):
        if page_size <= 0 or page_size & (page_size - 1):
            raise ValueError("page_size must be a positive power of two.")
        if page_size < INSTRUCTION_BYTES:
            raise ValueError("page_size must be at least four bytes.")
        if entry < 0 or entry > WORD_MASK or entry % INSTRUCTION_ALIGNMENT:
            raise ValueError("entry must be a 4-byte-aligned unsigned 64-bit address.")
        if not 1 <= object_generation_seed <= OBJECT_GENERATION_MASK:
            raise ValueError("object_generation_seed must be a nonzero 32-bit value")
        self.object_generation_seed = object_generation_seed
        self.page_size = page_size
        self.memory: Dict[int, bytearray] = {}
        self.regs = [0] * GPR_COUNT
        self.pc = entry
        self._csr = [0] * CSR_COUNT
        for index, csr in CSR_TABLE.items():
            self._csr[index] = csr.reset
        self._exception_stack: List[Tuple[int, int, int, int]] = []
        self.objects: Dict[int, Dict[str, int]] = {}
        self.next_obj_id = 1
        self.object_generation = object_generation_seed
        self.halted = False
        # Nova v1.0 architectural state.
        self._tlb: Dict[Tuple[int, int, int, int], List[int]] = {}
        self._tlb2: Dict[Tuple[int, int, int, int], List[int]] = {}
        self.ipi_log: List[Tuple[int, int]] = []
        self.irq_log: List[int] = []
        self.io_log: List[Tuple[int, bytes]] = []
        self._host_state: Optional[Tuple[int, int, int]] = None
        self.last_amo_ordering: Tuple[int, int] = (0, 0)
        self.load_code(code, entry)

    # -- architectural state ------------------------------------------------

    @property
    def psr(self) -> int:
        return self._csr[PSR]

    @psr.setter
    def psr(self, value: int) -> None:
        self._csr[PSR] = to_u64(value)

    @property
    def epc(self) -> int:
        return self._csr[EPC]

    @epc.setter
    def epc(self, value: int) -> None:
        self._csr[EPC] = to_u64(value)

    @property
    def ecause(self) -> int:
        return self._csr[ECAUSE]

    @ecause.setter
    def ecause(self, value: int) -> None:
        self._csr[ECAUSE] = to_u64(value)

    @property
    def exception_code(self) -> int:
        return self._csr[ECODE]

    @exception_code.setter
    def exception_code(self, value: int) -> None:
        self._csr[ECODE] = to_u64(value)

    @property
    def priv(self) -> int:
        return self._csr[PRIV]

    @priv.setter
    def priv(self, value: int) -> None:
        self._csr[PRIV] = to_u64(value)

    @property
    def vbar(self) -> int:
        return self._csr[VBAR]

    @vbar.setter
    def vbar(self, value: int) -> None:
        self._csr[VBAR] = to_u64(value)

    @property
    def coreid(self) -> int:
        return self._csr[COREID]

    @property
    def privilege_name(self) -> str:
        return PRIVILEGE_NAMES.get(self.priv, "reserved")

    @property
    def exception_stack_depth(self) -> int:
        return len(self._exception_stack)

    def exception_stack(self) -> List[Tuple[int, int, int, int]]:
        """Copy of the exception save stack: ``(epc, ecode, ecause, priv)``."""
        return list(self._exception_stack)

    # -- registers ----------------------------------------------------------

    @staticmethod
    def register_index(reg: str | int) -> int:
        if isinstance(reg, bool):
            raise ValueError(f"Invalid register reference: {reg!r}")
        if isinstance(reg, int):
            if 0 <= reg < GPR_COUNT:
                return reg
            raise ValueError(f"Register index out of range: {reg}")
        key = reg.strip().lower()
        if key in REGISTERS:
            return REGISTERS[key]
        raise ValueError(f"Unknown register: {reg}")

    def read_register(self, reg: str | int) -> int:
        index = self.register_index(reg)
        return 0 if index == 0 else self.regs[index]

    def write_register(self, reg: str | int, value: int) -> None:
        index = self.register_index(reg)
        if index != 0:
            self.regs[index] = to_u64(value)

    def _read_reg(self, index: int) -> int:
        if not 0 <= index < GPR_COUNT:
            raise ValueError(f"Register index out of range: {index}")
        return 0 if index == 0 else self.regs[index]

    def _write_reg(self, index: int, value: int) -> None:
        if not 0 <= index < GPR_COUNT:
            raise ValueError(f"Register index out of range: {index}")
        if index != 0:
            self.regs[index] = to_u64(value)

    # -- control/status registers ------------------------------------------

    def read_csr(self, csr: str | int) -> int:
        """Host-side CSR read; bypasses the privilege check."""
        index = self._csr_index(csr)
        return self._csr[index]

    def write_csr(self, csr: str | int, value: int) -> None:
        """Host-side CSR write; bypasses privilege and WARL checks."""
        index = self._csr_index(csr)
        self._csr[index] = to_u64(value)
        if index in (PTBR, S2PTBR):
            self.flush_tlb(None)

    @staticmethod
    def _csr_index(csr: str | int) -> int:
        if isinstance(csr, bool):
            raise ValueError(f"Invalid CSR reference: {csr!r}")
        if isinstance(csr, int):
            if 0 <= csr < CSR_COUNT:
                return csr
            raise ValueError(f"CSR index out of range: {csr}")
        key = csr.strip().lower()
        if key in CSR_BY_NAME:
            return CSR_BY_NAME[key].index
        raise ValueError(f"Unknown CSR: {csr}")

    def _read_csr(self, index: int) -> int:
        csr = CSR_TABLE.get(index)
        if csr is None:
            # WARL: an unimplemented CSR index reads as zero.
            return 0
        if not csr.readable:
            self._trap(EXCEPTION_CODES["PRIVILEGE"], index)
        if self._privilege_rank(self.priv) < self._privilege_rank(
            PRIVILEGE_LEVELS[csr.min_read_privilege]
        ):
            self._trap(EXCEPTION_CODES["PRIVILEGE"], index)
        return self._csr[index] & csr.mask

    def _write_csr(self, index: int, value: int) -> None:
        csr = CSR_TABLE.get(index)
        if csr is None:
            # WARL: an unimplemented CSR index discards writes, so a later
            # revision can assign it without changing existing code.
            return
        if not csr.writable:
            self._trap(EXCEPTION_CODES["PRIVILEGE"], index)
        if self._privilege_rank(self.priv) < self._privilege_rank(
            PRIVILEGE_LEVELS[csr.min_write_privilege]
        ):
            self._trap(EXCEPTION_CODES["PRIVILEGE"], index)
        value = to_u64(value) & csr.mask
        if index == PRIV and value > PRIV_MACHINE:
            self._trap(EXCEPTION_CODES["PRIVILEGE"], value)
        if csr.align4 and value % INSTRUCTION_ALIGNMENT:
            self._trap(EXCEPTION_CODES["ALIGNMENT_FAULT"], value)
        self._csr[index] = value
        if index in (PTBR, S2PTBR):
            self.flush_tlb(None)

    @staticmethod
    def _privilege_rank(level: int) -> int:
        """Numeric ordering of a privilege level; reserved ranks above machine."""
        return _PRIVILEGE_RANKS.get(level, PRIV_RESERVED)

    # -- memory -------------------------------------------------------------

    def _page_base(self, address: int) -> int:
        return address & ~(self.page_size - 1)

    def _read_byte(self, address: int) -> int:
        page = self.memory.get(self._page_base(address))
        if page is None:
            return 0
        return page[address & (self.page_size - 1)]

    def _write_byte(self, address: int, value: int) -> None:
        base = self._page_base(address)
        page = self.memory.get(base)
        if page is None:
            page = bytearray(self.page_size)
            self.memory[base] = page
        page[address & (self.page_size - 1)] = value & 0xFF

    def _read_mem(self, address: int, size: int) -> int:
        if size not in (1, 2, 4, 8):
            raise ValueError(f"Unsupported access size: {size}")
        if address < 0 or address + size - 1 > WORD_MASK:
            raise ValueError(f"Data address out of range: {address}")
        value = 0
        for offset in range(size):
            value |= self._read_byte(address + offset) << (8 * offset)
        return value

    def _write_mem(self, address: int, value: int, size: int) -> None:
        if size not in (1, 2, 4, 8):
            raise ValueError(f"Unsupported access size: {size}")
        if address < 0 or address + size - 1 > WORD_MASK:
            raise ValueError(f"Data address out of range: {address}")
        for offset in range(size):
            self._write_byte(address + offset, value >> (8 * offset))

    def load_code(self, code: bytes, entry: int) -> None:
        """Write a raw ``.nob`` instruction image at ``entry``."""
        if entry < 0 or entry > WORD_MASK or entry % INSTRUCTION_ALIGNMENT:
            raise ValueError("entry must be a 4-byte-aligned unsigned 64-bit address.")
        if not isinstance(code, (bytes, bytearray, memoryview)):
            raise TypeError("Nova code images are byte strings.")
        code = bytes(code)
        if len(code) % INSTRUCTION_BYTES:
            raise ValueError("Code image length must be a multiple of four bytes.")
        if code and entry + len(code) - 1 > WORD_MASK:
            raise ValueError("Code image exceeds the 64-bit address space.")
        self.pc = entry
        for offset, byte in enumerate(code):
            self._write_byte(entry + offset, byte)

    # -- virtual memory (Nova v1.0) ----------------------------------------

    def flush_tlb(self, address: Optional[int] = None) -> None:
        """Flush every translation, or only the page containing ``address``."""
        if address is None:
            self._tlb.clear()
            self._tlb2.clear()
            return
        vpn = to_u64(address) >> PAGE_BITS
        for tlb in (self._tlb, self._tlb2):
            for key in [key for key in tlb if key[1] == vpn]:
                del tlb[key]

    def _canonical_va(self, address: int) -> bool:
        top_bits = WORD_BITS - VIRTUAL_ADDRESS_BITS + 1
        high = address >> (VIRTUAL_ADDRESS_BITS - 1)
        return high == 0 or high == (1 << top_bits) - 1

    def _mmu_fault(self, address: int, reason: int) -> None:
        self._csr[TVAL] = to_u64(address)
        self._trap(EXCEPTION_CODES["MMU_FAULT"], reason)

    def _walk_page_table(self, address: int) -> Tuple[int, int]:
        """Walk the Nova page table; return ``(pte, pte_address, level)``.

        A PTE with any of R/W/X set is a leaf. Its level determines the page
        size: level 3 maps 4 KiB, level 2 maps 2 MiB, and level 1 maps 1 GiB.
        A level-0 leaf is reserved.
        """
        ptbr = self._csr[PTBR]
        ppn_mask = (1 << (PHYSICAL_ADDRESS_BITS - PAGE_BITS)) - 1
        ppn = (ptbr >> PAGE_BITS) & ppn_mask
        for level in range(PT_LEVELS):
            shift = VIRTUAL_ADDRESS_BITS - PT_INDEX_BITS * (level + 1)
            index = (address >> shift) & (PT_ENTRIES - 1)
            pte_address = (ppn << PAGE_BITS) + index * 8
            if pte_address + 7 >= (1 << PHYSICAL_ADDRESS_BITS):
                self._mmu_fault(address, MMU_DETAIL_MISSING)
            pte = self._read_mem(pte_address, 8)
            if not pte & 0x1:
                self._mmu_fault(address, MMU_DETAIL_MISSING)
            if pte & ~((1 << PHYSICAL_ADDRESS_BITS) - 1):
                self._mmu_fault(address, MMU_DETAIL_RESERVED_PTE)
            if pte & 0xE:
                if level == 0:
                    # A level-0 leaf would be a 1 TiB page, which Nova does
                    # not define.
                    self._mmu_fault(address, MMU_DETAIL_RESERVED_PTE)
                page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
                if pte & PTE_PPN_MASK & ((1 << page_shift) - 1):
                    self._mmu_fault(address, MMU_DETAIL_MISALIGNED_PTE)
                return pte, pte_address, level
            ppn = (pte >> PAGE_BITS) & ppn_mask
        self._mmu_fault(address, MMU_DETAIL_MISSING)

    def _translate_stage2(self, ipa: int, size: int, access: str,
                          fault_va: int) -> int:
        """Translate a guest-physical address to a host physical address.

        Stage 2 uses the same four-level table and PTE format as stage 1, but
        ignores the user bit: ownership is enforced by stage 1. When stage 2
        is disabled it is an identity map. Faults always report the original
        guest virtual address in ``tval``.
        """
        s2ptbr = self._csr[S2PTBR]
        if not s2ptbr & (1 << 63):
            return ipa
        if ipa < 0 or ipa + size - 1 > WORD_MASK:
            self._trap(EXCEPTION_CODES["MEMORY_FAULT"], ipa)
        access_class = {"r": 0, "w": 1, "x": 2}[access]
        key = (s2ptbr, ipa >> PAGE_BITS, access_class, 0)
        entry = self._tlb2.get(key)
        if entry is None:
            ppn_mask = (1 << (PHYSICAL_ADDRESS_BITS - PAGE_BITS)) - 1
            ppn = (s2ptbr >> PAGE_BITS) & ppn_mask
            for level in range(PT_LEVELS):
                shift = VIRTUAL_ADDRESS_BITS - PT_INDEX_BITS * (level + 1)
                index = (ipa >> shift) & (PT_ENTRIES - 1)
                pte_ipa = (ppn << PAGE_BITS) + index * 8
                if pte_ipa + 7 >= (1 << PHYSICAL_ADDRESS_BITS):
                    self._mmu_fault(fault_va, MMU_DETAIL_MISSING)
                # Stage-2 PTEs live in host physical memory, so their
                # addresses are physical, not guest-physical.
                pte_address = pte_ipa
                pte = self._read_mem(pte_address, 8)
                if not pte & 0x1:
                    self._mmu_fault(fault_va, MMU_DETAIL_MISSING)
                if pte & ~((1 << PHYSICAL_ADDRESS_BITS) - 1):
                    self._mmu_fault(fault_va, MMU_DETAIL_RESERVED_PTE)
                if pte & 0xE:
                    if level == 0:
                        self._mmu_fault(fault_va, MMU_DETAIL_RESERVED_PTE)
                    page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
                    if pte & PTE_PPN_MASK & ((1 << page_shift) - 1):
                        self._mmu_fault(fault_va, MMU_DETAIL_MISALIGNED_PTE)
                    entry = [pte, pte_address, level]
                    self._tlb2[key] = entry
                    break
                ppn = (pte >> PAGE_BITS) & ppn_mask
            else:
                self._mmu_fault(fault_va, MMU_DETAIL_MISSING)
        pte, pte_address, level = entry
        required = {"r": 0x2, "w": 0x4, "x": 0x8}[access]
        if not pte & required:
            self._mmu_fault(fault_va, MMU_DETAIL_PERMISSION)
        if not pte & 0x40 or (access == "w" and not pte & 0x80):
            pte |= 0x40 | (0x80 if access == "w" else 0)
            entry[0] = pte
            self._write_mem(pte_address, pte, 8)
        page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
        physical = (pte & PTE_PPN_MASK) | (ipa & ((1 << page_shift) - 1))
        return physical & ((1 << PHYSICAL_ADDRESS_BITS) - 1)

    def _walk_guest_page_table(self, address: int,
                               fault_va: int) -> Tuple[int, int, int]:
        """Stage-1 walk whose PTE reads pass through stage 2."""
        ptbr = self._csr[PTBR]
        ppn_mask = (1 << (PHYSICAL_ADDRESS_BITS - PAGE_BITS)) - 1
        ppn = (ptbr >> PAGE_BITS) & ppn_mask
        for level in range(PT_LEVELS):
            shift = VIRTUAL_ADDRESS_BITS - PT_INDEX_BITS * (level + 1)
            index = (address >> shift) & (PT_ENTRIES - 1)
            pte_ipa = (ppn << PAGE_BITS) + index * 8
            pte_address = self._translate_stage2(pte_ipa, 8, "r", fault_va)
            pte = self._read_mem(pte_address, 8)
            if not pte & 0x1:
                self._mmu_fault(fault_va, MMU_DETAIL_MISSING)
            if pte & ~((1 << PHYSICAL_ADDRESS_BITS) - 1):
                self._mmu_fault(fault_va, MMU_DETAIL_RESERVED_PTE)
            if pte & 0xE:
                if level == 0:
                    self._mmu_fault(fault_va, MMU_DETAIL_RESERVED_PTE)
                page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
                if pte & PTE_PPN_MASK & ((1 << page_shift) - 1):
                    self._mmu_fault(fault_va, MMU_DETAIL_MISALIGNED_PTE)
                return pte, pte_address, level
            ppn = (pte >> PAGE_BITS) & ppn_mask
        self._mmu_fault(fault_va, MMU_DETAIL_MISSING)

    def _translate_nested(self, address: int, size: int, access: str) -> int:
        """Compose stage 1 (virtual to guest-physical) with stage 2."""
        ptbr = self._csr[PTBR]
        if not ptbr & (1 << 63):
            ipa = address  # guest bare mode still passes through stage 2
        else:
            pte, pte_address, level = self._walk_guest_page_table(address, address)
            required = {"r": 0x2, "w": 0x4, "x": 0x8}[access]
            if not pte & required or (self.priv == PRIV_USER and not pte & 0x10):
                self._mmu_fault(address, MMU_DETAIL_PERMISSION)
            page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
            ipa = (pte & PTE_PPN_MASK) | (address & ((1 << page_shift) - 1))
            if not pte & 0x40 or (access == "w" and not pte & 0x80):
                pte |= 0x40 | (0x80 if access == "w" else 0)
                # pte_address is already the host physical address returned
                # by the stage-2 walk, so write back directly.
                self._write_mem(pte_address, pte, 8)
        return self._translate_stage2(ipa, size, access, address)

    def _translate(self, address: int, size: int, access: str) -> int:
        """Translate a virtual address and check its access permissions."""
        address = to_u64(address)
        if (self._csr[HSTATUS] & 1) and (self._csr[S2PTBR] & (1 << 63)):
            # A guest with stage-2 translation active composes both stages.
            if not self._canonical_va(address):
                self._mmu_fault(address, MMU_DETAIL_NONCANONICAL)
            if address + size - 1 > WORD_MASK:
                self._trap(EXCEPTION_CODES["MEMORY_FAULT"], address)
            return self._translate_nested(address, size, access)
        ptbr = self._csr[PTBR]
        if not ptbr & (1 << 63):
            # Bare mode is an identity map over the whole 64-bit address space.
            # The 52-bit physical width constrains addresses produced by a
            # page-table walk, not the identity map (a downward-growing stack
            # below address zero is a legitimate bare-mode address).
            return address
        if not self._canonical_va(address):
            self._mmu_fault(address, MMU_DETAIL_NONCANONICAL)
        if address + size - 1 > WORD_MASK:
            self._trap(EXCEPTION_CODES["MEMORY_FAULT"], address)
        access_class = {"r": 0, "w": 1, "x": 2}[access]
        key = (ptbr, address >> PAGE_BITS, access_class,
               int(self.priv == PRIV_USER))
        entry = self._tlb.get(key)
        if entry is None:
            pte, pte_address, level = self._walk_page_table(address)
            entry = [pte, pte_address, level]
            self._tlb[key] = entry
        pte, pte_address, level = entry
        required = {"r": 0x2, "w": 0x4, "x": 0x8}[access]
        if not pte & required or (self.priv == PRIV_USER and not pte & 0x10):
            self._mmu_fault(address, MMU_DETAIL_PERMISSION)
        if not pte & 0x40 or (access == "w" and not pte & 0x80):
            pte |= 0x40 | (0x80 if access == "w" else 0)
            entry[0] = pte
            self._write_mem(pte_address, pte, 8)
        page_shift = PAGE_BITS + PT_INDEX_BITS * (PT_LEVELS - 1 - level)
        physical = (pte & PTE_PPN_MASK) | (address & ((1 << page_shift) - 1))
        return physical & ((1 << PHYSICAL_ADDRESS_BITS) - 1)

    # -- interrupts (Nova v1.0) ---------------------------------------------

    def set_interrupt_pending(self, source: int) -> None:
        """Host-side interrupt injection."""
        if source not in INTERRUPT_SOURCES:
            raise ValueError(f"Unknown interrupt source: {source}")
        self._csr[IP] |= 1 << source

    def send_ipi(self, target: int) -> None:
        """Architecturally send a software interrupt to hart ``target``."""
        target = to_u64(target)
        self.ipi_log.append((target, INT_SOFTWARE))
        if target == self.coreid:
            self.set_interrupt_pending(INT_SOFTWARE)

    def _tick_timer(self) -> None:
        self._csr[TIME] = to_u64(self._csr[TIME] + 1)
        compare = self._csr[TIMECMP]
        # The timer fires once when the counter reaches the compare value.
        # A handler acknowledges it by programming a new future timecmp.
        if compare != WORD_MASK and self._csr[TIME] == compare:
            self.set_interrupt_pending(INT_TIMER)

    def _pending_interrupt(self) -> Optional[int]:
        pending = self._csr[IP] & self._csr[IE] & INTERRUPT_IE_MASK
        for source in INTERRUPT_SOURCES:
            if pending & (1 << source):
                return source
        return None

    # -- virtualization (Nova v1.0) -----------------------------------------

    def _vm_leave(self) -> None:
        if self._host_state is not None:
            pc, priv, ptbr = self._host_state
            self.pc = pc
            self._csr[PRIV] = priv
            self._csr[PTBR] = ptbr
            self._host_state = None
        self._csr[HSTATUS] = 0
        self.flush_tlb(None)

    def _guest_trap(self, trap: "NovaTrap") -> None:
        """Turn a guest trap into a VM exit and resume the host."""
        self._csr[VEXIT] = trap.code
        self._csr[ECAUSE] = trap.detail
        self._csr[ECODE] = EXCEPTION_CODES["NONE"]
        self._vm_leave()

    # -- objects ------------------------------------------------------------

    @staticmethod
    def _permission_key(permission: int) -> str:
        return {
            OBJECT_PERM_READ: "read",
            OBJECT_PERM_WRITE: "write",
            OBJECT_PERM_EXEC: "exec",
            OBJECT_PERM_GRANT: "grant",
            OBJECT_PERM_DROP: "drop",
            OBJECT_PERM_META: "meta",
        }[permission]

    @staticmethod
    def _permission_mask(obj: Mapping[str, int]) -> int:
        return (
            obj["read"] | obj["write"] | obj["exec"]
            | obj["grant"] | obj["drop"] | obj["meta"]
        )

    @staticmethod
    def _object_from_mask(mask: int, size: int) -> Dict[str, int]:
        if mask & ~OBJECT_PERM_MASK:
            raise ObjectFormatError(f"unknown object permission bits: {mask:#x}")
        record: Dict[str, int] = {"size": to_u64(size)}
        for permission in (OBJECT_PERM_READ, OBJECT_PERM_WRITE,
                           OBJECT_PERM_EXEC, OBJECT_PERM_GRANT,
                           OBJECT_PERM_DROP, OBJECT_PERM_META):
            record[NovaMachine._permission_key(permission)] = mask & permission
        return record

    def _next_generation(self) -> int:
        if self.object_generation > OBJECT_GENERATION_MASK:
            raise ObjectFormatError("object generation space exhausted")
        generation = self.object_generation
        self.object_generation += 1
        return generation

    def _create_object(self, size: int) -> int:
        """Create an object capability and return its stable 64-bit reference.

        The reference encodes a generation and an index. Neither is ever
        reused, so a reference to a destroyed object can never accidentally
        resolve to a later allocation.
        """
        if self.next_obj_id > OBJECT_INDEX_MASK:
            raise ObjectFormatError("object index space exhausted")
        index = self.next_obj_id
        self.next_obj_id += 1
        reference = make_object_reference(self._next_generation(), index)
        self.objects[reference] = self._object_from_mask(OBJECT_PERM_MASK, size)
        return reference

    def _object_field(self, reference: int,
                      permission: Optional[int] = None) -> Dict[str, int]:
        """Resolve a reference, checking nullness and capability permissions."""
        if reference == 0:
            self._trap(EXCEPTION_CODES["OBJECT_FAULT"], OBJECT_DETAIL_NULL)
        obj = self.objects.get(reference)
        if obj is None:
            self._trap(EXCEPTION_CODES["OBJECT_FAULT"], OBJECT_DETAIL_INVALID)
        if permission is not None and not obj[self._permission_key(permission)]:
            self._trap(EXCEPTION_CODES["OBJECT_FAULT"], OBJECT_DETAIL_PERMISSION)
        return obj  # type: ignore[return-value]

    def _drop_object(self, reference: int) -> None:
        self._object_field(reference, OBJECT_PERM_DROP)
        del self.objects[reference]

    # -- persistent object identities ---------------------------------------

    def export_objects(self) -> bytes:
        """Export live objects as a deterministic, versioned binary container.

        References are exported unchanged, so they remain valid identities in
        any machine that imports the container.
        """
        records = [
            {
                "reference": reference,
                "size": obj["size"],
                "permissions": self._permission_mask(obj),
                "flags": 0,
            }
            for reference, obj in self.objects.items()
        ]
        return objfmt.encode_objects(records)

    def import_objects(self, data: bytes, *, on_duplicate: str = "error") -> int:
        """Import persistent object identities from ``data``.

        ``on_duplicate`` selects the behavior when a reference already exists
        in this machine: ``error`` (default) rejects it, ``skip`` leaves the
        existing object untouched, and ``replace`` overwrites it. External
        records are never created: they must resolve to an existing object
        with matching size and permissions.

        Returns the number of objects actually created.
        """
        if on_duplicate not in ("error", "skip", "replace"):
            raise ValueError("on_duplicate must be 'error', 'skip', or 'replace'")
        records = objfmt.decode_objects(data)

        seen = set()
        for record in records:
            reference = record["reference"]
            if reference in seen:
                raise ObjectFormatError(
                    f"duplicate reference in container: {reference:#x}"
                )
            seen.add(reference)
            if record["flags"] & OBJECT_FLAG_EXTERNAL and reference not in self.objects:
                raise ObjectFormatError(
                    f"unresolved external reference: {reference:#x}"
                )

        created = 0
        for record in records:
            reference = record["reference"]
            if record["flags"] & OBJECT_FLAG_EXTERNAL:
                obj = self.objects[reference]
                if (obj["size"] != record["size"]
                        or self._permission_mask(obj) != record["permissions"]):
                    raise ObjectFormatError(
                        f"inconsistent external reference: {reference:#x}"
                    )
                continue
            if reference in self.objects:
                if on_duplicate == "error":
                    raise ObjectFormatError(
                        f"duplicate import of reference: {reference:#x}"
                    )
                if on_duplicate == "skip":
                    continue
            self.objects[reference] = self._object_from_mask(
                record["permissions"], record["size"]
            )
            created += 1

        # Keep local allocators ahead of every reference now in the table so a
        # fresh object cannot be given a reference that was just imported.
        for reference in self.objects:
            generation, index = split_object_reference(reference)
            self.object_generation = min(
                OBJECT_GENERATION_MASK + 1,
                max(self.object_generation, generation + 1),
            )
            self.next_obj_id = min(
                OBJECT_INDEX_MASK + 1,
                max(self.next_obj_id, index + 1),
            )
        return created

    # -- exceptions ---------------------------------------------------------

    def _trap(self, code: int, detail: int = 0,
              return_pc: Optional[int] = None) -> None:
        raise NovaTrap(code, to_u64(detail), return_pc)

    def _set_compare_flags(self, lhs: int, rhs: int) -> None:
        result = to_u64(lhs - rhs)
        zero = result == 0
        negative = bool(result & (1 << 63))
        carry = lhs >= rhs
        overflow = bool((lhs ^ rhs) & (lhs ^ result) & (1 << 63))
        self._csr[PSR] = (
            (int(zero) << FLAG_ZERO)
            | (int(negative) << FLAG_NEGATIVE)
            | (int(carry) << FLAG_CARRY)
            | (int(overflow) << FLAG_OVERFLOW)
        )

    def _enter_exception(self, code: int, detail: int = 0, *,
                         return_pc: Optional[int] = None,
                         vector: Optional[int] = None) -> None:
        """Architectural exception entry.

        Entry saves the outgoing exception context on a save stack, writes the
        new exception state into the CSRs, switches to machine privilege, and
        transfers control to ``vbar``. The frame records the return address and
        the privilege level that was active before entry, which is exactly what
        ``MRET`` restores.
        """
        epc = to_u64(self.pc if return_pc is None else return_pc)
        detail = to_u64(detail)
        previous_priv = self._csr[PRIV]
        self._exception_stack.append((epc, code, detail, previous_priv))
        if len(self._exception_stack) > EXCEPTION_STACK_DEPTH:
            # The save stack has a fixed depth; the oldest frame is discarded.
            del self._exception_stack[0]
        self._csr[ECODE] = code
        self._csr[ECAUSE] = detail
        self._csr[EPC] = epc
        self._csr[PRIV] = PRIV_MACHINE
        self.pc = self._csr[VBAR] if vector is None else to_u64(vector)

    def inject_exception(self, code: int, detail: int = 0, *,
                         return_pc: Optional[int] = None) -> None:
        """Host-side exception injection, used to drive handlers in tests."""
        self._enter_exception(code, detail, return_pc=return_pc)

    # -- execution ----------------------------------------------------------

    def _pc_target(self, target: int) -> int:
        if target < 0 or target > WORD_MASK:
            self._trap(EXCEPTION_CODES["MEMORY_FAULT"], to_u64(target))
        if target % INSTRUCTION_ALIGNMENT:
            self._trap(EXCEPTION_CODES["ALIGNMENT_FAULT"], target)
        return target

    def _fetch(self, pc: int) -> int:
        if pc % INSTRUCTION_ALIGNMENT:
            self._trap(EXCEPTION_CODES["ALIGNMENT_FAULT"], pc)
        if pc + INSTRUCTION_BYTES - 1 > WORD_MASK:
            self._trap(EXCEPTION_CODES["MEMORY_FAULT"], pc)
        physical = self._translate(pc, INSTRUCTION_BYTES, "x")
        return self._read_mem(physical, INSTRUCTION_BYTES)

    def step(self) -> None:
        """Execute one architectural instruction."""
        if self.halted:
            return
        pc = self.pc
        try:
            self._tick_timer()
            source = self._pending_interrupt()
            if source is not None:
                self.irq_log.append(source)
                self._csr[IP] &= ~(1 << source)
                self._enter_exception(
                    EXCEPTION_CODES["INTERRUPT"], source,
                    return_pc=pc, vector=self._csr[IVEC],
                )
                return
            word = self._fetch(pc)
            decoded = decode_instruction(word)
            if decoded is None:
                self._trap(EXCEPTION_CODES["ILLEGAL_INSTRUCTION"], word)
            if decoded.privilege == "machine" and self.priv != PRIV_MACHINE:
                self._trap(EXCEPTION_CODES["PRIVILEGE"], decoded.opcode)
            target = HANDLERS[decoded.mnemonic](self, decoded)
            if target is None:
                # Retire: the next instruction follows this one.
                next_pc = pc + INSTRUCTION_BYTES
                if next_pc > WORD_MASK:
                    self._trap(EXCEPTION_CODES["MEMORY_FAULT"], pc)
                target = next_pc
        except NovaTrap as trap:
            if self._csr[HSTATUS] & 1:
                self._guest_trap(trap)
            else:
                self._enter_exception(trap.code, trap.detail,
                                      return_pc=trap.return_pc)
            return
        self.pc = target

    def run(self, steps: int = 1, *, stop_on_exception: bool = True) -> None:
        """Execute up to ``steps`` instructions, or until halt or exception."""
        for _ in range(steps):
            if self.halted:
                break
            self.step()
            if stop_on_exception and self.exception_code != EXCEPTION_CODES["NONE"]:
                break


class NovaCluster:
    """A cooperative cluster of Nova harts with shared memory and IPIs.

    This models only the ISA-visible multicore contract: each hart has a
    unique ``coreid``, all harts observe the same memory, and a hart can raise
    a software interrupt on another hart. No interconnect, cache coherence,
    or physical core design is modeled.
    """

    def __init__(self, hart_count: int):
        if hart_count < 1:
            raise ValueError("a Nova cluster needs at least one hart")
        self.harts: List[NovaMachine] = []
        for hart_id in range(hart_count):
            machine = NovaMachine()
            machine.write_csr(COREID, hart_id)
            if self.harts:
                machine.memory = self.harts[0].memory
            self.harts.append(machine)

    @property
    def hart_count(self) -> int:
        return len(self.harts)

    def load(self, hart_id: int, code: bytes, entry: int) -> None:
        """Load a code image into one hart."""
        self._hart(hart_id).load_code(code, entry)

    def _hart(self, hart_id: int) -> NovaMachine:
        if not 0 <= hart_id < self.hart_count:
            raise ValueError(f"Hart index out of range: {hart_id}")
        return self.harts[hart_id]

    def send_ipi(self, source: int, target: int) -> None:
        """Deliver a software interrupt from one hart to another."""
        if not 0 <= target < self.hart_count:
            raise ValueError(f"Hart index out of range: {target}")
        sender = self._hart(source)
        sender.ipi_log.append((target, INT_SOFTWARE))
        self._hart(target).set_interrupt_pending(INT_SOFTWARE)

    def step(self, hart_id: int) -> None:
        self._hart(hart_id).step()

    def run(self, steps: int = 1, *, stop_on_exception: bool = True) -> None:
        """Round-robin ``steps`` instruction slots across the harts."""
        for _ in range(steps):
            for machine in self.harts:
                if not machine.halted:
                    machine.step()
                    if stop_on_exception and machine.exception_code != EXCEPTION_CODES["NONE"]:
                        break