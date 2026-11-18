# Phase 0 implementation plan — Nova64 / NARCH64 toolchain hardening

**Status:** active plan. This document records the audit result, the frozen-ISA
decisions, the dependency order, and the implementation status of each item.
It is kept in sync with `docs/PORTING_READINESS.md`.

## 1. Audit result (what already exists)

A full source audit was performed before any changes. The following already
exist and are **not** being reimplemented:

- `nova/isa.py`: authoritative instruction encodings, operand rules, CSR file,
  exception codes, opcode allocation, 64-bit profile enforcement.
- `nova/assembler.py`: table-driven instruction encoding; labels (byte offsets);
  directives `.global`/`.globl`, `.section`, `.byte`, `.hword`, `.word`, `.quad`,
  `.asciz`, `.space`, `.align`; `LI`/`LA` pseudo-instruction fixed-point layout;
  `.nobj` object frontend with three relocation kinds.
- `nova/object.py`: `.nobj` v1 container, sections, symbols (name/binding/
  section/value), relocations, strict validation and deterministic encoding.
- `nova/link.py`: section grouping and alignment, symbol resolution, entry
  point checks, `PCREL_DISP14` and `LA64` application, error strings.
- `nova/nob.py`: `.nob` v1 executable container.
- `nova/emulator.py`: architectural reference model, MMU (including stage 2),
  atomics, interrupts, virtualization, object capabilities.
- `nova/objfmt.py`: `NOBO` v1 external object identity format.
- `nova/syscalls.py`: standard syscall table and dispatcher.
- `nova/conformance.py`: 18 hardware-independent architecture vectors.
- `docs/COMPILER_BACKEND.md`: LLVM/GCC backend plan.
- `docs/PORTING_READINESS.md`: readiness audit and roadmap.

Confirmed gaps (audited, not assumed):

1. `ABS64` is defined and emitted by the assembler but **has no linker applier**
   — linking a `.quad symbol` object fails with `unsupported relocation kind`.
2. `.nobj` symbols carry no type or size, and the record stream has no spare
   bytes in a symbol record, so metadata requires a format version bump.
3. `.nox` has reserved magic `b"NOVADBG\x00"` and `implemented=False`, but no
   layout, encoder, or decoder.
4. `.type`, `.size`, `.cfi_*`, `.file`, `.loc`, `.include`, and `.macro` are all
   explicitly rejected by the assembler's directive whitelist.
5. `nova/link.py` is single-pass: layout precedes relocation and there is no
   branch relaxation or long-branch sequence.
6. There is no formal C ABI document covering data layout, aggregates,
   varargs, struct returns, or compiler runtime helpers.

## 2. Frozen-ISA decisions

### 2.1 `LUI`: decision = **do not add in v1.0**

`LUI` does not exist anywhere in the tree, and no instruction materializes a
32-bit immediate cheaply: `MOVI` zero-extends 14 bits, and a full 64-bit
constant uses the nine-word `LI` expansion (`MOVI` + four `SHLI 13`/`ORI 13`
pairs). Tests pin that expansion (`tests/test_pseudo.py`) and the generated
README tables are compared byte-for-byte, so adding an instruction has a large
blast radius across assembler, disassembler, emulator, linker relocations,
documentation, and conformance.

**Decision:** Nova ISA v1.0 stays frozen. `LUI` is a candidate for a future,
explicitly versioned ISA extension. Phase 0 solves the *practical* problem —
long branches and large addresses — at the linker/pseudo-instruction layer,
which is outside the architectural contract: a `LI rd, target` followed by
`JMP rd` is already a legal five-word sequence (or nine words for a full 64-bit
constant), so long-branch expansion needs no new opcode.

### 2.2 Naming

`Nova64` and `NARCH64` are both accepted names for this architecture. Code
identifiers remain `nova`/`Nova`; documentation may use either name.

## 3. Dependency order and affected files

```
ABS64 linker fix  (independent, highest priority: live bug)
        │
Object v2 symbol metadata ──► assembler .type/.size
        │                            │
        └────────► .nox debug format ◄┘  (.file/.loc/.cfi_* records)
                            │
                  include + macro pre-pass
                            │
              C ABI + compiler-runtime docs
                            │
              relaxation / PIC / GOT (remaining Phase 0 work)
```

| Item | Affected files |
| --- | --- |
| ABS64 | `nova/link.py`, `tests/test_linker.py` |
| Object v2 | `nova/object.py`, `nova/assembler.py`, `tests/test_object.py`, `tests/test_linker.py`, `tests/test_documentation.py`, `README.md` |
| `.type`/`.size` | `nova/assembler.py`, `nova/object.py`, new assembler tests |
| `.nox` | new `nova/nox.py`, `nova/fileformats.py`, `nova/assembler.py`, new tests, `README.md` |
| include/macro | `nova/assembler.py`, new tests |
| ABI/runtime docs | new `docs/NOVA_C_ABI.md`, new `docs/COMPILER_RUNTIME.md`, `README.md` |
| relaxation/PIC | `nova/link.py`, `nova/object.py`, `nova/assembler.py`, `nova/pseudo.py`, tests |

## 4. Backward-compatibility rules

- Existing `.nob` and `.nobj` v1 files must remain readable. The object decoder
  will accept both v1 and v2; the encoder will emit v2, and tests will check
  that a v1 stream still decodes with default metadata.
- Existing relocation kinds keep their byte values and spans.
- Existing assembler sources must keep assembling byte-for-byte identically
  (verified by the existing example and CLI tests).
- No ISA encoding, opcode, CSR index, exception code, or instruction semantic
  changes are permitted in Phase 0.
- New directives are additive; previously rejected directives may become
  accepted, but no accepted syntax changes meaning.

## 5. Implementation status

| # | Item | Status |
| --- | --- | --- |
| 0 | Audit + plan + LUI decision | Done |
| 1 | ABS64 linker applier + tests | Done (`e8a9c72`) |
| 2 | Object v2 symbol type/size + `.type`/`.size` | Done (`c1e11e7`) |
| 3 | `.nox` debug format + `.file`/`.loc`/`.cfi_*` | Done (`7b827e8`) |
| 4 | `.include` + `.macro` | Done (`a9c2935`) |
| 5 | C ABI specification + compiler runtime requirements | Done (`d8306b0`) |
| 6 | Long branches (`BL` + `PCREL_LONG`) | Done (`9f4d99d`) |
| 6b | Automatic linker relaxation | Remaining Phase 0 work |
| 6c | PIC/GOT relocations and addressing | Remaining Phase 0 work |
| 6d | Weak symbols, linker scripts, dynamic linking | Out of scope for this pass |
| 7 | Update `PORTING_READINESS.md` and final verification | Done |

## 6. Definition of done

- All pre-existing tests and the 18 conformance vectors still pass.
- Each new feature has positive and negative tests.
- The documentation tests still pass with regenerated tables.
- `python -m compileall -q nova tests` succeeds.
- Remaining Phase 0 work is explicitly listed, not silently omitted.
