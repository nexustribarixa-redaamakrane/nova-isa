# NPK Milestone 6 reconnaissance

This document records the source-level findings gathered before any Milestone 6
implementation change. It is a planning record, not a claim that M6 is
implemented. The repository was verified clean and synchronized at
`7859f8d9540afc04b0478f2d4af691a6cc39b71b` (`HEAD == origin/master`), with the
M5 implementation, test, and documentation commits present in history.

The pre-change `python scripts/npk_validate.py` baseline was reported successful
by the operator. No implementation file was edited while reconnaissance was in
progress.

## Scope boundary

M6 exposes existing M5 object-manager functionality through the frozen Nova
syscall ABI using the shared NPK processing core. It adds no new syscall number,
changes no ISA or compiler behavior, and does not implement devices, timers,
tasks, scheduling, process ownership, an executable loader, or a hosted user
environment.

## Frozen ABI facts (`nova/syscalls.py`, `nova/isa.py`)

- `SYSCALL_VERSION = "Nova syscall ABI v1.0"`.
- `SCALL imm` is S-format opcode `0x60`; `imm` is delivered in `ecause`, not in
  a register. `SYSTEM_CALL` is exception code `7`.
- Arguments are `a0`–`a5`, which are registers `r1`–`r6`. A successful call
  writes its return value to `a0`; a failed call writes the all-ones unsigned
  64-bit value (the architectural `-1`) to `a0` and one of four errors to `a1`:
  `1 = BAD_ARGUMENT`, `2 = UNSUPPORTED`, `3 = NOT_FOUND`, `4 = NO_RESOURCE`.
- The registered numbers are `0 SYS_EXIT`, `1 SYS_WRITE`, `2 SYS_READ`,
  `3 SYS_TIME`, `4 SYS_OBJ_CREATE`, `5 SYS_OBJ_SIZE`, `6 SYS_OBJ_DESTROY`,
  and `7 SYS_YIELD`. Any other number must produce `UNSUPPORTED`.
- `nova.syscalls.dispatch_syscall()` is a host/reference helper used by Phase 1
  tests. It is not the NPK kernel syscall path and its emulator-object handling
  must not be copied into the kernel implementation.
- `SYS_READ` remains unsupported because no input device exists. The reference
  `SYS_YIELD` and `SYS_EXIT` helpers halt the reference machine; NPK has no
  scheduler or process object, so any M6 kernel treatment must be explicitly
  documented rather than presented as scheduling.

## Trap and privilege path (`nova/emulator.py`, `nova/npk/src/trap.s`, `trap.c`)

- `SCALL` calls `_trap(SYSTEM_CALL, uimm, return_pc=pc + 4)`. Exception entry
  forces machine privilege, writes `ecode = 7` and `ecause = uimm`, saves the
  return PC in `epc`, and pushes `(epc, ecode, ecause, previous_priv)` on the
  emulator's internal depth-four save stack.
- The software trap frame at `0x90900` is 304 bytes. GPR slots occupy offsets
  `0..248` (`r0` at 0, `a0`/`r1` at 8, `a1`/`r2` at 16); `psr`, `epc`,
  `ecause`, `ecode`, `handler_priv`, and `tval` follow at offsets 256–296.
- Frame offset 288 is the **handler** privilege, always machine (`2`), not the
  caller privilege. Pre-trap privilege is only in the emulator's internal save
  stack and is not observable to guest kernel software in the frame. M6 must
  state this limitation instead of claiming caller-privilege enforcement from
  that field.
- No GPR is modified on exception entry. The assembly entry saves the incoming
  registers, calls `npk_trap_dispatch()`, then restores every GPR from the frame
  before `MRET`. Consequently a syscall result must be written into the frame's
  `gpr[0]`/`a0` and `gpr[1]`/`a1` slots, ideally during COMMIT; writing live
  registers in C would be lost during restoration.
- `MRET` clears `ecode`, restores the saved privilege, sets `pc = epc`, leaves
  `ecause` unchanged, and leaves `epc` at the resumed PC. For `SCALL`, `epc` is
  already `faulting PC + 4`.
- `npk_trap_classification()` already maps `ecode == 7` to
  `NPK_CLASS_SYSTEM_CALL`, and `npk_trap_action()` chooses
  `NPK_ACTION_RESUME_NEXT`. M3 does not inspect `ecause`, admit a syscall
  operation, validate arguments, or write `a0`/`a1`. Existing tests assert that
  M3 behavior and must remain compatible where required.
- Exception entry is single-level in the software frame. A fault inside the C
  handler reuses the frame; platform save-stack overflow discards the oldest
  frame. The handler must not fault without repointing `vbar`.
- User mode cannot read `ecode`, `ecause`, or `epc`, and `HALT` is machine-only.
  User termination therefore requires a serviced syscall path.

## Shared processing core (`nova/npk/src/processing.c`, `nova/npk/core.py`)

- `struct NpkOperation` contains common lifecycle fields plus M4 memory fields
  and M5 object fields (`nova/npk/src/npk_core.h`).
- Existing operation types are `1..20`: boot/diagnostic/trap `1..3`, memory
  `4..8`, object `9..20`. New syscall operations must start after `20`.
- Lifecycle states/stages/outcomes are unchanged:
  `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE`; `REJECTED` and `FAILED` are
  distinct terminal outcomes.
