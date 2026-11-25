"""Host-side checks for the generated QEMU boot harness.

QEMU has no Nova target. These tests verify that the generated ELF carries the
real ``.nob`` image into the documented shadow and that the x86-64 interpreter
emits the correctness-critical sequences found during boot validation. Running
these tests does not require QEMU.
"""

from __future__ import annotations

import struct

from nova import npk
from nova.nob import NobImage
from nova.npk import qemu_translator as qt


def _elf32_phdrs(data: bytes) -> list[tuple[int, int, int, int]]:
    assert data[:6] == b"\x7fELF\x01\x01"
    phoff = struct.unpack_from("<I", data, 28)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    assert phentsize == 32
    result = []
    for index in range(phnum):
        offset = phoff + index * phentsize
        p_type, p_offset, p_vaddr, _p_paddr, p_filesz, _p_memsz, _flags, _align = (
            struct.unpack_from("<IIIIIIII", data, offset))
        assert p_type == 1  # PT_LOAD
        result.append((p_offset, p_vaddr, p_filesz, _flags))
    return result


def test_qemu_layout_keeps_shadow_host_and_observation_regions_apart():
    layout = qt.QEMU_LAYOUT
    assert layout["shadow"] + layout["shadow_size"] == 0x200000
    assert layout["code"] > layout["shadow"] + layout["shadow_size"]
    assert layout["harness_base"] < layout["table"] < layout["code"]
    assert layout["done"] > layout["shadow"] + layout["shadow_size"]
    # The table grows with the image; even a maximum-size image must not reach
    # the step/done/error metadata used by the harness and boot validator.
    max_table_end = layout["table"] + (layout["shadow_size"] // 4) * 8
    assert max_table_end <= layout["done"]


def test_generated_elf_loads_real_image_into_nova_shadow(tmp_path):
    kernel = npk.build_kernel(tmp_path / "kernel")
    image = npk.load_kernel_image(kernel)
    elf_path = qt.build_qemu_elf(image, tmp_path / "npk_qemu.elf")
    data = elf_path.read_bytes()

    entry = struct.unpack_from("<I", data, 24)[0]
    assert entry == qt.QEMU_LAYOUT["entry"]
    phdrs = _elf32_phdrs(data)
    assert len(phdrs) == 3

    nova_offset, nova_vaddr, nova_size, _ = phdrs[0]
    assert nova_vaddr == qt.QEMU_LAYOUT["shadow"]
    assert nova_size == qt.QEMU_LAYOUT["shadow_size"]
    assert data[nova_offset:nova_offset + len(image.image)] == image.image

    assert phdrs[1][1] == qt.QEMU_LAYOUT["harness_base"]
    assert phdrs[2][1] == qt.QEMU_LAYOUT["code"]
    # The real multiboot header is inside loaded data, not just the ELF file.
    assert struct.pack("<III", 0x1BADB002, 0, (-0x1BADB002) & 0xFFFFFFFF) in data


def test_translator_emits_correct_imul_and_csr_init():
    # MOVI r9,8; MUL r8,r8,r9; CSRW vbar,r1; HALT. The MUL word exposed an
    # operand-order bug in the x86 emitter; the CSRW word checks that the CSR
    # file is modelled instead of silently treated as a NOP.
    words = [0x2A480008, 0x12421200, 0x81014200, 0x64000000]
    image = NobImage(
        image_base=0x1000,
        entry=0x1000,
        image=b"".join(struct.pack("<I", word) for word in words),
    )
    harness, code = qt._NovaTranslator(image).translate()

    # IMUL rax,rcx: opcode 0F AF with reg=RAX and r/m=RCX.
    assert b"\x48\x0f\xaf\xc1" in code
    # CSRW index 5 (vbar): mov [rdx+0x28],rcx after loading the CSR base.
    assert b"\x48\x89\x8a\x28\x00\x00\x00" in code

    csr_offset = qt.QEMU_LAYOUT["csrs"] - qt.QEMU_LAYOUT["harness_base"]
    vbar_reset = struct.unpack_from("<Q", harness, csr_offset + 5 * 8)[0]
    assert vbar_reset == 0x1000


def _translate_words(words, image_base=0x1000):
    image = NobImage(
        image_base=image_base,
        entry=image_base,
        image=b"".join(struct.pack("<I", word) for word in words),
    )
    return qt._NovaTranslator(image).translate()


def test_translator_supports_m8_integer_and_control_flow_instructions():
    # UDIV r4,r5,r6; REM r7,r8,r9; UREM r10,r11,r12; NOT r13,r14; RET; MRET; TLBI r0; HALT
    words = [
        0x03214C00,  # UDIV
        0x143A1200,  # REM
        0x0452D800,  # UREM
        0x1D6B8000,  # NOT
        0x5C000000,  # RET
        0x61000000,  # MRET
        0x2D000000,  # TLBI
        0x64000000,  # HALT
    ]
    harness, code = _translate_words(words)
    assert b"\x48\xf7\xf1" in code      # div rcx (UDIV/UREM)
    assert b"\x48\xf7\xf9" in code      # idiv rcx (DIV/REM)
    assert b"\x48\xf7\xd0" in code      # not rax
    assert qt.QEMU_LAYOUT["ex_depth"] < qt.QEMU_LAYOUT["ex_stack"]
    # MRET path reads/writes hidden exception stack and restores EPC/ECODE/PRIV.
    assert struct.pack("<I", qt.QEMU_LAYOUT["ex_depth"]) in code
    assert struct.pack("<I", qt.QEMU_LAYOUT["ex_stack"]) in code
    # No translation error: harness contains jump table entries for each word.
    table_off = qt.QEMU_LAYOUT["table"] - qt.QEMU_LAYOUT["harness_base"]
    assert len(harness) >= table_off + len(words) * 8


def test_translator_mret_loads_csr_base_before_csr_access():
    # MRET restores PRIV/ECODE/EPC through load_csr/store_csr, which index the
    # CSR file from RDX. The emitted path must load that base before every CSR
    # access or the restored state lands in unrelated harness data.
    _harness, code = _translate_words([0x61000000, 0x64000000])  # MRET; HALT
    mov_rdx_csr_base = b"\x48\xba" + struct.pack("<Q", qt.QEMU_LAYOUT["csrs"])
    assert code.count(mov_rdx_csr_base) >= 2
    # The error handler stores the failing PC held in RDI.
    assert b"\x48\x89\x3a" in code  # mov [rdx],rdi


def test_translator_undecodable_word_reports_its_own_pc():
    # An undecodable word branches to the shared error handler before any
    # instruction emitter runs, so the translator must carry that word's PC.
    address = 0x2000
    _harness, code = _translate_words([0xFFFFFFFF], image_base=address)
    assert b"\x48\xbf" + struct.pack("<Q", address) in code  # mov rdi,imm64


def test_qemu_error_handler_reports_failing_pc():
    layout = qt.QEMU_LAYOUT
    assert "error_pc" in layout
    assert layout["error_pc"] > layout["steps"]
    assert layout["error_word"] > layout["error_pc"]
