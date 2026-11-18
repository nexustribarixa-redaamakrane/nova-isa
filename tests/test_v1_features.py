"""Conformance tests for the Nova ISA v1.0 architectural features.

These cover the areas that turned the v0.3 experimental draft into a real
architecture: the single 64-bit profile, virtual memory, atomics, interrupts
and multicore identity, virtualization, the finalized object model, and the
new arithmetic instructions.
"""

import pytest

from nova import NovaCluster, NovaMachine, objfmt
from nova.assembler import assemble_text
from nova.isa import (
    ADDRESS_BITS,
    CSR_TABLE,
    EXCEPTION_CODES,
    EXECUTION_MODES,
    INT_SOFTWARE,
    INT_TIMER,
    MMU_DETAIL_MISALIGNED_PTE,
    OBJECT_FLAG_EXTERNAL,
    OBJECT_MAGIC,
    OBJECT_PERM_DROP,
    OBJECT_PERM_EXEC,
    OBJECT_PERM_GRANT,
    OBJECT_PERM_META,
    OBJECT_PERM_READ,
    OBJECT_PERM_WRITE,
    PAGE_SIZE,
    PHYSICAL_ADDRESS_BITS,
    PROFILE,
    WORD_BITS,
    ObjectFormatError,
    make_object_reference,
    split_object_reference,
)

USER = 0
MACHINE = 2


def machine_for(source, entry=0x1000):
    machine = NovaMachine(assemble_text(source), entry=entry)
    machine.pc = entry
    machine.priv = MACHINE
    return machine


# --------------------------------------------------------------------------
# 64-bit-only profile
# --------------------------------------------------------------------------


def test_profile_declares_one_64_bit_mode():
    assert EXECUTION_MODES == ("Nova64",)
    assert PROFILE["execution_modes"] == EXECUTION_MODES
    for key in ("word_bits", "address_bits", "gpr_bits", "csr_bits",
                "reference_bits"):
        assert PROFILE[key] == WORD_BITS == 64
    assert ADDRESS_BITS == 64
    assert PHYSICAL_ADDRESS_BITS == 52
    assert PAGE_SIZE == 4096


def test_full_64_bit_values_round_trip_through_registers_and_memory():
    machine = NovaMachine()
    all_ones = (1 << 64) - 1
    machine.write_register("r1", all_ones)
    assert machine.read_register("r1") == all_ones
    machine._write_mem(0x8000, all_ones, 8)
    assert machine._read_mem(0x8000, 8) == all_ones


# --------------------------------------------------------------------------
# Virtual memory
# --------------------------------------------------------------------------


def install_page_tables(machine, va, pa, *, flags=0x1F, root=0x10000):
    """Build a four-level table mapping one virtual page to one physical page."""
    levels = [root, root + 0x1000, root + 0x2000, root + 0x3000]
    for level, table in enumerate(levels):
        index = (va >> (48 - 9 * (level + 1))) & 0x1FF
        pte_address = table + index * 8
        if level < 3:
            machine._write_mem(pte_address, (levels[level + 1] >> 12) << 12 | 1, 8)
        else:
            machine._write_mem(pte_address, ((pa >> 12) << 12) | 1 | flags, 8)
    return (1 << 63) | root


def install_huge_page(machine, va, pa, level, *, flags=0x1F, root=0x10000):
    """Install a leaf at ``level``: 1 = 1 GiB, 2 = 2 MiB, 3 = 4 KiB."""
    tables = [root + 0x1000 * index for index in range(level + 1)]
    for current in range(level):
        index = (va >> (48 - 9 * (current + 1))) & 0x1FF
        machine._write_mem(tables[current] + index * 8,
                           (tables[current + 1] >> 12) << 12 | 1, 8)
    leaf_index = (va >> (48 - 9 * (level + 1))) & 0x1FF
    machine._write_mem(tables[level] + leaf_index * 8,
                       ((pa >> 12) << 12) | 1 | flags, 8)
    return (1 << 63) | root


