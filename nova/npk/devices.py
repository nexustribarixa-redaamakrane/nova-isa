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
from .traps import (
    CLASS_EXTERNAL_INTERRUPT,
    CLASS_SOFTWARE_INTERRUPT,
    CLASS_TIMER_INTERRUPT,
)
from .layout import (
    DEVICE_CAPABILITY_ENTRY_SIZE,
    DEVICE_CAPABILITY_SLOTS,
    DEVICE_CAPABILITY_TABLE_ADDRESS,
    DEVICE_INTERRUPT_ENTRY_SIZE,
    DEVICE_INTERRUPT_LOG_ADDRESS,
    DEVICE_INTERRUPT_SLOTS,
    DEVICE_LAYOUT_LIMIT,
    DEVICE_STATE_ADDRESS,
    DEVICE_STATE_MAGIC,
    DEVICE_STATE_SIZE,
    DEVICE_STATE_VERSION,
)

__all__ = [
    "DEVICE_KIND_CONSOLE",
    "DEVICE_KIND_TIMER",
    "DEVICE_KIND_BLOCK",
    "DEVICE_KIND_INPUT",
    "DEVICE_KIND_NETWORK",
    "DEVICE_STATUS_SUPPORTED",
    "DEVICE_STATUS_UNSUPPORTED",
    "SUPPORTED_DEVICE_KINDS",
    "UNSUPPORTED_DEVICE_KINDS",
    "SOURCE_CLASS",
    "NpkDeviceError",
    "DeviceState",
    "Capability",
    "InterruptRecord",
    "InterruptResult",
    "word",
    "validate_device_layout",
    "decode_device_state",
    "decode_capabilities",
    "decode_interrupt_log",
    "validate_device_state",
    "validate_capabilities",
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


def word(machine: NovaMachine, address: int) -> int:
    """Read one 64-bit device observation word."""
    return machine._read_mem(address, 8)


def validate_device_layout() -> None:
    """Check that M7 regions are bounded and do not overwrite earlier evidence."""
    if DEVICE_STATE_SIZE != 144:
        raise NpkDeviceError("device state block is not 144 bytes")
    if DEVICE_INTERRUPT_ENTRY_SIZE != 32 or DEVICE_INTERRUPT_SLOTS != 16:
        raise NpkDeviceError("unexpected interrupt observation log geometry")
    if DEVICE_CAPABILITY_ENTRY_SIZE != 32 or DEVICE_CAPABILITY_SLOTS != 8:
        raise NpkDeviceError("unexpected capability table geometry")
    if DEVICE_STATE_ADDRESS < syscall_api.SYSCALL_LAYOUT_LIMIT:
        raise NpkDeviceError("M7 state overlaps the M6 observation region")
    state_end = DEVICE_STATE_ADDRESS + DEVICE_STATE_SIZE
    log_end = (DEVICE_INTERRUPT_LOG_ADDRESS
               + DEVICE_INTERRUPT_SLOTS * DEVICE_INTERRUPT_ENTRY_SIZE)
    cap_end = (DEVICE_CAPABILITY_TABLE_ADDRESS
               + DEVICE_CAPABILITY_SLOTS * DEVICE_CAPABILITY_ENTRY_SIZE)
    if DEVICE_INTERRUPT_LOG_ADDRESS < state_end:
        raise NpkDeviceError("interrupt log overlaps the device state")
    if DEVICE_CAPABILITY_TABLE_ADDRESS < log_end:
        raise NpkDeviceError("capability table overlaps the interrupt log")
    if cap_end > DEVICE_LAYOUT_LIMIT:
        raise NpkDeviceError(
            f"M7 region ends at 0x{cap_end:x}, past 0x{DEVICE_LAYOUT_LIMIT:x}"
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
