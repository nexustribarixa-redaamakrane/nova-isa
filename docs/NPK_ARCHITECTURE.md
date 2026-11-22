# NPK — Nova Processed Kernel architecture

**Status:** authoritative Phase 2 architecture baseline; Milestone 2 processing
core, Milestone 3 exception/trap subsystem, Milestone 4 memory subsystem,
Milestone 5 Object Manager, Milestone 6 syscall and privilege-context processing,
and Milestone 7 expanded typed device/timer processing implemented.

**Kernel:** NPK — Nova Processed Kernel.  
**Classification:** Winoid.  
**Phase:** Phase 2, NPK only. Phase 3 and OpenWindows work are out of scope.

NPK is the native kernel of Nova OS. It is not OpenWindows, NovaOW, Oryn, a
generic Linux-like kernel, or an emulator-only abstraction. Nova OS is a
separate operating-system project with its own future desktop identity; no
desktop, window manager, shell, or application work is part of Phase 2.

This document describes the architecture that Phase 2 implements incrementally.
A component described here is not assumed to exist until its milestone is marked
COMPLETE in `docs/PHASE2_PLAN.md`.

## The Processed Kernel Architecture

### Status of this definition

The existing repository documentation uses the name "Nova Processed Kernel"
but does not previously define what "Processed" means. It is not a canonical
term inherited from Nova ISA v1.0, the Phase 1 ABI, OpenWindows, or an older
kernel design. The following model is therefore a concrete **Phase 2 NPK design
decision**, introduced here rather than presented as an already-established
fact. It becomes authoritative for Phase 2 from this architecture baseline and
must be implemented, tested, and revised openly if later evidence requires it.

### Definition of "Processed"

In NPK, *processing* is the deterministic transformation of an explicit kernel
operation record through a defined pipeline of stages before its result is
committed. The name is not decorative: an operation is not handled by calling a
conventional kernel function directly. It is admitted, validated, executed,
committed, and receipted by a processing unit.

The kernel work processed by NPK includes boot verification, exception and
interrupt events, system-call requests, memory-management operations, object
operations, device events, task transitions, and executable-loading requests.
Boot verification, trap processing, the five memory operations, and the twelve
object operations are implemented; system-call, device, task, and loader work
remain architecture targets rather than implemented features.

An operation is *processed* only when:

1. it has a typed operation code, input, stage cursor, output, and status;
2. a processing unit advances it through the ordered stage pipeline;
3. each stage observes the invariants required by the previous stage;
4. the result is committed at an explicit commit point; and
5. a receipt records the performed stage sequence and terminal status.

Simple calls, threads, or ready queues are not relabeled as processing. A
function call is an implementation mechanism; an NPK processing operation is
the architectural unit of work whose progress and outcome are observable.

### Processing model

The pipeline is:

```text
producer -> ADMIT -> VALIDATE -> EXECUTE -> COMMIT -> COMPLETE -> receipt
                 \        \          \
                  \        \          +-> FAILED
                   \        +------------> REJECTED
                    +---------------------> REJECTED
```

- **Producer.** Boot code, a trap vector, a syscall adapter, a device interrupt
  adapter, or the executable loader creates an operation record. Producers
  classify and submit work; they do not implement kernel policy.
- **ADMIT.** The operation receives a legal code and input, is bound to a
  processing unit, and enters the pipeline. Admission has no kernel state
  side effects.
- **VALIDATE.** Operation type, argument shape, address ranges, capability
  requirements, privilege, and stage ordering are checked. An invalid operation
  becomes `REJECTED` with a reason and never reaches `EXECUTE`.
- **EXECUTE.** The validated operation performs its transformation and computes
  an output. For operations with memory or device effects, all required memory
  and device capabilities must already have been validated.
- **COMMIT.** The output and status become visible at a single defined commit
  point. This allows validation and rejection to be distinguished from a
  completed operation.
- **COMPLETE.** The processing unit emits a receipt and becomes available. The
  receipt is evidence for tests and debugging, not a user-visible ABI.
- **REJECTED / FAILED.** `REJECTED` means the operation never satisfied its
  entry invariants. `FAILED` means it passed validation but could not complete;
  failures are never silently converted to success.

A processing unit is the logical owner of one operation while it traverses the
pipeline. Milestone 1 has exactly one fixed unit, `PU0`, and no concurrency.
Later milestones may bind units to tasks or harts, but that is not implemented
or claimed here.

### Components responsible for processing

| Component | Responsibility | Status |
| --- | --- | --- |
| Operation producers | Create typed work from boot/trap/memory/object/syscall/device/loader events | Boot, trap, post-boot memory, post-boot object, syscall, and device-interrupt/console producers implemented; loader producers remain future work |
| `NpkOperation` | Carries operation code, input, stage cursor, provisional result, result, reason, receipt index, and typed evidence | Generalized typed records in Milestone 2; trap type added in Milestone 3; memory fields added in Milestone 4; stable object identity/handle/namespace fields added in Milestone 5 |
| Processing units `PU0`/`PU1` | Own one operation each and drive stage advancement | Two sequential units in Milestone 2; memory and object operations use the same ownership model |
| Stage handlers | Implement validate/execute/commit invariants per operation type | Boot, diagnostic, trap, five memory, twelve object, and syscall operation types implemented; device interrupts use the trap operation path |
| Admission interface | Validates id/type, rejects duplicates, assigns a free unit, and emits a receipt | Implemented in Milestone 2; trap, memory, and object operations use the same interface |
| Receipt memory | Publishes stage trace and terminal result for host observation | Fixed boot receipt at `0x90008`; core log at `0x90220`; trap log at `0x90B00`; memory log at `0x92050`; object log at `0x9A190` plus extension records at `0x9EA10` |
| Core/operation snapshots | Expose counters, unit state, and operation slots to host tests | Implemented in Milestone 2; trap state at `0x90A40` in Milestone 3; memory state at `0x90F10` in Milestone 4; object state at `0x96060` in Milestone 5 |
| Processing coordinator | Submits work and checks the terminal result | `npk_main` and the Milestone 2 self-test; trap dispatch in `trap.c`; memory request wrapper in `memory.c`; object request wrapper in `objects.c`; device backends called from trap/syscall paths |
| Scheduler | Chooses which unit/operation receives CPU time | Not implemented |

Milestone 3 separated the kernel entry from this mechanism:
`nova/npk/src/start.s` establishes the machine context, `nova/npk/src/trap.s`
is the Nova assembly trap entry and frame preserver, `nova/npk/src/trap.c`
classifies traps and submits typed operations, and `nova/npk/src/processing.c`
implements the operation pipeline and receipts. Milestone 4 adds
`nova/npk/src/memory.c` for the software allocator, page-table construction,
typed memory handlers, and published request wrapper, plus
`nova/npk/src/memory.s` for the real `TLBI r0` full-flush helper.
Milestone 5 adds `nova/npk/src/objects.c` for the typed object manager and its
request wrapper. `nova/npk/src/kernel.c` remains only a coordinator that
initializes boot, trap, memory, and object subsystems in order.

### Relationship between processing and scheduling

Processing and scheduling are different mechanisms:

- scheduling decides **which** ready work occupies a CPU;
- processing defines **how** an operation becomes a committed state change;
- a task may be blocked or resumed at a processing-stage boundary, but its
  stage progress is part of the operation record, not an implicit call stack;
- a scheduler must not skip a stage or promote an unvalidated operation.

Milestone 1 has a fixed boot operation and therefore no scheduler. Milestone 2
has four operation slots and two units but still no scheduler. Cooperative
scheduling in Milestone 8, and any later preemptive scheduling, will select
operations for processing units; they will not replace the pipeline.

### Interaction with other subsystems

