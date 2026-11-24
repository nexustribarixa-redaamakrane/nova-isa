# NPK Milestone 8 report - Task management and cooperative scheduling

**Milestone:** 8 of 10 - Task management and cooperative scheduling.  
**Status:** COMPLETE after validation.  
**Scope:** NPK only. No preemption, SMP, per-task address-space isolation, user
loader, process/thread split, MMIO, M9, or M10 work is included. Nova ISA v1.0,
Nova64 ABI, syscall IDs `0..7`, error codes `1..4`, Phase 1 behavior, and M1–M7
semantics are preserved except where M8 deliberately makes `SYS_YIELD`/
`SYS_EXIT` real cooperative scheduling behavior.

## 1. Summary

Milestone 8 adds honest cooperative task and execution management through the
existing NPK processing core and trap/syscall boundary. Tasks are bounded
kernel-managed records with stable IDs, generations, deterministic ready-queue
membership, explicit lifecycle states, and bounded receipts. Task operations are
typed NPK operations and traverse `ADMIT → VALIDATE → EXECUTE → COMMIT →
COMPLETE`.

`SYS_YIELD` and `SYS_EXIT` remain ABI-compatible syscalls. After a successful
syscall dispatch, the trap dispatcher invokes the cooperative scheduler; rejected
or failed syscalls do not reschedule. The scheduler is honest about Nova
semantics: Nova `MRET` restores EPC/privilege from the emulator hidden exception
stack, so NPK cannot redirect return by editing `frame->epc` alone. M8 patches a
suspended writable return site with a `JMP r15` trampoline, restores the selected
task software frame into the live trap frame, and sets `r15` to the selected
resume PC before `MRET`.

## 2. Task model

- Fixed task region: state `0xB0010`, request `0xB0090`, response `0xB00F0`,
  TCB table `0xB0130` (8 × 576 bytes), ready queue `0xB1D30` (8 × 16 bytes),
  task receipts `0xB1DB0` (32 × 64 bytes).
- TCB fields include stable ID, generation, state, owner, parent, entry, stack
  base/size/top, arg, address-space field, exit status, resume PC, resume patch
  addr/word/active, flags, reserved words, and saved software trap frame.
- States: `FREE`, `CREATED`, `READY`, `RUNNING`, `BLOCKED`, `TERMINATED`.
- Typed operations: `OP_TASK_CREATE`, `OP_TASK_TERMINATE`, `OP_TASK_YIELD`,
  `OP_TASK_BLOCK`, `OP_TASK_WAKE`, `OP_TASK_QUERY`, `OP_TASK_DESTROY`.

## 3. Context-switch and syscall integration

- `SYS_EXIT` and `SYS_YIELD` keep syscall numbers `0` and `7`, argument counts,
  and `a0/a1` return/error ABI.
- Successful `SYS_YIELD` records the yield request and cooperatively reschedules.
- Successful `SYS_EXIT` records exit request, terminates the current captured task
  context, and reschedules or halts if no resumable task remains.
- Only resumable tasks with captured trap context are scheduled by patching return
  sites. Freshly created task records remain READY kernel records; M8 does not
  invent a user start/loader model.
- No runnable resumable task results in an honest halt trampoline patch.

## 4. QEMU translation evidence and fidelity boundary

The boot blocker was an unsupported valid instruction in the generated x86-64
interpreter, not a Nova kernel or ISA defect. The translator error handler now
records the failing Nova PC and word; the observed blocker was `UDIV` at PC
`0x67890`, word `0x339d000`. The translator now covers `UDIV`, `REM`, `UREM`,
`NOT`, `RET`, `MRET`, and `TLBI` in addition to the prior instruction set.
`TLBI` is an explicit no-op barrier in this non-paging shadow interpreter because
there is no software TLB to invalidate.

The MRET audit reached the following conclusion. The reference emulator fully
models Nova exception return: normal and nested returns restore EPC and prior
privilege, misaligned EPC raises `ALIGNMENT_FAULT`, and underflow raises
`ILLEGAL_INSTRUCTION` at the architectural vector. The QEMU translator emits a normal MRET return path against a shadow exception
stack: it checks EPC alignment, requires a non-empty stack, pops
EPC/ecode/ecause/privilege, clears ECODE, and transfers to the saved EPC. The
harness has no exception-entry producer, so this path is translated but not
exercised by boot validation. It does **not** implement exception entry, the
architectural depth-4 overflow case, or Nova trap delivery for MRET underflow and
misaligned EPC; those conditions terminate the harness at the recorded error PC
instead of entering a Nova vector. The generated interpreter is therefore
harness-only evidence for this milestone, not a full ISA conformance model.

## 5. Validation

The final combined working tree was validated with the following commands. All
results below are from that final tree, not from earlier partial states.

| Command | Result |
| --- | --- |
| `python scripts/npk_validate.py` | PASS: `npk` stage 185 passed; phase1 `full_suite` 2430 passed; `NPK validation: PASS` |
| `python scripts/phase1_validate.py` | PASS: `full_suite` 2430 passed; `Phase 1 validation: PASS` |
| `python -m pytest -q` | PASS: 2430 passed |
| `python -m compileall -q nova scripts tests` | PASS (no output) |
| `python -m pytest -q tests/test_documentation.py` | PASS: 17 passed |
| `python -m pytest -q tests/test_npk*.py` | PASS: 185 passed |
| `python -m pytest tests/test_npk_qemu.py -q` | PASS: 7 passed |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu --timeout 120` | PASS |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu-m8 --timeout 120` | PASS |
| `cmake -S . -B build-cmake-mingw -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release` | PASS |
| `cmake --build build-cmake-mingw -j 4` | PASS; `npk.nob` built |
| `ctest --test-dir build-cmake-mingw --output-on-failure -j 4` | Exit 0, but CTest discovered `No tests were found!!!`; no tests were executed by CTest |
| `git diff --check` | No whitespace errors; CRLF normalization warnings only |

CTest exit status is not reported as test coverage: in this configuration it
discovers no tests. NPK correctness evidence comes from the passing pytest,
NPK validation, phase1 validation, and QEMU boot commands above.

## 6. Limitations

- Cooperative scheduling only; no preemption, timer preemption, SMP, or
  multiprocessing claims.
- Single shared address space; no per-task/process isolation or user loader.
- Context switching depends on writable return sites and single-level trap-frame
  assumptions; it is not a hardware context switch.
- Freshly created tasks are not auto-started user contexts.
- M6 caller-privilege enforcement remains limited by trap-frame visibility.
- The QEMU path executes generated x86-64 code that interprets Nova bytecode; it
  is not native Nova CPU support in QEMU and is not hardware validation.
- The QEMU MRET model covers the normal return path only. Exception entry,
  exception-stack overflow, and trap delivery for underflow/misaligned EPC are
  not modeled in the generated interpreter.
- No MMIO, M9 executable loading, or M10 closeout claims in this milestone.
