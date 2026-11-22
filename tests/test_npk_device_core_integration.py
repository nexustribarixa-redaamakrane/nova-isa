"""Milestone 7 core and trap-path integration tests.

These tests deliberately mix typed device/timer requests with evidence from the
shared Milestone 2 core, the Milestone 3 trap path, the Milestone 6 write log,
and the boot marker. They guard against a device implementation that bypasses
the processing pipeline or corrupts already completed Phase 2 state.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.isa import INT_SOFTWARE
from nova.npk import core as core_api
from nova.npk import devices

OWNER = 1


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m7-core"))
    )


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


def request(machine, *, operation, operation_id, target=0, **kwargs):
    response = npk.call_device_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        target=target,
        owner=OWNER,
        **kwargs,
    )
    devices.validate_device_response(response)
    assert response.status == npk.REQUEST_OK
    assert response.reason == devices.REASON_NONE
    return response


def test_device_and_timer_requests_reuse_the_shared_processing_core(image):
    machine = fresh_machine(image)
    before = npk.decode_core_snapshot(machine).core
    core_api.validate_core_state(before)
    assert before.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)

    device = request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1000,
        target=100,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    after_device = npk.decode_core_snapshot(machine).core
    assert after_device.admitted == before.admitted + 1
    assert after_device.completed == before.completed + 1
    assert after_device.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert device.slot != npk.NO_INDEX

    timer = request(
        machine,
        operation=npk.OP_TIMER_CREATE,
        operation_id=1001,
        target=500,
        kind=npk.DEVICE_KIND_TIMER,
        capability=npk.DEVICE_CAP_TIME,
        args=(64, 0, 0, 0),
    )
    after_timer = npk.decode_core_snapshot(machine).core
    assert after_timer.admitted == before.admitted + 2
    assert after_timer.completed == before.completed + 2
    assert after_timer.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert after_timer.pu_operation == (0, 0)
    assert timer.slot != npk.NO_INDEX

    snapshot = npk.decode_device_snapshot(machine)
    assert snapshot.operation_state.device_operations == 1
    assert snapshot.operation_state.timer_operations == 1
    assert snapshot.operation_state.last_outcome == devices.OUTCOME_COMPLETE


def test_m7_receipts_do_not_overwrite_earlier_core_receipts(image):
    machine = fresh_machine(image)
    core_before = npk.decode_core_snapshot(machine)
    device_before = npk.decode_device_snapshot(machine)

    request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1100,
        target=101,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    request(
        machine,
        operation=npk.OP_TIMER_CREATE,
        operation_id=1101,
        target=501,
        kind=npk.DEVICE_KIND_TIMER,
        capability=npk.DEVICE_CAP_TIME,
        args=(32, 0, 0, 0),
    )

    core_after = npk.decode_core_snapshot(machine)
    device_after = npk.decode_device_snapshot(machine)
    assert core_after.receipts == core_before.receipts
    assert [receipt.id for receipt in device_after.device_receipts[:2]] == [1100, 0]
    assert device_after.device_receipts[0].type == npk.OP_DEVICE_REGISTER
    assert device_after.timer_receipts[0].id == 1101
    assert device_after.timer_receipts[0].type == npk.OP_TIMER_CREATE
    assert device_before.device_receipts[0] != device_after.device_receipts[0]


def test_typed_requests_coexist_with_the_m3_trap_path(image):
    machine = fresh_machine(image)
    request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1200,
        target=102,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    operation_before = npk.decode_operation_state(machine)
    core_before = npk.decode_core_snapshot(machine).core

    result = devices.run_interrupt(machine, INT_SOFTWARE)
    devices.validate_interrupt_result(result)

    operation_after = npk.decode_operation_state(machine)
    core_after = npk.decode_core_snapshot(machine).core
    assert operation_after == operation_before
    assert core_after.admitted == core_before.admitted + 1
    assert core_after.completed == core_before.completed + 1
    assert core_after.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert result.state.interrupt_count == result.before.interrupt_count + 1


def test_boot_evidence_survives_typed_device_activity(image):
    machine = fresh_machine(image)
    marker = npk.BOOT_MARKER_ADDRESS
    marker_before = machine._read_mem(marker, 8)
    core_before = npk.decode_core_snapshot(machine).core

    request(
        machine,
        operation=npk.OP_DEVICE_REGISTER,
        operation_id=1300,
        target=103,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )

    assert machine._read_mem(marker, 8) == marker_before
    core_after = npk.decode_core_snapshot(machine).core
    assert core_after.magic == core_before.magic
    assert core_after.version == core_before.version
    assert core_after.selftest_status == 0
    assert core_after.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
