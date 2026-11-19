# Nova64 freestanding runtime and `libnova`

**Status:** implemented in Phase 1 Milestone 7. This document describes the
runtime that exists in this tree. It is intentionally **freestanding**: there is
no hosted C process model, no operating-system loader, no `FILE` layer, and no
claim of full ISO C library conformance.

The authoritative calling and variadic contracts remain in
`docs/NOVA_C_ABI.md`. Milestone 7 did not change the Nova ISA, instruction
encodings, registers, CSRs, exceptions, translation rules, relocation kinds,
syscall numbers, `.nobj`/`.nob` formats, or the 24-byte `va_list` layout.

## 1. Package layout

| Path | Contents |
| --- | --- |
| `nova/runtime/__init__.py` | Builds and caches runtime objects; exposes `runtime_objects()` |
| `nova/runtime/startup.s` | `_start`, stack setup, `main` invocation, termination |
| `nova/runtime/syscalls.s` | Thin `SCALL` wrappers |
| `nova/runtime/include/*.h` | Nine built-in system headers |
| `nova/runtime/src/string.c` | Memory and string routines |
| `nova/runtime/src/stdlib.c` | Integer conversion and termination routines |
| `nova/runtime/src/stdio.c` | Scoped formatted output |
| `examples/hello_runtime.c` | Small end-to-end program using the runtime |

Runtime C sources go through the same preprocess, parse, semantic analysis,
codegen, and assembler APIs as user code. They are compiled once with
`long_calls=True` and `long_branches=True`, cached as immutable object
templates, and deep-copied for each link so linker bookkeeping cannot leak
between invocations. `runtime_objects()` returns five objects by default:
startup, syscall wrappers, and the three `libnova` objects.

`pyproject.toml` declares the headers, C sources, and assembly files as package
data so the runtime is present in an installed wheel as well as in a source
checkout.

## 2. Startup contract

The runtime supplies the startup object; user programs do not need a private
`crt0` when they link with `nova-cc --runtime`.

| Property | Contract |
| --- | --- |
| Entry symbol | `_start` (the linker default) |
| Stack supplier | Runtime startup supplies `sp = 0x80000`, a fixed 16-byte-aligned address |
| Initial registers | No architectural arguments, `argc`, `argv`, or `envp`; only `sp` and linked symbols are relied upon |
| Invocation | `LA ra, .Lnova_start_return` followed by `BL main` |
| C entry point | `int main(void)` |
| Return handling | `main` returns its value in `a0`; `_start` executes `HALT` immediately afterwards, leaving that value in `a0` |
| Termination | `HALT` is the reference-model termination mechanism |
| Syscall exit | `nova_exit(status)` uses `SYS_EXIT`; it needs a host or kernel syscall dispatcher to halt |
| Hosted claims | None. There is no process cleanup, stdio flushing model, environment block, or hosted startup |

The reference emulator does not supply a stack or install a kernel vector. The
documented host harness therefore:

1. loads the `.nob` image at `image.image_base`, not at `image.entry`;
2. sets `pc = image.entry` and machine privilege;
3. steps the emulator;
4. on `SYSTEM_CALL`, calls `nova.syscalls.dispatch_syscall(machine)`, resumes
   at `machine.epc`, pops the saved exception frame, and clears `ecode`;
5. treats `HALT` as termination.

`tests/test_runtime.py` and `scripts/phase1_validate.py` use exactly this
harness. Loading the image at `entry` would shift code and corrupt the startup
stack address.

## 3. Syscall wrappers

`nova.h` declares thin wrappers over the frozen syscall table in
`nova/syscalls.py`.

| Function | Syscall | Notes |
| --- | ---: | --- |
| `nova_write(fd, buffer, count)` / `write` | `SYS_WRITE` (1) | Returns the transferred byte count, or `-1` |
| `nova_read(fd, buffer, count)` / `read` | `SYS_READ` (2) | The reference model has no input source and returns unsupported (`-1`) |
| `nova_time(void)` | `SYS_TIME` (3) | Reads the architectural `time` CSR through the dispatcher |
| `nova_exit(status)` | `SYS_EXIT` (0) | Halts only when a syscall dispatcher is present |
| `nova_halt(status)` | architectural `HALT` | Leaves `status` in `a0`; no dispatcher required |

`SCALL` raises `SYSTEM_CALL`; the wrappers then `RET` after the host resumes at
`EPC`. No syscall number, argument convention, or return convention was added.

## 4. Built-in headers

`nova/runtime/include` is installed as a built-in system include directory by
the `nova-cc` driver. User `-I` directories are searched first, so a project may
replace a header deliberately.

| Header | Provided subset |
| --- | --- |
| `stddef.h` | `size_t`, `ptrdiff_t`, `NULL`, `offsetof` |
| `stdint.h` | Exact-width integer typedefs, limit macros, constant macros |
| `stdbool.h` | `bool`, `true`, `false` |
| `limits.h` | LP64 integer limits; plain `char` is unsigned |
| `string.h` | `mem*`, `strlen`, `strcmp`, `strncmp`, `strcpy`, `strncpy`, `strcat` |
| `stdlib.h` | `atoi`, `strtol`, `strtoul`, `exit`, `abort`, success/failure macros |
| `stdio.h` | `printf`, `putchar`, `puts`, `EOF` |
| `stdarg.h` | `va_list`, `va_start`, `va_arg`, `va_copy`, `va_end` |
| `nova.h` | Nova syscall wrappers and architecture-specific `nova_halt` |

