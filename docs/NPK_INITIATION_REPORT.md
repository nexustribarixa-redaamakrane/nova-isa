# NPK initiation report — Phase 2

**Project:** Nova OS  
**Kernel:** NPK — Nova Processed Kernel  
**Kernel classification:** Winoid  
**Baseline:** `0139e5a`  
**Phase status:** Phase 2 AUTHORIZED; first foundation milestone starts after this report.

This is the first Phase 2 deliverable. It records the verified repository
baseline, an audit of what the reference platform actually implements, the NPK
architecture proposal, the CPU policy proposal, and the exact first milestone.
It is not a claim that the kernel already exists.

## 1. Verified Phase 1 repository baseline

Before any Phase 2 change:

- `git fetch origin --prune` completed.
- Local `HEAD` and `origin/master` both resolved to
  `0139e5a3e66e2d4f33b7e4154158cda075b50ba2`.
- `git status -sb` reported `## master...origin/master` with no dirty or
  untracked files.
- `python scripts/phase1_validate.py` was rerun and finished with
  **Phase 1 validation: PASS**.

Validation counts:

| Stage | Result |
| --- | ---: |
| frontend | 180 passed |
| sema | 85 passed |
| codegen | 59 passed |
| driver | 55 passed |
| runtime | 15 passed |
| library | 1 passed |
| end-to-end | 13 passed |
| emulator | 1831 passed |
| example | 1 passed |
| conformance | 18/18 passed |
| compileall | passed |
| full suite | 2245 passed |

The Phase 1 compiler, toolchain, object/executable formats, runtime, and tests
are preserved as the foundation for NPK.

## 2. Existing NovaMachine capabilities

| Capability | Classification | Evidence / limitation |
| --- | --- | --- |
| 64-bit ISA, 32 GPRs, three privilege levels | Implemented and tested | `nova/isa.py`, `tests/test_csr.py`, `tests/test_trap_abi.py` |
| Machine-only instruction and CSR checks | Implemented and tested | `emulator.py:1571-1572`, `tests/test_csr.py` |
| Exception entry/return (`ecode`, `ecause`, `epc`, `MRET`, `IRET`) | Implemented and tested | `emulator.py:1505-1527`, `tests/test_trap_abi.py` |
| `SCALL` and `TRAP` entry semantics | Implemented and tested for `SCALL`; Implemented but incomplete for `TRAP` runtime delivery | `SCALL` is conformance-tested; `TRAP` mostly encoding-tested |
| Automatic GPR trap frame | Not implemented | Entry saves only an internal 4-deep `(epc, ecode, ecause, priv)` frame |
| Supervisor trap delegation, `SRET`, `ERET` | Not implemented | No delegation CSR or return instruction exists |
| CSR file (20 assigned, reserved WARL range) | Implemented and tested; per-CSR instruction coverage incomplete | `isa.py:1134-1324`, `tests/test_csr.py` |
| Software/timer interrupts, `ie`, `ip`, `ivec`, `SIPI`, `WAIT` | Implemented and tested | `tests/test_v1_features.py:247-298` |
| External interrupt source | Requires emulator enhancement for devices; host injection works | `set_interrupt_pending(INT_EXTERNAL)` only |
| Wake-from-halted on interrupt | Not implemented | `halted` is never cleared by interrupt injection |
| Sparse physical memory and 64-bit access checks | Implemented and tested | `emulator.py:845`, `:1035-1068` |
| RAM-size limit / frame allocator / public map API | Not implemented; Requires emulator enhancement | No RAM bound, free list, or `map_page()` exists |
| Four-level paged translation via `ptbr` | Implemented and tested | `emulator.py:1107-1282`, `tests/test_v1_features.py` |
| 4 KiB / 2 MiB / 1 GiB pages and R/W/X/U/A/D PTE bits | Implemented and tested | Paging tests and conformance vectors |
| ASID / tagged TLB / shootdown | Not implemented; Requires emulator enhancement | TLB key includes `ptbr`; every `ptbr` write flushes all |
| TLB instruction `TLBI` | Implemented and tested for full flush; Implemented but incomplete for single-page instruction semantics | `tests/test_v1_features.py` calls `flush_tlb()` directly |
| PTE global bit | Requires emulator enhancement | Documented in README but ignored by translator |
| Nested stage-1 + stage-2 translation | Implemented and tested | `s2ptbr`, `vptbr`, `hstatus`, conformance vectors |
| MMIO/device memory regions | Not implemented; Requires emulator enhancement | No serial, framebuffer, block, input, or interrupt controller |
| Atomic instructions (`AMOADD`, `AMOSWAP`, `AMOCAS`, AND/OR/XOR) | Implemented and tested | `isa.py`, instruction tests |
| Capability object instructions and table | Implemented and tested | `OBJ.*`, `tests/test_syscalls.py`, conformance |
| Syscall table and host dispatcher | Implemented but incomplete | `nova/syscalls.py`; no in-emulator kernel dispatch |
| `.nobj` objects, linker, `.nob` container | Implemented and tested | Phase 1 driver/linker/container tests |
| Physical hardware / RTL | Requires future physical hardware | No HDL, device tree, firmware, or board support exists |

