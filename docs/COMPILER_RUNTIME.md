# Nova compiler runtime requirements

**Status:** specification of the helper contracts a Nova C compiler may call.
Phase 0 defines these names and signatures; it does **not** implement a compiler
runtime. A freestanding `libnova` in Phase 1 will provide the subset the C
subset compiler needs.

The base ISA already implements 64-bit add, subtract, multiply, signed and
unsigned divide/remainder, shifts, rotates, atomics and `FENCE`. Helpers are
therefore needed only for operations the base ISA lacks or where the compiler
must avoid an architectural trap.

## 1. Naming and calling convention

Every helper follows `docs/NOVA_C_ABI.md`: integer arguments in `a0`-`a5`,
results in `a0` (`a1` for a second word), caller-saved `a0`-`a5`/`t0`-`t3`,
callee-saved `s0`-`s3`, 16-byte stack alignment. Prefix `__nova_` avoids
collisions with libc and libgcc names.

## 2. Required base helpers

| Helper | Arguments | Result | Reason |
| --- | --- | --- | --- |
| `__nova_divdi3(a, b)` | signed 64-bit | signed quotient | Non-trapping divide for language semantics that require it |
| `__nova_moddi3(a, b)` | signed 64-bit | signed remainder | Same |
| `__nova_udivdi3(a, b)` | unsigned 64-bit | unsigned quotient | Same |
| `__nova_umoddi3(a, b)` | unsigned 64-bit | unsigned remainder | Same |

The ISA's `DIV`/`UDIV`/`REM`/`UREM` trap on a zero divisor. A compiler targeting
C semantics (where division by zero is undefined but must not take a machine
trap in every build mode) may either emit the instructions directly with a
guarantee from the program, or call these helpers. Division overflow of the
signed minimum by `-1` is defined by the ISA to wrap; the helpers preserve that
behavior unless the language front end requests a trap.

## 3. Wide integer helpers

Nova has 64-bit registers, so C's `__int128` and wide shifts need library
support. A value is passed as two consecutive eight-byte chunks in argument
registers, low word first.

| Helper | Semantics |
| --- | --- |
| `__nova_multi3(a, b)` | 128-bit x 128-bit multiply (low 128 bits) |
| `__nova_udivti3(a, b)` | unsigned 128-bit divide |
| `__nova_umodti3(a, b)` | unsigned 128-bit remainder |
| `__nova_divti3(a, b)` | signed 128-bit divide |
| `__nova_modti3(a, b)` | signed 128-bit remainder |
| `__nova_ashlti3(a, n)` | arithmetic shift left, 128-bit |
| `__nova_lshrti3(a, n)` | logical shift right, 128-bit |
| `__nova_ashrti3(a, n)` | arithmetic shift right, 128-bit |

These are ordinary ABI-conforming functions; they may use `DIV`/`UDIV` on the
high word when safe.

## 4. Atomic and memory helpers

The base ISA provides 64-bit `AMOADD`, `AMOSWAP`, `AMOCAS`, `AMOAND`, `AMOOR`,
`AMOXOR` and `FENCE`. Helpers are needed for narrower or wider operations and
for ordering sugar:

| Helper | Semantics |
| --- | --- |
| `__nova_atomic_load32(p)` | Load with acquire ordering |
| `__nova_atomic_store32(p, v)` | Store with release ordering |
| `__nova_atomic_cas32(p, expected, desired)` | 32-bit CAS in a 64-bit slot |
| `__nova_atomic_fetch_add64(p, v)` | Wrapper over `AMOADD` with requested ordering |
| `__nova_atomic_thread_fence(order)` | Wrapper over `FENCE` for the SC model |

32-bit atomics must be implemented by a 64-bit CAS on the aligned containing
word so that neighboring fields are not disturbed. An 8-byte-aligned address is
architecturally required for the underlying atomic.

## 5. Stack and builtins

| Helper | Semantics |
| --- | --- |
| `__nova_probe_stack(size)` | Touch each page of a large frame; no ISA stack-probe instruction exists |
| `__nova_stack_chk_fail()` | Report stack corruption and terminate |
| `__nova_trap_unreachable()` | Raise `ILLEGAL_INSTRUCTION` for compiler-inserted traps |
| `__nova_expect(cond, expected)` | Branchhint placeholder; currently returns `cond` |

`__nova_trap_unreachable()` is the only helper allowed to rely on an exception
rather than returning.

## 6. Floating point

No base-ISA floating-point helper is specified yet. A soft-float library would
need at least add/subtract/multiply/divide and conversions for 32- and 64-bit
formats, following the pass-by-bits rule in `docs/NOVA_C_ABI.md` section 9. That
work belongs to a future ISA extension plus a new ABI version.

## 7. What libc provides, not the compiler runtime

`memcpy`, `memmove`, `memset`, `memcmp`, `strlen` and similar routines are libc
responsibilities. A freestanding runtime may provide a minimal subset, but the
compiler must not assume their presence unless the target libc is linked.

## 8. Phase 0 status

- Defined here: names, signatures, calling convention and rationale.
- Not implemented here: any helper body, no `libgcc`-style library, no soft
  float, no startup files.
- Phase 1 gate: the C-subset compiler emits calls only to helpers that
  `libnova` actually provides, or emits native instructions when the helper is
  unnecessary.
