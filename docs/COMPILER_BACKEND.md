# Porting a C compiler to Nova ISA v1.0 — implementation plan

**Status:** plan only. No LLVM or GCC backend exists in this repository yet. The
Nova toolchain today assembles hand-written assembly; this document describes
the concrete work needed to compile C to Nova machine code.

## 1. Target facts a backend must respect

| Property | Value |
| --- | --- |
| Execution mode | One mode, `Nova64`; 64-bit registers, addresses, and references |
| Registers | 32 GPRs, `r0` hardwired zero; aliases `sp=r29`, `fp=r30`, `ra=r31` |
| ABI arguments | `a0`-`a5` = `r1`-`r6`; `a0` also returns values |
| ABI temporaries | `t0`-`t3` = `r7`-`r10` (caller-saved) |
| ABI saved | `s0`-`s3` = `r11`-`r14` (callee-saved) |
| Stack | Grows down; 16-byte aligned at call boundary; `sp` points at next free slot |
| Instruction size | Fixed 32-bit little-endian, 4-byte aligned |
| Branch displacement | Signed 14-bit **bytes**, PC-relative to the branch instruction |
| Call/return | `CALL` stores `pc+4` in `ra`; `RET` jumps through `ra` |
| Condition handling | Branches compare registers directly; they do not read PSR flags |
| Flags | Only `CMP`/`CMPI` write `Z N C V` in `psr`, readable with `CSRR` |
| Large constants | No `LUI`; `LI` pseudo expands to up to nine `MOVI`/`SHLI`/`ORI` words |
| Atomics | `AMOADD`/`AMOSWAP`/`AMOCAS`/`AMOAND`/`AMOOR`/`AMOXOR`, 8-byte aligned |
| Privilege | Compiler-generated code runs in user mode; system code needs `CSRR`/`CSRW` |

## 2. LLVM backend milestones

### Milestone A — Target description

- Add `llvm/lib/Target/Nova`: `NovaTargetMachine`, `NovaTargetObjectFile`,
  `NovaISelLowering`, `NovaInstrInfo`, `NovaRegisterInfo`, `NovaFrameLowering`,
  `NovaSubtarget`, `NovaCallingConv`.
- Declare the 32 GPRs in `NovaRegisterInfo.td` with one register class `GPR`
  (all 64-bit), and encode `r0` as a zero register that the allocator never
  assigns.
- Add `NovaCallingConv.td`: arguments/returns in `a0`-`a5`, `ra` as the return
  address, `sp`/`fp` special, `s0`-`s3` callee-saved, all other registers
  caller-saved.
- Define one instruction class per Nova format (`R`, `I`, `M`, `J`, `S`, `O`,
  `X`, `A`) with fields matching `nova/isa.py`, so the assembler emitted by the
  backend is always canonical.

### Milestone B — Instruction selection

Lower common LLVM IR operations as follows:

| IR operation | Nova sequence | Notes |
| --- | --- | --- |
| `add`, `sub`, `mul` | `ADD`, `SUB`, `MUL` (or `ADDI`/`SUBI`/`MULI`) | Low 64 bits; wrap is defined |
| `sdiv`, `srem` | `DIV`, `REM` | Divide-by-zero traps; gate if language requires defined behavior |
| `udiv`, `urem` | `UDIV`, `UREM` | Unsigned variants |
| `and`, `or`, `xor` | `AND`, `OR`, `XOR` / immediates | |
| `shl`, `lshr`, `ashr` | `SHL`, `SHR`, `SAR` / immediates | Shift count masked to six bits |
| `fshl`, `fshr` | `ROTL`, `ROTR` | Register rotate amount |
| load/store | `LDB/U`, `LDH/U`, `LDW/U`, `LDD`, `STB/H/W/D` | Select by type width; sign/zero extension is explicit in the mnemonic |
| `icmp eq/ne` | `BEQ`/`BNE` using the branch form | No separate compare needed |
| `icmp slt/sgt/sle/sge` | `BLT`/`BGT`/`BLE`/`BGE` | Signed |
| `icmp ult/ugt/ule/uge` | `BLTU`/`BGTU`/`BLEU`/`BGEU` | Unsigned |
| materialized `psr` flags | `CMP rs1, rs2` then `CSRR rd, psr` | Only if IR observes the flags |
| `br` / `ret` | `JMP` / `RET` | Indirect targets must be 4-byte aligned |
| `call` | `CALL` for PC-relative, `JMP` through a register for indirect | `ra` is clobbered by `CALL` |
| atomic add/swap/cas/and/or/xor | Corresponding `AMO*` with `aq`/`rl` bits | Address must be 8-byte aligned |
| `fence` / `atomicrmw` barriers | `FENCE` | Full SC barrier |
| `trap` | `TRAP` / `SCALL` | `SCALL` number from the syscall table |

