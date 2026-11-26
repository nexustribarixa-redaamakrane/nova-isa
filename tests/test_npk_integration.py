"""NPK Milestone 10 cross-subsystem integration tests.

These tests are deliberately broader than the per-milestone suites. They boot the
real kernel once per test module, submit typed operations through the published
M4/M5/M7/M8/M9 entries, and verify shared processing-core evidence, receipt
consistency, resource cleanup, processing-unit release, and boot-state survival.
They do not duplicate every unit test and do not claim preemption, SMP, physical
hardware, native QEMU Nova support, or full process isolation.
"""

from __future__ import annotations

import copy
import re
import runpy
import struct
from pathlib import Path

import pytest

from nova import npk
from nova.assembler import assemble_text
from nova.isa import INT_SOFTWARE
from nova.nob import NOB_MAGIC, NOB_VERSION, NobImage, encode_nob
from nova.npk import core as core_api
from nova.npk import devices as dev_api
from nova.npk import memory as mem_api
from nova.npk import objects as obj_api
from nova.npk import qemu_translator as qt
from nova.npk import syscalls as sys_api
from nova.npk import traps as trap_api

OWNER = 1
OTHER_OWNER = 2
TASK_A = 0x5441534B00000101
TASK_B = 0x5441534B00000102
BAD_TASK = 0x5441534B00000FFF
BASE = 0x9000000000
STACK = BASE + npk.MEMORY_PAGE_SIZE
EXIT_CODE = 42
RWXU = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC | npk.PERM_USER
RWU = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_USER


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m10"))
    )


_BOOTED_MACHINE = None


def fresh_machine(image):
    global _BOOTED_MACHINE
    if _BOOTED_MACHINE is None:
        result = npk.boot_kernel(image)
        assert result.status == 0
        assert result.marker == npk.BOOT_MARKER_VALUE
        _BOOTED_MACHINE = result.machine
    machine = copy.deepcopy(_BOOTED_MACHINE)
    machine.halted = False
    return machine


def u64(machine, address):
    return machine._read_mem(address, 8)


def core_counts(machine):
    core = npk.decode_core_snapshot(machine).core
    return core.admitted, core.completed, core.rejected, core.failed


def assert_core_idle(machine):
    """Check a live post-operation core without imposing boot-only counters."""
    core = npk.decode_core_snapshot(machine).core
    assert core.magic == core_api.CORE_MAGIC
    assert core.version == core_api.CORE_VERSION
    assert core.op_slots == core_api.OP_SNAPSHOT_SLOTS
    assert core.pu_count == 2
    assert core.selftest_status == 0
    assert core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert core.pu_operation == (0, 0)
    return core


def allocate_page(machine, operation_id, *, owner=OWNER):
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
        item for item in mem_api.decode_memory_regions(machine)
        if item.base == response.result and item.state == npk.REGION_ALLOCATED
    )
    return response.result, region.id


def map_page(machine, operation_id, address, region_id, permissions, *, owner=OWNER):
    response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=operation_id,
        owner=owner,
        address=address,
        size=npk.MEMORY_PAGE_SIZE,
        flags=permissions,
        aux=(region_id << 16) | 0,
    )
    assert response.status == npk.REQUEST_OK
    return response


def unmap_page(machine, operation_id, address, *, owner=OWNER):
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
                         type_=npk.OBJECT_TYPE_MEMORY_REGION):
    response = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CREATE,
        operation_id=operation_id,
        owner=owner,
        type=type_,
        permissions=permissions,
        payload=region_id,
    )
    assert response.status == npk.REQUEST_OK
    return obj_api.decode_object_state(machine).last_handle


def find_region(machine, region_id):
    return next(item for item in mem_api.decode_memory_regions(machine)
                if item.id == region_id)


def find_task(machine, task_id):
    return next(task for task in npk.decode_task_tcbs(machine)
                if task.id == task_id and task.state != npk.TASK_STATE_FREE)


