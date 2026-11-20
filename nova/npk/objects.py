"""Host-side decoding and validation of NPK Milestone 5 object processing.

These helpers read the kernel's fixed object-manager observation regions through
the reference machine. They are harness code, not guest kernel APIs. An object
receipt is only meaningful together with the object header, handle slots,
reference counts, type registry, namespace entries, extension record, and any
payload state that the operation actually changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assembler import AssemblyError, assemble_text
from ..emulator import NovaMachine
from .core import (
    EXPECTED_STAGE_TRACE,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_REJECTED,
    PU_IDLE,
    RECEIPT_MAGIC,
    REASON_NONE,
    Receipt,
    ReceiptExpectation,
    STAGE_ADMIT,
    STAGE_COMMIT,
    STAGE_COMPLETE,
    STAGE_EXECUTE,
    STAGE_NONE,
    STAGE_VALIDATE,
    STATE_ADMITTED,
    STATE_COMMITTED,
    STATE_COMPLETE,
    STATE_EXECUTED,
    STATE_FAILED,
    STATE_REJECTED,
    validate_receipt_structure,
)
from .layout import (
    HANDLE_OWNERS,
    HANDLE_SLOT_SIZE,
    HANDLE_SLOTS,
    HANDLE_TABLE_ADDRESS,
    NAMESPACE_ENTRY_SIZE,
    NAMESPACE_ENTRY_SLOTS,
    NAMESPACE_ENTRY_TABLE_ADDRESS,
    NAMESPACE_STATE_ADDRESS,
    NAMESPACE_STATE_SIZE,
    OBJECT_ENTRY_ADDRESS,
    OBJECT_HEADER_SIZE,
    OBJECT_RECEIPT_EXT_ADDRESS,
    OBJECT_RECEIPT_EXT_SIZE,
    OBJECT_RECEIPT_SLOTS,
    OBJECT_RECEIPT_TABLE_ADDRESS,
    OBJECT_REQUEST_ADDRESS,
    OBJECT_REQUEST_SIZE,
    OBJECT_RESPONSE_ADDRESS,
    OBJECT_RESPONSE_SIZE,
    OBJECT_SLOTS,
    OBJECT_STATE_ADDRESS,
    OBJECT_STATE_SIZE,
    OBJECT_TABLE_ADDRESS,
    RECEIPT_SIZE,
    TYPE_DESCRIPTOR_SIZE,
    TYPE_REGISTRY_ADDRESS,
    TYPE_SLOTS,
)

__all__ = [
    "OBJECT_STATE_MAGIC",
    "OBJECT_STATE_VERSION",
    "TYPE_MAGIC",
    "NAMESPACE_MAGIC",
    "OBJECT_REQUEST_MAGIC",
    "OBJECT_RESPONSE_MAGIC",
    "OBJECT_RECEIPT_SIZE",
    "OBJECT_ID_BASE",
    "OP_OBJECT_CREATE",
    "OP_OBJECT_DESTROY",
    "OP_OBJECT_OPEN",
    "OP_OBJECT_CLOSE",
    "OP_OBJECT_QUERY",
    "OP_OBJECT_RETAIN",
    "OP_OBJECT_RELEASE",
    "OP_OBJECT_DIRECTORY_INSERT",
    "OP_OBJECT_DIRECTORY_REMOVE",
    "OP_OBJECT_DIRECTORY_LOOKUP",
    "OP_OBJECT_TYPE_REGISTER",
    "OP_OBJECT_TYPE_QUERY",
    "REASON_OBJECT_BAD_REQUEST",
    "REASON_OBJECT_BAD_TYPE",
    "REASON_OBJECT_BAD_HANDLE",
    "REASON_OBJECT_BAD_OBJECT",
    "REASON_OBJECT_BAD_OWNER",
    "REASON_OBJECT_BAD_PERMISSION",
    "REASON_OBJECT_STATE",
    "REASON_OBJECT_EXHAUSTED",
    "REASON_OBJECT_DUPLICATE",
    "REASON_OBJECT_BAD_PARENT",
    "REASON_OBJECT_PAYLOAD",
    "REASON_OBJECT_UNSUPPORTED",
    "REASON_OBJECT_BAD_NAME",
    "REASON_OBJECT_NO_ENTRY",
    "REASON_OBJECT_NOT_DIRECTORY",
    "REASON_OBJECT_BAD_PATH",
    "OBJECT_STATE_FREE",
    "OBJECT_STATE_ACTIVE",
    "HANDLE_STATE_FREE",
    "HANDLE_STATE_ACTIVE",
    "OBJECT_TYPE_GENERIC",
    "OBJECT_TYPE_MEMORY_REGION",
    "OBJECT_TYPE_ADDRESS_SPACE",
    "OBJECT_TYPE_DIRECTORY",
    "OBJ_PERM_READ",
    "OBJ_PERM_WRITE",
    "OBJ_PERM_EXEC",
    "OBJ_PERM_SHARE",
    "OBJ_PERM_DESTROY",
    "OBJ_PERM_ALL",
    "encode_handle",
    "decode_handle",
    "encode_name",
    "decode_name",
    "NpkObjectError",
    "ObjectState",
    "ObjectHeader",
    "HandleSlot",
    "ObjectRequest",
    "ObjectResponse",
    "TypeDescriptor",
    "NamespaceState",
    "NamespaceEntry",
    "ReceiptExt",
    "ObjectSnapshot",
    "word",
    "decode_object_entry",
    "decode_object_state",
    "decode_object_headers",
    "decode_handle_slots",
    "decode_object_request",
    "decode_object_response",
    "decode_object_receipts",
    "decode_object_receipt_exts",
    "decode_type_descriptors",
    "decode_namespace_state",
    "decode_namespace_entries",
    "decode_object_snapshot",
    "validate_object_layout",
    "validate_object_state",
    "validate_object_response",
    "validate_type_descriptor",
    "validate_namespace_state",
    "validate_receipt_ext",
    "write_object_request",
    "call_object_entry",
    "call_object_request",
    "object_receipt_expectation",
    "validate_complete_object_receipt",
    "validate_rejected_object_receipt",
    "validate_failed_object_receipt",
]


OBJECT_STATE_MAGIC = 0x4E504B4F  # "NPKO"
OBJECT_STATE_VERSION = 5
TYPE_MAGIC = 0x4E504B54  # "NPKT"
NAMESPACE_MAGIC = 0x4E504B4E  # "NPKN"
OBJECT_REQUEST_MAGIC = 0x4E504B51  # "NPKQ"
OBJECT_RESPONSE_MAGIC = 0x4E504B5A  # "NPKZ"
OBJECT_RECEIPT_SIZE = RECEIPT_SIZE
OBJECT_ID_BASE = 0x4E504B4F00000000

OP_OBJECT_CREATE = 9
OP_OBJECT_DESTROY = 10
OP_OBJECT_OPEN = 11
OP_OBJECT_CLOSE = 12
OP_OBJECT_QUERY = 13
OP_OBJECT_RETAIN = 14
OP_OBJECT_RELEASE = 15
OP_OBJECT_DIRECTORY_INSERT = 16
OP_OBJECT_DIRECTORY_REMOVE = 17
OP_OBJECT_DIRECTORY_LOOKUP = 18
OP_OBJECT_TYPE_REGISTER = 19
OP_OBJECT_TYPE_QUERY = 20

REASON_OBJECT_BAD_REQUEST = 30
REASON_OBJECT_BAD_TYPE = 31
REASON_OBJECT_BAD_HANDLE = 32
REASON_OBJECT_BAD_OBJECT = 33
REASON_OBJECT_BAD_OWNER = 34
REASON_OBJECT_BAD_PERMISSION = 35
REASON_OBJECT_STATE = 36
REASON_OBJECT_EXHAUSTED = 37
REASON_OBJECT_DUPLICATE = 38
REASON_OBJECT_BAD_PARENT = 39
REASON_OBJECT_PAYLOAD = 40
REASON_OBJECT_UNSUPPORTED = 41
REASON_OBJECT_BAD_NAME = 42
REASON_OBJECT_NO_ENTRY = 43
REASON_OBJECT_NOT_DIRECTORY = 44
REASON_OBJECT_BAD_PATH = 45

REQUEST_OK = 0
REQUEST_BAD_REQUEST = 1
REQUEST_BAD_OP = 2
REQUEST_ADMIT_FAIL = 3
REQUEST_ADVANCE_FAIL = 4
REQUEST_BAD_OUTCOME = 5
REQUEST_REJECTED = 6
REQUEST_FAILED = 7
_VALID_REQUEST_STATUSES = frozenset(range(REQUEST_OK, REQUEST_FAILED + 1))
_VALID_REASONS = frozenset(range(REASON_OBJECT_BAD_REQUEST,
                                 REASON_OBJECT_BAD_PATH + 1))

OBJECT_STATE_FREE = 0
OBJECT_STATE_ACTIVE = 1
HANDLE_STATE_FREE = 0
HANDLE_STATE_ACTIVE = 1
TYPE_STATE_FREE = 0
TYPE_STATE_ACTIVE = 1
NAMESPACE_ENTRY_FREE = 0
NAMESPACE_ENTRY_ACTIVE = 1

OBJECT_TYPE_GENERIC = 1
OBJECT_TYPE_MEMORY_REGION = 2
OBJECT_TYPE_ADDRESS_SPACE = 3
OBJECT_TYPE_DIRECTORY = 4
_VALID_OBJECT_TYPES = frozenset((
    OBJECT_TYPE_GENERIC,
    OBJECT_TYPE_MEMORY_REGION,
    OBJECT_TYPE_ADDRESS_SPACE,
    OBJECT_TYPE_DIRECTORY,
))

OBJ_PERM_READ = 0x01
OBJ_PERM_WRITE = 0x02
OBJ_PERM_EXEC = 0x04
OBJ_PERM_SHARE = 0x08
OBJ_PERM_DESTROY = 0x10
OBJ_PERM_ALL = 0x1F

_DESTROY_NONE = 0
_DESTROY_MEMORY_REGION = 1
_DESTROY_DIRECTORY = 2
_VALID_DESTROY_ACTIONS = frozenset((
    _DESTROY_NONE, _DESTROY_MEMORY_REGION, _DESTROY_DIRECTORY
))

_NAME_WORDS = 4
_NAME_BYTES = 32
_HANDLE_OWNER_SHIFT = 56
_HANDLE_GENERATION_SHIFT = 16
_HANDLE_INDEX_MASK = 0xFFFF
_HANDLE_GENERATION_MASK = 0xFFFF


class NpkObjectError(Exception):
    """Raised when M5 observation regions do not satisfy their contract."""


def encode_handle(owner: int, generation: int, index: int) -> int:
    """Pack an owner-scoped, generation-tagged 64-bit handle."""
    if not 1 <= owner <= HANDLE_OWNERS:
        raise NpkObjectError(f"handle owner outside the table: {owner}")
    if not 1 <= generation <= _HANDLE_GENERATION_MASK:
        raise NpkObjectError(f"handle generation out of range: {generation}")
    if not 0 <= index < HANDLE_SLOTS:
        raise NpkObjectError(f"handle index out of range: {index}")
    return (owner << _HANDLE_OWNER_SHIFT |
            generation << _HANDLE_GENERATION_SHIFT | index)


def decode_handle(handle: int) -> tuple[int, int, int]:
    """Unpack a handle into ``(owner, generation, index)``."""
    owner = handle >> _HANDLE_OWNER_SHIFT
    generation = (handle >> _HANDLE_GENERATION_SHIFT) & _HANDLE_GENERATION_MASK
    index = handle & _HANDLE_INDEX_MASK
    return owner, generation, index


def encode_name(name: str) -> tuple[int, int, int, int]:
    """Encode a path/name into four little-endian observation words.

    The four words form one contiguous 32-byte buffer. Padding is applied only
    after the whole name so multi-word paths do not acquire embedded NULs at
    word boundaries.
    """
    if not isinstance(name, str):
        raise NpkObjectError("object names must be strings")
    data = name.encode("ascii")
    if len(data) > _NAME_BYTES:
        raise NpkObjectError(f"object name exceeds {_NAME_BYTES} bytes")
    buffer = data.ljust(_NAME_BYTES, b"\0")
    return tuple(
        int.from_bytes(buffer[index * 8:(index + 1) * 8], "little")
        for index in range(_NAME_WORDS)
    )  # type: ignore[return-value]


def decode_name(words: tuple[int, ...] | list[int]) -> str:
    """Decode a little-endian observation-word name."""
    data = b"".join(word.to_bytes(8, "little") for word in words)
    return data.rstrip(b"\0").decode("ascii", errors="replace")


@dataclass(frozen=True)
class ObjectState:
    magic: int
    version: int
    init_status: int
    next_id: int
    next_epoch: int
    object_count: int
    handle_count: int
    last_status: int
    last_reason: int
    last_object: int
    last_handle: int
    last_epoch: int
    object_slots: int
    handle_owners: int
    handle_slots: int
    selftest_status: int


@dataclass(frozen=True)
class ObjectHeader:
    slot: int
    magic: int
    id: int
    type: int
    owner: int
    parent: int
    permissions: int
    refcount: int
    handle_count: int
    retain_count: int
    payload: int
    created_epoch: int
    state: int
    namespace_count: int
    body: tuple[int, ...]


@dataclass(frozen=True)
class HandleSlot:
    owner: int
    index: int
    state: int
    generation: int
    object_id: int
    permissions: int


@dataclass(frozen=True)
class ObjectRequest:
    magic: int
    operation: int
    id: int
    owner: int
    type: int
    handle: int
    permissions: int
    payload: int
    parent: int
    aux: int
    name: str


@dataclass(frozen=True)
class ObjectResponse:
    magic: int
    status: int
    operation: int
    reason: int
    result: int
    aux: int
    receipt_index: int
    slot: int


@dataclass(frozen=True)
class TypeDescriptor:
    slot: int
    state: int
    id: int
    name: str
    body_size: int
    valid_ops: int
    allowed_access: int
    destroy_action: int


@dataclass(frozen=True)
class NamespaceState:
    magic: int
    version: int
    init_status: int
    root_id: int
    entry_count: int
    next_id: int
    next_epoch: int
    last_status: int
    last_reason: int
    last_parent: int
    last_object: int
    last_entry: int


@dataclass(frozen=True)
class NamespaceEntry:
    slot: int
    state: int
    id: int
    parent_id: int
    object_id: int
    name: str
    owner: int
    created_epoch: int
    generation: int


@dataclass(frozen=True)
class ReceiptExt:
    object_id: int
    handle: int
    refcount: int
    handle_count: int
    namespace_id: int


@dataclass(frozen=True)
class ObjectSnapshot:
    state: ObjectState
    objects: tuple[ObjectHeader, ...]
    handles: tuple[HandleSlot, ...]
    types: tuple[TypeDescriptor, ...]
    namespace: NamespaceState
    entries: tuple[NamespaceEntry, ...]
    request: ObjectRequest
    response: ObjectResponse
    receipts: tuple[Receipt, ...]
    ext: tuple[ReceiptExt, ...]


def word(machine: NovaMachine, address: int) -> int:
    """Read one 64-bit observation word."""
    return machine._read_mem(address, 8)


def decode_object_entry(machine: NovaMachine) -> int:
    """Return the published address of the typed object request entry."""
    return word(machine, OBJECT_ENTRY_ADDRESS)


def decode_object_state(machine: NovaMachine) -> ObjectState:
    """Decode the fixed object-manager state block."""
    def state_word(offset: int) -> int:
        return word(machine, OBJECT_STATE_ADDRESS + offset)

    return ObjectState(
        magic=state_word(0),
        version=state_word(8),
        init_status=state_word(16),
        next_id=state_word(24),
        next_epoch=state_word(32),
        object_count=state_word(40),
        handle_count=state_word(48),
        last_status=state_word(56),
        last_reason=state_word(64),
        last_object=state_word(72),
        last_handle=state_word(80),
        last_epoch=state_word(88),
        object_slots=state_word(96),
        handle_owners=state_word(104),
        handle_slots=state_word(112),
        selftest_status=state_word(120),
    )


def decode_object_headers(machine: NovaMachine) -> tuple[ObjectHeader, ...]:
    """Decode every fixed object header slot."""
    headers = []
    for index in range(OBJECT_SLOTS):
        base = OBJECT_TABLE_ADDRESS + index * OBJECT_HEADER_SIZE
        headers.append(ObjectHeader(
            slot=index,
            magic=word(machine, base + 0),
            id=word(machine, base + 8),
            type=word(machine, base + 16),
            owner=word(machine, base + 24),
            parent=word(machine, base + 32),
            permissions=word(machine, base + 40),
            refcount=word(machine, base + 48),
            handle_count=word(machine, base + 56),
            retain_count=word(machine, base + 64),
            payload=word(machine, base + 72),
            created_epoch=word(machine, base + 80),
            state=word(machine, base + 88),
            namespace_count=word(machine, base + 96),
            body=tuple(word(machine, base + offset) for offset in
                       (104, 112, 120)),
        ))
    return tuple(headers)


def decode_handle_slots(machine: NovaMachine) -> tuple[HandleSlot, ...]:
    """Decode every owner-scoped handle slot."""
    slots = []
    linear = 0
    for owner in range(1, HANDLE_OWNERS + 1):
        for index in range(HANDLE_SLOTS):
            base = HANDLE_TABLE_ADDRESS + linear * HANDLE_SLOT_SIZE
            slots.append(HandleSlot(
                owner=owner,
                index=index,
                state=word(machine, base + 0),
                generation=word(machine, base + 8),
                object_id=word(machine, base + 16),
                permissions=word(machine, base + 24),
            ))
            linear += 1
    return tuple(slots)


def decode_object_request(machine: NovaMachine) -> ObjectRequest:
    """Decode the shared object request block."""
    def request_word(offset: int) -> int:
        return word(machine, OBJECT_REQUEST_ADDRESS + offset)

    name_words = tuple(request_word(80 + index * 8) for index in
                       range(_NAME_WORDS))
    return ObjectRequest(
        magic=request_word(0),
        operation=request_word(8),
        id=request_word(16),
        owner=request_word(24),
        type=request_word(32),
        handle=request_word(40),
        permissions=request_word(48),
        payload=request_word(56),
        parent=request_word(64),
        aux=request_word(72),
        name=decode_name(name_words),
    )


def decode_object_response(machine: NovaMachine) -> ObjectResponse:
    """Decode the shared object response block."""
    def response_word(offset: int) -> int:
        return word(machine, OBJECT_RESPONSE_ADDRESS + offset)

    return ObjectResponse(
        magic=response_word(0),
        status=response_word(8),
        operation=response_word(16),
        reason=response_word(24),
        result=response_word(32),
        aux=response_word(40),
        receipt_index=response_word(48),
        slot=response_word(56),
    )


def decode_object_receipts(machine: NovaMachine) -> tuple[Receipt, ...]:
    """Decode the dedicated M5 receipt log."""
    receipts = []
    for index in range(OBJECT_RECEIPT_SLOTS):
        base = OBJECT_RECEIPT_TABLE_ADDRESS + index * OBJECT_RECEIPT_SIZE
        receipts.append(Receipt(
            magic=word(machine, base + 0),
            id=word(machine, base + 8),
            type=word(machine, base + 16),
            pu=word(machine, base + 24),
            stage=word(machine, base + 32),
            state=word(machine, base + 40),
            outcome=word(machine, base + 48),
            result=word(machine, base + 56),
            reason=word(machine, base + 64),
            trace=tuple(word(machine, base + 72 + step * 8) for step in
                        range(5)),
            reserved=(word(machine, base + 112), word(machine, base + 120)),
        ))
    return tuple(receipts)


def decode_object_receipt_exts(machine: NovaMachine) -> tuple[ReceiptExt, ...]:
    """Decode the object-specific receipt extension records."""
    records = []
    for index in range(OBJECT_RECEIPT_SLOTS):
        base = OBJECT_RECEIPT_EXT_ADDRESS + index * OBJECT_RECEIPT_EXT_SIZE
        counts = word(machine, base + 16)
        records.append(ReceiptExt(
            object_id=word(machine, base + 0),
            handle=word(machine, base + 8),
            refcount=counts >> 32,
            handle_count=counts & 0xFFFFFFFF,
            namespace_id=word(machine, base + 24),
        ))
    return tuple(records)


def decode_type_descriptors(machine: NovaMachine) -> tuple[TypeDescriptor, ...]:
    """Decode the registered object type descriptors."""
    descriptors = []
    for index in range(TYPE_SLOTS):
        base = TYPE_REGISTRY_ADDRESS + index * TYPE_DESCRIPTOR_SIZE
        descriptors.append(TypeDescriptor(
            slot=index,
            state=word(machine, base + 0),
            id=word(machine, base + 8),
            name=decode_name((
                word(machine, base + 16), word(machine, base + 24)
            )),
            body_size=word(machine, base + 32),
            valid_ops=word(machine, base + 40),
            allowed_access=word(machine, base + 48),
            destroy_action=word(machine, base + 56),
        ))
    return tuple(descriptors)


def decode_namespace_state(machine: NovaMachine) -> NamespaceState:
    """Decode the namespace observation state."""
    def state_word(offset: int) -> int:
        return word(machine, NAMESPACE_STATE_ADDRESS + offset)

    return NamespaceState(
        magic=state_word(0),
        version=state_word(8),
        init_status=state_word(16),
        root_id=state_word(24),
        entry_count=state_word(32),
        next_id=state_word(40),
        next_epoch=state_word(48),
        last_status=state_word(56),
        last_reason=state_word(64),
        last_parent=state_word(72),
        last_object=state_word(80),
        last_entry=state_word(88),
    )


def decode_namespace_entries(machine: NovaMachine) -> tuple[NamespaceEntry, ...]:
    """Decode every fixed namespace directory entry slot."""
    entries = []
    for index in range(NAMESPACE_ENTRY_SLOTS):
        base = NAMESPACE_ENTRY_TABLE_ADDRESS + index * NAMESPACE_ENTRY_SIZE
        entries.append(NamespaceEntry(
            slot=index,
            state=word(machine, base + 0),
            id=word(machine, base + 8),
            parent_id=word(machine, base + 16),
            object_id=word(machine, base + 24),
            name=decode_name(tuple(word(machine, base + 32 + step * 8)
                                   for step in range(_NAME_WORDS))),
            owner=word(machine, base + 64),
            created_epoch=word(machine, base + 72),
            generation=word(machine, base + 80),
        ))
    return tuple(entries)


def decode_object_snapshot(machine: NovaMachine) -> ObjectSnapshot:
    """Decode all M5 observation regions into one immutable snapshot."""
    return ObjectSnapshot(
        state=decode_object_state(machine),
        objects=decode_object_headers(machine),
        handles=decode_handle_slots(machine),
        types=decode_type_descriptors(machine),
        namespace=decode_namespace_state(machine),
        entries=decode_namespace_entries(machine),
        request=decode_object_request(machine),
        response=decode_object_response(machine),
        receipts=decode_object_receipts(machine),
        ext=decode_object_receipt_exts(machine),
    )


def validate_object_layout() -> None:
    """Check that all M5 observation regions are distinct and bounded."""
    ranges = (
        ("entry", OBJECT_ENTRY_ADDRESS, 8),
        ("state", OBJECT_STATE_ADDRESS, OBJECT_STATE_SIZE),
        ("objects", OBJECT_TABLE_ADDRESS, OBJECT_HEADER_SIZE * OBJECT_SLOTS),
        ("handles", HANDLE_TABLE_ADDRESS,
         HANDLE_SLOT_SIZE * HANDLE_OWNERS * HANDLE_SLOTS),
        ("request", OBJECT_REQUEST_ADDRESS, OBJECT_REQUEST_SIZE),
        ("response", OBJECT_RESPONSE_ADDRESS, OBJECT_RESPONSE_SIZE),
        ("receipts", OBJECT_RECEIPT_TABLE_ADDRESS,
         OBJECT_RECEIPT_SIZE * OBJECT_RECEIPT_SLOTS),
        ("types", TYPE_REGISTRY_ADDRESS, TYPE_DESCRIPTOR_SIZE * TYPE_SLOTS),
        ("namespace_entries", NAMESPACE_ENTRY_TABLE_ADDRESS,
         NAMESPACE_ENTRY_SIZE * NAMESPACE_ENTRY_SLOTS),
        ("namespace_state", NAMESPACE_STATE_ADDRESS, NAMESPACE_STATE_SIZE),
        ("receipt_ext", OBJECT_RECEIPT_EXT_ADDRESS,
         OBJECT_RECEIPT_EXT_SIZE * OBJECT_RECEIPT_SLOTS),
    )
    ordered = sorted(ranges, key=lambda item: item[1])
    for index in range(len(ordered) - 1):
        left_name, left_base, left_size = ordered[index]
        right_name, right_base, _ = ordered[index + 1]
        left_end = left_base + left_size
        if left_end > right_base:
            raise NpkObjectError(
                f"{left_name} region [0x{left_base:x}, 0x{left_end:x}) overlaps "
                f"{right_name} at 0x{right_base:x}"
            )

    ext_end = (OBJECT_RECEIPT_EXT_ADDRESS +
               OBJECT_RECEIPT_EXT_SIZE * OBJECT_RECEIPT_SLOTS)
    if ext_end > 0xA0000:
        raise NpkObjectError("M5 receipt extensions reach guest test code")
    if OBJECT_SLOTS == 0 or HANDLE_SLOTS == 0 or HANDLE_OWNERS == 0:
        raise NpkObjectError("M5 fixed tables must have positive capacity")


def validate_object_state(state: ObjectState) -> None:
    """Validate boot-time object state without trusting the kernel status."""
    if state.magic != OBJECT_STATE_MAGIC:
        raise NpkObjectError(f"object state magic mismatch: 0x{state.magic:x}")
    if state.version != OBJECT_STATE_VERSION:
        raise NpkObjectError(f"object state version mismatch: {state.version}")
    if state.init_status != 0:
        raise NpkObjectError(f"object init failed with status {state.init_status}")
    if state.selftest_status != 0:
        raise NpkObjectError(f"object selftest failed: {state.selftest_status}")
    if state.object_slots != OBJECT_SLOTS:
        raise NpkObjectError(f"unexpected object slots {state.object_slots}")
    if state.handle_owners != HANDLE_OWNERS:
        raise NpkObjectError(f"unexpected handle owners {state.handle_owners}")
    if state.handle_slots != HANDLE_SLOTS:
        raise NpkObjectError(f"unexpected handle slots {state.handle_slots}")
    if state.object_count > state.object_slots:
        raise NpkObjectError("object count exceeds the fixed table")
    if state.handle_count > HANDLE_OWNERS * HANDLE_SLOTS:
        raise NpkObjectError("handle count exceeds the fixed tables")
    if state.last_status not in _VALID_REQUEST_STATUSES:
        raise NpkObjectError(f"invalid last request status {state.last_status}")
    if state.last_reason not in _VALID_REASONS and state.last_reason != REASON_NONE:
        raise NpkObjectError(f"invalid last reason {state.last_reason}")


def validate_object_response(response: ObjectResponse, *, status: int,
                             operation: int, reason: int = REASON_NONE,
                             result: int = 0) -> None:
    """Validate one object response against an independent expectation."""
    if response.magic != OBJECT_RESPONSE_MAGIC:
        raise NpkObjectError(f"response magic mismatch: 0x{response.magic:x}")
    checks = (
        ("status", response.status, status),
        ("operation", response.operation, operation),
        ("reason", response.reason, reason),
        ("result", response.result, result),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise NpkObjectError(f"response {name} mismatch: {actual} != {expected}")
    if response.slot >= 4:
        raise NpkObjectError(f"response slot outside the core: {response.slot}")
    if response.receipt_index >= OBJECT_RECEIPT_SLOTS:
        raise NpkObjectError(f"receipt index out of range: {response.receipt_index}")
    if response.aux != response.receipt_index:
        raise NpkObjectError("response aux is not the receipt index")


def validate_type_descriptor(descriptor: TypeDescriptor) -> None:
    """Validate one registered type descriptor."""
    if descriptor.state != TYPE_STATE_ACTIVE:
        raise NpkObjectError(f"type {descriptor.id} is not active")
    if descriptor.id == 0 or descriptor.id > TYPE_SLOTS:
        raise NpkObjectError(f"type id outside registry: {descriptor.id}")
    if descriptor.name == "":
        raise NpkObjectError(f"type {descriptor.id} has no name")
    if descriptor.body_size > 32:
        raise NpkObjectError(f"type {descriptor.id} body is too large")
    if descriptor.allowed_access == 0 or descriptor.allowed_access > OBJ_PERM_ALL:
        raise NpkObjectError(f"type {descriptor.id} has invalid access policy")


def validate_namespace_state(state: NamespaceState) -> None:
    """Validate the namespace boot state."""
    if state.magic != NAMESPACE_MAGIC:
        raise NpkObjectError(f"namespace magic mismatch: 0x{state.magic:x}")
    if state.version != OBJECT_STATE_VERSION:
        raise NpkObjectError("namespace version mismatch")
    if state.init_status != 0:
        raise NpkObjectError("namespace init failed")
    if state.root_id != OBJECT_ID_BASE:
        raise NpkObjectError("namespace root identity changed")
    if state.entry_count > NAMESPACE_ENTRY_SLOTS:
        raise NpkObjectError("namespace entry count exceeds the table")
    if state.next_id > NAMESPACE_ENTRY_SLOTS + 1:
        raise NpkObjectError("namespace id counter exceeds the table")


def validate_receipt_ext(ext: ReceiptExt) -> None:
    """Check the packed object-specific receipt fields."""
    if ext.refcount > 0xFFFFFFFF or ext.handle_count > 0xFFFFFFFF:
        raise NpkObjectError("object receipt counts are not 32-bit values")


def write_object_request(machine: NovaMachine, *, operation: int, operation_id: int,
                         owner: int, type: int = 0, handle: int = 0,
                         permissions: int = 0, payload: int = 0, parent: int = 0,
                         aux: int = 0, name: str = "") -> None:
    """Publish one 112-byte object request block."""
    values = (
        OBJECT_REQUEST_MAGIC,
        operation,
        operation_id,
        owner,
        type,
        handle,
        permissions,
        payload,
        parent,
        aux,
        *encode_name(name),
    )
    for index, value in enumerate(values):
        machine._write_mem(OBJECT_REQUEST_ADDRESS + index * 8, value, 8)


def call_object_entry(machine: NovaMachine, *, code_address: int = 0xA0000,
                      max_steps: int = 400_000) -> None:
    """Branch to the published kernel entry using real guest instructions."""
    entry = decode_object_entry(machine)
    if entry == 0:
        raise NpkObjectError("the kernel did not publish an object request entry")

    try:
        probe = assemble_text(
            f"LI ra, {code_address}\nBL 0x{entry:x}\nHALT\n"
        )
        return_address = code_address + len(probe) - 4
        code = assemble_text(
            f"LI ra, {return_address}\nBL 0x{entry:x}\nHALT\n"
        )
    except AssemblyError as exc:
        raise NpkObjectError(f"could not build object call stub: {exc}") from exc

    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.halted = False
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    if not machine.halted:
        raise NpkObjectError("object request did not return")
    if machine.exception_code != 0:
        raise NpkObjectError(f"object request raised exception {machine.exception_code}")


def call_object_request(machine: NovaMachine, *, operation: int, operation_id: int,
                        owner: int, type: int = 0, handle: int = 0,
                        permissions: int = 0, payload: int = 0, parent: int = 0,
                        aux: int = 0, name: str = "",
                        code_address: int = 0xA0000,
                        max_steps: int = 400_000) -> ObjectResponse:
    """Run one typed object request through the real processed kernel entry."""
    write_object_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        owner=owner,
        type=type,
        handle=handle,
        permissions=permissions,
        payload=payload,
        parent=parent,
        aux=aux,
        name=name,
    )
    call_object_entry(machine, code_address=code_address, max_steps=max_steps)
    return decode_object_response(machine)


def object_receipt_expectation(
    operation_id: int,
    operation: int,
    outcome: int,
    *,
    result: int = 0,
    reason: int = REASON_NONE,
    pu: int = PU_IDLE,
    stage: int | None = None,
    state: int | None = None,
    trace: tuple[int, ...] | None = None,
) -> ReceiptExpectation:
    """Build a receipt expectation for one M5 lifecycle outcome."""
    return ReceiptExpectation(
        id=operation_id,
        type=operation,
        pu=pu,
        outcome=outcome,
        result=result,
        reason=reason,
        stage=stage,
        state=state,
        trace=trace,
    )


def validate_complete_object_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a successful M5 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_COMPLETE:
        raise NpkObjectError("object receipt is not complete")
    if receipt.stage != STAGE_COMPLETE or receipt.state != STATE_COMPLETE:
        raise NpkObjectError("complete object receipt lacks the complete stage/state")
    if receipt.trace != EXPECTED_STAGE_TRACE:
        raise NpkObjectError(f"incomplete object lifecycle trace: {receipt.trace}")


def validate_rejected_object_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a validation-rejected M5 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_REJECTED:
        raise NpkObjectError("object receipt is not rejected")
    if receipt.stage != STAGE_VALIDATE or receipt.state != STATE_REJECTED:
        raise NpkObjectError("rejected object receipt advanced past validation")
    if receipt.trace != (STAGE_ADMIT, STAGE_VALIDATE, STAGE_NONE, STAGE_NONE,
                         STAGE_NONE):
        raise NpkObjectError(f"rejected object lifecycle trace: {receipt.trace}")


def validate_failed_object_receipt(receipt: Receipt, *, stage: int) -> None:
    """Check structural invariants of an execution- or commit-failed M5 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_FAILED:
        raise NpkObjectError("object receipt is not failed")
    if stage not in (STAGE_EXECUTE, STAGE_COMMIT):
        raise NpkObjectError(f"invalid failed stage: {stage}")
    if receipt.stage != stage or receipt.state != STATE_FAILED:
        raise NpkObjectError("failed object receipt has the wrong stage/state")
