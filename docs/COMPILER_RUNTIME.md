# Nova compiler runtime requirements

**Status:** specification of optional helper contracts plus the implemented
Phase 1 boundary. Phase 0 defined these names and signatures. Phase 1 Milestone 7
adds a freestanding `libnova` (see `docs/RUNTIME.md`), but the current compiler
does **not** emit any `__nova_*` helper call: division and remainder use native
ISA instructions, so no helper body is required. The contracts below remain
reserved for future compiler modes or wider integer support.

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
guarantee from the program, or call these helpers. The Phase 1 compiler takes the
first path and emits the native instructions; no helper object defines these
names. Division overflow of the
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
responsibilities. The Phase 1 `libnova` now provides a freestanding subset of
them, along with integer conversions, scoped `printf`, and syscall wrappers; see
`docs/RUNTIME.md`. The compiler still must not assume their presence unless the
runtime is linked with `nova-cc --runtime`.

## 8. Implementation status

- **Specified here:** optional helper names, signatures, calling convention,
  and rationale.
- **Implemented in `nova/runtime/`:** startup, syscall wrappers, headers, and
  the freestanding `libnova` subset documented in `docs/RUNTIME.md`.
- **Not implemented:** any `__nova_*` helper body, 128-bit arithmetic, soft
  float, or GCC/LLVM-class runtime support. These are intentional limitations,
  not Phase 1 blockers.
- **Phase 1 gate satisfied:** the compiler emits no external helper symbols for
  supported arithmetic; when a library routine is called, `--runtime` supplies
  the implementation or the linker reports a normal undefined symbol.
- **Future work:** provide the reserved helpers only when a compiler mode or
  language extension actually references them.
