# NPK Milestone 4 report — physical and virtual memory processing

**Milestone:** 4 of 10 — Physical and virtual memory management.  
**Status:** COMPLETE.  
**Baseline:** Milestones 1–3 preserved. Nova ISA v1.0 and the Nova64 ABI remain
frozen.  
**Scope:** NPK only. No Phase 3, OpenWindows/NovaOW/Oryn, desktop, RTL,
compiler redesign, syscall service, Object Manager, device, timer, scheduler, or
executable loader work is included.

## 1. Summary

Milestone 4 adds an NPK-managed memory subsystem built on the existing processed
kernel pipeline. Memory work is not a bolted-on allocator call: each supported
operation is admitted as a typed core operation, validated, executed, committed,
completed, and receipted through the shared five-stage state machine.

`NovaMachine` already implements sparse byte memory and a real four-level
translator, but it has no RAM-size model, frame allocator, address-space object,
or `map_page()` API. NPK therefore declares a finite physical arena and
implements allocation, ownership, page-table construction, mapping state, and
receipt evidence in kernel code. This is a software subsystem inside the
reference machine, not a claim of physical hardware support.

## 2. Capability audit

The implementation deliberately distinguishes what exists from what is only
architecturally specified or planned.

| Classification | What it means | Milestone 4 treatment |
| --- | --- | --- |
| Implemented `NovaMachine` functionality | Sparse 64-bit memory and the four-level `ptbr` translator with R/W/X/U/A/D PTEs, `tval`, `MMU_FAULT` details, `TLBI`, and `ptbr`-write flushes | Reused unchanged. NPK builds valid page tables and lets the emulator enforce them. |
| Architecturally specified ISA functionality | Nova v1.0 instruction encodings, CSRs, exception codes, page sizes, and syscall numbers | Preserved byte-for-byte. No instruction, CSR, exception code, page format, or syscall number was added or changed. |
| NPK M4 functionality | Declared arena, allocator, region identity, owner-scoped address spaces, typed memory operations, PTE construction, full `TLBI` wrappers, dedicated receipts | Implemented in `memory.c`, `memory.s`, `processing.c`, `start.s`, and `memory.py`. |
| Future / not implemented | RAM discovery, MMIO, ASID, shootdown, global-bit policy, demand paging, huge-page construction, Object Manager, syscalls, devices, tasks, loader, physical hardware | Explicitly excluded and documented; no capability was invented. |

## 3. Layout and state model

All observation regions are fixed and disjoint. The dedicated memory receipt log
is necessary because the 12-slot Milestone 2 log is already full after boot.

| Region | Address | Size | Purpose |
| --- | --- | --- | --- |
| Memory entry | `0x90F00` | 8 bytes | Published address of `npk_memory_request` |
| Memory state | `0x90F10` | 128 bytes | Magic/version, arena, counters, epochs, ids, last status/reason |
| Region table | `0x90F90` | 64 × 64 bytes | State, id, base, size, owner, type, flags, live-map count |
| Address spaces | `0x91F90` | 2 × 32 bytes | State, owner, root PPN, mapping count |
| Memory request | `0x91FD0` | 64 bytes | Typed request block |
| Memory response | `0x92010` | 64 bytes | Status, reason, result, receipt index, slot |
| Memory receipt log | `0x92050` | 128 × 128 bytes | Independent receipts for memory operations |
| Declared arena | `0xC0000` | `0x40000` bytes | NPK policy arena over sparse memory |

Memory state uses magic `0x4E504B4D` and version `4`. Requests use magic
`0x4E504B51`; responses use `0x4E504B5A`. Page-table pages are ordinary arena
regions with `PAGE_TABLE` state, so they share the same base/size/identity rules
as data regions and cannot be confused with host allocator objects.

## 4. Typed operations and lifecycle

| Type | Operation | Commit-time effect |
| ---: | --- | --- |
| `4` | `NPK_OP_MEMORY_ALLOCATE` | Reserve aligned pages, publish region id/base/size/owner, write allocation tag |
| `5` | `NPK_OP_MEMORY_RELEASE` | Clear region identity and reclaim the base when no mappings are live |
| `6` | `NPK_OP_MEMORY_MAP` | Allocate page-table pages as needed and install a 4 KiB leaf PTE |
| `7` | `NPK_OP_MEMORY_UNMAP` | Clear a leaf PTE, decrement live-map counts, increment invalidation epoch |
| `8` | `NPK_OP_MEMORY_QUERY` | Publish the region base without changing region or arena state |

