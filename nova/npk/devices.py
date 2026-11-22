"""Host-side decoding and validation of NPK Milestone 7 device processing.

The kernel has no MMIO, device tree, interrupt controller, framebuffer,
keyboard, disk, or network. This module observes only the two capabilities the
platform actually exposes through Nova ISA v1.0:

* the console sink used by ``SYS_WRITE``, which is the same bounded kernel log
  decoded by :mod:`nova.npk.syscalls` and corresponds to the reference model's
  ``machine.io_log`` evidence;
* the architectural timer and interrupt CSRs (``time``, ``timecmp``, ``ie``,
  ``ip``, ``ivec``), whose one-shot reprogramming is performed by real kernel
  code in ``nova/npk/src/devices.c``.

Interrupt tests inject events through the reference machine. They never claim
that an external device, interrupt controller, or physical hart exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assembler import AssemblyError, assemble_text
from ..emulator import NovaMachine
from ..isa import (
    EXCEPTION_CODES,
    INT_EXTERNAL,
    INT_SOFTWARE,
    INT_TIMER,
    INTERRUPT_SOURCES,
    PRIVILEGE_LEVELS,
    WORD_MASK,
)
from . import syscalls as syscall_api
from .core import (
    EXPECTED_STAGE_TRACE,
    NO_INDEX,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_REJECTED,
    REASON_NONE,
    Receipt,
    ReceiptExpectation,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_REJECTED,
    STAGE_COMPLETE,
    STAGE_EXECUTE,
    STAGE_NONE,
    STAGE_VALIDATE,
    validate_receipt,
    validate_receipt_structure,
)
from .traps import (
    CLASS_EXTERNAL_INTERRUPT,
    CLASS_SOFTWARE_INTERRUPT,
    CLASS_TIMER_INTERRUPT,
)
from .layout import (
    DEVICE_CAPABILITY_ENTRY_SIZE,
    DEVICE_CAPABILITY_SLOTS,
    DEVICE_CAPABILITY_TABLE_ADDRESS,
    DEVICE_ENTRY_ADDRESS,
    DEVICE_INTERRUPT_ENTRY_SIZE,
    DEVICE_INTERRUPT_LOG_ADDRESS,
    DEVICE_INTERRUPT_SLOTS,
    DEVICE_LAYOUT_LIMIT,
    DEVICE_OPERATION_STATE_ADDRESS,
    DEVICE_OPERATION_STATE_SIZE,
    DEVICE_REGISTRY_ADDRESS,
    DEVICE_REGISTRY_ENTRY_SIZE,
    DEVICE_REGISTRY_SLOTS,
    DEVICE_REQUEST_ADDRESS,
    DEVICE_REQUEST_SIZE,
    DEVICE_RESPONSE_ADDRESS,
    DEVICE_RESPONSE_SIZE,
    DEVICE_RECEIPT_SLOTS,
    DEVICE_RECEIPT_TABLE_ADDRESS,
    DEVICE_SCRATCH_ADDRESS,
    DEVICE_SCRATCH_SIZE,
    DEVICE_STATE_ADDRESS,
    DEVICE_STATE_MAGIC,
    DEVICE_STATE_SIZE,
    DEVICE_STATE_VERSION,
    RECEIPT_SIZE,
    TIMER_ENTRY_SIZE,
    TIMER_EVENT_ENTRY_SIZE,
    TIMER_EVENT_LOG_ADDRESS,
    TIMER_EVENT_SLOTS,
    TIMER_RECEIPT_SLOTS,
    TIMER_RECEIPT_TABLE_ADDRESS,
    TIMER_SLOTS,
    TIMER_TABLE_ADDRESS,
)

__all__ = [
    "DEVICE_KIND_CONSOLE",
    "DEVICE_KIND_TIMER",
    "DEVICE_KIND_BLOCK",
    "DEVICE_KIND_INPUT",
    "DEVICE_KIND_NETWORK",
    "DEVICE_STATUS_SUPPORTED",
    "DEVICE_STATUS_UNSUPPORTED",
    "DEVICE_CAP_WRITE",
    "DEVICE_CAP_TIME",
    "DEVICE_OPERATION_STATE_MAGIC",
    "DEVICE_OPERATION_STATE_VERSION",
    "DEVICE_REQUEST_MAGIC",
    "DEVICE_RESPONSE_MAGIC",
    "DEVICE_RECEIPT_SIZE",
    "OP_DEVICE_REGISTER",
    "OP_DEVICE_UNREGISTER",
    "OP_DEVICE_QUERY",
    "OP_DEVICE_WRITE",
    "OP_TIMER_CREATE",
    "OP_TIMER_CANCEL",
    "OP_TIMER_INSPECT",
    "OP_TIMER_EXPIRE",
    "OP_TIMER_DELIVER",
    "REASON_DEVICE_BAD_REQUEST",
    "REASON_DEVICE_BAD_KIND",
    "REASON_DEVICE_UNSUPPORTED",
    "REASON_DEVICE_NOT_FOUND",
    "REASON_DEVICE_DUPLICATE",
    "REASON_DEVICE_EXHAUSTED",
    "REASON_DEVICE_STATE",
    "REASON_DEVICE_CAPABILITY",
    "REASON_DEVICE_BAD_PARAMETER",
    "REASON_DEVICE_OWNER",
    "REASON_DEVICE_OBJECT",
    "REASON_TIMER_BAD_REQUEST",
    "REASON_TIMER_NOT_FOUND",
    "REASON_TIMER_DUPLICATE",
    "REASON_TIMER_EXHAUSTED",
    "REASON_TIMER_STATE",
    "REASON_TIMER_NOT_DUE",
    "REASON_TIMER_BAD_INTERVAL",
    "REASON_TIMER_EVENT",
    "REASON_TIMER_UNSUPPORTED",
    "REASON_DEVICE_BACKEND",
    "REQUEST_OK",
    "REQUEST_BAD_REQUEST",
    "REQUEST_BAD_OP",
    "REQUEST_ADMIT_FAIL",
    "REQUEST_ADVANCE_FAIL",
    "REQUEST_BAD_OUTCOME",
    "REQUEST_REJECTED",
    "REQUEST_FAILED",
    "REGISTRY_FREE",
    "REGISTRY_REGISTERED",
    "REGISTRY_UNREGISTERED",
    "TIMER_FREE",
    "TIMER_PENDING",
    "TIMER_EXPIRED",
    "TIMER_DELIVERED",
    "TIMER_CANCELLED",
    "SUPPORTED_DEVICE_KINDS",
    "UNSUPPORTED_DEVICE_KINDS",
    "SOURCE_CLASS",
    "NpkDeviceError",
    "DeviceState",
    "DeviceOperationState",
    "DeviceRegistryEntry",
    "TimerEntry",
    "TimerEvent",
    "DeviceRequest",
    "DeviceResponse",
    "DeviceSnapshot",
    "Capability",
    "InterruptRecord",
    "InterruptResult",
    "word",
    "validate_device_layout",
    "decode_device_entry",
    "decode_device_state",
    "decode_capabilities",
    "decode_interrupt_log",
    "decode_operation_state",
    "decode_registry",
    "decode_timers",
    "decode_timer_events",
    "decode_device_request",
    "decode_device_response",
    "decode_device_receipts",
    "decode_timer_receipts",
    "decode_device_snapshot",
    "validate_device_state",
    "validate_capabilities",
    "validate_operation_state",
    "validate_registry",
    "validate_timers",
    "validate_timer_events",
    "validate_device_response",
    "validate_complete_device_receipt",
    "validate_rejected_device_receipt",
    "validate_failed_device_receipt",
    "write_device_request",
    "call_device_entry",
    "call_device_request",
    "device_receipt_expectation",
    "console_bytes",
    "publish_console",
    "run_interrupt",
    "validate_interrupt_result",
]


DEVICE_KIND_CONSOLE = 1
DEVICE_KIND_TIMER = 2
DEVICE_KIND_BLOCK = 3
DEVICE_KIND_INPUT = 4
DEVICE_KIND_NETWORK = 5

DEVICE_STATUS_SUPPORTED = 1
DEVICE_STATUS_UNSUPPORTED = 2

SUPPORTED_DEVICE_KINDS = (DEVICE_KIND_CONSOLE, DEVICE_KIND_TIMER)
UNSUPPORTED_DEVICE_KINDS = (
    DEVICE_KIND_BLOCK,
    DEVICE_KIND_INPUT,
    DEVICE_KIND_NETWORK,
)

#: Architectural interrupt source to the M7 trap classification constant.
SOURCE_CLASS = {
    INT_SOFTWARE: CLASS_SOFTWARE_INTERRUPT,
    INT_TIMER: CLASS_TIMER_INTERRUPT,
    INT_EXTERNAL: CLASS_EXTERNAL_INTERRUPT,
}

DEVICE_OPERATION_STATE_MAGIC = 0x4E504B4F  # "NPKO"
DEVICE_OPERATION_STATE_VERSION = 1
DEVICE_REQUEST_MAGIC = 0x4E504B46  # "NPKF"
DEVICE_RESPONSE_MAGIC = 0x4E504B47  # "NPKG"
DEVICE_RECEIPT_SIZE = RECEIPT_SIZE

DEVICE_CAP_WRITE = 1
DEVICE_CAP_TIME = 2

OP_DEVICE_REGISTER = 22
OP_DEVICE_UNREGISTER = 23
OP_DEVICE_QUERY = 24
OP_DEVICE_WRITE = 25
OP_TIMER_CREATE = 26
OP_TIMER_CANCEL = 27
OP_TIMER_INSPECT = 28
OP_TIMER_EXPIRE = 29
OP_TIMER_DELIVER = 30

REASON_DEVICE_BAD_REQUEST = 57
REASON_DEVICE_BAD_KIND = 58
REASON_DEVICE_UNSUPPORTED = 59
REASON_DEVICE_NOT_FOUND = 60
REASON_DEVICE_DUPLICATE = 61
REASON_DEVICE_EXHAUSTED = 62
REASON_DEVICE_STATE = 63
REASON_DEVICE_CAPABILITY = 64
REASON_DEVICE_BAD_PARAMETER = 65
REASON_DEVICE_OWNER = 66
REASON_DEVICE_OBJECT = 67
REASON_TIMER_BAD_REQUEST = 68
REASON_TIMER_NOT_FOUND = 69
REASON_TIMER_DUPLICATE = 70
REASON_TIMER_EXHAUSTED = 71
REASON_TIMER_STATE = 72
REASON_TIMER_NOT_DUE = 73
REASON_TIMER_BAD_INTERVAL = 74
REASON_TIMER_EVENT = 75
REASON_TIMER_UNSUPPORTED = 76
REASON_DEVICE_BACKEND = 77

REQUEST_OK = 0
REQUEST_BAD_REQUEST = 1
REQUEST_BAD_OP = 2
REQUEST_ADMIT_FAIL = 3
REQUEST_ADVANCE_FAIL = 4
REQUEST_BAD_OUTCOME = 5
REQUEST_REJECTED = 6
REQUEST_FAILED = 7

REGISTRY_FREE = 0
REGISTRY_REGISTERED = 1
REGISTRY_UNREGISTERED = 2

TIMER_FREE = 0
TIMER_PENDING = 1
TIMER_EXPIRED = 2
TIMER_DELIVERED = 3
TIMER_CANCELLED = 4

_VALID_REQUEST_STATUSES = frozenset(range(REQUEST_OK, REQUEST_FAILED + 1))
_VALID_REASONS = frozenset(range(REASON_NONE, REASON_DEVICE_BACKEND + 1))
_VALID_DEVICE_OPERATIONS = frozenset(range(OP_DEVICE_REGISTER, OP_TIMER_DELIVER + 1))


class NpkDeviceError(ValueError):
    """Raised when observable device state is missing or inconsistent."""


@dataclass(frozen=True)
class DeviceState:
    """The 144-byte Milestone 7 device observation block."""

    magic: int
    version: int
    init_status: int
    interrupt_count: int
    console_calls: int
    console_bytes: int
    timer_interrupts: int
    timer_reprograms: int
    software_interrupts: int
    external_interrupts: int
    last_source: int
    last_ecause: int
    last_time: int
    last_timecmp: int
    last_ie: int
    last_ip: int
    last_ivec: int
    timer_interval: int


@dataclass(frozen=True)
class Capability:
    """One fixed device capability declaration written by the kernel."""

    kind: int
    status: int
    reserved: tuple[int, int]


@dataclass(frozen=True)
class InterruptRecord:
    """One bounded observation of an interrupt serviced by the kernel."""

    source: int
    time: int
    timecmp: int
    ie: int
    ip: int
    ivec: int
    ecause: int
    epc: int


@dataclass(frozen=True)
class InterruptResult:
    """Observable outcome of one host-injected architectural interrupt."""

    source: int
    before: DeviceState
    state: DeviceState
    record: InterruptRecord
    compare: int
    epc: int
    ivec: int


@dataclass(frozen=True)
class DeviceOperationState:
    """The 144-byte typed device/timer operation observation block."""

    magic: int
    version: int
    init_status: int
    last_status: int
    last_operation: int
    last_target: int
    last_reason: int
    last_outcome: int
    last_receipt: int
    device_operations: int
    timer_operations: int
    registered_count: int
    timer_count: int
    timer_events: int
    timer_expirations: int
    timer_deliveries: int
    timer_cancellations: int
    next_event_id: int


@dataclass(frozen=True)
class DeviceRegistryEntry:
    """One bounded device registration record."""

    state: int
    id: int
    kind: int
    capability: int
    owner: int
    object_handle: int
    operations: int
    last_result: int


@dataclass(frozen=True)
class TimerEntry:
    """One bounded software timer record."""

    state: int
    id: int
    owner: int
    deadline: int
    interval: int
    event: int
    created: int
    scratch: int


@dataclass(frozen=True)
class TimerEvent:
    """One bounded timer expiration/delivery event record."""

    event_id: int
    timer_id: int
    deadline: int
    delivered: int


@dataclass(frozen=True)
class DeviceRequest:
    """The shared 104-byte typed device/timer request block."""

    magic: int
    operation: int
    id: int
    target: int
    owner: int
    object: int
    kind: int
    capability: int
    arg0: int
    arg1: int
    arg2: int
    arg3: int
    aux: int


@dataclass(frozen=True)
class DeviceResponse:
    """The shared 80-byte typed device/timer response block."""

    magic: int
    status: int
    operation: int
    reason: int
    result: int
    target: int
    receipt_index: int
    slot: int
    error: int
    aux: int


@dataclass(frozen=True)
class DeviceSnapshot:
    """All M7 observation regions at one point in time."""

    state: DeviceState
    capabilities: tuple[Capability, ...]
    interrupts: tuple[InterruptRecord, ...]
    operation_state: DeviceOperationState
    registry: tuple[DeviceRegistryEntry, ...]
    timers: tuple[TimerEntry, ...]
    events: tuple[TimerEvent, ...]
    request: DeviceRequest
    response: DeviceResponse
    device_receipts: tuple[Receipt, ...]
    timer_receipts: tuple[Receipt, ...]


def word(machine: NovaMachine, address: int) -> int:
    """Read one 64-bit device observation word."""
    return machine._read_mem(address, 8)


def validate_device_layout() -> None:
    """Check that all M7 regions are bounded, disjoint, and ordered."""
    if DEVICE_STATE_SIZE != 144:
        raise NpkDeviceError("device state block is not 144 bytes")
    if DEVICE_OPERATION_STATE_SIZE != 144:
        raise NpkDeviceError("operation state block is not 144 bytes")
    if DEVICE_INTERRUPT_ENTRY_SIZE != 32 or DEVICE_INTERRUPT_SLOTS != 16:
        raise NpkDeviceError("unexpected interrupt observation log geometry")
    if DEVICE_CAPABILITY_ENTRY_SIZE != 32 or DEVICE_CAPABILITY_SLOTS != 8:
        raise NpkDeviceError("unexpected capability table geometry")
    if DEVICE_REGISTRY_ENTRY_SIZE != 64 or DEVICE_REGISTRY_SLOTS != 8:
        raise NpkDeviceError("unexpected device registry geometry")
    if TIMER_ENTRY_SIZE != 64 or TIMER_SLOTS != 8:
        raise NpkDeviceError("unexpected timer table geometry")
    if TIMER_EVENT_ENTRY_SIZE != 32 or TIMER_EVENT_SLOTS != 8:
        raise NpkDeviceError("unexpected timer event log geometry")
    if DEVICE_REQUEST_SIZE != 104 or DEVICE_RESPONSE_SIZE != 80:
        raise NpkDeviceError("unexpected typed request/response geometry")
    if DEVICE_RECEIPT_SLOTS != 32 or TIMER_RECEIPT_SLOTS != 32:
        raise NpkDeviceError("unexpected device/timer receipt geometry")
    if DEVICE_SCRATCH_SIZE != 64:
        raise NpkDeviceError("unexpected device scratch geometry")

    ranges = (
        ("entry", DEVICE_ENTRY_ADDRESS, 8),
        ("state", DEVICE_STATE_ADDRESS, DEVICE_STATE_SIZE),
        ("interrupt log", DEVICE_INTERRUPT_LOG_ADDRESS,
         DEVICE_INTERRUPT_SLOTS * DEVICE_INTERRUPT_ENTRY_SIZE),
        ("capability table", DEVICE_CAPABILITY_TABLE_ADDRESS,
         DEVICE_CAPABILITY_SLOTS * DEVICE_CAPABILITY_ENTRY_SIZE),
        ("request", DEVICE_REQUEST_ADDRESS, DEVICE_REQUEST_SIZE),
        ("response", DEVICE_RESPONSE_ADDRESS, DEVICE_RESPONSE_SIZE),
        ("operation state", DEVICE_OPERATION_STATE_ADDRESS,
         DEVICE_OPERATION_STATE_SIZE),
        ("registry", DEVICE_REGISTRY_ADDRESS,
         DEVICE_REGISTRY_SLOTS * DEVICE_REGISTRY_ENTRY_SIZE),
        ("timer table", TIMER_TABLE_ADDRESS, TIMER_SLOTS * TIMER_ENTRY_SIZE),
        ("timer events", TIMER_EVENT_LOG_ADDRESS,
         TIMER_EVENT_SLOTS * TIMER_EVENT_ENTRY_SIZE),
        ("device receipts", DEVICE_RECEIPT_TABLE_ADDRESS,
         DEVICE_RECEIPT_SLOTS * DEVICE_RECEIPT_SIZE),
        ("timer receipts", TIMER_RECEIPT_TABLE_ADDRESS,
         TIMER_RECEIPT_SLOTS * DEVICE_RECEIPT_SIZE),
        ("scratch", DEVICE_SCRATCH_ADDRESS, DEVICE_SCRATCH_SIZE),
    )
    if ranges[0][1] < syscall_api.SYSCALL_LAYOUT_LIMIT:
        raise NpkDeviceError("M7 entry overlaps the M6 observation region")
    previous_end = syscall_api.SYSCALL_LAYOUT_LIMIT
    for name, address, size in ranges:
        if address < previous_end:
            raise NpkDeviceError(f"{name} overlaps the previous M7 region")
        previous_end = address + size
    if previous_end > DEVICE_LAYOUT_LIMIT:
        raise NpkDeviceError(
            f"M7 region ends at 0x{previous_end:x}, past 0x{DEVICE_LAYOUT_LIMIT:x}"
        )


def decode_device_state(machine: NovaMachine) -> DeviceState:
    """Decode the kernel's fixed device dispatch state."""

    def field(offset: int) -> int:
        return word(machine, DEVICE_STATE_ADDRESS + offset)

    return DeviceState(
        magic=field(0),
        version=field(8),
        init_status=field(16),
        interrupt_count=field(24),
        console_calls=field(32),
        console_bytes=field(40),
        timer_interrupts=field(48),
        timer_reprograms=field(56),
        software_interrupts=field(64),
        external_interrupts=field(72),
        last_source=field(80),
        last_ecause=field(88),
        last_time=field(96),
        last_timecmp=field(104),
        last_ie=field(112),
        last_ip=field(120),
        last_ivec=field(128),
        timer_interval=field(136),
    )


