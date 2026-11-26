# NPK Milestone 10 report — integrated validation and Phase 2 closeout

**Milestone:** 10 of 10 — Integrated validation and Phase 2 closeout.
**Status:** COMPLETE after validation.
**Scope:** stabilization and integration only. No Phase 3 subsystem, ISA/ABI
change, layout change, new syscall, new executable format, desktop/GUI work,
driver framework, new scheduler, networking, filesystem, SMP, or dynamic linking
was introduced.

## 1. Summary

Milestone 10 closes Phase 2. It preserves the M1–M9 processed-kernel
architecture and verifies that the published subsystems work as one kernel:
boot and core readiness, trap and syscall integration, memory and object
lifetimes, device/timer policy, cooperative tasks, and native `.nob` user
execution all share one lifecycle and one set of receipts.

Every typed operation still traverses
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` through the shared processing
core. Helper functions publish request records or decode observation state; they
do not bypass processing. Nova ISA v1.0, the frozen ABI, syscall IDs `0..7`, task
register conventions, and Phase 1 behavior are unchanged.

## 2. Audit results

The final-tree audit inspected the NPK Python package, C/assembly sources,
kernel build and boot path, layout constants, validators, documentation, and
tests before making changes.

Confirmed preserved invariants:

- no duplicate `npk_core_init()` path; `processing.c` is the only caller;
- no numeric drift between Python `OP_*`/`REASON_*` values and the union of C
  definitions, now guarded by a source-level parity test;
- no M1–M9 observation/table/receipt layout overlap;
- boot counters and selftest status remain the published M1 values;
- the M4/M5/M6/M7/M8/M9 entry points and receipt tables remain distinct.

Concrete integration defects fixed:

| Finding | Minimal fix |
| --- | --- |
| `nova.npk.__all__` advertised `InterruptRecord` and `InterruptResult` without importing them | Imported both from `nova.npk.devices`; added an export-resolution meta-test |
| `nova.npk.core.__all__` omitted M8/M9 operation and reason names | Added the published `OP_EXEC_*`, `REASON_TASK_STALE`, and `REASON_EXEC_*` names |
| `exec.c` compared `npk_core_admit()` results with receipt reason names | Moved shared `NPK_ERR_*` values to `npk_core.h`, removed local duplicates, and compared against `NPK_ERR_*` |
| `kernel.c` comment claimed syscall initialization preceded device initialization | Corrected the comment to match the existing boot → trap → memory → object → device → task → exec → syscall order |
| `scripts/npk_validate.py` used a hardcoded NPK file list, so new suites could be silently skipped | Replaced it with sorted discovery of `tests/test_npk*.py` and added a meta-test |
| Operation/reason and layout literals had no parity guard | Added Python/C constant and register-save layout meta-tests; no numeric values changed |
| Documentation still described M7-era or “not started” state | Updated plan, architecture, validation, README, and M9 cross-reference; added closeout reports |

No large refactor was attempted. The remaining per-module duplication of some
operation/reason literals is unchanged; parity is guarded by tests instead.

## 3. Integration test coverage

`tests/test_npk_integration.py` adds fifteen focused tests:

1. all published subsystem entries initialize after boot;
2. boot marker, boot receipt, and core receipts survive cross-subsystem work;
3. four subsystems share one processing lifecycle and produce consistent
   complete/rejected receipts;
4. memory and object lifetimes remain coherent after a failed destroy, including
   PU release and no partial publication;
5. `SYS_YIELD` integrates trap frame, syscall dispatch, scheduler state, and task
   dispatch evidence;
6. `SYS_EXIT` terminates the current task without leaving processing units busy;
7. device and timer operations preserve core state and distinguish valid from
   unsupported requests;
8. executable load, mapping, start, `VMLAUNCH`, `SCALL` exit, and task attachment
   are coherent;
9. a rejected executable container publishes no partial image or receipt state;
10. bounded task receipt exhaustion is demonstrated and does not block a later
    memory operation;
11. QEMU harness layout and failing-PC diagnostics remain intact;
12. the toolchain rebuilds the same frozen kernel contract;
13. all advertised package/core exports resolve;
14. the NPK validator discovers every `test_npk*.py` suite;
15. Python/C processing constants and register-save layout literals agree.

The suite builds one kernel image and boots one machine per module, then uses a
deep copy per test. This preserves boot evidence, keeps tests isolated, and
reduced the suite runtime from roughly 23 minutes to roughly three minutes.

## 4. Final validation matrix

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

Phase 1 stage groups are unchanged. The `full_suite` count intentionally
includes all NPK tests; it is not a claim that the original Phase 1-only count
is unchanged. Bare CTest is not counted as test coverage because it discovers no
tests.

## 5. Scope classification

| Classification | Phase 2 final state |
| --- | --- |
| **Implemented** | Deterministic `.nob` boot image; shared five-stage processing core; trap frame and dispatch; memory manager; object manager; syscall services; console/timer backend; cooperative task scheduler; native `.nob` loader and `VMLAUNCH` user execution; M10 cross-subsystem validation |
| **Tested** | All 210 NPK tests in the reference emulator, including 15 M10 integration tests; both generated QEMU harness boot configurations; full 2455-test pytest regression; deterministic CMake/MinGW rebuild |
| **Modeled** | QEMU boot is a generated x86-64 interpreter of Nova bytecode; timer events use the emulator `time` CSR and bounded logical records; cooperative scheduler uses software trap-frame and writable return-site state; user execution is validated in the reference emulator |
| **Unsupported by design** | MMIO devices, device tree, interrupt controller, physical hardware, preemptive scheduling, timer-driven scheduling, SMP, process/thread split, strong per-process isolation, ELF/dynamic formats, networking, filesystem, desktop/GUI, and all Phase 3 work |

## 6. Completion

Milestone 10 is complete only when the closeout report, integration tests,
validation wiring, documentation, and three-subject commit series are committed
and `master` is clean and synchronized with `origin/master`. No Milestone 11 is
created without explicit authorization.
