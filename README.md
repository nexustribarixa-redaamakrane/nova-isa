# Nova ISA v1.0 — Architectural Specification

**Version:** Nova ISA v1.0
**Status:** Frozen base architecture. `nova/isa.py` is the normative machine-readable contract; this document is its human-readable form.
**Reference implementation:** `nova` Python package (emulator, assembler, disassembler, linker, object and executable containers, command-line tools).

Nova is an original, independent, **64-bit-only** instruction-set architecture. This repository specifies the architectural contract exposed to software: what a conforming Nova processor must do when executing Nova machine code. It does not specify a CPU product, microarchitecture, cache hierarchy, pipeline, or silicon implementation.

`nova/isa.py` is the single source of truth. The assembler, disassembler, emulator, and the generated tables below are derived from it. `tests/test_documentation.py` fails if this document and the code disagree.

## Version history

| Version | Change |
| --- | --- |
| v1.0 | Frozen base architecture. Added a concrete four-level virtual-memory translator with 4 KiB, 2 MiB, and 1 GiB pages, permissions and a defined TLB; the finalized capability-based object model with stable, versioned cross-process identities; a hardware-independent architecture conformance suite; 64-bit atomics with acquire/release ordering; the interrupt architecture (software, timer, external sources; `ie`, `ip`, `ivec`, `time`, `timecmp`; `SIPI`, `IRET`); the virtualization extension V; multicore architectural requirements; the full Nova ABI v1.0; rotate and unsigned-divide instructions; and explicit 64-bit-only profile enforcement. Replaced the former "experimental" and "deliberately incomplete" sections with normative definitions. |
| v0.3-experimental | Added the X format and `CSRR`/`CSRW`, a defined CSR file, the exception save stack, pseudo-instructions, the `.nob`/`.nobj` containers, the linker, the command-line tools, and the trap ABI. |
| v0.2-experimental | Fixed the 32-bit instruction encoding, 32 GPRs, flat memory, branches, `CALL`/`RET`, `TRAP`/`SCALL`, and experimental object handles. |
| v0.1-experimental | Initial register/immediate arithmetic, memory access, and exception model. |

## 1. Scope and identity

Nova is not a renamed ARM, x86-64, RISC-V, MIPS, PowerPC, or WebAssembly. It uses established computer-architecture concepts where appropriate, but its instruction definitions, architectural state, encoding, privilege model, memory model, and object model are defined here as Nova.

Out of scope for this specification: CPU families or product models, microarchitectures, caches, pipelines, branch predictors, out-of-order execution, transistor or silicon designs, clock rates, and marketing. Floating-point, vector, and debug extensions are reserved but are not part of the base architecture.

## 2. The 64-bit-only profile

Nova has exactly one execution mode, named `Nova64`. There is no 32-bit compatibility mode, no narrower address mode, and no alternate register width. The 32-bit instruction word is an encoding decision, not a 32-bit execution mode.

| Property | Normative definition |
| --- | --- |
| Native integer / register width | 64 bits |
| Virtual address width | 64 bits; 48-bit canonical form when paging is enabled |
| Physical address width | 64 bits in bare mode; translations produced by the page-table walker are limited to 52 bits |
| Pointer / object reference width | 64 bits |
| CSR register width | 64 bits (individual CSRs may mask narrower fields) |
| Instruction width and alignment | 32 bits; 4-byte aligned |
| Endianness | Little-endian for instructions and data |
| GPR count | 32 |
| CSR count | 32; indices 0–19 are assigned, 20–31 are reserved |
| Privilege values | user `0`, supervisor `1`, machine `2`; `3` is reserved |
| Page sizes | 4 KiB, 2 MiB, and 1 GiB; base page is 4096 bytes |
| Page-table depth | 4 levels of 512 entries |
| Reset entry point and vector base | `0x1000` |
| Interrupt vector base | `0x2000` |
| Exception save stack depth | 4 frames |

`nova.isa.PROFILE` publishes these values, and `validate_table()` rejects any table that contradicts the single 64-bit mode at import time.

## 3. Architectural state

### 3.1 General-purpose registers

`r0` through `r31` are 64-bit registers. Reads of `r0` return zero and writes to it are discarded. The architectural aliases are `zero = r0`, `sp = r29`, `fp = r30`, and `ra = r31`. The ABI aliases defined in section 15 are `a0`–`a5`, `t0`–`t3`, and `s0`–`s3`.

### 3.2 Control and status registers

`CSRR rd, csr` reads a CSR into a GPR and `CSRW csr, rs1` writes a GPR into a CSR. The `csr` operand is written by name or as an explicit index; a disassembler renders an assigned index as its name and a reserved index as `#index`.

<!-- BEGIN GENERATED: csr_table -->
| Index | Name | Width | Reset | Access | Alignment | Purpose |
| ---: | --- | ---: | --- | --- | --- | --- |
| `0` | `psr` | 4 | `0x0` | user read, user write | any value | Processor status. `Z`=bit 0, `N`=bit 1, `C`=bit 2, `V`=bit 3, written by `CMP` and `CMPI`. Bits 63:4 are reserved: they read as zero and discard writes. |
| `1` | `epc` | 64 | `0x0` | machine read, machine write | 4-byte aligned writes | Saved program counter of the active exception; the address that `MRET` returns to. |
| `2` | `ecause` | 64 | `0x0` | machine read, machine write | any value | Exception detail: faulting address, immediate, CSR index, or offending privilege value depending on `ecode`. |
| `3` | `ecode` | 64 | `0x0` | machine read, machine write | any value | Active exception code. `NONE` means no exception is active. `MRET` sets it to `NONE`. |
| `4` | `priv` | 2 | `0x0` | user read, machine write | any value | Current privilege level: 0 user, 1 supervisor, 2 machine. Level 3 is reserved; writing it raises PRIVILEGE. |
| `5` | `vbar` | 64 | `0x1000` | machine read, machine write | 4-byte aligned writes | Exception vector base address used by exception entry. |
| `6` | `coreid` | 64 | `0x0` | user read, read-only | any value | Architectural identity of the executing processor. The reference model is single-processor, so it reads 0; a Nova cluster assigns one unique id per hart. |
| `7` | `ptbr` | 64 | `0x0` | machine read, machine write | any value | Page-table base and paging mode. Bit 63 enables the four-level translator; bits 51:12 hold the root page number. Reset 0 selects bare mode (virtual == physical). Writing it flushes every TLB entry. |
| `8` | `tval` | 64 | `0x0` | machine read, machine write | any value | Trap value: the faulting virtual address for MMU_FAULT. |
| `9` | `ie` | 3 | `0x0` | machine read, machine write | any value | Interrupt enable mask. Bit 0 software, bit 1 timer, bit 2 external. Higher bits are reserved (WARL). |
| `10` | `ip` | 3 | `0x0` | machine read, machine write | any value | Interrupt pending mask, same bit positions as `ie`. Set by SIPI, the timer, the host, or an external source. |
| `11` | `ivec` | 64 | `0x2000` | machine read, machine write | 4-byte aligned writes | Interrupt vector base address used on interrupt entry. |
| `12` | `time` | 64 | `0x0` | user read, read-only | any value | Monotonic 64-bit time counter; incremented once per retired instruction by the reference model. |
| `13` | `timecmp` | 64 | `0xffffffffffffffff` | machine read, machine write | any value | Timer compare value. When `time` reaches `timecmp`, the timer interrupt pending bit is set. All ones disables it. |
| `14` | `vpc` | 64 | `0x0` | machine read, machine write | any value | Guest program counter used by VMLAUNCH and VMRESUME. |
| `15` | `vpriv` | 2 | `0x0` | machine read, machine write | any value | Guest privilege level (0 user, 1 supervisor, 2 machine) entered by VMLAUNCH. |
| `16` | `vptbr` | 64 | `0x0` | machine read, machine write | any value | Guest page-table base, in the same format as `ptbr`. |
| `17` | `vexit` | 64 | `0x0` | machine read, machine write | any value | VM exit reason, written on every guest exit. |
| `18` | `hstatus` | 1 | `0x0` | machine read, machine write | any value | Hypervisor status. Bit 0 is set while guest execution is active; other bits are reserved (WARL). |
| `19` | `s2ptbr` | 64 | `0x0` | machine read, machine write | any value | Stage-2 (guest-physical to host-physical) page-table base. Same format as `ptbr`: bit 63 enables stage-2 translation and bits 51:12 hold the root page number. Reset 0 disables stage 2, preserving the flat guest behavior. Writing it flushes every TLB entry. |
<!-- END GENERATED: csr_table -->