def decode_capabilities(machine: NovaMachine) -> tuple[Capability, ...]:
    """Decode the kernel's fixed device capability declarations."""
    result = []
    for index in range(DEVICE_CAPABILITY_SLOTS):
        base = (DEVICE_CAPABILITY_TABLE_ADDRESS
                + index * DEVICE_CAPABILITY_ENTRY_SIZE)
        result.append(Capability(
            kind=word(machine, base),
            status=word(machine, base + 8),
            reserved=(word(machine, base + 16), word(machine, base + 24)),
        ))
    return tuple(result)


def decode_interrupt_log(machine: NovaMachine) -> tuple[InterruptRecord, ...]:
    """Decode the bounded interrupt observation ring."""
    result = []
    for index in range(DEVICE_INTERRUPT_SLOTS):
        base = (DEVICE_INTERRUPT_LOG_ADDRESS
                + index * DEVICE_INTERRUPT_ENTRY_SIZE)
        result.append(InterruptRecord(
            source=word(machine, base),
            time=word(machine, base + 8),
            timecmp=word(machine, base + 16),
            ie=word(machine, base + 24),
            ip=word(machine, base + 32),
            ivec=word(machine, base + 40),
            ecause=word(machine, base + 48),
            epc=word(machine, base + 56),
        ))
    return tuple(result)


