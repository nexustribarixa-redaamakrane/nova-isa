# NPK Phase 2 closeout

**Phase:** 2 — Nova Processed Kernel (NPK) only.
**Status:** COMPLETE after Milestone 10 validation.
**Final gate:** `master` clean and synchronized with `origin/master`.
**Detail:** `docs/NPK_MILESTONE10_REPORT.md`, `docs/NPK_MILESTONE9_REPORT.md`,
`docs/PHASE2_PLAN.md`, `docs/NPK_ARCHITECTURE.md`, and `docs/VALIDATION.md`.

## 1. What Phase 2 delivered

| Milestone | Delivered contract |
| --- | --- |
| M1 | Deterministic `.nob` kernel image, boot marker, boot receipt, and processed boot verification |
| M2 | Reusable processing core with four operation slots, two sequential PUs, five-stage lifecycle, receipts, and selftests |
| M3 | Real Nova assembly trap vector, 304-byte software trap frame, typed trap operations, and `MRET` return |
| M4 | Physical and virtual memory management with bounded regions, mappings, permissions, faults, and request receipts |
| M5 | Typed object manager and Universal Object Model with handles, references, namespaces, access policy, and destruction |
| M6 | In-kernel syscall dispatch over the trap path with validation and error conversion |
| M7 | Typed device/timer backend, bounded registry/timer/receipt tables, console sink, and interrupt classification |
| M8 | Cooperative task lifecycle, FIFO ready queue, scheduler integration at the trap return, and `SYS_YIELD`/`SYS_EXIT` behavior |
| M9 | Native `.nob` v1 loader, `VMLAUNCH` user execution, guest `SCALL` VM-exit evidence, and task termination |
| M10 | Cross-subsystem integration tests, repeatable discovery-based validation, parity guards, documentation, and closeout |

## 2. Frozen contracts preserved

- Nova ISA v1.0 and all Phase 1 compiler/runtime/emulator behavior.
- Nova64 ABI, task register conventions, and syscall IDs `0..7`.
- The typed `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` lifecycle; no
  helper-call bypasses.
- Published M1–M9 observation, table, and receipt layout ranges; M10 moved no
  addresses and changed no numeric operation, reason, or status value.
- Native `.nob` v1 as the only supported executable container; no ELF
  compatibility or fabricated `MRET` user-entry frame.

## 3. M10 integration changes

The final tree added `tests/test_npk_integration.py` with fifteen tests and wired
all `tests/test_npk*.py` files into `scripts/npk_validate.py` through sorted
discovery. It also fixed public export gaps, the `exec.c` admission-status/reason
name conflation, duplicated `NPK_ERR_*` definitions, a stale initialization
comment, and stale M7-era/M-not-started documentation.

No new subsystem was introduced. No layout, ISA, ABI, syscall, or lifecycle
semantic changed. The M10 audit found no duplicate core initialization, no
Python/C operation or reason drift after the parity guard, and no M1–M9 layout
overlap.

## 4. Final validation

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

Phase 1 stage groups are unchanged. The `full_suite` gate intentionally
includes all NPK tests, so its total is larger than the original Phase 1-only
count. CTest is reported honestly and is not treated as coverage.

## 5. Honest capability classification

**Implemented.** Boot, processing core, trap, memory, objects, syscalls,
devices/timers, cooperative tasks, native executable loading, user execution in
the reference emulator, integration tests, and validation wiring.

**Tested.** 210 NPK tests and 2455 total pytest tests in the final tree; both
QEMU harness boot configurations with exact step count and 64 KiB shadow-memory
comparison; deterministic CMake/MinGW rebuild; Python syntax compilation; docs
tests; whitespace check.

**Modeled.** QEMU boot uses a generated x86-64 interpreter of Nova bytecode, not
native Nova support. Timers use the emulator `time` CSR and bounded logical
records. Cooperative scheduling uses software trap-frame state and writable
return sites. M9 user execution is exercised through real Nova `VMLAUNCH` and
`SCALL` in the reference emulator, not by the QEMU boot harness.

**Not implemented / unsupported.** MMIO devices, device tree, interrupt
controller, physical interrupts or hardware, preemptive or timer-driven
scheduling, SMP, process/thread split, strong per-process isolation, ELF or
dynamic executable formats, networking, filesystem, desktop/OpenWindows work,
and Phase 3 features.

**Known limitations.** Caller privilege remains unobservable from the exposed
machine-mode trap frame. The QEMU MRET model covers the normal return path only.
Some per-module C operation/reason literals remain duplicated, but a
source-level parity test now guards them. CTest discovers no tests in this
configuration.

## 6. Closeout position

Phase 2 is complete and closed at Milestone 10. No Milestone 11, Phase 3
subsystem, desktop work, or new hardware claim is authorized by this closeout.