## 3. Missing platform facilities

The audit found no working kernel platform. The following are genuine gaps,
not unimplemented stubs to be advertised:

- **Boot contract.** No firmware, bootloader-to-kernel handoff, kernel header
  beyond `.nob`, device tree, or RAM descriptor. `Requires emulator enhancement`.
- **Kernel syscall hook.** `NovaMachine.step()` never calls
  `dispatch_syscall()`. Host harnesses do it manually. `Requires emulator enhancement`
  for autonomous kernel dispatch, or NPK must own the trap path in kernel code.
- **Privilege routing.** Every trap and interrupt enters machine privilege. A
  supervisor kernel would require a machine-mode shim or a new delegation
  facility. `Requires emulator enhancement` for delegation.
- **Software trap frame.** GPRs, `sp`, `psr`, and `tval` are not saved by entry.
  NPK must implement a software frame. `Not implemented` in the platform by design.
- **Memory.** No RAM-size model, frame allocator, mapping helper, MMIO regions,
  ASID, TLB shootdown, or enforced global PTE bit. `Requires emulator enhancement`.
- **Devices.** Only syscall-based write logging and CSR timer interrupts exist.
  No input source, block device, interrupt controller, or framebuffer.
  `Requires emulator enhancement` / `Requires future physical hardware`.
- **SMP.** `NovaCluster` shares memory and supports IPIs, but has no secondary
  hart boot protocol, cache model, or shootdown. `Requires emulator enhancement`.
- **Userspace.** No process image format, dynamic loader, address-space layout
  convention, or process lifecycle. `.nod` is reserved but unspecified.
  `Not implemented`.

## 4. Available ISA functionality

NPK can build on the following implemented ISA surface:

- 64-bit `ADD`/`SUB`, shifts, rotates, `MUL`, native `DIV`/`UDIV`/`REM`/`UREM`;
- branches, `CALL`/`RET`, `TRAP`, `SCALL`, `MRET`, `IRET`, `FENCE`, `WAIT`,
  `HALT`, `SIPI`, `TLBI`;
- atomic memory operations including `AMOCAS`;
- machine-only privileged instructions and CSR access checks;
- 20 assigned CSRs: `psr`, `epc`, `ecause`, `ecode`, `priv`, `vbar`, `coreid`,
  `ptbr`, `tval`, `ie`, `ip`, `ivec`, `time`, `timecmp`, `vpc`, `vpriv`,
  `vptbr`, `vexit`, `hstatus`, `s2ptbr`;
- 11 exception codes, four-deep internal exception save stack;
- capability-based objects `OBJ.CREATE`, `OBJ.GET`, `OBJ.SET`, `OBJ.DROP`,
  `OBJ.PERM`, `OBJ.SEND`, `OBJ.RECV`;
- full four-level translator with 4 KiB/2 MiB/1 GiB leaves;
- nested virtualization primitives, reserved for a later NPK feature.

No ISA v1.0 change is proposed.

## 5. Privilege and trap behavior

The concrete behavior NPK must respect:

- A fresh `NovaMachine` resets to **user** privilege (`priv = 0`) at
  `0x1000`. `nova-run` defaults to machine privilege because it explicitly
  sets it. The README sentence claiming architectural reset at machine
  privilege is inaccurate and will be corrected in the Phase 2 docs update.
- Machine-only instructions trap with `PRIVILEGE` when executed below machine
  privilege.
- `SCALL imm` raises `SYSTEM_CALL` with `ecause = imm`, `epc = PC + 4`.
- Faults use the faulting PC as `epc`; interrupts use the next PC.
- Entry pushes `(epc, ecode, ecause, previous_priv)` onto a host-side stack,
  sets `ecode`, `ecause`, `epc`, forces machine privilege, and jumps to `vbar`
  or `ivec`.
- `MRET`/`IRET` pop one frame, set `pc = epc`, restore privilege, clear
  `ecode`, and leave `ecause` unchanged.
- No GPR is modified by entry, so the kernel must save whatever it clobbers.
- There is no supervisor delegation and no `SRET`/`ERET`.