Rules that apply to every CSR:

- **WARL.** Writes to reserved bits are discarded and those bits read as zero. `psr` has a mask of `0xF`, `priv` `0x3`, `ie`/`ip` `0x7`, `vpriv` `0x3`, and `hstatus` `0x1`.
- **Reserved indices.** Indices 20–31 are WARL: reads return zero and writes are discarded. This lets a future revision assign one without changing the meaning of existing streams.
- **Privilege.** Executing `CSRR`/`CSRW` below the privilege required by the CSR raises PRIVILEGE with `ecause` equal to the CSR index. Machine access is always permitted.
- **Alignment.** Writing a misaligned `epc`, `vbar`, or `ivec` raises ALIGNMENT_FAULT with `ecause` equal to the rejected value; the CSR keeps its old contents.
- **Read-only.** Writing `coreid` or `time` raises PRIVILEGE. `coreid` reads the hart's architectural identity; the reference model has one hart and reads zero.
- **Paging side effects.** Writing `ptbr` flushes every TLB entry. `TLBI` flushes all entries or one page.
- **Host access.** `NovaMachine.read_csr`/`write_csr` are host-side and bypass privilege, WARL, and alignment checks for test setup. Only executed instructions are checked.

PSR bits are `Z=0`, `N=1`, `C=2`, and `V=3`. `CMP` and `CMPI` replace these four bits with the flags of a 64-bit subtraction of the left operand minus the right operand: `C` means no unsigned borrow and `V` is signed subtraction overflow. No other instruction modifies them, and branches never consume them.

### 3.3 The program counter

`pc` is architectural state outside the CSR file. Reset sets it to `0x1000`. `CALL` writes `pc + 4` to `ra`, and every other transfer of control is described in the instruction tables. Instruction addresses must be 4-byte aligned; the reference model raises ALIGNMENT_FAULT rather than wrapping.

### 3.4 Exception and interrupt state

The exception state is the `epc`, `ecause`, `ecode`, `vbar`, and `priv` CSRs plus a depth-4 save stack of `(epc, ecode, ecause, previous_priv)` frames. Interrupt state is the `ie`, `ip`, `ivec`, `time`, and `timecmp` CSRs. Object state is the capability table described in section 13. There is no other software-visible state.

## 4. 64-bit execution model

Register width, virtual address width, and physical address width are separate concepts, but Nova fixes them as follows. Registers, addresses, pointers, references, and CSRs are all 64 bits. In bare mode, a virtual address is used unchanged as a physical address over the full 64-bit space, which is what makes downward-growing stacks below address zero work. When paging is enabled, virtual addresses use the 48-bit canonical form (bits 63:48 must all equal bit 47) and translations may produce physical addresses of at most 52 bits.

Integer operations use the low 64 bits of the mathematical result; add, subtract, multiply, and negate never raise overflow. `DIV`/`REM` interpret operands as signed 64-bit values; `UDIV`/`UREM` interpret them as unsigned. Shift counts are the low six bits of the register operand, or an immediate in `0..63`. Rotates use the low six bits as well. Comparisons are performed directly on register values; signed and unsigned branch variants are distinct instructions. Loads sign- or zero-extend sub-word values to 64 bits as named by their mnemonic.

## 5. Instruction encoding

Every instruction is one little-endian 32-bit word. The high 8 bits are a unique opcode; there is no secondary function field. Register and CSR indices are unsigned 5-bit values. Unless a definition says otherwise, reserved bits must encode zero, and any non-canonical encoding raises ILLEGAL_INSTRUCTION.

| Format | Layout, from bit 31 down to bit 0 | Used by |
| --- | --- | --- |
| R | `opcode[31:24] rd[23:19] rs1[18:14] rs2[13:9] rsv9[8:0]` | Register arithmetic, logic, comparison, object comparison |
| I | `opcode[31:24] rd[23:19] rs1[18:14] imm14[13:0]` | Immediate arithmetic, rotates, TLB invalidation |
| M | `opcode[31:24] rdata[23:19] rbase[18:14] disp14[13:0]` | Loads and stores |
| J | `opcode[31:24] rca[23:19] rcb[18:14] disp14[13:0]` | Branches and control transfer |
| S | `opcode[31:24] rsv10[23:14] uimm14[13:0]` | System, traps, interrupts, virtualization |
| O | `opcode[31:24] rd[23:19] rs1[18:14] rsv14[13:0]` | Object/reference operations |
| X | `opcode[31:24] rd[23:19] csr[18:14] rs1[13:9] rsv9[8:0]` | Control/status register access |
| A | `opcode[31:24] rd[23:19] rs1[18:14] rs2[13:9] aq[8] rl[7] rsv7[6:0]` | Atomic memory operations |

`CMP` requires `rd = 0`; `MOV`, `NOT`, and `NEG` require `rs2 = 0`; `CMPI` requires `rd = 0`; `MOVI` requires `rs1 = 0`; `OBJ.DROP` requires `rs1 = 0`; `TRAP`, `CALL`, `SIPI`, and `VMEXIT` require the unused register fields to be zero; `RET`, `MRET`, `IRET`, `FENCE`, `WAIT`, `HALT`, `VMLAUNCH`, and `VMRESUME` require every non-opcode field to be zero; `JMP` requires `rcb = disp14 = 0`; `CSRR` requires `rs1 = 0`; `CSRW` requires `rd = 0`; and `TLBI` requires `rd = imm14 = 0`. For an A-format instruction, `aq` and `rl` are each a one-bit unsigned operand; values greater than one are rejected by the assembler.

`NOP` is opcode `0x00`, and every non-opcode field of `NOP` is reserved, so the all-zero word is the only valid `NOP` encoding. A word with opcode `0x00` and any other bit set is not an instruction.

### 5.1 Opcode allocation

<!-- BEGIN GENERATED: opcode_table -->
| Opcodes | Purpose |
| --- | --- |
| `0x00` | `NOP` |
| `0x01-0x04` | R format: rotates and unsigned division |
| `0x05-0x0A` | A format: atomic memory operations |
| `0x0B-0x0F` | reserved |
| `0x10-0x1E` | R format: integer, logic, comparison |
| `0x1F` | reserved |
| `0x20-0x2A` | I format: immediate arithmetic and logic |
| `0x2B-0x2C` | I format: rotate immediates |
| `0x2D` | I format: TLB invalidation |
| `0x2E-0x2F` | reserved |
| `0x30-0x36` | M format: loads |
| `0x37-0x3F` | reserved |
| `0x40-0x43` | M format: stores |
| `0x44-0x4F` | reserved |
| `0x50-0x5D` | J format: branches and control transfer |
| `0x5E-0x5F` | reserved |
| `0x60-0x69` | S format: system, interrupts, virtualization |
| `0x6A-0x6F` | reserved |
| `0x70-0x75` | O/R format: capability-based object/reference operations |
| `0x76-0x7F` | reserved |
| `0x80-0x81` | X format: control/status register access |
| `0x82-0xFF` | reserved |
<!-- END GENERATED: opcode_table -->

### 5.2 Immediate and address rules

Most 14-bit immediates are signed two's-complement values in `[-8192, 8191]`. `MOVI` is the exception: its immediate is unsigned in `[0, 16383]` and zero-extends to 64 bits. The shift-immediate and rotate-immediate instructions accept only `0..63`; register shift and rotate counts use their low six bits. The `aq` and `rl` operands accept only `0` or `1`.

Memory effective addresses are `base + sign_extend(disp14)`. Loads, stores, and atomics must be naturally aligned to their access size (1, 2, 4, or 8 bytes); otherwise they raise ALIGNMENT_FAULT. An access that leaves the unsigned 64-bit address space raises MEMORY_FAULT, and address arithmetic does not wrap. The address-range check precedes the alignment check.

