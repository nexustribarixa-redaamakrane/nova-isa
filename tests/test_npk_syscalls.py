"""Milestone 6 syscall and privilege-context processing tests.

Every call enters through the real Milestone 3 ``SCALL`` trap vector and is
processed as a typed operation on the shared five-stage core. Tests validate the
saved frame, dispatch state, dedicated syscall receipt, and any child M4/M5
receipts; a live ``a0`` return alone is never treated as evidence.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.isa import PRIVILEGE_LEVELS, TIME
from nova.npk import core as core_api

CODE_ADDRESS = 0xA0000
OWNER = 1
MAP_VA = 0x8000000000
MAP_FLAGS = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_USER
SUCCESS = 0
FAILURE = 0xFFFFFFFFFFFFFFFF


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m6"))
    )


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def check_call(machine, outcome, *, number=None, a0=None, a1=SUCCESS,
               reason=core_api.REASON_NONE, receipt_result=None,
               stage=None, syscall_state=None, trace=None):
    """Validate one syscall result jointly against all observable evidence."""
    npk.validate_syscall_layout()
    npk.validate_syscall_state(outcome.state)
    npk.validate_syscall_return(outcome.a0, outcome.a1)
    npk.validate_syscall_frame_publication(outcome)

    if number is not None:
        assert outcome.number == number
        assert outcome.state.last_number == number
    if a0 is not None:
        assert outcome.a0 == a0
    assert outcome.a1 == a1
    assert outcome.state.last_error == a1
    assert outcome.frame.handler_priv == PRIVILEGE_LEVELS["machine"]
    assert machine.read_register("a0") == outcome.a0
    assert machine.read_register("a1") == outcome.a1
    assert machine.pc == CODE_ADDRESS + 4
    assert machine.priv == PRIVILEGE_LEVELS["user"]

    assert outcome.receipt is not None
    assert outcome.state.last_receipt_index < npk.SYSCALL_RECEIPT_SLOTS
    expected = npk.syscall_receipt_expectation(
        outcome.state,
        outcome=(
            core_api.OUTCOME_COMPLETE
            if a1 == SUCCESS else core_api.OUTCOME_REJECTED
        ),
        result=(
            outcome.a0 if a1 == SUCCESS
            else SUCCESS if receipt_result is None else receipt_result
        ),
        reason=reason,
        stage=(
            core_api.STAGE_COMPLETE if a1 == SUCCESS else core_api.STAGE_VALIDATE
        ) if stage is None else stage,
        syscall_state=(
            core_api.STATE_COMPLETE if a1 == SUCCESS else core_api.STATE_REJECTED
        ) if syscall_state is None else syscall_state,
        trace=(
            core_api.EXPECTED_STAGE_TRACE if a1 == SUCCESS
            else (core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE, 0, 0, 0)
        ) if trace is None else trace,
    )
    npk.validate_syscall_receipt(outcome.receipt, expected)
    assert outcome.receipt.id == npk.SYSCALL_ID_BASE + outcome.state.dispatch_count
    assert outcome.receipt.reserved == (outcome.number, a1)
    assert outcome.state.last_outcome == expected.outcome
    assert outcome.state.last_reason == reason
    if a1 == SUCCESS:
        assert outcome.state.last_result == outcome.a0
        assert outcome.receipt.result == outcome.a0
    else:
        assert outcome.a0 == FAILURE
        assert outcome.state.last_result == FAILURE
    return outcome.receipt


def setup_mapped_page(machine, *, operation_id_base=100):
    """Allocate and map one owner-1 user page through the real M4 entry."""
    allocation = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_ALLOCATE,
        operation_id=operation_id_base,
        owner=OWNER,
        size=npk.MEMORY_PAGE_SIZE,
        flags=npk.REGION_TYPE_NORMAL,
        aux=npk.MEMORY_PAGE_SIZE,
    )
    assert allocation.status == npk.REQUEST_OK
    region = next(item for item in npk.decode_memory_regions(machine)
                  if item.base == allocation.result and
                  item.state == npk.REGION_ALLOCATED)
    mapping = npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=operation_id_base + 1,
        owner=OWNER,
        address=MAP_VA,
        size=npk.MEMORY_PAGE_SIZE,
        flags=MAP_FLAGS,
        aux=(region.id << 16) | 0,
    )
    assert mapping.status == npk.REQUEST_OK
    return allocation.result


def write_physical_bytes(machine, physical, data):
    """Place raw bytes at a physical address before a user-pointer syscall."""
    for offset, value in enumerate(data):
        address = physical + offset
        word = machine._read_mem(address & ~7, 8)
        shift = (address & 7) * 8
        word = (word & ~(0xFF << shift)) | (value << shift)
        machine._write_mem(address & ~7, word, 8)


def test_milestone6_layout_regions_are_bounded_and_disjoint():
    assert npk.SYSCALL_STATE_ADDRESS == 0x9FA10
    assert npk.SYSCALL_STATE_SIZE == 128
    assert npk.SYSCALL_RECEIPT_TABLE_ADDRESS == 0x9FA90
    assert npk.SYSCALL_RECEIPT_SLOTS == 8
    assert npk.SYSCALL_WRITE_LOG_ADDRESS == 0x9FE90
    assert npk.SYSCALL_WRITE_LOG_SIZE == 256
    assert npk.SYSCALL_SCRATCH_ADDRESS == 0x9FF90
    assert npk.SYSCALL_SCRATCH_SIZE == 64

    m5_ext_end = (npk.OBJECT_RECEIPT_EXT_ADDRESS
                  + npk.OBJECT_RECEIPT_SLOTS * npk.OBJECT_RECEIPT_EXT_SIZE)
    state_end = npk.SYSCALL_STATE_ADDRESS + npk.SYSCALL_STATE_SIZE
    receipt_end = (npk.SYSCALL_RECEIPT_TABLE_ADDRESS
                   + npk.RECEIPT_SIZE * npk.SYSCALL_RECEIPT_SLOTS)
    write_end = npk.SYSCALL_WRITE_LOG_ADDRESS + npk.SYSCALL_WRITE_LOG_SIZE
    scratch_end = npk.SYSCALL_SCRATCH_ADDRESS + npk.SYSCALL_SCRATCH_SIZE

    assert m5_ext_end == npk.SYSCALL_STATE_ADDRESS
    assert state_end <= npk.SYSCALL_RECEIPT_TABLE_ADDRESS
    assert receipt_end <= npk.SYSCALL_WRITE_LOG_ADDRESS
    assert write_end <= npk.SYSCALL_SCRATCH_ADDRESS
    assert scratch_end < npk.SYSCALL_LAYOUT_LIMIT
    npk.validate_syscall_layout()


def test_milestone6_constants_extend_but_do_not_change_the_abi():
    assert npk.OP_SYSCALL_SERVICE == 21
    assert core_api.OP_OBJECT_TYPE_QUERY == 20
    assert (npk.REASON_SYSCALL_BAD_FRAME, npk.REASON_SYSCALL_BAD_NUMBER,
            npk.REASON_SYSCALL_CHILD) == (46, 48, 56)
    assert (npk.ERR_OK, npk.ERR_BAD_ARGUMENT, npk.ERR_UNSUPPORTED,
            npk.ERR_NOT_FOUND, npk.ERR_NO_RESOURCE) == (0, 1, 2, 3, 4)
    assert npk.SYSCALL_ID_BASE == 0x53590000
    assert npk.OBJECT_REQUEST_ID_BASE == 0x4F420000
    assert npk.SYSCALL_STATE_MAGIC == 0x4E504B53
    assert npk.SYSCALL_STATE_VERSION == 1
    assert npk.SYSCALL_COUNT == 8


def test_syscall_initialization_preserves_milestone1_to_milestone5_evidence(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    state = npk.decode_syscall_state(machine)
    assert state.magic == npk.SYSCALL_STATE_MAGIC
    assert state.version == npk.SYSCALL_STATE_VERSION
    assert state.init_status == 0
    assert state.dispatch_count == 0
    assert state.flags == 0
    assert state.write_log_count == 0
    npk.validate_core_state(npk.decode_core_snapshot(machine).core)


def test_sys_time_returns_time_and_publishes_a_complete_receipt(image):
    machine = fresh_machine(image)
    before = machine._read_csr(TIME)
    outcome = npk.call_syscall(machine, npk.SYS_TIME)
    after = machine._read_csr(TIME)
    receipt = check_call(machine, outcome, number=npk.SYS_TIME)

    assert before < outcome.a0 < after
    assert outcome.state.last_arg_count == 0
    assert outcome.state.last_op_slot < 4
    assert outcome.receipt is receipt
    assert npk.decode_syscall_receipts(machine)[
        outcome.state.last_receipt_index] == receipt


def test_exit_and_yield_use_cooperative_task_scheduler(image):
    machine = fresh_machine(image)
    first = npk.call_syscall(machine, npk.SYS_EXIT, 7)
    check_call(machine, first, number=npk.SYS_EXIT, a0=0)
    assert first.state.flags & npk.FLAG_EXIT_REQUESTED

    second = npk.call_syscall(machine, npk.SYS_YIELD)
    check_call(machine, second, number=npk.SYS_YIELD, a0=0)
    assert second.state.flags & npk.FLAG_YIELD_REQUESTED
    assert second.state.flags & npk.FLAG_EXIT_REQUESTED
    assert second.state.flags & npk.FLAG_HANDLER_CONTEXT

    task_state = npk.decode_task_state(machine)
    npk.validate_task_state(task_state)
    assert task_state.task_count >= 1
    assert task_state.current_task != 0

    core = npk.decode_core_snapshot(machine).core
    assert core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert core.pu_operation == (0, 0)
    assert not machine.halted


def test_invalid_syscall_number_is_rejected_as_unsupported(image):
    machine = fresh_machine(image)
    outcome = npk.call_syscall(machine, 9)
    check_call(
        machine, outcome, number=9, a0=FAILURE, a1=npk.ERR_UNSUPPORTED,
        reason=npk.REASON_SYSCALL_BAD_NUMBER,
    )
    assert outcome.state.last_arg_count == 0xFF


def test_unsupported_sys_read_is_rejected_before_execution(image):
    machine = fresh_machine(image)
    outcome = npk.call_syscall(machine, npk.SYS_READ, 0, 0x1000, 4)
    check_call(
        machine, outcome, number=npk.SYS_READ, a0=FAILURE,
        a1=npk.ERR_UNSUPPORTED, reason=npk.REASON_SYSCALL_UNSUPPORTED,
    )
    assert npk.decode_write_log(machine) == b""


def test_sys_write_validates_fd_and_bounded_count(image):
    machine = fresh_machine(image)
    bad_fd = npk.call_syscall(machine, npk.SYS_WRITE, 3, MAP_VA, 1)
    check_call(
        machine, bad_fd, number=npk.SYS_WRITE, a0=FAILURE,
        a1=npk.ERR_BAD_ARGUMENT, reason=npk.REASON_SYSCALL_BAD_ARGUMENT,
    )

    too_large = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, 65)
    check_call(
        machine, too_large, number=npk.SYS_WRITE, a0=FAILURE,
        a1=npk.ERR_BAD_ARGUMENT, reason=npk.REASON_SYSCALL_BAD_ARGUMENT,
    )
    assert npk.decode_syscall_state(machine).write_log_count == 0


def test_unmapped_user_pointer_is_rejected_without_writing_sink(image):
    machine = fresh_machine(image)
    outcome = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, 3)
    check_call(
        machine, outcome, number=npk.SYS_WRITE, a0=FAILURE,
        a1=npk.ERR_BAD_ARGUMENT, reason=npk.REASON_SYSCALL_BAD_POINTER,
    )
    assert npk.decode_syscall_state(machine).write_log_count == 0
    assert npk.decode_write_log(machine) == b""


def test_sys_write_uses_m4_user_mapping_and_bounded_kernel_sink(image):
    machine = fresh_machine(image)
    physical = setup_mapped_page(machine)
    write_physical_bytes(machine, physical, b"NovaM6")

    outcome = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, 6)
    check_call(machine, outcome, number=npk.SYS_WRITE, a0=6)

    state = npk.decode_syscall_state(machine)
    assert state.write_log_count == 6
    assert npk.decode_write_log(machine) == b"NovaM6"
    assert outcome.receipt.result == 6
    assert b"NovaM6" not in npk.decode_scratch(machine)


def test_write_log_capacity_is_enforced_across_calls(image):
    machine = fresh_machine(image)
    physical = setup_mapped_page(machine)
    write_physical_bytes(machine, physical, b"A" * 64)

    for filled in range(4):
        written = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, 64)
        check_call(machine, written, number=npk.SYS_WRITE, a0=64)
        assert npk.decode_syscall_state(machine).write_log_count == (filled + 1) * 64

    overflow = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, 1)
    check_call(
        machine, overflow, number=npk.SYS_WRITE, a0=FAILURE,
        a1=npk.ERR_NO_RESOURCE, reason=npk.REASON_SYSCALL_NO_RESOURCE,
    )
    assert npk.decode_syscall_state(machine).write_log_count == 256
    assert npk.decode_write_log(machine) == b"A" * 256


def test_object_syscalls_use_real_m5_create_query_and_destroy(image):
    machine = fresh_machine(image)
    size = 0x1234

    created = npk.call_syscall(machine, npk.SYS_OBJ_CREATE, size)
    check_call(machine, created, number=npk.SYS_OBJ_CREATE)
    handle = created.a0
    assert handle != FAILURE
    owner, _, index = npk.decode_handle(handle)
    assert owner == OWNER
    handle_slot = npk.decode_handle_slots(machine)[
        (owner - 1) * npk.HANDLE_SLOTS + index]
    assert handle_slot.state == npk.HANDLE_STATE_ACTIVE
    created_receipt = npk.decode_object_receipts(machine)[
        created.state.child_receipt]
    assert created_receipt.type == npk.OP_OBJECT_CREATE
    assert created_receipt.outcome == core_api.OUTCOME_COMPLETE

    queried = npk.call_syscall(machine, npk.SYS_OBJ_SIZE, handle)
    check_call(machine, queried, number=npk.SYS_OBJ_SIZE, a0=size)
    query_receipt = npk.decode_object_receipts(machine)[
        queried.state.child_receipt]
    assert query_receipt.type == npk.OP_OBJECT_QUERY
    assert query_receipt.outcome == core_api.OUTCOME_COMPLETE

    destroyed = npk.call_syscall(machine, npk.SYS_OBJ_DESTROY, handle)
    check_call(machine, destroyed, number=npk.SYS_OBJ_DESTROY, a0=0)
    close_index, destroy_index = npk.decode_child_receipt_indexes(
        destroyed.state)
    close_receipt = npk.decode_object_receipts(machine)[close_index]
    destroy_receipt = npk.decode_object_receipts(machine)[destroy_index]
    assert close_receipt.type == npk.OP_OBJECT_CLOSE
    assert destroy_receipt.type == npk.OP_OBJECT_DESTROY
    assert close_receipt.outcome == core_api.OUTCOME_COMPLETE
    assert destroy_receipt.outcome == core_api.OUTCOME_COMPLETE
    assert npk.decode_object_state(machine).handle_count == 0
    assert not any(item.state == npk.HANDLE_STATE_ACTIVE
                   for item in npk.decode_handle_slots(machine))


def test_object_handle_validation_rejects_unknown_references(image):
    machine = fresh_machine(image)
    stale = npk.encode_handle(OWNER, 1, 63)
    outcome = npk.call_syscall(machine, npk.SYS_OBJ_DESTROY, stale)
    check_call(
        machine, outcome, number=npk.SYS_OBJ_DESTROY, a0=FAILURE,
        a1=npk.ERR_NOT_FOUND, reason=npk.REASON_SYSCALL_NOT_FOUND,
    )
    assert outcome.state.child_receipt == core_api.NO_INDEX


def test_dedicated_receipt_log_is_bounded_without_silent_overwrite(image):
    machine = fresh_machine(image)
    recorded = []

    for expected_index in range(npk.SYSCALL_RECEIPT_SLOTS):
        outcome = npk.call_syscall(machine, npk.SYS_YIELD)
        check_call(machine, outcome, number=npk.SYS_YIELD, a0=0)
        assert outcome.state.last_receipt_index == expected_index
        assert outcome.receipt.id == npk.SYSCALL_ID_BASE + expected_index + 1
        recorded.append(outcome.receipt)

    full = npk.decode_syscall_receipts(machine)
    assert full == tuple(recorded)
    assert sum(bool(item.magic) for item in full) == npk.SYSCALL_RECEIPT_SLOTS

    overflow = npk.call_syscall(machine, npk.SYS_YIELD)
    npk.validate_syscall_state(overflow.state)
    assert overflow.receipt is None
    assert overflow.a0 == FAILURE
    assert overflow.a1 == npk.ERR_NO_RESOURCE
    assert overflow.state.last_status == npk.STATUS_ADMIT_FAIL
    assert overflow.state.last_reason == npk.REASON_SYSCALL_NO_RESOURCE
    assert overflow.state.last_receipt_index == core_api.NO_INDEX
    assert npk.decode_syscall_receipts(machine) == full


def test_npk_syscall_path_does_not_use_the_host_reference_dispatcher(
    image, monkeypatch
):
    import nova.syscalls

    def forbidden(*_args, **_kwargs):
        pytest.fail("NPK syscall path called the host reference dispatcher")

    monkeypatch.setattr(nova.syscalls, "dispatch_syscall", forbidden)
    machine = fresh_machine(image)
    outcome = npk.call_syscall(machine, npk.SYS_TIME)
    check_call(machine, outcome, number=npk.SYS_TIME)
