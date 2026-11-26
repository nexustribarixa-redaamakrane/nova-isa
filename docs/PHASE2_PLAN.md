# Phase 2 plan — NPK (Nova Processed Kernel)

**Phase status:** COMPLETE / CLOSED after Milestone 10 validation.

**Phase 1 baseline:** `0139e5a`, verified clean and synchronized before work began.

**Phase 2 final tree:** see `docs/NPK_PHASE2_CLOSEOUT.md`.

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
| 5 | Object Manager and Universal Object Model | COMPLETE | Typed object identity, handles, references, namespaces, access policy, destruction, and receipts tested |
| 6 | Syscall and privilege subsystem | COMPLETE | In-kernel ABI dispatch with validation and error conversion tested |
| 7 | Device abstraction and timer integration | COMPLETE | Typed device/timer lifecycle, bounded receipts, console sink, and software timer events tested without invented devices |
| 8 | Task management and cooperative scheduling | COMPLETE | Deterministic task creation, yield, and switch tested |
| 9 | Executable loading and user execution | COMPLETE | `.nob` user image loads, runs, and terminates under NPK |
| 10 | Integrated validation and Phase 2 closeout | COMPLETE | Full regression, NPK validation, report, clean synchronized push |

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

**Status:** COMPLETE.

**Objective.** Add a real NPK Object Manager that applies the Winoid principle
"everything is an object" as one common contract for kernel-managed resources:
identity, type, owner, lifetime, references, handles, access policy, namespace
membership, and type-specific destruction. It does not mean one object per
variable, register, instruction, or byte.

**Dependencies.** Milestones 1–4.

**Files changed.**

- `nova/npk/src/objects.c`: object state, typed headers, handles, references,
  type registry, namespace, typed validate/execute/commit handlers, and request
  wrapper.
- `nova/npk/src/processing.c`, `nova/npk/src/npk_core.h`: object operation
  arguments, stable object identity, dedicated receipt routing, and extension
  synchronization.
- `nova/npk/src/kernel.c`, `nova/npk/src/start.s`: coordinator-only
  initialization and the published object request entry.
- `nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/objects.py`,
  `nova/npk/__init__.py`: fixed layout, operation/reason constants, host
  decoders, validators, and request-call helpers.
- `nova/npk/build.py`: compiles and links the new object manager source.
- `tests/test_npk_objects.py`, `scripts/npk_validate.py`: 23 real-machine tests
  and repeatable validation.
- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_INITIATION_REPORT.md`, `docs/NPK_MILESTONE5_REPORT.md`.

**Implementation scope.**

- Twelve typed operations — create, destroy, open, close, query, retain,
  release, directory insert/remove/lookup, and type register/query — each
  admitted through the shared core and advanced through all five stages.
- Monotonic 64-bit object identities based on `0x4E504B4F00000000`, object state
  magic `0x4E504B4F`, version `5`, and a canonical header-first 128-byte object
  slot with a fixed inline body area.
- Two owner-scoped handle tables of 64 generation-tagged slots. Handles pack
  owner, generation, and index into 64 bits; stale and reused handles are
  rejected.
- References are the sum of handle, retain, and namespace references. Destruction
  requires owner match, the `DESTROY` permission, and zero references; type
  descriptors dispatch bounded cleanup actions through central C code rather
  than C function pointers.
- Permissions are `READ`, `WRITE`, `EXEC`, `SHARE`, and `DESTROY`. Same-owner
  operations require handle ownership; cross-owner `OPEN` additionally requires
  `SHARE` on both source handle and object and a requested subset of both
  permission sets.
- A fixed type registry has four built-in types and four runtime slots. Types
  carry body size, valid operations, allowed access, and a destroy action.
- A fixed root directory and 16 namespace entries support insertion, removal,
  duplicate-name rejection, single-component names, and hierarchical paths up
  to 32 bytes with no symbolic links. Namespace membership owns one reference.
- M4 memory regions and address spaces can be wrapped as typed objects with
  validation of the underlying owner. Cleanup calls the existing M4 release
  service; a live mapping makes destruction fail without claiming rollback.
- A 112-byte request block, 64-byte response block, 128 dedicated receipts, and
  128 parallel 32-byte extension records expose object identity, handle,
  packed counts, and namespace id without changing M1–M4 receipt words.
- `kernel.c` remains coordinator-only; object policy lives in `objects.c`.

**Tests.**

- Fixed-layout disjointness, boot-time object/type/namespace state, reserved
  syscall numbers, and unchanged M1–M4 receipts, counters, trap path, and
  memory behavior.
- Type query, runtime registration, duplicate rejection, and invalid type ids.
- Create/query/destroy, receipt extensions, close/double-close/stale handle
  reuse, same-owner and cross-owner access, retain/release, owner checks, and
  zero-reference destruction.
- M4 payload wrapping, memory cleanup, commit failure with a live mapping,
  namespace create/insert/lookup/remove lifetime, path/name validation, and
  no-mutation rejection.
- Bad magic, unknown operation, invalid request shapes, object-table and
  handle-table exhaustion as `FAILED`, and dedicated receipt routing.

**Acceptance criteria.** Every supported object operation traverses the shared
five-stage lifecycle or an explicit terminal `REJECTED`/`FAILED` path with a
specific reason and observable receipt. Identity, handle, reference, access,
namespace, and destruction behavior are independently decoded from real kernel
state. Nova ISA v1.0, the Nova64 ABI, Phase 1, and M1–M4 behavior are unchanged.

**Known limitations.** Storage is fixed and static; there is no general kernel
heap, dynamic type destruction, symbol table, symbolic link, ACL/token model,
full NT security model, quotas, inheritance, multilevel handle table, 37-bucket
hash table, broad callback matrix, process/thread identity, device/file object,
scheduler, syscall dispatcher, user process, or loader. No synchronization is
claimed: the core's processing units remain sequential and non-preemptive.
Commit is not transactional; there is no rollback, journal, isolation, or
durability. Namespace paths are flat component chains over a fixed table, not
a general filesystem. At Milestone 5 completion, reserved syscall numbers `4`,
`5`, and `6` remained unchanged and inert; Milestone 6 now maps them to these M5
operations without renumbering them. This is not physical-hardware support.

**Validation result.**

- `python -m pytest -q tests/test_npk_objects.py`: **23 passed**.
- `python -m pytest -q tests/test_npk.py tests/test_npk_core.py
  tests/test_npk_exceptions.py tests/test_npk_memory.py
  tests/test_npk_objects.py`: **123 passed** (100 M1–M4 plus 23 M5).
- `python scripts/npk_validate.py`: **PASS** (NPK 123 passed; Phase 1 PASS).
- `python scripts/phase1_validate.py`: **PASS**; counts unchanged (frontend 180,
  sema 85, codegen 59, driver 55, runtime 15, library 1, end-to-end 13,
  emulator 1831, example 1, conformance 18/18, compileall passed, full suite
  2368).
- `python -m pytest -q tests/test_documentation.py`: **17 passed**.
- `python -m pytest -q`: **2368 passed** (2345 baseline plus 23 M5 tests).
- `python -m compileall -q nova tests scripts`: **passed**.
- Preserved M1/M2 evidence and all M3/M4 interfaces; full detail:
  `docs/NPK_MILESTONE5_REPORT.md`.

**Completion conditions.** Object design comparison, implementation, tests,
documentation, and scoped commits complete; all validation commands green;
`master` clean and equal to `origin/master`.

## Milestone 6 — Syscall and privilege subsystem

**Status:** COMPLETE.

**Objective.** Expose existing M5 object-manager functionality through the frozen
Nova syscall ABI using a real, separately processed NPK syscall subsystem.

**Dependencies.** Milestones 1–5.

**Files changed.**

- `nova/npk/src/syscalls.c`, `nova/npk/src/syscalls.s`: syscall admission,
  validation, execution, commit, error conversion, and the C-ABI time helper.
- `nova/npk/src/trap.c`, `nova/npk/src/kernel.c`: `SCALL` handoff and
  coordinator-only syscall initialization.
- `nova/npk/src/processing.c`, `nova/npk/src/npk_core.h`: type `21`, reasons
  `46..56`, dedicated receipt routing, syscall operation arguments, and child
  receipt publication.
- `nova/npk/src/memory.c`, `nova/npk/src/objects.c`: read-only user-pointer walk
  over M4 page tables and a typed bridge to real M5 object requests.
- `nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/syscalls.py`,
  `nova/npk/__init__.py`: fixed M6 layout, constants, state/receipt decoders,
  ABI expectations, and the real `SCALL` harness helper.
- `nova/npk/build.py`: compiles and links the two new syscall objects.
- `tests/test_npk_syscalls.py`, `scripts/npk_validate.py`: real-machine syscall
  tests and repeatable validation.
- `tests/test_npk_exceptions.py`, `tests/test_npk_memory.py`: preserve the
  original M3/M4 intent while accounting for published syscall results.
- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_MILESTONE6_RECONNAISSANCE.md`, `docs/NPK_MILESTONE6_REPORT.md`.

