# NPK Milestone 9 report - Executable loading and user execution

**Milestone:** 9 of 10 - Executable loading and user execution.  
**Status:** COMPLETE after validation.  
**Scope:** NPK only. The Nova ISA v1.0, Nova64 ABI, syscall IDs `0..7`, error
codes `1..4`, Phase 1 behavior, and M1–M8 semantics are preserved. No M10 or
Phase 3 work is included.

## 1. Summary

Milestone 9 adds typed executable processing to the real NPK processed-kernel
lifecycle. Four operations - `OP_EXEC_LOAD`, `OP_EXEC_START`, `OP_EXEC_QUERY`,
and `OP_EXEC_RELEASE` - traverse `ADMIT → VALIDATE → EXECUTE → COMMIT →
COMPLETE` through the existing processing core and publish deterministic receipts
in a dedicated bounded table. There is no direct helper-call replacement loader,
second object manager, ISA/ABI change, or new syscall number.

The supported executable container is the repository's implemented native
format: flat `.nob` v1. M9 loads it through an M5 memory-region handle into an M4
physical page, binds an M8 task, performs a real `VMLAUNCH` into user privilege,
observes the guest's architectural `SCALL` VM exit, terminates the task, and
commits the exit status. No Nova ELF compatibility is invented.

## 2. Container and request contract

A `.nob` v1 image has `NOBV` magic at offset 0, `u16` version at 4, `u16` flags
at 6, `image_base` at 8, `entry` at 16, `image_size` at 24, and image bytes at
32. Its exact length is `32 + image_size`. M9 accepts only version 1, flags 0,
a canonical page-aligned base, a 4-byte-aligned entry inside the image range, a
nonzero image no larger than one page, and an exact source-length match.

The 96-byte request block carries magic, operation, operation ID, image ID,
owner, handle, source address/size, task, ASID, argument, and flags. The 64-byte
response block carries magic, status, operation, reason, result, image ID,
receipt index, and slot. Invalid flags and unsupported operations are rejected
before admission or validation; no meaning is guessed for reserved flags.

## 3. Layout and lifecycle

- Entry pointer `0xB3000`; state `0xB3010` (128 bytes); request `0xB3090`
  (96 bytes); response `0xB30F0` (64 bytes).
- Eight 144-byte image records at `0xB3130`; sixteen 128-byte receipts at
  `0xB4000`; 128-byte launch context at `0xB4800`; 64-byte host register save
  at `0xB4880`.
- Staged source window `0xB5000` with 0xB000-byte capacity, ending at the M4
  physical arena base `0xC0000`. `BOOT_STACK_GUARD` was reduced to `0x8000` so
  the enlarged kernel image still fits the existing boot contract.
- Image states: `FREE`, `LOADED`, `RUNNING`, `EXITED`, `FAILED`.
- Reason codes `94..115` distinguish bad requests, containers, sources, image
  fields, handles, objects, owners, permissions, tasks, states, unsupported
  behavior, exhaustion, mappings, contexts, launch failures, traps, commit
  failures, and stale IDs.
- Successful receipts show the full five-stage trace. Validation rejections stop
  at `VALIDATE`; failure receipts stop at `EXECUTE` or `COMMIT`. Admission
  failures publish no receipt and are visible in state/response.

## 4. Integration with M4, M5, M8, and traps

`LOAD` resolves the typed M5 handle through read-only object services, checks
owner and `OBJ_PERM_EXEC`, resolves the M4 region through read-only memory
services, and copies image bytes into the physical page. It rejects a region
that is already mapped instead of overwriting live translations.

`START` checks the image record, owner, matching handle/region, M8 task
ownership and bindability, and M4 user mappings for code and stack. The code page
requires `PERM_READ | PERM_EXEC | PERM_USER`; the stack page requires
`PERM_READ | PERM_WRITE | PERM_USER`. `npk_task_bind_execution()` sets the TCB
entry, stack, argument, ASID, `RUNNING` state, and current-task field.