def create_task(machine, operation_id, task_id, entry, *, owner=OWNER):
    response = npk.call_task_request(
        machine,
        operation=npk.OP_TASK_CREATE,
        id=operation_id,
        task=task_id,
        arg0=owner,
        arg1=entry,
        arg2=entry,
        arg3=npk.MEMORY_PAGE_SIZE,
        arg4=0,
    )
    assert response.status == npk.REQUEST_OK
    return response


def make_user_container(*, exit_code=EXIT_CODE):
    code = assemble_text(f"LI r1, {exit_code}\nSCALL 0\nHALT\n")
    code += b"\x00" * ((-len(code)) % 4)
    return encode_nob(image_base=BASE, entry=BASE, image=code), code


def load_image(machine, operation_id, *, owner=OWNER):
    _physical, region_id = allocate_page(machine, operation_id)
    stack_physical, stack_region = allocate_page(machine, operation_id + 1,
                                                   owner=owner)
    handle = create_region_object(machine, operation_id + 2, region_id,
                                  owner=owner)
    container, code = make_user_container()
    source_size = npk.stage_nob(machine, container)
    response = npk.call_exec_request(
        machine,
        operation=npk.OP_EXEC_LOAD,
        id=operation_id + 3,
        owner=owner,
        handle=handle,
        source=npk.EXEC_SOURCE_ADDRESS,
        source_size=source_size,
    )
    assert response.status == npk.REQUEST_OK
    npk.validate_complete_exec_receipt(
        npk.decode_exec_receipts(machine)[response.receipt_index]
    )
    return {
        "handle": handle,
        "region_id": region_id,
        "stack_region": stack_region,
        "stack_physical": stack_physical,
        "image_id": response.result,
        "entry": BASE,
        "source_size": source_size,
        "code": code,
    }


def map_execution(machine, operation_id, image_info, *, owner=OWNER):
    map_page(machine, operation_id, BASE, image_info["region_id"], RWXU,
             owner=owner)
    map_page(machine, operation_id + 1, STACK, image_info["stack_region"], RWU,
             owner=owner)


def test_boot_initializes_all_published_subsystems(image):
    result = npk.boot_kernel(image)
    machine = result.machine

    assert result.steps > 0
    assert result.status == 0
    assert result.vbar != 0
    npk.validate_memory_layout()
    obj_api.validate_object_layout()
    trap_api.validate_trap_layout()
    sys_api.validate_syscall_layout()
    dev_api.validate_device_layout()
    npk.validate_exec_layout()

    for address in (
        npk.MEMORY_ENTRY_ADDRESS,
        npk.OBJECT_ENTRY_ADDRESS,
        npk.DEVICE_ENTRY_ADDRESS,
        npk.TASK_ENTRY_ADDRESS,
        npk.EXEC_ENTRY_ADDRESS,
    ):
        assert u64(machine, address) != 0

    npk.validate_memory_state(mem_api.decode_memory_state(machine))
    npk.validate_object_state(obj_api.decode_object_state(machine))
    trap_state = trap_api.decode_trap_state(machine)
    assert trap_state.magic == npk.TRAP_STATE_MAGIC
    assert trap_state.version == npk.TRAP_STATE_VERSION
    assert trap_state.dispatch_count == 0
    syscall_state = sys_api.decode_syscall_state(machine)
    assert syscall_state.magic == npk.SYSCALL_STATE_MAGIC
    assert syscall_state.version == npk.SYSCALL_STATE_VERSION
    assert syscall_state.init_status == 0
    assert syscall_state.dispatch_count == 0
    npk.validate_device_state(dev_api.decode_device_state(machine))
    npk.validate_task_state(npk.decode_task_state(machine))
    npk.validate_exec_state(npk.decode_exec_state(machine))

    assert npk.DEVICE_LAYOUT_LIMIT <= npk.TASK_ENTRY_ADDRESS
    assert npk.TASK_LAYOUT_LIMIT <= npk.EXEC_ENTRY_ADDRESS
    assert npk.EXEC_LAYOUT_LIMIT == npk.PHYSICAL_ARENA_BASE
    assert_core_idle(machine)


