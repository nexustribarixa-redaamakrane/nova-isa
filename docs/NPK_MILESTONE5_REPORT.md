# NPK Milestone 5 report — Object Manager and Universal Object Model

**Milestone:** 5 of 10 — Object Manager and Universal Object Model.  
**Status:** COMPLETE after validation.  
**Baseline:** `349ab48fb7843b7f0a0024d52519730f1795d0f5`; `master` and
`origin/master` were clean and synchronized before M5 work. Milestones 1–4,
Nova ISA v1.0, and the Nova64 ABI are preserved.  
**Scope:** NPK only. No Phase 3, OpenWindows/NovaOW/Oryn, desktop, RTL,
compiler redesign, syscall dispatcher, device, timer, task, scheduler, user
process, or executable loader work is included.

## 1. Summary

Milestone 5 adds a processed-kernel Object Manager in `nova/npk/src/objects.c`.
The Winoid principle "everything is an object" is implemented as one common
contract for kernel-managed resources: stable identity, typed headers,
ownership, handles, references, access policy, optional namespace membership,
and type-specific destruction. It is not interpreted as one object per byte,
register, instruction, or variable.

Object work is not a direct helper call. Twelve typed operations enter the
shared NPK core and traverse `ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE`,
or an explicit `REJECTED`/`FAILED` terminal path. Each operation has a dedicated
receipt. A parallel extension record adds object-specific evidence without
altering the Milestone 1–4 receipt words.

`kernel.c` remains coordinator-only. It initializes the object manager and runs
its self-test; all object policy and state transitions live in the separate
object-manager source file.

## 2. External source study

External study was mandatory before implementation.

- **OpenWindows:** `https://github.com/nexustribarixa-redaamakrane/OpenWindows`,
  latest reviewed commit
  `9ea21e8007975191f12deba9be7db58d199349f8`.
- **WRK Object Manager:** public mirror
  `https://github.com/ayyucedemirbas/Windows-Research-Kernel-WRK-`, WRK v1.2.
  The mirror was not independently authenticated against the official Microsoft
  WRK distribution. Its files carry the WRK Source Code License notice.

No source code was copied into NPK. The study produced the following decisions.

| Source area | Adopted in NPK | Deliberately avoided |
| --- | --- | --- |
| OpenWindows `OW_OBJECT_HEADER`, `OW_OBJECT_TYPE`, `OW_OBJECT_DIR`, `OW_HANDLE` | Typed header-first object slots, typed directories, owner-scoped generation-tagged handles | One high-water global handle table and opaque handle lifetimes |
| OpenWindows `OwObjInitialize()`, `OwObjCreateDirectory()`, `OwObjCreateObject()`, `OwObjReference()`, `OwObjDereference()`, `OwObjOpenByName()`, `OwObjClose()` | Common create, directory insert/lookup/remove, retain/release, open, close, query, and destroy vocabulary | A close operation that does not dereference; NPK close drops exactly one real handle reference |
| OpenWindows object counting behavior | `refcount = handle_count + retain_count + namespace_count` | Pretending a handle exists by initializing `HandleCount = 1` before installation |
| OpenWindows directory behavior | Explicit remove and duplicate-name rejection | Unremovable entries, duplicate names, and ambiguous lookup |
| OpenWindows lookup and layout | One canonical header layout and component-wise path resolution | Flat-only lookup and inconsistent header/body assumptions |
| OpenWindows access and concurrency treatment | Explicit permission subsets, owner checks, and a declared sequential model | Claims of security enforcement or synchronization that the code does not provide |
| WRK header/body and type descriptors | Header plus fixed inline body, type ids, allowed access, valid operations, and destroy actions | Full NT object-manager generality and dynamic type objects |
| WRK pointer versus handle counts | Separate handle, retain, and namespace counts with explicit destruction ordering | Hidden lifetime accounting |
| WRK allocation/insertion split and granted access per handle | Validation reserves state; commit publishes it; each handle stores its own permission subset | State changes during validation and shared mutable permission assumptions |
| WRK lookup and management features | Bounded component-wise lookup and central dispatch | Symlinks, quotas, inheritance, 37-bucket hashing, multilevel handle tables, broad callback matrices, and native function pointers |

