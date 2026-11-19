# NPK — Nova Processed Kernel architecture

**Status:** authoritative Phase 2 architecture baseline.  
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
interrupt events, system-call requests, memory-management operations, device
events, task transitions, and executable-loading requests. In Milestone 1 the
only implemented operation is boot verification; the other listed classes are
architecture targets, not implemented features.

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

| Component | Responsibility | Milestone 1 status |
| --- | --- | --- |
| Operation producers | Create typed work from boot/trap/syscall/device/loader events | Boot producer only |
| `NpkOperation` | Carries operation code, input, output, status, and stage cursor | Implemented in C |
| Processing unit `PU0` | Owns the active operation and drives stage advancement | Implemented in C |
| Stage functions | Implement the five stage invariants separately | Implemented for boot verification |
| Receipt memory | Publishes stage trace and terminal result for host observation | Implemented at `0x90008` |
| Processing coordinator | Submits work and checks the terminal result | Implemented in `npk_main` |
| Scheduler | Chooses which unit/operation receives CPU time | Not implemented |

Milestone 1 source layout separates the kernel entry from this mechanism:
`nova/npk/src/start.s` establishes the machine context, `nova/npk/src/processing.c`
implements the operation pipeline and receipt, and `nova/npk/src/kernel.c`
submits and checks the boot verification operation.

### Relationship between processing and scheduling

Processing and scheduling are different mechanisms:

- scheduling decides **which** ready work occupies a CPU;
- processing defines **how** an operation becomes a committed state change;
- a task may be blocked or resumed at a processing-stage boundary, but its
  stage progress is part of the operation record, not an implicit call stack;
- a scheduler must not skip a stage or promote an unvalidated operation.

Milestone 1 has a fixed boot operation and therefore no scheduler. Cooperative
and later preemptive scheduling in Milestones 6 and 8 will select operations for
processing units; they will not replace the pipeline.

### Interaction with other subsystems

| Subsystem | Interaction with processing |
| --- | --- |
| Exceptions | A trap vector classifies the event and admits an exception operation; software frame restoration happens after the operation commits or fails. |
| Memory | `VALIDATE` declares address/range/permission requirements; `EXECUTE` performs mapped operations; a fault re-enters processing as a fault event. |
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

Milestone 1 guarantees only these properties for boot verification. It does not
guarantee transactional rollback, isolation, preemption, multiprocessing, or
general syscall/device processing.

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
4. There is no boot firmware, device tree, MMIO device, or kernel syscall hook
   inside `NovaMachine`. A host harness dispatches reference syscalls today.
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
foundations needed for a reproducible boot image. Empty directories for the
other subsystems are deliberately not created.

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
4. calls `npk_main`, which submits and checks the boot verification operation;
5. executes `HALT` with `npk_main`'s return value in `a0`.

`HALT` is the Phase 2 termination mechanism. `SYS_EXIT` remains an ABI syscall
for future user processes, but there is no kernel dispatcher yet.

### 5.4 Initialization order

The intended order as subsystems are added is:

1. architecturally required boot state (`sp`, `vbar`);
2. boot marker and minimal debug state;
3. Core initialization and panic formatting support;
4. physical memory and kernel allocator;
5. exception/trap frame support;
6. syscall dispatcher;
7. device abstraction and timer;
8. scheduler;
9. executable loader and first user process.

Each milestone may implement only the next coherent prefix of this order.

## 6. Kernel memory layout

Milestone 1 runs in bare translation mode (`ptbr = 0`), so virtual and physical
addresses are identical. The initial layout is:

| Region | Address | Purpose |
| --- | --- | --- |
| Kernel image | `0x1000` upward | Code and data loaded from `.nob` |
| Exception vector | link-computed inside the image | `vbar` target installed by `_npk_start` |
| Boot stack | top `0x80000`, grows down | Kernel stack before a real allocator exists |
| Boot marker | `0x90000` | 8-byte handshake word written by `_npk_start` |
| Unmapped/unused | elsewhere | No RAM bound is modeled by the emulator |

The boot marker value is `0x4E504B31` (`"NPK1"`). It proves that the kernel
entry ran and that the host loaded the image at the expected base. It is a
debug/handshake mechanism, not a kernel API.

Future address-space policy, to be enforced once MMU support lands:

- low canonical half: kernel direct map and kernel heap;
- high canonical half: user code, data, heap, and stacks;
- user PTEs require the U bit and explicit R/W/X permissions;
- kernel PTEs are never marked user-accessible.

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

## 8. Exception architecture

