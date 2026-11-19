# Phase 2 plan — NPK (Nova Processed Kernel)

**Phase status:** AUTHORIZED / IN PROGRESS.  
**Phase 1 baseline:** `0139e5a`, verified clean and synchronized before work began.  
**Phase 3:** NOT STARTED.

Phase 2 is exclusively NPK development. It is not OpenWindows Nova64, NovaOW,
Oryn integration, NVMP, a desktop, a window manager, an application port, CPU
RTL, a replacement compiler, or an ISA redesign.

This roadmap has nine milestones. That count follows the real dependency chain
identified in the platform audit; the candidate work areas from the Phase 2
charter are mapped onto them without inventing empty milestones. A milestone is
COMPLETE only when its implementation, tests, documentation, and commit are
finished. No future milestone is marked complete early.

## Milestone map

| # | Milestone | Status | Gate |
| --- | --- | --- | --- |
| 1 | NPK architecture and boot foundation | COMPLETE | Bootable deterministic `.nob` kernel image that processes and receipt-verifies boot work on `NovaMachine` |
| 2 | Reusable processing core and operation management | COMPLETE | Typed operations, controlled lifecycle, processing-unit ownership, and receipt evidence tested |
| 3 | Exception and trap subsystem | NOT STARTED | Register-preserving trap frame and `MRET` return test |
| 4 | Physical and virtual memory management | NOT STARTED | Allocation, mapping, permissions, faults, and address-space isolation tested |
| 5 | Syscall and privilege subsystem | NOT STARTED | In-kernel ABI dispatch with validation and error conversion tested |
| 6 | Device abstraction and timer integration | NOT STARTED | Console/timer interfaces tested without invented devices |
| 7 | Task management and cooperative scheduling | NOT STARTED | Deterministic task creation, yield, and switch tested |
| 8 | Executable loading and user execution | NOT STARTED | `.nob` user image loads, runs, and terminates under NPK |
| 9 | Integrated validation and Phase 2 closeout | NOT STARTED | Full regression, NPK validation, report, clean synchronized push |

## Milestone 1 — NPK architecture and boot foundation

**Status:** COMPLETE.

**Objective.** Establish the authoritative architecture, boot contract, kernel
layout, build workflow, and a minimal bootable NPK image using the existing
Nova toolchain. The image must demonstrably process work through the NPK
pipeline rather than merely boot a conventional call chain.

**Dependencies.**

- Phase 1 complete at `0139e5a`.
- `docs/NPK_ARCHITECTURE.md` accepted as the Phase 2 architecture baseline.
- Platform audit recorded in `docs/NPK_INITIATION_REPORT.md`.

**Files likely to change.**

- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_INITIATION_REPORT.md`, `docs/PORTING_READINESS.md`, `README.md`.
- `nova/npk/` package: layout constants, image validation, boot helper, build
  helper, processing receipt decoder, and minimal kernel sources including the
  boot pipeline.
- `tests/test_npk.py`.
- `scripts/npk_validate.py`.
- `pyproject.toml` package data.

**Implementation scope.**

- Define `NPK` identity, Nova64-only CPU policy, boot contract, layout, and the
  processed-kernel architecture explicitly.
- Build a `.nob` kernel with global entry `_npk_start` at `image_base = 0x1000`.
- Kernel entry sets `sp`, installs `vbar` from a link-computed symbol, writes a
  boot marker, calls `npk_main`, and halts with its return code in `a0`.
- Implement one real processing operation, `NPK_OP_BOOT_VERIFY`, with a single
  processing unit and the five documented pipeline stages.
- Publish a fixed processing receipt that records the operation, input, output,
  status, final stage, and complete stage trace.
- Gate the kernel return code on successful processing completion.
- Provide deterministic host-side build, boot, and receipt-decoding helpers.
- Reject invalid kernel images and unexpected boot exceptions in tests.

**Tests.**

- NPK layout constants and non-overlapping regions.
- Kernel image validation.
- Deterministic rebuild produces byte-identical `.nob`.
- Boot reaches `HALT`, `a0 == 0`, marker matches, `vbar` lies in the image, and
  `sp` is the boot stack top.
- The processing receipt has the expected magic, operation code, input, output,
  terminal status, final stage, and exact stage trace.
- The kernel returns zero only after the boot operation reaches `COMPLETE`.
- A corrupted/undefined instruction image is rejected without a false pass.
- Phase 1 regression remains green.

**Acceptance criteria.**

- `python scripts/npk_validate.py` passes.
- `python scripts/phase1_validate.py` remains green with unchanged counts.
- The minimal kernel builds from source through `nova-as`, `nova-cc`, and
  `nova-ld` with no new executable format.
- The boot path is observably a processed kernel: work enters a typed pipeline,
  advances through every stage, commits a result, and emits a receipt.
- Documentation states that bare mode gives no user/kernel memory isolation.

**Known limitations.**

- Machine privilege is supplied by the host harness; there is no firmware.
- Processing is limited to one boot verification operation on one fixed unit;
  there is no general operation queue, trap/syscall producer, device producer,
  allocator, trap frame, syscall dispatcher, scheduler, user process, or
  preemptive behavior.
- Boot uses the host-side sparse memory model and private reference-memory
  reads for the marker and receipt assertions; these are test helpers, not
  kernel APIs.

**Validation result.**

- `python -m pytest -q tests/test_npk.py`: **9 passed**.
- `python scripts/npk_validate.py`: **PASS**.
- Phase 1 counts unchanged: frontend 180, sema 85, codegen 59, driver 55,
  runtime 15, library 1, end-to-end 13, emulator 1831, example 1, conformance
  18/18, compileall passed.
- Full suite after adding NPK tests: **2254 passed** (Phase 1 baseline 2245 plus
  9 NPK tests).
- `python -m pytest -q tests/test_documentation.py`: **17 passed**.
- Observed processing receipt: magic `0x4E504B50`, operation `1`, input
  `0x4E504B31`, output `0x4E504B32`, status `1`, final stage `5`, trace
  `(1, 2, 3, 4, 5)`.

**Completion conditions.** Architecture and roadmap committed, minimal kernel
source and helpers committed, tests and validation script green, documentation
updated, and clean synchronized `origin/master`.

## Milestone 2 — Reusable processing core and operation management

**Status:** COMPLETE.

**Objective.** Generalize the Milestone 1 boot pipeline into a reusable NPK
processing core with typed operation records, controlled lifecycle transitions,
processing-unit ownership, explicit commit semantics, and receipt-backed
evidence for success, rejection, and failure. The Milestone 1 boot contract must
remain byte-for-byte compatible.

**Dependencies.** Milestone 1 complete.

**Files changed.**

- `nova/npk/src/processing.c`: generic operations, units, stage handlers,
  receipt log, snapshots, core self-test, and boot synchronization.
- `nova/npk/core.py`: host-side core state/snapshot/receipt decoding and
  independent validation.
- `nova/npk/boot.py`: boot result now carries the decoded core snapshot and uses
  a step budget large enough for the full self-test.
- `nova/npk/build.py`: `--long-calls` compiler option for cross-object calls.
- `nova/npk/layout.py`: Milestone 2 observation-region constants.
- `nova/npk/__init__.py`: public exports for core constants and helpers.
- `tests/test_npk_core.py`: comprehensive processing-core tests.
- `scripts/npk_validate.py`: runs both NPK test files.
- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`.

**Implementation scope.**

- Four typed operation slots with ids, types, inputs, stage/state cursors,
  provisional and published results, reasons, outcomes, and receipt indices.
- Two logical processing units (`PU0`, `PU1`) with admission selection, busy/idle
  state, ownership enforcement, and terminal-only release.
- Admission validation for zero ids, unknown types, duplicate live ids, no free
  slot, no free unit, and receipt exhaustion, each with a specific reason.
- Controlled `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` state machine with
  distinct `REJECTED` and `FAILED` terminal outcomes; no skipped stages and no
  advancement after a terminal state.
- Direct type dispatch for boot and diagnostic operations; no function-pointer
  indirection is claimed.
- `EXECUTE` writes `op->provisional`; `COMMIT` publishes `op->result` only on a
  successful commit handler.
- A 12-slot receipt log, four operation snapshots, and a 144-byte core snapshot
  at fixed addresses, while preserving the 96-byte Milestone 1 receipt at
  `0x90008`.
- A kernel self-test that exercises complete operations, validation rejection,
  execution failure, commit failure, duplicate ids, unavailable units, both
  processing units, wrong-unit advancement, and incomplete-operation release.

**Tests.**