| Subsystem | Interaction with processing |
| --- | --- |
| Exceptions | The Nova assembly vector saves the 304-byte software frame, admits an `NPK_OP_TRAP_PROCESS` operation, classifies the event, and restores the frame before `MRET`. |
| Memory | `VALIDATE` declares address/range/permission requirements; `EXECUTE` performs mapped operations; a fault re-enters processing as a fault event. |
| Object Manager | Object requests are admitted as typed operations; `VALIDATE` checks identity, handle, access, type, payload, and path invariants; `EXECUTE` reserves slots or computes a result; `COMMIT` publishes headers, handles, references, namespace entries, and type state. |
| Syscalls | The trap adapter converts the frozen ABI call into a syscall operation; validation checks number, arguments, and user pointers; commit writes the `a0`/`a1` result. |
| Devices | Interrupt adapters admit device-event operations; no driver bypasses validation or commits directly from an interrupt handler. |
| Tasks | Task creation, yield, block, resume, and exit are processing operations; the TCB holds the processing state that survives a stage boundary. |
| Executable loader | Header validation, allocation, mapping, entry setup, and activation are stages of a load operation, not uncontrolled side effects in a syscall. |
| Debugging | Receipts and boot markers provide deterministic, host-visible evidence of which stages ran and what terminal status was reached. |

### Guarantees

The processed architecture provides these guarantees as its subsystems are
implemented and tested:

- explicit provenance for every kernel action;
- ordered stage progression with no validation bypass;
- deterministic operation and receipt format;
- observable completion, rejection, or failure;
- an explicit commit point separating checked work from visible state changes;
- scheduler independence from processing correctness;
- a stable extension point for future trap, syscall, device, task, and loader
  operations without changing the Phase 1 ABI or ISA.

Milestone 1 guarantees these properties for boot verification. Milestone 2
extends them to four typed operation slots, two sequentially owned processing
units, explicit commit publication, and receipt-backed evidence for rejection
and failure paths. Milestone 3 extends them to machine-mode exception entry:
every trapped event is processed through the same lifecycle before its result
is published, and its own receipt records completion, rejection, or failure.
Milestone 5 extends evidence to object identity, handle, reference counts, and
namespace membership while preserving earlier receipt words. None of these
milestones guarantees transactional rollback, isolation, preemption,
multiprocessing, nested exception handling, or general syscall/device
processing.

### Difference from a conventional kernel

An ordinary conventional kernel can route a request directly to a subsystem
function, perform side effects inside that function, and treat the call stack as
the implicit record of progress. NPK instead makes the request itself an
architectural object:

- the operation record, not the call stack, defines progress;
- stage functions are small, ordered, and side-effect disciplined rather than
  being a collection of interchangeable service routines;
- a processing unit owns an operation across stage boundaries, so an operation
  can later be suspended and resumed without losing its processing state;
- `REJECTED` and `FAILED` are distinct from an unhandled call error;
- commit and completion are observable events with a receipt;
- the scheduler selects work for a unit but cannot redefine pipeline semantics.

This makes NPK a processed kernel by architecture and behavior. The distinction
does not come from naming existing threads or queues "processing". Milestone 1
demonstrates it with one operation; the distinction becomes more significant as
traps, syscalls, devices, tasks, and loading are admitted to the same pipeline
in later milestones.

### Testing the processed architecture

Processing is testable without inspecting undocumented host state:

1. build the kernel from the real assembler, compiler, linker, and `.nob` path;
2. boot it on `NovaMachine`;
3. read the fixed receipt memory and assert the magic, operation code, input,
   terminal status, final stage, output, and full stage trace;
4. assert that the kernel's return status is zero only when the receipt is
   complete;
5. add negative tests at each later milestone for rejected operations, failed
   execution, interrupted stage boundaries, and scheduler interactions.

The Milestone 1 receipt is defined as follows:

| Address / offset | Size | Field |
| --- | ---: | --- |
| `0x90000` | 8 | Existing boot marker |
| `0x90008 + 0` | 8 | Processing magic `0x4E504B50` (`NPKP`) |
| `0x90008 + 8` | 8 | Operation code, `1` for boot verification |
| `0x90008 + 16` | 8 | Current/final stage code |
| `0x90008 + 24` | 8 | Status: `0` pending, `1` processed, `2` rejected, `3` failed |
| `0x90008 + 32` | 8 | Input, equal to the boot marker value |
| `0x90008 + 40` | 8 | Output, `0x4E504B32` for a verified boot |
| `0x90008 + 48` | 40 | Five 8-byte stage-trace words, expected `1,2,3,4,5` |

The receipt is a kernel-to-harness observability structure, not a new syscall,
ISA facility, or executable format.

### Milestone 2 reusable processing core

Milestone 2 generalizes the single boot pipeline into a reusable core while
keeping the Milestone 1 receipt byte-for-byte compatible. The generic core lives
in `nova/npk/src/processing.c`; host-side decoding and validation live in
`nova/npk/core.py`. Two operation types are implemented: `NPK_OP_BOOT_VERIFY`
(the Milestone 1 operation) and `NPK_OP_CORE_DIAGNOSTIC` (a test-only type used
to exercise rejection and failure paths).

**Typed operation record.** Each of the four slots holds an id, type, stage,
state, owning processing unit, input, provisional output, published result,
reason, outcome, and receipt index. Admission rejects id `0`, unknown types,
duplicate live ids, a full slot table, no free unit, and receipt-table
exhaustion. A successful admission records `ADMIT` immediately and assigns a
unit; an admission failure emits a stage-zero rejection receipt with
`pu = 0xFFFFFFFFFFFFFFFF` and a specific reason.

**Controlled lifecycle.** The only legal forward path is:

```text
FREE --admit--> ADMITTED --validate--> VALIDATED --execute--> EXECUTED
    --commit--> COMMITTED --complete--> COMPLETE --release--> FREE
```

`VALIDATE` may terminate as `REJECTED`; `EXECUTE` and `COMMIT` may terminate as
`FAILED`. Terminal operations cannot be advanced again, and a non-terminal
operation cannot be released. `npk_core_advance_as(slot, pu)` enforces unit
ownership; `npk_core_advance(slot)` derives the requested unit from the
operation record. No stage can be skipped, repeated, or reordered.

**Processing units.** `PU0` and `PU1` are logical owners with `IDLE`/`BUSY`
state. Admission selects the first idle unit; both units busy yields
`NPK_ERR_UNIT_UNAVAILABLE`. Release is only valid for a terminal operation and
returns the unit to `IDLE`. The units are advanced sequentially by the single
machine-mode execution thread. This model does not claim SMP, harts,
preemption, or parallel execution.

**Commit semantics.** `EXECUTE` computes into `op->provisional`. The commit
handler publishes `op->result` only when its own check succeeds; a failed commit
leaves the published result zero and records a `FAILED` receipt. Commit is a
defined visibility point for the modeled operation and receipt state only. It
is **not** a transactional commit: there is no rollback, journal, isolation, or
durability guarantee, and no general memory or device side effects are performed
by Milestone 2 operations.

**Observation regions.** All regions are fixed, eight-byte-word aligned, and
outside the kernel image and boot stack:

| Region | Address | Size | Purpose |
| --- | --- | --- | --- |
| Core state | `0x90080` | 144 bytes | Magic/version, counters, last statuses, self-test status, unit state and unit operation ids |
| Operation snapshots | `0x90120` | 4 × 64 bytes | One snapshot per operation slot |
| Receipt log | `0x90220` | 12 × 128 bytes | Append-only evidence of admissions, rejections, failures, and completions |

Core state word offsets: `0` magic `0x4E504B43`, `8` version `2`, `16` slot
count `4`, `24` unit count `2`, `32` admitted, `40` rejected, `48` failed, `56`
completed, `64` last admission status, `72` last admission reason, `80` last PU
status, `88` last transition status, `96` self-test status, `104` reserved,
`112`/`120` unit states, and `128`/`136` unit operation ids.

Operation snapshot word offsets within each 64-byte slot: `0` id, `8` type,
`16` stage, `24` state, `32` pu, `40` result, `48` reason, `56` reserved. A free
slot has id/type/stage/state/result/reason zero and `pu = NO_INDEX`.

Receipt word offsets within each 128-byte slot: `0` magic `0x4E504B52`, `8` id,
`16` type, `24` pu, `32` final stage, `40` final state, `48` outcome, `56`
result, `64` reason, `72..104` five stage-trace words, `112`/`120` reserved.
Stage codes are `0` none, `1` admit, `2` validate, `3` execute, `4` commit, `5`
complete. Outcome codes are `0` none, `1` pending, `2` complete, `3` rejected,
`4` failed. State codes are `0` free, `1` admitted, `2` validated, `3` executed,
`4` committed, `5` complete, `6` rejected, `7` failed. Reason `0` means no
reason; `1..8` cover admission faults, `9..11` cover the diagnostic
validate/execute/commit failures, and `12..13` are reserved contract/output
reasons.

