# Porting readiness assessment

**Question:** is the current Nova codebase ready for (a) a real C compiler and
libc, (b) a Nova64 kernel, (c) a port of the existing OpenWindows kernel to
arm64/nova64, and (d) hardware?

**Short answer:** the *ISA definition* is ready to be targeted. The *codebase*
is not yet ready to build a GCC/LLVM-class compiler or to host a kernel port.
It has a stable encoding, ABI, syscall table, MMU, exception model and a
conformance suite — but it lacks the assembler/linker/debug infrastructure a
real compiler expects, and it has no boot, device, or SMP platform contract.
OpenWindows is an x86-64/PE kernel; nothing in this repository targets arm64.

This document is an audit, not a promise of work.

## 1. What exists today (verified in this tree)

| Capability | Evidence |
| --- | --- |
| Frozen ISA v1.0, 83 instructions, 8 formats, 20 CSRs | `nova/isa.py`, `README.md` |
| Authoritative tables drive assembler, disassembler, emulator and docs | `validate_table()` at import; generated README blocks |
| 64-bit-only profile with enforcement | `PROFILE`, `EXECUTION_MODES`, import-time validation |
| MMU: 4-level paging, 4 KiB/2 MiB/1 GiB pages, stage 2, permissions, faults, TLB flush | `nova/emulator.py`, CSRs `ptbr`/`tval`/`s2ptbr` |
| Atomics, SC memory model, FENCE, aq/rl | A format, `AMO*` handlers |
| Interrupts, timer, IPI, multicore cluster | `ie`/`ip`/`ivec`/`time`/`timecmp`, `SIPI`, `NovaCluster` |
| Virtualization with two-dimensional translation | `VMLAUNCH`/`VMRESUME`/`VMEXIT`, `vpc`/`vptbr`/`s2ptbr` |
| Object capabilities + stable identities + NOBO v1 | `nova/objfmt.py`, `OBJ.*` |
| ABI v1.0 calling convention + standard syscall table | `README.md` section 15, `nova/syscalls.py` |
| Assembler data directives | `.byte`, `.hword`, `.word`, `.quad`, `.asciz`, `.space`, `.align`, `.section`, `.global`/`.globl` |
| Relocation kinds | `PCREL_DISP14`, `LA64`, `ABS64` in `nova/object.py` |
| Containers and tools | `.nob` v1, `.nobj` v1, `nova-as`/`nova-ld`/`nova-objdump`/`nova-run` |
| Verification | 1,789 tests, 18/18 conformance vectors, `compileall` |

## 2. Readiness matrix

| Target | Ready? | Why |
| --- | --- | --- |
| Define a C target / write a backend plan | **Yes** | Encoding, ABI, relocations and instruction selection are concrete; `docs/COMPILER_BACKEND.md` already maps IR ops to Nova instructions |
| Build a GCC/LLVM-class C compiler here | **No** | No Nova LLVM/GCC target in this tree, no LLVM source or build integration, and several prerequisites below are missing |
| Build a small freestanding C compiler + libc | **Conditionally yes** | Possible as a scoped project after Phase 0; needs a defined C subset and a freestanding libc |
| Run a Nova64 kernel in the emulator | **Conditionally yes** | MMU, exceptions, interrupts and syscalls exist; no boot protocol, device model or SMP startup contract |
| Port OpenWindows to Nova64 | **No (later project)** | It is C99 + x86 asm, PE/OWX, `int 0x80`, PML4/ACPI/APIC, NASM; every one of those needs a Nova equivalent |
| Port OpenWindows to arm64 | **No (separate project)** | Requires an AArch64 HAL (GIC, MMU, EL exceptions), UEFI boot, aarch64 toolchain; Nova code contributes nothing directly |
| Hardware core | **No artifact yet** | Conformance suite is the contract; no RTL exists |

## 3. Blocking gaps for a real compiler

1. **No large-immediate instruction.** There is no `LUI`; constants need the
   nine-word `LI` expansion. Position-independent code and GOT access have no
   defined model.