def decode_device_entry(machine: NovaMachine) -> int:
    """Return the published address of the typed device request entry."""
    return word(machine, DEVICE_ENTRY_ADDRESS)


def decode_operation_state(machine: NovaMachine) -> DeviceOperationState:
    """Decode the typed device/timer operation observation block."""
    def state_word(offset: int) -> int:
        return word(machine, DEVICE_OPERATION_STATE_ADDRESS + offset)

    return DeviceOperationState(
        magic=state_word(0),
        version=state_word(8),
        init_status=state_word(16),
        last_status=state_word(24),
        last_operation=state_word(32),
        last_target=state_word(40),
        last_reason=state_word(48),
        last_outcome=state_word(56),
        last_receipt=state_word(64),
        device_operations=state_word(72),
        timer_operations=state_word(80),
        registered_count=state_word(88),
        timer_count=state_word(96),
        timer_events=state_word(104),
        timer_expirations=state_word(112),
        timer_deliveries=state_word(120),
        timer_cancellations=state_word(128),
        next_event_id=state_word(136),
    )


def decode_registry(machine: NovaMachine) -> tuple[DeviceRegistryEntry, ...]:
    """Decode every bounded device registration record."""
    entries = []
    for index in range(DEVICE_REGISTRY_SLOTS):
        base = DEVICE_REGISTRY_ADDRESS + index * DEVICE_REGISTRY_ENTRY_SIZE
        entries.append(DeviceRegistryEntry(
            state=word(machine, base + 0),
            id=word(machine, base + 8),
            kind=word(machine, base + 16),
            capability=word(machine, base + 24),
            owner=word(machine, base + 32),
            object_handle=word(machine, base + 40),
            operations=word(machine, base + 48),
            last_result=word(machine, base + 56),
        ))
    return tuple(entries)