Every accepted request traverses
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE`. Validation never mutates
region, address-space, page-table, or arena state. Execution computes a
provisional base or PTE address. Commit publishes the result and updates the
observable state.

Reason codes `18..29` distinguish bad request, region, size, alignment,
overlap, exhaustion, owner, state, mapping, unsupported operation, address
space, and PTE conflict conditions. Bad input is `REJECTED`; exhaustion and PTE
conflicts discovered after validation are `FAILED`. The memory response status
separates `BAD_REQUEST`, `BAD_OP`, `ADMIT_FAIL`, `ADVANCE_FAIL`, `BAD_OUTCOME`,
`REJECTED`, and `FAILED` from `OK`.

Boot behavior is unchanged: startup publishes the memory entry address but does
not admit a memory operation. The M1 boot receipt, the M2 counters and 12
receipts, and the M3 trap state remain intact. Memory receipts carry the
operation size and evidence word in their reserved fields so tests can tie a
receipt to concrete state; M1–M3 receipt reserved words remain zero.

## 5. Virtual memory behavior

NPK constructs four-level page tables using the existing Nova PTE format and
installs 4 KiB leaves with translated R/W/X/U bits. Two software address spaces
have independent roots and owner fields. Map and unmap call the real `TLBI r0`
full-flush helper; `ptbr` writes continue to flush translations through the
platform.

Tests verify:

- actual root/intermediate/leaf PTE addresses and leaf PPN/permission bits;
- paged guest instruction fetch, load, and store through a mapped page;
- A/D bit updates performed by the real translator;
- user permission enforcement, `MMU_FAULT`, detail `PERMISSION`, and `tval`;
- missing-translation faults after unmap and full TLB invalidation;
- independent page tables for two address spaces;
- rejection of noncanonical/unaligned addresses, bad sizes, and invalid
  permissions;
- commit-time PTE conflict without replacing an existing leaf.

## 6. Ownership and lifetime

A region has a stable id for its live lifetime. Releasing a region zeros its
descriptor and invalidates the id; a second release or stale query is rejected
with `BAD_REGION`. Wrong-owner query, release, map, and unmap requests are
rejected. A region with live mappings cannot be released; unmapping decrements
the live-map count, and reuse gets a new region id. Query is read-only with
respect to the region table, arena, page tables, and raw allocation tag.

## 7. Validation evidence

All required commands were run successfully:

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_npk.py tests/test_npk_core.py tests/test_npk_exceptions.py tests/test_npk_memory.py` | **100 passed** (71 M1–M3 plus 29 M4) |
| `python scripts/npk_validate.py` | **PASS** |
| `python scripts/phase1_validate.py` | **PASS**, Phase 1 counts unchanged |
| `python -m pytest -q tests/test_documentation.py` | **17 passed** |
| `python -m pytest -q` | **2345 passed** (2316 baseline plus 29 M4) |
| `python -m compileall -q nova tests scripts` | **passed** |

Phase 1 counts remained frontend 180, sema 85, codegen 59, driver 55, runtime
15, library 1, end-to-end 13, emulator 1831, example 1, and conformance 18/18.

Preserved M1 evidence: boot receipt magic `NPKP`, input `0x4E504B31`, output
`0x4E504B32`, status `1`, trace `(1, 2, 3, 4, 5)`. Preserved M2 evidence:
`admitted=10`, `completed=7`, `rejected=3`, `failed=2`, `selftest_status=0`,
both units idle, and all 12 main receipt reserved words zero. A real trap still
dispatches and completes after memory initialization.

## 8. Explicit non-claims and limitations

- The arena is a software policy region over sparse memory, not discovered RAM,
  a hardware frame pool, or MMIO.
- Only 4 KiB leaves are constructed. The platform's 2 MiB and 1 GiB leaves are
  not used by NPK in this milestone.
- No ASID, tagged TLB, shootdown, global-bit policy, demand paging, page
  replacement, copy-on-write, or protection-key feature is implemented.
- Commit is not transactional. There is no rollback, journal, isolation, or
  durability guarantee; a commit-time failure may leave earlier page-table
  allocations visible.
- Processing units remain sequential. No SMP, hart, preemption, or interrupt-
  driven memory producer is claimed.
- No syscall, Object Manager, device, timer, scheduler, task, user process, or
  executable loader behavior is implemented. Reserved syscall numbers
  `SYS_OBJ_CREATE = 4`, `SYS_OBJ_SIZE = 5`, and `SYS_OBJ_DESTROY = 6` are
  unchanged and inert in NPK.
- M3 frame offset `288` remains handler privilege, not pre-trap privilege;
  nested exception handling remains unsupported.
- This is not RTL, firmware, board support, or physical-hardware validation.

## 9. Files

Implementation: `nova/npk/src/memory.c`, `nova/npk/src/memory.s`,
`nova/npk/src/processing.c`, `nova/npk/src/kernel.c`,
`nova/npk/src/npk_core.h`, `nova/npk/src/start.s`, `nova/npk/build.py`,
`nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/memory.py`,
`nova/npk/__init__.py`.

Tests and tooling: `tests/test_npk_memory.py`, `scripts/npk_validate.py`.

Documentation: `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
`docs/NPK_INITIATION_REPORT.md`, this report.
