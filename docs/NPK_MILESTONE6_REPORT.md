# NPK Milestone 6 report — Syscall and privilege-context processing

**Milestone:** 6 of 10 — Syscall and privilege subsystem.  
**Status:** COMPLETE after validation.  
**Baseline:** `7859f8d9540afc04b0478f2d4af691a6cc39b71b`; `master` and
`origin/master` were clean and synchronized before M6 work. M5 commits
`55716ad`, `84e9b4a`, and `7859f8d` are present. Milestones 1–5, Nova ISA v1.0,
the Nova64 ABI, and Phase 1 are preserved.  
**Scope:** NPK only. No Phase 3, OpenWindows/NovaOW/Oryn, desktop, RTL, compiler
redesign, device/timer, task/scheduler, process, or executable-loader work is
included.

## 1. Summary

Milestone 6 exposes selected Milestone 5 Object Manager services through the
frozen Nova syscall ABI. Calls do not bypass the kernel: each real `SCALL` enters
the existing Milestone 3 trap vector, is classified by the C trap dispatcher,
and is handed to a dedicated syscall subsystem. That subsystem admits a new typed
operation into the shared processing core and drives the unchanged
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE` lifecycle.

The implementation does not reimplement object management, user memory, or the
host reference dispatcher. Object syscalls build existing M5 request blocks and
observe real child receipts. `SYS_WRITE` validates buffers through M4 page-table
state. Results are published into the saved trap frame before `MRET`, using the
existing GPR restore path.

## 2. Frozen ABI contract

No ISA, compiler, opcode, register, or syscall-table entry changed.

| Property | Preserved contract |
| --- | --- |
| Syscall numbers | `0 SYS_EXIT`, `1 SYS_WRITE`, `2 SYS_READ`, `3 SYS_TIME`, `4 SYS_OBJ_CREATE`, `5 SYS_OBJ_SIZE`, `6 SYS_OBJ_DESTROY`, `7 SYS_YIELD` |
| Call instruction | `SCALL imm`; `imm` is available as `ecause` |
| Exception class | `SYSTEM_CALL`, `ecode = 7` |
| Arguments | `a0..a5`, saved as frame `gpr[1]..gpr[6]` |
| Success | Result in `a0` (`gpr[1]`), zero error in `a1` (`gpr[2]`) |
| Failure | All-ones unsigned 64-bit `-1` in `a0`, error in `a1` |
| Errors | `1 BAD_ARGUMENT`, `2 UNSUPPORTED`, `3 NOT_FOUND`, `4 NO_RESOURCE` |
| Return path | `SCALL` resumes at `PC + 4` after `MRET` |

`nova.syscalls.dispatch_syscall()` remains a Phase 1 host/reference helper. It is
not used by the NPK kernel syscall path and was not modified.

## 3. Processing design

`nova/npk/src/syscalls.c` owns syscall policy. `kernel.c` only calls
`npk_syscall_init()` after trap, memory, and object initialization.
`trap.c` publishes the trap classification before M3 admission, records the
normal M3 `NPK_OP_TRAP_PROCESS` operation, and calls
`npk_syscall_dispatch(frame)` for a validated `SYSTEM_CALL`. If the M3 trap
receipt log is already full, the call is still handed off so the syscall service
can publish its own bounded `NO_RESOURCE` result instead of the trap being
silently swallowed. A trap operation that rejects or fails the frame is not
handed off.

Every syscall becomes operation type `21` with an id based on the dispatch
count. The shared core owns stage transitions, outcomes, receipts, and
publication. Reasons `46..56` distinguish bad frame, bad state, bad number, bad
argument, bad privilege context, bad pointer, unsupported operation, absent
resource, missing object, commit failure, and child-operation failure.
`REJECTED` and `FAILED` remain distinct; no result is published before commit or
a terminal failure path.

A dedicated eight-slot receipt log at `0x9FA90` prevents syscall activity from
overwriting the generic M1/M2, M3, M4, or M5 logs. The core's existing bounded
admission policy returns `NO_RECEIPT` when the syscall log is full; the syscall
state then reports no receipt and an appropriate resource error.

## 4. Validation and policy implementation

Validation checks:

- the saved frame address, `ecode == 7`, and aligned `epc`;
- syscall state magic, version, and initialization status;
- machine-mode handler context;
- syscall number and per-number argument count;
- `SYS_WRITE` descriptor, maximum byte count, capacity, and overflow-safe range;
- canonical user addresses and explicit `U`/read PTE bits through M4;
- object handle decode, fixed owner, and existence before object operations;
- M5 child request status, reason, and receipt indexes.

`SYS_TIME` uses a two-instruction assembly leaf. It reads `time` into `r7`
because `nova-cc` returns C values in `r7`, then `npk_syscall_execute()` stores
that value in the operation. The syscall layer separately publishes the
user-visible `a0` result into `gpr[1]`.

## 5. Syscall behavior

| Syscall | NPK behavior |
| --- | --- |
| `SYS_EXIT` | Records `FLAG_EXIT_REQUESTED`; does not halt or terminate a process |
| `SYS_WRITE` | Validates a bounded user buffer, stages bytes in scratch, commits to a 256-byte log, returns byte count |
| `SYS_READ` | Rejected at validation as `ERR_UNSUPPORTED`; no input device exists |
| `SYS_TIME` | Returns the live `time` CSR value |
| `SYS_OBJ_CREATE` | Uses real M5 `OP_OBJECT_CREATE` and returns a handle |
| `SYS_OBJ_SIZE` | Uses real M5 `OP_OBJECT_QUERY` and returns the payload word |
| `SYS_OBJ_DESTROY` | Uses real M5 `OP_OBJECT_CLOSE` and `OP_OBJECT_DESTROY`, publishing both child receipt indexes |
| `SYS_YIELD` | Records `FLAG_YIELD_REQUESTED`; no scheduler or context switch exists |

Object syscalls use one fixed kernel-owned observation owner. This is a testable
policy boundary, not a process or caller identity. There is no permission token,
ACL, or user-supplied owner.

## 6. Memory and receipt publication

M6 adds fixed observation regions that do not overlap M5 evidence or guest code:

| Region | Address | Size |
| --- | --- | ---: |
| Syscall state | `0x9FA10` | 128 bytes |
| Syscall receipts | `0x9FA90` | 8 × 128 bytes |
| Write log | `0x9FE90` | 256 bytes |
| Scratch | `0x9FF90` | 64 bytes |

A rejected `SYS_WRITE` never copies into the write log. Bytes are first copied
from translated user memory into scratch during execution, then moved to the log
only after the commit guard succeeds. Scratch is cleared on commit so committed
payload is not retained in the uncommitted staging region. Capacity is checked in both stages.

On commit, `op->result` is written to saved `gpr[1]` and `gpr[2]` is set to zero.
On rejection or failure, `gpr[1]` receives all ones and `gpr[2]` receives the
mapped ABI error. The assembly trap entry then restores all GPRs before `MRET`,
so the live user registers and the saved frame agree.

## 7. Preservation of M1–M5 evidence

The M1 boot receipt, M2 counters, M3 trap classification and frame behavior, M4
memory receipts and direct request behavior, and M5 object receipts and direct
request behavior remain unchanged. Tests that previously used `SCALL` merely as
an arbitrary trap now use `TRAP` to preserve their original intent, while new
tests assert the intentional syscall result publication.

The stale M5 statement that object syscalls were inert was documentation only;
the reserved numbers were never consumed by M1–M5. M6 gives them their defined
kernel behavior without changing their numeric assignments.

## 8. Validation evidence

All required commands were run successfully after the implementation and test
changes:

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_npk.py tests/test_npk_core.py tests/test_npk_exceptions.py tests/test_npk_memory.py tests/test_npk_objects.py tests/test_npk_syscalls.py` | **138 passed** (100 M1–M4 plus 23 M5 plus 15 M6) |
| `python scripts/npk_validate.py` | **PASS** (NPK 138 passed; Phase 1 PASS) |
| `python scripts/phase1_validate.py` | **PASS**, Phase 1 counts unchanged |
| `python -m pytest -q tests/test_documentation.py` | **17 passed** |
| `python -m pytest -q` | **2383 passed** (2345 baseline plus 23 M5 plus 15 M6) |
| `python -m compileall -q nova scripts tests` | **passed** |