def decode_timers(machine: NovaMachine) -> tuple[TimerEntry, ...]:
    """Decode every bounded software timer record."""
    entries = []
    for index in range(TIMER_SLOTS):
        base = TIMER_TABLE_ADDRESS + index * TIMER_ENTRY_SIZE
        entries.append(TimerEntry(
            state=word(machine, base + 0),
            id=word(machine, base + 8),
            owner=word(machine, base + 16),
            deadline=word(machine, base + 24),
            interval=word(machine, base + 32),
            event=word(machine, base + 40),
            created=word(machine, base + 48),
            scratch=word(machine, base + 56),
        ))
    return tuple(entries)


def decode_timer_events(machine: NovaMachine) -> tuple[TimerEvent, ...]:
    """Decode every bounded timer expiration/delivery record."""
    entries = []
    for index in range(TIMER_EVENT_SLOTS):
        base = TIMER_EVENT_LOG_ADDRESS + index * TIMER_EVENT_ENTRY_SIZE
        entries.append(TimerEvent(
            event_id=word(machine, base + 0),
            timer_id=word(machine, base + 8),
            deadline=word(machine, base + 16),
            delivered=word(machine, base + 24),
        ))
    return tuple(entries)


def decode_device_request(machine: NovaMachine) -> DeviceRequest:
    """Decode the shared typed device/timer request block."""
    def request_word(offset: int) -> int:
        return word(machine, DEVICE_REQUEST_ADDRESS + offset)

    return DeviceRequest(
        magic=request_word(0),
        operation=request_word(8),
        id=request_word(16),
        target=request_word(24),
        owner=request_word(32),
        object=request_word(40),
        kind=request_word(48),
        capability=request_word(56),
        arg0=request_word(64),
        arg1=request_word(72),
        arg2=request_word(80),
        arg3=request_word(88),
        aux=request_word(96),
    )