**Implementation scope.**

- `SCALL imm` continues through the real M3 trap entry. The C trap dispatcher
  records its trap operation and hands the frame to the syscall subsystem;
  `frame->ecause` is the syscall number and `gpr[1]..gpr[6]` are `a0..a5`.
- Every call admits a type-21 operation into the shared core and traverses
  `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE`. Malformed calls are
  `REJECTED`; execution, child, and commit faults are `FAILED`.
- A dedicated eight-slot syscall receipt log at `0x9FA90` never overwrites a
  record silently. M1–M5 receipts, counters, and layout are unchanged.
- Validation covers the frame, initialization, handler context, number and
  argument count, write capacity, canonical user addresses with explicit `U`
  and read PTE bits, object handles, ownership, and access requirements.
- `SYS_EXIT` and `SYS_YIELD` set observation flags only. `SYS_TIME` reads the
  live CSR; `SYS_READ` reports `UNSUPPORTED`.
- `SYS_WRITE` stages at most 64 bytes in a scratch buffer and publishes to a
  256-byte bounded log only at commit.
- Object syscalls translate into M5 `OP_OBJECT_CREATE`, `OP_OBJECT_QUERY`,
  `OP_OBJECT_CLOSE`, and `OP_OBJECT_DESTROY` requests and preserve child receipt
  evidence. No object logic is reimplemented.
- Success writes the result to saved `gpr[1]` and zero to `gpr[2]`; failure
  writes all-ones and error `1..4`. Publication occurs before the assembly
  trap entry restores GPRs and executes `MRET`.

**Tests.**

- Initialization, frozen ABI constants, real `SCALL` framing, `a0`/`a1`
  publication, invalid numbers and arguments, and dedicated receipt routing.
- Success and failure paths for `SYS_EXIT`, `SYS_YIELD`, `SYS_TIME`,
  `SYS_WRITE`, `SYS_READ`, and object create/size/destroy.
- Mapped, unmapped, non-user, non-readable, overflowing, and out-of-range user
  pointers with no kernel write on rejection.
- Bounded write/scratch logs, child M5 receipt indexes, preserved M1–M5
  evidence, and unchanged Phase 1 host dispatcher behavior.

**Acceptance criteria.** Every syscall traverses the shared five-stage lifecycle
or an explicit terminal path with a specific reason and receipt. Return values
and errors are observable in restored `a0`/`a1`, invalid user pointers cannot
read or write kernel memory, and Nova ISA v1.0, the Nova64 ABI, Phase 1, and
M1–M5 behavior are unchanged.

**Known limitations.** Nova v1.0 does not expose the pre-trap caller privilege
inside the software trap frame; `handler_priv` is always machine mode, so caller
privilege is not enforced. There is no hosted libc, dynamic linking, `FILE`
stream, process, scheduler, input device, or executable loader. Object calls use
one fixed kernel-owned observation owner. Commit is not transactional, and no
caller/kernel address-space split or hardware device support is claimed.

**Completion conditions.** Dispatch contract documented, tests green,
regression green, committed.

## Kernel workflow — CMake and QEMU validation

**Status:** IMPLEMENTED after Milestone 6; not a new Phase 2 milestone.

The kernel build is orchestrated by the root `CMakeLists.txt`. CMake calls
`scripts/npk_cmake_build.py`, which calls the same `nova.npk.build_kernel()`
API used by the host tests. The generated `npk.nob` therefore still comes from
the in-tree Nova assembler, compiler, linker, and `.nob` container. No host C
compiler builds kernel code, and no second kernel source path exists.