Conditional branches compare the register encoded in `rca` with the register encoded in `rcb`. Their signed 14-bit displacement is in **bytes** and is added to the address of the branch instruction, not to the following instruction. Branch and call targets must be four-byte aligned. `JMP` is an indirect jump through `rca`; `CALL` is PC-relative and stores `pc + 4` in `ra`; `RET` jumps through `ra`.

## 6. Defined instructions

This table is generated from `nova.isa.INSTRUCTION_TABLE` and is the authoritative list of what this revision defines.

<!-- BEGIN GENERATED: instruction_table -->
| Mnemonic | Opcode | Format | Operands | Privilege | Flags | Exceptions | Summary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `NOP` | `0x00` | S | `no operands` | user | - | - | No operation; `PC` advances by four. The all-zero word is the only valid NOP encoding. |
| `ROTL` | `0x01` | R | `rd, rs1, rs2` | user | - | - | `rd = rotl64(rs1, rs2[5:0])`; rotate left. |
| `ROTR` | `0x02` | R | `rd, rs1, rs2` | user | - | - | `rd = rotr64(rs1, rs2[5:0])`; rotate right. |
| `UDIV` | `0x03` | R | `rd, rs1, rs2` | user | - | ARITHMETIC | Unsigned `rd = rs1 / rs2`; divide by zero raises ARITHMETIC. |
| `UREM` | `0x04` | R | `rd, rs1, rs2` | user | - | ARITHMETIC | Unsigned `rd = rs1 % rs2`; divide by zero raises ARITHMETIC. |
| `AMOADD` | `0x05` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic `mem64[rs1] += rs2`; `rd` is the old value. Alignment 8, aq/rl order the operation. |
| `AMOSWAP` | `0x06` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic `mem64[rs1] = rs2`; `rd` is the old value. |
| `AMOCAS` | `0x07` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic compare-and-swap: if `mem64[rs1] == rd` write `rs2`; `rd` always receives the observed old value. |
| `AMOAND` | `0x08` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic `mem64[rs1] &= rs2`; `rd` is the old value. |
| `AMOOR` | `0x09` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic `mem64[rs1] |= rs2`; `rd` is the old value. |
| `AMOXOR` | `0x0A` | A | `rd, rs1, rs2, aq, rl` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT, MMU_FAULT | Atomic `mem64[rs1] ^= rs2`; `rd` is the old value. |
| `ADD` | `0x10` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 + rs2`, low 64 bits of the sum. |
| `SUB` | `0x11` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 - rs2`, low 64 bits of the difference. |
| `MUL` | `0x12` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 * rs2`, low 64 bits of the product. |
| `DIV` | `0x13` | R | `rd, rs1, rs2` | user | - | ARITHMETIC | Signed `rd = trunc(rs1 / rs2)`; `MIN / -1` wraps to `MIN`. |
| `REM` | `0x14` | R | `rd, rs1, rs2` | user | - | ARITHMETIC | Signed `rd = rs1 - trunc(rs1 / rs2) * rs2`, sign of the dividend. |
| `AND` | `0x15` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 & rs2`. |
| `OR` | `0x16` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 | rs2`. |
| `XOR` | `0x17` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 ^ rs2`. |
| `SHL` | `0x18` | R | `rd, rs1, rs2` | user | - | - | `rd = rs1 << rs2[5:0]`; zeros shifted in. |
| `SHR` | `0x19` | R | `rd, rs1, rs2` | user | - | - | Logical `rd = rs1 >> rs2[5:0]`; zeros shifted in. |
| `SAR` | `0x1A` | R | `rd, rs1, rs2` | user | - | - | Arithmetic `rd = rs1 >> rs2[5:0]`; sign bit replicated. |
| `CMP` | `0x1B` | R | `rs1, rs2` | user | ZNCV | - | Set `Z N C V` from `rs1 - rs2`; no register written. |
| `MOV` | `0x1C` | R | `rd, rs1` | user | - | - | `rd = rs1`. |
| `NOT` | `0x1D` | R | `rd, rs1` | user | - | - | `rd = ~rs1`. |
| `NEG` | `0x1E` | R | `rd, rs1` | user | - | - | `rd = -rs1`, two's complement. |
| `ADDI` | `0x20` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 + sext(simm)`. |
| `SUBI` | `0x21` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 - sext(simm)`. |
| `MULI` | `0x22` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 * sext(simm)`. |
| `ANDI` | `0x23` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 & sext(simm)`. |
| `ORI` | `0x24` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 | sext(simm)`. |
| `XORI` | `0x25` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 ^ sext(simm)`. |
| `SHLI` | `0x26` | I | `rd, rs1, simm` | user | - | - | `rd = rs1 << simm[5:0]`; assembler requires 0..63. |
| `SHRI` | `0x27` | I | `rd, rs1, simm` | user | - | - | Logical `rd = rs1 >> simm[5:0]`; requires 0..63. |
| `SARI` | `0x28` | I | `rd, rs1, simm` | user | - | - | Arithmetic `rd = rs1 >> simm[5:0]`; requires 0..63. |
| `CMPI` | `0x29` | I | `rs1, simm` | user | ZNCV | - | Set `Z N C V` from `rs1 - sext(simm)`; no register written. |
| `MOVI` | `0x2A` | I | `rd, uimm` | user | - | - | `rd = zext(uimm)` for uimm in 0..16383. Cannot load a full 64-bit constant. |
| `ROTLI` | `0x2B` | I | `rd, rs1, simm` | user | - | - | `rd = rotl64(rs1, simm[5:0])`; requires 0..63. |
| `ROTRI` | `0x2C` | I | `rd, rs1, simm` | user | - | - | `rd = rotr64(rs1, simm[5:0])`; requires 0..63. |
| `TLBI` | `0x2D` | I | `rs1` | machine | - | - | Invalidate translations: `rs1 = 0` flushes every entry, otherwise only the entry for virtual address `rs1` is flushed. |
| `LDB` | `0x30` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = sext(mem8[rbase + sext(disp14)])`. |
| `LDH` | `0x31` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = sext(mem16[rbase + sext(disp14)])`. |
| `LDW` | `0x32` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = sext(mem32[rbase + sext(disp14)])`. |
| `LDD` | `0x33` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = mem64[rbase + sext(disp14)]`. |
| `LDBU` | `0x34` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = zext(mem8[rbase + sext(disp14)])`. |
| `LDHU` | `0x35` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = zext(mem16[rbase + sext(disp14)])`. |
| `LDWU` | `0x36` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `rd = zext(mem32[rbase + sext(disp14)])`. |
| `STB` | `0x40` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `mem8[rbase + sext(disp14)] = rd[7:0]`. |
| `STH` | `0x41` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `mem16[rbase + sext(disp14)] = rd[15:0]`. |
| `STW` | `0x42` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `mem32[rbase + sext(disp14)] = rd[31:0]`. |
| `STD` | `0x43` | M | `rd, mem` | user | - | MEMORY_FAULT, ALIGNMENT_FAULT | `mem64[rbase + sext(disp14)] = rd`. |
| `JMP` | `0x50` | J | `reg_a` | user | - | ALIGNMENT_FAULT, MEMORY_FAULT | `PC = reg_a`. |
| `BEQ` | `0x51` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a == reg_b`. |
| `BNE` | `0x52` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a != reg_b`. |
| `BLT` | `0x53` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a < reg_b`, signed. |
| `BLE` | `0x54` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a <= reg_b`, signed. |
| `BGT` | `0x55` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a > reg_b`, signed. |
| `BGE` | `0x56` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a >= reg_b`, signed. |
| `BLTU` | `0x57` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a < reg_b`, unsigned. |
| `BLEU` | `0x58` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a <= reg_b`, unsigned. |
| `BGTU` | `0x59` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a > reg_b`, unsigned. |
| `BGEU` | `0x5A` | J | `reg_a, reg_b, target` | user | - | ALIGNMENT_FAULT | Branch if `reg_a >= reg_b`, unsigned. |
| `CALL` | `0x5B` | J | `target` | user | - | ALIGNMENT_FAULT | `ra = PC + 4`; `PC = PC + sext(disp14)`. |
| `RET` | `0x5C` | J | `no operands` | user | - | ALIGNMENT_FAULT | `PC = ra`. |
| `TRAP` | `0x5D` | J | `uimm` | user | - | - | Raise SOFTWARE_TRAP with `ecause = zext(disp14)` and `epc = PC + 4`. |
| `SCALL` | `0x60` | S | `uimm` | user | - | - | Raise SYSTEM_CALL with `ecause = uimm` and `epc = PC + 4`. |
| `MRET` | `0x61` | S | `no operands` | machine | - | ALIGNMENT_FAULT, ILLEGAL_INSTRUCTION | Pop one exception frame: `PC = epc`, `priv = frame.priv`, `ecode = 0`, `ecause` unchanged. |
| `FENCE` | `0x62` | S | `no operands` | user | - | - | Full memory barrier: every load, store, and atomic before FENCE is ordered before every one after it, on every hart. Nova v1.0 is sequentially consistent for ordinary memory; FENCE is the explicit synchronization point for conforming relaxed implementations. |
| `WAIT` | `0x63` | S | `no operands` | machine | - | - | Wait for an enabled, pending interrupt: retire normally when one is pending, otherwise stop execution until a host or hart raises one. |
| `HALT` | `0x64` | S | `no operands` | machine | - | - | Stop architectural execution; no further architectural activity occurs without explicit host restart. |
| `IRET` | `0x65` | S | `no operands` | machine | - | ALIGNMENT_FAULT, ILLEGAL_INSTRUCTION | Return from interrupt: pop one exception frame and restore `pc`, `priv`, and `ecode` exactly as MRET does. |
| `SIPI` | `0x66` | S | `uimm` | machine | - | PRIVILEGE | Send a software interrupt to the hart identified by `uimm`; the target's `ip` software bit becomes pending. |
| `VMLAUNCH` | `0x67` | S | `no operands` | machine | - | PRIVILEGE, VM_FAULT | Enter guest execution from `vpc`/`vpriv`/`vptbr`, saving host state; guest traps cause a VM exit. |
| `VMRESUME` | `0x68` | S | `no operands` | machine | - | PRIVILEGE, VM_FAULT | Resume guest execution after a VM exit using the saved host state. |
| `VMEXIT` | `0x69` | S | `uimm` | machine | - | PRIVILEGE, VM_FAULT | Return from guest to host with `ecause = uimm`; restore host `pc`, `priv`, and page table base. |
| `OBJ.CREATE` | `0x70` | O | `rd, rs1` | user | - | - | Create an object whose prototype metadata size is `rs1`; `rd = new reference`. |
| `OBJ.GET` | `0x71` | O | `rd, rs1` | user | - | OBJECT_FAULT | `rd = metadata size of the object referenced by rs1`. |
| `OBJ.SET` | `0x72` | O | `rd, rs1` | user | - | OBJECT_FAULT | Set the metadata size of the object referenced by `rs1` to `rd`. |
| `OBJ.DROP` | `0x73` | O | `rd` | user | - | OBJECT_FAULT | Destroy the object referenced by `rd`; the reference becomes invalid. |
| `OBJ.PERM` | `0x74` | O | `rd, rs1` | user | - | OBJECT_FAULT | `rd = permission mask of the object referenced by rs1`; the mask uses the OBJECT_PERM_* bits. |
| `OBJ.CMP` | `0x75` | R | `rd, rs1, rs2` | user | - | - | `rd = 1` when `rs1` and `rs2` name the same live object, else `0`; invalid references do not fault. |
| `CSRR` | `0x80` | X | `rd, csr` | user | - | PRIVILEGE | `rd = CSR[csr]`. Reserved CSR indices read as zero. |
| `CSRW` | `0x81` | X | `csr, rs1` | user | - | PRIVILEGE, ALIGNMENT_FAULT | `CSR[csr] = rs1`. Reserved bits are discarded (WARL); misaligned EPC/VBAR writes and reserved privilege values fault. |
<!-- END GENERATED: instruction_table -->

### 6.1 Integer, logic, rotate, and comparison

Add, subtract, multiply, and negate keep the low 64 result bits. `DIV` and `REM` use signed operands, truncate toward zero, and take the remainder's sign from the dividend. `UDIV` and `UREM` use unsigned operands. A zero divisor raises ARITHMETIC with detail `0` for divide and `1` for remainder. Signed minimum divided by `-1` wraps to the signed minimum. `SHR` is logical, `SAR` sign-fills, and `ROTL`/`ROTR` rotate through the carry-free 64-bit word. Signed and unsigned branch instructions compare registers directly and ignore the PSR flags.

### 6.2 Memory

The reference model provides sparse, zero-initialized, byte-addressable memory backed by fixed-size pages, with the translation layer of section 9 in front of it. `NovaMachine.load_code` writes a raw image at an address and leaves the program counter there. Loads and stores are byte, halfword, word, or doubleword in width and must be naturally aligned.

### 6.3 System, exceptions, and control

`MRET`, `IRET`, `WAIT`, `HALT`, `TLBI`, `SIPI`, and the virtualization instructions require machine privilege; executing them below machine privilege raises PRIVILEGE with `ecause` equal to the opcode. `FENCE`, `SCALL`, and `TRAP` are available at every privilege level.

## 7. Memory architecture

Nova memory is byte-addressed and sparse. The architectural state consists of a virtual address space and, when paging is enabled, a page table that maps virtual pages to physical pages.

- **Bare mode.** Reset state. `ptbr[63] = 0`. Virtual addresses are physical addresses; no translation, permissions, or page faults apply. The full 64-bit address space is usable, which permits stacks that grow below address zero.
- **Paged mode.** `ptbr[63] = 1`. Virtual addresses must be canonical 48-bit values. Four levels of 512 entries translate a virtual page number to a physical page number. `ptbr[51:12]` is the physical page number of the root table. A PTE with any of the read, write, or execute bits set is a leaf: at level 3 it maps a 4 KiB page, at level 2 a 2 MiB page, and at level 1 a 1 GiB page. A level-0 leaf is reserved. The leaf's physical page number must be aligned to its page size. A translation whose reserved PTE bits are set is illegal.
- **PTE format.** Bit 0 is valid, bit 1 readable, bit 2 writable, bit 3 executable, bit 4 user-accessible, bit 5 global, bit 6 accessed, bit 7 dirty, and bits 51:12 are the physical page number. Bits 63:52 are reserved and must be zero. The reference model sets accessed on every successful translation and dirty on every write.
- **Permissions.** A load requires readable, a store or atomic requires writable, and instruction fetch requires executable. When `priv = 0`, the page must also be user-accessible; supervisor and machine may access user pages. Any other combination raises MMU_FAULT.
- **Faults.** MMU_FAULT saves the faulting virtual address in `tval`. Its `ecause` detail is `0` for a missing or invalid translation, `1` for a permission violation, `2` for reserved PTE bits, `3` for a non-canonical address, and `4` for a leaf whose physical page number is misaligned to its page size. A physical address out of range raises MEMORY_FAULT.
- **TLB.** Translations may be cached. The architecture exposes no TLB state; it exposes only the effects of `TLBI` (which flushes all entries when `rs1 = 0`, otherwise the entry for that virtual page) and the rule that writing `ptbr` flushes every entry. An implementation may use any internal mechanism that honors those rules.
- **Alignment.** Loads, stores, and atomics must be naturally aligned; misalignment raises ALIGNMENT_FAULT after the address-range check.

## 8. Memory consistency and atomics

Nova v1.0 has a **sequentially consistent** memory model for all ordinary loads, stores, and atomic operations: there is a single total order of memory operations, consistent with each hart's program order, in which every load observes the most recent store to that location. Atomics are indivisible in that order. The reference model realizes this order directly because it executes one architectural instruction at a time with shared memory.

`FENCE` is a full barrier: all memory operations before it are ordered before all memory operations after it, on every hart. A conforming implementation with a relaxed internal model must preserve this observable ordering; the explicit model makes `FENCE` a synchronization point rather than a decorative no-op.

An A-format instruction reads the old 64-bit value, applies its update, and writes the new value as one indivisible action. The address must be 8-byte aligned. `AMOCAS` treats the incoming value of `rd` as the expected value and always returns the observed old value in `rd`, so a caller can test success by comparing the result with the expected value. The `aq` bit requests acquire semantics (later accesses cannot move before the atomic) and the `rl` bit requests release semantics (earlier accesses cannot move after it); setting both requests acquire-release. In the sequentially consistent reference model these bits are already satisfied, but they constrain conforming relaxed implementations. The most recent atomic's ordering bits are exposed to the host as `NovaMachine.last_amo_ordering`.

## 9. Virtual memory interface

The architectural MMU contract is the page-table format and fault behavior in section 7. `ptbr`, `tval`, and `TLBI` are the only software-visible MMU controls. The root table base is a physical page number; the walker reads PTEs as 64-bit little-endian words. For a virtual address, level 0 uses bits 47:39, level 1 bits 38:30, level 2 bits 29:21, and level 3 bits 20:12; bits 11:0 are the 4 KiB page offset. An intermediate PTE must be valid and have no read/write/execute bit set. A leaf at level 3 provides all 12 offset bits, a leaf at level 2 replaces bits 20:12 with physical address bits (2 MiB page), and a leaf at level 1 replaces bits 29:12 (1 GiB page). A level-0 leaf and a misaligned leaf physical address are illegal translations. Restart after a fault is the faulting instruction: exception entry saves the faulting PC (not `pc + 4`) for synchronous memory faults.

## 10. Privilege architecture

The three privilege levels are user `0`, supervisor `1`, and machine `2`. Reset enters machine privilege at `0x1000`; `nova-run` starts at machine privilege by default. `priv` is user-readable and machine-writable; writing `3` raises PRIVILEGE. All synchronous traps and interrupts enter machine privilege and jump to `vbar` or `ivec` respectively, so a machine-mode handler can delegate in software if it chooses. There is no separate delegation CSR in the base architecture. Supervisor mode exists so software can structure a kernel, but the base architecture routes its traps to machine mode.

## 11. Interrupts

There are three architectural interrupt sources, in descending delivery priority: software (bit 0), timer (bit 1), and external (bit 2). `ie` enables sources and `ip` records pending ones; both are machine-writable so firmware and the host can raise and mask interrupts. `time` increments once per retired instruction and the timer bit becomes pending when `time` reaches `timecmp` and `timecmp` is not all ones. `SIPI uimm` sends a software interrupt to the hart whose `coreid` equals `uimm`; sending to a hart that does not exist has no other effect. `nova.emulator.NovaCluster` can route IPIs between hart instances.

Between instructions, if any enabled source is pending in `ip`, the machine takes an interrupt: it clears that source's pending bit, saves a frame with `epc` equal to the next instruction's address and `ecode = INTERRUPT`, switches to machine privilege, and jumps to `ivec`. The handler returns with `IRET`, which has the same frame-pop semantics as `MRET`. `WAIT` retires normally when an enabled interrupt is pending and stops the machine otherwise. `FENCE` orders memory operations before raising or acknowledging an interrupt.

## 12. Virtualization (extension V)

Virtualization is an optional architectural extension that is implemented in the reference model. While `hstatus[0] = 0`, execution is normal. `VMLAUNCH` saves the host program counter, privilege, and page-table base, then loads `vpc`, `vpriv`, and `vptbr` into the active state and sets `hstatus[0]`. `VMRESUME` re-enters a guest after an exit without overwriting the saved host state. `VMEXIT uimm` writes the reason to `vexit`, restores the host state, and clears `hstatus[0]`.

While a guest is active, any trap that would otherwise enter the host handler instead causes a VM exit: `vexit` receives the trap code, `ecause` receives its detail, `ecode` is cleared, and the host resumes after its entry instruction. The host can then inspect `vexit` and decide whether to inject the event into the guest or handle it itself. A nested `VMLAUNCH` or a `VMRESUME` with no saved host state raises VM_FAULT.

### 12.1 Two-dimensional translation

When `s2ptbr[63] = 1`, address translation inside a guest is **two-dimensional**. Stage 1 uses the guest page table (`vptbr`, loaded into `ptbr` on entry) to map a guest virtual address to a guest-physical address. Stage 2 uses `s2ptbr` to map that guest-physical address to a host physical address. Both stages use the same four-level table, page sizes, PTE format, and fault codes. The effective access must be permitted by both stages; the user bit is checked by stage 1 only, so a hypervisor cannot accidentally grant a guest user access by setting a stage-2 user bit. Stage-2 PTE reads use host physical addresses directly, and stage-1 PTE reads themselves pass through stage 2, so guest page tables cannot escape stage-2 isolation. A fault in either stage raises MMU_FAULT with the original guest virtual address in `tval`; when a guest is active that trap becomes a VM exit with `vexit = MMU_FAULT`, letting the host inspect `tval` and decide how to respond.

With `s2ptbr[63] = 0`, stage 2 is an identity map and the guest sees the flat behavior of earlier revisions. Writing `ptbr` or `s2ptbr` flushes all TLB entries, including stage-2 entries. The reference model walks stage-2 translations on each access rather than caching them in hardware; this is architecturally invisible because only the flush behavior is specified.

## 13. Objects and references

Nova objects are an architectural concept, not a language feature. An object is a record in a machine-local capability table with a 64-bit opaque reference, a size, and six independent permission bits: read (bit 0), write (bit 1), execute (bit 2), grant (bit 3), drop (bit 4), and metadata (bit 5). `OBJ.PERM` reports the mask. References are never reused within a machine instance, so validity is decided by membership in the capability table; reference zero is null.

- `OBJ.CREATE rd, rs1` creates an object with size `rs1` and the full permission set, returning its reference in `rd`.
- `OBJ.GET rd, rs1` reads the size of the object in `rs1`; it requires read permission.
- `OBJ.SET rd, rs1` writes `rd` as the size of the object in `rs1`; it requires write permission.
- `OBJ.DROP rd` destroys the object in `rd`; it requires drop permission, and the reference becomes invalid.
- `OBJ.PERM rd, rs1` reports the permission mask of the object in `rs1`.
- `OBJ.CMP rd, rs1, rs2` writes `1` when both operands name the same live object, otherwise `0`; invalid references do not fault.

The read, write, and drop bits are checked by `OBJ.GET`, `OBJ.SET`, and `OBJ.DROP` respectively; the execute, grant, and metadata bits are reported by `OBJ.PERM` and are reserved for future extensions of the object model. OBJECT_FAULT detail is `0` for a null reference, `1` for an invalid reference, and `2` for a permission violation. Object references are not memory addresses: they do not participate in address translation and are not readable or writable as data. Objects do not themselves grant access to memory; a kernel may associate a memory region with an object in software.

A reference is a **64-bit persistent identity**, not a machine-local table slot. Bits 31:0 hold an object index and bits 63:32 hold a generation; both start at 1 and neither is ever reused. A fresh `OBJ.CREATE` receives a new generation, so a stale reference can never resolve to a later object even if indices are reused by another implementation. Local handles and persistent identities are the same value: the `OBJ.*` instructions accept the 64-bit reference, and `export_objects()` preserves it exactly.

### 13.1 External object format

`NovaMachine.export_objects()` writes a deterministic, versioned binary container and `NovaMachine.import_objects(data, on_duplicate=...)` reads it. The container is not an executable or a memory image; it describes identities only.

| Field | Encoding | Meaning |
| --- | --- | --- |
| Magic | 4 bytes, `NOBO` | Identifies the container |
| Version | 2-byte little-endian unsigned | Format version, currently 1 |
| Flags | 2-byte little-endian unsigned | Must be zero |
| Count | 4-byte little-endian unsigned | Number of records |
| Reserved | 4-byte little-endian unsigned | Must be zero |
| Records | 32 bytes each, sorted by reference | `reference`, `size`, `permissions`, `flags` (four little-endian 64-bit words) |
| Checksum | 4-byte little-endian CRC-32 | Covers the header and records |
| Reserved | 4-byte little-endian unsigned | Must be zero |

Import validates the container and every record: unknown versions, bad magic, length or checksum mismatches, malformed references (zero generation or index), duplicate references, unknown permission bits, and unknown flags are errors. A record with the external-reference flag does not create an object: it must resolve to an object already present with matching size and permissions, otherwise the import reports an unresolved or inconsistent reference. For a reference that already exists, `on_duplicate` selects `error` (default), `skip`, or `replace`. Import advances the local index and generation allocators past every imported reference so a later local allocation cannot collide with one that was imported. `object_generation_seed` on `NovaMachine` lets independent machines use disjoint generation spaces before exchanging containers. Version 1 is the only accepted version; a higher version is rejected and a lower version is rejected as obsolete, so compatibility never depends on silent reinterpretation.

## 14. Exceptions

Exception entry performs, in order:

1. Push `(epc, ecode, ecause, priv)` onto the save stack, where `epc` is the return address (`pc + 4` for `SCALL` and `TRAP`, otherwise the faulting instruction address) and `priv` is the privilege level in effect before entry.
2. Discard the oldest frame if the stack is already at its depth of 4.
3. Write `ecode`, `ecause`, `epc`, and `priv = machine`.
4. Set `pc = vbar` (or `ivec` for an interrupt).

`MRET` pops one frame, sets `pc` to the frame's `epc`, restores the frame's privilege level, and clears `ecode` to `NONE`. It does not modify `ecause`, so a handler can inspect the cause after returning. `MRET` with an empty save stack raises ILLEGAL_INSTRUCTION; a misaligned `epc` raises ALIGNMENT_FAULT. A handler that re-enters without repointing `vbar` will loop on the same fault, which is a software bug, not an architectural ambiguity.

| Code | Name | Raised by |
| ---: | --- | --- |
| 0 | `NONE` | No active exception; also the value `MRET` writes to `ecode` |
| 1 | `ILLEGAL_INSTRUCTION` | Undefined or non-canonical encoding; `MRET`/`IRET` with an empty save stack |
| 2 | `ARITHMETIC` | `DIV`/`REM`/`UDIV`/`UREM` by zero; `ecause` is `0` for divide, `1` for remainder |
| 3 | `MEMORY_FAULT` | Data access or control transfer leaving the 64-bit address space |
| 4 | `ALIGNMENT_FAULT` | Misaligned data access, control transfer, or CSR write |
| 5 | `PRIVILEGE` | Privileged instruction below machine privilege, forbidden CSR access, or a reserved `priv` value |
| 6 | `OBJECT_FAULT` | Null, invalid, or permission-denied object reference |
| 7 | `SYSTEM_CALL` | `SCALL`; `ecause` is the unsigned immediate |
| 8 | `INTERRUPT` | Enabled pending interrupt; `ecause` is the source bit |
| 9 | `MMU_FAULT` | Missing, illegal, or forbidden translation; `tval` is the faulting address |
| 10 | `SOFTWARE_TRAP` | `TRAP`; `ecause` is the unsigned immediate |
| 11 | `VM_FAULT` | Illegal virtualization state transition on `VMLAUNCH`, `VMRESUME`, or `VMEXIT` |

## 15. Nova ABI v1.0

The ABI is a software convention layered on the ISA, not part of the machine contract.

- **Arguments and returns.** `a0`–`a5` (registers 1–6) carry the first six integer arguments. `a0` also carries the single integer return value.
- **Temporaries.** `t0`–`t3` (registers 7–10) are caller-saved scratch registers.
- **Saved registers.** `s0`–`s3` (registers 11–14) are callee-saved; a callee must restore them before returning.
- **Stack and frame.** `sp` (register 29) points to the next free slot. `fp` (register 30) is an optional frame pointer. The stack grows downward and must be 16-byte aligned at a call boundary. An 8-byte-aligned slot is sufficient for the two-register save area used by exception handlers.
- **Return address.** `CALL` writes `pc + 4` to `ra` (register 31); `RET` jumps through `ra`. Leaf functions may use `ra` directly.
- **System calls.** `SCALL imm` invokes system call number `imm`; arguments are in `a0`–`a5` and the return value is in `a0`. The handler reads `ecause` for the call number. A failed call returns `-1` in `a0` and an error code in `a1`.

### 15.1 Standard syscall table

`nova.syscalls` publishes "Nova syscall ABI v1.0", the standard call numbers below. They are a software convention layered on the ISA: a kernel may implement them, and `nova.syscalls.dispatch_syscall(machine)` demonstrates them for the reference model.

| Number | Name | Arguments (a0–a5) | Result | Meaning |
| ---: | --- | --- | --- | --- |
| `0` | `SYS_EXIT` | `code` | `never returns` | Terminate the calling hart's program with the given exit code. |
| `1` | `SYS_WRITE` | `fd, buffer, count` | `bytes written` | Write ``count`` bytes from virtual address ``buffer`` to handle ``fd``. Handle assignment is implementation-defined; the reference model records writes in ``machine.io_log``. |
| `2` | `SYS_READ` | `fd, buffer, count` | `bytes read` | Read up to ``count`` bytes into ``buffer`` from handle ``fd``. |
| `3` | `SYS_TIME` | `none` | `time` | Return the current value of the ``time`` CSR. |
| `4` | `SYS_OBJ_CREATE` | `size` | `object reference` | Create an object of ``size`` bytes; equivalent to OBJ.CREATE. |
| `5` | `SYS_OBJ_SIZE` | `reference` | `size` | Return the size of a live object; equivalent to OBJ.GET. |
| `6` | `SYS_OBJ_DESTROY` | `reference` | `zero or error` | Destroy a live object; equivalent to OBJ.DROP. |
| `7` | `SYS_YIELD` | `none` | `zero` | Suggest that the implementation schedule another hart; the reference model halts until an interrupt is injected. |

Error codes in `a1` are `1` bad argument, `2` unsupported, `3` not found, and `4` no resource. The reference model implements the architectural calls (exit, time, object operations, yield) and records `SYS_WRITE` data in `machine.io_log`; `SYS_READ` is deliberately unsupported because input devices are outside the ISA.
- **Exception handlers.** No GPR is altered on entry; a handler that must preserve a register saves and restores it. The canonical save/restore template uses `sp` and `LDD`/`STD` and always repoints `vbar` before any operation that can fault again.

## 16. Assembly language and tools

Source is case-insensitive for mnemonics, register names, and CSR names. A semicolon starts a comment. Labels use letters, digits, `_`, `.`, or `$` and may share a line with an instruction. Immediates accept decimal, `0x`, `0b`, and `0o` literals with optional `_` separators and an optional `#` or `$` prefix. Memory operands are `[rN]`, `[rN + disp]`, or `[rN - disp]`. A branch or call target is a label or a signed byte displacement relative to that instruction. Directives are not defined by this revision and are rejected explicitly. The one exception is the pair of linking directives `.global` and `.globl`, accepted only when the assembler is building a relocatable object for a `.nobj` output name, where they mark the named symbol as global. Every malformed input raises `nova.AssemblyError` carrying the source line number, and no operand is ever silently substituted or truncated.

