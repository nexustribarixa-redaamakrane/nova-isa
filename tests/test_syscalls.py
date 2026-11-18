"""Tests for the standard Nova syscall table."""

import pytest

from nova import NovaMachine, syscalls
from nova.assembler import assemble_text
from nova.isa import WORD_MASK, make_object_reference


def test_syscall_table_is_well_formed():
    assert syscalls.SYSCALL_VERSION == "Nova syscall ABI v1.0"
    numbers = list(syscalls.SYSCALL_TABLE)
    assert numbers == list(range(len(numbers)))
    assert len(numbers) >= 8
    for syscall in syscalls.SYSCALL_TABLE.values():
        assert len(syscall.arguments) <= 5
        assert syscall.name in syscalls.SYSCALLS_BY_NAME
        assert syscall.result


def test_syscall_table_markdown_has_one_row_per_syscall():
    rows = syscalls.syscall_table_markdown()
    assert len(rows) == len(syscalls.SYSCALL_TABLE)
    assert all(row.startswith("| `") for row in rows)


def test_scall_number_is_the_dispatch_key():
    machine = NovaMachine(assemble_text("SCALL 3\n"), entry=0x1000)
    machine.run(1)
    assert machine.read_csr("ecause") == 3


def test_time_syscall_returns_the_time_csr():
    machine = NovaMachine()
    machine._csr[3] = 0  # ecode
    machine.write_csr("ecause", 3)
    machine.write_csr("time", 1234)
    assert syscalls.dispatch_syscall(machine) == 1234
    assert machine.read_register("a0") == 1234


def test_exit_syscall_halts_the_machine():
    machine = NovaMachine()
    machine.write_csr("ecause", 0)
    machine.write_register("a0", 7)
    assert syscalls.dispatch_syscall(machine) == 0
    assert machine.halted


def test_write_syscall_records_bytes_from_virtual_memory():
    machine = NovaMachine()
    machine._write_mem(0x2000, ord("H"), 1)
    machine._write_mem(0x2001, ord("i"), 1)
    machine.write_csr("ecause", 1)
    machine.write_register("a0", 1)      # fd 1
    machine.write_register("a1", 0x2000)  # buffer
    machine.write_register("a2", 2)       # count
    assert syscalls.dispatch_syscall(machine) == 2
    assert machine.io_log == [(1, b"Hi")]
    assert machine.read_register("a0") == 2


def test_object_syscalls_use_the_capability_table():
    machine = NovaMachine()
    machine.write_csr("ecause", 4)
    machine.write_register("a0", 32)
    reference = syscalls.dispatch_syscall(machine)
    assert reference == make_object_reference(1, 1)
    assert machine.read_register("a0") == reference

    machine.write_csr("ecause", 5)
    machine.write_register("a0", reference)
    assert syscalls.dispatch_syscall(machine) == 32

    machine.write_csr("ecause", 6)
    machine.write_register("a0", reference)
    assert syscalls.dispatch_syscall(machine) == 0
    assert reference not in machine.objects


def test_unknown_syscall_and_unimplemented_read_report_errors():
    machine = NovaMachine()
    machine.write_csr("ecause", 99)
    assert syscalls.dispatch_syscall(machine) == WORD_MASK
    assert machine.read_register("a1") == syscalls.ERROR_UNSUPPORTED

    machine.write_csr("ecause", 2)
    assert syscalls.dispatch_syscall(machine) == WORD_MASK
    assert machine.read_register("a1") == syscalls.ERROR_UNSUPPORTED


@pytest.mark.parametrize("number,name", [(0, "SYS_EXIT"), (3, "SYS_TIME"),
                                         (5, "SYS_OBJ_SIZE")])
def test_syscall_names_round_trip(number, name):
    assert syscalls.SYSCALL_TABLE[number].name == name
    assert syscalls.SYSCALLS_BY_NAME[name].number == number