**Kernel self-test evidence.** After the boot operation, the core self-test
produces twelve receipts: a complete boot, a complete diagnostic, a validation
rejection, an execution failure, a commit failure, a complete operation plus a
duplicate-id rejection, two complete operations on different units, an
unavailable-unit rejection, and two final complete operations. The final core
state is `admitted=10`, `completed=7`, `rejected=3`, `failed=2`,
`selftest_status=0`, and both units `IDLE`. Tests validate each receipt's magic,
operation, unit, stage trace, outcome, result, and reason independently of the
kernel's `a0` return value.

**Milestone 2 limitations.** There is no trap/syscall/device/task/loader
producer, no general operation queue beyond four slots, no interrupt-driven
processing, no memory allocator, no scheduler, no user isolation, and no
hardware parallelism. Receipts are kernel-to-harness evidence, not an ABI. The
host decoders use private `NovaMachine` memory reads; those are test helpers,
not guest kernel APIs.

### Milestone 3 exception and trap subsystem

Milestone 3 replaces the placeholder vector with a real Nova assembly trap entry
in `nova/npk/src/trap.s`. On entry it preserves all 32 GPRs and the guest-visible
exception state in a fixed software frame, switches to a dedicated trap stack,
calls the C dispatcher in `nova/npk/src/trap.c`, restores the saved context, and
returns with `MRET`. The frame is written by Nova assembly, not by generic or
host assembly.

The 304-byte frame is 16-byte aligned at `0x90900` and has this exact layout:

| Offset | Size | Field |
| ---: | ---: | --- |
| `0..248` | 256 | `gpr[0..31]`, eight bytes each |
| `256` | 8 | `psr` |
| `264` | 8 | `epc` |
| `272` | 8 | `ecause` |
| `280` | 8 | `ecode` |
| `288` | 8 | handler privilege (`2`, machine) |
| `296` | 8 | `tval` |

The word at offset `288` is the **handler** privilege observed after exception
entry. It is not the pre-trap privilege: that value is available only in the
reference machine's internal exception save stack and is restored by `MRET`.
Guest software cannot inspect saved privilege through this frame. The frame is
single-level; nested exception handling is **not supported**, and the platform's
depth-4 internal save stack remains an architectural limit rather than a
multi-level NPK handler.

Trap classification is by `ecode`:

| `ecode` | Class | Action |
| ---: | --- | --- |
| `7` (`SYSTEM_CALL`) | system call | classify, service through the Milestone 6 syscall subsystem, and resume after the call |
| `8` (`INTERRUPT`) | software/timer/external interrupt | classify by low `ecause` source bits, service through the Milestone 7 device backend, and resume |
| `10` (`SOFTWARE_TRAP`) | software trap | resume after the trap instruction |
| `1` (`ILLEGAL_INSTRUCTION`), `3` (`MEMORY_FAULT`), `4` (`ALIGNMENT_FAULT`), `9` (`MMU_FAULT`) | fault | report and resume at the faulting PC |
| other assigned codes | unsupported | fail the operation and report; do not claim a service |
| `0` or unknown | invalid | reject the operation before publication |

Milestone 3 introduced the classification and frame contract. Milestone 6 now
services `SYSTEM_CALL` through a separate syscall operation without changing that
classification: `a0`/`a1` results are written into the saved frame before `MRET`,
and supported object numbers reach the Milestone 5 manager. Milestone 7 services
`ecode == 8` by the architectural `ecause` source bits (`0` software, `1` timer,
`2` external) instead of treating all interrupts as one unsupported class. Codes
`2`, `5`, `6`, and `11` remain recognized as assigned but unsupported exception
classes and produce a distinct `FAILED` trap receipt.

The dispatcher allocates an `NPK_OP_TRAP_PROCESS` operation through the ordinary
admission interface and advances it through `ADMIT → VALIDATE → EXECUTE → COMMIT →
COMPLETE`. `VALIDATE` checks the frame pointer, trap-state magic/version, legal
`ecode`, aligned `epc`, and machine handler privilege. `EXECUTE` computes a
provisional result only; `COMMIT` publishes it, and only then may `MRET` return.
Malformed frames and unknown codes are `REJECTED`; unsupported classes are
`FAILED`. Both outcomes have independent receipts and never publish a trap
result. The PU is released only after the operation reaches a terminal state.

Observation regions added in Milestone 3:

| Region | Address | Size | Purpose |
| --- | --- | --- | --- |
| Trap scratch | `0x200` | 24 bytes | Two saved registers plus an active-trap flag used by the assembly entry |
| Trap stack | top `0x88000` | grows down | Handler stack, separate from the boot stack |
| Software trap frame | `0x90900` | 304 bytes | Saved GPRs and exception state |
| Trap state | `0x90A40` | 128 bytes | Dispatch count, last frame fields, classification, action, result, receipt index, reason |
| Trap receipt log | `0x90B00` | 8 × 128 bytes | Independent receipts for trap operations |

The trap state begins with magic `0x4E504B54`, version `3`, and an initial
dispatch count of zero. Its `last_op_slot` and `receipt_index` fields use
`NO_INDEX` before a dispatch. Host decoders in `nova/npk/traps.py` read these
fixed regions through `NovaMachine`; they are test helpers, not guest APIs.

### Milestone 4 memory subsystem

Milestone 4 adds an NPK-managed memory subsystem over `NovaMachine`'s existing
sparse memory and real translator. The emulator has no RAM-size model, frame
allocator, or `map_page()` API, so NPK **declares** a finite physical arena and
implements ownership and mapping policy itself. This is a software kernel
subsystem in the reference machine, not a physical-hardware claim.

Five typed operations are admitted through the ordinary core:

| Type | Operation | Committed effect |
| ---: | --- | --- |
| `4` | `NPK_OP_MEMORY_ALLOCATE` | Reserves aligned physical pages and writes an allocation tag |
| `5` | `NPK_OP_MEMORY_RELEASE` | Frees an owned region only when it has no live mappings |
| `6` | `NPK_OP_MEMORY_MAP` | Allocates page-table pages and installs a 4 KiB leaf PTE |
| `7` | `NPK_OP_MEMORY_UNMAP` | Clears a leaf PTE and decrements the region's mapping count |
| `8` | `NPK_OP_MEMORY_QUERY` | Reports a region's base without mutating it |

The request entry is published at `0x90F00`; the fixed state, region, address
space, request/response, and receipt regions are:

| Region | Address | Size | Purpose |
| --- | --- | --- | --- |
| Memory entry | `0x90F00` | 8 bytes | Published address of `npk_memory_request` |
| Memory state | `0x90F10` | 128 bytes | Magic/version, arena, counters, epochs, next id, last status/reason |
| Region table | `0x90F90` | 64 × 64 bytes | Region state, id, base, size, owner, type, flags, live-map count |
| Address spaces | `0x91F90` | 2 × 32 bytes | State, owner, root PPN, mapping count |
| Memory request | `0x91FD0` | 64 bytes | Caller-published typed request |
| Memory response | `0x92010` | 64 bytes | Status, reason, result, receipt index, slot |
| Memory receipt log | `0x92050` | 128 × 128 bytes | Independent receipts for all memory operations |
| Physical arena | `0xC0000` | `0x40000` bytes | NPK-declared 4 KiB allocation arena |

`VALIDATE` checks the request magic, id, owner, argument shape, alignment,
canonical addresses, region identity/ownership/state, and permission encoding;
it performs no state mutation. `EXECUTE` computes a provisional base or PTE
address and checks arena fit or existing page-table structure. `COMMIT` writes
the region descriptor, tag, PTEs, counters, and TLB invalidation epoch. A
validation failure is `REJECTED`; exhaustion or a PTE conflict discovered after
validation is `FAILED`. The receipt jointly records the size and evidence word
so host tests can correlate it with real state.

Only 4 KiB leaf mappings are installed by this milestone. The translator still
supports 2 MiB and 1 GiB leaves, but NPK does not yet construct them. Full
`TLBI r0` is used after map/unmap; there is no ASID, shootdown, global-bit
policy, MMIO region, RAM discovery, executable loader, or user process. The
commit point is not transactional: there is no rollback, journal, isolation, or
durability guarantee, and a commit-time failure may leave earlier page-table
allocations visible. Boot work never admits a memory operation and leaves the
Milestone 1–3 receipts and counters unchanged.