- Core layout regions are disjoint and above the boot stack.
- Core counters are `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
  with `selftest_status=0` and both units idle.
- The Milestone 1 boot receipt and `a0 == 0` remain unchanged.
- All twelve receipts match independently constructed expectations.
- Complete, rejected, execution-failed, and commit-failed receipts each show the
  exact stage, state, outcome, result, reason, and trace.
- Duplicate-id and unavailable-unit admissions are rejected without execution.
- Operation slots are released; unit assignment is visible in receipts.
- Receipt-structure validation rejects invalid, incomplete, and inconsistent
  receipts, and expectation validation rejects mismatched operations.
- `validate_core_state` rejects wrong magic/version/dimensions, incomplete
  self-test, busy units, retained operations, and bad counters.
- Tampered core-magic and receipt-magic memory is detected by host decoding.

**Acceptance criteria.** The kernel halts with `a0 == 0`; all NPK tests pass;
the Phase 1 regression counts are unchanged; documentation states the limited
commit guarantees and the sequential-unit model.

**Known limitations.** Four operation slots and twelve receipt slots are fixed.
The two processing units are modeled sequentially, not as SMP or harts. Commit
is not transactional and provides no rollback, isolation, or durability. There
is no trap/syscall/device/task/loader producer, no allocator, no scheduler, no
user isolation, and no interrupt-driven processing. Receipts are host-visible
evidence rather than an ABI, and host decoders use private machine memory reads
as test helpers only.

**Validation result.**

- `python -m pytest -q tests/test_npk.py tests/test_npk_core.py`: **50 passed**
  (9 Milestone 1 tests and 41 Milestone 2 tests).
- `python scripts/npk_validate.py`: **PASS**.
- `python scripts/phase1_validate.py`: **PASS**; counts unchanged (frontend 180,
  sema 85, codegen 59, driver 55, runtime 15, library 1, end-to-end 13,
  emulator 1831, example 1, conformance 18/18, compileall passed).
- `python -m pytest -q`: **2295 passed** (Phase 1 baseline 2245 plus 9 Milestone 1
  NPK tests plus 41 Milestone 2 NPK tests).
- `python -m pytest -q tests/test_documentation.py`: **17 passed**.
- Final core state: `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
  `selftest_status=0`, `pu_state=(0, 0)`, `pu_operation=(0, 0)`.
- Milestone 1 receipt unchanged: magic `0x4E504B50`, operation `1`, input
  `0x4E504B31`, output `0x4E504B32`, status `1`, final stage `5`, trace
  `(1, 2, 3, 4, 5)`.

**Completion conditions.** Implementation, tests, documentation, and scoped
commit complete; NPK and Phase 1 validation green; `master` clean and equal to
`origin/master`.

## Milestone 3 — Exception and trap subsystem

**Objective.** Replace the placeholder vector with a real machine-mode trap
entry, software trap frame, classification, fault reporting, and `MRET` return.

**Dependencies.** Milestones 1–2.

**Files likely to change.** `nova/npk/exceptions.py`, `nova/npk/src/trap.s`,
kernel C dispatch code, `tests/test_npk_exceptions.py`,
`docs/NPK_ARCHITECTURE.md`.

**Implementation scope.**

- Implement the documented 304-byte software trap frame.
- Preserve all GPRs and `psr` used by the handler.
- Dispatch by `ecode`, with `SYSTEM_CALL` classified for Milestone 5.
- Save `tval` for MMU faults.
- Return through `MRET` and verify restored privilege.

**Tests.** `SCALL`, `TRAP`, illegal instruction, alignment, memory, and MMU
faults; nested exception depth; full register preservation; return-state
restoration.

**Acceptance criteria.** Every handler test proves register/CSR state and exact
resumption PC; no trap is silently swallowed.

**Known limitations.** No supervisor delegation, no `SRET`/`ERET`, no hardware
trap frame, depth-4 hardware save stack remains architectural.

**Completion conditions.** Trap frame layout and dispatch documented, tests
green, regression green, committed.

## Milestone 4 — Physical and virtual memory management

**Objective.** Add a declared physical RAM range, frame allocator, kernel
page-table manager, mapping/unmapping, permissions, fault handling, and TLB
policy wrappers.

**Dependencies.** Milestones 1–3.

**Files likely to change.** `nova/npk/memory.py`, kernel memory C/assembly,
`tests/test_npk_memory.py`, architecture docs.

**Implementation scope.**

- Physical frame allocator over an explicit base/size, not emulator internals.
- Page tables using Nova PTE bits and 4 KiB/2 MiB/1 GiB leaves.
- Map/unmap/protect APIs for kernel and early user spaces.
- MMU fault handler uses `tval` and `ecause`.
- Address-space switching by `ptbr` with full TLB flush; `TLBI` wrappers.

**Tests.** Allocation exhaustion, map/unmap, R/W/X/U permissions, missing and
misaligned mappings, huge pages, fault details, TLB invalidation, isolation.

**Acceptance criteria.** Kernel code constructs page tables through its own
API and the emulator enforces the resulting permissions.

**Known limitations.** No ASID, no TLB shootdown, global PTE bit ignored by the
platform, no MMIO regions, no RAM-size model in the reference emulator.

**Completion conditions.** Memory layout and policies documented, tests green,
regression green, committed.

## Milestone 5 — Syscall and privilege subsystem

**Objective.** Implement the in-kernel syscall dispatcher while preserving Nova
syscall ABI v1.0.

**Dependencies.** Milestones 1–4.

**Files likely to change.** `nova/npk/syscalls.py`, kernel dispatch code,
`tests/test_npk_syscalls.py`, `docs/NPK_ARCHITECTURE.md`.

**Implementation scope.**

