# Phase 2 plan — NPK (Nova Processed Kernel)

**Phase status:** AUTHORIZED / IN PROGRESS.  
**Phase 1 baseline:** `0139e5a`, verified clean and synchronized before work began.  
**Phase 3:** NOT STARTED.

Phase 2 is exclusively NPK development. It is not OpenWindows Nova64, NovaOW,
Oryn integration, NVMP, a desktop, a window manager, an application port, CPU
RTL, a replacement compiler, or an ISA redesign.

This roadmap has eight milestones. That count follows the real dependency chain
identified in the platform audit; the candidate work areas from the Phase 2
charter are mapped onto them without inventing empty milestones. A milestone is
COMPLETE only when its implementation, tests, documentation, and commit are
finished. No future milestone is marked complete early.

## Milestone map

| # | Milestone | Status | Gate |
| --- | --- | --- | --- |
| 1 | NPK architecture and boot foundation | NOT STARTED | Bootable deterministic `.nob` kernel image on `NovaMachine` |
| 2 | Exception and trap subsystem | NOT STARTED | Register-preserving trap frame and `MRET` return test |
| 3 | Physical and virtual memory management | NOT STARTED | Allocation, mapping, permissions, faults, and address-space isolation tested |
| 4 | Syscall and privilege subsystem | NOT STARTED | In-kernel ABI dispatch with validation and error conversion tested |
| 5 | Device abstraction and timer integration | NOT STARTED | Console/timer interfaces tested without invented devices |
| 6 | Task management and cooperative scheduling | NOT STARTED | Deterministic task creation, yield, and switch tested |
| 7 | Executable loading and user execution | NOT STARTED | `.nob` user image loads, runs, and terminates under NPK |
| 8 | Integrated validation and Phase 2 closeout | NOT STARTED | Full regression, NPK validation, report, clean synchronized push |

## Milestone 1 — NPK architecture and boot foundation

**Objective.** Establish the authoritative architecture, boot contract, kernel
layout, build workflow, and a minimal bootable NPK image using the existing
Nova toolchain.

**Dependencies.**

- Phase 1 complete at `0139e5a`.
- `docs/NPK_ARCHITECTURE.md` accepted as the Phase 2 architecture baseline.
- Platform audit recorded in `docs/NPK_INITIATION_REPORT.md`.

**Files likely to change.**

- `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
  `docs/NPK_INITIATION_REPORT.md`, `docs/PORTING_READINESS.md`, `README.md`.
- `nova/npk/` package: layout constants, image validation, boot helper, build
  helper, minimal kernel sources.
- `tests/test_npk.py`.
- `scripts/npk_validate.py`.
- `pyproject.toml` package data.

**Implementation scope.**

- Define `NPK` identity, Nova64-only CPU policy, boot contract, and layout.
- Build a `.nob` kernel with global entry `_npk_start` at `image_base = 0x1000`.
- Kernel entry sets `sp`, installs `vbar` from a link-computed symbol, writes a
  boot marker, calls `npk_main`, and halts with its return code in `a0`.
- Provide deterministic host-side build and boot helpers.
- Reject invalid kernel images and unexpected boot exceptions in tests.

**Tests.**

- NPK layout constants and non-overlapping regions.
- Kernel image validation.
- Deterministic rebuild produces byte-identical `.nob`.
- Boot reaches `HALT`, `a0 == 0`, marker matches, `vbar` lies in the image, and
  `sp` is the boot stack top.
- A corrupted/undefined instruction image is rejected without a false pass.
- Phase 1 regression remains green.

**Acceptance criteria.**

- `python scripts/npk_validate.py` passes.
- `python scripts/phase1_validate.py` remains green with unchanged counts.
- The minimal kernel builds from source through `nova-as`, `nova-cc`, and
  `nova-ld` with no new executable format.
- Documentation states that bare mode gives no user/kernel memory isolation.

**Known limitations.**

- Machine privilege is supplied by the host harness; there is no firmware.
- No allocator, trap frame, syscall dispatcher, devices, scheduler, user
  process, or preemptive behavior.
- Boot uses the host-side sparse memory model and a private reference-memory
  read for the marker assertion; this is a test helper, not a kernel API.

**Completion conditions.** Architecture and roadmap committed, minimal kernel
source and helpers committed, tests and validation script green, documentation
updated, and clean synchronized `origin/master`.

## Milestone 2 — Exception and trap subsystem

**Objective.** Replace the placeholder vector with a real machine-mode trap
entry, software trap frame, classification, fault reporting, and `MRET` return.

**Dependencies.** Milestone 1.

**Files likely to change.** `nova/npk/exceptions.py`, `nova/npk/src/trap.s`,
kernel C dispatch code, `tests/test_npk_exceptions.py`,
`docs/NPK_ARCHITECTURE.md`.

**Implementation scope.**

- Implement the documented 304-byte software trap frame.
- Preserve all GPRs and `psr` used by the handler.
- Dispatch by `ecode`, with `SYSTEM_CALL` classified for Milestone 4.
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

## Milestone 3 — Physical and virtual memory management

**Objective.** Add a declared physical RAM range, frame allocator, kernel
page-table manager, mapping/unmapping, permissions, fault handling, and TLB
policy wrappers.

**Dependencies.** Milestones 1–2.

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

## Milestone 4 — Syscall and privilege subsystem

**Objective.** Implement the in-kernel syscall dispatcher while preserving Nova
syscall ABI v1.0.

**Dependencies.** Milestones 1–3.

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

## Milestone 5 — Device abstraction and timer integration

**Objective.** Provide generic device interfaces only for capabilities the
platform genuinely has.

**Dependencies.** Milestones 1–4.

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

## Milestone 6 — Task management and cooperative scheduling

**Objective.** Implement tasks, states, ready queues, `SYS_YIELD`, and context
switching without claiming preemption prematurely.

**Dependencies.** Milestones 1–5.

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

## Milestone 7 — Executable loading and user execution

**Objective.** Load and run user `.nob` images under NPK.

**Dependencies.** Milestones 1–6.

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

## Milestone 8 — Integrated validation and Phase 2 closeout

**Objective.** Harden, document, and close Phase 2 with repeatable validation.

**Dependencies.** Milestones 1–7.

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

**Completion conditions.** Closeout report committed and pushed; no milestone 9
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