def test_huge_page_translations():
    machine = NovaMachine()
    machine.write_csr("ptbr", install_huge_page(machine, 0x40000000,
                                                0x50000000, 2))
    assert machine._translate(0x40000000, 8, "r") == 0x50000000
    assert machine._translate(0x40123456, 8, "r") == 0x50123456
    assert machine._translate(0x401FFFFF, 1, "r") == 0x501FFFFF

    machine = NovaMachine()
    machine.write_csr("ptbr", install_huge_page(machine, 0x40000000,
                                                0x80000000, 1))
    assert machine._translate(0x40000000, 8, "r") == 0x80000000
    assert machine._translate(0x40123456, 8, "r") == 0x80123456
    assert machine._translate(0x4FFFFFFF, 1, "r") == 0x8FFFFFFF


def test_misaligned_huge_page_physical_address_faults():
    machine = NovaMachine()
    # 0x50001000 is not 2 MiB aligned, so it cannot back a 2 MiB leaf.
    machine.write_csr("ptbr", install_huge_page(machine, 0x40000000,
                                                0x50001000, 2))
    with pytest.raises(Exception) as caught:
        machine._translate(0x40000000, 8, "r")
    assert caught.value.detail == MMU_DETAIL_MISALIGNED_PTE


def test_paged_translation_maps_virtual_to_physical():
    machine = NovaMachine()
    ptbr = install_page_tables(machine, 0x40000000, 0x50000000)
    machine.write_csr("ptbr", ptbr)
    assert machine._translate(0x40000000, 8, "r") == 0x50000000
    assert machine._translate(0x40000ABC, 4, "w") == 0x50000ABC


def test_paged_instruction_fetch_and_data_access():
    code = assemble_text("MOVI r1, 42\nADDI r2, r1, 1\nHALT\n")
    machine = NovaMachine()
    machine.load_code(code, 0x50000000)
    ptbr = install_page_tables(machine, 0x40000000, 0x50000000)
    machine.write_csr("ptbr", ptbr)
    machine.pc = 0x40000000
    machine.priv = MACHINE
    machine.run(3)
    assert machine.halted
    assert machine.read_register(1) == 42
    assert machine.read_register(2) == 43


def test_permission_fault_reports_tval_and_detail():
    code = assemble_text("MOVI r1, 7\nSTD r1, [r2]\nHALT\n")
    machine = NovaMachine()
    machine.load_code(code, 0x50000000)
    machine.pc = 0x40000000
    machine.priv = MACHINE
    machine.write_register("r2", 0x40000100)
    # Read/execute/user but not writable.
    ptbr = install_page_tables(machine, 0x40000000, 0x50000000, flags=0x1B)
    machine.write_csr("ptbr", ptbr)
    machine.run(3)  # stop at the first fault
    assert machine.exception_code == EXCEPTION_CODES["MMU_FAULT"]
    assert machine.read_csr("tval") == 0x40000100
    assert machine.ecause == 1


def test_noncanonical_address_raises_mmu_fault():
    machine = NovaMachine()
    machine.write_csr("ptbr", (1 << 63) | 0x10000)
    with pytest.raises(Exception):
        machine._translate(0x0000800000000000, 8, "r")


def test_tlbi_flushes_cached_translations():
    machine = NovaMachine()
    ptbr = install_page_tables(machine, 0x40000000, 0x50000000)
    machine.write_csr("ptbr", ptbr)
    assert machine._translate(0x40000000, 8, "r") == 0x50000000
    # Invalidate the leaf PTE and flush: the next translation must fault.
    machine._write_mem(0x13000, 0, 8)
    machine.flush_tlb(None)
    with pytest.raises(Exception):
        machine._translate(0x40000000, 8, "r")


# --------------------------------------------------------------------------
# Atomic operations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mnemonic,initial,operand,expected_mem",
    [
        ("AMOADD", 10, 5, 15),
        ("AMOSWAP", 10, 5, 5),
        ("AMOAND", 0b1100, 0b1010, 0b1000),
        ("AMOOR", 0b1100, 0b1010, 0b1110),
        ("AMOXOR", 0b1100, 0b1010, 0b0110),
    ],
)
def test_atomic_operations_return_old_value_and_update_memory(
        mnemonic, initial, operand, expected_mem):
    machine = machine_for(
        f"MOVI r1, 0x2000\nMOVI r2, {operand}\n"
        f"{mnemonic} r3, r1, r2, 1, 1\nHALT\n"
    )
    machine._write_mem(0x2000, initial, 8)
    machine.run(4)
    assert machine.read_register("r3") == initial
    assert machine._read_mem(0x2000, 8) == expected_mem
    assert machine.last_amo_ordering == (1, 1)