- Reason codes currently end at `45`; M6 codes must be allocated after that.
- Each operation family has a dedicated receipt base, slot count, and counter.
  Generic `0x90220`/12, trap `0x90B00`/8, memory `0x92050`/128, object
  `0x9A190`/128. Without explicit routing, a new operation type would corrupt
  the preserved M1/M2 generic log, so M6 needs its own receipt base and counter.
- `npk_core_admit()` already refuses to overwrite a full receipt log by
  returning `NPK_ERR_NO_RECEIPT`; that bounded behavior must be preserved.
- `npk_core_sync_receipt()` publishes only after the lifecycle reaches a given
  stage and keeps M4/M5 evidence in receipt reserved words/extensions. No
  syscall result may be copied to the frame until its operation commits.
- Dispatch branches by operation type in `npk_core_advance_as()`; a syscall
  type needs branches for validate, execute, and commit.
- Host mirrors exist in `nova/npk/core.py`, `memory.py`, and `objects.py`.

## Memory facts (`nova/npk/src/memory.c`, `nova/emulator.py`)

- M4 manages a finite software-declared physical arena at `0xC0000` with 64
  region slots and two address spaces. It is not a hardware frame pool or
  RAM-size model.
- PTEs are built with `NPK_PERM_USER -> PTE_USER (0x10)`, read `0x2`, write
  `0x4`, execute `0x8`, valid `0x1`. Page-table writes are flushed with `TLBI`.
- The emulator's real translator checks `PTE_USER` only when the current
  privilege is user. A machine-mode translation success alone is therefore not
  proof that a buffer is user-accessible.
- No guest-callable pointer-validation or byte-copy helper exists. Only
  ownership/query helpers are exported today:
  `npk_memory_region_owned_by`, `npk_memory_address_space_owned_by`, and
  `npk_memory_release_for_object`. Mapping internals and canonical-address
  checks are static in `memory.c`.
- No process/task root or global low-kernel/high-user split exists. M6 can
  validate a range against the active M4 address space and explicit user PTE
  bits, but cannot claim a process model.

## Object-manager facts (`nova/npk/src/objects.c`)

- M5 implements real typed objects, handles, permissions, references,
  namespaces, receipts, and type descriptors. Its operation types are `9..20`.
- Exposed non-static integration points include `npk_find_object()`,
  `npk_object_validate()`, `npk_object_execute()`, `npk_object_commit()`,
  `npk_object_sync_receipt_ext()`, `npk_object_init()`, `npk_object_selftest()`,
  and `npk_object_request()`.
- Object handles encode owner, generation, and index. Validation checks state,
  generation, ownership, share bits, and permission subsets. Commit publishes
  headers, handles, namespace entries, type descriptors, and results.
- The direct typed `npk_object_request()` entry published at `0x96050` is a
  machine-mode observation/test entry. It is not a syscall and existing tests
  explicitly distinguish it. M6 should translate object syscalls into the same
  M5 operations rather than adding a second object model.

## Build, layout, and test plumbing

- `nova/npk/build.py` has a fixed source and link list. A new `syscalls.c`
  translation unit must be added to the source path, object path, compile step,
  link order, and cleanup list. `objects.c` alone uses `--long-branches`; other
  C sources use `--long-calls -c`.
- `nova/npk/layout.py` contains all fixed observation addresses. Free space
  between the M5 receipt extension (`0x9EA10` plus 32 bytes) and guest code at
  `0xA0000` is available for bounded M6 observation regions.
- `nova/npk/src/kernel.c` is strictly a coordinator. Its current order is boot
  → trap → memory → object init → object selftest. Syscall initialization
  belongs after the subsystems it depends on; syscall policy belongs in a new
  subsystem file, not in `kernel.c`.
- `tests/test_npk_exceptions.py` provides the real `SCALL` guest-code harness:
  boot a fresh image, install code at `0xA0000`, set user privilege, step until
  trap dispatch or MRET, and inspect fixed observation regions.
- `scripts/npk_validate.py` currently runs five NPK test files plus Phase 1.
  The new `tests/test_npk_syscalls.py` must be added to both that list and the
  focused pytest command in the completion criteria.

## Implementation implications

1. Keep `kernel.c` coordinator-only and add a dedicated syscall subsystem.
2. Gate on `ecode == 7`, read the syscall number from `frame->ecause`, and
   validate number, argument count, privilege feasibility, pointers, handles,
   and access requirements using M3/M4/M5 state.
3. Admit each syscall as a new typed processing-core operation and drive all
   five stages. Use `REJECTED` for malformed calls and `FAILED` for execution
   or commit errors; never silently drop or overwrite receipts.
4. Add a dedicated syscall receipt log and host decoder with bounded capacity.
5. Translate `SYS_OBJ_CREATE`, `SYS_OBJ_SIZE`, and `SYS_OBJ_DESTROY` into real
   M5 `OP_OBJECT_CREATE`, `OP_OBJECT_QUERY`, and `OP_OBJECT_DESTROY`
   operations. Do not use `nova.syscalls.dispatch_syscall()` object semantics.
6. Validate `SYS_WRITE` buffers against active M4 page-table mappings with
   explicit user/read bits. Report unsupported input as `SYS_READ` remains
   reserved.
7. Publish `a0`/`a1` only at commit by writing the saved frame GPR slots before
   the assembly restore and `MRET`.
8. Keep all M1–M5 addresses, receipts, counters, ABI constants, and documented
   behavior unchanged. Record any limitation rather than implying a scheduler,
   process model, hardware device, or caller privilege bit that does not exist.
