"""Milestone 4 physical and virtual memory processing tests.

Every memory operation is admitted through the real post-boot kernel entry and
runs the shared five-stage core. Tests validate the receipt jointly with the
region descriptors, page tables, raw memory bytes, and translator behavior; a
successful response alone is never treated as evidence.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.assembler import assemble_text
from nova.npk import core as npk_core
from nova.isa import EXCEPTION_CODES, MMU_DETAIL_MISSING, MMU_DETAIL_PERMISSION

OWNER = 7
OTHER_OWNER = 8
MAP_VA = 0x8000000000  # canonical, and in L0 index 1 so low memory can be identity-mapped
CODE_ADDRESS = 0xA0000
USER = 0

ALLOC_FLAGS = npk.REGION_TYPE_NORMAL
RWXU = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC | npk.PERM_USER
RWX = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC
RU = npk.PERM_READ | npk.PERM_USER
WITHOUT_READ = npk.PERM_WRITE | npk.PERM_USER


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(npk.build_kernel(tmp_path_factory.mktemp("npk-m4")))


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def call(machine, operation, operation_id, **kwargs):
    response = npk.call_memory_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        owner=kwargs.pop("owner", OWNER),
        **kwargs,
    )
    receipts = npk.decode_memory_receipts(machine)
    receipt = receipts[response.receipt_index]
    return response, receipt


def call_complete(machine, operation, operation_id, *, result, **kwargs):
    response, receipt = call(machine, operation, operation_id, **kwargs)
    npk.validate_memory_response(
        response, status=npk.REQUEST_OK, operation=operation, result=result
    )
    npk.validate_complete_memory_receipt(receipt)
    return response, receipt


def call_rejected(machine, operation, operation_id, *, reason, **kwargs):
    response, receipt = call(machine, operation, operation_id, **kwargs)
    npk.validate_memory_response(
        response, status=npk.REQUEST_REJECTED, operation=operation, reason=reason
    )
    npk.validate_rejected_memory_receipt(receipt)
    return response, receipt


def call_failed(machine, operation, operation_id, *, reason, stage, **kwargs):
    response, receipt = call(machine, operation, operation_id, **kwargs)
    npk.validate_memory_response(
        response, status=npk.REQUEST_FAILED, operation=operation, reason=reason
    )
    npk.validate_failed_memory_receipt(receipt, stage=stage)
    return response, receipt


def allocate(machine, operation_id, *, owner=OWNER, size=npk.MEMORY_PAGE_SIZE,
             align=npk.MEMORY_PAGE_SIZE):
    # The result is part of the state under test, so request it without an
    # asserted expected base and then validate it independently.
    response, receipt = call(
        machine,
        npk.OP_MEMORY_ALLOCATE,
        operation_id,
        owner=owner,
        size=size,
        flags=ALLOC_FLAGS,
        aux=align,
    )
    npk.validate_memory_response(
        response,
        status=npk.REQUEST_OK,
        operation=npk.OP_MEMORY_ALLOCATE,
        result=response.result,
    )
    npk.validate_complete_memory_receipt(receipt)
    regions = npk.decode_memory_regions(machine)
    region = next(item for item in regions if item.state == npk.REGION_ALLOCATED
                  and item.base == response.result)
    assert receipt.result == response.result
    assert region.id != 0
    return response.result, region.id, region


def region_by_id(machine, region_id):
    return next(item for item in npk.decode_memory_regions(machine)
                if item.id == region_id)


def pte_walk_address(machine, root, va):
    table = root & npk.PTE_PPN_MASK
    for level in range(3):
        shift = 48 - 9 * (level + 1)
        index = (va >> shift) & 0x1FF
        pte = machine._read_mem(table + index * 8, 8)
        assert pte & npk.PTE_VALID
        table = pte & npk.PTE_PPN_MASK
    index = (va >> 12) & 0x1FF
    return table + index * 8


def leaf_pte(machine, root, va):
    return machine._read_mem(pte_walk_address(machine, root, va), 8)


def install_identity_low(machine, root):
    """Identity-map the low 1 GiB so paged guest code can still execute.

    This is host test setup, not a kernel API. A level-0 leaf is reserved in
    Nova, so the identity range uses a valid L0 table plus a 1 GiB leaf at L1.
    NPK's own mapping occupies L0 index 1 because ``MAP_VA`` is in the next
    1 GiB slot.
    """
    table = 0x30000
    machine._write_mem(root & npk.PTE_PPN_MASK, (table & npk.PTE_PPN_MASK) | 1, 8)
    machine._write_mem(table, 1 | 0x1F, 8)


def run_code(machine, source, *, privilege=None, max_steps=50_000):
    code = assemble_text(source)
    machine.load_code(code, CODE_ADDRESS)
    machine.pc = CODE_ADDRESS
    machine.halted = False
    if privilege is not None:
        machine.priv = privilege
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    assert machine.halted, f"guest code did not halt: pc=0x{machine.pc:x}"
    return machine


def test_layout_and_boot_preserve_m1_to_m3_evidence(image):
    npk.validate_memory_layout()
    assert npk.MEMORY_ENTRY_ADDRESS == 0x90F00
    assert npk.MEMORY_STATE_ADDRESS == 0x90F10
    assert npk.MEMORY_REGION_TABLE_ADDRESS == 0x90F90
    assert npk.MEMORY_REGION_SIZE == 64
    assert npk.MEMORY_REGION_SLOTS == 64
    assert npk.ADDRESS_SPACE_TABLE_ADDRESS == 0x91F90
    assert npk.MEMORY_REQUEST_ADDRESS == 0x91FD0
    assert npk.MEMORY_RESPONSE_ADDRESS == 0x92010
    assert npk.MEMORY_RECEIPT_TABLE_ADDRESS == 0x92050
    assert npk.MEMORY_RECEIPT_SLOTS == 128
    assert npk.PHYSICAL_ARENA_BASE == 0xC0000
    assert npk.PHYSICAL_ARENA_SIZE == 0x40000

    machine = fresh_machine(image)
    receipt = npk.decode_processing_receipt(machine)
    npk.validate_boot_receipt(receipt)
    assert receipt.trace == npk.EXPECTED_BOOT_TRACE

    snapshot = npk.decode_core_snapshot(machine)
    npk.validate_core_state(snapshot.core)
    assert (snapshot.core.admitted, snapshot.core.completed,
            snapshot.core.rejected, snapshot.core.failed) == (10, 7, 3, 2)
    assert all(item.reserved == (0, 0) for item in snapshot.receipts[:12])

    trap_state = npk.decode_trap_state(machine)
    assert trap_state.dispatch_count == 0
    assert trap_state.last_status == 0
    assert npk.decode_memory_state(machine).region_count == 0


def test_memory_init_publishes_arena_entry_and_zeroed_observation_regions(image):
    machine = fresh_machine(image)
    state = npk.decode_memory_state(machine)
    npk.validate_memory_state(state)
    assert state.magic == npk.MEMORY_STATE_MAGIC
    assert state.version == npk.MEMORY_STATE_VERSION
    assert state.init_status == 0
    assert state.arena_base == npk.PHYSICAL_ARENA_BASE
    assert state.arena_size == npk.PHYSICAL_ARENA_SIZE
    assert state.page_size == npk.MEMORY_PAGE_SIZE
    assert state.region_slots == npk.MEMORY_REGION_SLOTS
    assert state.address_space_slots == npk.ADDRESS_SPACE_SLOTS
    assert state.next_physical == npk.PHYSICAL_ARENA_BASE
    assert state.next_region_id == 1
    assert state.mapping_count == 0
    assert state.invalidation_epoch == 0

    entry = npk.decode_memory_entry(machine)
    assert npk.KERNEL_IMAGE_BASE <= entry < npk.MEMORY_ENTRY_ADDRESS
    assert npk.decode_memory_request(machine).magic == 0
    assert npk.decode_memory_response(machine).magic == 0
    assert not any(item.state for item in npk.decode_memory_regions(machine))
    assert not any(item.state for item in npk.decode_address_spaces(machine))
    assert not any(item.magic for item in npk.decode_memory_receipts(machine))


def test_allocate_commits_region_identity_and_raw_tag(image):
    machine = fresh_machine(image)
    main_receipts_before = npk.decode_core_snapshot(machine).receipts[:12]
    base, region_id, region = allocate(machine, 100, size=0x2000)

    assert npk.decode_core_snapshot(machine).receipts[:12] == main_receipts_before
    assert base == npk.PHYSICAL_ARENA_BASE
    assert region.state == npk.REGION_ALLOCATED
    assert region.id == region_id
    assert region.base == base
    assert region.size == 0x2000
    assert region.owner == OWNER
    assert region.type == npk.REGION_TYPE_NORMAL
    assert region.flags == 0
    assert region.reserved == 0
    assert machine._read_mem(base, 8) == npk.ALLOCATION_TAG + region_id

    state = npk.decode_memory_state(machine)
    assert state.region_count == 1
    assert state.next_physical == base + 0x2000
    assert state.allocation_epoch == 1

    receipts = npk.decode_memory_receipts(machine)
    receipt = receipts[0]
    assert receipt.id == 100
    assert receipt.type == npk.OP_MEMORY_ALLOCATE
    assert receipt.result == base
    assert receipt.reserved == (0x2000, 0x2000)


def test_query_reports_committed_region_without_mutating_it(image):
    machine = fresh_machine(image)
    base, region_id, region = allocate(machine, 100, size=0x2000)
    state_before = npk.decode_memory_state(machine)
    raw_before = machine._read_mem(base, 8)

    response, receipt = call_complete(
        machine, npk.OP_MEMORY_QUERY, 101, address=region_id, result=base
    )
    assert response.receipt_index == 1
    assert receipt.reserved == (0, region.size)
    assert npk.decode_memory_state(machine).region_count == state_before.region_count
    assert npk.decode_memory_state(machine).allocation_epoch == state_before.allocation_epoch
    assert machine._read_mem(base, 8) == raw_before
    assert region_by_id(machine, region_id) == region


def test_release_frees_identity_and_allows_reuse(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    response, receipt = call_complete(
        machine, npk.OP_MEMORY_RELEASE, 101, address=region_id, result=base
    )
    assert receipt.reserved == (0, region_id)
    assert not any(item.id == region_id or item.state
                   for item in npk.decode_memory_regions(machine))

    state = npk.decode_memory_state(machine)
    assert state.region_count == 0
    assert state.next_physical == npk.PHYSICAL_ARENA_BASE

    second_base, second_id, _ = allocate(machine, 102)
    assert second_base == base
    assert second_id > region_id


def test_double_release_and_stale_region_id_are_rejected(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    call_complete(machine, npk.OP_MEMORY_RELEASE, 101,
                  address=region_id, result=base)
    call_rejected(machine, npk.OP_MEMORY_RELEASE, 102,
                  address=region_id, reason=npk.REASON_MEMORY_BAD_REGION)
    call_rejected(machine, npk.OP_MEMORY_QUERY, 103,
                  address=region_id, reason=npk.REASON_MEMORY_BAD_REGION)
    assert npk.decode_memory_state(machine).region_count == 0


@pytest.mark.parametrize(
    "operation_id,size,align,reason",
    [
        (200, 0, 0x1000, npk.REASON_MEMORY_BAD_SIZE),
        (201, 0x1800, 0x1000, npk.REASON_MEMORY_BAD_SIZE),
        (202, npk.PHYSICAL_ARENA_SIZE + 0x1000, 0x1000,
         npk.REASON_MEMORY_BAD_SIZE),
        (203, 0x1000, 0x3000, npk.REASON_MEMORY_BAD_ALIGN),
    ],
)
def test_allocate_rejects_invalid_size_and_alignment(image, operation_id,
                                                     size, align, reason):
    machine = fresh_machine(image)
    call_rejected(
        machine,
        npk.OP_MEMORY_ALLOCATE,
        operation_id,
        size=size,
        flags=ALLOC_FLAGS,
        aux=align,
        reason=reason,
    )
    state = npk.decode_memory_state(machine)
    assert state.region_count == 0
    assert state.next_physical == npk.PHYSICAL_ARENA_BASE
    assert not any(item.state for item in npk.decode_memory_regions(machine))


def test_owner_checks_protect_allocated_regions(image):
    machine = fresh_machine(image)
    _, region_id, _ = allocate(machine, 100, owner=OWNER)
    call_rejected(machine, npk.OP_MEMORY_QUERY, 101, owner=OTHER_OWNER,
                  address=region_id, reason=npk.REASON_MEMORY_OWNER)
    call_rejected(machine, npk.OP_MEMORY_RELEASE, 102, owner=OTHER_OWNER,
                  address=region_id, reason=npk.REASON_MEMORY_OWNER)
    assert region_by_id(machine, region_id).state == npk.REGION_ALLOCATED


def test_map_builds_four_level_page_table_and_publishes_permissions(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    aux = (region_id << 16) | 0
    response, receipt = call_complete(
        machine, npk.OP_MEMORY_MAP, 101,
        address=MAP_VA, size=npk.MEMORY_PAGE_SIZE, flags=RWXU, aux=aux,
        result=MAP_VA,
    )
    assert response.receipt_index == 1
    assert receipt.reserved == (npk.MEMORY_PAGE_SIZE, base)

    space = npk.decode_address_spaces(machine)[0]
    assert space.state == npk.ADDRESS_SPACE_ACTIVE
    assert space.owner == OWNER
    assert space.root != 0
    assert space.mapping_count == 1

    state = npk.decode_memory_state(machine)
    assert state.mapping_count == 1
    assert state.invalidation_epoch == 1
    assert state.allocation_epoch == 5  # one data page plus four table pages
    assert region_by_id(machine, region_id).reserved == 1

    pte = leaf_pte(machine, space.root, MAP_VA)
    assert pte & npk.PTE_PPN_MASK == base
    assert pte & 0x1F == npk.PTE_VALID | npk.PTE_READ | npk.PTE_WRITE | \
        npk.PTE_EXEC | npk.PTE_USER


def test_rejected_map_does_not_mutate_memory_or_page_tables(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    aux = (region_id << 16) | 0
    regions_before = npk.decode_memory_regions(machine)
    spaces_before = npk.decode_address_spaces(machine)
    state_before = npk.decode_memory_state(machine)
    raw_before = machine._read_mem(base, 8)

    call_rejected(
        machine, npk.OP_MEMORY_MAP, 101,
        address=MAP_VA, size=npk.MEMORY_PAGE_SIZE, flags=WITHOUT_READ, aux=aux,
        reason=npk.REASON_MEMORY_BAD_MAPPING,
    )
    assert npk.decode_memory_regions(machine) == regions_before
    assert npk.decode_address_spaces(machine) == spaces_before
    state_after = npk.decode_memory_state(machine)
    assert state_after.allocation_epoch == state_before.allocation_epoch
    assert state_after.mapping_count == state_before.mapping_count
    assert state_after.next_physical == state_before.next_physical
    assert machine._read_mem(base, 8) == raw_before


@pytest.mark.parametrize(
    "operation_id,address,size,flags,aux,reason",
    [
        (200, MAP_VA + 1, npk.MEMORY_PAGE_SIZE, RWXU, 0,
         npk.REASON_MEMORY_BAD_MAPPING),
        (201, 0x0000800000000000, npk.MEMORY_PAGE_SIZE, RWXU, 0,
         npk.REASON_MEMORY_BAD_MAPPING),
        (202, MAP_VA, npk.MEMORY_PAGE_SIZE * 2, RWXU, 0,
         npk.REASON_MEMORY_BAD_SIZE),
        (203, MAP_VA, npk.MEMORY_PAGE_SIZE, 0, 0,
         npk.REASON_MEMORY_UNSUPPORTED),
        (204, MAP_VA, npk.MEMORY_PAGE_SIZE, RWXU, 0xFFFF,
         npk.REASON_MEMORY_BAD_ADDRESS_SPACE),
    ],
)
def test_map_rejects_invalid_arguments(image, operation_id, address, size,
                                       flags, aux, reason):
    machine = fresh_machine(image)
    _, region_id, _ = allocate(machine, 100)
    aux = (region_id << 16) | (aux & 0xFFFF)
    call_rejected(
        machine, npk.OP_MEMORY_MAP, operation_id,
        address=address, size=size, flags=flags, aux=aux, reason=reason,
    )
    assert npk.decode_memory_state(machine).mapping_count == 0


def test_duplicate_map_fails_at_commit_without_replacing_leaf(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    aux = (region_id << 16) | 0
    call_complete(machine, npk.OP_MEMORY_MAP, 101, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU, aux=aux, result=MAP_VA)
    space = npk.decode_address_spaces(machine)[0]
    leaf_address = pte_walk_address(machine, space.root, MAP_VA)
    leaf_before = machine._read_mem(leaf_address, 8)
    state_before = npk.decode_memory_state(machine)

    call_failed(
        machine, npk.OP_MEMORY_MAP, 102,
        address=MAP_VA, size=npk.MEMORY_PAGE_SIZE, flags=RU, aux=aux,
        reason=npk.REASON_MEMORY_PTE_CONFLICT,
        stage=npk.STAGE_COMMIT,
    )
    assert machine._read_mem(leaf_address, 8) == leaf_before
    state_after = npk.decode_memory_state(machine)
    assert state_after.mapping_count == state_before.mapping_count
    assert state_after.allocation_epoch == state_before.allocation_epoch
    assert region_by_id(machine, region_id).reserved == 1
    assert base == leaf_before & npk.PTE_PPN_MASK


def test_mapped_region_cannot_be_released_until_unmapped(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    aux = (region_id << 16) | 0
    call_complete(machine, npk.OP_MEMORY_MAP, 101, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU, aux=aux, result=MAP_VA)
    call_rejected(machine, npk.OP_MEMORY_RELEASE, 102, address=region_id,
                  reason=npk.REASON_MEMORY_STATE)

    call_complete(machine, npk.OP_MEMORY_UNMAP, 103, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, aux=0, result=MAP_VA)
    assert region_by_id(machine, region_id).reserved == 0
    assert npk.decode_memory_state(machine).mapping_count == 0
    call_complete(machine, npk.OP_MEMORY_RELEASE, 104,
                  address=region_id, result=base)


def test_double_unmap_and_stale_unmap_are_distinguished(image):
    machine = fresh_machine(image)
    _, region_id, _ = allocate(machine, 100, size=0x2000)
    aux = (region_id << 16) | 0
    first_va = MAP_VA
    second_va = MAP_VA + npk.MEMORY_PAGE_SIZE
    for operation_id, va in ((101, first_va), (102, second_va)):
        call_complete(machine, npk.OP_MEMORY_MAP, operation_id, address=va,
                      size=npk.MEMORY_PAGE_SIZE, flags=RWXU, aux=aux,
                      result=va)

    call_complete(machine, npk.OP_MEMORY_UNMAP, 103, address=first_va,
                  size=npk.MEMORY_PAGE_SIZE, aux=0, result=first_va)
    call_failed(
        machine, npk.OP_MEMORY_UNMAP, 104,
        address=first_va, size=npk.MEMORY_PAGE_SIZE, aux=0,
        reason=npk.REASON_MEMORY_BAD_MAPPING, stage=npk.STAGE_EXECUTE,
    )
    call_complete(machine, npk.OP_MEMORY_UNMAP, 105, address=second_va,
                  size=npk.MEMORY_PAGE_SIZE, aux=0, result=second_va)
    call_rejected(machine, npk.OP_MEMORY_UNMAP, 106, address=first_va,
                  size=npk.MEMORY_PAGE_SIZE, aux=0,
                  reason=npk.REASON_MEMORY_STATE)


def test_two_address_spaces_are_independent(image):
    machine = fresh_machine(image)
    base0, id0, _ = allocate(machine, 100, owner=OWNER)
    base1, id1, _ = allocate(machine, 101, owner=OWNER)
    call_complete(machine, npk.OP_MEMORY_MAP, 102, owner=OWNER, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(id0 << 16) | 0, result=MAP_VA)
    call_complete(machine, npk.OP_MEMORY_MAP, 103, owner=OWNER, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(id1 << 16) | 1, result=MAP_VA)

    spaces = npk.decode_address_spaces(machine)
    assert spaces[0].root != spaces[1].root
    assert spaces[0].mapping_count == spaces[1].mapping_count == 1
    assert leaf_pte(machine, spaces[0].root, MAP_VA) & npk.PTE_PPN_MASK == base0
    assert leaf_pte(machine, spaces[1].root, MAP_VA) & npk.PTE_PPN_MASK == base1
    assert leaf_pte(machine, spaces[0].root, MAP_VA) != leaf_pte(
        machine, spaces[1].root, MAP_VA
    )


def test_address_space_owner_is_enforced(image):
    machine = fresh_machine(image)
    _, owned_id, _ = allocate(machine, 100, owner=OWNER)
    _, foreign_id, _ = allocate(machine, 101, owner=OTHER_OWNER)
    call_complete(machine, npk.OP_MEMORY_MAP, 102, owner=OWNER, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(owned_id << 16) | 0, result=MAP_VA)
    call_rejected(machine, npk.OP_MEMORY_MAP, 103, owner=OTHER_OWNER,
                  address=MAP_VA + npk.MEMORY_PAGE_SIZE,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(foreign_id << 16) | 0,
                  reason=npk.REASON_MEMORY_OWNER)
    assert npk.decode_address_spaces(machine)[0].mapping_count == 1


def test_paged_guest_code_reads_and_writes_the_mapped_page(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    call_complete(machine, npk.OP_MEMORY_MAP, 101, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(region_id << 16) | 0, result=MAP_VA)
    root = npk.decode_address_spaces(machine)[0].root
    install_identity_low(machine, root)
    machine.write_csr("ptbr", (1 << 63) | (root & npk.PTE_PPN_MASK))

    sentinel = 0x4E504B4454415401
    run_code(machine, f"""
        LI r1, {sentinel}
        LI r2, {MAP_VA}
        STD r1, [r2]
        LDD r3, [r2]
        HALT
    """)
    assert machine.read_register(3) == sentinel
    assert machine._read_mem(base, 8) == sentinel

    leaf = leaf_pte(machine, root, MAP_VA)
    assert leaf & 0xC0 == 0xC0  # accessed and dirty were set by real translation


def test_user_permission_bits_are_enforced_by_the_real_translator(image):
    machine = fresh_machine(image)
    base, region_id, _ = allocate(machine, 100)
    call_complete(machine, npk.OP_MEMORY_MAP, 101, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RU,
                  aux=(region_id << 16) | 0, result=MAP_VA)
    root = npk.decode_address_spaces(machine)[0].root
    install_identity_low(machine, root)
    machine.write_csr("ptbr", (1 << 63) | (root & npk.PTE_PPN_MASK))

    sentinel = 0x4E504B52
    machine._write_mem(base, sentinel, 8)

    machine.priv = USER
    assert machine._translate(MAP_VA, 8, "r") == base
    with pytest.raises(Exception) as caught:
        machine._translate(MAP_VA, 8, "w")
    assert caught.value.code == EXCEPTION_CODES["MMU_FAULT"]
    assert caught.value.detail == MMU_DETAIL_PERMISSION
    assert machine.read_csr("tval") == MAP_VA
    assert machine._read_mem(base, 8) == sentinel


def test_unmap_clears_leaf_and_flushes_cached_translation(image):
    machine = fresh_machine(image)
    _, region_id, _ = allocate(machine, 100)
    call_complete(machine, npk.OP_MEMORY_MAP, 101, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, flags=RWXU,
                  aux=(region_id << 16) | 0, result=MAP_VA)
    root = npk.decode_address_spaces(machine)[0].root
    install_identity_low(machine, root)
    machine.write_csr("ptbr", (1 << 63) | (root & npk.PTE_PPN_MASK))
    assert machine._translate(MAP_VA, 8, "r") in range(
        npk.PHYSICAL_ARENA_BASE,
        npk.PHYSICAL_ARENA_BASE + npk.PHYSICAL_ARENA_SIZE,
    )
    epoch_before = npk.decode_memory_state(machine).invalidation_epoch

    call_complete(machine, npk.OP_MEMORY_UNMAP, 102, address=MAP_VA,
                  size=npk.MEMORY_PAGE_SIZE, aux=0, result=MAP_VA)
    assert leaf_pte(machine, root, MAP_VA) == 0
    assert npk.decode_memory_state(machine).invalidation_epoch == epoch_before + 1
    with pytest.raises(Exception) as caught:
        machine._translate(MAP_VA, 8, "r")
    assert caught.value.code == EXCEPTION_CODES["MMU_FAULT"]
    assert caught.value.detail == MMU_DETAIL_MISSING


def test_arena_exhaustion_is_a_failed_execute_then_recovers(image):
    machine = fresh_machine(image)
    base, _, _ = allocate(machine, 100, size=npk.PHYSICAL_ARENA_SIZE)
    call_failed(
        machine, npk.OP_MEMORY_ALLOCATE, 101,
        size=npk.MEMORY_PAGE_SIZE, flags=ALLOC_FLAGS,
        aux=npk.MEMORY_PAGE_SIZE,
        reason=npk.REASON_MEMORY_EXHAUSTED, stage=npk.STAGE_EXECUTE,
    )
    assert npk.decode_memory_state(machine).region_count == 1
    call_complete(machine, npk.OP_MEMORY_RELEASE, 102,
                  address=1, result=base)
    second_base, _, _ = allocate(machine, 103)
    assert second_base == base


def test_bad_magic_and_unknown_operation_do_not_admit(image):
    machine = fresh_machine(image)
    core_before = npk.decode_core_snapshot(machine).core

    npk.write_memory_request(
        machine, operation=npk.OP_MEMORY_ALLOCATE, operation_id=100,
        owner=OWNER, size=npk.MEMORY_PAGE_SIZE, flags=ALLOC_FLAGS,
    )
    machine._write_mem(npk.MEMORY_REQUEST_ADDRESS, 0, 8)
    npk.call_memory_entry(machine)
    bad_magic = npk.decode_memory_response(machine)
    npk.validate_memory_response(
        bad_magic, status=npk.REQUEST_BAD_REQUEST,
        operation=npk.OP_MEMORY_ALLOCATE,
    )

    npk.write_memory_request(
        machine, operation=99, operation_id=101, owner=OWNER,
    )
    npk.call_memory_entry(machine)
    bad_op = npk.decode_memory_response(machine)
    npk.validate_memory_response(
        bad_op, status=npk.REQUEST_BAD_OP, operation=99,
    )

    core_after = npk.decode_core_snapshot(machine).core
    assert core_after.admitted == core_before.admitted
    assert core_after.rejected == core_before.rejected
    assert core_after.failed == core_before.failed
    assert not any(item.magic for item in npk.decode_memory_receipts(machine))


def test_m3_trap_processing_still_works_after_memory_init(image):
    machine = fresh_machine(image)
    machine.load_code(assemble_text("SCALL 5\nHALT\n"), CODE_ADDRESS)
    machine.pc = CODE_ADDRESS
    machine.priv = USER
    machine.halted = False
    for _ in range(50_000):
        machine.run(1)
        current = npk.decode_trap_state(machine)
        if (current.dispatch_count and current.classification and current.action
                and current.result == npk.TRAP_RESULT_PROCESSED
                and current.receipt_index != npk.NO_INDEX):
            break

    state = npk.decode_trap_state(machine)
    npk.validate_trap_state(state)
    assert state.dispatch_count == 1
    assert state.classification == npk.CLASS_SYSTEM_CALL
    assert state.action == npk.ACTION_RESUME_NEXT
    receipt = npk.decode_trap_receipts(machine)[0]
    assert receipt.type == npk.OP_TRAP_PROCESS
    assert receipt.outcome == npk_core.OUTCOME_COMPLETE
    core = npk.decode_core_snapshot(machine).core
    assert (core.admitted, core.completed) == (11, 8)