Targets:

- `npk_kernel` (default): build `npk.nob` into `${build}/npk-kernel`.
- `npk_validate`: run the NPK and Phase 1 regression suites.
- `npk_qemu_boot`: build the kernel, generate a QEMU-loadable image, boot it in
  `qemu-system-x86_64`, and compare guest memory with the reference boot.

QEMU has no Nova target. `nova/npk/qemu_translator.py` does not add one. It
emits an x86-64 multiboot ELF that interprets the real `.nob` bytecode in long
mode. The Nova image is copied into a 1 MiB guest-physical shadow at
`0x100000`; every Nova data address is offset into that shadow, so Nova software
still observes its architectural stack, marker, receipt, and vector addresses.
Generated interpreter code is placed above the shadow so an out-of-range Nova
store cannot overwrite the host. This is a generated validation harness, not
firmware, board support, RTL, or native QEMU Nova support.

The validator checks the boot marker, exact step count, and a 64 KiB guest
memory region against `boot_kernel()`. On this platform the expanded M7 kernel
halts after `311762` interpreted Nova steps with the expected `0x4E504B31`
marker and a byte-identical observation region. The narrow pre-expansion M7
image halted after `254492` steps, and the M6 image used before device
integration halted after `246745` steps. The host boot default is `400000`
steps; the expanded kernel still halts deterministically well within it.

**Validation result.**

- `cmake -S . -B <build>` and `cmake --build <build> --target npk_kernel`: PASS.
- `cmake --build <build> --target npk_qemu_boot`: `QEMU boot: PASS`,
  `boot marker: 0x4e504b31`, `steps: 311762`.
- `python scripts/npk_qemu_boot.py --verbose`: PASS.
- `python -m pytest -q tests/test_npk_qemu.py`: PASS.

## Milestone 7 — Device abstraction and timer integration

**Status:** COMPLETE.

**Objective.** Provide typed device and timer processing through the real NPK
lifecycle, using only capabilities the platform genuinely has.

**Dependencies.** Milestones 1–6.

**Files changed.** `nova/npk/layout.py`, `nova/npk/core.py`,
`nova/npk/devices.py`, `nova/npk/__init__.py`, `nova/npk/boot.py`,
`nova/npk/build.py`, `nova/npk/src/npk_core.h`, `nova/npk/src/processing.c`,
`nova/npk/src/devices.c`, `nova/npk/src/devices.s`, `nova/npk/src/start.s`,
`nova/npk/src/syscalls.c`, `nova/npk/src/trap.c`, `tests/test_npk_devices.py`,
`tests/test_npk_device_registry.py`, `tests/test_npk_timers.py`,
`tests/test_npk_device_core_integration.py`, `scripts/npk_validate.py`, and
architecture docs. `nova/npk/src/kernel.c` is unchanged by the expanded work;
it retains the single deterministic `npk_device_init()` call in the existing
initialization order.

**Implementation scope.**

- Typed request operations `22..30` cover device register, unregister, query,
  and write, plus timer create, cancel, inspect, expire, and deliver. Reasons
  `57..77` distinguish bad requests, unsupported kinds/capabilities/backends,
  missing or duplicate ids, exhaustion, state, parameter, owner/object, and
  timer-specific failures.
- Every accepted request uses the shared Milestone 2 operation slots and two
  processing units. `ADMIT`, `VALIDATE`, `EXECUTE`, `COMMIT`, and `COMPLETE` are
  real stages with bounded helpers; rejected, failed, and completed outcomes are
  distinct, and the processing unit is released on every terminal outcome.
- The 8-entry registry stores unique nonzero ids, kind, caller capability,
  owner, object handle, state, operation count, and last result. Multiple
  supported-kind devices are allowed; unsupported kinds and capability or
  owner/object mismatches are explicit rejections.
- Device writes use Milestone 4 user-physical validation, copy into bounded
  kernel scratch during `EXECUTE`, and commit to the Milestone 6 write log. The
  console sink remains the same bounded log, mirrored by the host into
  `machine.io_log`; no MMIO, DMA, or host syscall dispatch is involved.
