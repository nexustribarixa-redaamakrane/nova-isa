"""Generate a QEMU-loadable x86-64 host for a Nova NPK kernel image.

QEMU has no Nova target. This module does not claim otherwise. It emits a
small multiboot ELF that boots in ``qemu-system-x86_64``, switches to long mode,
and interprets every 32-bit Nova instruction in the supplied ``.nob`` image.

The Nova image is placed in a 1 MiB guest-physical shadow at ``0x100000``. The
interpreter adds that offset to every Nova data address, so Nova software still
observes its architectural addresses (stack, boot marker, receipts, vectors)
even though x86-64 never executes Nova code natively. Translated code lives far
above the shadow so an out-of-range Nova store cannot overwrite the host.

The translation is a boot verifier, not a new NPK execution backend. It is used
only by the CMake/QEMU kernel workflow and never by the Nova ISA reference
emulator or Phase 1 tests.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Optional

from ..isa import (
    CSR_COUNT,
    CSR_TABLE,
    ECODE as CSR_ECODE,
    EPC as CSR_EPC,
    EXCEPTION_CODES,
    INSTRUCTION_ALIGNMENT,
    PRIV as CSR_PRIV,
    decode_instruction,
)
from ..nob import NobImage

__all__ = ["QemuTranslationError", "build_qemu_elf", "QEMU_LAYOUT"]


class QemuTranslationError(RuntimeError):
    """Raised when a Nova image cannot be represented in the QEMU harness."""


# Fixed guest-physical locations used by the generated harness. These do not
# overlap the Nova image, boot stack, or NPK observation regions.
QEMU_LAYOUT = {
    # Nova's 0x1000-based image is loaded into a 1 MiB shadow above the PC
    # BIOS region. The interpreter adds ``shadow`` to every Nova address;
    # Nova software still observes its architectural addresses unchanged.
    "shadow": 0x100000,
    "shadow_size": 0x100000,
    "harness_base": 0x800000,
    "entry": 0x800010,
    "pml4": 0x810000,
    "pdpt": 0x811000,
    "pd": 0x812000,
    "gdt": 0x816000,
    "regs": 0x817000,
    "csrs": 0x818000,
    "table": 0x819000,
    # Keep translated code far above the 1 MiB Nova shadow so an out-of-range
    # Nova store cannot overwrite the interpreter itself.
    "code": 0x02000000,
    "done": 0x8F0000,
    "steps": 0x8F0008,
    "error_pc": 0x8F0010,
    "error_word": 0x8F0018,
    "ex_depth": 0x8F0020,
    "ex_stack": 0x8F0100,  # 32 entries * 32 bytes: epc, ecode, ecause, priv
}

_DONE_MAGIC = 0x4E564D51  # "QMVN" little-endian
_ERROR_MAGIC = 0xDEADBEEF
_REGS_SIZE = 32 * 8

# x86-64 register numbers used by the emitter.
_RAX, _RCX, _RDX, _RBX, _RDI = 0, 1, 2, 3, 7
_R15 = 15


def _imm32(value: int) -> bytes:
    return struct.pack("<i", value)


def _rel32(target: int, current: int, size: int = 5) -> bytes:
    delta = target - (current + size)
    if not -(1 << 31) <= delta < (1 << 31):
        raise QemuTranslationError("generated branch is out of range")
    return struct.pack("<i", delta)


class _Emitter:
    """A deliberately small x86-64 machine-code emitter."""

    def __init__(self, base: int) -> None:
        self.base = base
        self.buf = bytearray()

    @property
    def address(self) -> int:
        return self.base + len(self.buf)

    def emit(self, *data: int) -> None:
        self.buf.extend(data)

    def emit_bytes(self, data: bytes) -> None:
        self.buf.extend(data)

    # -- register/register ---------------------------------------------------
    def mov_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x89, 0xC0 | (src << 3) | dst)

    def add_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x01, 0xC0 | (src << 3) | dst)

    def sub_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x29, 0xC0 | (src << 3) | dst)

    def imul_rr(self, dst: int, src: int) -> None:
        # 0F AF /r is IMUL r64, r/m64: reg is the destination, r/m the source.
        self.emit(0x48, 0x0F, 0xAF, 0xC0 | (dst << 3) | src)

    def and_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x21, 0xC0 | (src << 3) | dst)

    def or_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x09, 0xC0 | (src << 3) | dst)

    def xor_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x31, 0xC0 | (src << 3) | dst)

    def cmp_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x39, 0xC0 | (src << 3) | dst)

    def test_rr(self, dst: int, src: int) -> None:
        self.emit(0x48, 0x85, 0xC0 | (src << 3) | dst)

    # -- register/immediate --------------------------------------------------
    def mov_ri(self, reg: int, value: int) -> None:
        rex = 0x48 | (1 if reg >= 8 else 0)
        self.emit(rex, 0xB8 | (reg & 7))
        self.emit_bytes(struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def add_ri(self, reg: int, value: int) -> None:
        if -128 <= value < 128:
            self.emit(0x48, 0x83, 0xC0 | reg, value & 0xFF)
        else:
            self.emit(0x48, 0x81, 0xC0 | reg)
            self.emit_bytes(_imm32(value))

    def sub_ri(self, reg: int, value: int) -> None:
        if -128 <= value < 128:
            self.emit(0x48, 0x83, 0xE8 | reg, value & 0xFF)
        else:
            self.emit(0x48, 0x81, 0xE8 | reg)
            self.emit_bytes(_imm32(value))

    def and_ri(self, reg: int, value: int) -> None:
        if -128 <= value < 128:
            self.emit(0x48, 0x83, 0xE0 | reg, value & 0xFF)
        else:
            self.emit(0x48, 0x81, 0xE0 | reg)
            self.emit_bytes(_imm32(value))

    def or_ri(self, reg: int, value: int) -> None:
        if -128 <= value < 128:
            self.emit(0x48, 0x83, 0xC8 | reg, value & 0xFF)
        else:
            self.emit(0x48, 0x81, 0xC8 | reg)
            self.emit_bytes(_imm32(value))

    def xor_ri(self, reg: int, value: int) -> None:
        if -128 <= value < 128:
            self.emit(0x48, 0x83, 0xF0 | reg, value & 0xFF)
        else:
            self.emit(0x48, 0x81, 0xF0 | reg)
            self.emit_bytes(_imm32(value))

    def cmp_ri(self, reg: int, value: int) -> None:
        self.emit(0x48, 0x81, 0xF8 | reg)
        self.emit_bytes(_imm32(value))

    def shr_ri(self, reg: int, value: int) -> None:
        self.emit(0x48, 0xC1, 0xE8 | reg, value & 0x3F)

    # -- Nova register file ([r15 + offset]) ---------------------------------
    def load_reg(self, dst: int, index: int) -> None:
        # REX.W|B, opcode 8B /r, mod=10 rm=111 ([r15 + disp32]).
        self.emit(0x49, 0x8B, 0x80 | (dst << 3) | 7)
        self.emit_bytes(_imm32(index * 8))

    def store_reg(self, index: int, src: int) -> None:
        self.emit(0x49, 0x89, 0x80 | (src << 3) | 7)
        self.emit_bytes(_imm32(index * 8))

    def store_reg_imm(self, index: int, value: int) -> None:
        self.emit(0x49, 0xC7, 0x87)
        self.emit_bytes(_imm32(index * 8))
        self.emit_bytes(_imm32(value))

    # -- CSR file ([csr_base + index*8]) ------------------------------------
    def load_csr(self, reg: int, index: int) -> None:
        # mov reg,[rdx+disp32]; rdx already holds the CSR base.
        rex = 0x48 | (1 if reg >= 8 else 0)
        self.emit(rex, 0x8B, 0x82 | ((reg & 7) << 3))
        self.emit_bytes(_imm32(index * 8))

    def store_csr(self, index: int, src: int) -> None:
        # mov [rdx+disp32],src; rdx already holds the CSR base.
        rex = 0x48 | (1 if src >= 8 else 0)
        self.emit(rex, 0x89, 0x82 | ((src & 7) << 3))
        self.emit_bytes(_imm32(index * 8))

    # -- guest memory (identity mapped) -------------------------------------
    def load_mem(self, dst: int, size: int, signed: bool) -> None:
        modrm = (dst & 7) << 3
        if size == 8:
            self.emit(0x48, 0x8B, modrm)
        elif size == 4:
            self.emit(0x48, 0x63, modrm) if signed else self.emit(
                0x8B, modrm)
        elif size == 2:
            self.emit(0x48, 0x0F, 0xBF if signed else 0xB7, modrm)
        elif size == 1:
            self.emit(0x48, 0x0F, 0xBE if signed else 0xB6, modrm)
        else:
            raise QemuTranslationError(f"unsupported load size {size}")

    def store_mem(self, src: int, size: int) -> None:
        modrm = (src & 7) << 3
        if size == 8:
            self.emit(0x48, 0x89, modrm)
        elif size == 4:
            self.emit(0x89, modrm)
        elif size == 2:
            self.emit(0x66, 0x89, modrm)
        elif size == 1:
            self.emit(0x88, modrm)
        else:
            raise QemuTranslationError(f"unsupported store size {size}")

    # -- control flow -------------------------------------------------------
    def jmp_rel(self, target: int) -> None:
        start = self.address
        self.emit(0xE9)
        self.emit_bytes(_rel32(target, start))

    def jcc_rel(self, code: int, target: int) -> None:
        start = self.address
        self.emit(0x0F, code)
        self.emit_bytes(_rel32(target, start + 2))

    def jmp_reg(self, reg: int) -> None:
        self.emit(0xFF, 0xE0 | reg)

    def hlt(self) -> None:
        self.emit(0xF4)


def _multiboot_header() -> bytes:
    magic = 0x1BADB002
    checksum = (-magic) & 0xFFFFFFFF
    return struct.pack("<III", magic, 0, checksum)


def _long_mode_stub(entry: int) -> bytes:
    """32-bit multiboot entry that installs identity paging and jumps to 64-bit."""
    stub = bytearray()
    stub.extend(b"\xfa")                                   # cli
    # lgdt [absolute gdtr] (mod=00, reg=2, rm=101 => disp32 in 32-bit mode).
    # _gdt() places the six-byte pseudo-descriptor after the three descriptors.
    stub.extend(b"\x0f\x01\x15")
    stub.extend(struct.pack("<I", QEMU_LAYOUT["gdt"] + 24))
    # mov eax, cr4; or eax, PAE|PSE; mov cr4, eax
    stub.extend(b"\x0f\x20\xe0\x0d\x30\x00\x00\x00\x0f\x22\xe0")
    # mov eax, PML4; mov cr3, eax
    stub.extend(b"\xb8")
    stub.extend(struct.pack("<I", QEMU_LAYOUT["pml4"]))
    stub.extend(b"\x0f\x22\xd8")
    # mov ecx, MSR_EFER; rdmsr; or eax, LME; wrmsr
    stub.extend(b"\xb9\x80\x00\x00\xc0\x0f\x32\x0d\x00\x01\x00\x00\x0f\x30")
    # mov eax, cr0; or eax, PG|PE; mov cr0, eax
    stub.extend(b"\x0f\x20\xc0\x0d\x01\x00\x00\x80\x0f\x22\xc0")
    # jmp far 0x08:entry
    stub.append(0xEA)
    stub.extend(struct.pack("<I", entry))
    stub.extend(struct.pack("<H", 0x08))
    return bytes(stub)


def _page_tables() -> bytes:
    """Build PML4, PDPT, and four 2 MiB page-directory tables."""
    pml4 = bytearray(4096)
    pdpt = bytearray(4096)
    pml4[0:8] = struct.pack("<Q", QEMU_LAYOUT["pdpt"] | 0x7)
    for i in range(4):
        pdpt[i * 8:(i + 1) * 8] = struct.pack(
            "<Q", QEMU_LAYOUT["pd"] + i * 4096 | 0x7)
    tables = bytearray()
    for region in range(4):
        pd = bytearray(4096)
        for i in range(512):
            pd[i * 8:(i + 1) * 8] = struct.pack(
                "<Q", (region * (1 << 30) + i * (1 << 21)) | 0x83)
        tables.extend(pd)
    return bytes(pml4 + pdpt + tables)


def _gdt() -> bytes:
    null = b"\x00" * 8
    code = struct.pack("<HHBBBB", 0xFFFF, 0, 0, 0x9A, 0xAF, 0)
    data = struct.pack("<HHBBBB", 0xFFFF, 0, 0, 0x92, 0xCF, 0)
    table = null + code + data
    gdtr = struct.pack("<HI", len(table) - 1, QEMU_LAYOUT["gdt"])
    return table + gdtr


class _NovaTranslator:
    def __init__(self, image: NobImage) -> None:
        self.image = image
        self.image_base = image.image_base
        self.image_end = image.image_base + len(image.image)
        self.words = len(image.image) // 4
        if self.words * 4 != len(image.image):
            raise QemuTranslationError("Nova image size is not instruction aligned")
        self.table_addr = QEMU_LAYOUT["table"]
        self.code_base = QEMU_LAYOUT["code"]
        self.code = _Emitter(self.code_base)
        self.blocks: List[int] = [0] * self.words
        self._error_addr = 0
        self._dispatch_addr = 0

    def block_addr(self, index: int) -> int:
        return self.blocks[index]

    def target_index(self, address: int) -> Optional[int]:
        if address < self.image_base or address >= self.image_end:
            return None
        if address % INSTRUCTION_ALIGNMENT:
            return None
        index = (address - self.image_base) // 4
        return index if 0 <= index < self.words else None

    def _emit_dispatch(self) -> None:
        # Decode the current Nova PC from the dispatch table. PC is kept in
        # rax only for the duration of an indirect jump; normal blocks branch
        # directly to their statically translated successor.
        self._dispatch_addr = self.code.address
        e = self.code
        e.shr_ri(_RAX, 2)                                   # index = delta / 4
        e.mov_ri(_RDX, self.table_addr)
        e.emit(0x48, 0x8B, 0x04, 0xC2)                      # mov rax,[rdx+rax*8]
        e.jmp_reg(_RAX)

    def _emit_error(self) -> None:
        self._error_addr = self.code.address
        e = self.code
        # RDI carries the failing Nova PC; helpers may freely overwrite RAX.
        e.mov_ri(_RDX, QEMU_LAYOUT["error_pc"])
        e.emit(0x48, 0x89, 0x3A)                            # mov [rdx],rdi
        e.mov_ri(_RCX, _ERROR_MAGIC)
        e.mov_ri(_RDX, QEMU_LAYOUT["done"])
        e.emit(0x48, 0x89, 0x0A)                            # mov [rdx],rcx
        e.hlt()

    def _emit_exception_return(self) -> None:
        """Model Nova MRET/IRET shared semantics against hidden exception stack.

        Mirrors emulator._exception_return: checks EPC alignment, requires
        non-empty exception stack, pops frame (epc,ecode,ecause,priv), sets
        ECODE=NONE, EPC=epc, PRIV=priv, and transfers to epc.
        """
        e = self.code
        # RDI holds the failing Nova PC for any error exit; do not clobber it.
        # load_csr/store_csr require RDX to hold the CSR-file base.
        # Check EPC CSR alignment (architectural check in _exception_return).
        e.mov_ri(_RDX, QEMU_LAYOUT["csrs"])
        e.load_csr(_RAX, CSR_EPC)
        e.emit(0x48, 0xA9)                                  # test rax,3
        e.emit_bytes(_imm32(INSTRUCTION_ALIGNMENT - 1))
        e.jcc_rel(0x85, self._error_addr)                   # jne error
        # depth at ex_depth
        e.mov_ri(_RDX, QEMU_LAYOUT["ex_depth"])
        e.emit(0x48, 0x8B, 0x02)                            # mov rax,[rdx]
        e.test_rr(_RAX, _RAX)
        e.jcc_rel(0x84, self._error_addr)                   # je error (underflow)
        e.sub_ri(_RAX, 1)
        e.emit(0x48, 0x89, 0x02)                            # depth--
        # entry = ex_stack + depth*32
        e.emit(0x48, 0xC1, 0xE0, 0x05)                      # shl rax,5
        e.mov_ri(_RCX, QEMU_LAYOUT["ex_stack"])
        e.add_rr(_RAX, _RCX)                                # rax=entry
        # epc
        e.emit(0x48, 0x8B, 0x18)                            # mov rbx,[rax]
        # priv
        e.emit(0x48, 0x8B, 0x48, 0x18)                      # mov rcx,[rax+24]
        # Restore saved context into the CSR file (RDX must be its base).
        e.mov_ri(_RDX, QEMU_LAYOUT["csrs"])
        e.store_csr(CSR_PRIV, _RCX)
        # ECODE = NONE (0)
        e.mov_ri(_RCX, EXCEPTION_CODES["NONE"])
        e.store_csr(CSR_ECODE, _RCX)
        # EPC = epc
        e.store_csr(CSR_EPC, _RBX)
        # jump to epc
        e.mov_rr(_RAX, _RBX)
        e.mov_ri(_RCX, self.image_base)
        e.sub_rr(_RAX, _RCX)
        e.cmp_ri(_RAX, len(self.image.image))
        e.jcc_rel(0x83, self._error_addr)
        e.emit(0x48, 0xA9)
        e.emit_bytes(_imm32(3))
        e.jcc_rel(0x85, self._error_addr)
        e.jmp_rel(self._dispatch_addr)

    def _emit_dispatch_target(self, target: int) -> None:
        e = self.code
        e.mov_ri(_RAX, target)
        e.sub_ri(_RAX, self.image_base)
        e.jmp_rel(self._dispatch_addr)

    def _emit_indirect_jump(self, reg_index: int) -> None:
        e = self.code
        e.load_reg(_RAX, reg_index)
        e.mov_ri(_RCX, self.image_base)
        e.sub_rr(_RAX, _RCX)
        e.cmp_ri(_RAX, len(self.image.image))
        e.jcc_rel(0x83, self._error_addr)                   # jae error
        e.emit(0x48, 0xA9)                                  # test rax,3
        e.emit_bytes(_imm32(3))
        e.jcc_rel(0x85, self._error_addr)                   # jne error
        e.jmp_rel(self._dispatch_addr)

    def _emit_counter(self) -> None:
        e = self.code
        e.mov_ri(_RDX, QEMU_LAYOUT["steps"])
        e.emit(0x48, 0x8B, 0x12)                            # mov rdx,[rdx]
        e.add_ri(_RDX, 1)
        e.mov_ri(_RAX, QEMU_LAYOUT["steps"])
        e.emit(0x48, 0x89, 0x10)                            # mov [rax],rdx

    def _emit_binary_reg(self, rd: int, rs1: int, rs2: int, kind: str) -> None:
        e = self.code
        e.load_reg(_RAX, rs1)
        e.load_reg(_RCX, rs2)
        if kind == "add":
            e.add_rr(_RAX, _RCX)
        elif kind == "sub":
            e.sub_rr(_RAX, _RCX)
        elif kind == "mul":
            e.imul_rr(_RAX, _RCX)
        elif kind == "and":
            e.and_rr(_RAX, _RCX)
        elif kind == "or":
            e.or_rr(_RAX, _RCX)
        elif kind == "xor":
            e.xor_rr(_RAX, _RCX)
        elif kind == "div":
            e.test_rr(_RCX, _RCX)
            e.jcc_rel(0x84, self._error_addr)               # je error
            e.emit(0x48, 0x99)                              # cqo
            e.emit(0x48, 0xF7, 0xF9)                        # idiv rcx
        elif kind == "udiv":
            e.test_rr(_RCX, _RCX)
            e.jcc_rel(0x84, self._error_addr)               # je error
            e.emit(0x48, 0x31, 0xD2)                        # xor rdx,rdx
            e.emit(0x48, 0xF7, 0xF1)                        # div rcx
        elif kind == "rem":
            e.test_rr(_RCX, _RCX)
            e.jcc_rel(0x84, self._error_addr)               # je error
            e.emit(0x48, 0x99)                              # cqo
            e.emit(0x48, 0xF7, 0xF9)                        # idiv rcx
            e.mov_rr(_RAX, _RDX)
        elif kind == "urem":
            e.test_rr(_RCX, _RCX)
            e.jcc_rel(0x84, self._error_addr)               # je error
            e.emit(0x48, 0x31, 0xD2)                        # xor rdx,rdx
            e.emit(0x48, 0xF7, 0xF1)                        # div rcx
            e.mov_rr(_RAX, _RDX)
        elif kind == "shl":
            e.and_ri(_RCX, 0x3F)
            e.emit(0x48, 0xD3, 0xE0)                        # shl rax,cl
        elif kind == "shr":
            e.and_ri(_RCX, 0x3F)
            e.emit(0x48, 0xD3, 0xE8)                        # shr rax,cl
        elif kind == "sar":
            e.and_ri(_RCX, 0x3F)
            e.emit(0x48, 0xD3, 0xF8)                        # sar rax,cl
        elif kind == "mov":
            e.mov_rr(_RAX, _RCX)
        else:
            raise QemuTranslationError(f"unknown binary operation {kind}")
        if rd:
            e.store_reg(rd, _RAX)

    def _emit_immediate(self, rd: int, rs1: int, value: int, kind: str) -> None:
        e = self.code
        if kind == "mov":
            if rd:
                e.store_reg_imm(rd, value)
            return
        e.load_reg(_RAX, rs1)
        if kind == "add":
            e.add_ri(_RAX, value)
        elif kind == "sub":
            e.sub_ri(_RAX, value)
        elif kind == "and":
            e.and_ri(_RAX, value)
        elif kind == "or":
            e.or_ri(_RAX, value)
        elif kind == "xor":
            e.xor_ri(_RAX, value)
        elif kind == "shl":
            e.emit(0x48, 0xC1, 0xE0, value & 0x3F)
        elif kind == "shr":
            e.emit(0x48, 0xC1, 0xE8, value & 0x3F)
        elif kind == "sar":
            e.emit(0x48, 0xC1, 0xF8, value & 0x3F)
        else:
            raise QemuTranslationError(f"unknown immediate operation {kind}")
        if rd:
            e.store_reg(rd, _RAX)

    def _emit_csr(self, decoded, read: bool) -> None:
        e = self.code
        index = decoded.csr_index
        e.mov_ri(_RDX, QEMU_LAYOUT["csrs"])
        csr = CSR_TABLE.get(index)

        if read:
            if index >= CSR_COUNT:
                if decoded.register("rd"):
                    e.store_reg_imm(decoded.register("rd"), 0)
                return
            if csr is not None and not csr.readable:
                e.jmp_rel(self._error_addr)
                return
            e.load_csr(_RAX, index)
            if csr is not None and csr.mask != 0xFFFFFFFFFFFFFFFF:
                e.mov_ri(_RCX, csr.mask)
                e.and_rr(_RAX, _RCX)
            if decoded.register("rd"):
                e.store_reg(decoded.register("rd"), _RAX)
            return

        if index >= CSR_COUNT:
            return  # WARL: unimplemented CSR indices discard writes.
        if csr is not None and not csr.writable:
            e.jmp_rel(self._error_addr)
            return
        e.load_reg(_RCX, decoded.register("rs1"))
        if index == 4:  # PRIV: values above machine privilege fault.
            e.cmp_ri(_RCX, 3)
            e.jcc_rel(0x87, self._error_addr)  # ja error
        if csr is not None and csr.align4:
            e.emit(0x48, 0xF7, 0xC1)           # test rcx, 3
            e.emit_bytes(_imm32(3))
            e.jcc_rel(0x85, self._error_addr)  # jne error
        if csr is not None and csr.mask != 0xFFFFFFFFFFFFFFFF:
            e.mov_ri(_RAX, csr.mask)
            e.and_rr(_RCX, _RAX)
        e.store_csr(index, _RCX)

    def _emit_memory(self, rd: int, base: int, disp: int, size: int,
                     store: bool, signed: bool = False) -> None:
        e = self.code
        e.load_reg(_RAX, base)
        e.add_ri(_RAX, disp)
        e.add_ri(_RAX, QEMU_LAYOUT["shadow"])
        if store:
            e.load_reg(_RCX, rd)
            e.store_mem(_RCX, size)
        else:
            e.load_mem(_RCX, size, signed)
            if rd:
                e.store_reg(rd, _RCX)

    def _emit_branch(self, mnemonic: str, a: int, b: int, target: int,
                     fallthrough: int) -> None:
        e = self.code
        e.load_reg(_RAX, a)
        e.load_reg(_RCX, b)
        e.cmp_rr(_RAX, _RCX)
        codes = {
            "BEQ": 0x84, "BNE": 0x85,
            "BLTU": 0x82, "BGEU": 0x83,
            "BLEU": 0x86, "BGTU": 0x87,
            "BLT": 0x8C, "BGE": 0x8D,
            "BLE": 0x8E, "BGT": 0x8F,
        }
        if self.target_index(target) is None:
            e.jmp_rel(self._error_addr)
            return
        # Patch the conditional jump after emitting the fall-through path so
        # forward branches do not depend on block addresses being known yet.
        e.emit(0x0F, codes[mnemonic])
        disp_at = len(e.buf)
        e.emit_bytes(b"\x00\x00\x00\x00")
        self._emit_dispatch_target(fallthrough)
        taken = e.address
        struct.pack_into("<i", e.buf, disp_at,
                         taken - (self.code_base + disp_at + 4))
        self._emit_dispatch_target(target)

    def translate(self) -> tuple[bytes, bytes]:
        # Reserve fixed addresses so error/dispatch targets are known before
        # instruction blocks are emitted.
        self._emit_dispatch()
        self._emit_error()

        entry = self.code.address
        self.code.mov_ri(_R15, QEMU_LAYOUT["regs"])
        if self.target_index(self.image.entry) is None:
            raise QemuTranslationError("kernel entry is outside the image")
        self._emit_dispatch_target(self.image.entry)

        for index in range(self.words):
            address = self.image_base + index * 4
            word = int.from_bytes(
                self.image.image[index * 4:index * 4 + 4], "little")
            decoded = decode_instruction(word)
            self.blocks[index] = self.code.address
            self._emit_counter()
            if decoded is None:
                self.code.mov_ri(_RDI, address)
                self.code.jmp_rel(self._error_addr)
                continue
            try:
                self._emit_instruction(decoded, address)
            except (KeyError, QemuTranslationError):
                self.code.jmp_rel(self._error_addr)

        # Build the indirect-jump table now that all block addresses are known.
        table = bytearray(self.words * 8)
        for index, addr in enumerate(self.blocks):
            struct.pack_into("<Q", table, index * 8, addr)

        harness = bytearray()
        # SeaBIOS looks for the multiboot header inside the loaded kernel data,
        # not only in the ELF file header scanned by QEMU.
        harness.extend(_multiboot_header())
        harness.extend(b"\x00" * 4)
        harness.extend(_long_mode_stub(entry))
        harness.extend(b"\x00" * (QEMU_LAYOUT["pml4"] - QEMU_LAYOUT["harness_base"]
                                  - len(harness)))
        harness.extend(_page_tables())
        harness.extend(b"\x00" * (QEMU_LAYOUT["gdt"] - QEMU_LAYOUT["harness_base"]
                                  - len(harness)))
        harness.extend(_gdt())
        harness.extend(b"\x00" * (QEMU_LAYOUT["regs"] - QEMU_LAYOUT["harness_base"]
                                  - len(harness)))
        harness.extend(b"\x00" * _REGS_SIZE)
        harness.extend(b"\x00" * (QEMU_LAYOUT["csrs"] - QEMU_LAYOUT["harness_base"]
                                  - len(harness)))
        for index in range(CSR_COUNT):
            harness.extend(struct.pack("<Q", CSR_TABLE[index].reset
                                       if index in CSR_TABLE else 0))
        harness.extend(b"\x00" * (QEMU_LAYOUT["table"] - QEMU_LAYOUT["harness_base"]
                                  - len(harness)))
        harness.extend(table)
        return bytes(harness), bytes(self.code.buf)

    def _emit_instruction(self, decoded, address: int) -> None:
        mnemonic = decoded.mnemonic
        e = self.code

        # Keep failing Nova PC in RDI; arithmetic helpers overwrite RAX/RCX.
        e.mov_ri(_RDI, address)
        if mnemonic in {"NOP", "FENCE"}:
            return
        if mnemonic in {"CSRR", "CSRW"}:
            self._emit_csr(decoded, mnemonic == "CSRR")
            return
        if mnemonic == "HALT":
            e.mov_ri(_RAX, _DONE_MAGIC)
            e.mov_ri(_RDX, QEMU_LAYOUT["done"])
            e.emit(0x48, 0x89, 0x02)
            e.hlt()
            return

        if mnemonic == "MOV":
            rd = decoded.register("rd")
            rs1 = decoded.register("rs1")
            self._emit_binary_reg(rd, rs1, rs1, "mov")
            return
        if mnemonic == "NOT":
            rd = decoded.register("rd")
            rs1 = decoded.register("rs1")
            e.load_reg(_RAX, rs1)
            e.emit(0x48, 0xF7, 0xD0)                        # not rax
            if rd:
                e.store_reg(rd, _RAX)
            return

        r_type = {
            "ADD": "add", "SUB": "sub", "MUL": "mul", "DIV": "div", "UDIV": "udiv",
            "REM": "rem", "UREM": "urem",
            "AND": "and", "OR": "or", "XOR": "xor",
            "SHL": "shl", "SHR": "shr", "SAR": "sar",
        }
        if mnemonic in r_type:
            self._emit_binary_reg(
                decoded.register("rd"), decoded.register("rs1"),
                decoded.register("rs2"), r_type[mnemonic])
            return

        i_type = {
            "ADDI": "add", "SUBI": "sub", "MULI": "mul",
            "ANDI": "and", "ORI": "or", "XORI": "xor",
            "SHLI": "shl", "SHRI": "shr", "SARI": "sar",
        }
        if mnemonic in i_type:
            try:
                value = decoded.signed_immediate("simm")
            except KeyError:
                value = decoded.signed_immediate("uimm")
            self._emit_immediate(
                decoded.register("rd"), decoded.register("rs1"),
                value, i_type[mnemonic])
            return
        if mnemonic == "MOVI":
            self._emit_immediate(decoded.register("rd"), 0,
                                 decoded.unsigned_immediate("uimm"), "mov")
            return

        memory = {
            "LDB": (1, True, False), "LDBU": (1, False, False),
            "LDH": (2, True, False), "LDHU": (2, False, False),
            "LDW": (4, True, False), "LDWU": (4, False, False),
            "LDD": (8, False, False),
            "STB": (1, False, True), "STH": (2, False, True),
            "STW": (4, False, True), "STD": (8, False, True),
        }
        if mnemonic in memory:
            size, signed, store = memory[mnemonic]
            self._emit_memory(decoded.register("rd"),
                              decoded.memory_base,
                              decoded.memory_displacement,
                              size, store, signed)
            return

        branches = {"BEQ", "BNE", "BLTU", "BGEU", "BLEU", "BGTU",
                    "BLT", "BGE", "BLE", "BGT"}
        if mnemonic in branches:
            self._emit_branch(
                mnemonic,
                decoded.register("reg_a"), decoded.register("reg_b"),
                address + decoded.target_displacement, address + 4)
            return
        if mnemonic == "CALL":
            e.store_reg_imm(31, address + 4)
            target = address + decoded.target_displacement
            if self.target_index(target) is None:
                e.jmp_rel(self._error_addr)
            else:
                self._emit_dispatch_target(target)
            return
        if mnemonic == "JMP":
            self._emit_indirect_jump(decoded.register("reg_a"))
            return
        if mnemonic == "RET":
            self._emit_indirect_jump(31)
            return
        if mnemonic == "MRET":
            self._emit_exception_return()
            return
        if mnemonic == "TLBI":
            # NPK boot uses TLBI as a TLB barrier in a non-paging runtime.
            # This QEMU shadow has no software TLB; architectural no-op.
            return

        raise QemuTranslationError(f"unsupported instruction {mnemonic}")


def _elf32(phdrs: List[bytes], entry: int) -> bytes:
    ehdr = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\x00" * 9,
        2,       # ET_EXEC
        3,       # EM_386
        1,       # EV_CURRENT
        entry,
        52,      # e_phoff
        0,       # e_shoff
        0,       # e_flags
        52,      # e_ehsize
        32,      # e_phentsize
        len(phdrs),
        0, 0, 0,
    )
    return ehdr + b"".join(phdrs)


def _phdr(offset: int, vaddr: int, size: int, flags: int) -> bytes:
    return struct.pack("<IIIIIIII", 1, offset, vaddr, vaddr,
                       size, size, flags, 0x1000)


def build_qemu_elf(image: NobImage, output: str | Path) -> Path:
    """Write a multiboot ELF containing the Nova image and its x86-64 host."""
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)

    harness, translated_code = _NovaTranslator(image).translate()
    elf = bytearray(0x1000)
    elf[0x200:0x200 + 12] = _multiboot_header()

    shadow_size = QEMU_LAYOUT["shadow_size"]
    if len(image.image) > shadow_size:
        raise QemuTranslationError("Nova image is too large for the QEMU shadow")
    nova_offset = 0x1000
    elf.extend(b"\x00" * (nova_offset - len(elf)))
    elf.extend(image.image)
    elf.extend(b"\x00" * (shadow_size - len(image.image)))
    harness_offset = (nova_offset + shadow_size + 0xFFF) & ~0xFFF
    elf.extend(b"\x00" * (harness_offset - len(elf)))
    elf.extend(harness)
    code_offset = (len(elf) + 0xFFF) & ~0xFFF
    elf.extend(b"\x00" * (code_offset - len(elf)))
    elf.extend(translated_code)

    phdrs = [
        _phdr(nova_offset, QEMU_LAYOUT["shadow"], shadow_size, 6),
        _phdr(harness_offset, QEMU_LAYOUT["harness_base"], len(harness), 5),
        _phdr(code_offset, QEMU_LAYOUT["code"], len(translated_code), 5),
    ]
    header = _elf32(phdrs, QEMU_LAYOUT["entry"])
    elf[0:len(header)] = header
    out.write_bytes(elf)
    return out