def decode_device_response(machine: NovaMachine) -> DeviceResponse:
    """Decode the shared typed device/timer response block."""
    def response_word(offset: int) -> int:
        return word(machine, DEVICE_RESPONSE_ADDRESS + offset)

    return DeviceResponse(
        magic=response_word(0),
        status=response_word(8),
        operation=response_word(16),
        reason=response_word(24),
        result=response_word(32),
        target=response_word(40),
        receipt_index=response_word(48),
        slot=response_word(56),
        error=response_word(64),
        aux=response_word(72),
    )


def _decode_receipt_table(machine: NovaMachine, base: int,
                          slots: int) -> tuple[Receipt, ...]:
    receipts = []
    for index in range(slots):
        address = base + index * DEVICE_RECEIPT_SIZE
        receipts.append(Receipt(
            magic=word(machine, address + 0),
            id=word(machine, address + 8),
            type=word(machine, address + 16),
            pu=word(machine, address + 24),
            stage=word(machine, address + 32),
            state=word(machine, address + 40),
            outcome=word(machine, address + 48),
            result=word(machine, address + 56),
            reason=word(machine, address + 64),
            trace=tuple(word(machine, address + 72 + step * 8)
                        for step in range(5)),
            reserved=(word(machine, address + 112),
                      word(machine, address + 120)),
        ))
    return tuple(receipts)


def decode_device_receipts(machine: NovaMachine) -> tuple[Receipt, ...]:
    """Decode the dedicated device receipt log."""
    return _decode_receipt_table(machine, DEVICE_RECEIPT_TABLE_ADDRESS,
                                 DEVICE_RECEIPT_SLOTS)


def decode_timer_receipts(machine: NovaMachine) -> tuple[Receipt, ...]:
    """Decode the dedicated timer receipt log."""
    return _decode_receipt_table(machine, TIMER_RECEIPT_TABLE_ADDRESS,
                                 TIMER_RECEIPT_SLOTS)


def decode_device_snapshot(machine: NovaMachine) -> DeviceSnapshot:
    """Decode all M7 observation regions into one immutable snapshot."""
    return DeviceSnapshot(
        state=decode_device_state(machine),
        capabilities=decode_capabilities(machine),
        interrupts=decode_interrupt_log(machine),
        operation_state=decode_operation_state(machine),
        registry=decode_registry(machine),
        timers=decode_timers(machine),
        events=decode_timer_events(machine),
        request=decode_device_request(machine),
        response=decode_device_response(machine),
        device_receipts=decode_device_receipts(machine),
        timer_receipts=decode_timer_receipts(machine),
    )


def validate_device_state(state: DeviceState) -> None:
    """Check invariant properties of the device state block."""
    if state.magic != DEVICE_STATE_MAGIC:
        raise NpkDeviceError(f"device state magic mismatch: 0x{state.magic:x}")
    if state.version != DEVICE_STATE_VERSION:
        raise NpkDeviceError(f"device state version mismatch: {state.version}")
    if state.init_status != 0:
        raise NpkDeviceError(f"device initialization failed: {state.init_status}")
    if state.timer_interval == 0 or state.timer_interval & 3:
        raise NpkDeviceError(
            f"timer interval is not a positive aligned value: {state.timer_interval}"
        )
    if state.interrupt_count != (state.timer_interrupts
                                 + state.software_interrupts
                                 + state.external_interrupts):
        raise NpkDeviceError("interrupt counters do not sum to the dispatch count")
    if state.last_source > INT_EXTERNAL:
        raise NpkDeviceError(f"invalid interrupt source: {state.last_source}")
    if state.last_ie & ~0x7 or state.last_ip & ~0x7:
        raise NpkDeviceError("interrupt CSR observation has reserved bits set")
    if state.last_ivec & 3:
        raise NpkDeviceError("interrupt vector observation is not aligned")


