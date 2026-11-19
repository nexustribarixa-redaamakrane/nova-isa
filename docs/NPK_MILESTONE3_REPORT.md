# NPK Milestone 3 report — Exception and trap subsystem

**Status:** COMPLETE.  
**Kernel:** NPK — Nova Processed Kernel, Winoid, Nova64-only.  
**Date:** 2026-11-19.  
**Milestone 3 commit sequence:** `930c9fa` roadmap/Object Manager planning,
`6e8e3af` implementation, `d6d5452` tests and validation, final documentation
commit in this change.

## 1. Objective and result

Milestone 3 replaces the boot-only placeholder exception vector with a real
Nova assembly trap path. The kernel now saves a fixed 304-byte software trap
frame, processes every trapped event as a typed NPK operation, classifies the
event, publishes an independent receipt, restores the interrupted context, and
returns with `MRET`.

Milestone 1 and Milestone 2 behavior is preserved exactly: the boot receipt at
`0x90008` is still 96 bytes and byte-compatible, and the processing core still
finishes with `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
`selftest_status=0`, and both processing units idle.

## 2. Implementation

- `nova/npk/src/trap.s` is written in **Nova assembly**, not generic assembly.
  It saves all 32 GPRs and the exception-state words, uses the low scratch slots
  at `0x200`, switches to a separate trap stack at `0x88000`, calls the C
  dispatcher, restores the context, and executes `MRET`.
- `nova/npk/src/trap.c` initializes the trap observations, classifies `ecode`,
  rejects invalid frames/codes, fails unsupported assigned classes, and submits
  an `NPK_OP_TRAP_PROCESS` operation through the existing core.
- `nova/npk/src/processing.c` owns the trap operation lifecycle and routes
  trap receipts to their own eight-slot log at `0x90B00`. No result is published
  before `COMMIT`.
- `nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/traps.py`, and
  `nova/npk/__init__.py` define and expose the fixed M3 contract and host
  decoders.
- `nova/npk/build.py` assembles and links the Nova assembly trap entry and C
  dispatcher using the existing toolchain. No new language, file format, ISA
  constant, or relocation kind was introduced.

The frame at `0x90900` is 16-byte aligned and exactly 304 bytes. GPRs occupy
offsets `0..255`; `psr` is at `256`, `epc` at `264`, `ecause` at `272`, `ecode`
at `280`, the **handler** privilege at `288`, and `tval` at `296`. The pre-trap
privilege is not exposed to guest software; it remains only in the reference
machine's internal exception save stack and is restored by `MRET`.

Classification covers `SYSTEM_CALL`, `SOFTWARE_TRAP`, illegal instruction,
alignment fault, memory fault, and MMU fault. `SYSTEM_CALL` is classified for
the planned Milestone 6 dispatcher; no syscall is serviced and no object-manager
semantics were added. Syscall numbers `4`, `5`, and `6` remain reserved for
`SYS_OBJ_CREATE`, `SYS_OBJ_SIZE`, and `SYS_OBJ_DESTROY`.

## 3. Tests

`tests/test_npk_exceptions.py` contains 21 tests. They boot the real deterministic
kernel image and use real Nova instructions for system calls, software traps,
illegal instructions, alignment faults, and memory faults. MMU faults and
assigned-but-unsupported classes use explicit host injection where the reference
model cannot provoke them deterministically from guest code; entry, frame
capture, dispatch, receipts, return, and `MRET` still run through the real
kernel vector.

Coverage includes exact frame size and offsets, non-overlapping regions, real
vector installation, boot trap-state initialization, all 32 GPRs, `psr`, `tval`,
`epc`, `ecode`, `ecause`, privilege, exact PC restoration, platform save-stack
depth four with oldest-frame discard, invalid-code rejection, malformed-frame
rejection, unsupported-class failure, corrupt-state handling, independent trap
receipt validation, M1/M2 evidence preservation, reserved syscall numbers, and
documentation statements for frame size, `MRET`, and nesting limitations.

## 4. Validation

All required commands were run from the repository root:

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_npk.py tests/test_npk_core.py tests/test_npk_exceptions.py` | 71 passed |
| `python scripts/npk_validate.py` | PASS; NPK 71 passed; Phase 1 PASS |
| `python scripts/phase1_validate.py` | PASS; frontend 180, sema 85, codegen 59, driver 55, runtime 15, library 1, end-to-end 13, emulator 1831, example 1, conformance 18/18, compileall passed, full suite 2316 |
| `python -m pytest -q tests/test_documentation.py` | 17 passed |
| `python -m pytest -q` | 2316 passed |

## 5. Limitations

The trap frame is single-level; nested exception handling is not supported. The
platform's four-entry internal save stack discards the oldest frame on overflow
and remains authoritative for `MRET`. There is no supervisor trap delegation,
`SRET`, `ERET`, hardware GPR trap frame, transactional rollback, kernel syscall
dispatcher, object manager, allocator, scheduler, device driver, user loader,
SMP, preemption, or physical-hardware validation. Commit applies to the modeled
operation and receipt state only.

## 6. Completion conditions

Implementation, 21 tests, documentation, five validation commands, and three
scoped implementation/test/documentation commits are complete. The final push
must leave `master` clean and equal to `origin/master`.