def test_atomic_compare_and_swap_success_and_failure():
    machine = machine_for(
        "MOVI r1, 0x2000\nMOVI r4, 10\nMOVI r2, 99\n"
        "AMOCAS r4, r1, r2, 0, 0\nHALT\n"
    )
    machine._write_mem(0x2000, 10, 8)
    machine.run(5)
    assert machine.read_register("r4") == 10
    assert machine._read_mem(0x2000, 8) == 99

    failure = machine_for(
        "MOVI r1, 0x2000\nMOVI r4, 7\nMOVI r2, 99\n"
        "AMOCAS r4, r1, r2, 0, 0\nHALT\n"
    )
    failure._write_mem(0x2000, 10, 8)
    failure.run(5)
    assert failure.read_register("r4") == 10
    assert failure._read_mem(0x2000, 8) == 10


def test_atomic_requires_eight_byte_alignment():
    machine = machine_for("MOVI r1, 0x2004\nMOVI r2, 1\nAMOADD r3, r1, r2, 0, 0\n")
    machine.run(3, stop_on_exception=False)
    assert machine.exception_code == EXCEPTION_CODES["ALIGNMENT_FAULT"]


# --------------------------------------------------------------------------
# Interrupts and multicore
# --------------------------------------------------------------------------


def test_timer_interrupt_delivers_to_ivec_and_iret_returns():
    machine = machine_for(
        "MOVI r1, 1\nMOVI r2, 2\nHALT\n", entry=0x1000
    )
    machine.load_code(assemble_text("IRET\n"), 0x2000)
    machine.pc = 0x1000
    machine.write_csr("timecmp", 2)
    machine.write_csr("ie", 1 << INT_TIMER)

    machine.step()  # time = 1
    assert machine.exception_code == EXCEPTION_CODES["NONE"]
    machine.step()  # time = 2, interrupt taken
    assert machine.exception_code == EXCEPTION_CODES["INTERRUPT"]
    assert machine.ecause == INT_TIMER
    assert machine.pc == machine.read_csr("ivec")
    assert machine.epc == 0x1004
    machine.step()  # IRET
    assert machine.pc == 0x1004
    assert machine.exception_code == EXCEPTION_CODES["NONE"]


def test_wait_stops_without_interrupt_and_interrupt_is_taken_before_it():
    idle = machine_for("WAIT\nHALT\n")
    idle.run(1, stop_on_exception=False)
    assert idle.halted
    assert idle.pc == 0x1000

    awake = machine_for("WAIT\nHALT\n")
    awake.load_code(assemble_text("IRET\n"), 0x2000)
    awake.pc = 0x1000
    awake.set_interrupt_pending(INT_TIMER)
    awake.write_csr("ie", 1 << INT_TIMER)
    awake.step()  # enabled interrupt is delivered before WAIT executes
    assert awake.pc == awake.read_csr("ivec")
    awake.step()  # IRET returns to the WAIT instruction
    assert awake.pc == 0x1000
    awake.step()  # no interrupt pending now, so WAIT stops
    assert awake.halted


def test_cluster_gives_unique_ids_shared_memory_and_ipi():
    cluster = NovaCluster(2)
    assert [hart.coreid for hart in cluster.harts] == [0, 1]
    cluster.harts[0]._write_mem(0x9000, 77, 8)
    assert cluster.harts[1]._read_mem(0x9000, 8) == 77

    cluster.harts[1].write_csr("ie", 1 << INT_SOFTWARE)
    cluster.send_ipi(0, 1)
    assert cluster.harts[1].read_csr("ip") & (1 << INT_SOFTWARE)
    cluster.step(1)
    assert cluster.harts[1].exception_code == EXCEPTION_CODES["INTERRUPT"]
    assert cluster.harts[1].ecause == INT_SOFTWARE


# --------------------------------------------------------------------------
# Virtualization
# --------------------------------------------------------------------------