### Milestone 5 object manager

The Winoid principle "everything is an object" is implemented as a common
contract for kernel-managed resources, not as one allocation per variable,
register, instruction, or byte. Milestone 4 supplies the memory payloads and
address spaces that M5 can wrap; M5 supplies stable identity, type, ownership,
handles, references, access checks, namespace membership, and lifetime. Milestone
6 exposes selected object operations through syscalls, but every object
operation still enters the NPK pipeline rather than bypassing it.

The fixed object state uses magic `0x4E504B4F` and version `5`. Object ids begin
at `0x4E504B4F00000000` and increase monotonically. Each 128-byte object slot
begins with a canonical header — id, type, owner, parent, permissions, refcount,
handle count, retain count, payload, creation epoch, state, namespace count —
followed by three inline body words. Storage is static and fixed; there is no
general kernel heap claim.

Operations `9..20` are typed core operations:

| Type | Operation | Main committed effect |
| ---: | --- | --- |
| `9` | `CREATE` | Publish a typed object and its initial handle; optionally insert a name |
| `10` | `DESTROY` | Run the bounded type destructor and clear the object when references are zero |
| `11` | `OPEN` | Create a second handle with a permissions subset |
| `12` | `CLOSE` | Clear one handle and drop its reference |
| `13` | `QUERY` | Observe header state without mutation |
| `14` | `RETAIN` | Add a non-handle reference |
| `15` | `RELEASE` | Drop one non-handle reference |
| `16` | `DIRECTORY_INSERT` | Add a named child and one namespace reference |
| `17` | `DIRECTORY_REMOVE` | Remove a named child and its namespace reference |
| `18` | `DIRECTORY_LOOKUP` | Resolve `/` or component-wise paths without adding a reference |
| `19` | `TYPE_REGISTER` | Activate one of the four runtime type slots |
| `20` | `TYPE_QUERY` | Read an active type descriptor |

Reasons `30..45` cover bad request, type, handle, object, owner, permission,
state, exhaustion, duplicate, parent, payload, unsupported operation, name,
missing entry, non-directory parent, and bad path. `REJECTED` means the request
failed validation; `FAILED` means a capacity or commit-time destructor check
failed after admission.

A handle is `(owner << 56) | (generation << 16) | index`. Two owners each have
64 slots. Generations make close/reuse observable and stale handles invalid.
The lifetime equation is
`refcount = handle_count + retain_count + namespace_count`. `DESTROY` requires
the same owner, the `DESTROY` permission, no root identity, no namespace entry,
and zero references. Permissions are `READ`, `WRITE`, `EXEC`, `SHARE`, and
`DESTROY` (`0x1F`). Same-owner operations require matching handle ownership;
cross-owner `OPEN` requires `SHARE` on both the source handle and object, plus a
requested permission subset of both.

The type registry has four built-in descriptors — generic, memory region,
address space, and directory — plus four runtime slots. Descriptors record body
size, valid operations, allowed access, and a small destroy action. Central C
code dispatches those actions; the implementation does not store native function
pointers. A directory is a typed object with a namespace table, and named M4
payloads are validated against their M4 owner before wrapping. Memory-region
destruction calls the existing M4 release service; a live mapping makes the
object commit fail and is not silently rolled back.

The namespace has a fixed root object at the object-id base, 16 entries, and a
32-byte contiguous name buffer. Insert rejects duplicates; remove releases the
namespace reference; lookup supports `/`, single components, and up to four
components separated by `/`, with 16 bytes per component. Symbolic links are
not implemented.

M5 adds a 112-byte request block, a 64-byte response block, 128 dedicated core
receipts, and 128 parallel 32-byte extension records. Extension words record
`object_id`, `handle`, packed `refcount`/`handle_count`, and `namespace_id`. The
M1–M4 receipt words remain unchanged; the extension is a parallel observation
record, not an ABI. `kernel.c` only calls object initialization and self-test;
object policy remains in `objects.c`.

The syscall numbers `SYS_OBJ_CREATE = 4`, `SYS_OBJ_SIZE = 5`, and
`SYS_OBJ_DESTROY = 6` are unchanged. Milestone 6 maps them to real M5 `CREATE`,
`QUERY`, `CLOSE`+`DESTROY`, and handle/payload services; see the syscall section
below. There is no process object, device object, file object, scheduler, user
executable loader, symbol table, ACL/token security model, quota system, or
physical-hardware claim. Commit is not transactional; no rollback, isolation, or
durability is claimed. Processing units remain sequential and no synchronization
model is claimed.

### Milestone 6 syscall and privilege-context processing

Milestone 6 adds a real syscall service in `nova/npk/src/syscalls.c`. `SCALL imm`
continues to enter through the Milestone 3 vector; `npk_trap_dispatch()` classifies
`ecode == 7`, records the trap receipt as before, and hands the frame to
`npk_syscall_dispatch()`. The syscall number is `frame->ecause`; arguments are
`saved frame GPRs a0..a5` (`gpr[1]..gpr[6]`). The kernel never calls the Phase 1
host helper `nova.syscalls.dispatch_syscall()`.

Each call admits one new typed operation, type `21`, into the shared core with a
fixed id derived from the syscall dispatch count. The operation traverses
`ADMIT → VALIDATE → EXECUTE → COMMIT → COMPLETE`; malformed or unauthorized calls
are `REJECTED`, while execution, child-operation, or commit faults are `FAILED`.
Reasons `46..56` identify frame/state/number/argument/privilege/pointer/
unsupported/resource/not-found/commit/child failures. A dedicated eight-slot
receipt log at `0x9FA90` records syscall operations without touching the M1–M5
logs; `npk_core_admit()` refuses to overwrite it when full.

Validation checks the trap frame, `ecode`, aligned `epc`, syscall initialization,
machine-mode handler context, syscall number and argument count, buffer capacity,
canonical user addresses, explicit user/read PTE bits through the active M4
address space, and fixed object handles/ownership. `SYS_READ` remains
`UNSUPPORTED` because no input device exists. `SYS_EXIT` and `SYS_YIELD` only set
observation flags; no process is terminated or scheduled. `SYS_TIME` reads the
live `time` CSR through a small `r7`-returning assembly leaf, because the C
compiler's return register is `r7` while the user-visible syscall return is `a0`
(`r1`).

`SYS_WRITE` copies at most 64 bytes into a bounded scratch buffer during
execution and publishes them to a 256-byte write log only at commit. Invalid
ranges are rejected before execution; capacity exhaustion at either stage is a
distinct failure with `ERR_NO_RESOURCE`. `SYS_OBJ_CREATE`, `SYS_OBJ_SIZE`, and
`SYS_OBJ_DESTROY` build existing M5 request blocks and call
`npk_object_syscall_request()`, producing real child M5 operations and receipts.
No object-manager logic is duplicated.

The frozen ABI is preserved: success writes the result to the saved frame's
`gpr[1]` and zero to `gpr[2]` (`a1`); failure writes the all-ones architectural
`-1` to `gpr[1]` and one of errors `1..4` to `gpr[2]`. Publication happens only
at commit or at a terminal failure, before the assembly trap entry restores GPRs
and executes `MRET`. The `SCALL` stub then resumes at `PC + 4` in user mode. The
host helper `npk.call_syscall()` only builds that stub and decodes observable
state; it is not a guest API.

The subsystem records a `handler_priv` value, but Nova v1.0 exposes only the
machine-mode handler privilege in the software frame. The pre-trap caller
privilege exists only on the emulator's internal save stack and is not visible
to guest kernel software. Consequently M6 validates the handler context but does
**not** claim caller-privilege enforcement, process ownership, or a user/kernel
address-space split. M6 also adds no device, timer, scheduler, loader, or new
syscall number.

### Milestone 7 device abstraction and timer integration

Milestone 7 adds `nova/npk/src/devices.c` and `nova/npk/src/devices.s` without
moving device policy into `kernel.c`, `trap.c`, or `syscalls.c`. The coordinator
calls `npk_device_init()` once; the syscall commit path calls
`npk_device_console_commit()`; the trap execute path calls
`npk_device_interrupt()`. `kernel.c` neither registers devices nor resets M7
state after initialization. No ISA instruction, encoding, CSR, trap code, syscall
number, or error code is changed.

