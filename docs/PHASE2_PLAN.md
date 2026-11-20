# Phase 2 plan — NPK (Nova Processed Kernel)

**Phase status:** AUTHORIZED / IN PROGRESS.  
**Phase 1 baseline:** `0139e5a`, verified clean and synchronized before work began.  
**Phase 3:** NOT STARTED.

Phase 2 is exclusively NPK development. It is not OpenWindows Nova64, NovaOW,
Oryn integration, NVMP, a desktop, a window manager, an application port, CPU
RTL, a replacement compiler, or an ISA redesign.

This roadmap has ten milestones. That count follows the real dependency chain
identified in the platform audit; the candidate work areas from the Phase 2
charter are mapped onto them without inventing empty milestones. A milestone is
COMPLETE only when its implementation, tests, documentation, and commit are
finished. No future milestone is marked complete early.

## Milestone map

| # | Milestone | Status | Gate |
| --- | --- | --- | --- |
| 1 | NPK architecture and boot foundation | COMPLETE | Bootable deterministic `.nob` kernel image that processes and receipt-verifies boot work on `NovaMachine` |
| 2 | Reusable processing core and operation management | COMPLETE | Typed operations, controlled lifecycle, processing-unit ownership, and receipt evidence tested |
| 3 | Exception and trap subsystem | COMPLETE | Register-preserving trap frame and `MRET` return test |
| 4 | Physical and virtual memory management | COMPLETE | Allocation, mapping, permissions, faults, and address-space isolation tested |
| 5 | Object Manager and Universal Object Model | PLANNED ONLY | Consistent kernel resource identity, handles, and lifetime management designed after memory foundations exist |
| 6 | Syscall and privilege subsystem | NOT STARTED | In-kernel ABI dispatch with validation and error conversion tested |
| 7 | Device abstraction and timer integration | NOT STARTED | Console/timer interfaces tested without invented devices |
| 8 | Task management and cooperative scheduling | NOT STARTED | Deterministic task creation, yield, and switch tested |
| 9 | Executable loading and user execution | NOT STARTED | `.nob` user image loads, runs, and terminates under NPK |
| 10 | Integrated validation and Phase 2 closeout | NOT STARTED | Full regression, NPK validation, report, clean synchronized push |

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

**Status:** COMPLETE.

**Objective.** Replace the placeholder vector with a real machine-mode Nova
assembly trap entry, exact software trap frame, typed processing-core dispatch,
classification, fault reporting, and `MRET` return while preserving Milestone 1
and Milestone 2 behavior.

**Dependencies.** Milestones 1–2.

**Files changed.**

- `nova/npk/src/trap.s`: real Nova assembly exception entry, 304-byte frame
  save/restore, separate trap stack, and `MRET`.
- `nova/npk/src/trap.c`: trap state initialization, classification, typed
  operation validation/execute/commit handlers, and dispatch.
- `nova/npk/src/npk_core.h`: operation layout shared by C translation units.
- `nova/npk/src/processing.c`: `NPK_OP_TRAP_PROCESS`, type-specific receipt
  routing, and trap receipt log.
- `nova/npk/src/kernel.c`, `nova/npk/src/start.s`: initialize trap state and
  remove the placeholder vector.
- `nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/traps.py`,
  `nova/npk/__init__.py`: fixed M3 regions, constants, decoders, and exports.
- `nova/npk/build.py`: assemble and link the Nova assembly trap entry and the
  C trap dispatcher.
- `tests/test_npk_exceptions.py`, `scripts/npk_validate.py`: 21 real-machine
  trap cases and repeatable validation.
- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_INITIATION_REPORT.md`, `docs/NPK_MILESTONE3_REPORT.md`.

**Implementation scope.**

- The fixed 304-byte frame at `0x90900` preserves all 32 GPRs, `psr`, `epc`,
  `ecause`, `ecode`, the handler privilege word, and `tval`.
- The assembly entry uses the low scratch slots at `0x200`, a separate trap
  stack at `0x88000`, and long-call return setup for the C dispatcher.
- `SYSTEM_CALL`, `SOFTWARE_TRAP`, illegal instruction, alignment, memory, and
  MMU faults are classified; other assigned codes fail as unsupported; invalid
  frames or codes are rejected.
- Every trap is admitted as `NPK_OP_TRAP_PROCESS` and advanced through
  `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` with PU ownership and an
  independent eight-slot trap receipt log at `0x90B00`.
- Results are published only at `COMMIT`; `MRET` restores GPRs, `psr`, `tval`,
  exact `epc`, privilege, and clears `ecode`.
- No syscall service, object-manager behavior, allocator, scheduler, device,
  loader, delegation, transactional rollback, or nested handler is implemented.

**Tests.**

- Layout, exact frame offsets, alignment, real vector installation, and boot
  initialization of trap observations.
- Real `SCALL` frame capture for every GPR and exception field.
- Full `MRET` restoration of registers, `psr`, `tval`, `epc`, `ecode`,
  `ecause`, privilege, and the platform save stack.
- `TRAP`, illegal instruction, alignment, memory, and injected MMU fault
  classification and exact resume PC.
- Invalid `ecode`, malformed frame, unsupported class, and corrupt trap-state
  rejection/failure receipts without result publication.
- Independent trap receipt validation without disturbing Milestone 1 or
  Milestone 2 receipts and counters.
- Platform exception-stack depth four with oldest-frame discard.
- Preservation of syscall numbers 4, 5, and 6 for the planned Object Manager.
- Documentation checks for frame size, `MRET`, and nesting limitations.

**Acceptance criteria.** Every handler test proves register/CSR state and exact
resumption PC; no trap is silently swallowed; Milestone 1 and Milestone 2
receipts and counters remain exactly unchanged.

**Known limitations.** The frame is single-level and nested exception handling
is unsupported. The platform's depth-4 internal save stack remains the only
privilege/PC return source. The privilege word in the frame is the handler
privilege, not saved user privilege. There is no supervisor delegation,
`SRET`/`ERET`, hardware trap frame, syscall dispatch, or physical-hardware
claim. MMU and unsupported-class cases use explicit host injection where the
guest cannot provoke them deterministically in the reference model.

**Validation result.**

- `python -m pytest -q tests/test_npk.py tests/test_npk_core.py
  tests/test_npk_exceptions.py`: **71 passed**.
- `python scripts/npk_validate.py`: **PASS** (NPK 71 passed; Phase 1 PASS).
- `python scripts/phase1_validate.py`: **PASS**; counts unchanged (frontend 180,
  sema 85, codegen 59, driver 55, runtime 15, library 1, end-to-end 13,
  emulator 1831, example 1, conformance 18/18, compileall passed, full suite
  2316).
- `python -m pytest -q tests/test_documentation.py`: **17 passed**.
- `python -m pytest -q`: **2316 passed**.
- Milestone 1 and Milestone 2 evidence unchanged: boot receipt magic `NPKP`,
  input `0x4E504B31`, output `0x4E504B32`, status `1`, trace `1,2,3,4,5`;
  core counters `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
  `selftest_status=0`, both units idle.
- Full detail: `docs/NPK_MILESTONE3_REPORT.md`.

**Completion conditions.** Trap frame layout and dispatch documented, all
validation commands green, scoped commits created, and `master` clean and equal
to `origin/master`.

## Milestone 4 — Physical and virtual memory management

**Status:** COMPLETE.

**Objective.** Add an NPK-managed physical allocator, software page-table
manager, owner-scoped address spaces, typed memory operations, permission
validation, and `TLBI` invalidation while preserving Milestones 1–3 exactly.

**Dependencies.** Milestones 1–3.

**Files changed.**

- `nova/npk/src/memory.c`: allocator, region/address-space tables, typed
  validate/execute/commit handlers, page-table construction, and request wrapper.
- `nova/npk/src/memory.s`: real `TLBI r0` full-invalidation helper.
- `nova/npk/src/start.s`: publishes the memory request entry at `0x90F00`.
- `nova/npk/src/kernel.c`: coordinator only; boot → trap → memory init.
- `nova/npk/src/npk_core.h`, `nova/npk/src/processing.c`: memory operation
  arguments, dedicated receipt routing, and core initialization.