def test_vmlaunch_guest_execution_and_explicit_vmexit():
    host = (
        "MOVI r1, 0x3000\nCSRW vpc, r1\n"
        "MOVI r1, 2\nCSRW vpriv, r1\n"
        "VMLAUNCH\nMOVI r6, 9\nHALT\n"
    )
    machine = machine_for(host)
    machine.load_code(assemble_text("MOVI r5, 7\nVMEXIT 1\n"), 0x3000)
    machine.pc = 0x1000
    machine.run(9, stop_on_exception=False)
    assert machine.read_register("r5") == 7
    assert machine.read_register("r6") == 9
    assert machine.read_csr("vexit") == 1
    assert machine.read_csr("hstatus") == 0


def test_guest_trap_causes_vm_exit_and_host_resumes():
    machine = machine_for("MOVI r1, 0x3000\nCSRW vpc, r1\nVMLAUNCH\nHALT\n")
    machine.load_code(assemble_text("SCALL 3\nHALT\n"), 0x3000)
    machine.pc = 0x1000
    machine.run(5, stop_on_exception=False)
    assert machine.read_csr("vexit") == EXCEPTION_CODES["SYSTEM_CALL"]
    assert machine.read_csr("hstatus") == 0


# --------------------------------------------------------------------------
# Object capabilities
# --------------------------------------------------------------------------


def test_object_capabilities_permissions_comparison_and_faults():
    machine = machine_for(
        "MOVI r1, 24\nOBJ.CREATE r2, r1\n"
        "OBJ.PERM r3, r2\nOBJ.GET r4, r2\n"
        "OBJ.CMP r5, r2, r2\nOBJ.DROP r2\nOBJ.GET r6, r2\nHALT\n"
    )
    machine.run(7, stop_on_exception=False)
    assert machine.read_register("r2") == make_object_reference(1, 1)
    expected = (
        OBJECT_PERM_READ | OBJECT_PERM_WRITE | OBJECT_PERM_EXEC
        | OBJECT_PERM_GRANT | OBJECT_PERM_DROP | OBJECT_PERM_META
    )
    assert machine.read_register("r3") == expected
    assert machine.read_register("r4") == 24
    assert machine.read_register("r5") == 1
    assert machine.exception_code == EXCEPTION_CODES["OBJECT_FAULT"]
    assert machine.ecause == 1  # invalid reference after DROP


def test_null_object_reference_reports_null_detail():
    machine = machine_for("OBJ.GET r1, r0\n")
    machine.run(1, stop_on_exception=False)
    assert machine.exception_code == EXCEPTION_CODES["OBJECT_FAULT"]
    assert machine.ecause == 0


# --------------------------------------------------------------------------
# New arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("MOVI r1, 1\nMOVI r3, 1\nROTL r2, r1, r3\n", 2),
        ("MOVI r1, 3\nROTRI r2, r1, 1\n", (1 << 63) | 1),
        ("MOVI r1, 100\nMOVI r3, 7\nUDIV r2, r1, r3\n", 14),
        ("MOVI r1, 100\nMOVI r3, 7\nUREM r2, r1, r3\n", 2),
        ("MOVI r1, 1\nSHLI r1, r1, 63\nROTLI r2, r1, 1\n", 1),
    ],
)
def test_new_arithmetic_instructions(source, expected):
    machine = machine_for(source)
    machine.run(3, stop_on_exception=False)
    assert machine.read_register("r2") == expected


def test_division_by_zero_uses_distinct_details():
    divide = machine_for("MOVI r1, 5\nUDIV r2, r1, r0\n")
    divide.run(2, stop_on_exception=False)
    assert divide.exception_code == EXCEPTION_CODES["ARITHMETIC"]
    assert divide.ecause == 0

    remainder = machine_for("MOVI r1, 5\nUREM r2, r1, r0\n")
    remainder.run(2, stop_on_exception=False)
    assert remainder.ecause == 1


# --------------------------------------------------------------------------
# Stage-2 virtualization
# --------------------------------------------------------------------------