Phase 1 counts inside validation were unchanged: frontend 180, sema 85,
codegen 59, driver 55, runtime 15, library 1, end-to-end 13, emulator 1831,
example 1, conformance 18/18, compileall passed, and full suite 2383. Both
validator aggregates were `NPK validation: PASS` and `Phase 1 validation: PASS`.

## 9. Implemented versus planned

| Area | M6 status |
| --- | --- |
| Frozen numbers `0..7`, arguments, return convention, errors | Implemented |
| Real `SCALL` trap path and frame publication | Implemented |
| Typed syscall operations on the shared processing core | Implemented |
| Dedicated bounded receipts and stable error mapping | Implemented |
| User pointer validation through M4 | Implemented |
| Object syscalls through real M5 operations and child receipts | Implemented |
| Bounded write sink and staged commit | Implemented |
| Caller privilege enforcement | Not possible from the exposed frame |
| Scheduler, tasks, process ownership, `SYS_EXIT`/`SYS_YIELD` effects | Not implemented; policy recorded only |
| Devices, timers, input, loader, hosted libc | Not implemented |

## 10. Explicit non-claims and limitations

- Nova v1.0 does not expose the pre-trap caller privilege in the software trap
  frame. `frame->handler_priv` is always machine mode, so M6 validates handler
  context but does **not** enforce caller privilege.
