"""Milestone 5 object manager and universal object model tests.

Every object operation is admitted through the real post-boot kernel entry and
runs the shared five-stage core. Tests validate the receipt jointly with object
headers, handle slots, type descriptors, namespace entries, receipt extensions,
reference counts, and payload state; a success response alone is never evidence.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nova import npk
from nova.assembler import assemble_text
from nova.isa import EXCEPTION_CODES, PRIVILEGE_LEVELS

OWNER = 1
OTHER_OWNER = 2
ROOT_ID = npk.OBJECT_ID_BASE
CODE_ADDRESS = 0xA0000
USER = PRIVILEGE_LEVELS["user"]
MACHINE = PRIVILEGE_LEVELS["machine"]

FULL = npk.OBJ_PERM_ALL
READ_ONLY = npk.OBJ_PERM_READ
READ_SHARE = npk.OBJ_PERM_READ | npk.OBJ_PERM_SHARE
RWXU = (npk.PERM_READ | npk.PERM_WRITE | npk.PERM_EXEC | npk.PERM_USER)
MAP_VA = 0x8000000000


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(npk.build_kernel(tmp_path_factory.mktemp("npk-m5")))


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    assert result.machine.exception_code == EXCEPTION_CODES["NONE"]
    result.machine.halted = False
    return result.machine


def call(machine, operation, operation_id, *, name="", **kwargs):
    response = npk.call_object_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        owner=kwargs.pop("owner", OWNER),
        name=name,
        **kwargs,
    )
    receipts = npk.decode_object_receipts(machine)
    ext = npk.decode_object_receipt_exts(machine)
    return response, receipts[response.receipt_index], ext[response.receipt_index]


def call_complete(machine, operation, operation_id, *, result=None, **kwargs):
    response, receipt, ext = call(machine, operation, operation_id, **kwargs)
    expected = response.result if result is None else result
    npk.validate_object_response(
        response, status=npk.REQUEST_OK, operation=operation, result=expected
    )
    npk.validate_complete_object_receipt(receipt)
    npk.validate_receipt_ext(ext)
    assert receipt.result == expected
    assert receipt.trace == npk.EXPECTED_STAGE_TRACE
    return response, receipt, ext


def call_rejected(machine, operation, operation_id, *, reason, **kwargs):
    response, receipt, ext = call(machine, operation, operation_id, **kwargs)
    npk.validate_object_response(
        response, status=npk.REQUEST_REJECTED, operation=operation, reason=reason
    )
    npk.validate_rejected_object_receipt(receipt)
    npk.validate_receipt_ext(ext)
    return response, receipt, ext


def call_failed(machine, operation, operation_id, *, reason, stage, **kwargs):
    response, receipt, ext = call(machine, operation, operation_id, **kwargs)
    npk.validate_object_response(
        response, status=npk.REQUEST_FAILED, operation=operation, reason=reason
    )
    npk.validate_failed_object_receipt(receipt, stage=stage)
    npk.validate_receipt_ext(ext)
    return response, receipt, ext


def object_by_id(machine, object_id):
    return next(item for item in npk.decode_object_headers(machine)
                if item.id == object_id and item.state == npk.OBJECT_STATE_ACTIVE)


def handle_slot(machine, handle):
    owner, _generation, index = npk.decode_handle(handle)
    linear = (owner - 1) * npk.HANDLE_SLOTS + index
    return npk.decode_handle_slots(machine)[linear]


def entry_by_name(machine, parent_id, name):
    return next(item for item in npk.decode_namespace_entries(machine)
                if item.state == npk.NAMESPACE_ENTRY_ACTIVE
                and item.parent_id == parent_id and item.name == name)


def create(machine, operation_id, *, owner=OWNER,
           type=npk.OBJECT_TYPE_GENERIC, permissions=FULL, payload=0,
           parent=0, name=""):
    response, receipt, _ext = call_complete(
        machine,
        npk.OP_OBJECT_CREATE,
        operation_id,
        owner=owner,
        type=type,
        permissions=permissions,
        payload=payload,
        parent=parent,
        name=name,
    )
    object_id = response.result
    header = object_by_id(machine, object_id)
    handle = response.result  # replaced below by the response's handle evidence
    handle = npk.decode_object_state(machine).last_handle
    assert receipt.reserved[0] == type
    assert receipt.reserved[1] == object_id
    assert header.id == object_id
    assert header.type == type
    assert header.owner == owner
    assert header.parent == parent
    assert header.permissions == permissions
    assert header.state == npk.OBJECT_STATE_ACTIVE
    assert header.refcount == 1 + header.namespace_count
    assert header.handle_count == 1
    assert header.retain_count == 0
    assert handle == npk.encode_handle(owner, handle_slot(machine, handle).generation,
                                       npk.decode_handle(handle)[2])
    return object_id, handle, header


def close(machine, operation_id, handle, *, owner=OWNER):
    return call_complete(
        machine, npk.OP_OBJECT_CLOSE, operation_id, owner=owner, handle=handle
    )


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
    region = next(item for item in npk.decode_memory_regions(machine)
                  if item.base == response.result)
    return response.result, region.id


def run_user_scall(machine, number):
    machine.load_code(assemble_text(f"SCALL {number}\nHALT\n"), CODE_ADDRESS)
    machine.pc = CODE_ADDRESS
    machine.priv = USER
    machine.halted = False
    seen_exception = False
    for _ in range(50_000):
        if (seen_exception and machine.exception_code == 0 and
                machine.pc == CODE_ADDRESS + 4):
            return
        machine.run(1)
        if machine.exception_code != 0:
            seen_exception = True
    raise AssertionError("system call did not return to guest code")


def stable_snapshot(machine):
    state = npk.decode_object_state(machine)
    persistent_state = replace(state, last_status=0, last_reason=0)
    return (
        persistent_state,
        npk.decode_object_headers(machine),
        npk.decode_handle_slots(machine),
        npk.decode_type_descriptors(machine),
        npk.decode_namespace_state(machine),
        npk.decode_namespace_entries(machine),
        npk.decode_memory_state(machine),
    )


def test_layout_boot_registry_namespace_and_m1_to_m4_evidence(image):
    npk.validate_object_layout()
    assert npk.OBJECT_ENTRY_ADDRESS == 0x96050
    assert npk.OBJECT_RECEIPT_EXT_ADDRESS == 0x9EA10
    assert npk.TYPE_REGISTRY_ADDRESS == 0x9E190
    assert npk.NAMESPACE_ENTRY_TABLE_ADDRESS == 0x9E390

    machine = fresh_machine(image)
    receipt = npk.decode_processing_receipt(machine)
    npk.validate_boot_receipt(receipt)
    assert receipt.trace == npk.EXPECTED_BOOT_TRACE

    snapshot = npk.decode_core_snapshot(machine)
    npk.validate_core_state(snapshot.core)
    assert (snapshot.core.admitted, snapshot.core.completed,
            snapshot.core.rejected, snapshot.core.failed) == (10, 7, 3, 2)
    assert all(item.reserved == (0, 0) for item in snapshot.receipts[:12])
    assert npk.decode_trap_state(machine).dispatch_count == 0
    npk.validate_memory_state(npk.decode_memory_state(machine))
    assert npk.decode_memory_receipts(machine)[0].magic == 0

    state = npk.decode_object_state(machine)
    npk.validate_object_state(state)
    assert state.magic == npk.OBJECT_STATE_MAGIC
    assert state.version == npk.OBJECT_STATE_VERSION
    assert state.init_status == 0
    assert state.selftest_status == 0
    assert (state.next_id, state.next_epoch, state.object_count,
            state.handle_count) == (1, 1, 1, 0)
    assert state.object_slots == npk.OBJECT_SLOTS
    assert state.handle_owners == npk.HANDLE_OWNERS
    assert state.handle_slots == npk.HANDLE_SLOTS

    entry = npk.decode_object_entry(machine)
    assert npk.KERNEL_IMAGE_BASE <= entry < npk.OBJECT_ENTRY_ADDRESS
    assert npk.decode_object_request(machine).magic == 0
    assert npk.decode_object_response(machine).magic == 0
    assert not any(item.magic for item in npk.decode_object_receipts(machine))
    assert not any(item.object_id for item in npk.decode_object_receipt_exts(machine))

    ns = npk.decode_namespace_state(machine)
    npk.validate_namespace_state(ns)
    assert ns.entry_count == 1
    assert ns.next_id == 2
    root_entry = npk.decode_namespace_entries(machine)[0]
    assert root_entry.name == "/"
    assert root_entry.object_id == ROOT_ID
    assert object_by_id(machine, ROOT_ID).type == npk.OBJECT_TYPE_DIRECTORY

    types = npk.decode_type_descriptors(machine)
    for descriptor in types[:4]:
        npk.validate_type_descriptor(descriptor)
    assert [item.name for item in types[:4]] == [
        "GENERIC", "MEMORY_REGION", "ADDRESS_SPACE", "DIRECTORY"
    ]
    assert [item.body_size for item in types[:4]] == [0, 8, 8, 8]
    assert all(item.state == npk.TYPE_STATE_FREE for item in types[4:])


def test_type_query_registration_duplicate_and_invalid_ids(image):
    machine = fresh_machine(image)
    response, receipt, ext = call_complete(
        machine, npk.OP_OBJECT_TYPE_QUERY, 100, type=npk.OBJECT_TYPE_MEMORY_REGION
    )
    assert response.result == npk.OBJECT_TYPE_MEMORY_REGION
    assert receipt.reserved == (npk.OBJECT_TYPE_MEMORY_REGION,
                                npk.OBJECT_TYPE_MEMORY_REGION)
    assert ext.object_id == npk.OBJECT_TYPE_MEMORY_REGION

    permissions = npk.OBJ_PERM_READ | npk.OBJ_PERM_WRITE
    response, receipt, _ext = call_complete(
        machine, npk.OP_OBJECT_TYPE_REGISTER, 101, type=5,
        permissions=permissions, payload=16, name="CUSTOM"
    )
    assert response.result == 5
    assert receipt.reserved == (5, 5)
    descriptor = npk.decode_type_descriptors(machine)[4]
    npk.validate_type_descriptor(descriptor)
    assert descriptor.name == "CUSTOM"
    assert descriptor.body_size == 16
    assert descriptor.allowed_access == permissions

    call_rejected(machine, npk.OP_OBJECT_TYPE_REGISTER, 102, type=5,
                  permissions=permissions, payload=16, name="CUSTOM",
                  reason=npk.REASON_OBJECT_DUPLICATE)
    call_rejected(machine, npk.OP_OBJECT_TYPE_REGISTER, 103, type=0,
                  permissions=permissions, name="BAD", reason=npk.REASON_OBJECT_BAD_TYPE)
    call_rejected(machine, npk.OP_OBJECT_TYPE_REGISTER, 104, type=99,
                  permissions=permissions, name="BAD", reason=npk.REASON_OBJECT_BAD_TYPE)
    call_rejected(machine, npk.OP_OBJECT_TYPE_QUERY, 105, type=6,
                  reason=npk.REASON_OBJECT_BAD_TYPE)


def test_create_query_and_receipt_extension_reflect_committed_state(image):
    machine = fresh_machine(image)
    object_id, handle, header = create(machine, 100, payload=0x1234)

    slot = handle_slot(machine, handle)
    assert slot.state == npk.HANDLE_STATE_ACTIVE
    assert slot.object_id == object_id
    assert slot.permissions == FULL
    assert npk.decode_object_state(machine).handle_count == 1

    response, receipt, ext = call_complete(
        machine, npk.OP_OBJECT_QUERY, 101, handle=handle, result=object_id
    )
    assert receipt.reserved == (npk.OBJECT_TYPE_GENERIC, 0x1234)
    assert ext.object_id == object_id
    assert ext.handle == handle
    assert (ext.refcount, ext.handle_count) == (1, 1)
    assert object_by_id(machine, object_id) == header
    assert response.result == object_id


def test_close_double_close_stale_handle_and_safe_reuse(image):
    machine = fresh_machine(image)
    object_id, handle, _header = create(machine, 100)
    close(machine, 101, handle)

    slot = handle_slot(machine, handle)
    assert slot.state == npk.HANDLE_STATE_FREE
    assert slot.object_id == 0
    assert object_by_id(machine, object_id).refcount == 0
    assert npk.decode_object_state(machine).handle_count == 0

    call_rejected(machine, npk.OP_OBJECT_CLOSE, 102, handle=handle,
                  reason=npk.REASON_OBJECT_BAD_HANDLE)
    object_id2, handle2, _header2 = create(machine, 103)
    owner, generation, index = npk.decode_handle(handle2)
    assert object_id2 != object_id
    assert (owner, index) == (OWNER, 0)
    assert generation == 2

    call_rejected(machine, npk.OP_OBJECT_QUERY, 104, handle=handle,
                  reason=npk.REASON_OBJECT_BAD_HANDLE)
    call_complete(machine, npk.OP_OBJECT_QUERY, 105, handle=handle2,
                  result=object_id2)


def test_open_same_owner_cross_owner_sharing_and_owner_checks(image):
    machine = fresh_machine(image)
    object_id, handle, _header = create(machine, 100)

    response, receipt, ext = call_complete(
        machine, npk.OP_OBJECT_OPEN, 101, handle=handle,
        permissions=npk.OBJ_PERM_READ
    )
    same_owner_handle = response.result
    assert npk.decode_handle(same_owner_handle)[0] == OWNER
    assert receipt.reserved == (npk.OBJECT_TYPE_GENERIC, object_id)
    assert ext.handle == same_owner_handle
    assert handle_slot(machine, same_owner_handle).permissions == npk.OBJ_PERM_READ
    assert object_by_id(machine, object_id).handle_count == 2

    response, _receipt, _ext = call_complete(
        machine, npk.OP_OBJECT_OPEN, 102, owner=OTHER_OWNER, handle=handle,
        permissions=npk.OBJ_PERM_READ
    )
    shared_handle = response.result
    assert npk.decode_handle(shared_handle)[0] == OTHER_OWNER
    assert handle_slot(machine, shared_handle).object_id == object_id
    assert object_by_id(machine, object_id).handle_count == 3

    call_complete(machine, npk.OP_OBJECT_QUERY, 103, owner=OTHER_OWNER,
                  handle=shared_handle, result=object_id)
    call_rejected(machine, npk.OP_OBJECT_QUERY, 104, owner=OTHER_OWNER,
                  handle=handle, reason=npk.REASON_OBJECT_BAD_OWNER)
    call_rejected(machine, npk.OP_OBJECT_QUERY, 105, owner=OWNER,
                  handle=shared_handle, reason=npk.REASON_OBJECT_BAD_OWNER)

    close(machine, 106, same_owner_handle)
    close(machine, 107, shared_handle, owner=OTHER_OWNER)
    assert object_by_id(machine, object_id).handle_count == 1

    private_id, private_handle, _header = create(machine, 108,
                                                 permissions=READ_ONLY)
    call_rejected(machine, npk.OP_OBJECT_OPEN, 109, owner=OTHER_OWNER,
                  handle=private_handle, permissions=npk.OBJ_PERM_READ,
                  reason=npk.REASON_OBJECT_BAD_PERMISSION)
    call_rejected(machine, npk.OP_OBJECT_OPEN, 110, handle=private_handle,
                  permissions=FULL, reason=npk.REASON_OBJECT_BAD_PERMISSION)
    assert object_by_id(machine, private_id).handle_count == 1


def test_retain_release_and_destroy_require_zero_references(image):
    machine = fresh_machine(image)
    object_id, handle, _header = create(machine, 100)

    call_rejected(machine, npk.OP_OBJECT_DESTROY, 101, payload=object_id,
                  reason=npk.REASON_OBJECT_STATE)
    call_complete(machine, npk.OP_OBJECT_RETAIN, 102, handle=handle,
                  result=object_id)
    header = object_by_id(machine, object_id)
    assert (header.retain_count, header.refcount) == (1, 2)

    call_complete(machine, npk.OP_OBJECT_RELEASE, 103, handle=handle,
                  result=object_id)
    header = object_by_id(machine, object_id)
    assert (header.retain_count, header.refcount) == (0, 1)
    call_rejected(machine, npk.OP_OBJECT_RELEASE, 104, handle=handle,
                  reason=npk.REASON_OBJECT_STATE)

    close(machine, 105, handle)
    state_before = npk.decode_object_state(machine)
    response, receipt, ext = call_complete(
        machine, npk.OP_OBJECT_DESTROY, 106, payload=object_id, result=object_id
    )
    assert receipt.reserved == (npk.OBJECT_TYPE_GENERIC, object_id)
    assert ext.refcount == 0
    assert not any(item.id == object_id for item in npk.decode_object_headers(machine))
    state_after = npk.decode_object_state(machine)
    assert state_after.object_count == state_before.object_count - 1
    assert state_after.last_object == object_id
    assert state_after.next_epoch == state_before.next_epoch + 1
    call_rejected(machine, npk.OP_OBJECT_DESTROY, 107, payload=object_id,
                  reason=npk.REASON_OBJECT_BAD_OBJECT)

    no_destroy_id, no_destroy_handle, _header = create(
        machine, 108, permissions=npk.OBJ_PERM_READ
    )
    close(machine, 109, no_destroy_handle)
    call_rejected(machine, npk.OP_OBJECT_DESTROY, 110, payload=no_destroy_id,
                  reason=npk.REASON_OBJECT_BAD_PERMISSION)


def test_memory_payload_cleanup_and_commit_failure_with_live_mapping(image):
    machine = fresh_machine(image)
    base, region_id = allocate_region(machine, 500)
    object_id, handle, _header = create(
        machine, 100, type=npk.OBJECT_TYPE_MEMORY_REGION, payload=region_id
    )
    assert object_by_id(machine, object_id).payload == region_id
    close(machine, 101, handle)

    npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=501,
        owner=OWNER,
        address=MAP_VA,
        size=npk.MEMORY_PAGE_SIZE,
        flags=RWXU,
        aux=(region_id << 16) | 0,
    )
    before = object_by_id(machine, object_id)
    call_failed(machine, npk.OP_OBJECT_DESTROY, 102, payload=object_id,
                reason=npk.REASON_OBJECT_STATE, stage=npk.STAGE_COMMIT)
    assert object_by_id(machine, object_id) == before
    assert any(item.id == region_id for item in npk.decode_memory_regions(machine))

    npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_UNMAP,
        operation_id=502,
        owner=OWNER,
        address=MAP_VA,
        size=npk.MEMORY_PAGE_SIZE,
        aux=0,
    )
    call_complete(machine, npk.OP_OBJECT_DESTROY, 103, payload=object_id,
                  result=object_id)
    assert not any(item.id == region_id for item in npk.decode_memory_regions(machine))
    memory_state = npk.decode_memory_state(machine)
    assert memory_state.mapping_count == 0
    assert npk.decode_address_spaces(machine)[0].mapping_count == 0


def test_namespace_creation_insertion_lookup_removal_and_lifetime(image):
    machine = fresh_machine(image)
    dir_id, dir_handle, dir_header = create(
        machine, 100, type=npk.OBJECT_TYPE_DIRECTORY, parent=ROOT_ID, name="Kernel"
    )
    assert dir_header.namespace_count == 1
    assert entry_by_name(machine, ROOT_ID, "Kernel").object_id == dir_id

    named_id, named_handle, named_header = create(
        machine, 101, parent=dir_id, name="Alpha"
    )
    assert named_header.parent == dir_id
    assert entry_by_name(machine, dir_id, "Alpha").object_id == named_id

    loose_id, loose_handle, loose_header = create(machine, 102)
    response, receipt, _ext = call_complete(
        machine, npk.OP_OBJECT_DIRECTORY_INSERT, 103, handle=dir_handle,
        payload=loose_id, name="Beta"
    )
    beta_entry_id = response.result
    assert beta_entry_id == npk.decode_namespace_state(machine).last_entry
    assert receipt.reserved == (npk.OBJECT_TYPE_DIRECTORY, loose_id)
    assert entry_by_name(machine, dir_id, "Beta").object_id == loose_id
    assert object_by_id(machine, loose_id).namespace_count == 1
    assert object_by_id(machine, loose_id).refcount == 2

    call_complete(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 104, payload=ROOT_ID,
                  name="/", result=ROOT_ID)
    call_complete(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 105, payload=ROOT_ID,
                  name="Kernel", result=dir_id)
    call_complete(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 106, payload=ROOT_ID,
                  name="Kernel/Alpha", result=named_id)
    call_complete(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 107, payload=ROOT_ID,
                  name="Kernel/Beta", result=loose_id)

    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_INSERT, 108,
                  handle=dir_handle, payload=named_id, name="Beta",
                  reason=npk.REASON_OBJECT_DUPLICATE)
    call_rejected(machine, npk.OP_OBJECT_DESTROY, 109, payload=loose_id,
                  reason=npk.REASON_OBJECT_STATE)

    call_complete(machine, npk.OP_OBJECT_DIRECTORY_REMOVE, 110,
                  handle=dir_handle, name="Beta", result=beta_entry_id)
    assert object_by_id(machine, loose_id).namespace_count == 0
    assert object_by_id(machine, loose_id).refcount == 1
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 111,
                  payload=ROOT_ID, name="Kernel/Beta",
                  reason=npk.REASON_OBJECT_NO_ENTRY)

    close(machine, 112, loose_handle)
    close(machine, 113, named_handle)
    call_complete(machine, npk.OP_OBJECT_DESTROY, 114, payload=loose_id,
                  result=loose_id)
    assert not any(item.object_id == loose_id
                   for item in npk.decode_namespace_entries(machine))


def test_namespace_validation_errors_do_not_mutate(image):
    machine = fresh_machine(image)
    dir_id, dir_handle, _header = create(
        machine, 100, type=npk.OBJECT_TYPE_DIRECTORY, parent=ROOT_ID, name="D"
    )
    object_id, object_handle, _obj_header = create(machine, 101)

    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_INSERT, 102,
                  handle=object_handle, payload=object_id, name="Bad",
                  reason=npk.REASON_OBJECT_NOT_DIRECTORY)
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_INSERT, 103,
                  handle=dir_handle, payload=object_id, name="Bad/",
                  reason=npk.REASON_OBJECT_BAD_NAME)
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_REMOVE, 104,
                  handle=dir_handle, name="Missing",
                  reason=npk.REASON_OBJECT_NO_ENTRY)
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 105,
                  payload=object_id, name="Bad",
                  reason=npk.REASON_OBJECT_NOT_DIRECTORY)
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 106,
                  payload=ROOT_ID, name="D/",
                  reason=npk.REASON_OBJECT_BAD_NAME)
    call_rejected(machine, npk.OP_OBJECT_DIRECTORY_LOOKUP, 107,
                  payload=ROOT_ID, name="Missing/Child",
                  reason=npk.REASON_OBJECT_NO_ENTRY)

    assert not any(item.name == "Bad" for item in npk.decode_namespace_entries(machine))
    assert object_by_id(machine, object_id).namespace_count == 0
    assert object_by_id(machine, dir_id).namespace_count == 1


@pytest.mark.parametrize("operation,kwargs,reason", [
    (npk.OP_OBJECT_CREATE, {"type": 99}, npk.REASON_OBJECT_BAD_TYPE),
    (npk.OP_OBJECT_CREATE, {"permissions": 0}, npk.REASON_OBJECT_BAD_PERMISSION),
    (npk.OP_OBJECT_CREATE, {"permissions": 0x20}, npk.REASON_OBJECT_BAD_PERMISSION),
    (npk.OP_OBJECT_CREATE, {"type": npk.OBJECT_TYPE_MEMORY_REGION, "payload": 0},
     npk.REASON_OBJECT_PAYLOAD),
    (npk.OP_OBJECT_CREATE, {"parent": 0x1234}, npk.REASON_OBJECT_BAD_PARENT),
    (npk.OP_OBJECT_CREATE, {"parent": ROOT_ID, "name": "Bad/"},
     npk.REASON_OBJECT_BAD_NAME),
    (npk.OP_OBJECT_QUERY, {"handle": 0}, npk.REASON_OBJECT_BAD_HANDLE),
    (npk.OP_OBJECT_TYPE_REGISTER, {"type": 5, "name": "", "permissions": FULL},
     npk.REASON_OBJECT_BAD_NAME),
])
def test_invalid_requests_are_rejected_without_mutation(image, operation, kwargs,
                                                         reason):
    kwargs = dict(kwargs)
    machine = fresh_machine(image)
    if operation == npk.OP_OBJECT_CREATE and kwargs.get("parent") == 0x1234:
        create(machine, 100)
    before = stable_snapshot(machine)

    defaults = {
        "owner": OWNER,
        "type": npk.OBJECT_TYPE_GENERIC if operation == npk.OP_OBJECT_CREATE else 0,
        "handle": 0,
        "permissions": FULL if operation in (
            npk.OP_OBJECT_CREATE, npk.OP_OBJECT_TYPE_REGISTER
        ) else 0,
        "payload": 0,
        "parent": 0,
        "name": "",
    }
    defaults.update(kwargs)
    call_rejected(machine, operation, 200, reason=reason, **defaults)
    assert stable_snapshot(machine) == before


def test_bad_magic_and_unknown_operation_do_not_admit(image):
    machine = fresh_machine(image)
    baseline = npk.decode_core_snapshot(machine)
    receipts_before = npk.decode_object_receipts(machine)

    npk.write_object_request(
        machine, operation=npk.OP_OBJECT_CREATE, operation_id=300,
        owner=OWNER, type=npk.OBJECT_TYPE_GENERIC, permissions=FULL,
    )
    machine._write_mem(npk.OBJECT_REQUEST_ADDRESS, 0, 8)
    npk.call_object_entry(machine)
    response = npk.decode_object_response(machine)
    assert response.status == npk.REQUEST_BAD_REQUEST
    assert response.magic == npk.OBJECT_RESPONSE_MAGIC

    response = npk.call_object_request(
        machine, operation=99, operation_id=301, owner=OWNER
    )
    assert response.status == npk.REQUEST_BAD_OP

    after = npk.decode_core_snapshot(machine)
    assert after.core.admitted == baseline.core.admitted
    assert after.core.completed == baseline.core.completed
    assert npk.decode_object_receipts(machine) == receipts_before


def test_object_table_exhaustion_is_failed_execute(image):
    machine = fresh_machine(image)
    for index in range(63):
        create(machine, 1000 + index)
    assert npk.decode_object_state(machine).object_count == npk.OBJECT_SLOTS

    call_failed(machine, npk.OP_OBJECT_CREATE, 2000, owner=OWNER,
                type=npk.OBJECT_TYPE_GENERIC, permissions=FULL,
                reason=npk.REASON_OBJECT_EXHAUSTED, stage=npk.STAGE_EXECUTE)
    assert npk.decode_object_state(machine).object_count == npk.OBJECT_SLOTS


def test_handle_table_exhaustion_is_failed_execute(image):
    machine = fresh_machine(image)
    _object_id, handle, _header = create(machine, 100)
    for index in range(63):
        npk.call_object_request(
            machine,
            operation=npk.OP_OBJECT_OPEN,
            operation_id=200 + index,
            owner=OWNER,
            handle=handle,
            permissions=npk.OBJ_PERM_READ,
        )
    before = npk.decode_object_state(machine)
    before = stable_snapshot(machine)
    call_failed(machine, npk.OP_OBJECT_OPEN, 500, handle=handle,
                permissions=npk.OBJ_PERM_READ,
                reason=npk.REASON_OBJECT_EXHAUSTED, stage=npk.STAGE_EXECUTE)
    assert stable_snapshot(machine) == before


def test_receipts_use_dedicated_log_with_extended_evidence(image):
    machine = fresh_machine(image)
    core_before = npk.decode_core_snapshot(machine).receipts[:12]
    call_rejected(machine, npk.OP_OBJECT_QUERY, 100, handle=0,
                  reason=npk.REASON_OBJECT_BAD_HANDLE)
    receipts = npk.decode_object_receipts(machine)
    assert receipts[0].type == npk.OP_OBJECT_QUERY
    assert receipts[0].reason == npk.REASON_OBJECT_BAD_HANDLE
    assert receipts[0].reserved == (0, 0)
    assert npk.decode_core_snapshot(machine).receipts[:12] == core_before

    object_id, handle, _header = create(machine, 101)
    close(machine, 102, handle)
    call_complete(machine, npk.OP_OBJECT_DESTROY, 103, payload=object_id,
                  result=object_id)
    ext = npk.decode_object_receipt_exts(machine)[2]
    assert ext.object_id == object_id
    assert ext.handle == 0
    assert ext.refcount == 0
    assert ext.handle_count == 0


def test_address_space_payload_can_be_wrapped(image):
    machine = fresh_machine(image)
    base, region_id = allocate_region(machine, 600)
    assert base
    npk.call_memory_request(
        machine,
        operation=npk.OP_MEMORY_MAP,
        operation_id=601,
        owner=OWNER,
        address=MAP_VA,
        size=npk.MEMORY_PAGE_SIZE,
        flags=RWXU,
        aux=(region_id << 16) | 0,
    )
    spaces = npk.decode_address_spaces(machine)
    assert spaces[0].state == npk.ADDRESS_SPACE_ACTIVE
    assert spaces[0].owner == OWNER

    object_id, handle, _header = create(
        machine, 100, type=npk.OBJECT_TYPE_ADDRESS_SPACE, payload=0
    )
    assert object_by_id(machine, object_id).payload == 0
    close(machine, 101, handle)
    call_complete(machine, npk.OP_OBJECT_DESTROY, 102, payload=object_id,
                  result=object_id)
    assert npk.decode_address_spaces(machine)[0].state == npk.ADDRESS_SPACE_ACTIVE


def test_m3_trap_and_m4_memory_still_work_after_object_init(image):
    machine = fresh_machine(image)
    object_id, _handle, _header = create(machine, 100)
    assert object_by_id(machine, object_id).state == npk.OBJECT_STATE_ACTIVE

    run_user_scall(machine, 5)
    state = npk.decode_trap_state(machine)
    npk.validate_trap_state(state)
    assert state.dispatch_count == 1

    # The trap return restores user privilege; the direct memory entry used by
    # the harness is a machine-privilege kernel entry, not a syscall.
    machine.priv = MACHINE
    base, region_id = allocate_region(machine, 700)
    assert any(item.base == base and item.id == region_id
               for item in npk.decode_memory_regions(machine))
    npk.validate_object_state(npk.decode_object_state(machine))