NPK therefore starts as a machine-mode kernel.

## 6. Existing virtual-memory implementation

The reference translator is usable and should be reused:

- `ptbr[63]` enables paged mode; `bits[51:12]` are the root PPN; `ptbr = 0` is
  bare identity mode with no permission checks.
- Four levels index VA bits `[47:39]`, `[38:30]`, `[29:21]`, `[20:12]`.
- Leaves at levels 3/2/1 map 4 KiB/2 MiB/1 GiB; level-0 leaves are reserved.
- PTE bits: valid, R, W, X, U, global (documented but ignored), A, D.
- Faults raise `MMU_FAULT`, write `tval`, and use details 0–4.
- TLB is a functional cache keyed by `ptbr`, VPN, access type, and user flag.
  Writing `ptbr` flushes all entries; `TLBI` flushes all or one page.
- Address-space switching is by `ptbr` write only; no ASID exists.

NPK must add the page-table construction and policy layer; it must not rewrite
the emulator translator.

## 7. Existing syscall implementation

`nova/syscalls.py` defines ABI v1.0:

| # | Name | Reference behavior |
| ---: | --- | --- |
| 0 | `SYS_EXIT` | Host dispatcher sets `halted`; exit code argument is discarded |
| 1 | `SYS_WRITE` | Writes recorded bytes to `machine.io_log` for fd 0/1/2 |
| 2 | `SYS_READ` | Always unsupported (`-1`) — no input source |
| 3 | `SYS_TIME` | Returns the `time` CSR |
| 4 | `SYS_OBJ_CREATE` | Uses the capability object table |
| 5 | `SYS_OBJ_SIZE` | Reads object size with read permission |
| 6 | `SYS_OBJ_DESTROY` | Drops a live object |
| 7 | `SYS_YIELD` | Host model sets `halted`; no scheduler exists |

These are host-side reference implementations. The emulator does not dispatch
them. NPK will preserve the numbering and conventions and implement kernel-side
dispatch in Milestone 6.

Known reference-model inaccuracies to avoid inheriting silently: `SYS_WRITE`
reads raw memory rather than translating a virtual buffer; `SYS_EXIT` discards
its status; `SYS_YIELD` is terminal rather than a scheduler call.

## 8. NPK architectural overview

NPK is a modular, machine-mode-first kernel for Nova OS. The repository has no
pre-existing definition of "processed kernel", so Phase 2 introduces one
explicitly in `docs/NPK_ARCHITECTURE.md`: kernel work is transformed through an
admit/validate/execute/commit/complete pipeline by a processing unit and proved
by a receipt. This is a new Phase 2 design decision, not a canon recovered from
Phase 1.

- **Core/Boot/Architecture/Processing:** image validation, boot contract, CSR
  and vector setup, operation records, pipeline stages, processing units,
  receipts, and deterministic initialization.
- **Exceptions:** software trap frame, dispatch, `MRET` return.
- **Memory:** frame allocation, page tables, address spaces, faults, TLB.
- **Syscalls:** ABI-compatible dispatcher with validation.
- **Devices:** console and timer over real platform facilities only.
- **Scheduler:** cooperative tasks first; preemption only after timer proof.
- **Loader:** `.nob` user images and process termination.
- **IPC/Debugging:** reserved and observability helpers respectively.

The authoritative design is `docs/NPK_ARCHITECTURE.md`. No desktop or
OpenWindows component is part of Phase 2.

## 9. CPU compatibility policy proposal

**NPK targets Nova ISA v1.0 exclusively in Phase 2.** This is the intended
Winoid policy for NPK: 64-bit only, Nova-only, no silent multi-architecture
design.

Rationale:

- the toolchain, ABI, CSR model, page format, syscall table, and test suite are
  all Nova-specific;
- no other backend, HAL, firmware interface, or device model exists in the
  repository;
- adding a second architecture would multiply kernel contracts without a
  concrete requirement.

Any future non-Nova Winoid target must be a separate, authorized HAL project.

## 10. Kernel memory layout proposal

Milestone 1 uses bare mode:

| Region | Address | Purpose |
| --- | --- | --- |
| Kernel image | `0x1000` upward | `.nob` code/data |
| Exception vector | link-computed inside image | `vbar` target |
| Boot stack | top `0x80000`, grows down | Initial kernel stack |
| Boot marker | `0x90000` | 8-byte `0x4E504B31` handshake |
| Processing receipt | `0x90008`, 96 bytes | Operation stage trace and terminal result |

Future policy reserves low canonical addresses for kernel mappings and high
canonical addresses for user spaces, with U-bit enforcement and no user access
to kernel pages. Bare mode provides no isolation; this is explicit.