`exec.s` saves host callee-saved registers, programs guest `vpc`/`vpriv`/`vptbr`,
loads a deterministic guest register file, runs `VMLAUNCH`, records guest `r1`,
`VEXIT`, `ecause`, host privilege, host `PTBR`, and status in the launch context,
and returns the kernel reason-code scalar. The guest `SCALL` exit code is `7`.
The kernel terminates the M8 task with `npk_task_finish_execution()` and commits
the image as `EXITED` with the guest exit code. Non-`SCALL` exits or failed host
resume produce `LAUNCH_FAILED`/`TRAP` and a `FAILED` image. No fabricated `MRET`
frame is used.

The preserved syscall table remains `SYS_EXIT=0`, `SYS_WRITE=1`, `SYS_READ=2`,
`SYS_TIME=3`, `SYS_OBJ_CREATE=4`, `SYS_OBJ_SIZE=5`, `SYS_OBJ_DESTROY=6`, and
`SYS_YIELD=7`. The M6 caller-privilege limitation is unchanged.

## 5. QEMU and emulator boundary

The reference emulator executes the new code through real Nova `VMLAUNCH` and
`SCALL` instructions, and tests assert guest exit code, exit cause, `VEXIT`
evidence, restored host privilege/PTBR, task termination, and receipt traces.
The generated QEMU interpreter remains a harness-only x86-64 translator of Nova
bytecode. Its normal boot path has no exception-frame producer and does not
implement `SCALL`, `TRAP`, `IRET`, or VM instructions. M9 user execution is
therefore not claimed on QEMU; QEMU boot still validates the unchanged boot
marker, reference step count, and 64 KiB shadow-memory comparison, with failing
Nova PC/word diagnostics preserved. The larger M9 image also exposed a harness
layout collision: the indirect-jump dispatch table had grown into the step/done/
error metadata at `0x8F0000`. Metadata was moved to `0xB00000`, above the maximum
table extent, and a layout assertion now prevents recurrence.

## 6. Validation

The final combined working tree was validated with the following commands. All
results below are from that final tree, not from earlier partial states.

| Command | Result |
| --- | --- |
| `python scripts/npk_validate.py` | PASS: NPK stage 195 passed; phase1 `full_suite` 2440 passed |
| `python scripts/phase1_validate.py` | PASS: `full_suite` 2440 passed |
| `python -m pytest -q` | PASS: 2440 passed |
| `python -m compileall -q nova scripts tests` | PASS (no output) |
| `python -m pytest -q tests/test_documentation.py` | PASS: 17 passed |
| `python -m pytest -q tests/test_npk*.py` | PASS: 195 passed |
| `python -m pytest -q tests/test_npk_qemu.py` | PASS: 7 passed |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu --timeout 120` | PASS: exact step count and shadow memory |
| `python scripts/npk_qemu_boot.py --build-dir build-qemu-m8 --timeout 120` | PASS: exact step count and shadow memory |
| `cmake -S . -B build-cmake-mingw -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release` | PASS |
| `cmake --build build-cmake-mingw -j 4` | PASS: `npk.nob` rebuilt |
| `ctest --test-dir build-cmake-mingw --output-on-failure -j 4` | Exit 0 with `No tests were found!!!`; not reported as test coverage |
| `git diff --check` | PASS (no whitespace errors; line-ending normalization warnings only) |

CTest exit status is not reported as test coverage when it discovers no tests.
QEMU boot evidence is generated interpreter/harness evidence, not native Nova
CPU or physical-hardware validation.

## 7. Limitations

- Native `.nob` v1 only: no ELF, dynamic loader, relocation processing, shared
  libraries, `.nod`, `.nop`, or userland shell.
- One image page, one stack page, up to eight concurrent image records, and
  sixteen dedicated executable receipts.
- No per-process isolation beyond the selected M4 address space; no preemptive
  scheduling, SMP, MMIO, or process/thread split.
- `VMLAUNCH` execution is validated in the reference emulator, not by the QEMU
  boot harness or hardware.
- M6 caller-privilege enforcement remains limited by trap-frame visibility.
- No M10 closeout claims are made in this milestone.