```asm
MOVI r1, 100
ADDI r2, r1, -4
STW r2, [sp + 8]
LDBU r3, [r4]
CMP r1, r2
BLT r1, r2, less
SCALL 7
less: RET
```

### 16.1 Pseudo-instructions

`LI rd, imm` and `LA rd, target` are assembler pseudo-instructions: they have no opcode, never appear in a `.nob` image, and are never produced by the disassembler. Each expands to a canonical sequence of real instructions whose length is deterministic for a given constant.

`LI rd, imm` loads an arbitrary signed or unsigned 64-bit constant. If `imm` fits an unsigned 14-bit immediate, it expands to a single `MOVI`; otherwise it expands to `MOVI rd, top` followed by one `SHLI rd, rd, 13` / `ORI rd, rd, chunk` pair per lower 13-bit chunk. Expansion length grows monotonically and is bounded at nine instructions. `LA rd, target` loads the absolute link address of `target` and expands exactly like `LI`; the assembler converges label offsets with a monotone fixed-point pass.

### 16.2 Python entry points

| Entry point | Purpose |
| --- | --- |
| `assemble_text(source, entry=0)` | Assemble source into a raw little-endian image |
| `assemble_program(lines, entry=0)` | Assemble an iterable of source lines |
| `assemble_line(text, pc)` | Assemble one instruction for a known address |
| `disassemble_word(word)` | Canonical text for one 32-bit word |
| `disassemble_bytes(data)` | Canonical text per word of an image |
| `disassemble_words(words)` | Decoded instructions, with `None` for undefined words |
| `disassemble_stream(data)` | Strict decode that raises on undefined words |
| `NovaMachine(code, entry=0x1000)` | Reference machine for architectural state |
| `NovaCluster` | Shared-memory cluster of harts with IPI routing |
| `INSTRUCTION_TABLE`, `CSR_TABLE`, `EXCEPTION_CODES` | The authoritative tables |
| `PROFILE`, `EXECUTION_MODES` | The single 64-bit execution profile |
| `objfmt` | Deterministic encoder/decoder for external object identities |
| `syscalls` | Standard syscall table and reference dispatcher |
| `CONFORMANCE_VECTORS`, `run_conformance_suite()`, `conformance_report()` | Architecture conformance suite |
| `validate_table()` | Raise if the ISA tables are not internally consistent |

