# Nova C ABI v1.0

**Status:** normative software convention layered on `Nova ISA v1.0` and
`Nova ABI v1.0`. It is not part of the architectural contract, but a C compiler
and libc that claim Nova64/NARCH64 compatibility must follow it. Every decision
below is explicit; where the base ISA cannot support a feature, that is stated
rather than left implicit.

## 1. Data model

LP64: `long`, pointers, `size_t`, and `ptrdiff_t` are 64-bit; `int` is 32-bit.

| C type | Size (bytes) | Alignment | Representation |
| --- | ---: | ---: | --- |
| `char`, `signed char`, `unsigned char`, `_Bool` | 1 | 1 | Two's complement for signed; plain `char` is **unsigned** |
| `short`, `unsigned short` | 2 | 2 | Two's complement |
| `int`, `unsigned int`, `enum` | 4 | 4 | Two's complement |
| `long`, `unsigned long`, `long long`, `unsigned long long` | 8 | 8 | Two's complement |
| pointers, `size_t`, `ptrdiff_t`, `intptr_t`, `uintptr_t` | 8 | 8 | Flat 64-bit address |
| `float`, `double`, `long double` | — | — | No base-ISA representation; see section 9 |

`char` is unsigned so that `LDBU` is the natural byte load and sign extension is
always explicit. Endianness is little, matching instruction and data fetch.

## 2. Aggregates and alignment

- A struct's alignment is the maximum alignment of its members; its size is
  rounded up to a multiple of that alignment.
- Members are laid out in declaration order with natural padding. There is no
  packed-by-default rule; `__attribute__((packed))`-style behavior is a compiler
  extension and must not be relied on by ABI-visible interfaces.
- Unions use the maximum member size rounded to the maximum member alignment.
- Arrays have the element's size and alignment; an array's size is
  `count * sizeof(element)`.
- The maximum alignment required by this ABI is 16 bytes. A type requiring more
  than 16 bytes is not ABI-portable.
- Bit-fields are allocated from low to high within a storage unit of the
  declared type's size and alignment; allocation across units follows the
  natural alignment of the unit.

## 3. Registers

| Register | ABI name | Role | Preserved across calls? |
| --- | --- | --- | --- |
| `r0` | `zero` | Hardwired zero | n/a |
| `r1`-`r6` | `a0`-`a5` | Integer arguments; `a0` return value | No |
| `r7`-`r10` | `t0`-`t3` | Caller-saved temporaries | No |
| `r11`-`r14` | `s0`-`s3` | Callee-saved | **Yes** |
| `r15`-`r27` | — | Caller-saved temporaries | No |
| `r28` | `gp` | PIC global-pointer register (section 8) | **Yes in PIC code** |
| `r29` | `sp` | Stack pointer | **Yes** (must be restored) |
| `r30` | `fp` | Optional frame pointer | **Yes if used** |
| `r31` | `ra` | Return address written by `CALL` | No |

## 4. Stack and frames

- The stack grows downward. `sp` points to the next free byte.
- At every call site `sp` is 16-byte aligned (`sp % 16 == 0`) immediately before
  the `CALL` instruction. `CALL` does not adjust `sp` and writes `pc + 4` to
  `ra`.
- Stack arguments occupy `[sp, sp + 8*n)` at the call site and are visible at
  the same offsets on entry to the callee (no return address is pushed).
- There is **no red zone**. Interrupt and exception handlers do not use the
  interrupted context's stack; a leaf function may still allocate stack space.
- A frame may use `fp` as a stable frame pointer. If used, the callee must
  restore it before returning.
- Large frames must be probed explicitly by the compiler or runtime; the ISA has
  no stack-probe instruction.

## 5. Argument passing

1. Scalars of at most 8 bytes (integers, pointers) are classified as one
   eight-byte chunk. Aggregates are split into consecutive eight-byte chunks;
   the final chunk may be smaller and is passed as a full slot.
2. Chunks are assigned to `a0`-`a5` in order while registers remain.
3. Remaining chunks are passed on the stack in order at 8-byte slots starting
   at `[sp]`.
