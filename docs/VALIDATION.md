# Repeatable Phase 1 validation

**Status:** green. Run the following from the repository root:

```sh
python scripts/phase1_validate.py
```

The script prints each command it runs, reports a pass count per pipeline
stage, and finishes with `Phase 1 validation: PASS` or `FAIL`. It exits `0`
only when every group succeeds. No network, hardware, or external toolchain is
needed.

## 1. Ordered checks

| Order | Group | What it covers | Latest count |
| ---: | --- | --- | ---: |
| 1 | `frontend` | Lexer, preprocessor (including multi-line comments and directive continuation), parser | 180 passed |
| 2 | `sema` | Types, scopes, conversions, variadic default promotions/decay | 85 passed |
| 3 | `codegen` | Assembly shape, ABI chunks, variadic lowering, switch-in-if, end-to-end programs | 59 passed |
| 4 | `driver` | CLI surface, file handling, multi-TU links, range diagnostics, emulator runs | 55 passed |
| 5 | `runtime` | Runtime package, startup contract, syscall wrappers, header compilation, diagnostics | 15 passed |
| 6 | `library` | `mem*`, `str*`, `atoi`, `strtol`, `strtoul` | 3 passed |
| 7 | `end_to_end` | Variadics, `printf`, and driver/runtime integration | 13 passed |
| 8 | `example` | `examples/hello_runtime.c` compiled, linked, loaded, and executed | 1 passed |
| 9 | `conformance` | Hardware-independent Nova architecture vectors | 18/18 passed |
| 10 | `compileall` | Python syntax check for `nova`, `tests`, `scripts` | passed |
| 11 | `full_suite` | Entire pytest suite as a final regression gate | 2245 passed |

The disjoint `runtime`, `library`, and `end_to_end` selections use pytest `-k`
expressions over `tests/test_runtime.py`, so their counts can be reported
separately without double-counting.

## 2. End-to-end runtime harness

The example and every execution test in `tests/test_runtime.py` use the real
driver, assembler, object format, linker, `.nob` decoder, and emulator. The
harness:

- writes source files with `nova-cc --runtime`;
- calls `load_nob(path)` and `machine.load_code(image.image, image.image_base)`;
- sets `machine.pc = image.entry` and `machine.priv = PRIVILEGE_LEVELS["machine"]`;
- dispatches `SYSTEM_CALL` through `nova.syscalls.dispatch_syscall(machine)`;
- resumes at `machine.epc`, pops `_exception_stack`, and clears `ecode`;
- collects stdout from `machine.io_log` file descriptor 1;
- asserts termination, final `a0`, and exact byte output where relevant.

Loading the image at `image.entry` instead of `image.image_base` is incorrect:
it shifts the code and startup computes `sp = 0xfffffffffffffec0`. The harness
also rejects any unexpected emulator exception rather than silently passing.

## 3. Exact hello output

`examples/hello_runtime.c` produces:

```text
hello world -42 42 ff FF ! %
done
```

As bytes, including the trailing newline:

```python
b"hello world -42 42 ff FF ! %\ndone\n"
```

The check also requires `a0 == 0` and `machine.halted`.

## 4. Focused commands

Individual stages can be run without the wrapper:

```sh
python -m pytest -q
python -m pytest -q tests/test_runtime.py
python -m nova.conformance
python -m compileall -q nova tests scripts
python -m nova.cc --runtime examples/hello_runtime.c -o /tmp/hello.nob
```

The last command only builds the image; execution needs the host syscall
dispatcher described above. `nova-run` alone does not install a kernel.

## 5. Interpretation

A green run demonstrates:

- all seven Phase 1 milestones remain regression-clean;
- the runtime can be linked repeatedly and deterministically;
- the variadic ABI works for scalars, pointers, promotions, copies, and
  aggregates;
- the scoped formatter emits exact bytes and return counts;
- the ISA conformance vectors are unchanged at 18/18;
- documentation changes did not alter generated code.

It does not claim hardware validation, hosted C conformance, input support, or
full ISO C library coverage.