`nova.isa` is import-safe: `validate_table()` runs at import, so an inconsistent instruction table is a startup error rather than a wrong answer later. The emulator performs the same coverage check between its handler table and the instruction table, so an instruction cannot be added without an implementation.

### 16.3 Command-line tools

Installing this package (`pip install -e .`) exposes four console scripts:

| Command | Purpose |
| --- | --- |
| `nova-as` | Assemble Nova assembly into a `.nob` executable, or a `.nobj` relocatable object with a `.nobj` output name |
| `nova-ld` | Link one or more `.nobj` objects into a `.nob` executable |
| `nova-objdump` | Print a `.nob` header and disassemble its image |
| `nova-run` | Load and execute a `.nob` on the reference emulator |

Each tool is also reachable as `python -m nova.as`, `python -m nova.ld`, `python -m nova.objdump`, and `python -m nova.run`; the console scripts and the module invocations run the identical implementation. Run any tool with `--help` for options and `--version` for the toolchain and ISA versions. If a console script reports an older version than this repository, an older non-editable install is shadowing it; reinstall with `python -m pip install -e .` from this directory.

A smoke session on `hello.no` (containing `MOVI r1, 42` and `HALT`):

```text
$ nova-as hello.no
nova-as: wrote hello.nob: 2 instruction(s), 40 bytes, image base 0x1000, entry 0x1000
$ nova-objdump hello.nob
Nova .nob executable (format v1)
  image_base: 0x0000000000001000
  entry:      0x0000000000001000
  flags:      0x0000
  image_size: 8

00001000: 2a08002a  MOVI r1, 42
00001004: 64000000  HALT
$ nova-run hello.nob
nova-run: halted after 2 step(s) at pc 0x1004, privilege machine
```