def validate_capabilities(capabilities: tuple[Capability, ...]) -> None:
    """Check the exact supported/rejected capability declarations."""
    if len(capabilities) != DEVICE_CAPABILITY_SLOTS:
        raise NpkDeviceError("capability table has the wrong length")
    by_kind = {capability.kind: capability for capability in capabilities
               if capability.kind}
    expected = {kind: DEVICE_STATUS_SUPPORTED for kind in SUPPORTED_DEVICE_KINDS}
    expected.update({kind: DEVICE_STATUS_UNSUPPORTED
                     for kind in UNSUPPORTED_DEVICE_KINDS})
    if set(by_kind) != set(expected):
        raise NpkDeviceError("capability table kinds do not match the platform")
    for kind, status in expected.items():
        if by_kind[kind].status != status:
            raise NpkDeviceError(
                f"capability {kind} status mismatch: {by_kind[kind].status}"
            )
    for capability in capabilities:
        if capability.kind and capability.reserved != (0, 0):
            raise NpkDeviceError("capability has nonzero reserved fields")


def validate_operation_state(state: DeviceOperationState) -> None:
    """Check invariant properties of the typed operation observation block."""
    if state.magic != DEVICE_OPERATION_STATE_MAGIC:
        raise NpkDeviceError(
            f"device operation state magic mismatch: 0x{state.magic:x}"
        )
    if state.version != DEVICE_OPERATION_STATE_VERSION:
        raise NpkDeviceError(
            f"device operation state version mismatch: {state.version}"
        )
    if state.init_status != 0:
        raise NpkDeviceError(f"device operation initialization failed: {state.init_status}")
    if state.last_status not in _VALID_REQUEST_STATUSES:
        raise NpkDeviceError(f"invalid last request status: {state.last_status}")
    if state.last_operation not in _VALID_DEVICE_OPERATIONS:
        raise NpkDeviceError(f"invalid last device operation: {state.last_operation}")
    if state.last_reason not in _VALID_REASONS:
        raise NpkDeviceError(f"invalid last device reason: {state.last_reason}")
    if state.last_outcome not in (0, OUTCOME_PENDING, OUTCOME_COMPLETE,
                                  OUTCOME_REJECTED, OUTCOME_FAILED):
        raise NpkDeviceError(f"invalid last outcome: {state.last_outcome}")
    if state.last_receipt != NO_INDEX and state.last_receipt >= 32:
        raise NpkDeviceError(f"last receipt index out of range: {state.last_receipt}")
    if state.registered_count > DEVICE_REGISTRY_SLOTS:
        raise NpkDeviceError("registered device count exceeds the registry")
    if state.timer_count > TIMER_SLOTS:
        raise NpkDeviceError("timer count exceeds the timer table")
    if state.next_event_id == 0:
        raise NpkDeviceError("timer event ids must start at one")
    if state.timer_events > state.next_event_id - 1:
        raise NpkDeviceError("timer event count exceeds allocated ids")


def _supported_kind(kind: int) -> bool:
    return kind in SUPPORTED_DEVICE_KINDS


def _capability_for_kind(kind: int) -> int:
    if kind == DEVICE_KIND_CONSOLE:
        return DEVICE_CAP_WRITE
    if kind == DEVICE_KIND_TIMER:
        return DEVICE_CAP_TIME
    return 0


def validate_registry(entries: tuple[DeviceRegistryEntry, ...]) -> None:
    """Check bounded device registry invariants."""
    if len(entries) != DEVICE_REGISTRY_SLOTS:
        raise NpkDeviceError("device registry has the wrong length")
    seen: set[int] = set()
    for entry in entries:
        if entry.state not in (REGISTRY_FREE, REGISTRY_REGISTERED,
                               REGISTRY_UNREGISTERED):
            raise NpkDeviceError(f"invalid registry state: {entry.state}")
        if entry.state == REGISTRY_FREE:
            if (entry.id or entry.kind or entry.owner or entry.object_handle or
                    entry.operations or entry.last_result):
                raise NpkDeviceError("free registry slot retains stale fields")
            continue
        if entry.id == 0:
            raise NpkDeviceError("registered device has a zero id")
        if entry.id in seen:
            raise NpkDeviceError(f"duplicate device id in registry: {entry.id}")
        seen.add(entry.id)
        if not _supported_kind(entry.kind):
            raise NpkDeviceError(f"invalid registered device kind: {entry.kind}")
        if entry.capability != _capability_for_kind(entry.kind):
            raise NpkDeviceError("registered device capability does not match its kind")
        if entry.owner > 2:
            raise NpkDeviceError("registered device owner is out of range")