- `nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/memory.py`,
  `nova/npk/__init__.py`: fixed layout, M4 op/reason constants, host decoders,
  validators, and request-call helpers.
- `nova/npk/build.py`: assembles and links the new M4 sources.
- `tests/test_npk_memory.py`, `scripts/npk_validate.py`: 29 real-machine tests
  and repeatable validation.
- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_INITIATION_REPORT.md`, `docs/NPK_MILESTONE4_REPORT.md`.

**Implementation scope.**

- A declared 256 KiB physical arena at `0xC0000` over sparse reference memory,
  with 4 KiB pages, aligned fit, stable region ids, owner, type, flags, and
  live-map counts. No emulator internals or host allocator objects are used.
- Five typed operations — `ALLOCATE`, `RELEASE`, `MAP`, `UNMAP`, `QUERY` — each
  admitted through the shared processing core and advanced through all five
  lifecycle stages.
- A 64-slot region table, 2-slot address-space table, request/response block,
  and independent 128-slot memory receipt log at fixed addresses. Boot work
  never admits a memory operation and leaves M1–M2 counters/receipts unchanged.
- Four-level Nova page-table construction with 4 KiB leaf PTEs and R/W/X/U bit
  translation. The existing emulator translator enforces the resulting
  permissions, A/D updates, `tval`, and `MMU_FAULT` details.
- Release is rejected while a region has live mappings; unmap decrements the
  region count and performs a full `TLBI r0` invalidation. Each address space
  has an independent root and owner.
- `REJECTED` is used for bad request shape, size, alignment, address,
  permissions, region, address space, owner, or state. `FAILED` is used for
  arena exhaustion and PTE conflicts discovered after validation.

**Tests.**

- Boot-time memory initialization, published entry, zeroed observation regions,
  unchanged M1 evidence, unchanged M2 counters, and zeroed main-core receipt
  reserved words.
- Allocation identity, region descriptors, allocation epoch, raw tag bytes,
  query immutability, release, stale ids, double release, owner enforcement,
  invalid sizes/alignment, and arena exhaustion recovery.
- Four-level page-table walks, leaf PPN/permission bits, duplicate-map commit
  failure, rejected-map immutability, unmap, double-unmap distinctions, and
  release protection while mapped.
- Two independent address spaces, paged guest code reading and writing mapped
  memory, real user-permission enforcement, `tval`/failure details, and TLB
  invalidation after unmap.
- Dedicated memory receipts for complete, rejected, and failed outcomes, full
  stage traces, receipt size/evidence words, and no mutation of the 12-slot M2
  log.
- A real trap still dispatches and completes after memory initialization.

**Acceptance criteria.** Every supported memory operation traverses
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` or an explicit terminal
`REJECTED`/`FAILED` path; every success receipt is checked against real page
tables, descriptors, epochs, and memory bytes. Nova ISA v1.0, the Nova64 ABI,
reserved syscall numbers, and M1–M3 behavior are unchanged.

**Known limitations.** The arena is a software policy region, not discovered
RAM or a hardware frame pool. Only 4 KiB leaf mappings are constructed; the
platform's 2 MiB and 1 GiB leaves are not used by NPK yet. There is no ASID,
TLB shootdown, MMIO region, RAM-size model, global-bit policy, demand paging,
copy-on-write, page replacement, executable loader, user process, or syscall
service. Commit is not transactional: there is no rollback, journal, isolation,
or durability guarantee, and a commit-time failure may leave earlier page-table
allocations visible. PUs remain sequential; no SMP or preemption is claimed.
This is not a physical-hardware implementation.

**Validation result.**

- `python -m pytest -q tests/test_npk.py tests/test_npk_core.py
  tests/test_npk_exceptions.py tests/test_npk_memory.py`: **100 passed**
  (71 M1–M3 plus 29 M4).
