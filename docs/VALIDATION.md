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
| 6 | `library` | `mem*`, `str*`, `atoi`, `strtol`, `strtoul` | 1 passed |
| 7 | `end_to_end` | Variadics, `printf`, and driver/runtime integration | 13 passed |
| 8 | `emulator` | Instruction table, assembler, objects, linker, CSR, traps, CLI, examples, docs | 1831 passed |
| 9 | `example` | `examples/hello_runtime.c` compiled, linked, loaded, and executed | 1 passed |
| 10 | `conformance` | Hardware-independent Nova architecture vectors | 18/18 passed |
| 11 | `compileall` | Python syntax check for `nova`, `tests`, `scripts` | passed |
| 12 | `full_suite` | Entire pytest suite as a final regression gate; Phase 1 stage groups unchanged, NPK tests intentionally included | 2455 passed |

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

## 6. NPK validation

Phase 2 NPK milestones have a separate repeatable entrypoint:

```sh
python scripts/npk_validate.py
```

It discovers every `tests/test_npk*.py` file in sorted order, including the
Milestone 10 cross-subsystem suite, and then runs the unchanged Phase 1 stage
groups. NPK validation is green only when both stages pass.
Optional QEMU boot evidence is built separately by the NPK tooling and is
reported honestly as emulator/QEMU-harness evidence, not hardware/native-QEMU
evidence.

### Final Milestone 8 results

The final combined tree for Milestone 8 produced these results:

| Command | Result |
| --- | --- |
| `python scripts/npk_validate.py` | PASS: NPK stage 185 passed; phase1 `full_suite` 2430 passed |
| `python scripts/phase1_validate.py` | PASS: `full_suite` 2430 passed |
| `python -m pytest -q` | PASS: 2430 passed |
| `python -m compileall -q nova scripts tests` | PASS (no output) |
| `python -m pytest -q tests/test_documentation.py` | PASS: 17 passed |
| `python -m pytest -q tests/test_npk*.py` | PASS: 185 passed |
| `python -m pytest tests/test_npk_qemu.py -q` | PASS: 7 passed |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu --timeout 120` | PASS |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu-m8 --timeout 120` | PASS |
| `ctest --test-dir build-cmake-mingw --output-on-failure -j 4` | Exit 0, but reports `No tests were found!!!`; no tests executed |

The CTest exit status is not counted as test coverage: no tests were discovered.
QEMU boot evidence comes from a generated x86-64 interpreter executing Nova
bytecode, not native Nova CPU support in QEMU.

### Final Milestone 9 results

The final combined tree for Milestone 9 produced these results:

| Command | Result |
| --- | --- |
| `python scripts/npk_validate.py` | PASS: NPK stage 195 passed; phase1 `full_suite` 2440 passed |
| `python scripts/phase1_validate.py` | PASS: `full_suite` 2440 passed |
| `python -m pytest -q` | PASS: 2440 passed |
| `python -m compileall -q nova scripts tests` | PASS (no output) |
| `python -m pytest -q tests/test_documentation.py` | PASS: 17 passed |
| `python -m pytest -q tests/test_npk*.py` | PASS: 195 passed |
| `python -m pytest -q tests/test_npk_qemu.py` | PASS: 7 passed |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu --timeout 120` | PASS: exact step count and shadow memory |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu-m8 --timeout 120` | PASS: exact step count and shadow memory |
| `cmake -S . -B build-cmake-mingw -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release` | PASS |
| `cmake --build build-cmake-mingw -j 4` | PASS: `npk.nob` rebuilt |
| `ctest --test-dir build-cmake-mingw --output-on-failure -j 4` | Exit 0, but reports `No tests were found!!!`; no tests executed |
| `git diff --check` | PASS (no whitespace errors; line-ending normalization warnings only) |

The M9 kernel image enlarged the QEMU dispatch table into the harness metadata
region. Metadata was moved to `0xB00000` with a layout assertion and a
regression test; both QEMU boot configurations again match the reference step
count and shadow memory. CTest still discovers no tests, and M9 user execution
is validated in the reference emulator through real `VMLAUNCH`/`SCALL`, not by
the QEMU boot harness.

### Final Milestone 10 results

The final Phase 2 closeout tree produced these results:

| Command | Result |
| --- | --- |
| `python scripts/npk_validate.py` | PASS: NPK stage 210 passed; phase1 `full_suite` 2455 passed |
| `python scripts/phase1_validate.py` | PASS: `full_suite` 2455 passed |
| `python -m pytest -q` | PASS: 2455 passed |
| `python -m compileall -q nova scripts tests` | PASS (no output) |
| `python -m pytest -q tests/test_documentation.py` | PASS: 17 passed |
| `python -m pytest -q tests/test_npk*.py` | PASS: 210 passed |
| `python -m pytest -q tests/test_npk_qemu.py` | PASS: 7 passed |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu --timeout 300` | PASS: exact step count and shadow memory |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu-m8 --timeout 300` | PASS: exact step count and shadow memory |
| `cmake -S . -B build-cmake-mingw -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release` | PASS |
| `cmake --build build-cmake-mingw -j 4` | PASS: `npk.nob` rebuilt |
| `ctest --test-dir build-cmake-mingw --output-on-failure -j 4` | CTest exited 0; no tests were discovered or executed. |
| `git diff --check` | PASS (no whitespace errors; line-ending normalization warnings only) |

M10 added tests and validation wiring only. The fifteen new
`tests/test_npk_integration.py` cases are included in both the 210-test NPK
stage and the 2455-test full suite; the Phase 1 stage groups themselves are
unchanged. See `docs/NPK_MILESTONE10_REPORT.md` and
`docs/NPK_PHASE2_CLOSEOUT.md` for the final scope classification.
