"""Machine-readable constants for the Nova C ABI v1.0.

The narrative contract lives in ``docs/NOVA_C_ABI.md``. These constants let a
compiler, runtime, and their tests agree on the layout without re-reading prose.
"""

from __future__ import annotations

__all__ = [
    "C_ABI_VERSION",
    "STACK_ALIGNMENT",
    "ARGUMENT_REGISTERS",
    "CALLER_SAVED_REGISTERS",
    "CALLEE_SAVED_REGISTERS",
    "GLOBAL_POINTER_REGISTER",
    "MAX_REGISTER_RETURN_BYTES",
    "VA_LIST_SIZE",
    "VA_LIST_ALIGNMENT",
    "VA_GP_OFFSET",
    "VA_FP_OFFSET",
    "VA_OVERFLOW_ARG_AREA",
    "VA_REG_SAVE_AREA",
    "VA_REGISTER_SAVE_BYTES",
]

C_ABI_VERSION = "Nova C ABI v1.0"

STACK_ALIGNMENT = 16
ARGUMENT_REGISTERS = ("a0", "a1", "a2", "a3", "a4", "a5")
CALLER_SAVED_REGISTERS = ("a0", "a1", "a2", "a3", "a4", "a5",
                         "t0", "t1", "t2", "t3")
CALLEE_SAVED_REGISTERS = ("s0", "s1", "s2", "s3", "gp", "sp", "fp")
GLOBAL_POINTER_REGISTER = "gp"
MAX_REGISTER_RETURN_BYTES = 16

VA_LIST_SIZE = 24
VA_LIST_ALIGNMENT = 8
VA_GP_OFFSET = 0
VA_FP_OFFSET = 4
VA_OVERFLOW_ARG_AREA = 8
VA_REG_SAVE_AREA = 16
VA_REGISTER_SAVE_BYTES = 6 * 8
