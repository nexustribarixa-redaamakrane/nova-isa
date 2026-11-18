"""The standard Nova syscall table (Nova ABI v1.0).

System calls enter through ``SCALL imm``. The immediate in ``ecause`` is the
syscall number; arguments are in ``a0``-``a5`` (registers 1-6) and the return
value is in ``a0``. A call that fails returns ``-1`` in ``a0`` and puts an
error code in ``a1``. This module is the authoritative registry; it does not
define an operating system, only the software contract at the ABI boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, List, Mapping, Tuple

from .isa import OBJECT_PERM_READ, WORD_MASK

__all__ = [
    "SYSCALL_VERSION",
    "SyscallDef",
    "SYSCALL_TABLE",
    "SYSCALLS_BY_NUMBER",
    "SYSCALLS_BY_NAME",
    "syscall_table_markdown",
    "dispatch_syscall",
    "return_value",
    "error_value",
]

SYSCALL_VERSION = "Nova syscall ABI v1.0"

#: Common error codes returned in ``a1`` when ``a0`` is ``-1``.
ERROR_BAD_ARGUMENT = 1
ERROR_UNSUPPORTED = 2
ERROR_NOT_FOUND = 3
ERROR_NO_RESOURCE = 4


@dataclass(frozen=True)
class SyscallDef:
    """One standard Nova system call."""

    number: int
    name: str
    arguments: Tuple[str, ...]
    result: str
    description: str

    def summary_row(self) -> str:
        args = ", ".join(self.arguments) if self.arguments else "none"
        return (
            f"| `{self.number}` | `{self.name}` | `{args}` | "
            f"`{self.result}` | {self.description} |"
        )


_SYSCALLS: Tuple[SyscallDef, ...] = (
    SyscallDef(0, "SYS_EXIT", ("code",), "never returns",
               "Terminate the calling hart's program with the given exit code."),
    SyscallDef(1, "SYS_WRITE", ("fd", "buffer", "count"), "bytes written",
               "Write ``count`` bytes from the virtual address ``buffer`` to "
               "handle ``fd``. Handle assignment is implementation-defined; "
               "the reference model records writes in ``machine.io_log``."),
    SyscallDef(2, "SYS_READ", ("fd", "buffer", "count"), "bytes read",
               "Read up to ``count`` bytes into ``buffer`` from handle ``fd``."),
    SyscallDef(3, "SYS_TIME", (), "time",
               "Return the current value of the ``time`` CSR."),
    SyscallDef(4, "SYS_OBJ_CREATE", ("size",), "object reference",
               "Create an object of ``size`` bytes; equivalent to OBJ.CREATE."),
    SyscallDef(5, "SYS_OBJ_SIZE", ("reference",), "size",
               "Return the size of a live object; equivalent to OBJ.GET."),
    SyscallDef(6, "SYS_OBJ_DESTROY", ("reference",), "zero or error",
               "Destroy a live object; equivalent to OBJ.DROP."),
    SyscallDef(7, "SYS_YIELD", (), "zero",
               "Suggest that the implementation schedule another hart; the "
               "reference model halts until an interrupt is injected."),
)

SYSCALL_TABLE: Mapping[int, SyscallDef] = MappingProxyType(
    {syscall.number: syscall for syscall in _SYSCALLS}
)
SYSCALLS_BY_NUMBER: Mapping[int, SyscallDef] = SYSCALL_TABLE
SYSCALLS_BY_NAME: Mapping[str, SyscallDef] = MappingProxyType(
    {syscall.name: syscall for syscall in _SYSCALLS}
)


def return_value(value: int) -> int:
    """Normalize a syscall return value to an unsigned 64-bit register."""
    return value & WORD_MASK


def error_value(code: int) -> int:
    """Return the architectural failure value ``-1`` for syscalls."""
    return WORD_MASK


def syscall_table_markdown() -> List[str]:
    """Markdown rows for the standard syscall table."""
    return [syscall.summary_row() for syscall in _SYSCALLS]


def dispatch_syscall(machine) -> int:
    """Execute the syscall named by ``machine``'s ``ecause``.

    This is a host-side helper for the reference model: it reads ``a0``-``a5``,
    performs the operation, writes the result to ``a0``, and returns that
    result. Real kernels implement the same table for their own harts.
    """
    number = machine.read_csr("ecause")
    syscall = SYSCALL_TABLE.get(number)
    if syscall is None:
        machine.write_register("a1", ERROR_UNSUPPORTED)
        machine.write_register("a0", error_value(ERROR_UNSUPPORTED))
        return error_value(ERROR_UNSUPPORTED)

    args = [
        machine.read_register(index)
        for index in range(1, 1 + len(syscall.arguments))
    ]
    if syscall.name == "SYS_EXIT":
        machine.halted = True
        result = 0
    elif syscall.name == "SYS_TIME":
        result = machine.read_csr("time")
    elif syscall.name == "SYS_WRITE":
        fd, buffer, count = args
        if fd not in (0, 1, 2):
            machine.write_register("a1", ERROR_BAD_ARGUMENT)
            result = error_value(ERROR_BAD_ARGUMENT)
        else:
            data = bytes(machine._read_mem(buffer + offset, 1)
                         for offset in range(count))
            machine.io_log.append((fd, data))
            result = count
    elif syscall.name == "SYS_READ":
        # Input devices are implementation-defined and have no standard
        # reference-model source.
        machine.write_register("a1", ERROR_UNSUPPORTED)
        result = error_value(ERROR_UNSUPPORTED)
    elif syscall.name == "SYS_OBJ_CREATE":
        result = machine._create_object(args[0])
    elif syscall.name == "SYS_OBJ_SIZE":
        obj = machine.objects.get(args[0])
        if obj is None or not obj.get("read", 0):
            machine.write_register("a1", ERROR_NOT_FOUND)
            result = error_value(ERROR_NOT_FOUND)
        else:
            result = obj["size"]
    elif syscall.name == "SYS_OBJ_DESTROY":
        obj = machine.objects.get(args[0])
        if obj is None or not obj.get("drop", 0):
            machine.write_register("a1", ERROR_NOT_FOUND)
            result = error_value(ERROR_NOT_FOUND)
        else:
            del machine.objects[args[0]]
            result = 0
    else:  # SYS_YIELD
        machine.halted = True
        result = 0

    machine.write_register("a0", result)
    return result


def _validate() -> None:
    numbers = [syscall.number for syscall in _SYSCALLS]
    names = [syscall.name for syscall in _SYSCALLS]
    if len(numbers) != len(set(numbers)) or len(names) != len(set(names)):
        raise ValueError("Duplicate syscall number or name")
    for syscall in _SYSCALLS:
        if len(syscall.arguments) > 5:
            raise ValueError(f"{syscall.name} has more than five arguments")


_validate()