def validate_timers(entries: tuple[TimerEntry, ...]) -> None:
    """Check bounded software timer invariants."""
    if len(entries) != TIMER_SLOTS:
        raise NpkDeviceError("timer table has the wrong length")
    seen: set[int] = set()
    for entry in entries:
        if entry.state not in (TIMER_FREE, TIMER_PENDING, TIMER_EXPIRED,
                               TIMER_DELIVERED, TIMER_CANCELLED):
            raise NpkDeviceError(f"invalid timer state: {entry.state}")
        if entry.state == TIMER_FREE:
            if any((entry.id, entry.owner, entry.deadline, entry.interval,
                    entry.event, entry.created, entry.scratch)):
                raise NpkDeviceError("free timer slot retains stale fields")
            continue
        if entry.id == 0 or entry.id in seen:
            raise NpkDeviceError("timer ids must be unique and nonzero")
        seen.add(entry.id)
        if entry.owner > 2:
            raise NpkDeviceError("timer owner is out of range")
        if entry.deadline == 0 or entry.interval == 0:
            raise NpkDeviceError("timer deadline and interval must be nonzero")
        if entry.state in (TIMER_PENDING, TIMER_CANCELLED) and entry.event != 0:
            raise NpkDeviceError("pending or cancelled timer has an event id")
        if entry.state in (TIMER_EXPIRED, TIMER_DELIVERED) and entry.event == 0:
            raise NpkDeviceError("expired timer lacks an event id")


def validate_timer_events(events: tuple[TimerEvent, ...]) -> None:
    """Check bounded timer event log invariants."""
    if len(events) != TIMER_EVENT_SLOTS:
        raise NpkDeviceError("timer event log has the wrong length")
    seen: set[int] = set()
    for event in events:
        if event.event_id == 0:
            continue
        if event.event_id in seen:
            raise NpkDeviceError("duplicate timer event id")
        seen.add(event.event_id)
        if event.timer_id == 0 or event.deadline == 0:
            raise NpkDeviceError("timer event lacks its source identity")
        if event.delivered not in (0, 1):
            raise NpkDeviceError("timer event delivery flag is invalid")


def validate_device_response(response: DeviceResponse) -> None:
    """Check structural invariants of one device/timer response."""
    if response.magic != DEVICE_RESPONSE_MAGIC:
        raise NpkDeviceError(f"response magic mismatch: 0x{response.magic:x}")
    if response.status not in _VALID_REQUEST_STATUSES:
        raise NpkDeviceError(f"invalid response status: {response.status}")
    if response.operation not in _VALID_DEVICE_OPERATIONS:
        raise NpkDeviceError(f"invalid response operation: {response.operation}")
    if response.reason not in _VALID_REASONS:
        raise NpkDeviceError(f"invalid response reason: {response.reason}")
    if response.slot >= 4 and response.slot != NO_INDEX:
        raise NpkDeviceError(f"response slot outside the core: {response.slot}")
    if response.receipt_index >= 32 and response.receipt_index != NO_INDEX:
        raise NpkDeviceError(
            f"receipt index out of range: {response.receipt_index}"
        )


def device_receipt_expectation(
    operation_id: int,
    operation: int,
    outcome: int,
    *,
    result: int = 0,
    reason: int = REASON_NONE,
    pu: int = 0,
    stage: int | None = None,
    state: int | None = None,
    trace: tuple[int, ...] | None = None,
) -> ReceiptExpectation:
    """Build a receipt expectation for one M7 lifecycle outcome."""
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


def validate_complete_device_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a successful M7 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_COMPLETE:
        raise NpkDeviceError("device receipt is not complete")
    if receipt.stage != STAGE_COMPLETE or receipt.state != STATE_COMPLETE:
        raise NpkDeviceError("complete device receipt lacks the complete stage/state")
    if receipt.trace != EXPECTED_STAGE_TRACE:
        raise NpkDeviceError("complete device receipt has an invalid stage trace")


def validate_rejected_device_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a rejected M7 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_REJECTED:
        raise NpkDeviceError("device receipt is not rejected")
    if receipt.state != STATE_REJECTED:
        raise NpkDeviceError("rejected device receipt lacks STATE_REJECTED")
    if receipt.stage > STAGE_VALIDATE:
        raise NpkDeviceError("rejected device receipt reached execution")


def validate_failed_device_receipt(receipt: Receipt) -> None:
    """Check structural invariants of a failed M7 receipt."""
    validate_receipt_structure(receipt)
    if receipt.outcome != OUTCOME_FAILED:
        raise NpkDeviceError("device receipt is not failed")
    if receipt.state != STATE_FAILED:
        raise NpkDeviceError("failed device receipt lacks STATE_FAILED")
    if receipt.stage not in (STAGE_EXECUTE, STAGE_COMMIT):
        raise NpkDeviceError("failed device receipt lacks an execution stage")
    if receipt.trace[2] != STAGE_EXECUTE:
        raise NpkDeviceError("failed device receipt did not execute")


def console_bytes(machine: NovaMachine) -> bytes:
    """Return the bytes committed by NPK's console backend."""
    validate_device_layout()
    return syscall_api.decode_write_log(machine)


def publish_console(machine: NovaMachine) -> bytes:
    """Mirror the NPK console sink into the reference ``io_log`` evidence.

    This is host-side observation only. Nova has no I/O instruction and NPK
    does not call the Phase 1 ``dispatch_syscall`` helper.
    """
    data = console_bytes(machine)
    machine.io_log.append((1, data))
    return data