The full comparison is recorded in `docs/NPK_ARCHITECTURE.md` under
`Object Manager Design: OpenWindows and WRK Comparison`.

## 3. Capability audit

| Classification | Treatment in M5 |
| --- | --- |
| Existing reference-machine functionality | Sparse memory, the real four-level translator, and the M3 trap path are reused unchanged. |
| Frozen ISA/ABI contracts | No instruction, CSR, exception code, page format, syscall number, object-file format, or Nova64 ABI field changed. |
| New NPK M5 functionality | Object identity, typed headers, handles, references, type registry, namespace, access checks, destruction actions, typed operations, receipts, and extensions are implemented in kernel C. |
| Future or not implemented | Syscall dispatcher, processes/threads, devices, files, events, timers, communication ports, tokens, quotas, synchronization, scheduling, loader, physical hardware, Phase 3, and desktop work are excluded. |

## 4. Object model and fixed layout

Object state magic is `0x4E504B4F` and version is `5`. Object ids begin at
`0x4E504B4F00000000` and increase monotonically. Root directory identity is
`0x4E504B4F00000000`.

Each object slot is 128 bytes. It starts with a canonical header containing
magic, id, type, owner, parent, permissions, refcount, handle count, retain
count, payload, creation epoch, state, and namespace count, followed by three
inline body words. `CREATE` initializes the whole slot; `DESTROY` clears it.
Storage is static and fixed; the manager does not claim a general heap.

| Region | Address | Size | Purpose |
| --- | --- | --- | --- |
| Object entry | `0x96050` | 8 bytes | Published object request entry address |
| Object state | `0x96060` | 128 bytes | Magic, version, counters, epochs, ids, last status/reason |
| Object table | `0x960E0` | 64 × 128 bytes | Typed object headers and inline bodies |
| Handle tables | `0x980E0` | 2 × 64 × 64 bytes | Owner-scoped handle slots |
| Object request | `0x9A0E0` | 112 bytes | Ten request words plus four contiguous name words |
| Object response | `0x9A150` | 64 bytes | Status, reason, result, receipt index, slot |
| Object receipts | `0x9A190` | 128 × 128 bytes | Dedicated processing receipts |
| Type registry | `0x9E190` | 8 × 64 bytes | Built-in and runtime type descriptors |
| Namespace entries | `0x9E390` | 16 × 96 bytes | Directory entry table |
| Namespace state | `0x9E990` | 128 bytes | Namespace counters and last operation observations |
| Receipt extensions | `0x9EA10` | 128 × 32 bytes | Object identity, handle, counts, namespace evidence |

The 96-byte namespace entry is twelve 64-bit words; using an 80-byte stride
would overlap the generation/reserved fields and the namespace state. The
layout validator independently checks all fixed ranges and that extensions stay
below guest test code at `0xA0000`.

## 5. Typed operations and lifecycle

| Type | Operation | Main committed effect |
| ---: | --- | --- |
| `9` | `OBJECT_CREATE` | Publish a typed object and initial handle; optionally insert a name |
| `10` | `OBJECT_DESTROY` | Run the type destructor and clear an unreferenced object |
| `11` | `OBJECT_OPEN` | Create another handle with a permission subset |
| `12` | `OBJECT_CLOSE` | Clear one handle and drop one handle reference |
| `13` | `OBJECT_QUERY` | Observe committed object state without mutation |
| `14` | `OBJECT_RETAIN` | Add one non-handle reference |
| `15` | `OBJECT_RELEASE` | Drop one non-handle reference |
| `16` | `DIRECTORY_INSERT` | Add a named child and one namespace reference |
| `17` | `DIRECTORY_REMOVE` | Remove a named child and release its namespace reference |
| `18` | `DIRECTORY_LOOKUP` | Resolve a path without creating a reference |
| `19` | `TYPE_REGISTER` | Activate a runtime type descriptor |
| `20` | `TYPE_QUERY` | Read an active descriptor |

