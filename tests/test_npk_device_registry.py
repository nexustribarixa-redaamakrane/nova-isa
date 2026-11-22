"""Focused Milestone 7 typed device-registry tests.

Requests are written to the published NPK entry word and executed by real guest
code. Every assertion is backed by the kernel's bounded registry, operation
counters, dedicated receipt log, or Milestone 6 write log; a response word alone
is never treated as sufficient evidence.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.npk import core as core_api
from nova.npk import devices

OWNER = 1
OTHER_OWNER = 2
MAP_VA = 0x8000000000
MAP_FLAGS = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_USER


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m7-registry"))
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
    snapshot = npk.decode_device_snapshot(machine)
    if response.operation <= npk.OP_DEVICE_WRITE:
        return snapshot.device_receipts[response.receipt_index]
    return snapshot.timer_receipts[response.receipt_index]


def call(machine, operation_id, *, operation, **kwargs):
    response = npk.call_device_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        **kwargs,
    )
    return response, receipt_for(response, machine)


def call_complete(machine, operation_id, *, operation, result=None, **kwargs):
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
    assert receipt.trace == npk.EXPECTED_STAGE_TRACE
    if result is not None:
        assert response.result == result
    return response, receipt


def call_rejected(machine, operation_id, *, operation, reason, **kwargs):
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


def setup_mapped_page(machine, operation_id_base=100):
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
    region = next(
        item for item in npk.decode_memory_regions(machine)
        if item.base == allocation.result
        and item.state == npk.REGION_ALLOCATED
    )
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
    for offset, value in enumerate(data):
        address = physical + offset
        word = machine._read_mem(address & ~7, 8)
        shift = (address & 7) * 8
        machine._write_mem(
            address & ~7, (word & ~(0xFF << shift)) | (value << shift), 8
        )


def test_typed_registry_lifecycle_is_observable(image):
    machine = fresh_machine(image)

    response, receipt = call_complete(
        machine,
        1000,
        operation=npk.OP_DEVICE_REGISTER,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert response.result == 100
    assert receipt.reserved == (100, 0)
    snapshot = npk.decode_device_snapshot(machine)
    devices.validate_registry(snapshot.registry)
    entry = snapshot.registry[0]
    assert (entry.state, entry.id, entry.kind, entry.capability, entry.owner) == (
        npk.REGISTRY_REGISTERED,
        100,
        npk.DEVICE_KIND_CONSOLE,
        npk.DEVICE_CAP_WRITE,
        OWNER,
    )
    assert snapshot.operation_state.registered_count == 1

    query, query_receipt = call_complete(
        machine,
        1001,
        operation=npk.OP_DEVICE_QUERY,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert query.result == (npk.REGISTRY_REGISTERED << 32) | npk.DEVICE_KIND_CONSOLE
    assert query.aux == npk.DEVICE_CAP_WRITE
    assert query_receipt.reserved == (100, 0)

    unregister, unregister_receipt = call_complete(
        machine,
        1002,
        operation=npk.OP_DEVICE_UNREGISTER,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert unregister.result == 100
    assert unregister_receipt.reserved == (100, 0)
    after = npk.decode_device_snapshot(machine)
    devices.validate_registry(after.registry)
    assert after.registry[0].state == npk.REGISTRY_UNREGISTERED
    assert after.registry[0].owner == 0
    assert after.registry[0].object_handle == 0
    assert after.operation_state.registered_count == 0

    call_rejected(
        machine,
        1003,
        operation=npk.OP_DEVICE_QUERY,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        reason=npk.REASON_DEVICE_NOT_FOUND,
    )


def test_duplicate_unsupported_and_capability_rejections_preserve_registry(image):
    machine = fresh_machine(image)

    object_response = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CREATE,
        operation_id=900,
        owner=OWNER,
        type=npk.OBJECT_TYPE_GENERIC,
        permissions=npk.OBJ_PERM_ALL,
    )
    assert object_response.status == npk.REQUEST_OK
    handle = npk.decode_object_state(machine).last_handle
    other_response = npk.call_object_request(
        machine,
        operation=npk.OP_OBJECT_CREATE,
        operation_id=901,
        owner=OWNER,
        type=npk.OBJECT_TYPE_GENERIC,
        permissions=npk.OBJ_PERM_ALL,
    )
    assert other_response.status == npk.REQUEST_OK
    other_handle = npk.decode_object_state(machine).last_handle
    assert other_handle != handle

    call_complete(
        machine,
        1100,
        operation=npk.OP_DEVICE_REGISTER,
        target=100,
        owner=OWNER,
        object=handle,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    before = npk.decode_registry(machine)

    call_rejected(
        machine,
        1101,
        operation=npk.OP_DEVICE_REGISTER,
        target=100,
        owner=OWNER,
        object=handle,
        kind=npk.DEVICE_KIND_TIMER,
        capability=npk.DEVICE_CAP_TIME,
        reason=npk.REASON_DEVICE_DUPLICATE,
    )
    call_rejected(
        machine,
        1102,
        operation=npk.OP_DEVICE_REGISTER,
        target=101,
        owner=OWNER,
        kind=npk.DEVICE_KIND_BLOCK,
        capability=0,
        reason=npk.REASON_DEVICE_UNSUPPORTED,
    )
    call_rejected(
        machine,
        1103,
        operation=npk.OP_DEVICE_REGISTER,
        target=102,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_TIME,
        reason=npk.REASON_DEVICE_CAPABILITY,
    )
    call_rejected(
        machine,
        1104,
        operation=npk.OP_DEVICE_QUERY,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_TIME,
        reason=npk.REASON_DEVICE_CAPABILITY,
    )
    call_rejected(
        machine,
        1105,
        operation=npk.OP_DEVICE_QUERY,
        target=100,
        owner=OWNER,
        object=other_handle,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        reason=npk.REASON_DEVICE_OBJECT,
    )
    assert npk.decode_registry(machine) == before

    response = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_QUERY,
        operation_id=1106,
        target=100,
        owner=OTHER_OWNER,
        object=handle,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert response.status == npk.REQUEST_BAD_REQUEST
    assert response.reason == npk.REASON_DEVICE_OBJECT
    assert response.receipt_index == npk.NO_INDEX
    assert npk.decode_registry(machine) == before


def test_registry_exhaustion_is_rejected_without_overwriting_state(image):
    machine = fresh_machine(image)
    for index in range(npk.DEVICE_REGISTRY_SLOTS):
        target = 200 + index
        kind = (npk.DEVICE_KIND_CONSOLE if index % 2 == 0
                else npk.DEVICE_KIND_TIMER)
        capability = (npk.DEVICE_CAP_WRITE if kind == npk.DEVICE_KIND_CONSOLE
                      else npk.DEVICE_CAP_TIME)
        call_complete(
            machine,
            1200 + index,
            operation=npk.OP_DEVICE_REGISTER,
            target=target,
            owner=OWNER,
            kind=kind,
            capability=capability,
        )

    before = npk.decode_device_snapshot(machine)
    devices.validate_registry(before.registry)
    assert before.operation_state.registered_count == npk.DEVICE_REGISTRY_SLOTS
    assert all(
        entry.state == npk.REGISTRY_REGISTERED for entry in before.registry
    )
    first_receipt = before.device_receipts[0]

    response, receipt = call_rejected(
        machine,
        1300,
        operation=npk.OP_DEVICE_REGISTER,
        target=208,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        reason=npk.REASON_DEVICE_EXHAUSTED,
    )
    assert response.error == npk.ERR_NO_RESOURCE
    after = npk.decode_device_snapshot(machine)
    assert after.registry == before.registry
    assert after.operation_state.registered_count == npk.DEVICE_REGISTRY_SLOTS
    assert after.device_receipts[0] == first_receipt
    assert receipt.id == 1300


def test_typed_write_uses_m4_pointer_validation_and_m6_log(image):
    machine = fresh_machine(image)
    physical = setup_mapped_page(machine, operation_id_base=1400)
    payload = b"typed m7\n"
    write_physical_bytes(machine, physical, payload)

    call_complete(
        machine,
        1410,
        operation=npk.OP_DEVICE_REGISTER,
        target=300,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    response, receipt = call_complete(
        machine,
        1411,
        operation=npk.OP_DEVICE_WRITE,
        target=300,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        args=(MAP_VA, len(payload), 0, 0),
        result=len(payload),
    )
    assert response.aux == len(payload)
    assert receipt.reserved == (300, 0)
    assert npk.decode_write_log(machine) == payload
    assert devices.console_bytes(machine) == payload
    registry = npk.decode_registry(machine)
    assert registry[0].operations == 1
    assert registry[0].last_result == len(payload)

    log_before = npk.decode_write_log(machine)
    call_rejected(
        machine,
        1412,
        operation=npk.OP_DEVICE_WRITE,
        target=300,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        args=(MAP_VA + npk.MEMORY_PAGE_SIZE, 1, 0, 0),
        reason=npk.REASON_DEVICE_BAD_PARAMETER,
    )
    call_rejected(
        machine,
        1413,
        operation=npk.OP_DEVICE_WRITE,
        target=300,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
        args=(MAP_VA, 0, 0, 0),
        reason=npk.REASON_DEVICE_BAD_PARAMETER,
    )
    assert npk.decode_write_log(machine) == log_before
    assert npk.decode_registry(machine)[0].operations == 1


def test_device_receipt_exhaustion_does_not_overwrite_evidence(image):
    machine = fresh_machine(image)
    for index in range(npk.DEVICE_RECEIPT_SLOTS):
        call_rejected(
            machine,
            2000 + index,
            operation=npk.OP_DEVICE_QUERY,
            target=999999,
            owner=OWNER,
            kind=npk.DEVICE_KIND_CONSOLE,
            capability=npk.DEVICE_CAP_WRITE,
            reason=npk.REASON_DEVICE_NOT_FOUND,
        )

    before = npk.decode_device_snapshot(machine)
    receipts = before.device_receipts
    assert [receipt.id for receipt in receipts] == [
        2000 + index for index in range(npk.DEVICE_RECEIPT_SLOTS)
    ]
    first_receipt = receipts[0]
    last_receipt = receipts[-1]

    response = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_QUERY,
        operation_id=3000,
        target=999999,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    devices.validate_device_response(response)
    assert response.status == npk.REQUEST_ADMIT_FAIL
    assert response.reason == npk.REASON_DEVICE_EXHAUSTED
    assert response.receipt_index == npk.NO_INDEX

    after = npk.decode_device_snapshot(machine)
    assert after.device_receipts == before.device_receipts
    assert after.device_receipts[0] == first_receipt
    assert after.device_receipts[-1] == last_receipt
    assert after.operation_state.device_operations == 0
    assert after.operation_state.last_receipt == npk.NO_INDEX
    core = npk.decode_core_snapshot(machine)
    assert core.core.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)


def test_malformed_requests_are_rejected_before_admission(image):
    machine = fresh_machine(image)

    bad_op = npk.call_device_request(
        machine,
        operation=31,
        operation_id=4000,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert bad_op.status == npk.REQUEST_BAD_OP
    assert bad_op.reason == npk.REASON_DEVICE_UNSUPPORTED
    assert bad_op.receipt_index == npk.NO_INDEX

    bad_target = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_QUERY,
        operation_id=4001,
        target=0,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert bad_target.status == npk.REQUEST_BAD_REQUEST
    assert bad_target.reason == npk.REASON_DEVICE_BAD_REQUEST
    assert bad_target.receipt_index == npk.NO_INDEX

    bad_owner = npk.call_device_request(
        machine,
        operation=npk.OP_DEVICE_QUERY,
        operation_id=4002,
        target=100,
        owner=3,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    assert bad_owner.status == npk.REQUEST_BAD_REQUEST
    assert bad_owner.reason == npk.REASON_DEVICE_OBJECT
    assert bad_owner.receipt_index == npk.NO_INDEX

    devices.write_device_request(
        machine,
        operation=npk.OP_DEVICE_QUERY,
        operation_id=4003,
        target=100,
        owner=OWNER,
        kind=npk.DEVICE_KIND_CONSOLE,
        capability=npk.DEVICE_CAP_WRITE,
    )
    machine._write_mem(devices.DEVICE_REQUEST_ADDRESS, 0, 8)
    npk.call_device_entry(machine)
    bad_magic = npk.decode_device_response(machine)
    assert bad_magic.status == npk.REQUEST_BAD_REQUEST
    assert bad_magic.reason == npk.REASON_DEVICE_BAD_REQUEST
    assert bad_magic.receipt_index == npk.NO_INDEX

    snapshot = npk.decode_device_snapshot(machine)
    assert snapshot.operation_state.device_operations == 0
    assert not any(receipt.id for receipt in snapshot.device_receipts)