def install_stage1_through_stage2(machine, va, ipa, root_ipa=0x10000):
    """Write guest stage-1 tables into host memory via stage-2 translation."""
    levels = [root_ipa + 0x1000 * index for index in range(4)]
    for level in range(3):
        index = (va >> (48 - 9 * (level + 1))) & 0x1FF
        ipa_address = levels[level] + index * 8
        physical = machine._translate_stage2(ipa_address, 8, "w", va)
        machine._write_mem(physical, (levels[level + 1] >> 12) << 12 | 1, 8)
    index = (va >> 12) & 0x1FF
    ipa_address = levels[3] + index * 8
    physical = machine._translate_stage2(ipa_address, 8, "w", va)
    machine._write_mem(physical, ((ipa >> 12) << 12) | 0x1F, 8)
    return (1 << 63) | root_ipa


def test_stage2_translation_composes_with_stage1():
    machine = NovaMachine()
    # Stage 2 maps guest-physical 0..1 GiB to host-physical +0x80000000.
    machine.write_csr("s2ptbr", install_huge_page(machine, 0, 0x80000000, 1,
                                                  root=0x20000))
    stage1 = install_stage1_through_stage2(machine, 0x20000000, 0x30000000)
    machine.write_csr("vptbr", stage1)
    machine.write_csr("ptbr", stage1)
    machine.write_csr("hstatus", 1)

    assert machine._translate(0x20000000, 8, "r") == 0xB0000000
    assert machine._translate(0x20000123, 8, "r") == 0xB0000123


def test_stage2_permission_denial_faults_with_guest_va():
    machine = NovaMachine()
    machine.write_csr("s2ptbr", install_huge_page(machine, 0, 0x80000000, 1,
                                                  root=0x20000))
    stage1 = install_stage1_through_stage2(machine, 0x20000000, 0x30000000)
    machine.write_csr("vptbr", stage1)
    machine.write_csr("ptbr", stage1)
    machine.write_csr("hstatus", 1)
    # Remove read permission from the stage-2 leaf (level 1, entry 0).
    machine._write_mem(0x21000, ((0x80000000 >> 12) << 12) | 0x19, 8)
    machine.flush_tlb(None)
    with pytest.raises(Exception) as caught:
        machine._translate(0x20000123, 8, "r")
    assert caught.value.code == EXCEPTION_CODES["MMU_FAULT"]
    assert machine.read_csr("tval") == 0x20000123


def test_guest_execution_uses_two_dimensional_translation():
    host = (
        "LI r1, 0x40000000\nCSRW vpc, r1\n"
        "VMLAUNCH\nMOVI r4, 9\nHALT\n"
    )
    machine = machine_for(host)
    machine.load_code(assemble_text("MOVI r5, 7\nVMEXIT 1\n"), 0x30000000)
    machine.pc = 0x1000

    # Identity stage 2, so stage-1 tables at IPA 0x10000 live at PA 0x10000.
    machine.write_csr("s2ptbr", install_huge_page(machine, 0, 0, 1,
                                                  root=0x20000))
    stage1 = install_stage1_through_stage2(machine, 0x40000000, 0x30000000)
    machine.write_csr("vptbr", stage1)
    machine.write_csr("vpriv", 2)
    machine.run(11, stop_on_exception=False)
    assert machine.read_register("r5") == 7
    assert machine.read_register("r4") == 9
    assert machine.read_csr("vexit") == 1
    assert machine.read_csr("hstatus") == 0


# --------------------------------------------------------------------------
# Stable object identities, serialization, and versioning
# --------------------------------------------------------------------------


def test_reference_layout_round_trips_and_rejects_malformed_values():
    reference = make_object_reference(0x12345678, 0x9ABCDEF0)
    assert split_object_reference(reference) == (0x12345678, 0x9ABCDEF0)
    for bad in (0, 1, 1 << 32, 0x10000000000000000):
        with pytest.raises(ObjectFormatError):
            split_object_reference(bad)


def test_export_import_preserves_references_and_obj_instruction_semantics():
    source = NovaMachine()
    first = source._create_object(24)
    second = source._create_object(99)
    payload = source.export_objects()
    assert payload == source.export_objects()  # deterministic

    target = NovaMachine()
    assert target.import_objects(payload) == 2
    assert set(target.objects) == {first, second}
    assert target._object_field(first)["size"] == 24
    assert target._object_field(second)["size"] == 99

    # OBJ.GET works on the imported reference without renumbering it.
    program = machine_for(f"LI r1, {first}\nOBJ.GET r2, r1\nHALT\n")
    program.objects = dict(target.objects)
    program.object_generation = target.object_generation
    program.next_obj_id = target.next_obj_id
    program.run(12, stop_on_exception=False)
    assert program.read_register("r2") == 24