- `python scripts/npk_validate.py`: **PASS** (NPK 100 passed; Phase 1 PASS).
- `python scripts/phase1_validate.py`: **PASS**; counts unchanged (frontend 180,
  sema 85, codegen 59, driver 55, runtime 15, library 1, end-to-end 13,
  emulator 1831, example 1, conformance 18/18, compileall passed, full suite
  2345).
- `python -m pytest -q tests/test_documentation.py`: **17 passed**.
- `python -m pytest -q`: **2345 passed** (Phase 1 baseline 2316 plus 29 M4
  tests).
- `python -m compileall -q nova tests scripts`: **passed**.
- M1/M2 evidence unchanged: boot receipt magic `NPKP`, input `0x4E504B31`,
  output `0x4E504B32`, status `1`, trace `1,2,3,4,5`; core counters
  `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
  `selftest_status=0`, both units idle, and all 12 M2 receipt reserved words
  zero.
- Full detail: `docs/NPK_MILESTONE4_REPORT.md`.

**Completion conditions.** Memory layout and policies documented, all validation
commands green, scoped commits created, and `master` clean and equal to
`origin/master`.

## Milestone 5 — Object Manager and Universal Object Model

**Status:** PLANNED ONLY. This is an architectural milestone only; no
implementation is authorized in this task.

**Objective.** Define a consistent kernel-resource management contract based on
the Winoid principle "everything is an object." Interpret this as consistent
kernel-managed resource identity, typing, handles, permissions, and lifetime.
It does not mean that every variable, register, instruction, or byte becomes a
separately allocated object.

**Dependencies.** Milestones 1–4.

**Why here.** Memory management supplies the allocation and address-space
foundations that object storage and sharing need. The Object Manager then
provides stable identity, handles, and lifetime rules for kernel resources.
Syscalls can subsequently expose object operations through a stable interface
without baking object layout into the ABI. Future object operations must be
admitted as NPK operations and processed through the existing
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` pipeline, including rejection,
failure, and receipt evidence.

**Architectural candidates, not implemented claims.** processes, threads,
memory regions, address spaces, devices, files, events, timers, communication
ports, and security/access tokens.

**Design questions to review before implementation.**

- Object identity and type safety.
- Object headers and metadata; the binary layout is explicitly not frozen.
- Handles and handle tables.
- Reference and handle lifetime semantics.
- Creation, opening, closing, querying, referencing, dereferencing, and
  destruction.
- Access permissions and sharing between processes.
- Optional object naming and parent/child ownership where appropriate.
- Processing-pipeline integration, failure receipts, and resource cleanup.

**Nova ABI constraint.** Preserve the existing syscall numbers
`SYS_OBJ_CREATE = 4`, `SYS_OBJ_SIZE = 5`, and `SYS_OBJ_DESTROY = 6`. Their
object semantics are not implemented by this planned milestone description.

**Completion conditions (future).** A reviewed object contract, processing-core
integration, tests, documentation, and a scoped commit. No object-manager code
is part of the Milestone 4 implementation.

## Milestone 6 — Syscall and privilege subsystem

**Objective.** Implement the in-kernel syscall dispatcher while preserving Nova
syscall ABI v1.0.

**Dependencies.** Milestones 1–5.

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

## Milestone 7 — Device abstraction and timer integration

**Objective.** Provide generic device interfaces only for capabilities the
platform genuinely has.

**Dependencies.** Milestones 1–6.

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

## Milestone 8 — Task management and cooperative scheduling

**Objective.** Implement tasks, states, ready queues, `SYS_YIELD`, and context
switching without claiming preemption prematurely.

**Dependencies.** Milestones 1–7.

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

## Milestone 9 — Executable loading and user execution

**Objective.** Load and run user `.nob` images under NPK.

**Dependencies.** Milestones 1–8.

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

## Milestone 10 — Integrated validation and Phase 2 closeout

**Objective.** Harden, document, and close Phase 2 with repeatable validation.

**Dependencies.** Milestones 1–9.

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

**Completion conditions.** Closeout report committed and pushed; no milestone 11
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
