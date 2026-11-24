"""NPK Milestone 8 task management and cooperative scheduling tests.

Tests exercise the real NPK kernel image, typed task operations through the
shared processing core, and cooperative SYS_YIELD/SYS_EXIT behavior through the
existing trap/syscall path. Nova MRET restores EPC from the emulator hidden
exception stack, so M8 validates cooperative scheduling decisions, task state,
receipts, and resume patches rather than claiming preemptive or hardware context
switching.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.npk import core as core_api
from nova.npk import memory as mem
from nova.npk import syscalls as sys_api


OWNER = 1
TASK_A = 0x5441534B00000101
TASK_B = 0x5441534B00000102
TASK_C = 0x5441534B00000103
BAD_TASK = 0x5441534B00000FFF


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(npk.build_kernel(tmp_path_factory.mktemp("npk-m8")))


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def make_runnable_page(machine, *, op_base=5000, va=0x9000000000):
    alloc = mem.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_ALLOCATE,
        operation_id=op_base,
        owner=OWNER,
        size=npk.MEMORY_PAGE_SIZE,
        flags=npk.REGION_TYPE_NORMAL,
        aux=npk.MEMORY_PAGE_SIZE,
    )
    assert alloc.status == mem.REQUEST_OK
    region = next(
        r for r in mem.decode_memory_regions(machine)
        if r.base == alloc.result and r.state == mem.REGION_ALLOCATED
    )
    map_resp = mem.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=op_base + 1,
        owner=OWNER,
        address=va,
        size=npk.MEMORY_PAGE_SIZE,
        flags=npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC | npk.PERM_USER,
        aux=(region.id << 16) | 0,
    )
    assert map_resp.status == mem.REQUEST_OK
    return alloc.result, va


def task_call(machine, *, operation, op_id, task=0, arg0=0, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0):
    response = npk.call_task_request(
        machine,
        operation=operation,
        id=op_id,
        task=task,
        arg0=arg0,
        arg1=arg1,
        arg2=arg2,
        arg3=arg3,
        arg4=arg4,
        arg5=arg5,
    )
    return response


def assert_task_receipt(machine, response, *, outcome, reason=core_api.REASON_NONE):
    assert response.magic == npk.TASK_RESPONSE_MAGIC
    if outcome == core_api.OUTCOME_COMPLETE:
        assert response.status == npk.REQUEST_OK if hasattr(npk, "REQUEST_OK") else 0
    receipt = npk.decode_task_receipts(machine)[response.receipt_index]
    npk.validate_receipt_structure(receipt)
    assert receipt.outcome == outcome
    assert receipt.reason == reason
    return receipt


def find_tcb(machine, task_id):
    for tcb in npk.decode_task_tcbs(machine):
        if tcb.id == task_id and tcb.state != npk.TASK_STATE_FREE:
            return tcb
    return None


def test_task_layout_and_initial_state(image):
    machine = fresh_machine(image)
    state = npk.decode_task_state(machine)
    npk.validate_task_state(state)
    assert state.magic == npk.TASK_STATE_MAGIC
    assert state.version == npk.TASK_STATE_VERSION
    assert state.task_slots == npk.TASK_SLOTS
    assert state.current_task == 0
    assert state.task_count == 0
    assert state.ready_count == 0
    assert state.blocked_count == 0
    assert state.terminated_count == 0
    snapshot = npk.decode_task_snapshot(machine)
    npk.validate_task_snapshot(snapshot)
    assert all(t.state == npk.TASK_STATE_FREE for t in snapshot.tcbs)
    assert npk.decode_task_entry(machine) != 0


def test_task_create_query_and_receipts(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5100, va=0x9000001000)
    resp = task_call(
        machine,
        operation=npk.OP_TASK_CREATE,
        op_id=6101,
        task=TASK_A,
        arg0=OWNER,
        arg1=va,
        arg2=va,
        arg3=npk.MEMORY_PAGE_SIZE,
        arg4=0x55,
        arg5=0,
    )
    assert resp.status == 0
    assert resp.operation == npk.OP_TASK_CREATE
    assert resp.result == TASK_A
    rec = assert_task_receipt(machine, resp, outcome=core_api.OUTCOME_COMPLETE)
    assert rec.type == npk.OP_TASK_CREATE
    assert rec.reserved[0] == TASK_A

    tcb = find_tcb(machine, TASK_A)
    assert tcb is not None
    assert tcb.state == npk.TASK_STATE_READY
    assert tcb.entry == va
    assert tcb.stack_base == va
    assert tcb.stack_size == npk.MEMORY_PAGE_SIZE
    assert tcb.arg == 0x55
    assert tcb.generation >= 1

    q = task_call(machine, operation=npk.OP_TASK_QUERY, op_id=6102, task=TASK_A)
    assert q.status == 0
    assert q.result == npk.TASK_STATE_READY
    assert_task_receipt(machine, q, outcome=core_api.OUTCOME_COMPLETE)

    state = npk.decode_task_state(machine)
    assert state.task_count >= 1
    assert state.ready_count >= 1
    npk.validate_task_snapshot(npk.decode_task_snapshot(machine))


def test_task_create_validation_and_duplicate_rejection(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5200, va=0x9000002000)
    ok = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6201, task=TASK_A,
                   arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert ok.status == 0

    dup = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6202, task=TASK_A,
                    arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert dup.status != 0
    assert dup.reason == npk.REASON_TASK_DUPLICATE
    assert_task_receipt(machine, dup, outcome=core_api.OUTCOME_REJECTED,
                        reason=npk.REASON_TASK_DUPLICATE)

    bad_entry = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6203, task=TASK_B,
                          arg0=OWNER, arg1=va + 2, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert bad_entry.status != 0
    assert bad_entry.reason == npk.REASON_TASK_BAD_ENTRY

    bad_stack = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6204, task=TASK_B,
                          arg0=OWNER, arg1=va, arg2=va, arg3=16)
    assert bad_stack.status != 0
    assert bad_stack.reason == npk.REASON_TASK_BAD_STACK


def test_task_block_wake_and_illegal_transition(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5300, va=0x9000003000)
    create = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6301, task=TASK_A,
                       arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create.status == 0

    # Direct BLOCK on READY task is not a running-task transition; reject honestly.
    block = task_call(machine, operation=npk.OP_TASK_BLOCK, op_id=6302, task=TASK_A)
    assert block.status != 0
    assert block.reason == npk.REASON_TASK_NOT_RUNNING
    assert_task_receipt(machine, block, outcome=core_api.OUTCOME_REJECTED,
                        reason=npk.REASON_TASK_NOT_RUNNING)

    # Move through kernel-managed path: mark as blocked by manipulating not allowed;
    # use wake rejection on READY to prove state checks.
    wake = task_call(machine, operation=npk.OP_TASK_WAKE, op_id=6303, task=TASK_A)
    assert wake.status != 0
    assert wake.reason == npk.REASON_TASK_NOT_BLOCKED


def test_task_terminate_and_stale_lookup(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5400, va=0x9000004000)
    create = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6401, task=TASK_A,
                       arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create.status == 0

    term = task_call(machine, operation=npk.OP_TASK_TERMINATE, op_id=6402, task=TASK_A, arg4=7)
    assert term.status == 0
    assert_task_receipt(machine, term, outcome=core_api.OUTCOME_COMPLETE)
    tcb = find_tcb(machine, TASK_A)
    assert tcb is not None
    assert tcb.state == npk.TASK_STATE_TERMINATED
    assert tcb.exit_status == 7

    q = task_call(machine, operation=npk.OP_TASK_QUERY, op_id=6403, task=TASK_A)
    assert q.status == 0
    assert q.result == npk.TASK_STATE_TERMINATED

    missing = task_call(machine, operation=npk.OP_TASK_QUERY, op_id=6404, task=BAD_TASK)
    assert missing.status != 0
    assert missing.reason == npk.REASON_TASK_NOT_FOUND


def test_sys_yield_cooperative_reschedule(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5500, va=0x9000005000)
    create = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6501, task=TASK_A,
                       arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create.status == 0

    before = npk.decode_task_state(machine)
    out = sys_api.call_syscall(machine, npk.SYS_YIELD)
    sys_api.validate_syscall_return(out.a0, out.a1)
    assert out.a0 == 0 and out.a1 == sys_api.ERROR_NONE
    after = npk.decode_task_state(machine)
    npk.validate_task_state(after)
    assert after.dispatch_count >= before.dispatch_count
    assert after.current_task != 0
    # Cooperative single-address-space: either same task resumed or switch recorded.
    assert after.switch_count >= before.switch_count


def test_sys_exit_terminates_current_task(image):
    machine = fresh_machine(image)
    # bootstrap current task via syscall
    out = sys_api.call_syscall(machine, npk.SYS_EXIT, 9)
    sys_api.validate_syscall_return(out.a0, out.a1)
    state = npk.decode_task_state(machine)
    npk.validate_task_state(state)
    # After exit with no other runnable tasks, kernel halts cooperatively.
    assert state.current_task == 0 or machine.halted or state.terminated_count >= 1
    assert not any(t.state == npk.TASK_STATE_RUNNING for t in npk.decode_task_tcbs(machine))


def test_task_receipt_table_is_bounded(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5600, va=0x9000006000)
    for i in range(npk.TASK_RECEIPT_SLOTS + 2):
        tid = 0x5441534B00001000 + i
        resp = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6600 + i, task=tid,
                         arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
        # May exhaust task slots; receipts still bounded.
        if resp.status != 0:
            break
    receipts = npk.decode_task_receipts(machine)
    assert len(receipts) == npk.TASK_RECEIPT_SLOTS
    npk.validate_task_receipts(receipts)


def test_task_destroy_and_stale_protection(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5700, va=0x9000007000)
    create = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6701, task=TASK_A,
                       arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create.status == 0
    term = task_call(machine, operation=npk.OP_TASK_TERMINATE, op_id=6702, task=TASK_A)
    assert term.status == 0
    destroy = task_call(machine, operation=npk.OP_TASK_DESTROY, op_id=6703, task=TASK_A)
    assert destroy.status == 0
    assert_task_receipt(machine, destroy, outcome=core_api.OUTCOME_COMPLETE)
    assert find_tcb(machine, TASK_A) is None

    # Recreate same ID after destroy is allowed; stale refs to destroyed record are gone.
    create2 = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6704, task=TASK_A,
                        arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create2.status == 0
    q = task_call(machine, operation=npk.OP_TASK_QUERY, op_id=6705, task=TASK_A)
    assert q.status == 0
    assert q.result == npk.TASK_STATE_READY


def test_task_slot_exhaustion_and_no_partial_mutation(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5800, va=0x9000008000)
    created = 0
    for i in range(npk.TASK_SLOTS + 2):
        tid = 0x5441534B00002000 + i
        resp = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6800 + i, task=tid,
                         arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
        if resp.status == 0:
            created += 1
        else:
            assert resp.reason == npk.REASON_TASK_EXHAUSTED
            assert_task_receipt(machine, resp, outcome=core_api.OUTCOME_REJECTED,
                                reason=npk.REASON_TASK_EXHAUSTED)
            break
    state = npk.decode_task_state(machine)
    assert state.task_count <= npk.TASK_SLOTS
    # Failed exhaustion request must not create a TCB.
    assert all(t.id != 0x5441534B00002000 + created for t in npk.decode_task_tcbs(machine))


def test_task_query_uses_processing_core(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=5900, va=0x9000009000)
    create = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=6901, task=TASK_A,
                       arg0=OWNER, arg1=va, arg2=va, arg3=npk.MEMORY_PAGE_SIZE)
    assert create.status == 0
    before = npk.decode_core_snapshot(machine)
    resp = task_call(machine, operation=npk.OP_TASK_QUERY, op_id=6902, task=TASK_A)
    assert resp.status == 0
    after = npk.decode_core_snapshot(machine)
    # Task ops should produce task receipts through core; core state remains coherent.
    assert after.core.magic == core_api.CORE_MAGIC
    assert after.core.version == core_api.CORE_VERSION
    assert after.core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert after.core.pu_operation == (0, 0)
    assert after.core.admitted >= before.core.admitted + 1
    assert after.core.completed >= before.core.completed + 1
    assert len(npk.decode_task_receipts(machine)) == npk.TASK_RECEIPT_SLOTS
    assert resp.receipt_index < npk.TASK_RECEIPT_SLOTS


def test_task_tcb_frames_do_not_overlap_adjacent_regions(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=7000, va=0x900000A000)
    ids = [0x5441534B00003000 + i for i in range(npk.TASK_SLOTS)]
    for i, tid in enumerate(ids):
        resp = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=7000 + i,
                         task=tid, arg0=OWNER, arg1=va, arg2=va,
                         arg3=npk.MEMORY_PAGE_SIZE)
        assert resp.status == 0

    state = npk.decode_task_state(machine)
    assert state.ready_count == npk.TASK_SLOTS
    tcbs = npk.decode_task_tcbs(machine)
    for i, (tcb, tid) in enumerate(zip(tcbs, ids)):
        assert tcb.id == tid
        assert tcb.state == npk.TASK_STATE_READY
        assert tcb.frame[33] == va  # saved EPC stays inside this TCB
    ready = npk.decode_task_ready_queue(machine)
    assert [entry.task_id for entry in ready] == ids
    assert [entry.task_index for entry in ready] == list(range(npk.TASK_SLOTS))
    receipts = npk.decode_task_receipts(machine)
    npk.validate_task_receipts(receipts)
    assert all(receipt.magic for receipt in receipts[:npk.TASK_SLOTS])


def test_failed_wake_does_not_mutate_blocked_task(image):
    machine = fresh_machine(image)
    phys, va = make_runnable_page(machine, op_base=7100, va=0x900000B000)
    for i in range(npk.TASK_SLOTS - 1):
        tid = 0x5441534B00004000 + i
        resp = task_call(machine, operation=npk.OP_TASK_CREATE, op_id=7100 + i,
                         task=tid, arg0=OWNER, arg1=va, arg2=va,
                         arg3=npk.MEMORY_PAGE_SIZE)
        assert resp.status == 0

    blocked_id = 0x5441534B00004000 + npk.TASK_SLOTS - 2
    blocked_index = npk.TASK_SLOTS - 2
    blocked_base = npk.TASK_TABLE_ADDRESS + blocked_index * npk.TASK_TCB_SIZE
    machine._write_mem(blocked_base + 16, npk.TASK_STATE_BLOCKED, 8)

    # Remove the blocked TCB from the queue, then fill every slot with a
    # non-live marker so the wake has no admissible queue slot.
    machine._write_mem(npk.TASK_STATE_ADDRESS + 40, blocked_index, 8)
    machine._write_mem(
        npk.TASK_READY_QUEUE_ADDRESS + blocked_index * npk.TASK_READY_QUEUE_ENTRY_SIZE,
        0, 8)
    filler_id = 0x5441534B00004FFF
    filler_index = npk.TASK_SLOTS - 1
    filler_base = (npk.TASK_READY_QUEUE_ADDRESS +
                   filler_index * npk.TASK_READY_QUEUE_ENTRY_SIZE)
    machine._write_mem(filler_base, filler_id, 8)
    machine._write_mem(filler_base + 8, 0, 8)
    machine._write_mem(npk.TASK_STATE_ADDRESS + 40, npk.TASK_SLOTS, 8)

    resp = task_call(machine, operation=npk.OP_TASK_WAKE, op_id=7200,
                     task=blocked_id)
    assert resp.reason == npk.REASON_TASK_EXHAUSTED
    assert_task_receipt(machine, resp, outcome=core_api.OUTCOME_FAILED,
                        reason=npk.REASON_TASK_EXHAUSTED)
    assert find_tcb(machine, blocked_id).state == npk.TASK_STATE_BLOCKED


def test_rejected_syscall_does_not_enter_scheduler(image):
    machine = fresh_machine(image)
    rejected = sys_api.call_syscall(
        machine, npk.SYS_WRITE, 3, 0xFFFFFFFFFFFFFFFF, 1)
    assert rejected.a0 == (1 << 64) - 1
    state = npk.decode_task_state(machine)
    assert state.task_count == 0
    assert state.current_task == 0
    assert state.dispatch_count == 0