def test_stale_reference_cannot_resolve_to_a_new_object():
    machine = NovaMachine()
    old = machine._create_object(7)
    machine._drop_object(old)
    new = machine._create_object(8)
    assert new != old
    stale = machine_for(f"LI r1, {old}\nOBJ.GET r2, r1\nHALT\n")
    stale.objects = dict(machine.objects)
    stale.run(12, stop_on_exception=False)
    assert stale.exception_code == EXCEPTION_CODES["OBJECT_FAULT"]
    assert stale.ecause == 1  # invalid, not a successful alias


def test_import_duplicate_policies_and_validation():
    machine = NovaMachine()
    reference = machine._create_object(5)
    payload = machine.export_objects()
    with pytest.raises(ObjectFormatError, match="duplicate import"):
        machine.import_objects(payload)
    assert machine.import_objects(payload, on_duplicate="skip") == 0
    assert machine.import_objects(payload, on_duplicate="replace") == 1
    with pytest.raises(ValueError):
        machine.import_objects(payload, on_duplicate="bogus")
    assert reference in machine.objects


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda data: b"XXXX" + data[4:], "magic"),
        (lambda data: data[:4] + bytes([9]) + data[5:], "version"),
        (lambda data: data[:-1], "length"),
        (lambda data: data[:-8] + bytes([data[-8] ^ 0xFF]) + data[-7:], "checksum"),
    ],
)
def test_import_rejects_malformed_containers(mutate, message):
    machine = NovaMachine()
    machine._create_object(3)
    with pytest.raises(ObjectFormatError, match=message):
        machine.import_objects(mutate(machine.export_objects()))


def test_import_rejects_duplicate_and_invalid_records():
    machine = NovaMachine()
    ref = machine._create_object(3)
    with pytest.raises(ObjectFormatError, match="duplicate object reference"):
        objfmt.encode_objects([
            {"reference": ref, "size": 1, "permissions": 1, "flags": 0},
            {"reference": ref, "size": 2, "permissions": 1, "flags": 0},
        ])
    with pytest.raises(ObjectFormatError, match="Malformed object reference"):
        objfmt.encode_objects([
            {"reference": 1 << 32, "size": 1,
             "permissions": 1, "flags": 0},
        ])
    with pytest.raises(ObjectFormatError, match="unknown permissions"):
        objfmt.encode_objects([
            {"reference": ref, "size": 1, "permissions": 1 << 20, "flags": 0},
        ])


def test_external_references_validate_resolution_and_consistency():
    owner = NovaMachine()
    reference = owner._create_object(42)
    payload = owner.export_objects()

    importer = NovaMachine()
    importer.import_objects(payload)

    external = objfmt.encode_objects([
        {"reference": reference, "size": 42, "permissions": 0x3F,
         "flags": OBJECT_FLAG_EXTERNAL},
        {"reference": make_object_reference(99, 99), "size": 1,
         "permissions": 0x3F, "flags": 0},
    ])
    assert importer.import_objects(external) == 1
    assert len(importer.objects) == 2

    stranger = NovaMachine()
    with pytest.raises(ObjectFormatError, match="unresolved external"):
        stranger.import_objects(external)

    inconsistent = objfmt.encode_objects([
        {"reference": reference, "size": 43, "permissions": 0x3F,
         "flags": OBJECT_FLAG_EXTERNAL},
    ])
    with pytest.raises(ObjectFormatError, match="inconsistent external"):
        importer.import_objects(inconsistent)


def test_generation_seed_prevents_cross_machine_collisions():
    first = NovaMachine(object_generation_seed=100)
    second = NovaMachine(object_generation_seed=200)
    a = first._create_object(1)
    b = second._create_object(2)
    assert split_object_reference(a)[0] == 100
    assert split_object_reference(b)[0] == 200
    assert a != b

    merged = NovaMachine()
    assert merged.import_objects(first.export_objects()) == 1
    assert merged.import_objects(second.export_objects()) == 1
    fresh = merged._create_object(3)
    assert fresh not in (a, b)