def test_boot_evidence_survives_cross_subsystem_activity(image):
    machine = fresh_machine(image)
    marker = machine._read_mem(npk.BOOT_MARKER_ADDRESS, npk.BOOT_MARKER_SIZE)
    boot_receipt = npk.decode_processing_receipt(machine)
    core_before = npk.decode_core_snapshot(machine)

    _physical, region_id = allocate_page(machine, 1000)
    handle = create_region_object(machine, 1001, region_id)
    response = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1002,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert response.status == npk.REQUEST_OK

    assert machine._read_mem(
        npk.BOOT_MARKER_ADDRESS, npk.BOOT_MARKER_SIZE
    ) == marker
    assert npk.decode_processing_receipt(machine) == boot_receipt
    core_after = npk.decode_core_snapshot(machine)
    assert core_after.receipts == core_before.receipts
    assert core_after.core.admitted == core_before.core.admitted + 3
    assert core_after.core.completed == core_before.core.completed + 3
    assert handle != 0
    assert_core_idle(machine)


def test_operations_from_four_subsystems_share_one_lifecycle(image):
    machine = fresh_machine(image)
    before = core_counts(machine)

    memory_response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_ALLOCATE,
        operation_id=1100,
        owner=OWNER,
        size=npk.MEMORY_PAGE_SIZE,
        flags=npk.REGION_TYPE_NORMAL,
        aux=npk.MEMORY_PAGE_SIZE,
    )
    object_response = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CREATE,
        operation_id=1101,
        owner=OWNER,
        type=npk.OBJECT_TYPE_GENERIC,
        permissions=npk.OBJ_PERM_ALL,
    )
    task_response = npk.call_task_request(
        machine,
        operation=npk.OP_TASK_QUERY,
        id=1102,
        task=BAD_TASK,
    )
    device_response = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1103,
        target=101,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )

    assert memory_response.status == npk.REQUEST_OK
    assert object_response.status == npk.REQUEST_OK
    assert task_response.status != npk.REQUEST_OK
    assert task_response.reason == npk.REASON_TASK_NOT_FOUND
    assert device_response.status == npk.REQUEST_OK

    after = core_counts(machine)
    assert after[0] == before[0] + 4
    assert after[1] == before[1] + 3
    assert after[2] == before[2] + 1
    assert after[3] == before[3]

    memory_receipt = mem_api.decode_memory_receipts(
        machine
    )[memory_response.receipt_index]
    object_receipt = obj_api.decode_object_receipts(
        machine
    )[object_response.receipt_index]
    task_receipt = npk.decode_task_receipts(machine)[task_response.receipt_index]
    device_receipt = dev_api.decode_device_receipts(
        machine
    )[device_response.receipt_index]

    for receipt in (memory_receipt, object_receipt, task_receipt, device_receipt):
        core_api.validate_receipt_structure(receipt)
    assert memory_receipt.outcome == core_api.OUTCOME_COMPLETE
    assert object_receipt.outcome == core_api.OUTCOME_COMPLETE
    assert device_receipt.outcome == core_api.OUTCOME_COMPLETE
    assert task_receipt.outcome == core_api.OUTCOME_REJECTED
    assert task_receipt.result == 0
    assert task_receipt.trace == (
        core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE, 0, 0, 0
    )
    assert_core_idle(machine)


def test_memory_object_lifetimes_are_coherent_after_failed_destroy(image):
    machine = fresh_machine(image)
    before = core_counts(machine)

    _physical, region_id = allocate_page(machine, 1200)
    handle = create_region_object(machine, 1201, region_id)
    close = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CLOSE,
        operation_id=1202,
        owner=OWNER,
        handle=handle,
    )
    assert close.status == npk.REQUEST_OK
    map_page(machine, 1203, BASE, region_id, RWXU)

    failed = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_DESTROY,
        operation_id=1204,
        owner=OWNER,
        payload=close.result,
    )
    assert failed.status == npk.REQUEST_FAILED
    assert failed.reason == npk.REASON_OBJECT_STATE
    failed_receipt = obj_api.decode_object_receipts(machine)[
        failed.receipt_index
    ]
    obj_api.validate_failed_object_receipt(
        failed_receipt, stage=core_api.STAGE_COMMIT
    )
    assert find_region(machine, region_id).state == npk.REGION_ALLOCATED
    assert any(header.id == close.result
               for header in obj_api.decode_object_headers(machine))

    unmap_page(machine, 1205, BASE)
    destroyed = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_DESTROY,
        operation_id=1206,
        owner=OWNER,
        payload=close.result,
    )
    assert destroyed.status == npk.REQUEST_OK
    assert destroyed.result == close.result
    assert not any(item.id == region_id
                   for item in mem_api.decode_memory_regions(machine))
    assert not any(header.id == close.result
                   for header in obj_api.decode_object_headers(machine))
    stale = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_DESTROY,
        operation_id=1207,
        owner=OWNER,
        payload=close.result,
    )
    assert stale.status == npk.REQUEST_REJECTED
    assert stale.reason == npk.REASON_OBJECT_BAD_OBJECT
    after = core_counts(machine)
    assert after[3] == before[3] + 1
    assert_core_idle(machine)