- There is no process, task, scheduler, context switch, or trusted call gate.
  `SYS_EXIT` and `SYS_YIELD` are observations only.
- `SYS_READ` remains unsupported because no input device exists.
- User pointers are checked against explicit user/read PTE bits in the active M4
  address space. There is no global low-kernel/high-user split or per-process
  address space.
- M6 uses a fixed kernel-owned object owner; it does not establish caller
  ownership.
- There is no hosted libc, dynamic linking, `FILE` stream, user executable,
  device object, timer, or desktop integration.
- Commit is not transactional. No rollback, isolation, or durability is claimed.
- Nested software trap frames remain unsupported.
- CMake and QEMU integration was added after M6 as kernel-workflow tooling. It
  does not change the ISA, ABI, syscall numbers, error codes, or M1–M6 behavior.
- This is not RTL, firmware, board support, physical-hardware validation, Phase
  3, or OpenWindows/NovaOW/Oryn work.

## 11. Files

Implementation: `nova/npk/src/syscalls.c`, `nova/npk/src/syscalls.s`,
`nova/npk/src/trap.c`, `nova/npk/src/kernel.c`,
`nova/npk/src/processing.c`, `nova/npk/src/npk_core.h`,
`nova/npk/src/memory.c`, `nova/npk/src/objects.c`, `nova/npk/build.py`,
`nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/syscalls.py`,
`nova/npk/__init__.py`.

Tests and tooling: `tests/test_npk_syscalls.py`, `scripts/npk_validate.py`,
`tests/test_npk_exceptions.py`, `tests/test_npk_memory.py`.

Documentation: `docs/NPK_MILESTONE6_RECONNAISSANCE.md`,
`docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`, this report.

## 12. Post-M6 kernel workflow

The root `CMakeLists.txt` orchestrates the existing Nova toolchain through
`scripts/npk_cmake_build.py` and `nova.npk.build_kernel()`. It adds `npk_kernel`,
`npk_validate`, and `npk_qemu_boot` targets without introducing a host C build
of kernel sources.

`scripts/npk_qemu_boot.py` and `nova/npk/qemu_translator.py` provide an honest
QEMU workaround. QEMU has no Nova target; the tool generates an x86-64 multiboot
ELF that interprets the real `.nob` bytecode. The image is loaded into a
1 MiB guest-physical shadow and Nova addresses are offset into it, while the
interpreter itself is placed above the shadow. The validator compares the boot
marker, exact step count, and 64 KiB of guest memory against the Python
reference boot. The M6 image used `246745` steps; the current M7 image uses
`254492`.

This tooling validates the same processed M1–M7 kernel as `boot_kernel()`; it is
not native Nova execution in QEMU, firmware, board support, RTL, physical
hardware validation, or an ISA change.
