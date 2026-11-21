"""Milestone 7 device and timer integration tests.

Every observation comes from real kernel code: console bytes pass through the
same ``SYS_WRITE`` trap path as Milestone 6, and interrupt records are written by
the Milestone 3 vector after the Milestone 7 classifier and timer backend run.
The host only injects architectural interrupts and decodes bounded state.
"""

from __future__ import annotations

import pytest

from nova import npk
from nova.isa import INT_EXTERNAL, INT_SOFTWARE, INT_TIMER, PRIVILEGE_LEVELS
from nova.npk import devices, traps

CODE_ADDRESS = 0xA0000
MAP_VA = 0x8000000000
MAP_FLAGS = npk.PERM_READ | npk.PERM_WRITE | npk.PERM_USER
OWNER = 1


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return npk.load_kernel_image(
        npk.build_kernel(tmp_path_factory.mktemp("npk-m7"))
    )


def fresh_machine(image):
    result = npk.boot_kernel(image)
    assert result.status == 0
    machine = result.machine
    machine.halted = False
    return machine


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
    for offset, value in enumerate(data):
        address = physical + offset
        word = machine._read_mem(address & ~7, 8)
        shift = (address & 7) * 8
        word = (word & ~(0xFF << shift)) | (value << shift)
        machine._write_mem(address & ~7, word, 8)


def test_milestone7_layout_is_bounded_and_disjoint():
    assert npk.DEVICE_STATE_ADDRESS == 0xA1000
    assert npk.DEVICE_STATE_SIZE == 144
    assert npk.DEVICE_INTERRUPT_LOG_ADDRESS == 0xA1090
    assert npk.DEVICE_INTERRUPT_SLOTS == 16
    assert npk.DEVICE_CAPABILITY_TABLE_ADDRESS == 0xA1290
    assert npk.DEVICE_CAPABILITY_SLOTS == 8
    devices.validate_device_layout()


def test_device_initialization_publishes_only_real_capabilities(image):
    machine = fresh_machine(image)
    state = devices.decode_device_state(machine)
    caps = devices.decode_capabilities(machine)
    devices.validate_device_state(state)
    devices.validate_capabilities(caps)

    assert state.magic == npk.DEVICE_STATE_MAGIC
    assert state.version == npk.DEVICE_STATE_VERSION
    assert state.init_status == 0
    assert state.interrupt_count == 0
    assert state.console_calls == 0
    assert state.console_bytes == 0
    assert state.timer_interval > 0 and state.timer_interval % 4 == 0
    assert machine.read_csr("ie") == 0
    assert machine.read_csr("ip") == 0
    assert machine.read_csr("ivec") == machine.vbar
    assert machine.read_csr("timecmp") == 0xFFFFFFFFFFFFFFFF
    assert tuple(item.status for item in caps[:2]) == (
        devices.DEVICE_STATUS_SUPPORTED,
        devices.DEVICE_STATUS_SUPPORTED,
    )
    assert tuple(item.status for item in caps[2:5]) == (
        devices.DEVICE_STATUS_UNSUPPORTED,
        devices.DEVICE_STATUS_UNSUPPORTED,
        devices.DEVICE_STATUS_UNSUPPORTED,
    )


def test_console_backend_uses_the_real_sys_write_path(image):
    machine = fresh_machine(image)
    physical = setup_mapped_page(machine, operation_id_base=200)
    payload = b"NPK M7 console\n"
    write_physical_bytes(machine, physical, payload)
    before = devices.decode_device_state(machine)

    outcome = npk.call_syscall(machine, npk.SYS_WRITE, 1, MAP_VA, len(payload))

    assert outcome.a0 == len(payload)
    assert outcome.a1 == npk.ERR_OK
    assert npk.decode_write_log(machine) == payload
    assert devices.console_bytes(machine) == payload
    assert devices.publish_console(machine) == payload
    assert machine.io_log[-1] == (1, payload)

    after = devices.decode_device_state(machine)
    devices.validate_device_state(after)
    assert after.console_calls == before.console_calls + 1
    assert after.console_bytes == before.console_bytes + len(payload)
    assert traps.decode_trap_state(machine).classification == traps.CLASS_SYSTEM_CALL


@pytest.mark.parametrize(
    ("source", "classification", "counter"),
    [
        (INT_SOFTWARE, traps.CLASS_SOFTWARE_INTERRUPT, "software_interrupts"),
        (INT_TIMER, traps.CLASS_TIMER_INTERRUPT, "timer_interrupts"),
        (INT_EXTERNAL, traps.CLASS_EXTERNAL_INTERRUPT, "external_interrupts"),
    ],
)
def test_interrupts_are_classified_recorded_and_returned(
        image, source, classification, counter):
    machine = fresh_machine(image)
    result = devices.run_interrupt(machine, source)
    devices.validate_interrupt_result(result)

    assert machine.pc == CODE_ADDRESS
    assert machine.priv == PRIVILEGE_LEVELS["user"]
    assert machine.exception_code == 0
    assert machine.irq_log[-1] == source
    state = traps.decode_trap_state(machine)
    traps.validate_trap_state(state)
    assert state.last_ecode == 8
    assert state.last_ecause == source
    assert state.classification == classification
    assert state.action == traps.ACTION_SERVICE_INTERRUPT
    assert getattr(result.state, counter) == getattr(result.before, counter) + 1
    assert devices.SOURCE_CLASS[source] == classification


def test_timer_handler_reprograms_the_one_shot_compare(image):
    machine = fresh_machine(image)
    result = devices.run_interrupt(machine, INT_TIMER)
    devices.validate_interrupt_result(result)

    assert result.compare > 0
    assert result.state.timer_reprograms == result.before.timer_reprograms + 1
    assert result.record.timecmp > result.compare
    assert result.record.timecmp % 4 == 0
    assert machine.read_csr("timecmp") == result.record.timecmp
    assert result.record.ie & (1 << INT_TIMER)
    assert result.record.ip == 0


def test_unsupported_device_kinds_are_explicitly_rejected(image):
    machine = fresh_machine(image)
    caps = devices.decode_capabilities(machine)
    devices.validate_capabilities(caps)
    rejected = {item.kind: item.status for item in caps
                if item.kind in devices.UNSUPPORTED_DEVICE_KINDS}
    assert rejected == {
        devices.DEVICE_KIND_BLOCK: devices.DEVICE_STATUS_UNSUPPORTED,
        devices.DEVICE_KIND_INPUT: devices.DEVICE_STATUS_UNSUPPORTED,
        devices.DEVICE_KIND_NETWORK: devices.DEVICE_STATUS_UNSUPPORTED,
    }


def test_interrupt_helper_rejects_unknown_sources(image):
    machine = fresh_machine(image)
    with pytest.raises(devices.NpkDeviceError):
        devices.run_interrupt(machine, 3)
    with pytest.raises(devices.NpkDeviceError):
        devices.run_interrupt(machine, INT_TIMER, interval=3)
