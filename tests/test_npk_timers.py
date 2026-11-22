"""Focused Milestone 7 software timer and event tests.

Timer creation, inspection, cancellation, expiration, and delivery are submitted
through the real typed request entry. Events are software records in bounded
kernel RAM; these tests explicitly verify that no architectural timer interrupt
or `timecmp` reprogramming is claimed by the software timer table.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.npk import core as core_api
from nova.npk import devices

OWNER = 1
FAULTED_TIME = 0x2000000


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m7-timers"))
    )


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def receipt_for(response, machine):
    devices.validate_device_response(response)
    assert response.receipt_index != npk.NO_INDEX
    return npk.decode_device_snapshot(machine).timer_receipts[
        response.receipt_index
    ]


def call(machine, operation_id, *, operation, **kwargs):
    response = npk.call_device_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        **kwargs,
    )
    return response, receipt_for(response, machine)


def call_complete(machine, *args, operation, operation_id=None, result=None, **kwargs):
    if args:
        assert operation_id is None or operation_id == args[0]
        operation_id = args[0]
    assert operation_id is not None
    response, receipt = call(
        machine, operation_id, operation=operation, **kwargs
    )
    assert response.status == npk.REQUEST_OK
    assert response.reason == devices.REASON_NONE
    npk.validate_complete_device_receipt(receipt)
    npk.validate_receipt(
        receipt,
        npk.device_receipt_expectation(
            operation_id,
            operation,
            devices.OUTCOME_COMPLETE,
            result=response.result,
        ),
    )
    if result is not None:
        assert response.result == result
    return response, receipt


def call_rejected(machine, *args, operation, operation_id=None, reason, **kwargs):
    if args:
        assert operation_id is None or operation_id == args[0]
        operation_id = args[0]
    assert operation_id is not None
    response, receipt = call(
        machine, operation_id, operation=operation, **kwargs
    )
    assert response.status == npk.REQUEST_REJECTED
    assert response.reason == reason
    npk.validate_rejected_device_receipt(receipt)
    npk.validate_receipt(
        receipt,
        npk.device_receipt_expectation(
            operation_id,
            operation,
            devices.OUTCOME_REJECTED,
            reason=reason,
        ),
    )
    return response, receipt


def timer_request(operation, operation_id, timer_id=0, *, args=(0, 0, 0, 0)):
    return dict(
        operation=operation,
        operation_id=operation_id,
        target=timer_id,
        owner=OWNER,
        kind=npk.DEVICE_KIND_TIMER,
        capability=npk.DEVICE_CAP_TIME,
        args=args,
    )


def create(machine, operation_id, timer_id, interval):
    return call_complete(
        machine, operation_id,
        **timer_request(
            npk.OP_TIMER_CREATE, operation_id, timer_id, args=(interval, 0, 0, 0)
        ),
    )


def timer_at(machine, timer_id):
    return next(
        entry for entry in npk.decode_timers(machine)
        if entry.id == timer_id
    )


def set_deadline(machine, index, deadline):
    """Set one pending deadline to force deterministic tie selection."""
    address = npk.TIMER_TABLE_ADDRESS + index * npk.TIMER_ENTRY_SIZE + 24
    machine._write_mem(address, deadline, 8)


def test_timer_create_records_a_real_emulator_deadline(image):
    machine = fresh_machine(image)
    before = machine.read_csr("time")
    response, receipt = create(machine, 1000, 500, 64)
    after = machine.read_csr("time")

    assert response.result == 500
    assert receipt.reserved == (500, 0)
    timer = timer_at(machine, 500)
    devices.validate_timers(npk.decode_timers(machine))
    assert timer.state == npk.TIMER_PENDING
    assert timer.id == 500
    assert timer.owner == OWNER
    assert timer.interval == 64
    assert timer.event == 0
    assert before + 64 <= timer.deadline <= after + 64
    assert before <= timer.created <= after
    assert npk.decode_device_snapshot(machine).operation_state.timer_count == 1

    inspect, inspect_receipt = call_complete(
        machine, 1001, **timer_request(npk.OP_TIMER_INSPECT, 1001, 500)
    )
    assert inspect.result == npk.TIMER_PENDING
    assert inspect.aux == timer.deadline
    assert inspect_receipt.reserved == (500, 0)


def test_timer_parameter_and_capability_validation(image):
    machine = fresh_machine(image)
    create(machine, 1100, 600, 128)

    call_rejected(
        machine, 1101,
        **timer_request(
            npk.OP_TIMER_CREATE, 1101, 600, args=(128, 0, 0, 0)
        ),
        reason=npk.REASON_TIMER_DUPLICATE,
    )
    call_rejected(
        machine, 1102,
        **timer_request(
            npk.OP_TIMER_CREATE, 1102, 601, args=(0, 0, 0, 0)
        ),
        reason=npk.REASON_TIMER_BAD_INTERVAL,
    )
    call_rejected(
        machine, 1103,
        **timer_request(
            npk.OP_TIMER_CREATE, 1103, 601, args=(npk.TIMER_ENTRY_SIZE << 20, 0, 0, 0)
        ),
        reason=npk.REASON_TIMER_BAD_INTERVAL,
    )

    wrong_capability = timer_request(npk.OP_TIMER_CREATE, 1104, 601, args=(64, 0, 0, 0))
    wrong_capability["capability"] = npk.DEVICE_CAP_WRITE
    call_rejected(
        machine, 1104, **wrong_capability, reason=npk.REASON_TIMER_UNSUPPORTED
    )

    wrong_owner = timer_request(npk.OP_TIMER_INSPECT, 1105, 600)
    wrong_owner["owner"] = 2
    call_rejected(
        machine, 1105, **wrong_owner, reason=npk.REASON_TIMER_NOT_FOUND
    )

    snapshot = npk.decode_device_snapshot(machine)
    assert snapshot.operation_state.timer_count == 1
    assert len([entry for entry in snapshot.timers if entry.id == 600]) == 1
    devices.validate_timers(snapshot.timers)


def test_equal_deadlines_expire_by_id_without_duplicate_events(image):
    machine = fresh_machine(image)
    create(machine, 1200, 700, 64)
    create(machine, 1201, 701, 64)

    machine.write_csr("time", FAULTED_TIME)
    set_deadline(machine, 0, FAULTED_TIME - 1)
    set_deadline(machine, 1, FAULTED_TIME - 1)

    first, first_receipt = call_complete(
        machine, 1202, **timer_request(npk.OP_TIMER_EXPIRE, 1202)
    )
    assert first.result == 700
    assert first.aux == 700
    assert first_receipt.reserved == (0, 0)
    assert timer_at(machine, 700).state == npk.TIMER_EXPIRED
    assert timer_at(machine, 701).state == npk.TIMER_PENDING

    second, _second_receipt = call_complete(
        machine, 1203, **timer_request(npk.OP_TIMER_EXPIRE, 1203)
    )
    assert second.result == 701

    call_rejected(
        machine, 1204,
        **timer_request(npk.OP_TIMER_EXPIRE, 1204, 700),
        reason=npk.REASON_TIMER_NOT_DUE,
    )

    snapshot = npk.decode_device_snapshot(machine)
    devices.validate_timers(snapshot.timers)
    devices.validate_timer_events(snapshot.events)
    events = [event for event in snapshot.events if event.event_id]
    assert len(events) == 2
    assert [event.timer_id for event in events] == [700, 701]
    assert events[0].event_id != events[1].event_id
    assert events[0].deadline == FAULTED_TIME - 1
    assert all(event.delivered == 0 for event in events)
    state = snapshot.operation_state
    assert state.timer_events == 2
    assert state.timer_expirations == 2
    assert state.next_event_id == 3


def test_cancel_is_pending_only_and_delivery_is_terminal(image):
    machine = fresh_machine(image)
    create(machine, 1300, 800, 64)
    create(machine, 1301, 801, 64)

    cancelled, cancel_receipt = call_complete(
        machine, 1302, **timer_request(npk.OP_TIMER_CANCEL, 1302, 800)
    )
    assert cancelled.result == 800
    assert cancel_receipt.reserved == (800, 0)
    assert timer_at(machine, 800).state == npk.TIMER_CANCELLED

    call_rejected(
        machine, 1303,
        **timer_request(npk.OP_TIMER_EXPIRE, 1303, 800),
        reason=npk.REASON_TIMER_NOT_DUE,
    )
    call_rejected(
        machine, 1304,
        **timer_request(npk.OP_TIMER_DELIVER, 1304, 800),
        reason=npk.REASON_TIMER_STATE,
    )

    machine.write_csr("time", FAULTED_TIME)
    expired, _expired_receipt = call_complete(
        machine, 1305, **timer_request(npk.OP_TIMER_EXPIRE, 1305, 801)
    )
    assert expired.result == 801
    call_rejected(
        machine, 1306,
        **timer_request(npk.OP_TIMER_CANCEL, 1306, 801),
        reason=npk.REASON_TIMER_STATE,
    )
    assert timer_at(machine, 801).state == npk.TIMER_EXPIRED

    delivered, _delivered_receipt = call_complete(
        machine, 1307, **timer_request(npk.OP_TIMER_DELIVER, 1307, 801)
    )
    assert delivered.result == 801
    call_rejected(
        machine, 1308,
        **timer_request(npk.OP_TIMER_DELIVER, 1308, 801),
        reason=npk.REASON_TIMER_STATE,
    )

    snapshot = npk.decode_device_snapshot(machine)
    devices.validate_timers(snapshot.timers)
    devices.validate_timer_events(snapshot.events)
    assert timer_at(machine, 801).state == npk.TIMER_DELIVERED
    event = next(item for item in snapshot.events if item.timer_id == 801)
    assert event.delivered == 1
    assert snapshot.operation_state.timer_cancellations == 1
    assert snapshot.operation_state.timer_deliveries == 1


def test_timer_and_event_table_exhaustion_is_explicit(image):
    machine = fresh_machine(image)
    for index in range(npk.TIMER_SLOTS):
        create(machine, 1400 + index, 900 + index, 16)

    call_rejected(
        machine, 1500,
        **timer_request(
            npk.OP_TIMER_CREATE, 1500, 908, args=(16, 0, 0, 0)
        ),
        reason=npk.REASON_TIMER_EXHAUSTED,
    )

    machine.write_csr("time", FAULTED_TIME)
    for index in range(npk.TIMER_SLOTS):
        response, _receipt = call_complete(
            machine,
            1600 + index,
            **timer_request(npk.OP_TIMER_EXPIRE, 1600 + index, 900 + index),
        )
        assert response.result == 900 + index

    call_rejected(
        machine, 1700,
        **timer_request(npk.OP_TIMER_EXPIRE, 1700),
        reason=npk.REASON_TIMER_EVENT,
    )

    snapshot = npk.decode_device_snapshot(machine)
    devices.validate_timers(snapshot.timers)
    devices.validate_timer_events(snapshot.events)
    state = snapshot.operation_state
    assert state.timer_count == npk.TIMER_SLOTS
    assert state.timer_events == npk.TIMER_EVENT_SLOTS
    assert state.next_event_id == npk.TIMER_EVENT_SLOTS + 1
    assert all(entry.state == npk.TIMER_EXPIRED for entry in snapshot.timers)
    assert len({event.event_id for event in snapshot.events}) == npk.TIMER_EVENT_SLOTS


def test_timer_events_are_software_events_not_hardware_interrupts(image):
    machine = fresh_machine(image)
    timecmp_before = machine.read_csr("timecmp")
    ie_before = machine.read_csr("ie")
    state_before = npk.decode_device_state(machine)

    create(machine, 1800, 1000, 16)
    machine.write_csr("time", FAULTED_TIME)
    call_complete(
        machine, 1801, **timer_request(npk.OP_TIMER_EXPIRE, 1801, 1000)
    )
    call_complete(
        machine, 1802, **timer_request(npk.OP_TIMER_DELIVER, 1802, 1000)
    )

    state_after = npk.decode_device_state(machine)
    assert machine.read_csr("timecmp") == timecmp_before
    assert machine.read_csr("ie") == ie_before
    assert state_after.interrupt_count == state_before.interrupt_count
    assert state_after.timer_interrupts == state_before.timer_interrupts
    assert state_after.console_calls == state_before.console_calls


def test_timer_receipt_exhaustion_does_not_overwrite_evidence(image):
    machine = fresh_machine(image)
    for index in range(npk.TIMER_RECEIPT_SLOTS):
        call_rejected(
            machine,
            2000 + index,
            **timer_request(npk.OP_TIMER_INSPECT, 2000 + index, 999999),
            reason=npk.REASON_TIMER_NOT_FOUND,
        )

    before = npk.decode_device_snapshot(machine)
    assert [receipt.id for receipt in before.timer_receipts] == [
        2000 + index for index in range(npk.TIMER_RECEIPT_SLOTS)
    ]
    first_receipt = before.timer_receipts[0]
    last_receipt = before.timer_receipts[-1]

    response = npk.call_device_request(
        machine,
        **timer_request(npk.OP_TIMER_INSPECT, 3000, 999999),
    )
    devices.validate_device_response(response)
    assert response.status == npk.REQUEST_ADMIT_FAIL
    assert response.reason == npk.REASON_DEVICE_EXHAUSTED
    assert response.receipt_index == npk.NO_INDEX

    after = npk.decode_device_snapshot(machine)
    assert after.timer_receipts == before.timer_receipts
    assert after.timer_receipts[0] == first_receipt
    assert after.timer_receipts[-1] == last_receipt
    core = npk.decode_core_snapshot(machine)
    assert core.core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
