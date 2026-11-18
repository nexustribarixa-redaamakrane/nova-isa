# Pre-commit development history

**This file is a provenance record.** The Nova ISA project was designed and
implemented in this working directory over a period of work **before** this
repository was initialized with Git. Consequently, this first Git commit is a
snapshot of already-completed work — the beginning of version control, not the
beginning of the project. No earlier commits exist to squash or rewrite,
because no Git repository existed until this point.

## Version timeline (all work completed before the initial commit)

| Stage | What was implemented before the first commit |
| --- | --- |
| v0.1-experimental | Initial register/immediate arithmetic, memory access, and the exception model. |
| v0.2-experimental | Fixed 32-bit instruction encoding, 32 GPRs, flat byte-addressable memory, signed and unsigned branches, `CALL`/`RET`, `TRAP`/`SCALL`, and the first experimental object handles. |
| v0.3-experimental | X format and `CSRR`/`CSRW`; a defined CSR file (`psr`, `epc`, `ecause`, `ecode`, `priv`, `vbar`, `coreid`) with privilege and WARL rules; depth-4 exception save stack and `MRET` frame semantics; `nova/isa.py` established as the single source of truth for assembler, disassembler, emulator, and generated documentation. Added `LI`/`LA` pseudo-instructions, the `.nob` executable container v1, the Nova Trap ABI v0.1, the `nova-as`/`nova-ld`/`nova-objdump`/`nova-run` tools, the `.nobj` relocatable-object container v1, the static linker v0.1, and the canonical example suite. |
| v1.0 | Froze the architecture: the 64-bit-only profile with import-time enforcement; a concrete four-level MMU with permissions, faults, and TLB invalidation; the finalized capability-based object model; 64-bit atomics with acquire/release ordering; the interrupt architecture with software/timer/external sources, IPIs, and a multicore cluster helper; the virtualization extension; the full Nova ABI v1.0; rotate and unsigned-divide instructions; the Nova v1.0 specification in `README.md`; and the version bump across package, tools, and containers. |
| v1.0 post-freeze | Added 2 MiB and 1 GiB page support with leaf-alignment faults; stable cross-process object identities (generation + index) with the versioned `NOBO` v1 import/export format and full validation; and the hardware-independent Nova architecture conformance suite (`python -m nova.conformance`) with 17 vectors. |

## State at the initial commit

- **ISA:** Nova ISA v1.0 — 83 instructions in eight formats, 19 assigned CSRs, 11 exception codes.
- **Implementation:** reference emulator, assembler, disassembler, pseudo-instructions, relocatable objects, static linker, executable containers, and four command-line tools.
- **Verification:** 1,775 passing tests (`python -m pytest`) and 17/17 conformance vectors (`python -m nova.conformance`).
- **Documentation:** `README.md` (normative specification), this provenance record, and the MIT `LICENSE`.

The initial commit is titled `INITIAL COMMIT` and includes this history so that
the authorship and chronology of the work are unambiguous to anyone inspecting
the repository later.