def test_sys_yield_integrates_trap_syscall_and_task_state(image):
    machine = fresh_machine(image)
    _physical, region_id = allocate_page(machine, 1300)
    map_page(machine, 1301, BASE, region_id, RWXU)
    create_task(machine, 1302, TASK_A, BASE)
    create_task(machine, 1303, TASK_B, BASE)
    before_core = core_counts(machine)
    before_state = npk.decode_task_state(machine)

    result = sys_api.call_syscall(machine, npk.SYS_YIELD)
    sys_api.validate_syscall_return(result.a0, result.a1)
    sys_api.validate_syscall_frame_publication(result)
    trap_api.validate_trap_frame(result.frame)
    assert result.a0 == 0 and result.a1 == sys_api.ERROR_NONE

    after_state = npk.decode_task_state(machine)
    npk.validate_task_state(after_state)
    assert after_state.dispatch_count == before_state.dispatch_count + 1
    assert after_state.current_task != 0
    assert_core_idle(machine)
    assert core_counts(machine)[0] > before_core[0]

    if result.receipt is not None:
        sys_api.validate_syscall_receipt(
            result.receipt, sys_api.syscall_receipt_expectation(result.state)
        )


def test_sys_exit_terminates_current_task_without_leaving_units_busy(image):
    machine = fresh_machine(image)
    _physical, region_id = allocate_page(machine, 1400)
    map_page(machine, 1401, BASE, region_id, RWXU)
    create_task(machine, 1402, TASK_A, BASE)

    result = sys_api.call_syscall(machine, npk.SYS_EXIT, 7)
    sys_api.validate_syscall_return(result.a0, result.a1)
    state = npk.decode_task_state(machine)
    npk.validate_task_state(state)
    assert state.terminated_count >= 1
    assert not any(task.state == npk.TASK_STATE_RUNNING
                   for task in npk.decode_task_tcbs(machine))
    assert_core_idle(machine)


def test_device_and_timer_operations_preserve_core_state(image):
    machine = fresh_machine(image)
    before = core_counts(machine)

    console = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1500,
        target=110,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert console.status == npk.REQUEST_OK
    timer = npk.call_device_request(
        machine,
        operation=npk.OP_TIMER_CREATE,
        operation_id=1501,
        target=510,
        owner=OWNER,
        kind=npk.DEVICE_KIND_TIMER,
        capability=npk.DEVICE_CAP_TIME,
        args=(64, 0, 0, 0),
    )
    assert timer.status == npk.REQUEST_OK

    unsupported = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1502,
        target=111,
        owner=OWNER,
        kind=npk.DEVICE_KIND_NETWORK,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert unsupported.status == npk.REQUEST_REJECTED
    assert unsupported.reason == npk.REASON_DEVICE_UNSUPPORTED

    interrupt = dev_api.run_interrupt(machine, INT_SOFTWARE)
    dev_api.validate_interrupt_result(interrupt)

    snapshot = npk.decode_device_snapshot(machine)
    assert snapshot.operation_state.device_operations == 1
    assert snapshot.operation_state.timer_operations == 1
    assert interrupt.state.interrupt_count == interrupt.before.interrupt_count + 1
    after = core_counts(machine)
    assert after[0] >= before[0] + 4
    assert_core_idle(machine)