- The 8-entry timer table and 8-entry event log use explicit `PENDING`,
  `EXPIRED`, `DELIVERED`, and `CANCELLED` states. Expiration uses the earliest
  deadline and lowest timer id, cancellation is pending-only, and terminal
  events are never duplicated or retroactively cancelled.
- Dedicated 32-entry device and timer receipt tables record operation id, type,
  processing unit, stage/state/outcome, result/error, reason, full stage trace,
  and device/timer evidence. Exhaustion is rejected before admission and does
  not overwrite earlier receipts.
- The architectural timer/interrupt backend still uses only `time`, `timecmp`,
  `ie`, `ip`, and `ivec`. The real M3 trap path classifies `ecode == 8` source
  bits and records frames and exception receipts; timer API events are software
  events and never inject a hardware interrupt.

**Tests.** Bounded disjoint layout, initialization and capability status,
`SYS_WRITE` console bytes and `io_log` mirroring, all three interrupt classes
through the real vector, one-shot timer reprogramming, typed registry lifecycle,
duplicate/unsupported/capability/object rejection, registry exhaustion, M4
pointer validation for writes, write-log evidence, timer parameter validation,
equal-deadline ordering, cancellation/delivery terminality, timer and event
exhaustion, receipt exhaustion, shared-core processing-unit reuse, receipt
isolation, trap-path coexistence, and boot-evidence survival. External
interrupts and injected software interrupts are architectural test events only.

**Validation result.** Focused tests pass in `tests/test_npk_devices.py`,
`tests/test_npk_device_registry.py`, `tests/test_npk_timers.py`, and
`tests/test_npk_device_core_integration.py`. `python scripts/npk_validate.py`
passes both the NPK stage and the unchanged Phase 1 stage. The rebuilt QEMU
harness reports `steps: 311762` and byte-identical guest memory against the
reference boot.

**Acceptance criteria.** No invented MMIO device, physical interrupt, fake bus,
DMA, scheduler, wakeup, or claim of hardware timer support.

**Known limitations.** NovaMachine has no device tree, MMIO, interrupt
controller, framebuffer, keyboard, disk, or network model. The timer API uses
the emulator `time` CSR and bounded logical records; it is not a hardware timer.
Caller privilege remains unobservable from the exposed machine-mode trap frame.
There is no device-specific ready queue, preemptive scheduler, process model, or
user loader.

**Completion conditions.** Device/timer contract documented, focused tests
green, regression green, committed.

## Milestone 8 — Task management and cooperative scheduling

**Status:** COMPLETE.

**Objective.** Implement honest cooperative task and execution management through
the NPK processing core, without claiming preemption, process isolation, or
hardware context switching that Nova/NPK do not provide.

**Dependencies.** Milestones 1–7.

**Files changed.** `nova/npk/src/tasks.c`, `nova/npk/src/tasks.s`,
`nova/npk/src/trap.c`, `nova/npk/src/syscalls.c`, `nova/npk/src/processing.c`,
`nova/npk/src/npk_core.h`, `nova/npk/src/kernel.c`, `nova/npk/src/start.s`,
`nova/npk/tasks.py`, `nova/npk/core.py`, `nova/npk/layout.py`,
`nova/npk/build.py`, `nova/npk/__init__.py`, `nova/npk/qemu_translator.py`,
`scripts/npk_qemu_boot.py`, `scripts/npk_validate.py`, `.gitignore`,
`tests/test_npk_tasks.py`, updated `tests/test_npk_qemu.py`,
`tests/test_npk_syscalls.py`, and NPK docs.

**Implementation scope.**

- Bounded task table (8 slots), deterministic FIFO ready queue, and TCB records
  with stable task ID, generation, state, owner, parent, entry, stack bounds,
  arg, address-space field, exit status, resume PC/patch metadata, flags, and
  saved software trap-frame context.
- Task states: `FREE`, `CREATED`, `READY`, `RUNNING`, `BLOCKED`, `TERMINATED`.
  Typed task operations: create, terminate, yield, block, wake, query, destroy.
  Every supported task operation traverses `ADMIT → VALIDATE → EXECUTE → COMMIT →
  COMPLETE` through the shared processing core and emits a bounded task receipt.