2. **Branch range.** ±8191 bytes from the branch instruction, and `nova-ld`
   performs no relaxation. Large generated functions will not link.
3. **Relocation set is minimal.** Only `PCREL_DISP14`, `LA64`, `ABS64`. No GOT,
   PLT, TLS, weak/global-dynamic, or copy relocations; no dynamic linker.
4. **Debug format.** `.nox` is a reserved name with no defined layout, so no
   DWARF/source-level debugging.
5. **Assembly syntax gaps.** No `.type`, `.size`, `.cfi`, `.file`, `.loc`,
   macro/include facilities, or conditional assembly — compiler-generated
   assembly and hand-written runtime startup usually need these.
6. **Calling convention details.** Integer-only `a0`-`a5`; no defined struct
   return, varargs alignment beyond the 16-byte stack rule, or floating-point
   convention (the base ISA has no FP).
7. **Runtime library.** No libgcc-equivalent builtins (128-bit divide, `mulhi`,
   stack probing), no freestanding libc, no startup files.
8. **Linker features.** No weak symbols, linker scripts, dead-code elimination,
   PIE, or shared libraries.

## 4. Blocking gaps for a kernel

- **Boot contract.** No firmware/bootloader interface, no kernel image header
  beyond `.nob`, no device tree, no MMIO or I/O-device architecture.
- **Privilege routing.** All traps and interrupts enter machine privilege;
  there is no trap delegation to supervisor, so a supervisor kernel needs a
  machine-mode shim.
- **SMP.** `NovaCluster` is a cooperative host-side helper; no architectural
  startup protocol for secondary harts, no TLB shootdown semantics, no
  cache-coherence or memory-mapped interrupt controller definition.
- **I/O.** Syscalls exist, but there is no serial/framebuffer/block/network
  device ABI, no clocksource beyond `time`, and no interrupt-source identity
  beyond software/timer/external bits.
- **Userspace.** No ELF-like user executable format, dynamic loader, or process
  address-space layout convention; `.nod` is reserved but unspecified.
- **arm64 specifically.** Nothing in this repository targets arm64; porting
  OpenWindows there is an independent HAL/toolchain/bootloader project.

## 5. What is genuinely ready

The architectural contract is stable and machine-checked: instruction
encodings, CSR behavior, exception entry/return, MMU faults, atomics,
interrupts, objects, ABI and syscall numbers are all pinned by tests and by
18 hardware-independent conformance vectors. That is exactly the foundation a
compiler and kernel need *before* code generation begins. Starting compiler or
kernel work now would mean designing against a moving target; that risk is
gone.

## 6. Recommended sequence

| Phase | Deliverable | Gate to proceed |
| --- | --- | --- |
| 0. Toolchain hardening | Branch relaxation or long-branch sequences, GOT/PIC relocations, weak symbols, `.type`/`.size`/`.cfi`, `.nox` debug container | A generated C test object links and runs |
| 1. Compiler + libc | Freestanding C-subset compiler emitting Nova assembly, plus a minimal `libnova` (start files, syscall wrappers, printf/mem/string) | Compile and run a nontrivial C program on `nova-run` |
| 2. Nova64 kernel | Boot protocol in the emulator, machine-mode trap shim, supervisor kernel, MMU userspace, scheduler, minimal device ABI, userland libc | Run a userspace process through syscall/MMU path |
| 3. OpenWindows Nova64 slice | Nova HAL, syscall bridge, OWX loader port, using Phases 1-2 | One OpenWindows subsystem boots and runs |
| 4. OpenWindows arm64 | Independent AArch64 HAL, UEFI boot, toolchain | Separate project; not derived from Nova |
| 5. Hardware | Synthesizable core validated against the conformance vectors | RTL passes all 18 vectors |

## 7. Verdict

**Ready to design against. Not ready to compile or boot against yet.** The
highest-value next work is Phase 0/1, not a direct kernel port: a compiler needs
a linker that can handle generated code, and a kernel needs a compiler and a
boot contract. OpenWindows arm64 and hardware are explicitly later, separate
efforts.