#### Typed device and timer processing

The published observation word at `0xA0F00` holds the address of a bounded
104-byte request; boot does not admit an operation through it. Guest callers
publish a request with magic, operation id, operation code `22..30`, target
(device or timer id), owner, optional object handle, kind, capability, and four
argument words. The kernel replies through a bounded 80-byte response with
status, reason, architectural error, result, evidence, slot, and receipt index.
The request/response API is a kernel observation contract, not a new Nova ABI.

Every accepted request is admitted into the same Milestone 2 operation slots and
processing units used by earlier milestones. The five stages remain distinct:
`ADMIT` checks operation type, target, capacity, processing-unit availability,
and receipt slot; `VALIDATE` checks request identity, owner/object association,
registry or timer existence, registration state, operational state, capability,
and typed parameters; `EXECUTE` runs the real kernel backend and records a
provisional result without publishing state; `COMMIT` publishes the bounded
registry/timer/log state and receipt; `COMPLETE` releases the processing unit.
Rejection, backend failure, and completion remain distinguishable. A success is
never reported without execution and commit, and unsupported kinds or backends
return explicit unsupported results.

The device registry has eight bounded 64-byte records. Each record carries a
unique nonzero id, kind, capability, owner, object association, state, operation
count, and last result. Registration and unregistration are lifecycle
operations, not direct registry writes; duplicate ids, unsupported kinds,
capability mismatches, owner/object mismatches, and registry exhaustion are
rejected without mutating the registry. Multiple devices of a supported kind are
permitted. Console and timer are supported; block, input, and network are
declared unsupported. Device write validation uses the Milestone 4 user-physical
translation for the caller buffer, copies bytes into bounded kernel scratch in
`EXECUTE`, and appends them to the Milestone 6 write log in `COMMIT`. No caller
pointer is dereferenced unchecked, and no DMA is claimed.

The timer table has eight bounded 64-byte records with explicit `PENDING`,
`EXPIRED`, `DELIVERED`, and `CANCELLED` states. Creation records the current
emulator `time` and requested interval, then computes a real `time + interval`
deadline. Expiration chooses the earliest due deadline, breaking ties by lowest
timer id, writes a bounded event record, and marks the timer expired. Cancellation
is accepted only from `PENDING`; expiration and delivery are terminal and do not
create duplicate events. A separate eight-entry event log and event-id sequence
make delivery observable. Timer create/cancel/inspect/expire/deliver all carry
typed receipts. Timer events and receipts are bounded; exhaustion is rejected
and never overwrites earlier evidence.

Device and timer receipts live in separate 32-entry tables at `0xA1A00` and
`0xA2A00`, distinct from the M2/M3/M4/M5/M6 logs. Each receipt includes the
operation id, operation type, processing unit, final stage, state, outcome,
result/error, reason, complete stage trace, and device/timer evidence. Dedicated
device/timer operation counters record completed work without changing earlier
core counters.

#### Console, clock sources, and interrupts

The console backend does not invent a serial device. `SYS_WRITE` continues to
stage bytes in the M6 scratch buffer and commit them to the M6 write log; the
device backend owns that commit and counts calls/bytes. The host-side
`devices.publish_console()` helper mirrors those bytes into the reference
machine's `machine.io_log`. Nova has no I/O instruction, and NPK never calls the
Phase 1 host syscall dispatcher.

The clock-source audit is deliberately explicit:

| Clock or event source | Status in M7 | What NPK actually does |
| --- | --- | --- |
| Host reference clock | Host test/validation only | Used by the Python harness and QEMU runner to measure execution; never read by guest kernel code and never a timer source |
| Emulator cycle counter | Real `NovaMachine` CSR | `time` is read by the kernel to compute software timer deadlines and by the trap backend to record interrupt evidence |
| Software logical clock | Implemented | Bounded timer records and event ids order software timer events deterministically; this is kernel RAM state, not a hardware timer |
| Future hardware timer | Not present | `timecmp` is an architectural one-shot CSR, but there is no hardware timer device, timer MMIO, or device tree |
| Real hardware interrupt | Not present | `ip`, `ie`, and `ivec` are architectural CSRs; host-injected software/timer/external interrupts exercise the real M3 vector but are not physical interrupts |

The assembly leaves in `devices.s` return values in `r7` (the `nova-cc` C return
register) and mirror reads into `a0`. On `ecode == 8`, `trap.c` classifies the low
`ecause` source bits as software (`0`), timer (`1`), or external (`2`) and invokes
`npk_device_interrupt()` from the device backend. The backend records bounded
evidence and, for a timer interrupt, writes a new aligned one-shot `timecmp`.
The interval is large enough for the unoptimized C trap path to finish before a
new compare can fire. The emulator clears the delivered `ip` bit and appends the
source to `irq_log`. The M3 frame and exception receipts remain intact. Software
timer events delivered through the typed timer API are software events; they do
not set `ip`, inject an interrupt, wake a task, or imply a scheduler.

#### Observation layout and limits

The M7 layout is bounded and disjoint from M1–M6 evidence: device state at
`0xA1000` (144 bytes), interrupt log at `0xA1090` (16 × 32 bytes), capability
table at `0xA1290` (8 × 32 bytes), request entry at `0xA0F00`, request block at
`0xA1390`, response at `0xA1400`, operation state at `0xA1450`, registry at
`0xA14E0`, timer table at `0xA16E0`, event log at `0xA18E0`, device receipts at
`0xA1A00`, timer receipts at `0xA2A00`, scratch at `0xA3A00`, and a layout limit
of `0xB0000`. Host decoders and validators in `nova/npk/devices.py` observe this
contract but are test helpers, not guest APIs.

Milestone 7 claims no MMIO device, device tree, framebuffer, keyboard, disk,
network, DMA, interrupt controller, preemptive scheduler, wakeup, process model,
or user loader. Software IPIs and external interrupts are architectural events
injected by the reference machine for tests; they are not physical device
drivers. The backend is real kernel software over bounded RAM and the existing
Nova ISA CSRs, not a fabricated bus or device.

### Object Manager Design: OpenWindows and WRK Comparison

An external source study was completed before implementation. The OpenWindows
project was reviewed at
`https://github.com/nexustribarixa-redaamakrane/OpenWindows`, latest reviewed
commit `9ea21e8007975191f12deba9be7db58d199349f8`. The Windows Research Kernel
material was reviewed from the public mirror
`https://github.com/ayyucedemirbas/Windows-Research-Kernel-WRK-` for WRK v1.2;
the mirror was not independently authenticated against the official Microsoft
distribution, and its files carry the WRK Source Code License notice. These
sources informed design choices; no source was copied into NPK.

| Source observation | What NPK adopted | Risk or behavior NPK deliberately avoided |
| --- | --- | --- |
| OpenWindows defines `OW_OBJECT_HEADER`, `OW_OBJECT_TYPE`, `OW_OBJECT_DIR`, and `OW_HANDLE` | A common header-first typed object, typed directories, and generation-tagged owner-scoped handles | A single high-water global handle table; NPK uses fixed owner tables with explicit free-slot search and generation validation |
| OpenWindows exposes `OwObjInitialize()`, `OwObjCreateDirectory()`, `OwObjCreateObject()`, `OwObjReference()`, `OwObjDereference()`, `OwObjOpenByName()`, and `OwObjClose()` | Create, directory insert/lookup/remove, retain/release, open, and close as the core object vocabulary | Setting `HandleCount = 1` before a real handle exists; NPK derives refcount from real handle, retain, and namespace references |
| OpenWindows closing a handle does not dereference the object | Explicit handle close drops exactly one handle reference | Lifetime errors from a hidden reference; NPK rejects destruction while any handle remains |
| OpenWindows has no general unlink/delete path and allows duplicate directory names | Explicit directory remove and duplicate-name rejection | Unremovable names and ambiguous lookup results |
| OpenWindows lookup is flat and header/body layout is inconsistent | One canonical 128-byte header-first slot and component-wise path resolution | Hidden body-layout assumptions and unbounded flat scans |
| OpenWindows ignores access flags in several paths and has no synchronization | `READ`/`WRITE`/`EXEC`/`SHARE`/`DESTROY` checks, subset-based open, owner checks, and a sequential processing model | Claiming security or concurrency guarantees the implementation does not provide |
| WRK separates header and body, uses type descriptors, and distinguishes pointer counts from handle counts | Header plus fixed inline body, descriptor tables, and separate `handle_count`/`retain_count`/`namespace_count` | Full NT object manager generality |
| WRK separates object allocation from directory insertion and uses granted access per handle | Validation reserves state, commit publishes it; each handle stores its own permission subset | Mutable global permission assumptions |
| WRK performs component-wise lookup and explicit close/delete ordering | Component-wise paths, zero-reference destruction, and ordered type destructors | Symlinks, case-insensitive naming, and implicit deletion |
| WRK supports quotas, dynamic type objects, inheritance, hashing, multilevel handle tables, and broad callback matrices | Only bounded fixed tables and small central dispatch actions | Fabricated capacity, unsupported security semantics, native function pointers, and unproven synchronization |