- Cooperative scheduling is integrated at the existing trap/syscall boundary.
  `SYS_YIELD` and `SYS_EXIT` remain ABI-compatible syscalls (`0` and `7`); after
  a successful syscall dispatch, `trap.c` invokes `npk_task_schedule()`.
  Rejected/failed syscalls do not reschedule.
- Context-switch model is honest to Nova semantics: Nova `MRET` restores
  EPC/privilege from the emulator hidden exception stack, not from the software
  trap frame. The scheduler saves/restores software TCB frame state, patches a
  suspended writable return site with `JMP r15` (`0x50780000`), restores the
  selected task's software frame into the live trap frame, and sets `frame->gpr[15]`
  to the selected resume PC before `MRET` returns to the patched site.
- Bootstrap/idle behavior: the first scheduler-visible trap context is captured as
  a resumable kernel/bootstrap task. If no resumable task remains after terminate/
  block, the scheduler patches the return site to an honest halt trampoline rather
  than inventing a runnable user task.
- `SYS_YIELD`/`SYS_EXIT` behavior is real cooperative scheduling policy, not a
  host-only placeholder: yield records yield request and reschedules; exit records
  exit request, terminates the current task, and schedules or halts cooperatively.
- The generated QEMU interpreter gained `UDIV`, `REM`, `UREM`, `NOT`, `RET`,
  `MRET`, and `TLBI` coverage and records the failing PC/word on translator error.
  `TLBI` is a no-op barrier in the non-paging shadow. The normal MRET return path
  restores EPC/ECODE/privilege from a shadow stack; exception entry, depth-4
  overflow, and trap delivery for underflow/misaligned EPC remain harness
  limitations.

**Tests.** `tests/test_npk_tasks.py` covers layout/initial state, create/query
receipts, validation/duplicate rejection, block/wake/transition checks,
terminate/query/stale lookup behavior, cooperative `SYS_YIELD` reschedule,
`SYS_EXIT` termination/no-runnable behavior, bounded receipts, destroy/recreate,
slot exhaustion/no partial mutation, TCB frame non-overlap at full capacity,
failed `TASK_WAKE` with a full queue, rejected syscalls staying out of the
scheduler, and task-query processing-core integration.
`tests/test_npk_syscalls.py` was deliberately updated for M8 `SYS_YIELD`/
`SYS_EXIT` cooperative behavior while preserving unaffected syscall semantics.
`tests/test_npk_qemu.py` covers the new translator instruction coverage and
error-PC diagnostics. `scripts/npk_validate.py` includes both
`tests/test_npk_tasks.py` and `tests/test_npk_qemu.py`.

**Acceptance criteria.** Cooperative task lifecycle, deterministic ready queue,
bounded receipts, and syscall-integrated yield/exit are tested through real NPK
paths; no preemption/SMP/process-isolation claims are made.

**Known limitations (as of M8).** Cooperative only; no preemption,
timer-driven scheduling, SMP, per-task address-space isolation, user loader,
process/thread split, or MMIO. Later M9/M10 work added a native `.nob` loader and
integration validation but did not add preemption, SMP, MMIO, or strong process
isolation. Tasks share a single address space unless explicitly mapped per task. The context switch depends on
writable return sites and single-level trap-frame assumptions. Freshly created
tasks are kernel records/READY queue entries but are not magically started as
user contexts without an explicit future execution/start model. M6 caller-
privilege enforcement remains limited because the exposed trap frame does not
carry original caller privilege. The QEMU path is a generated x86-64 interpreter
executing Nova bytecode, not native Nova support in QEMU. Its MRET model covers
the normal return path only; exception entry, exception-stack overflow, and trap
delivery for underflow/misaligned EPC are not modeled.

**Completion conditions.** Task model/tests/docs/validation green, logical M8
commits created, `master` pushed only after validation passes. CTest exit status
is not treated as test evidence because it discovers no tests in this
configuration; full pytest, NPK validation, phase1 validation, and QEMU boot are
the recorded gates.

## Milestone 9 — Executable loading and user execution

**Status: COMPLETE.** See `docs/NPK_MILESTONE9_REPORT.md` for the final
validated results.

**Objective.** Load and run user `.nob` images under NPK.