def run_interrupt(machine: NovaMachine, source: int, *, interval: int | None = None,
                  code_address: int = 0xA0000,
                  max_steps: int = 400_000) -> InterruptResult:
    """Inject one architectural interrupt and return its kernel observations."""
    validate_device_layout()
    if source not in INTERRUPT_SOURCES:
        raise NpkDeviceError(f"unknown interrupt source: {source}")
    # A future timer compare must not let the interrupted instruction stream
    # wander into unrelated memory before the interrupt arrives. Branching to
    # itself keeps the saved EPC stable until MRET returns here.
    try:
        code = assemble_text("spin: BEQ r0, r0, spin\n")
    except AssemblyError as exc:
        raise NpkDeviceError(f"could not build interrupt stub: {exc}") from exc

    before = decode_device_state(machine)
    validate_device_state(before)
    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.priv = PRIVILEGE_LEVELS["user"]
    machine.halted = False
    machine.exception_code = EXCEPTION_CODES["NONE"]

    machine.write_csr("ip", 0)
    machine.write_csr("ie", 1 << source)
    if source == INT_TIMER:
        value = before.timer_interval if interval is None else interval
        if value <= 0 or value & 3:
            raise NpkDeviceError("timer interval must be positive and aligned")
        compare = (machine.read_csr("time") + value) & ~3
        machine.write_csr("timecmp", compare)
    else:
        machine.write_csr("timecmp", WORD_MASK)
        if source == INT_SOFTWARE:
            machine.send_ipi(machine.coreid)
        else:
            machine.set_interrupt_pending(source)
    compare = machine.read_csr("timecmp")

    seen_exception = False
    for _ in range(max_steps):
        if (seen_exception and machine.exception_code == EXCEPTION_CODES["NONE"]
                and machine.pc == code_address):
            break
        if machine.halted:
            raise NpkDeviceError("interrupt stub halted instead of returning")
        machine.step()
        if machine.exception_code != EXCEPTION_CODES["NONE"]:
            seen_exception = True
    else:
        raise NpkDeviceError("interrupt did not return through the kernel vector")
    if machine.exception_code != EXCEPTION_CODES["NONE"]:
        raise NpkDeviceError(
            f"interrupt returned with exception {machine.exception_code}"
        )

    after = decode_device_state(machine)
    validate_device_state(after)
    if after.interrupt_count != before.interrupt_count + 1:
        raise NpkDeviceError("kernel did not record exactly one interrupt")
    records = decode_interrupt_log(machine)
    record = records[(after.interrupt_count - 1) % DEVICE_INTERRUPT_SLOTS]
    return InterruptResult(
        source=source,
        before=before,
        state=after,
        record=record,
        compare=compare,
        epc=code_address,
        ivec=machine.read_csr("ivec"),
    )


def validate_interrupt_result(result: InterruptResult) -> None:
    """Validate one interrupt result against independent kernel observations."""
    if result.record.source != result.source:
        raise NpkDeviceError("interrupt record has the wrong source")
    if result.record.epc != result.epc:
        raise NpkDeviceError("interrupt record has the wrong return PC")
    if result.record.ivec != result.ivec:
        raise NpkDeviceError("interrupt record has the wrong vector")
    if not result.record.ie & (1 << result.source):
        raise NpkDeviceError("interrupt was not enabled in the saved ie value")
    if result.state.last_source != result.source:
        raise NpkDeviceError("device state has the wrong last source")
    if result.state.last_ecause != result.source:
        raise NpkDeviceError("device state has the wrong last ecause")
    if result.record.time != result.state.last_time:
        raise NpkDeviceError("record time does not match device state")
    if result.record.timecmp != result.state.last_timecmp:
        raise NpkDeviceError("record timecmp does not match device state")
    field = {
        INT_SOFTWARE: "software_interrupts",
        INT_TIMER: "timer_interrupts",
        INT_EXTERNAL: "external_interrupts",
    }[result.source]
    if getattr(result.state, field) != getattr(result.before, field) + 1:
        raise NpkDeviceError(f"{field} counter did not increment exactly once")


def write_device_request(machine: NovaMachine, *, operation: int, operation_id: int,
                         target: int = 0, owner: int = 0, object: int = 0,
                         kind: int = 0, capability: int = 0,
                         args: tuple[int, int, int, int] = (0, 0, 0, 0),
                         aux: int = 0) -> None:
    """Publish one typed 104-byte device/timer request block."""
    if len(args) != 4:
        raise NpkDeviceError("device requests carry exactly four arguments")
    values = (
        DEVICE_REQUEST_MAGIC,
        operation,
        operation_id,
        target,
        owner,
        object,
        kind,
        capability,
        args[0],
        args[1],
        args[2],
        args[3],
        aux,
    )
    for index, value in enumerate(values):
        machine._write_mem(DEVICE_REQUEST_ADDRESS + index * 8, value, 8)


def call_device_entry(machine: NovaMachine, *, code_address: int = 0xA0000,
                      max_steps: int = 400_000) -> None:
    """Branch to the published device entry using real guest instructions."""
    validate_device_layout()
    entry = decode_device_entry(machine)
    if entry == 0:
        raise NpkDeviceError("the kernel did not publish a device request entry")

    try:
        probe = assemble_text(
            f"LI ra, {code_address}\nBL 0x{entry:x}\nHALT\n"
        )
        return_address = code_address + len(probe) - 4
        code = assemble_text(
            f"LI ra, {return_address}\nBL 0x{entry:x}\nHALT\n"
        )
    except AssemblyError as exc:
        raise NpkDeviceError(f"could not build device call stub: {exc}") from exc

    machine.load_code(code, code_address)
    machine.pc = code_address
    machine.halted = False
    for _ in range(max_steps):
        if machine.halted:
            break
        machine.run(1)
    if not machine.halted:
        raise NpkDeviceError("device request did not return")
    if machine.exception_code != 0:
        raise NpkDeviceError(
            f"device request raised exception {machine.exception_code}"
        )


def call_device_request(machine: NovaMachine, *, operation: int, operation_id: int,
                        target: int = 0, owner: int = 0, object: int = 0,
                        kind: int = 0, capability: int = 0,
                        args: tuple[int, int, int, int] = (0, 0, 0, 0),
                        aux: int = 0, code_address: int = 0xA0000,
                        max_steps: int = 400_000) -> DeviceResponse:
    """Run one typed device/timer request through the real processed kernel."""
    write_device_request(
        machine,
        operation=operation,
        operation_id=operation_id,
        target=target,
        owner=owner,
        object=object,
        kind=kind,
        capability=capability,
        args=args,
        aux=aux,
    )
    call_device_entry(machine, code_address=code_address, max_steps=max_steps)
    return decode_device_response(machine)