## 1. Purpose and scope

NPK provides the kernel foundation for Nova OS:

- a defined boot contract over the existing Nova toolchain;
- machine-mode exception and syscall handling;
- physical and virtual memory management;
- a cooperative task substrate, later extended for timer-driven scheduling;
- a device abstraction over capabilities the reference model actually has;
- loading and running Nova user executables;
- debugging, validation, and explicit security boundaries.

Phase 2 preserves Nova ISA v1.0 and the Phase 1 C ABI. No instruction,
encoding, register, CSR, exception code, page format, syscall number, object
format, or relocation kind is changed by NPK. If an architectural change later
becomes necessary, it must be proposed and documented separately before any
implementation.

## 2. CPU compatibility policy

**NPK is Nova64-only in Phase 2.** It targets the Nova ISA v1.0 reference
contract and the `NovaMachine` model in this repository. There is no arm64,
x86-64, RISC-V, or other CPU target in Phase 2, and no multi-architecture HAL
is created.

This is consistent with the Winoid model: a Winoid is 64-bit-only and chooses
its own CPU compatibility policy. NPK's policy is currently Nova-exclusive.
Supporting another architecture would be a separate, explicitly authorized HAL
and toolchain project; it must not be silently assumed.

NPK's 64-bit assumptions are architectural, not incidental:

- 64-bit virtual addresses, with 48-bit canonical paged addresses;
- 64-bit general-purpose registers and `a0`-`a5` syscall arguments;
- 52-bit physical addresses in page-table entries;
- LP64 C data model from the Phase 1 ABI.

## 3. Execution environment

The reference platform for Phase 2 is `NovaMachine`, with its existing:

- three privilege levels: user `0`, supervisor `1`, machine `2`;
- machine-only privileged instructions and CSR access rules;
- one exception vector (`vbar`) and one interrupt vector (`ivec`);
- `SCALL`, `TRAP`, `MRET`, `IRET`, `HALT`, `WAIT`, `SIPI`, `FENCE`, and `TLBI`;
- capability-based object instructions and syscall numbers;
- sparse byte-addressable memory and a functional four-level translator;
- software, timer, and host-injected external interrupt sources;
- shared-memory `NovaCluster` with IPIs.

Important platform facts that shape NPK:

1. A fresh `NovaMachine` resets to **user** privilege; the host harness or
   `nova-run` must explicitly select machine privilege for kernel boot.
2. Exception entry saves only `(epc, ecode, ecause, previous_priv)` in an
   internal, non-memory save stack of depth 4. It saves no GPRs, no `sp`, no
   `psr`, and no `tval`.
3. All synchronous traps and interrupts enter machine privilege. There is no
   supervisor trap delegation and no `SRET`/`ERET` instruction.
4. There is no boot firmware, device tree, MMIO device, or architectural kernel
   syscall hook inside `NovaMachine`. NPK installs a software `SCALL` trap path;
   the separate host reference dispatcher used by Phase 1 is not that path.
5. There is no RAM-size limit or frame allocator. Memory is a sparse map of
   host backing pages.
6. There is no ASID, TLB shootdown, or supervisor delegation.

NPK therefore begins as a **machine-mode kernel**. Supervisor mode is reserved
for a later architecture step, not used as a fiction in Phase 2.

## 4. Subsystem organization

The logical NPK subsystems are:

```text
NPK
├── Core            constants, image validation, boot result, panic policy
├── Boot            entry assembly, initialization order, stack setup
├── Processing      operation records, stages, processing units, receipts
├── Architecture    privilege/CSR/vector contracts and ISA-facing helpers
├── Exceptions      trap vectors, software trap frame, fault dispatch, MRET
├── Memory          physical frames, page tables, address spaces, TLB policy
├── Scheduler       task representation, queues, context switch, yield
├── Syscalls        ABI table, argument validation, error conversion
├── Devices         console, timer, interrupt sources, future device drivers
├── Executable Loader  .nob validation, mapping, stack setup, entry
├── IPC              reserved for later message/port primitives
└── Debugging        boot markers, trace/log integration, panic diagnostics
```

Milestone 1 creates the Core, Boot, Processing, Architecture, and Debugging
foundations needed for a reproducible boot image. Milestone 2 generalizes the
Processing subsystem into the reusable operation core described above. Empty
directories for the other subsystems are deliberately not created.

## 5. Kernel image and boot contract

### 5.1 Image format

NPK uses the existing `.nob` container. No new kernel executable format is
introduced in Phase 2: `.nob` already supplies a load address, entry address,
flags, exact image size, and a validated byte image. A new format would require
a concrete technical requirement that `.nob` cannot express, and none has been
identified for first boot.

An NPK kernel image is a `.nob` with:

- `image_base = 0x1000`;
- `entry` resolving to the global symbol `_npk_start`;
- `flags = 0`;
- image size a multiple of four bytes;
- no relocation processing at load time.

### 5.2 Loader responsibility

The host-side loader or `nova-run`:

1. reads and validates the `.nob`;
2. loads the image bytes at `image_base`;
3. sets `pc = entry`;
4. sets privilege to machine;
5. transfers control.

The loader supplies no stack. `_npk_start` must establish `sp` before calling
any C function.

### 5.3 Kernel entry responsibility

`_npk_start`:

1. sets `sp` to `NPK_BOOT_STACK_TOP`;
2. installs the exception vector address from a link-time symbol;
3. writes the boot marker;
4. calls `npk_main`, which submits and checks the boot verification operation
   and initializes the trap observation state;
5. executes `HALT` with `npk_main`'s return value in `a0`.

`HALT` is the Phase 2 termination mechanism. `SYS_EXIT` is now serviced by the
Milestone 6 kernel syscall subsystem as a recorded policy request; NPK has no
task or scheduler, so it does not terminate a process or change machine state.

### 5.4 Initialization order

The implemented prefix of the initialization order is:

1. architecturally required boot state (`sp`, `vbar`);
2. boot marker and the `NPK_OP_BOOT_VERIFY` pipeline;
3. processing-core initialization and self-test;
4. exception/trap observation state (Milestone 3);
5. memory-manager state and published request entry (Milestone 4);
6. object-manager state, type registry, namespace, and published request entry
   (Milestone 5);
7. syscall observation state and dispatch policy (Milestone 6).

Future milestones would add, in order, scheduler and executable loader. The
coordinator in `kernel.c` calls `npk_process_boot()`, `npk_trap_init()`,
`npk_memory_init()`, `npk_object_init()`, `npk_object_selftest()`,
`npk_device_init()`, and `npk_syscall_init()` only. Syscall policy remains in
`syscalls.c`, device policy in `devices.c`; the trap C dispatcher publishes
classification and hands system-call or interrupt frames to the appropriate
backend.

Each milestone may implement only the next coherent prefix of this order.

### 5.5 Build and QEMU validation workflow

The root `CMakeLists.txt` orchestrates this build through
`scripts/npk_cmake_build.py`, which calls `nova.npk.build_kernel()`. CMake is a
workflow wrapper only: the kernel is still assembled, compiled, and linked by
the Nova toolchain, and the output is still the same deterministic `.nob`.