**Dependencies.** Milestones 1–8.

**Implemented files.** `nova/npk/src/exec.c`, `nova/npk/src/exec.s`,
`nova/npk/executables.py`, `nova/npk/layout.py`, `nova/npk/core.py`, the existing
M4/M5/M8 helper modules, `tests/test_npk_executables.py`,
`scripts/npk_validate.py`, and the NPK architecture/validation documentation.

**Implementation scope.**

- Typed `LOAD`, `START`, `QUERY`, and `RELEASE` operations through the real
  processed kernel lifecycle and dedicated receipts/reason codes.
- Native `.nob` v1 header, flags, alignment, size, base, and entry validation.
- Image-byte copying through an M5 region-object handle into an M4 page.
- M8 task binding, `VMLAUNCH`, user `SCALL`, VM-exit recording, task termination,
  and deterministic exit status.
- Explicit rejection of unsupported flags, wrong owners/permissions/objects,
  live mappings, stale IDs, and exhausted records/receipts.

**Tests.** Valid full lifecycle, malformed containers, bad source windows, bad
handles/owners/permissions/objects, stale IDs, mapping validation, slot and
receipt exhaustion, VM-exit evidence, guest trap failure, task termination,
receipt stage traces, and M1–M8 regressions.

**Acceptance criteria.** A user program runs through NPK's MMU and syscall path
and terminates deterministically.

**Known limitations.** No relocations, dynamic loader, shared libraries, Nova
ELF compatibility, `.nod`, graphical shell, per-process isolation, preemptive
scheduling, or native-QEMU user execution.

**Completion conditions.** Loader contract documented, tests green, regression
green, committed and pushed.

## Milestone 10 — Integrated validation and Phase 2 closeout

**Status:** COMPLETE after validation. See `docs/NPK_MILESTONE10_REPORT.md` and
`docs/NPK_PHASE2_CLOSEOUT.md` for the final validated results and scope
classification.

**Objective.** Harden, document, and close Phase 2 with repeatable validation.

**Dependencies.** Milestones 1–9.

**Files changed.** `tests/test_npk_integration.py`, `scripts/npk_validate.py`,
`nova/npk/__init__.py`, `nova/npk/core.py`, `nova/npk/src/kernel.c`,
`nova/npk/src/npk_core.h`, `nova/npk/src/exec.c`, `nova/npk/src/devices.c`,
`nova/npk/src/processing.c`, `docs/PHASE2_PLAN.md`,
`docs/NPK_ARCHITECTURE.md`, `docs/VALIDATION.md`, `README.md`,
`docs/NPK_MILESTONE9_REPORT.md`, and the two new closeout reports.

**Implementation scope.**

- Added focused cross-subsystem tests for boot/core readiness, boot receipt
  preservation, typed operations from four subsystems, memory/object lifetimes,
  trap/syscall/task integration, devices/timers, executable load/launch/attach,
  rejected partial state, bounded receipt exhaustion, QEMU diagnostics, and
  Phase 1 toolchain determinism.
- Changed `scripts/npk_validate.py` to discover every `tests/test_npk*.py` file
  in sorted order; added meta-tests for export resolution, validator discovery,
  Python/C operation and reason parity, and shared layout literals.
- Fixed integration-level defects without changing frozen behavior: published
  exports now resolve, shared admission errors live in `npk_core.h`, `exec.c`
  uses `NPK_ERR_*` names for `npk_core_admit()` results, and the kernel init
  comment matches the actual order.
- Ran the full final gate matrix, both QEMU boot configurations, CMake/MinGW
  build, CTest, and clean-tree checks recorded in the closeout.

**Acceptance criteria.** Full NPK suite green; Phase 1 stage groups unchanged;
`full_suite` intentionally includes all NPK tests; documentation complete;
`master` clean and equal to `origin/master`.

**Known limitations.** No new Phase 2 capability was added: no preemption, SMP,
MMIO, physical hardware, desktop/OpenWindows work, native-QEMU Nova support,
ELF compatibility, dynamic linking, or Phase 3 work. Bare CTest exits 0 but
discovers no tests, so it is not counted as test coverage.

**Completion conditions.** Closeout report committed and pushed; no Milestone 11
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
