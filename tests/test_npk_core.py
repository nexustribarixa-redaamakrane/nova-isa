"""NPK Milestone 2: reusable processing-core tests.

These tests boot the real NPK image once and then decode its observable core
state, operation snapshots, and receipt log. Receipt validation is deliberately
independent of the kernel's own return status: a receipt must prove its stage
sequence, outcome, result, and reason instead of merely reporting success.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nova import npk
from nova.npk import core as core_api


@pytest.fixture(scope="module")
def booted(tmp_path_factory):
    image_path = npk.build_kernel(tmp_path_factory.mktemp("npk-core"))
    image = npk.load_kernel_image(image_path)
    return npk.boot_kernel(image)


def test_milestone2_layout_regions_are_defined_and_non_overlapping():
    assert npk.CORE_STATE_ADDRESS == 0x90080
    assert npk.CORE_STATE_SIZE == 144
    assert npk.OP_SNAPSHOT_ADDRESS == 0x90120
    assert npk.OP_SNAPSHOT_SIZE == 64
    assert npk.OP_SNAPSHOT_SLOTS == 4
    assert npk.RECEIPT_TABLE_ADDRESS == 0x90220
    assert npk.RECEIPT_SIZE == 128
    assert npk.RECEIPT_SLOTS == 12

    legacy_end = npk.PROCESSING_RECEIPT_ADDRESS + npk.PROCESSING_RECEIPT_SIZE
    core_end = npk.CORE_STATE_ADDRESS + npk.CORE_STATE_SIZE
    snapshot_end = npk.OP_SNAPSHOT_ADDRESS + npk.OP_SNAPSHOT_SIZE * npk.OP_SNAPSHOT_SLOTS
    receipt_end = npk.RECEIPT_TABLE_ADDRESS + npk.RECEIPT_SIZE * npk.RECEIPT_SLOTS
    assert legacy_end <= npk.CORE_STATE_ADDRESS
    assert core_end <= npk.OP_SNAPSHOT_ADDRESS
    assert snapshot_end <= npk.RECEIPT_TABLE_ADDRESS
    assert npk.CORE_STATE_ADDRESS > npk.BOOT_STACK_TOP


def test_core_state_reports_expected_counters_and_idle_units(booted):
    state = booted.core.core
    npk.validate_core_state(state)

    assert state.magic == npk.CORE_MAGIC
    assert state.version == npk.CORE_VERSION
    assert state.op_slots == npk.OP_SNAPSHOT_SLOTS
    assert state.pu_count == 2
    assert state.selftest_status == 0
    assert state.admitted == 10
    assert state.completed == 7
    assert state.rejected == 3
    assert state.failed == 2
    assert state.pu_state == (core_api.PU_IDLE, core_api.PU_IDLE)
    assert state.pu_operation == (0, 0)
    assert state.last_admit_status == npk.ERR_OK
    assert state.last_pu_status == npk.ERR_UNIT_IDLE
    assert state.last_transition_status == npk.ERR_OK


def test_boot_receipt_still_matches_the_milestone1_contract(booted):
    assert booted.status == 0
    assert booted.processing == npk.ProcessingReceipt(
        magic=npk.PROCESSING_MAGIC,
        operation=npk.BOOT_VERIFY_OPERATION,
        stage=npk.STAGE_COMPLETE,
        status=npk.STATUS_PROCESSED,
        input=npk.BOOT_VERIFY_INPUT,
        output=npk.BOOT_VERIFY_OUTPUT,
        trace=npk.EXPECTED_BOOT_TRACE,
    )
    legacy = npk.decode_processing_receipt(booted.machine)
    npk.validate_boot_receipt(legacy)


def test_receipt_log_matches_the_full_milestone2_sequence(booted):
    receipts = booted.core.receipts
    assert len(receipts) == npk.RECEIPT_SLOTS
    for index, receipt in enumerate(receipts):
        npk.validate_receipt(receipt, npk.expected_receipt(index))


@pytest.mark.parametrize("index", [0, 1, 5, 7, 8, 10, 11])
def test_successful_operations_show_the_complete_lifecycle(booted, index):
    receipt = booted.core.receipts[index]
    assert receipt.stage == core_api.STAGE_COMPLETE
    assert receipt.state == core_api.STATE_COMPLETE
    assert receipt.outcome == core_api.OUTCOME_COMPLETE
    assert receipt.reason == core_api.REASON_NONE
    assert receipt.trace == core_api.EXPECTED_STAGE_TRACE


def test_rejected_operation_stops_at_validation(booted):
    receipt = booted.core.receipts[2]
    assert receipt.id == 3
    assert receipt.type == npk.OP_CORE_DIAGNOSTIC
    assert receipt.stage == core_api.STAGE_VALIDATE
    assert receipt.state == core_api.STATE_REJECTED
    assert receipt.outcome == core_api.OUTCOME_REJECTED
    assert receipt.result == 0
    assert receipt.reason == core_api.REASON_DIAG_REJECT
    assert receipt.trace == (core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE, 0, 0, 0)


def test_execution_failure_stops_at_execute(booted):
    receipt = booted.core.receipts[3]
    assert receipt.stage == core_api.STAGE_EXECUTE
    assert receipt.state == core_api.STATE_FAILED
    assert receipt.outcome == core_api.OUTCOME_FAILED
    assert receipt.result == 0
    assert receipt.reason == core_api.REASON_DIAG_EXEC
    assert receipt.trace == (
        core_api.STAGE_ADMIT,
        core_api.STAGE_VALIDATE,
        core_api.STAGE_EXECUTE,
        0,
        0,
    )


def test_commit_failure_stops_at_commit(booted):
    receipt = booted.core.receipts[4]
    assert receipt.stage == core_api.STAGE_COMMIT
    assert receipt.state == core_api.STATE_FAILED
    assert receipt.outcome == core_api.OUTCOME_FAILED
    assert receipt.result == 0
    assert receipt.reason == core_api.REASON_DIAG_COMMIT
    assert receipt.trace == (
        core_api.STAGE_ADMIT,
        core_api.STAGE_VALIDATE,
        core_api.STAGE_EXECUTE,
        core_api.STAGE_COMMIT,
        0,
    )


def test_duplicate_operation_id_is_rejected_without_execution(booted):
    original = booted.core.receipts[5]
    duplicate = booted.core.receipts[6]

    assert original.id == duplicate.id == 6
    assert original.outcome == core_api.OUTCOME_COMPLETE
    assert duplicate.stage == core_api.STAGE_NONE
    assert duplicate.state == core_api.STATE_REJECTED
    assert duplicate.outcome == core_api.OUTCOME_REJECTED
    assert duplicate.pu == npk.NO_INDEX
    assert duplicate.reason == core_api.REASON_DUPLICATE_ID
    assert duplicate.trace == (0, 0, 0, 0, 0)


def test_processing_unit_assignment_release_and_unavailable_rejection(booted):
    first = booted.core.receipts[7]
    second = booted.core.receipts[8]
    unavailable = booted.core.receipts[9]

    assert first.id == 7 and first.pu == 0
    assert second.id == 8 and second.pu == 1
    assert first.outcome == second.outcome == core_api.OUTCOME_COMPLETE
    assert unavailable.stage == core_api.STAGE_NONE
    assert unavailable.pu == npk.NO_INDEX
    assert unavailable.reason == core_api.REASON_UNIT_UNAVAILABLE
    assert unavailable.trace == (0, 0, 0, 0, 0)


def test_operation_slots_are_released_after_completion(booted):
    assert len(booted.core.operations) == npk.OP_SNAPSHOT_SLOTS
    for operation in booted.core.operations:
        assert operation.id == 0
        assert operation.type == 0
        assert operation.stage == core_api.STAGE_NONE
        assert operation.state == core_api.STATE_FREE
        assert operation.pu == npk.NO_INDEX
        assert operation.result == 0
        assert operation.reason == core_api.REASON_NONE


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"magic": 0}, "receipt magic"),
        ({"pu": 2}, "processing-unit id"),
        ({"stage": 6}, "invalid receipt stage"),
        ({"state": 99}, "invalid receipt state"),
        ({"outcome": 99}, "invalid receipt outcome"),
        ({"trace": (1, 2, 3, 4, 6)}, "invalid stage trace"),
        ({"trace": (1, 2, 3, 4, 0)}, "stage trace does not match"),
        ({"outcome": core_api.OUTCOME_PENDING}, "pending receipt has a terminal state"),
    ],
)
def test_receipt_structure_validation_rejects_invalid_receipts(booted, changes, message):
    invalid = replace(booted.core.receipts[0], **changes)
    with pytest.raises(npk.NpkCoreError, match=message):
        core_api.validate_receipt_structure(invalid)


@pytest.mark.parametrize(
    ("index", "changes", "message"),
    [
        (2, {"result": npk.DIAG_OUTPUT}, "rejected receipt"),
        (2, {"reason": core_api.REASON_NONE}, "rejected receipt"),
        (
            3,
            {
                "stage": core_api.STAGE_VALIDATE,
                "trace": (core_api.STAGE_ADMIT, core_api.STAGE_VALIDATE, 0, 0, 0),
            },
            "failed receipt lacks",
        ),
        (3, {"state": core_api.STATE_COMPLETE}, "failed receipt does not record"),
        (0, {"state": core_api.STATE_FAILED}, "complete outcome lacks"),
        (0, {"trace": (1, 3, 3, 4, 5)}, "stage trace does not match"),
    ],
)
def test_receipt_structure_validation_rejects_inconsistent_outcomes(
    booted, index, changes, message
):
    invalid = replace(booted.core.receipts[index], **changes)
    with pytest.raises(npk.NpkCoreError, match=message):
        core_api.validate_receipt_structure(invalid)


def test_receipt_expectation_validation_rejects_a_mismatched_operation(booted):
    receipt = booted.core.receipts[0]
    with pytest.raises(npk.NpkCoreError, match="receipt id mismatch"):
        npk.validate_receipt(receipt, npk.expected_receipt(1))
    with pytest.raises(npk.NpkCoreError, match="receipt reason mismatch"):
        npk.validate_receipt(
            receipt,
            npk.expected_receipt(0, reason=core_api.REASON_DIAG_EXEC),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"magic": 0}, "core magic mismatch"),
        ({"version": 1}, "core version mismatch"),
        ({"op_slots": 8}, "unexpected core dimensions"),
        ({"selftest_status": 1}, "kernel core self-test failed"),
        ({"pu_state": (1, 0)}, "processing units were not released"),
        ({"pu_operation": (0, 7)}, "processing units retain operations"),
        ({"admitted": 11}, "unexpected core counters"),
    ],
)
def test_core_state_validation_rejects_incomplete_or_inconsistent_state(booted, changes, message):
    invalid = replace(booted.core.core, **changes)
    with pytest.raises(npk.NpkCoreError, match=message):
        npk.validate_core_state(invalid)


def test_tampered_core_magic_is_detected(booted):
    address = npk.CORE_STATE_ADDRESS
    original = booted.machine._read_mem(address, 8)
    try:
        booted.machine._write_mem(address, 0, 8)
        with pytest.raises(npk.NpkCoreError, match="core magic mismatch"):
            npk.validate_core_state(npk.decode_core_snapshot(booted.machine).core)
    finally:
        booted.machine._write_mem(address, original, 8)


def test_tampered_receipt_magic_is_detected(booted):
    address = npk.RECEIPT_TABLE_ADDRESS
    original = booted.machine._read_mem(address, 8)
    try:
        booted.machine._write_mem(address, 0, 8)
        receipt = npk.decode_core_snapshot(booted.machine).receipts[0]
        with pytest.raises(npk.NpkCoreError, match="receipt magic mismatch"):
            core_api.validate_receipt_structure(receipt)
    finally:
        booted.machine._write_mem(address, original, 8)
