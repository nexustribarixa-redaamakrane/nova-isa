"""NPK Milestone 9 typed executable processing and user execution tests.

These tests boot the real NPK kernel and submit typed operations through the
published executable entry. Successful starts use the ISA's VMLAUNCH path: the
guest executes a staged native ``.nob`` image, raises ``SCALL``, and returns to
the host through the architectural VM-exit path. No ELF compatibility, second
object manager, or fabricated MRET frame is assumed.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from nova import npk
from nova.assembler import assemble_text
from nova.nob import NOB_HEADER_SIZE, encode_nob
from nova.npk import core as core_api

OWNER = 1
OTHER_OWNER = 2
TASK = 0x5441534B00000101
BAD_TASK = 0x5441534B00000FFF
BASE = 0x9000000000
STACK = BASE + npk.MEMORY_PAGE_SIZE
EXIT_CODE = 42
RWXU = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC | npk.PERM_USER
RWU = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_USER
NOB_MAGIC_INT = int.from_bytes(b"NOBV", "little")


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(npk.build_kernel(tmp_path_factory.mktemp("npk-m9")))


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def make_user_container(*, base=BASE, exit_code=EXIT_CODE, syscall=0):
    code = assemble_text(f"LI r1, {exit_code}\nSCALL {syscall}\nHALT\n")
    code += b"\x00" * ((-len(code)) % 4)
    entry = base
    data = encode_nob(image_base=base, entry=entry, image=code)
    return data, code, entry


def write_source(machine, data, *, address=npk.EXEC_SOURCE_ADDRESS):
    for offset in range(0, len(data), 8):
        chunk = data[offset:offset + 8].ljust(8, b"\x00")
        machine._write_mem(address + offset,
                           int.from_bytes(chunk, "little"), 8)
    return len(data)


def raw_container(*, base=BASE, entry=None, image_size=12, magic=NOB_MAGIC_INT,
                  version=1, flags=0, size_field=None):
    if entry is None:
        entry = base
    if size_field is None:
        size_field = image_size
    header = struct.pack("<IHHQQQ", magic & 0xFFFFFFFF, version & 0xFFFF,
                         flags & 0xFFFF, base, entry, size_field)
    return header + (b"\x00" * image_size)


def allocate_region(machine, operation_id, *, owner=OWNER):
    response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_ALLOCATE,
        operation_id=operation_id,
        owner=owner,
        size=npk.MEMORY_PAGE_SIZE,
        flags=npk.REGION_TYPE_NORMAL,
        aux=npk.MEMORY_PAGE_SIZE,
    )
    assert response.status == npk.REQUEST_OK
    region = next(
        item for item in npk.decode_memory_regions(machine)
        if item.base == response.result and item.state == npk.REGION_ALLOCATED
    )
    return response.result, region.id


def map_region(machine, operation_id, address, region_id, permissions,
               *, owner=OWNER, asid=0):
    response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=operation_id,
        owner=owner,
        address=address,
        size=npk.MEMORY_PAGE_SIZE,
        flags=permissions,
        aux=(region_id << 16) | asid,
    )
    assert response.status == npk.REQUEST_OK
    return response


def unmap_region(machine, operation_id, address, *, owner=OWNER):
    response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_UNMAP,
        operation_id=operation_id,
        owner=owner,
        address=address,
        size=npk.MEMORY_PAGE_SIZE,
        flags=0,
        aux=0,
    )
    assert response.status == npk.REQUEST_OK
    return response


def create_region_object(machine, operation_id, region_id, *, owner=OWNER,
                         permissions=npk.OBJ_PERM_ALL,
                         type=npk.OBJECT_TYPE_MEMORY_REGION):
    response = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CREATE,
        operation_id=operation_id,
        owner=owner,
        type=type,
        permissions=permissions,
        payload=region_id,
    )
    assert response.status == npk.REQUEST_OK
    return npk.decode_object_state(machine).last_handle


def exec_call(machine, operation_id, operation, **kwargs):
    return npk.call_exec_request(
        machine, operation=operation, id=operation_id, **kwargs
    )


def receipt_at(machine, response):
    assert response.receipt_index < npk.EXEC_RECEIPT_SLOTS
    return npk.decode_exec_receipts(machine)[response.receipt_index]


def assert_receipt(machine, response, operation_id, *, outcome, reason,
                   stage, state, result=0):
    receipt = receipt_at(machine, response)
    expectation = npk.exec_receipt_expectation(
        operation_id, response.operation, outcome, result=result,
        reason=reason, stage=stage, state=state,
    )
    core_api.validate_receipt(receipt, expectation)
    return receipt


def assert_complete(machine, response, operation_id, *, result=None):
    expected_result = response.result if result is None else result
    receipt = assert_receipt(
        machine, response, operation_id,
        outcome=core_api.OUTCOME_COMPLETE,
        reason=core_api.REASON_NONE,
        stage=core_api.STAGE_COMPLETE,
        state=core_api.STATE_COMPLETE,
        result=expected_result,
    )
    npk.validate_complete_exec_receipt(receipt)
    assert receipt.trace == core_api.EXPECTED_STAGE_TRACE
    return receipt


def assert_rejected(machine, response, operation_id, reason):
    assert response.status == npk.REQUEST_REJECTED
    assert response.reason == reason
    receipt = assert_receipt(
        machine, response, operation_id,
        outcome=core_api.OUTCOME_REJECTED,
        reason=reason,
        stage=core_api.STAGE_VALIDATE,
        state=core_api.STATE_REJECTED,
    )
    npk.validate_rejected_exec_receipt(receipt)
    return receipt


def assert_failed(machine, response, operation_id, reason,
                  *, stage=core_api.STAGE_EXECUTE):
    assert response.status == npk.REQUEST_FAILED
    assert response.reason == reason
    receipt = assert_receipt(
        machine, response, operation_id,
        outcome=core_api.OUTCOME_FAILED,
        reason=reason,
        stage=stage,
        state=core_api.STATE_FAILED,
    )
    npk.validate_failed_exec_receipt(receipt, stage=stage)
    return receipt


def setup_load(machine, op_base, *, base=BASE, owner=OWNER, container=None):
    code_physical, code_region = allocate_region(machine, op_base, owner=owner)
    stack_physical, stack_region = allocate_region(machine, op_base + 1,
                                                   owner=owner)
    handle = create_region_object(machine, op_base + 2, code_region,
                                  owner=owner)
    if container is None:
        container, code, entry = make_user_container(base=base)
    else:
        code = container[NOB_HEADER_SIZE:]
        base = int.from_bytes(container[8:16], "little")
        entry = int.from_bytes(container[16:24], "little")
    source_size = npk.stage_nob(machine, container)
    response = exec_call(
        machine, op_base + 3, npk.OP_EXEC_LOAD,
        owner=owner, handle=handle, source=npk.EXEC_SOURCE_ADDRESS,
        source_size=source_size,
    )
    assert_complete(machine, response, op_base + 3)
    return SimpleNamespace(
        code_physical=code_physical,
        code_region=code_region,
        stack_physical=stack_physical,
        stack_region=stack_region,
        handle=handle,
        container=container,
        code=code,
        entry=entry,
        base=base,
        source_size=source_size,
        image_id=response.result,
    )


def map_execution(machine, load, op_base, *, owner=OWNER):
    map_region(machine, op_base, load.base, load.code_region, RWXU, owner=owner)
    map_region(machine, op_base + 1, STACK, load.stack_region, RWU,
               owner=owner)
    assert [space.mapping_count for space in npk.decode_address_spaces(machine)][:2] == [2, 0]


def create_exec_task(machine, load, operation_id, *, task=TASK, owner=OWNER,
                     entry=None, stack=STACK):
    response = npk.call_task_request(
        machine,
        operation=npk.OP_TASK_CREATE,
        id=operation_id,
        task=task,
        arg0=owner,
        arg1=load.entry if entry is None else entry,
        arg2=stack,
        arg3=npk.MEMORY_PAGE_SIZE,
        arg4=0,
    )
    assert response.status == npk.REQUEST_OK
    return task


def find_task(machine, task_id):
    return next(task for task in npk.decode_task_tcbs(machine)
                if task.id == task_id and task.state != npk.TASK_STATE_FREE)


def page_table_entry(machine, address, *, asid=0):
    """Walk the M4 software page table and return the final leaf PTE."""
    root = npk.decode_address_spaces(machine)[asid].root
    for level in range(4):
        shift = 48 - 9 * (level + 1)
        index = (address >> shift) & 511
        entry_address = (root & npk.PTE_PPN_MASK) | (index * 8)
        pte = npk.exec_word(machine, entry_address)
        if level < 3:
            root = pte & npk.PTE_PPN_MASK
    return pte


def test_executable_layout_and_initial_state(image):
    machine = fresh_machine(image)
    npk.validate_exec_layout()
    assert npk.decode_exec_entry(machine) != 0
    state = npk.decode_exec_state(machine)
    npk.validate_exec_state(state)
    assert state.magic == npk.EXEC_STATE_MAGIC
    assert state.version == npk.EXEC_STATE_VERSION
    assert state.exec_slots == npk.EXEC_SLOTS
    assert state.exec_count == 0 and state.loaded_count == 0
    assert state.next_image_id == 1 and state.next_generation == 1
    assert state.target_architecture == npk.TARGET_ARCHITECTURE
    snapshot = npk.decode_exec_snapshot(machine)
    npk.validate_exec_snapshot(snapshot)
    assert all(record.state == npk.IMAGE_FREE for record in snapshot.records)
    assert all(receipt.magic == 0 for receipt in snapshot.receipts)
    # The frozen syscall table is unchanged by executable processing.
    assert (npk.SYS_EXIT, npk.SYS_WRITE, npk.SYS_READ, npk.SYS_TIME,
            npk.SYS_OBJ_CREATE, npk.SYS_OBJ_SIZE, npk.SYS_OBJ_DESTROY,
            npk.SYS_YIELD) == (0, 1, 2, 3, 4, 5, 6, 7)


def test_load_copies_native_nob_into_m4_region(image):
    machine = fresh_machine(image)
    load = setup_load(machine, 1000)
    state = npk.decode_exec_state(machine)
    assert state.exec_count == 1 and state.loaded_count == 1
    assert state.next_image_id == 2
    record = npk.decode_exec_records(machine)[0]
    assert record.state == npk.IMAGE_LOADED
    assert record.id == load.image_id
    assert record.owner == OWNER
    assert record.handle == load.handle
    assert record.region_id == load.code_region
    assert record.image_base == BASE
    assert record.entry == load.entry
    assert record.image_size == len(load.code)
    assert record.flags == npk.TARGET_ARCHITECTURE
    for offset in range(0, len(load.code), 8):
        expected = int.from_bytes(load.code[offset:offset + 8].ljust(8, b"\x00"),
                                  "little")
        assert npk.exec_word(machine, load.code_physical + offset) == expected
    staged_word = npk.exec_word(machine, npk.EXEC_SOURCE_ADDRESS)
    assert staged_word & 0xFFFFFFFF == npk.NOB_MAGIC_WORD


def test_load_rejects_malformed_containers(image):
    cases = [
        ("bad magic", dict(magic=0), npk.REASON_EXEC_BAD_CONTAINER),
        ("bad version", dict(version=2), npk.REASON_EXEC_BAD_CONTAINER),
        ("bad flags", dict(flags=1), npk.REASON_EXEC_BAD_CONTAINER),
        ("zero image", dict(image_size=0, size_field=0),
         npk.REASON_EXEC_BAD_IMAGE),
        ("oversize image", dict(image_size=0, size_field=0x2000),
         npk.REASON_EXEC_BAD_IMAGE),
        ("unaligned image", dict(image_size=13), npk.REASON_EXEC_BAD_ALIGN),
        ("unaligned base", dict(base=BASE + 1, entry=BASE + 1),
         npk.REASON_EXEC_BAD_ALIGN),
        ("noncanonical base", dict(base=0x0000800000000000,
                                   entry=0x0000800000000000),
         npk.REASON_EXEC_BAD_BASE),
        ("entry outside image", dict(entry=BASE + 12),
         npk.REASON_EXEC_BAD_ENTRY),
        ("size mismatch", dict(image_size=12, size_field=8),
         npk.REASON_EXEC_BAD_SIZE),
    ]
    for name, fields, reason in cases:
        machine = fresh_machine(image)
        _physical, region = allocate_region(machine, 1100)
        handle = create_region_object(machine, 1101, region)
        data = raw_container(**fields)
        size = write_source(machine, data)
        response = exec_call(
            machine, 1102, npk.OP_EXEC_LOAD, owner=OWNER, handle=handle,
            source=npk.EXEC_SOURCE_ADDRESS, source_size=size,
        )
        assert_rejected(machine, response, 1102, reason), name
        assert npk.decode_exec_state(machine).exec_count == 0
        assert all(record.state == npk.IMAGE_FREE
                   for record in npk.decode_exec_records(machine))


def test_load_rejects_bad_source_window_and_request_fields(image):
    machine = fresh_machine(image)
    _physical, region = allocate_region(machine, 1200)
    handle = create_region_object(machine, 1201, region)
    container, _code, _entry = make_user_container()
    size = npk.stage_nob(machine, container)

    short = exec_call(
        machine, 1202, npk.OP_EXEC_LOAD, owner=OWNER, handle=handle,
        source=npk.EXEC_SOURCE_ADDRESS, source_size=NOB_HEADER_SIZE - 1,
    )
    assert_rejected(machine, short, 1202, npk.REASON_EXEC_BAD_SOURCE)

    outside = exec_call(
        machine, 1203, npk.OP_EXEC_LOAD, owner=OWNER, handle=handle,
        source=0, source_size=size,
    )
    assert_rejected(machine, outside, 1203, npk.REASON_EXEC_BAD_SOURCE)

    flagged = exec_call(
        machine, 1204, npk.OP_EXEC_LOAD, owner=OWNER, handle=handle,
        source=npk.EXEC_SOURCE_ADDRESS, source_size=size, flags=1,
    )
    assert_rejected(machine, flagged, 1204, npk.REASON_EXEC_BAD_REQUEST)

    npk.write_exec_request(
        machine, operation=npk.OP_EXEC_LOAD, id=1205, owner=OWNER,
        handle=handle, source=npk.EXEC_SOURCE_ADDRESS, source_size=size,
    )
    npk.exec_word  # keep the public observation helper referenced
    machine._write_mem(npk.EXEC_REQUEST_ADDRESS, 0, 8)
    npk.call_exec_entry(machine)
    malformed = npk.decode_exec_response(machine)
    assert malformed.status == npk.REQUEST_BAD_REQUEST
    assert malformed.reason == npk.REASON_EXEC_BAD_REQUEST
    assert malformed.receipt_index == npk.NO_INDEX
    assert npk.decode_exec_state(machine).last_status == npk.REQUEST_BAD_REQUEST


def test_load_enforces_m5_handle_owner_permission_and_object_type(image):
    machine = fresh_machine(image)
    physical, region = allocate_region(machine, 1300)
    handle = create_region_object(machine, 1301, region)
    container, _code, _entry = make_user_container()
    size = npk.stage_nob(machine, container)
    kwargs = dict(source=npk.EXEC_SOURCE_ADDRESS, source_size=size)

    bad_handle = exec_call(
        machine, 1302, npk.OP_EXEC_LOAD, owner=OWNER, handle=0, **kwargs
    )
    assert_rejected(machine, bad_handle, 1302, npk.REASON_EXEC_BAD_HANDLE)

    bad_owner = exec_call(
        machine, 1303, npk.OP_EXEC_LOAD, owner=0, handle=handle, **kwargs
    )
    assert_rejected(machine, bad_owner, 1303, npk.REASON_EXEC_BAD_OWNER)

    readonly = create_region_object(
        machine, 1304, region, permissions=npk.OBJ_PERM_READ
    )
    bad_permission = exec_call(
        machine, 1305, npk.OP_EXEC_LOAD, owner=OWNER, handle=readonly, **kwargs
    )
    assert_rejected(machine, bad_permission, 1305,
                    npk.REASON_EXEC_BAD_PERMISSION)

    generic = create_region_object(
        machine, 1306, region, type=npk.OBJECT_TYPE_GENERIC
    )
    bad_object = exec_call(
        machine, 1307, npk.OP_EXEC_LOAD, owner=OWNER, handle=generic, **kwargs
    )
    assert_rejected(machine, bad_object, 1307, npk.REASON_EXEC_BAD_OBJECT)

    reserved = npk.REASON_EXEC_BAD_STATE
    map_region(machine, 1308, BASE, region, RWXU)
    mapped = exec_call(
        machine, 1309, npk.OP_EXEC_LOAD, owner=OWNER, handle=handle, **kwargs
    )
    assert_rejected(machine, mapped, 1309, reserved)
    pte = page_table_entry(machine, BASE)
    assert pte & npk.PTE_VALID != 0
    assert pte & npk.PTE_PPN_MASK == physical & npk.PTE_PPN_MASK


def test_start_validates_image_task_owner_and_m4_mapping(image):
    machine = fresh_machine(image)
    load = setup_load(machine, 1400)
    base = dict(image_id=load.image_id, owner=OWNER, handle=load.handle)
    create_exec_task(machine, load, 1401, task=TASK)

    missing_image = exec_call(
        machine, 1402, npk.OP_EXEC_START, image_id=0, owner=OWNER,
        handle=load.handle, task=TASK,
    )
    assert_rejected(machine, missing_image, 1402, npk.REASON_EXEC_BAD_REQUEST)

    wrong_owner = exec_call(
        machine, 1403, npk.OP_EXEC_START, image_id=load.image_id,
        owner=OTHER_OWNER, handle=load.handle, task=TASK,
    )
    assert_rejected(machine, wrong_owner, 1403, npk.REASON_EXEC_BAD_OWNER)

    no_task = exec_call(
        machine, 1404, npk.OP_EXEC_START, **base, task=0
    )
    assert_rejected(machine, no_task, 1404, npk.REASON_EXEC_BAD_STATE)

    bad_task = exec_call(
        machine, 1405, npk.OP_EXEC_START, **base, task=BAD_TASK
    )
    assert_rejected(machine, bad_task, 1405, npk.REASON_EXEC_BAD_TASK)

    no_mapping = exec_call(
        machine, 1406, npk.OP_EXEC_START, **base, task=TASK
    )
    assert_rejected(machine, no_mapping, 1406, npk.REASON_EXEC_BAD_MAPPING)
    assert npk.decode_exec_records(machine)[0].state == npk.IMAGE_LOADED


def test_start_executes_user_nob_through_vmlaunch_and_real_syscall(image):
    machine = fresh_machine(image)
    load = setup_load(machine, 1500)
    map_execution(machine, load, 1502)
    task = create_exec_task(machine, load, 1504)
    host_ptbr = machine.read_csr("ptbr")

    start = exec_call(
        machine, 1505, npk.OP_EXEC_START,
        image_id=load.image_id, owner=OWNER, handle=load.handle,
        task=task, asid=0, arg=7,
    )
    receipt = assert_complete(machine, start, 1505, result=EXIT_CODE)
    assert receipt.reserved[0] == load.image_id
    assert receipt.reserved[1] == 1 << 32

    snapshot = npk.decode_exec_snapshot(machine)
    context = snapshot.launch_context
    assert context.status == 1
    assert context.record_index == 0
    assert context.vpc == load.entry
    assert context.vpriv == 0
    root = npk.decode_address_spaces(machine)[0].root
    assert context.vptbr == (1 << 63) | (root & npk.PTE_PPN_MASK)
    assert context.guest_arg == 7
    assert context.guest_sp == load.base + 2 * npk.MEMORY_PAGE_SIZE - 16
    assert context.stack_top == load.base + 2 * npk.MEMORY_PAGE_SIZE
    assert context.exit_code == EXIT_CODE
    assert context.exit_cause == 7
    assert context.exit_ecause == 0
    assert context.launch_count == 1
    assert context.saved_priv == machine.priv == 2
    assert context.saved_ptbr == host_ptbr == machine.read_csr("ptbr")
    assert context.flags == 1

    record = snapshot.records[0]
    assert record.state == npk.IMAGE_EXITED
    assert record.task_id == task
    assert record.asid == 0
    assert record.exit_code == EXIT_CODE
    assert record.exit_cause == 7
    assert record.exit_ecause == 0
    assert record.launch_count == 1

    tcb = find_task(machine, task)
    assert tcb.state == npk.TASK_STATE_TERMINATED
    assert tcb.exit_status == EXIT_CODE
    assert tcb.entry == load.entry
    assert tcb.arg == 7
    assert tcb.address_space == 0
    assert tcb.flags == 2
    state = npk.decode_task_state(machine)
    assert state.current_task == 0
    assert state.terminated_count >= 1

    exec_state = snapshot.state
    assert exec_state.launch_count == 1
    assert exec_state.exited_count == 1
    assert exec_state.loaded_count == 0
    assert exec_state.exec_count == 1
    npk.validate_exec_snapshot(snapshot)


def test_start_failure_records_guest_trap_reason(image):
    machine = fresh_machine(image)
    # A software trap becomes a VM exit with a non-SCALL cause.
    code = assemble_text("TRAP 5\n")
    code += b"\x00" * ((-len(code)) % 4)
    trapping = encode_nob(image_base=BASE, entry=BASE, image=code)
    load = setup_load(machine, 1600, container=trapping)
    map_execution(machine, load, 1602)
    task = create_exec_task(machine, load, 1604)
    response = exec_call(
        machine, 1605, npk.OP_EXEC_START, image_id=load.image_id, owner=OWNER,
        handle=load.handle, task=task,
    )
    assert_failed(machine, response, 1605, npk.REASON_EXEC_LAUNCH_FAILED,
                  stage=core_api.STAGE_COMMIT)
    snapshot = npk.decode_exec_snapshot(machine)
    assert snapshot.launch_context.exit_cause != 7
    assert snapshot.state.last_reason == npk.REASON_EXEC_LAUNCH_FAILED
    assert find_task(machine, task).state == npk.TASK_STATE_TERMINATED


def test_query_release_and_stale_image_lookup(image):
    machine = fresh_machine(image)
    load = setup_load(machine, 1700)
    map_execution(machine, load, 1702)
    task = create_exec_task(machine, load, 1704)
    start = exec_call(
        machine, 1705, npk.OP_EXEC_START,
        image_id=load.image_id, owner=OWNER, handle=load.handle, task=task,
    )
    assert_complete(machine, start, 1705, result=EXIT_CODE)

    query = exec_call(machine, 1706, npk.OP_EXEC_QUERY,
                      image_id=load.image_id, owner=OWNER)
    assert_complete(machine, query, 1706, result=EXIT_CODE)

    release = exec_call(machine, 1707, npk.OP_EXEC_RELEASE,
                        image_id=load.image_id, owner=OWNER)
    assert_complete(machine, release, 1707, result=load.image_id)
    assert npk.decode_exec_records(machine)[0].state == npk.IMAGE_FREE
    assert npk.decode_exec_state(machine).exec_count == 0

    stale = exec_call(machine, 1708, npk.OP_EXEC_QUERY,
                      image_id=load.image_id, owner=OWNER)
    assert_rejected(machine, stale, 1708, npk.REASON_EXEC_BAD_REQUEST)

    # The released image may be loaded again only after its M4 mappings are
    # removed; executable LOAD does not silently overwrite a live mapping.
    unmap_region(machine, 1709, load.base)
    unmap_region(machine, 1710, STACK)
    reused = exec_call(
        machine, 1711, npk.OP_EXEC_LOAD, owner=OWNER, handle=load.handle,
        source=npk.EXEC_SOURCE_ADDRESS, source_size=load.source_size,
    )
    assert_complete(machine, reused, 1711)
    assert reused.result == load.image_id + 1


def test_image_slot_exhaustion_and_admission_receipt_exhaustion(image):
    machine = fresh_machine(image)
    _physical, region = allocate_region(machine, 1800)
    handle = create_region_object(machine, 1801, region)
    container, _code, _entry = make_user_container()
    size = npk.stage_nob(machine, container)
    kwargs = dict(owner=OWNER, handle=handle,
                  source=npk.EXEC_SOURCE_ADDRESS, source_size=size)

    image_ids = []
    for index in range(npk.EXEC_SLOTS):
        response = exec_call(machine, 1810 + index, npk.OP_EXEC_LOAD, **kwargs)
        assert_complete(machine, response, 1810 + index)
        image_ids.append(response.result)
    assert npk.decode_exec_state(machine).exec_count == npk.EXEC_SLOTS

    exhausted = exec_call(machine, 1820, npk.OP_EXEC_LOAD, **kwargs)
    assert_failed(machine, exhausted, 1820, npk.REASON_EXEC_EXHAUSTED)

    freed = exec_call(machine, 1821, npk.OP_EXEC_RELEASE,
                      image_id=image_ids[0], owner=OWNER)
    assert_complete(machine, freed, 1821, result=image_ids[0])
    reused = exec_call(machine, 1822, npk.OP_EXEC_LOAD, **kwargs)
    assert_complete(machine, reused, 1822)
    reused_release = exec_call(machine, 1823, npk.OP_EXEC_RELEASE,
                               image_id=reused.result, owner=OWNER)
    assert_complete(machine, reused_release, 1823, result=reused.result)

    # Four further LOAD/RELEASE pairs consume the remaining 4 receipt slots.
    for pair in range(2):
        load_id = 1830 + pair * 2
        release_id = load_id + 1
        loaded = exec_call(machine, load_id, npk.OP_EXEC_LOAD, **kwargs)
        assert_complete(machine, loaded, load_id)
        released = exec_call(machine, release_id, npk.OP_EXEC_RELEASE,
                             image_id=loaded.result, owner=OWNER)
        assert_complete(machine, released, release_id, result=loaded.result)

    full = exec_call(machine, 1840, npk.OP_EXEC_LOAD, **kwargs)
    assert full.status == npk.REQUEST_ADMIT_FAIL
    assert full.reason == npk.REASON_EXEC_EXHAUSTED
    assert full.receipt_index == npk.NO_INDEX
    assert npk.decode_exec_state(machine).last_status == npk.REQUEST_ADMIT_FAIL