The `npk_qemu_boot` target runs `scripts/npk_qemu_boot.py`. QEMU has no Nova
target, so the script generates an x86-64 multiboot ELF from the real `.nob`
image. Its long-mode stub interprets Nova instructions in generated code. The
Nova image is placed in a 1 MiB guest-physical shadow at `0x100000`, and the
interpreter adds that offset to Nova data addresses; Nova software therefore
sees its architectural stack, marker, receipt, and vector addresses. Generated
interpreter code is placed above the shadow to avoid self-overwrite.

This harness validates the boot marker, exact step count, and a 64 KiB guest
memory region against the Python reference boot. It is not native Nova support
in QEMU, firmware, board support, RTL, physical-hardware validation, or an ISA
change. It never changes the Nova64 ABI, syscall numbers, errors, or M1–M7
behavior.

## 6. Kernel memory layout

Milestone 1 runs in bare translation mode (`ptbr = 0`), so virtual and physical
addresses are identical. The initial layout is:

| Region | Address | Purpose |
| --- | --- | --- |
| Kernel image | `0x1000` upward | Code and data loaded from `.nob` |
| Exception vector | link-computed inside the image | `vbar` target installed by `_npk_start` |
| Boot stack | top `0x80000`, grows down | Kernel stack before a real allocator exists |
| Boot marker | `0x90000` | 8-byte handshake word written by `_npk_start` |
| Fixed boot receipt | `0x90008` | Milestone 1 receipt, 96 bytes, preserved by the generic boot operation |
| Core state | `0x90080` | Milestone 2 core snapshot, 144 bytes |
| Operation snapshots | `0x90120` | Four 64-byte operation-slot snapshots |
| Receipt log | `0x90220` | Twelve 128-byte processing receipts |
| Trap scratch | `0x200` | Three 8-byte entry-scratch slots |
| Trap stack | top `0x88000` | Separate machine-mode handler stack |
| Trap frame | `0x90900` | Milestone 3 304-byte Nova assembly trap frame |
| Trap state | `0x90A40` | 128-byte trap dispatch observations |
| Trap receipt log | `0x90B00` | Eight 128-byte trap receipts |
| Memory entry | `0x90F00` | Published memory request entry address |
| Memory state | `0x90F10` | 128-byte memory-manager observations |
| Region table | `0x90F90` | 64 × 64-byte physical region descriptors |
| Address spaces | `0x91F90` | 2 × 32-byte software page-table roots |
| Memory request/response | `0x91FD0` / `0x92010` | 64-byte typed request and response blocks |
| Memory receipt log | `0x92050` | 128 × 128-byte memory receipts |
| Object entry | `0x96050` | Published object request entry address |
| Object state | `0x96060` | 128-byte object-manager observations |
| Object table | `0x960E0` | 64 × 128-byte typed object headers |
| Handle tables | `0x980E0` | 2 owners × 64 × 64-byte handle slots |
| Object request/response | `0x9A0E0` / `0x9A150` | 112-byte request and 64-byte response |
| Object receipt log | `0x9A190` | 128 × 128-byte object receipts |
| Type registry | `0x9E190` | 8 × 64-byte type descriptors |
| Namespace entries | `0x9E390` | 16 × 96-byte directory entries |
| Namespace state | `0x9E990` | 128-byte namespace observations |
| Object receipt extensions | `0x9EA10` | 128 × 32-byte object evidence records |
| Syscall state | `0x9FA10` | 128-byte syscall dispatch observations |
| Syscall receipt log | `0x9FA90` | Eight 128-byte syscall receipts |
| Syscall write log | `0x9FE90` | 256-byte bounded `SYS_WRITE` sink |
| Syscall scratch | `0x9FF90` | 64-byte uncommitted `SYS_WRITE` copy buffer |
| Device entry/request/response | `0xA0F00` / `0xA1390` / `0xA1400` | Typed request publication word and bounded request/response blocks |
| Device state | `0xA1000` | 144-byte M7 state block: magic, version, counters, last interrupt observations, timer interval |
| Interrupt observation log | `0xA1090` | 16 × 32-byte bounded interrupt records |
| Capability table | `0xA1290` | 8 × 32-byte support/rejection declarations |
| Device operation state | `0xA1450` | 144-byte M7 lifecycle counters and last outcome |
| Device registry | `0xA14E0` | 8 × 64-byte bounded device records |
| Timer table | `0xA16E0` | 8 × 64-byte bounded software timer records |
| Timer event log | `0xA18E0` | 8 × 32-byte bounded delivery events |
| Device/timer receipts | `0xA1A00` / `0xA2A00` | Two independent 32 × 128-byte receipt logs |
| Device scratch | `0xA3A00` | 64-byte bounded kernel copy buffer |
| Declared physical arena | `0xC0000`, `0x40000` bytes | Sparse-memory policy arena, not discovered RAM |
| Unmapped/unused | elsewhere | No RAM bound is modeled by the emulator |

The boot marker value is `0x4E504B31` (`"NPK1"`). It proves that the kernel
entry ran and that the host loaded the image at the expected base. It is a
debug/handshake mechanism, not a kernel API.

Milestone 4 enforces canonical virtual addresses, explicit R/W/X/U PTE bits,
owner-scoped address spaces, and full `TLBI` invalidation on map/unmap. It does
not yet implement a global low-kernel/high-user layout policy: that belongs to
the future syscall/task/loader work. NPK currently maps both software address
spaces through distinct roots owned by the requesting owner, and no process or
user-executable concept exists.

There is no ASID in Nova v1.0, so address-space switches use `ptbr` writes and
the full TLB flush they cause. NPK must not claim ASID-tagged isolation.

## 7. Privilege model

NPK starts at machine privilege because the platform routes every trap to
machine mode. It must not use supervisor privilege until either:

- a machine-mode shim is implemented and tested, or
- the platform gains supervisor trap delegation.

The machine-only boundary includes `HALT`, `MRET`, `IRET`, `TLBI`, `SIPI`,
virtualization instructions, and all machine-only CSRs. User processes may read
`psr`, `priv`, `coreid`, and `time`, subject to the Phase 1 CSR rules.

Privilege transitions available to NPK:

- exception/interrupt entry: previous privilege → machine;
- `MRET`/`IRET`: machine → saved privilege;
- `CSRW priv`: machine software may explicitly change current privilege;
- VM transitions: reserved for a later, separate hypervisor feature.

Milestone 6 can validate that the handler is machine mode, but the software trap
frame does not expose the pre-trap caller privilege. It is therefore incorrect to
describe the syscall subsystem as enforcing caller privilege. No process,
scheduler, or trusted call gate exists; object calls use a fixed kernel-owned
observation owner, and M4 user pointers are checked against explicit `U` PTEs
rather than a process address space.

## 8. Exception architecture

### 8.1 Hardware entry

On a trap, `NovaMachine` writes `ecode`, `ecause`, and `epc`, forces machine
privilege, pushes the internal frame, and jumps to `vbar` or `ivec`. GPRs are
unchanged. Synchronous faults use the faulting PC as `epc`; `SCALL` and `TRAP`
use `PC + 4`.

### 8.2 Software trap frame

NPK owns its trap frame in kernel memory. Milestone 3 implements the exact
304-byte, 16-byte-aligned layout described in the Milestone 3 section above. All
32 GPRs are saved, including the scratch registers used by the entry stub. The
state fields save `psr`, `epc`, `ecause`, `ecode`, the handler privilege, and
`tval`. The privilege word at offset `288` is the privilege observed **inside**
the machine-mode handler, not the pre-trap privilege; the latter is restored by
`MRET` from the platform's internal save stack. Hardware does not preserve GPRs,
so the Nova assembly vector must do so before calling C.

The frame is single-level. Nested exceptions are not supported: a fault taken
while the handler is active would reuse the fixed frame. The internal platform
save stack is four entries deep and discards the oldest entry on overflow; NPK
does not implement a deeper software stack.

### 8.3 Dispatch

Milestone 3 dispatch classifies by `ecode` and processes every class as an
`NPK_OP_TRAP_PROCESS` operation through the full processing-core lifecycle.
For `SYSTEM_CALL`, Milestone 6 additionally hands the saved frame to the syscall
service, which admits a separate type-21 operation and owns the `a0`/`a1` result
contract. Classification is published before M3 admission, so a valid system
call still reaches the syscall service when the M3 trap receipt log is full; a
trap operation that rejects or fails the frame is not handed off. Faults report
their class and resume at the saved `epc`; software
traps and system calls resume at `epc`, which the platform sets to `PC + 4`.
Unknown or zero `ecode` values are rejected; assigned but unsupported classes
are failed. No syscall result is published before `COMMIT`, and `MRET` occurs
only after the operation reaches a terminal state.