`nova-as` assembles at the image base and writes the exact image the assembler produced. `--image-base` (default `0x1000`) is the load address of image byte 0 and the link address used to resolve labels; `--entry` (default: the image base) is where execution starts. `nova-run` loads the image at its image base, starts at `entry`, and by default runs at machine privilege; `--privilege` starts lower, `--steps` bounds execution, and `--dump-regs` prints all 32 registers. `nova-objdump` validates the container and prints one canonical instruction per word.

A multi-file pipeline assembles each unit to a relocatable object, links them, then runs:

```text
$ nova-as -o main.nobj main.no
$ nova-as -o math.nobj math.no
$ nova-ld -o program.nob main.nobj math.nobj
nova-ld: wrote program.nob: 2 object(s), 72 byte(s), entry 0x1000, base 0x1000 (Nova Linker v0.1)
$ nova-run --dump-regs program.nob
nova-run: halted after 17 step(s) at pc 0x1038, privilege machine
  r0=0000000000000000  r1=0000000000000009  r2=0000000000001044  ...
```

Exit codes are stable across the tools: `0` success (for `nova-run`, the machine halted); `1` an input/assembly/container/run error; `2` a usage error (for `nova-run`, the step budget being reached). Diagnostics go to standard error with no tracebacks.

The toolchain version is separate from the ISA revision: `nova.TOOLCHAIN_VERSION` is `1.1.0`, the `.nob` container format is `NOB_VERSION` 1, and the software conventions are `nova.TRAP_ABI_VERSION`, "Nova ABI v1.0". Toolchain release `1.0.0` is the snapshot at commit `6ccc28b` (INITIAL COMMIT); `1.1.0` adds the standard syscall ABI, the compiler backend plan, and stage-2 virtualization.