## 11. Boot contract

No new kernel executable format is justified. The `.nob` container already
provides base, entry, flags, size, and an exact image.

Loader responsibilities:

1. validate `.nob`;
2. load bytes at `image_base`;
3. set `pc = entry`;
4. set privilege to machine;
5. transfer control.

Kernel responsibilities:

1. `_npk_start` is the global entry symbol;
2. set `sp` before calling C;
3. install `vbar` from a link-computed vector symbol;
4. write the boot marker;
5. call `npk_main`;
6. `HALT` with the return value in `a0`.

Missing platform facility: no firmware or bootloader exists. The host harness
supplies the machine-privilege handoff. This is documented, not hidden.

## 12. System-call design

NPK will preserve syscall numbers `0..7`, `a0`-`a5` arguments, `a0` return, and
`-1`/`a1` error convention. The kernel handler will validate number, argument
count, user pointers, and permissions before executing an operation. Errors
will be converted to the existing `ERROR_*` values. The host dispatcher remains
a reference test helper only.

## 13. Task-management design

Phase 2 begins with cooperative scheduling:

- TCB: state, stack, saved context, address space;
- states: `NEW`, `READY`, `RUNNING`, `BLOCKED`, `EXITED`;
- explicit ready queue and deterministic `SYS_YIELD`;
- context switch includes the software trap frame and `ptbr`;
- preemptive scheduling is not claimed until the timer path is tested.

## 14. Executable-loader design

The loader consumes `.nob` images, validates header/flags/entry/range,
allocates target memory, maps image bytes, initializes a stack, sets the entry
PC and privilege, and defines termination. It rejects relocations, dynamic
features, unsupported flags, and malformed containers. No new userland, shell,
or graphical application work is part of this subsystem.

## 15. Proposed Phase 2 milestones

| # | Milestone | Gate |
| --- | --- | --- |
| 1 | NPK architecture and boot foundation | Deterministic bootable `.nob` kernel on `NovaMachine` |
| 2 | Exception and trap subsystem | Register-preserving trap frame and `MRET` return |
| 3 | Physical and virtual memory management | Allocation, mapping, permissions, faults, isolation |
| 4 | Syscall and privilege subsystem | In-kernel ABI dispatch with validation |
| 5 | Device abstraction and timer integration | Real console/timer interfaces only |
| 6 | Task management and cooperative scheduling | Task states, yield, deterministic switching |
| 7 | Executable loading and user execution | `.nob` user image runs under NPK |
| 8 | Integrated validation and Phase 2 closeout | Full regression, report, clean synchronized push |

Details are in `docs/PHASE2_PLAN.md`.

## 16. Risks and blockers

- **Boot/platform gap:** no firmware or kernel vector; the host harness must
  set machine privilege and load the image. Mitigation: explicit boot contract.
- **Privilege gap:** all traps enter machine mode; no supervisor delegation.
  Mitigation: machine-mode kernel design.
- **Memory gap:** no RAM model, mapper, ASID, or shootdown. Mitigation: declare
  RAM manually in NPK and use full TLB flushes.
- **Device gap:** no MMIO devices or input. Mitigation: console/timer only.
- **Test-harness gap:** reference syscall dispatch is host-side. Mitigation:
  kernel-owned trap path and ABI tests.
- **Documentation drift:** Phase 1 docs contain a reset-privilege inaccuracy
  and stale "Phase 2 not started" language. Mitigation: correct them in the
  first milestone's documentation update.
- **No physical hardware:** all validation is architectural/functional.
  Mitigation: keep claims bounded to the reference model.

None of these blocks the first foundation milestone.

## 17. Exact first implementation milestone

**Milestone 1 — NPK architecture and boot foundation** is the first milestone.

Scope:

- commit this report, `docs/NPK_ARCHITECTURE.md`, and `docs/PHASE2_PLAN.md`;
- add a small `nova/npk/` package with layout constants, image validation,
  deterministic build and boot helpers;
- add minimal `start.s` and `kernel.c` sources;
- build a `.nob` with `_npk_start` at `image_base = 0x1000`;
- prove boot, marker, stack, vector, return code, determinism, and exception
  rejection with `tests/test_npk.py`;
- add `scripts/npk_validate.py`;
- update packaging and stale Phase 1 status statements;
- keep Phase 1 validation green with unchanged counts.

Out of scope for Milestone 1: trap frame, allocator, syscall dispatcher,
devices, tasks, loader, preemptive scheduling, desktop, OpenWindows, RTL, and
any ISA/ABI change.