`VALIDATE` inspects the request, type, owner, arguments, payload ownership,
permissions, handle generation, object state, parent, name, and path. It does
not mutate published object state. `EXECUTE` finds a free slot or computes a
provisional result. `COMMIT` publishes the header, handle, counters, namespace
entry, type descriptor, or destructor result. Rejection reasons `30..45`
distinguish bad request, type, handle, object, owner, permission, state,
exhaustion, duplicate, parent, payload, unsupported operation, name, missing
entry, non-directory parent, and bad path. Object-table, handle-table, and
namespace-table exhaustion are distinct `FAILED` outcomes.

## 6. Handles, references, access, and destruction

A handle packs `(owner << 56) | (generation << 16) | index`. There are two
owners and 64 slots each. A generation is incremented on installation; close
clears state, object id, and permissions. Stale, forged, wrong-owner, and reused
generation handles are rejected.

Reference accounting is explicit:

```text
refcount = handle_count + retain_count + namespace_count
```

Handles, `RETAIN`, and namespace insertion each add one reference. `CLOSE`,
`RELEASE`, and namespace removal each drop one. Destruction requires the owner
to match, the object permission word to include `DESTROY`, the object not to be
the root, a zero reference count, no remaining namespace entry, and a successful
bounded destructor for its type. There is no implicit destruction on close.

Permission bits are `READ=0x01`, `WRITE=0x02`, `EXEC=0x04`, `SHARE=0x08`, and
`DESTROY=0x10`; all is `0x1F`. Same-owner operations require handle ownership.
Cross-owner `OPEN` requires `SHARE` on both the source handle and object, and
the requested permissions must be a subset of both. Directory insert/remove
requires `WRITE` on the owning directory handle; query and lookup require
`READ`. Type descriptors also declare allowed access.

## 7. Types and namespace

Four built-in types are registered at initialization: generic (`1`), memory
region (`2`), address space (`3`), and directory (`4`). Four additional slots
can be registered at runtime with a name, body size, valid operations, and
allowed access. Descriptors carry small destroy actions dispatched by central
C code; no native function pointers are stored.

Memory-region objects validate that the payload is an active M4 region owned by
the requesting owner. Address-space objects perform the matching M4 ownership
check. Memory-region destruction calls the existing M4 release service; a live
mapping causes a commit-time failure rather than a silent force-release.
Address spaces can be wrapped without inventing a process or task object.

The namespace root is a typed directory. Entries store parent id, object id, a
contiguous 32-byte name, owner, creation epoch, and generation. Insert rejects
duplicates in the same parent; remove releases the namespace reference. Lookup
supports `/`, single components, and up to four `/`-separated components with a
16-byte component limit. `Kernel/Alpha` resolves component by component. There
are no symbolic links, case folding, or general filesystem semantics.

## 8. Receipt evidence and preservation

M5 adds 128 receipts using the existing 128-byte receipt format and a parallel
128-record extension array. The extension stores object id, handle, packed
32-bit refcount/handle count, and namespace id. `NpkOperation` carries a stable
`object_identity` so the extension reflects the committed object when object
evidence is present. Main core, boot, trap, and memory receipt words remain
unchanged.

Preserved evidence includes the M1 boot receipt magic `NPKP`, input
`0x4E504B31`, output `0x4E504B32`, status `1`, final stage `5`, trace
`(1, 2, 3, 4, 5)`; M2 counters `admitted=10`, `completed=7`, `rejected=3`,
`failed=2`, `selftest_status=0`, both units idle, and all 12 main receipt
reserved words zero. A real M3 trap still dispatches with one receipt, and M4
allocation and mapping still work after object initialization.

## 9. Validation evidence