### 16.4 Examples

The `examples/` directory is the canonical, tested suite. Each program assembles through both `nova-as` and the Python API, runs to a clean `HALT`, and leaves the documented register or object state.

| Program | Demonstrates | Left behind after `HALT` |
| --- | --- | --- |
| `hello.no` | Smallest program: one `MOVI`, one `HALT` | `r1 == 42` |
| `arithmetic.no` | `LI`, arithmetic, and the three register pairs | `r3 == 51`, `r5 == 91`, `r6 == 90` |
| `branches.no` | Register-to-register comparisons and a loop | `r3 == 11`, `r4 == 3` |
| `labels.no` | Named data labels and `LA` | `r3 == 12`, the loaded sum starts at `r1` |
| `exceptions.no` | `SCALL` into a `vbar` handler, `CSRR`/`CSRW`, `MRET`, the trap-ABI save/restore template | `r5 == ecode`, `r6 == ecause`, the exception stack empty |
| `objects.no` | `OBJ.CREATE`/`OBJ.GET`/`OBJ.SET`/`OBJ.DROP` | `r2` a valid reference, `r3 == 24`, `r5 == 40`, the object dropped |

The `examples/link/` directory is a two-unit linker demo: `main.no` calls a function and loads an address from `math.no` across a file boundary. It builds to a program that runs to a clean `HALT` leaving `r1 == 9`, `r5 == 9`, and `r6 == 0`.

## 17. File extensions and containers

These suffixes are registered format names, published as `nova.FILE_FORMATS`; `nova.get_file_format("sample.no")` looks up a recognized name. A recognized suffix does not imply that a compiler exists. `nova-as` writes `.nob` executables and, with a `.nobj` output name, relocatable objects; `nova-ld` links `.nobj` objects into a `.nob` executable; `nova-objdump` and `nova-run` read `.nob` executables. The `nova` object module reads and writes `.nobj` directly (`assemble_object`, `encode_object`/`decode_object`/`write_object`/`load_object`). The other names are reserved.

| Extension | Nova format | Intended contents | Current support |
| --- | --- | --- | --- |
| `.no` | Nova Assembly | Canonical assembly text | Source text for `assemble_text`; no file reader |
| `.nova` | Nova Assembly | Alternate assembly-text suffix | Source text for `assemble_text`; no file reader |
| `.nos` | Nova Source | Future high-level Nova language source | Name reserved; no compiler |
| `.noi` | Nova Include | Include/interface/stub declarations | Name reserved; no include processor |
| `.nol` | Nova Listing | Text listing with addresses and decoded instructions | Name reserved; no listing-file writer |
| `.inok` | Intermediate Nova Kompile | Versioned compiler intermediate representation | Magic/version bytes reserved; no IR |
| `.noo` | Nova Object | Relocatable Nova code/data and relocations | Name reserved; the implemented object format is `.nobj` |
| `.nobj` | Nova Object | Relocatable Nova code/data and relocations | Written by `nova-as -o *.nobj`; read by the `nova` object module; linked by `nova-ld` |
| `.nok` | Nova Kompile | Linked Nova executable container | Magic bytes reserved; no executable loader |
| `.nob` | Nova Binary | Nova executable container v1: 32-byte header (magic `NOBV`, version, flags, image base, entry, image size) followed by a little-endian word image | Written by `nova-as`; read by `nova-objdump` and `nova-run` |
| `.nod` | Nova Dynamic Library | Loadable Nova module | Magic bytes reserved; no module ABI or loader |
| `.nox` | Nova Debug | Debug symbols and source mapping | Magic bytes reserved; no debug writer |
| `.nom` | Nova Module Manifest | Imports, exports, and module metadata | Magic bytes reserved; no manifest parser |
| `.nop` | Nova Package | Distribution package of Nova artifacts | Magic bytes reserved; no package manager |