def test_executable_load_launch_and_task_attachment_are_coherent(image):
    machine = fresh_machine(image)
    before = core_counts(machine)
    host_ptbr = machine.read_csr("ptbr")
    image_info = load_image(machine, 1600)
    map_execution(machine, 1603, image_info)
    create_task(machine, 1605, TASK_A, image_info["entry"], owner=OWNER)

    start = npk.call_exec_request(
        machine,
        operation=npk.OP_EXEC_START,
        id=1606,
        image_id=image_info["image_id"],
        owner=OWNER,
        handle=image_info["handle"],
        task=TASK_A,
        asid=0,
        arg=7,
    )
    receipt = npk.decode_exec_receipts(machine)[start.receipt_index]
    npk.validate_complete_exec_receipt(receipt)
    assert start.status == npk.REQUEST_OK
    assert start.result == EXIT_CODE

    snapshot = npk.decode_exec_snapshot(machine)
    npk.validate_exec_snapshot(snapshot)
    assert snapshot.state.launch_count == 1
    assert snapshot.state.exited_count == 1
    assert snapshot.records[0].state == npk.IMAGE_EXITED
    assert snapshot.records[0].task_id == TASK_A
    assert snapshot.launch_context.vpc == image_info["entry"]
    assert snapshot.launch_context.vpriv == 0
    assert snapshot.launch_context.exit_cause == 7
    assert snapshot.launch_context.exit_code == EXIT_CODE
    assert snapshot.launch_context.saved_priv == machine.priv
    assert snapshot.launch_context.saved_ptbr == host_ptbr

    tcb = find_task(machine, TASK_A)
    assert tcb.state == npk.TASK_STATE_TERMINATED
    assert tcb.exit_status == EXIT_CODE
    assert tcb.address_space == 0
    assert_core_idle(machine)
    assert core_counts(machine)[0] > before[0]


def test_failed_executable_load_does_not_publish_partial_state(image):
    machine = fresh_machine(image)
    _physical, region_id = allocate_page(machine, 1700)
    handle = create_region_object(machine, 1701, region_id)
    code = assemble_text("HALT\n")
    code += b"\x00" * ((-len(code)) % 4)
    # Encode a structurally valid v1 header with an entry outside the image;
    # encode_nob rejects it, so build the frozen container bytes directly and
    # let the kernel perform the semantic validation.
    bad_container = (
        NOB_MAGIC
        + struct.pack("<HHQQQ", NOB_VERSION, 0, BASE, BASE + 0x1000,
                      len(code))
        + code
    )
    # stage_nob intentionally validates containers before writing them, so
    # place the invalid bytes in the staging window directly and let the kernel
    # reject the bad entry during typed LOAD validation.
    source_size = len(bad_container)
    for offset in range(0, source_size, 8):
        chunk = bad_container[offset:offset + 8].ljust(8, b"\x00")
        machine._write_mem(
            npk.EXEC_SOURCE_ADDRESS + offset,
            int.from_bytes(chunk, "little"),
            8,
        )

    response = npk.call_exec_request(
        machine,
        operation=npk.OP_EXEC_LOAD,
        id=1702,
        owner=OWNER,
        handle=handle,
        source=npk.EXEC_SOURCE_ADDRESS,
        source_size=source_size,
    )
    assert response.status == npk.REQUEST_REJECTED
    assert response.reason == npk.REASON_EXEC_BAD_ENTRY
    receipt = npk.decode_exec_receipts(machine)[response.receipt_index]
    npk.validate_rejected_exec_receipt(receipt)
    state = npk.decode_exec_state(machine)
    assert state.exec_count == 0 and state.loaded_count == 0
    assert all(record.state == npk.IMAGE_FREE
               for record in npk.decode_exec_records(machine))
    assert_core_idle(machine)