`MRET` returns to `epc`, restores the saved privilege from the platform stack,
and clears `ecode`; `ecause` is not restored, so guest software must not assume
it is zero after return.

## 9. Memory management architecture

The existing translator is real and is reused, not duplicated:

- `ptbr[63]` enables four-level translation; `bits[51:12]` hold the root PPN;
- leaf at level 3 = 4 KiB, level 2 = 2 MiB, level 1 = 1 GiB;
- level 0 leaves are reserved;
- PTE bits: valid `0x1`, R `0x2`, W `0x4`, X `0x8`, U `0x10`, global `0x20`,
  accessed `0x40`, dirty `0x80`;
- faults report `MMU_FAULT` with details for missing, permission, reserved,
  noncanonical, and misaligned entries, and write the address to `tval`;
- `TLBI` and `ptbr` writes flush translations; the TLB is a functional cache,
  not a hardware-exact structure.

Milestone 4 implements the NPK policy layer over this translator:

1. a 4 KiB allocator over the declared `0xC0000..0x100000` arena with stable
   region ids, owner, type, size, and live-map count;
2. a page-table manager that constructs four-level Nova PTEs and allocates its
   own table pages from the same arena;
3. two software address spaces with independent roots and owner checks;
4. map/unmap handlers with R/W/X/U permission validation and full `TLBI`;
5. MMU fault behavior verified against the unmodified reference translator.

Physical pages are identified by base and size, not by host allocator objects.
A region cannot be released while it has live mappings. `QUERY` observes a
region without changing it. Allocation exhaustion is a `FAILED` execute result;
bad ids, sizes, alignment, addresses, permissions, and ownership are `REJECTED`
at validation; duplicate mappings are detected at commit and produce a distinct
PTE-conflict failure rather than silently replacing the leaf.

Platform gaps remain explicit: no RAM-size model, no MMIO regions, no ASID, no
TLB shootdown, and an ignored global PTE bit. NPK does not invent hardware
capabilities. There is no page replacement, demand paging, huge-page
construction, memory protection keys, copy-on-write, or transactional rollback.

## 10. System-call architecture

The syscall ABI stays unchanged: numbers `0..7`, arguments in `a0`-`a5`, return
in `a0`, error `-1` in `a0` with an error code in `a1`, and the call number
delivered as `ecause` on `SYSTEM_CALL`.

The in-kernel syscall service now:

1. verifies `0 <= ecause <= 7` and the expected argument count;
2. validates the saved trap frame, machine-mode handler context, and buffer
   capacity;
3. validates user pointers against explicit user/read PTEs in the active M4
   address space;
4. resolves object handles and access requirements through M5 state;
5. admits a type-21 operation and drives all five processing stages;
6. converts terminal failures to errors `1..4` and publishes `a0`/`a1` into the
   saved frame before `MRET`.

Calls `0`, `3`, and `7` are serviced locally as policy observations; `2` remains
unsupported; `1` uses the bounded write sink; and `4`–`6` are translated to real
M5 object operations. The host-side `nova.syscalls.dispatch_syscall()` remains
the reference-model helper used by Phase 1 tests. It is not the NPK kernel path
and is not called from `syscalls.c`. No caller-privilege claim is made because
the pre-trap privilege is not in the software trap frame.

## 11. Device architecture

NPK separates generic interfaces from concrete backends. Milestone 7 implements
this contract as follows:

- typed operations: device register/unregister/query/write and timer
  create/cancel/inspect/expire/deliver pass through the shared five-stage core;
  bounded registry, timer, event, and receipt tables are the published evidence;
- console: the M6 `SYS_WRITE` commit path calls the M7 backend and leaves bytes
  in the bounded write log; the host mirrors them to `machine.io_log`;
- timer: emulator `time` drives software deadlines in bounded RAM records;
  `timecmp` plus `ie`/`ip`/`ivec` remain the architectural interrupt CSR path,
  accessed only through the small Nova assembly leaves in `devices.s`;
- interrupt sources: software, timer, and external low `ecause` bits only; no
  interrupt controller device exists, and typed timer events are software events;
- block/input/network: no emulator device exists, so the capability table marks
  them explicitly unsupported instead of claiming drivers.

Any future MMIO serial, framebuffer, block, or input device requires an
emulator device model and later physical hardware definition. It must not be
faked as a register range in NPK code.

## 12. Task management and scheduling

NPK begins with cooperative multitasking:

- a task control block holds state, stack, saved context, and address space;
- states are `NEW`, `READY`, `RUNNING`, `BLOCKED`, and `EXITED`;
- ready tasks are held explicitly, not discovered from emulator state;
- `SYS_YIELD` becomes a real scheduler entry point in NPK;
- context switching saves/restores the software context and `ptbr`.

Preemptive scheduling is not claimed until the timer interrupt path is
implemented and tested end to end. The reference `SYS_YIELD` currently just
halts the machine; that is a limitation of the host model, not NPK scheduling.

Processes and threads remain distinct concepts in the architecture. Phase 2
may define a single task abstraction first, but the documentation must state
which model is implemented.

## 13. Executable-loading architecture

The NPK loader consumes `.nob` images because that is the implemented Nova
executable format. Its responsibilities will be:

1. validate magic, version, flags, alignment, size, and entry range;
2. reject unsupported flags rather than guessing their meaning;
3. allocate and copy image bytes into the target address space;
4. validate permissions for code and data regions;
5. initialize a stack;
6. set the entry PC and privilege;
7. define termination and status reporting;
8. reject relocations or dynamic features, which `.nob` does not carry.

There is no dynamic loader, shared library format, debug-container-based ELF
replacement, or userland shell in Phase 2. `.nod` and `.nop` are reserved names
with no implemented format.

## 14. Debugging facilities

NPK uses existing reference-model observability:

- `machine.io_log` for Phase 1 reference write calls and the NPK syscall write
  log at `0x9FE90` for Phase 2 kernel syscalls;
- `machine.irq_log` and `machine.ipi_log` for interrupt/IPI evidence;
- `machine.exception_stack()` for host-side platform save-stack inspection;
- `nova.npk.traps` for decoding the kernel-written software frame, trap state,
  and trap receipts;
- `nova-objdump` and `nova.disassembler` for image inspection;
- `nova.nox` debug containers for source/line and call-frame metadata;
- `nova-run --dump-regs` and host-side register reads;
- boot markers for deterministic early-boot assertions;
- the generated QEMU interpreter image and guest-memory dump comparison for
  harness-only boot validation, not hardware or native-QEMU evidence.

No GDB stub, hardware trace, or in-kernel debugger is claimed.

## 15. Security boundaries

The privilege levels alone do not guarantee isolation. Enforcement depends on:

- machine-only instruction and CSR checks;
- paged translation with U-bit checks;
- kernel pages never being user-accessible;
- syscall argument and pointer validation;
- deterministic fault handling for invalid access;
- explicit address-space switching with full TLB flush today.

Milestone 1 runs in bare mode, so it has **no user/kernel memory isolation**.
That limitation must remain visible until MMU-backed isolation is implemented
and tested. Host-side CSR setup APIs bypass privilege checks and are for tests
only; they are not kernel APIs.

## 16. Failure handling and panic policy

Early NPK failures are reported through a deterministic panic path:

1. record a panic reason in a reserved memory word;
2. emit a short message through the console backend when available;
3. stop with `HALT`.

Panic behavior becomes testable in the boot milestone as a defined error state
rather than an implicit exception. Until then, a boot helper that sees an
unexpected emulator exception fails the test instead of silently passing.

## 17. Future extension points

The following are architecture placeholders, not implemented features:

- machine-mode shim and supervisor-kernel operation after delegation exists;
- ASID-tagged address spaces and TLB shootdown;
- MMIO device model, interrupt controller, block and input devices;
- preemptive scheduling with a tested timer path;
- IPC ports/messages;
- dynamic executable formats and shared libraries;
- weak symbols, linker scripts, dead-code elimination, and relaxation;
- physical hardware RTL and conformance validation on silicon.

None of these belongs to the first NPK foundation milestone.