`.noo`, `.nod`, `.nok`, and the other container names are host-independent Nova format identifiers: they are not native Windows PE/COFF, ELF, or Mach-O files and are not directly loadable by an operating system loader. The `.nok`, `.nod`, and other container magic values are reserved identifiers; their complete binary layouts, compatibility rules, relocation records, signing, and loader semantics are outside this specification.

### 17.1 Relocatable objects (.nobj) v2

`nova-as` produces a relocatable object when the output name ends in `.nobj` (only the output name selects the mode). The current format version is `NOBJ_VERSION` 2; version 1 files remain readable and decode with default symbol metadata. The byte layout is little-endian. The container begins with a 32-byte header (magic `NOBJ`, version, flags, and three record counts) followed by section, symbol, and relocation records. Version 2 adds a one-byte symbol type and an eight-byte symbol size to each symbol record; types are `notype` (0), `object` (1), `function` (2), `section` (3), and `file` (4).

- **Sections.** A name, a kind (`progbits` carrying image bytes, `nobits` carrying no bytes), flags (`alloc`, `exec`, `write`, `read`), an alignment power of two, and a size.
- **Symbols.** A name, binding (`local` or `global`), a section index (`0xFFFFFFFF` for an undefined external), and a value that is the symbol's offset within its section.
- **Relocations.** `PCREL_DISP14` records a branch or call to an undefined symbol; `LA64` records an `LA` whose target is a symbol and whose expansion is the fixed nine-instruction maximal `LI` block; `ABS64` records a 64-bit absolute address for `.quad symbol`.

`LA` to a symbol becomes an `LA64` relocation over a fixed maximal-width expansion, so no relocatable constant depends on a load address. `.global`/`.globl` names appear in the symbol table with global binding. Encoding is deterministic and decoding rejects malformed, truncated, or over-long streams.

### 17.2 Nova Linker v0.1

`nova.LINKER_VERSION` is "Nova Linker v0.1", and `nova-ld` drives `nova.link.link_objects`. The linker is deliberately minimal and static: it takes any number of `.nobj` inputs and emits one `.nob` executable. There is no shared-library support, linker script language, dead-code elimination, relaxation, LTO, TLS, PIE, or debug output.

- **Section layout.** Groups are emitted in the order `.text`, `.rodata`, `.data`, `.bss`, then other names lexically. Each output section starts at an address aligned to the largest member alignment, and each member at its own alignment. `.bss` is materialized as zero bytes inside the flat image because the `.nob` container has no separate uninitialized area.
- **Symbol resolution.** A global definition is visible to every object; a local definition only inside its own object. Every undefined external must be satisfied and no global name may be defined twice. The entry symbol (default `_start`) must be defined exactly once. Failures are reported without a traceback: `undefined symbol 'foo'`, `multiple definition of 'foo'`, `undefined entry symbol '_start'`.
- **Relocations.** `PCREL_DISP14` writes the resolved address minus the site address into the signed 14-bit displacement of a branch or `CALL`, rejecting a misaligned target or out-of-range displacement. `LA64` re-encodes the resolved absolute address as the fixed nine-instruction expansion, preserving the destination register.
- **Options and determinism.** `-o`/`--output`, `--entry` (default `_start`), and `--base` (default `0x1000`). Linking the same inputs with the same options always produces byte-for-byte identical output.

### 17.3 User module directories

The library resolves default module search and install locations dynamically from the current user's home directory, under `Documents/OpenWindows` and `Documents/OpenWindows-Essentials`. They can be overridden independently with `NOVA_OPENWINDOWS_DIR` and `NOVA_OPENWINDOWS_ESSENTIALS_DIR`. Calling `nova.openwindows_directories()` only resolves and returns paths; it does not create directories or write files. No user-specific absolute path is stored in this repository or embedded in a Nova artifact.

## 18. Architecture conformance

`nova/conformance.py` publishes `Nova architecture conformance suite v1.0`: a set of hardware-independent vectors that state what every conforming Nova implementation must observe. Each vector carries its normative machine-code words, initial architectural state (registers, memory, CSRs, privilege), and the exact required result. Run it with `python -m nova.conformance`, or from Python with `nova.run_conformance_suite()` and `nova.conformance_report()`.

The vectors cover integer and unsigned arithmetic, flags, shifts and rotates, loads and stores, alignment faults, branches, calls and returns, exception entry and return, CSR warl behavior, atomics, paged translation, timer interrupts, object identities, and virtualization. They refer only to the architectural contract, never to caches, pipelines, or any physical design. A processor implementation conforms to Nova v1.0 if it satisfies every vector; the reference emulator is included as one implementation of that contract, not as the definition of it.

## 19. Scope boundaries

The base ISA intentionally does not define floating-point, vector/SIMD, debug, trace, or power-management instructions; these are reserved for future extensions and are not "incomplete" features of v1.0. Likewise, the repository does not define a compiler, an operating system, a dynamic loader, or any processor product. The static linker is `nova-ld` (Nova Linker v0.1) and is deliberately minimal, not a general-purpose link editor or a compiler; this repository is not a compiler. `docs/COMPILER_BACKEND.md` is the concrete LLVM/GCC backend porting plan for a future C compiler, and `docs/PORTING_READINESS.md` audits whether the current codebase is ready for a compiler, a Nova64 kernel, an OpenWindows port, or hardware.

The reference emulator is architectural: it models registers, the program counter, CSR state, exception and interrupt state, virtual memory as specified, atomics, the capability table, and the defined MMU behavior. It does not simulate pipelines, caches, branch predictors, or other microarchitecture. Passing its tests demonstrates conformance of the model to this specification; it is not evidence of a hardware implementation. New or changed instruction definitions must update `nova/isa.py`, the emulator handler table, this document's generated blocks, and the tests together.

## 20. Implementation report

**Implemented:** the complete base ISA defined above — 83 instructions across eight formats; the 64-bit-only profile with import-time enforcement; 20 assigned CSRs; the four-level translator with 4 KiB, 2 MiB and 1 GiB pages, permissions, faults, and TLB invalidation; six 64-bit atomics with acquire/release fields; the three-source interrupt architecture with IPIs and a multicore cluster helper; the virtualization extension with two-dimensional translation; the finalized capability/permission object model with stable, versioned cross-process identities and an import/export format; the Nova ABI v1.0 conventions and standard syscall table; the assembler, disassembler, pseudo-instructions, relocatable objects, static linker, executable container, and four command-line tools.

**Partially implemented:** the MMU TLB is a simple functional cache rather than a hardware-exact structure (the architecture exposes only its flush behavior, which is implemented); the cluster helper models hart identity, shared memory, and IPI routing, not physical interconnects; the memory consistency model is realized by direct sequential execution rather than by a relaxed hardware model with explicit barriers.

**Experimental:** none of the base ISA. The reserved opcode ranges, reserved CSR indices, and reserved file-format names are future extension space, not experimental features.

**Not implemented by design:** floating point, vectors, debug/trace, dynamic linking, a compiler, and any CPU or silicon product.

**Known limitations:** the reference model is single-threaded; a `NovaCluster` steps harts cooperatively. Generation and index spaces are 32 bits each; an implementation must seed distinct generation spaces before merging independently created object containers, and the importer detects collisions rather than silently reinterpreting them. No hardware implementation is provided; the architecture conformance suite is the implementation-independent contract.

**Tests:** the generated documentation tests, the full instruction-table conformance suite, the CSR/exception/interrupt tests, object and linker tests, example programs, and CLI end-to-end tests all run under `python -m pytest`. Additional v1.0 tests cover huge pages, stable object identities and import/export validation, virtual memory, atomics, interrupts, virtualization, the 64-bit profile, and the architecture conformance vectors (`python -m nova.conformance`).