These headers describe only the supported subset. They are not hosted C headers
and do not imply undeclared functions.

## 5. `libnova` routines

### Memory and strings (`string.c`)

- `memcpy`, `memmove`, `memset`, `memcmp`
- `strlen`, `strcmp`, `strncmp`
- `strcpy`, `strncpy`, `strcat`

`memmove` handles both overlapping directions. `strncpy` pads the destination to
`count` bytes. All routines obey the LP64 model and plain unsigned `char`.

### Integer conversion and termination (`stdlib.c`)

- `strtoul` and `strtol` accept optional whitespace, a sign, bases 2–36, base 0
  prefixes (`0x` and leading `0`), set `endptr`, and saturate on overflow.
- `strtol` parses its signed limit independently, including `LONG_MIN`; negative
  values are no longer parsed by delegating to `strtoul`.
- `atoi` is `strtol(text, 0, 10)` narrowed to `int`.
- `exit` calls `nova_exit`; `abort` calls `nova_halt(134)`.

There is no allocator, environment access, randomized sequence support, or
general process management.

### Output (`stdio.c`)

`printf` supports exactly:

| Conversion | Consumed type | Output |
| --- | --- | --- |
| `%c` | `int` | One byte |
| `%s` | `const char *` | NUL-terminated text; null prints `(null)` |
| `%d`, `%i` | `int` | Signed decimal |
| `%u` | `unsigned int` | Unsigned decimal |
| `%x`, `%X` | `unsigned int` | Lower/upper hexadecimal without a prefix |
| `%p` | `void *` | `0x` followed by lowercase hexadecimal |
| `%%` | none | A literal percent |

Behaviour outside that subset is deliberately conservative:

- Unsupported flags, widths, precisions, and length modifiers are emitted
  literally after the `%`; they do not consume arguments. For example,
  `%5d`, `%-d`, `%.2f`, and `%ld` print as text.
- An unsupported conversion character is emitted as `%` followed by that
  character.
- A trailing `%` emits a literal `%`.
- A null format returns `-1`; a failed write returns `-1`.
- `printf` returns the number of bytes emitted on success.
- `putchar` writes one byte (including NUL) and returns it as an unsigned byte,
  or `EOF` on write failure.
- `puts` writes text plus a newline and returns `0`, or `EOF` on failure. Null
  text prints `(null)`.

There are no `FILE` objects, input functions, `fprintf`, `sprintf`, `snprintf`,
or locale handling.

## 6. Variadic support

`stdarg.h` exposes the ABI-defined 24-byte state:

| Offset | Field | Meaning |
| ---: | --- | --- |
| 0 | `gp_offset` | Bytes consumed in the `a0`–`a5` save area |
| 4 | reserved | Zero; kept for a future FP extension |
| 8 | `overflow_arg_area` | First stack argument slot |
| 16 | `reg_save_area` | 48-byte callee spill area for `a0`–`a5` |

The implementation uses compiler intrinsics; there is no runtime `va_list`
helper library:

- `__builtin_va_start` initializes all fields. A variadic prologue spills
  `a0`–`a5` into the callee save area and records the incoming stack pointer.
- `__builtin_va_arg(ap, type)` is parsed as a dedicated AST node because its
  second operand is a type name. It reads either the register save area or the
  overflow area, advances the correct pointer, and handles multi-chunk
  aggregates.
- `__builtin_va_copy` copies the three architectural fields.
- `__builtin_va_end` is a semantic no-op in this ABI.

Default argument promotions are applied to unnamed arguments, including integer
promotions and array/function decay. Tests cover zero, one, multiple, register,
stack, mixed, pointer, narrowed, copied, repeated, and independent lists, plus
six-, twelve-, and sixteen-byte aggregate arguments.

`va_start` outside a variadic function and invalid `va_arg` types produce
source-located diagnostics. `printf` is itself the largest in-tree consumer of
this implementation.

## 7. Compiler runtime boundary

No `__nova_*` compiler-helper body is required by this compiler. Integer
division and remainder lower to the existing `DIV`, `UDIV`, `REM`, and `UREM`
instructions. The helper contracts in `docs/COMPILER_RUNTIME.md` remain a specification
for future compiler modes; they are not advertised as implemented symbols.

## 8. Linking recipes

```sh
# Compile, link startup + syscalls + libnova, and run with a host dispatcher.
nova-cc --runtime examples/hello_runtime.c -o hello.nob

# Keep a user-supplied entry file.
nova-cc --runtime --nostartfiles start.nobj main.c -o program.nob

# Keep syscall wrappers but omit libnova C objects.
nova-cc --runtime --nostdlib main.c -o program.nob
```

`--runtime` is link-mode only. `--nostartfiles` and `--nostdlib` require it.
Because a runtime image is large, `--runtime` implies layout-independent
`--long-calls` and `--long-branches` for user C sources. A two-step link from
prebuilt objects must therefore compile those objects with
`--long-calls --long-branches` as well; the linker cannot rewrite compact
14-bit displacements. See `docs/NOVA_CC.md`.

## 9. Supported subset summary

- Supported: the freestanding integer/pointer/aggregate C subset, variadic
  access through `stdarg.h`, the headers and library functions listed above,
  multiple translation units, and deterministic runtime linking.
- Not claimed: hosted C, full ISO C library conformance, full `printf`
  formatting, input streams, allocation, FP/soft-float, threads, dynamic
  linking, PIC/GOT, a kernel, or device drivers.

The complete blocker and limitation classification is in
`docs/PHASE1_COMPLETION.md`.