4. A call with more than six integer chunks therefore uses the stack for the
   excess. Alignment within a register or stack slot follows the natural
   alignment of the chunk; no chunk crosses a slot boundary.

Because there are no floating-point registers in the base ISA, floating-point
values (if supported by a software implementation) are classified by exactly
these integer rules.

## 6. Return values

- A scalar of at most 8 bytes is returned in `a0`.
- An aggregate of 9 to 16 bytes is returned in `a0` and `a1`, with the first
  eight bytes in `a0`.
- An aggregate larger than 16 bytes is returned through a **hidden pointer**:
  the caller allocates the result object and passes its address as the first
  argument in `a0`, shifting all other arguments to the next available
  register/stack slot. The callee returns that same address in `a0`.
- A function returning `void` leaves `a0` unspecified.

## 7. Variadic functions

A variadic call uses the same rules as section 5. `va_list` is a 24-byte,
8-byte-aligned structure:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | `uint32_t` | `gp_offset`: bytes already consumed in `a0`-`a5` |
| 4 | `uint32_t` | Reserved for a future FP extension; zero in this ABI |
| 8 | `uint64_t` | `overflow_arg_area`: address of the first stack argument |
| 16 | `uint64_t` | `reg_save_area`: address of a 48-byte spill area |

A callee that calls `va_start` must spill `a0`-`a5` into `reg_save_area` in
ascending register order before reading any variadic argument. Caller-saved
registers are not preserved across the call, so the spill must happen in the
callee. `va_arg` advances `gp_offset` by 8 until it reaches 48, then reads from
`overflow_arg_area` in 8-byte steps. There is no floating-point register save
area because the base ISA has no floating-point registers.

## 8. Position-independent code and the GOT

- In PIC translation units, `gp` (`r28`) holds the base address of the global
  offset table. `gp` is callee-saved in PIC code; a non-PIC function that calls
  PIC code must preserve it.
- Global data accesses use a `LA64` relocation into the GOT and a load through
  `gp`. Function calls to externals use a GOT entry through `gp` followed by
  `JMP`.
- The linker defines `_GLOBAL_OFFSET_TABLE_` only for PIC links; the symbol is
  reserved in non-PIC links and must not be referenced.
- PIC and non-PIC objects may be linked together only if the non-PIC object
  does not clobber `gp` across calls into PIC code.

## 9. Floating point

The base ISA has no floating-point instructions or registers. Therefore:

- This ABI defines **no floating-point argument or return register convention**.
- Source-level `float`/`double` support requires a software floating-point
  library. Such values are passed as their raw 64-bit (or 32-bit zero-extended)
  bit patterns through the integer rules of sections 5 and 6.
- A future ISA extension with floating-point registers must introduce a new ABI
  version rather than silently changing this one.

## 10. System calls and process state

`SCALL imm` passes the call number in `imm`, arguments in `a0`-`a5`, and the
return value in `a0`; failure is `-1` in `a0` with an error code in `a1`. The
standard call numbers are in `nova/syscalls.py` and in README section 15.1.
Startup code receives no arguments in registers: the stack pointer and the
program's global symbols are the only guaranteed initial state.

## 11. Conformance examples

A compiler emitting this ABI must agree with the emulator on these points:

```asm
; int add(int a, int b, int c, int d, int e, int f, int g)
; seventh argument g is at [sp] on entry
add:
    LDD  r1, [sp]        ; g
    ADD  r1, r1, r4      ; a+... (e and f in a4/a5)
    RET
```

```asm
; variadic prologue: spill a0-a5 before va_start
    ADDI sp, sp, -64
    STD  a0, [sp + 8]
    STD  a1, [sp + 16]
    STD  a2, [sp + 24]
    STD  a3, [sp + 32]
    STD  a4, [sp + 40]
    STD  a5, [sp + 48]
```

`tests/test_c_abi.py` checks the register aliases, the `va_list` layout, and
that the stack alignment rule holds for generated call sequences.