- Preserve numbers `0..7`, argument registers, return conventions, and errors.
- Validate syscall number, argument count, and user pointers.
- Implement `SYS_EXIT`, `SYS_WRITE`, `SYS_TIME`, `SYS_YIELD`, and object calls
  according to kernel policy; `SYS_READ` remains unsupported until an input
  device exists.
- Machine/user boundary tests using paged user mappings.

**Tests.** Number validation, bad arguments, invalid user pointers, success and
error paths, ABI compatibility with `nova.syscalls.SYSCALL_TABLE`.

**Acceptance criteria.** The syscall table is byte-for-byte number-compatible;
invalid input cannot read or write kernel memory.

**Known limitations.** No hosted libc, no dynamic linking, no `FILE` streams.

**Completion conditions.** Dispatch contract documented, tests green,
regression green, committed.

## Milestone 6 — Device abstraction and timer integration

**Objective.** Provide generic device interfaces only for capabilities the
platform genuinely has.

**Dependencies.** Milestones 1–5.

**Files likely to change.** `nova/npk/devices.py`, console/timer backends,
`tests/test_npk_devices.py`, architecture docs.

**Implementation scope.**

- Console backend over `SYS_WRITE`/`machine.io_log`.
- Timer backend using `time`, `timecmp`, `ie`, `ip`, and `ivec`.
- Interrupt source classification for software/timer/external bits.
- Explicit rejection of unsupported block/input/network devices.

**Tests.** Console output bytes, one-shot timer reprogramming, software IPI,
external injection only as a host-injected test event.

**Acceptance criteria.** No invented MMIO device or claim of physical hardware.

**Known limitations.** No device tree, MMIO, interrupt controller, framebuffer,
keyboard, disk, or network model.

**Completion conditions.** Device contract documented, tests green, regression
green, committed.

## Milestone 7 — Task management and cooperative scheduling

**Objective.** Implement tasks, states, ready queues, `SYS_YIELD`, and context
switching without claiming preemption prematurely.

**Dependencies.** Milestones 1–6.

**Files likely to change.** `nova/npk/tasks.py`, scheduler/context code,
`tests/test_npk_tasks.py`, architecture docs.

**Implementation scope.**

- TCB with state, stack, saved context, and address space.
- Cooperative scheduler and deterministic ready queue.
- Context switch that preserves the software trap frame and `ptbr`.
- Task creation and termination.

**Tests.** Creation, state transitions, yield ordering, isolation of stacks,
termination status.

**Acceptance criteria.** Tests demonstrate cooperative scheduling only.

**Known limitations.** No preemptive scheduling; `SYS_YIELD` in the reference
host model is not NPK's scheduler; no threads/processes split yet.

**Completion conditions.** Task model documented, tests green, regression green,
committed.

## Milestone 8 — Executable loading and user execution

**Objective.** Load and run user `.nob` images under NPK.

**Dependencies.** Milestones 1–7.

**Files likely to change.** `nova/npk/loader.py`, user stack/entry setup,
`tests/test_npk_loader.py`, examples, architecture docs.

**Implementation scope.**

- `.nob` header and flags validation.
- Image-byte mapping into a user address space.
- Stack initialization, entry setup, privilege transition, and termination.
- Unsupported-feature diagnostics.

**Tests.** Valid image, bad magic/flags/size/entry, unmapped range, permission
enforcement, user-to-kernel syscall path, exit status.

**Acceptance criteria.** A user program runs through NPK's MMU and syscall path
and terminates deterministically.

**Known limitations.** No relocations, dynamic loader, shared libraries,
`.nod`, or graphical shell.

**Completion conditions.** Loader contract documented, tests green, regression
green, committed.

## Milestone 9 — Integrated validation and Phase 2 closeout

**Objective.** Harden, document, and close Phase 2 with repeatable validation.

**Dependencies.** Milestones 1–8.

**Files likely to change.** `scripts/npk_validate.py`,
`docs/PHASE2_COMPLETION.md`, all NPK docs, README, package metadata.

**Implementation scope.**

- Extend the NPK validation script to cover every subsystem.
- Run Phase 1 validation unchanged.
- Produce the Phase 2 completion report and honest limitation classification.
- Review commits, documentation, and clean synchronization.

**Acceptance criteria.** Full NPK suite green, Phase 1 counts unchanged,
documentation complete, `master` clean and equal to `origin/master`.

**Known limitations.** Physical hardware, desktop/OpenWindows work, and Phase 3
remain out of scope.

**Completion conditions.** Closeout report committed and pushed; no milestone 10
created without explicit authorization.

## Rules for all milestones

1. Review the architecture before implementation.
2. Implement the smallest coherent subsystem.
3. Add unit and integration tests.
4. Run regression validation.
5. Update documentation.
6. Review the resulting code.
7. Commit logically related work.
8. Never advertise incomplete stubs as functional.
9. Never weaken Phase 1 tests to accommodate NPK.