All required commands were run successfully after the implementation and test
changes:

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_npk.py tests/test_npk_core.py tests/test_npk_exceptions.py tests/test_npk_memory.py tests/test_npk_objects.py` | **123 passed** (100 M1–M4 plus 23 M5) |
| `python scripts/npk_validate.py` | **PASS** (NPK 123 passed; Phase 1 PASS) |
| `python scripts/phase1_validate.py` | **PASS**, Phase 1 counts unchanged |
| `python -m pytest -q tests/test_documentation.py` | **17 passed** |
| `python -m pytest -q` | **2368 passed** (2345 baseline plus 23 M5) |
| `python -m compileall -q nova tests scripts` | **passed** |

The dedicated M5 suite also passed independently: **23 passed**.

Phase 1 counts inside validation were unchanged: frontend 180, sema 85,
codegen 59, driver 55, runtime 15, library 1, end-to-end 13, emulator 1831,
example 1, conformance 18/18, compileall passed, and full suite 2368.

The validator's aggregate result was `NPK validation: PASS`; the Phase 1
validator's aggregate result was `Phase 1 validation: PASS`. The kernel booted
with status `0` throughout.

## 10. Implemented versus planned

| Area | M5 status |
| --- | --- |
| Object identity, typed headers, body area, fixed static tables | Implemented |
| Create, destroy, open, close, query, retain, release | Implemented |
| Directory insert, remove, lookup; hierarchical paths | Implemented |
| Type register and query; built-in and runtime descriptors | Implemented |
| Owner-scoped generation handles and explicit reference accounting | Implemented |
| Permission subset checks, share rule, owner enforcement | Implemented |
| M4 memory-region and address-space payload wrapping and cleanup | Implemented |
| Dedicated receipts and object evidence extensions | Implemented |
| External OpenWindows/WRK comparison and risk review | Implemented in architecture documentation |
| Syscall dispatcher and object syscalls | Planned for Milestone 6; reserved numbers remain inert |
| Processes, threads, devices, files, timers, ports, tokens | Not implemented |
| Scheduler, user process, executable loader | Not implemented |
| Hardware, Phase 3, desktop, RTL | Out of scope |

## 11. Explicit non-claims and limitations

- Objects use fixed static storage; there is no general kernel heap, dynamic
  object array, or dynamic type deletion.
- No process, thread, device, file, event, timer, communication-port, or
  security-token object is implemented.
- No ACL/token model, full NT security model, quotas, inheritance, multilevel
  handle tables, hash table, symbolic links, case-insensitive names, or broad
  callback matrix is implemented.
- No synchronization, locking, SMP, hart parallelism, or preemption is claimed.
  Processing units remain sequential.
- Commit is not transactional. There is no rollback, journal, isolation, or
  durability guarantee. A failed destructor or capacity check can leave earlier
  validated observations unchanged, but no atomic multi-object transaction is
  claimed.
- Reserved syscalls `SYS_OBJ_CREATE = 4`, `SYS_OBJ_SIZE = 5`, and
  `SYS_OBJ_DESTROY = 6` are unchanged and inert.
- The M3 frame word at offset `288` remains handler privilege, not pre-trap
  privilege; nested exception handling remains unsupported.
- M4's arena remains a software policy region over sparse reference memory; no
  RAM-size model, hardware frame pool, or `map_page()` API was invented.
- This is not RTL, firmware, board support, physical-hardware validation,
  OpenWindows/NovaOW/Oryn work, a desktop, or Phase 3.

## 12. Files

Implementation: `nova/npk/src/objects.c`, `nova/npk/src/processing.c`,
`nova/npk/src/kernel.c`, `nova/npk/src/memory.c`,
`nova/npk/src/npk_core.h`, `nova/npk/src/start.s`, `nova/npk/build.py`,
`nova/npk/layout.py`, `nova/npk/core.py`, `nova/npk/objects.py`,
`nova/npk/__init__.py`.

Tests and tooling: `tests/test_npk_objects.py`, `scripts/npk_validate.py`.

Documentation: `docs/NPK_ARCHITECTURE.md`, `docs/PHASE2_PLAN.md`,
`docs/NPK_INITIATION_REPORT.md`, this report.