Because branches never consume flags, the backend should select conditions
directly on register operands. This avoids the artificial `CMP` + branch
pattern used on flag machines and removes a whole class of bugs.

### Milestone C — Constants and relocations

- Implement `NovaTargetLowering::lowerConstant()` by emitting the canonical
  `LI` expansion: `MOVI` for values in `[0, 16383]`, otherwise `MOVI` of the
  high chunk followed by `SHLI rd, rd, 13` / `ORI rd, rd, chunk` pairs, bounded
  at nine words. Reuse `nova.pseudo.li_maximal()` as the reference sequence.
- Map relocations to the containers already specified:
  - `PCREL_DISP14` for branches and `CALL`; the backend must verify the
    displacement fits signed 14-bit bytes after layout and insert no-op
    padding only via explicit relaxation, which `nova-ld` does not currently do.
  - `LA64` for address constants; the linker rewrites a fixed nine-word block.
  - `ABS64` for `.quad symbol`.
- Emit `.nobj` sections (`.text`, `.rodata`, `.data`, `.bss`) with the alignment
  and symbol binding rules in README section 17.1; `nova-ld` then produces a
  `.nob` executable without a new linker backend.

### Milestone D — Frame layout and prologues/epilogues

- 16-byte stack alignment at every call; reserve frame space and save `ra` and
  callee-saved registers with `STD`/`LDD` (8-byte aligned).
- Use `fp` as an optional frame pointer; `sp` points to the next free slot.
- Stack probing must be explicit: large frames should touch each page because
  Nova has no hardware stack-probe instruction.
- Varargs follow the register file: the first six integer arguments in
  `a0`-`a5`, further arguments on the stack in ascending address order.

### Milestone E — Verification

1. Compile a C test corpus to Nova assembly with `llc -march=nova`.
2. Assemble with `nova-as`, link with `nova-ld`, and run with `nova-run`.
3. Compare results with native execution of the same corpus.
4. Run `python -m nova.conformance` to ensure the backend did not rely on
   behavior outside the architectural contract.

## 3. GCC backend notes

A GCC port (`gcc/config/nova/`) follows the same target facts:

- Define `TARGET_CPU_CPP_BUILTINS` for `__nova__`, `GPR` register class, and
  `TARGET_64BIT` (Nova has no 32-bit mode).
- Implement `nova.md` patterns with predicates for register, 14-bit signed
  immediate, 14-bit unsigned immediate, and 6-bit shift amount. Use the same
  instruction selection table as LLVM.
- `TARGET_CALLING_CONVENTION` and `nova.h` must encode the ABI registers,
  16-byte stack alignment, and `ra` linkage.
- Emit `.nobj` assembly directly or emit GNU-as-like text translated by a thin
  script that calls `nova.as.assemble_text`; there is no GNU binutils target.

## 4. Runtime prerequisites

A C program needs more than instruction selection:

- A start file defining `_start` that initializes `sp`, clears `.bss`, and calls
  `main`. `nova-ld` already requires `_start` to be defined exactly once.
- Syscall wrappers using the table in `nova/syscalls.py` (`SCALL imm`).
- A minimal libc: `write`, `exit`, `time`, memory allocation (bump allocator or
  `SYS_OBJ_CREATE`), and printf built on `SYS_WRITE`.
- No dynamic linker exists; all programs are statically linked by `nova-ld`.

## 5. Risks and open decisions

- **No `LUI`-style instruction.** Address and constant materialization costs up
  to nine instructions; PIC code should prefer `LA64` relocations.
- **Branch range.** ±8191 bytes from the branch instruction; large functions
  need explicit relaxation or long-branch sequences, which the linker does not
  yet provide.
- **No floating point or vectors in the base ISA.** FP code needs a software
  library or a future ISA extension.
- **Atomic width.** Nova atomics are 64-bit and 8-byte aligned; 32-bit atomic
  patterns must be widened by the backend.
- **Privilege.** Compiler output assumes user mode; privileged instructions
  and CSR writes are rejected below machine privilege.
- **Debug information.** No debug container is specified; DWARF output needs a
  `.nox` format definition before source-level debugging is possible.

## 6. Suggested order of work

1. LLVM target description and register/calling-convention definitions.
2. Integer ISel and frame lowering; compile and run integer-only C programs.
3. Relocation and `.nobj` emission; switch from hand-written assembly to
   compiler-generated objects.
4. Syscall wrappers and a minimal libc.
5. Atomics and FENCE lowering.
6. GCC backend, if a second independent compiler is desired.
7. Debug format and optional FP/vector extensions, only if required later.