def test_bounded_receipt_exhaustion_does_not_block_other_subsystems(image):
    machine = fresh_machine(image)
    for index in range(npk.TASK_RECEIPT_SLOTS):
        response = npk.call_task_request(
            machine,
            operation=npk.OP_TASK_QUERY,
            id=1800 + index,
            task=BAD_TASK,
        )
        assert response.status != npk.REQUEST_OK
        assert response.reason == npk.REASON_TASK_NOT_FOUND
    task_receipts = npk.decode_task_receipts(machine)
    assert len(task_receipts) == npk.TASK_RECEIPT_SLOTS
    npk.validate_task_receipts(task_receipts)

    extra = npk.call_task_request(
        machine,
        operation=npk.OP_TASK_QUERY,
        id=1900,
        task=BAD_TASK,
    )
    assert extra.reason == npk.REASON_TASK_EXHAUSTED

    physical, region_id = allocate_page(machine, 1901)
    memory_response = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_QUERY,
        operation_id=1902,
        owner=OWNER,
        address=region_id,
    )
    assert memory_response.status == npk.REQUEST_OK
    assert memory_response.result == physical
    mem_api.validate_complete_memory_receipt(
        mem_api.decode_memory_receipts(machine)[memory_response.receipt_index]
    )
    assert find_region(machine, region_id).state == npk.REGION_ALLOCATED
    assert_core_idle(machine)


def test_qemu_harness_layout_and_error_diagnostics_remain_intact():
    layout = qt.QEMU_LAYOUT
    assert layout["shadow"] + layout["shadow_size"] < layout["done"]
    max_table_end = layout["table"] + (layout["shadow_size"] // 4) * 8
    assert max_table_end <= layout["done"]
    assert layout["error_pc"] > layout["steps"]
    assert layout["error_word"] > layout["error_pc"]

    bad_pc = 0x2000
    image = NobImage(
        image_base=bad_pc,
        entry=bad_pc,
        image=struct.pack("<I", 0xFFFFFFFF),
    )
    _harness, code = qt._NovaTranslator(image).translate()
    assert b"\x48\xbf" + struct.pack("<Q", bad_pc) in code


def test_phase1_toolchain_builds_the_same_frozen_kernel_contract(tmp_path):
    first = npk.build_kernel(tmp_path / "first")
    second = npk.build_kernel(tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    image = npk.load_kernel_image(first)
    npk.validate_kernel_image(image)
    assert image.image_base == npk.KERNEL_IMAGE_BASE
    assert image.entry == npk.KERNEL_IMAGE_BASE
    assert image.flags == 0
    assert npk.SYS_EXIT == 0 and npk.SYS_YIELD == 7
    assert struct.unpack_from("<I", image.image, 0)[0] != 0


def test_public_exports_and_validator_discovery_are_complete():
    root = Path(__file__).resolve().parents[1]
    assert [name for name in npk.__all__ if not hasattr(npk, name)] == []
    assert [name for name in core_api.__all__
            if not hasattr(core_api, name)] == []

    module = runpy.run_path(str(root / "scripts" / "npk_validate.py"))
    discovered = sorted(
        f"tests/{path.name}"
        for path in (root / "tests").glob("test_npk*.py")
    )
    assert discovered
    assert module["NPK_TESTS"] == discovered
    assert "tests/test_npk_integration.py" in discovered


def test_python_and_c_processing_constants_agree():
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (root / "nova" / "npk" / "src").iterdir()
        if path.is_file()
    )
    c_values = {
        match.group(1): int(match.group(2), 0)
        for match in re.finditer(
            r"^#define\s+(NPK_(?:OP|REASON)_[A-Z0-9_]+)\s+(\d+)\s*$",
            source,
            re.MULTILINE,
        )
    }
    names = [
        name for name in core_api.__all__
        if name.startswith(("OP_", "REASON_")) and not name.startswith("OP_SNAPSHOT")
    ]
    assert names
    for name in names:
        assert c_values.get(f"NPK_{name}") == getattr(core_api, name), name


def test_exec_register_save_layout_literals_match_python():
    root = Path(__file__).resolve().parents[1]
    expected = f"0x{npk.EXEC_REGISTER_SAVE_ADDRESS:X}"
    c_source = (root / "nova" / "npk" / "src" / "exec.c").read_text(
        encoding="utf-8"
    )
    asm_source = (root / "nova" / "npk" / "src" / "exec.s").read_text(
        encoding="utf-8"
    )
    assert expected in c_source
    assert expected in asm_source