### 8.1 Hardware entry

On a trap, `NovaMachine` writes `ecode`, `ecause`, and `epc`, forces machine
privilege, pushes the internal frame, and jumps to `vbar` or `ivec`. GPRs are
unchanged. Synchronous faults use the faulting PC as `epc`; `SCALL` and `TRAP`
use `PC + 4`.

### 8.2 Software trap frame

NPK owns its trap frame in kernel memory. The planned layout is:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 256 | `gpr[0..31]`, each 8 bytes little-endian |
| 256 | 8 | `psr` |
| 264 | 8 | `epc` |
| 272 | 8 | `ecause` |
| 280 | 8 | `ecode` |
| 288 | 8 | `previous_priv` |
| 296 | 8 | `tval` |
| 304 | — | total size, 16-byte aligned |

The exception handler must preserve every register it clobbers. Hardware does
not do this. The frame is not implemented in Milestone 1; it is the scope of a
later exception milestone.

### 8.3 Dispatch

Epoch 1 exception dispatch will classify by `ecode` and, for `SYSTEM_CALL`, by
`ecause`:

- `ILLEGAL_INSTRUCTION`, `ARITHMETIC`, `ALIGNMENT_FAULT`, `PRIVILEGE`,
  `OBJECT_FAULT`, `MMU_FAULT`, `SOFTWARE_TRAP`, `VM_FAULT`: fault handlers;
- `SYSTEM_CALL`: syscall dispatcher;
- `INTERRUPT`: source-specific handler using `ecause` as the source id.

`MRET` returns to `epc` and clears `ecode`; `ecause` is not restored, so the
handler must not assume it is zero after return.

## 9. Memory management architecture

The existing translator is real and must be reused, not duplicated:

- `ptbr[63]` enables four-level translation; `bits[51:12]` hold the root PPN;
- leaf at level 3 = 4 KiB, level 2 = 2 MiB, level 1 = 1 GiB;
- level 0 leaves are reserved;
- PTE bits: valid `0x1`, R `0x2`, W `0x4`, X `0x8`, U `0x10`, global `0x20`,
  accessed `0x40`, dirty `0x80`;
- faults report `MMU_FAULT` with details for missing, permission, reserved,
  noncanonical, and misaligned entries, and write the address to `tval`;
- `TLBI` and `ptbr` writes flush translations; the TLB is a functional cache,
  not a hardware-exact structure.

NPK will add, in order:

1. a physical page allocator over a declared RAM range;
2. a kernel page-table manager that constructs PTEs for Nova's format;
3. a virtual address-space manager with explicit kernel/user policies;
4. mapping, unmapping, protection, and fault handling;
5. `TLBI` policy wrappers and address-space switching.

Platform gaps to handle explicitly: no RAM-size model, no MMIO regions, no
ASID, no TLB shootdown, and an ignored global PTE bit. These are limitations to
design around, not behavior to invent.

## 10. System-call architecture

The syscall ABI stays unchanged: numbers `0..7`, arguments in `a0`-`a5`,
return in `a0`, error `-1` in `a0` with an error code in `a1`, and the call
number delivered as `ecause` on `SYSTEM_CALL`.

The future in-kernel dispatcher will:

1. verify `0 <= ecause <= 7`;
2. verify argument count and ranges;
3. validate user pointers against the active address space and permissions;
4. execute the operation;
5. convert failures to the existing error convention;
6. return through the software trap frame and `MRET`.

The host-side `nova.syscalls.dispatch_syscall()` remains the reference-model
helper used by Phase 1 tests. It is not an NPK kernel feature and it currently
reads buffers directly rather than through translation.

## 11. Device architecture

NPK separates generic interfaces from concrete backends:

- console: generic write handle implemented initially over `SYS_WRITE`, which
  the reference model records in `machine.io_log`;
- timer: `time` and `timecmp` CSRs plus timer interrupts and `ie`/`ip`/`ivec`;
- interrupt sources: software, timer, and external bits only; no interrupt
  controller device exists;
- block/input/network: no emulator device exists, so no driver is claimed.

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

- `machine.io_log` for write syscalls;
- `machine.irq_log` and `machine.ipi_log` for interrupt/IPI evidence;
- `machine.exception_stack()` for host-side trap-frame inspection;
- `nova-objdump` and `nova.disassembler` for image inspection;
- `nova.nox` debug containers for source/line and call-frame metadata;
- `nova-run --dump-regs` and host-side register reads;
- boot markers for deterministic early-boot assertions.

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
