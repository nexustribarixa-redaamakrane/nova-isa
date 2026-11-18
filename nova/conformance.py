"""Architecture conformance vectors for Nova ISA v1.0.

A conformance vector is a hardware-independent statement of what a conforming
Nova processor must observe. Each vector carries its normative machine code
(``code``), its initial architectural state, and the exact architectural state
the processor must reach. The human-readable assembly is only the source from
which ``code`` was produced.

This module lets any implementation be checked against the same vectors:

    python -m nova.conformance

The vectors are derived from the specification in ``README.md``; they do not
refer to any microarchitecture, cache, pipeline, or physical implementation.
"""

from __future__ import annotations

import struct
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .assembler import assemble_text
from .emulator import NovaMachine
from .isa import (
    EXCEPTION_CODES,
    PRIV_MACHINE,
    RESET_ENTRY,
    WORD_MASK,
    make_object_reference,
)

__all__ = [
    "CONFORMANCE_VERSION",
    "CONFORMANCE_VECTORS",
    "run_vector",
    "check_vector",
    "run_conformance_suite",
    "conformance_report",
]

CONFORMANCE_VERSION = "Nova architecture conformance suite v1.0"

# A memory write is (address, size_in_bytes, value). Images are (address, code).
MemoryWrite = Tuple[int, int, int]
Image = Tuple[int, Sequence[int]]


def _words(assembly: str) -> Tuple[int, ...]:
    data = assemble_text(assembly)
    return tuple(
        struct.unpack_from("<I", data, offset)[0]
        for offset in range(0, len(data), 4)
    )


def _vector(name: str, assembly: str, *, expect: Mapping[str, object],
            entry: int = RESET_ENTRY, load_at: Optional[int] = None,
            steps: int = 12, priv: int = PRIV_MACHINE,
            memory: Sequence[MemoryWrite] = (),
            images: Sequence[Image] = (),
            csrs: Optional[Mapping[str, int]] = None,
            stop_on_exception: bool = False) -> dict:
    return {
        "name": name,
        "assembly": assembly,
        "code": _words(assembly),
        "entry": entry,
        "load_at": entry if load_at is None else load_at,
        "steps": steps,
        "priv": priv,
        "memory": tuple(memory),
        "images": tuple((address, tuple(words)) for address, words in images),
        "csrs": dict(csrs or {}),
        "stop_on_exception": stop_on_exception,
        "expect": dict(expect),
    }


CONFORMANCE_VECTORS: Tuple[dict, ...] = (
    _vector(
        "nop-advances-pc",
        "NOP\nNOP\n",
        steps=2,
        expect={"pc": RESET_ENTRY + 8},
    ),
    _vector(
        "integer-arithmetic",
        "MOVI r1, 7\nMOVI r2, 5\nADD r3, r1, r2\n"
        "SUB r4, r1, r2\nMUL r5, r1, r2\nNEG r6, r1\n",
        expect={"regs": {1: 7, 2: 5, 3: 12, 4: 2, 5: 35,
                         6: WORD_MASK - 6}},
    ),
    _vector(
        "signed-and-unsigned-division",
        "MOVI r1, 100\nMOVI r2, 7\nDIV r3, r1, r2\nREM r4, r1, r2\n"
        "UDIV r5, r1, r2\nUREM r6, r1, r2\n",
        expect={"regs": {3: 14, 4: 2, 5: 14, 6: 2}},
    ),
    _vector(
        "division-by-zero-raises-arithmetic",
        "MOVI r1, 5\nDIV r2, r1, r0\nHALT\n",
        steps=2, stop_on_exception=True,
        expect={"ecode": EXCEPTION_CODES["ARITHMETIC"], "ecause": 0},
    ),
    _vector(
        "logical-operations",
        "MOVI r1, 0x3C\nMOVI r2, 0x0F\nAND r3, r1, r2\n"
        "OR r4, r1, r2\nXOR r5, r1, r2\nNOT r6, r1\n",
        expect={"regs": {3: 0x0C, 4: 0x3F, 5: 0x33,
                         6: WORD_MASK ^ 0x3C}},
    ),
    _vector(
        "shifts-and-rotates",
        "MOVI r1, 1\nMOVI r2, 4\nSHL r3, r1, r2\n"
        "SHRI r4, r3, 1\nROTLI r5, r1, 63\nROTRI r6, r5, 63\n",
        expect={"regs": {3: 16, 4: 8, 5: 1 << 63, 6: 1}},
    ),
    _vector(
        "compare-sets-psr-flags",
        "MOVI r1, 5\nMOVI r2, 8\nCMP r1, r2\nCSRR r3, psr\n",
        expect={"regs": {3: 2}, "csrs": {"psr": 2}},
    ),
    _vector(
        "loads-and-stores",
        "MOVI r1, 0x2000\nMOVI r2, 0x80\nSTB r2, [r1]\n"
        "STH r2, [r1 + 2]\nSTW r2, [r1 + 4]\nSTD r2, [r1 + 8]\n"
        "LDBU r3, [r1]\nLDHU r4, [r1 + 2]\nLDWU r5, [r1 + 4]\n"
        "LDD r6, [r1 + 8]\n",
        steps=10,
        expect={
            "regs": {3: 0x80, 4: 0x80, 5: 0x80, 6: 0x80},
            "memory": ((0x2000, 1, 0x80), (0x2002, 2, 0x80),
                       (0x2004, 4, 0x80), (0x2008, 8, 0x80)),
        },
    ),
    _vector(
        "misaligned-store-faults",
        "MOVI r1, 0x2001\nMOVI r2, 7\nSTD r2, [r1]\n",
        steps=3, stop_on_exception=True,
        expect={"ecode": EXCEPTION_CODES["ALIGNMENT_FAULT"]},
    ),
    _vector(
        "conditional-branch-and-call-return",
        "MOVI r1, 3\nMOVI r2, 5\nBLT r1, r2, taken\n"
        "MOVI r3, 0\nHALT\ntaken: CALL fn\nMOVI r4, 99\nHALT\n"
        "fn: MOVI r5, 7\nRET\n",
        steps=8,
        expect={"regs": {3: 0, 4: 99, 5: 7}},
    ),
    _vector(
        "system-call-exception-entry-and-mret",
        "SCALL 7\nMOVI r1, 1\nHALT\n",
        steps=6,
        images=((0x3000, _words("CSRR r2, ecode\nCSRR r3, ecause\nMRET\n")),),
        csrs={"vbar": 0x3000},
        expect={
            "regs": {1: 1, 2: EXCEPTION_CODES["SYSTEM_CALL"], 3: 7},
            "csrs": {"ecode": EXCEPTION_CODES["NONE"], "ecause": 7},
        },
    ),
    _vector(
        "csr-warlmasks-reserved-bits",
        "LI r1, -1\nCSRW psr, r1\nCSRR r2, psr\n",
        steps=11,
        expect={"regs": {2: 0xF}},
    ),
    _vector(
        "atomic-add-and-compare-and-swap",
        "MOVI r1, 0x2000\nMOVI r2, 5\nAMOADD r3, r1, r2, 0, 0\n"
        "LDD r4, [r1]\nMOVI r5, 15\nMOVI r2, 99\n"
        "AMOCAS r5, r1, r2, 0, 0\n",
        steps=8,
        memory=((0x2000, 8, 10),),
        expect={
            "regs": {3: 10, 4: 15, 5: 15},
            "memory": ((0x2000, 8, 99),),
        },
    ),
    _vector(
        "paged-translation-fetch-and-data",
        "MOVI r1, 42\nHALT\n",
        entry=0x40000000, load_at=0x50000000, steps=2,
        memory=(
            (0x10000, 8, (0x11000 >> 12) << 12 | 1),
            (0x11008, 8, (0x12000 >> 12) << 12 | 1),
            (0x12000, 8, (0x50000 << 12) | 0x1F),
        ),
        csrs={"ptbr": (1 << 63) | 0x10000},
        expect={"regs": {1: 42}, "halted": True},
    ),
    _vector(
        "timer-interrupt-and-return",
        "MOVI r1, 1\nHALT\n",
        steps=5,
        images=((0x2000, _words("IRET\n")),),
        csrs={"ie": 2, "timecmp": 2, "ivec": 0x2000},
        expect={"regs": {1: 1}, "csrs": {"ecode": 0}},
    ),
    _vector(
        "object-create-and-get",
        "MOVI r1, 24\nOBJ.CREATE r2, r1\nOBJ.GET r3, r2\n",
        expect={"regs": {2: make_object_reference(1, 1), 3: 24}},
    ),
    _vector(
        "stage2-nested-translation",
        "MOVI r1, 42\nHALT\n",
        entry=0x40000000, load_at=0x30000000, steps=2,
        memory=(
            # Stage-1 tables (host physical because stage 2 is identity here).
            (0x10000, 8, (0x11000 >> 12) << 12 | 1),
            (0x11008, 8, (0x12000 >> 12) << 12 | 1),
            (0x12000, 8, (0x13000 >> 12) << 12 | 1),
            (0x13000, 8, (0x30000 << 12) | 0x1F),
            # Stage-2 root and 1 GiB identity leaf.
            (0x20000, 8, (0x21000 >> 12) << 12 | 1),
            (0x21000, 8, 0x1F),
        ),
        csrs={
            "ptbr": (1 << 63) | 0x10000,
            "vptbr": (1 << 63) | 0x10000,
            "s2ptbr": (1 << 63) | 0x20000,
            "hstatus": 1,
        },
        expect={"regs": {1: 42}, "halted": True},
    ),
    _vector(
        "virtualization-guest-exit",
        "MOVI r1, 0x3000\nCSRW vpc, r1\nVMLAUNCH\nMOVI r4, 9\nHALT\n",
        steps=6,
        images=((0x3000, _words("MOVI r5, 7\nVMEXIT 1\n")),),
        csrs={"vpriv": 2},
        expect={
            "regs": {4: 9, 5: 7},
            "csrs": {"vexit": 1, "hstatus": 0},
        },
    ),
)


def run_vector(vector: Mapping[str, object]) -> NovaMachine:
    """Execute one conformance vector on the reference machine."""
    entry = int(vector["entry"])  # type: ignore[arg-type]
    machine = NovaMachine(entry=entry)
    machine.priv = int(vector.get("priv", PRIV_MACHINE))  # type: ignore[arg-type]
    for address, size, value in vector.get("memory", ()):  # type: ignore[misc]
        machine._write_mem(int(address), int(value), int(size))
    for csr, value in vector.get("csrs", {}).items():  # type: ignore[union-attr]
        machine.write_csr(str(csr), int(value))
    code = b"".join(struct.pack("<I", word) for word in vector["code"])  # type: ignore[union-attr]
    machine.load_code(code, int(vector.get("load_at", entry)))  # type: ignore[arg-type]
    machine.pc = entry
    for address, words in vector.get("images", ()):  # type: ignore[misc]
        image = b"".join(struct.pack("<I", word) for word in words)
        machine.load_code(image, int(address))
        machine.pc = entry
    machine.run(int(vector.get("steps", 12)),  # type: ignore[arg-type]
                stop_on_exception=bool(vector.get("stop_on_exception", False)))
    return machine


def check_vector(vector: Mapping[str, object]) -> List[str]:
    """Return a list of unmet architectural requirements for ``vector``."""
    machine = run_vector(vector)
    mismatches: List[str] = []
    expect = vector["expect"]

    for register, value in expect.get("regs", {}).items():  # type: ignore[union-attr]
        actual = machine.read_register(int(register))
        if actual != int(value):
            mismatches.append(f"r{register}: expected {value:#x}, got {actual:#x}")
    for address, size, value in expect.get("memory", ()):  # type: ignore[misc]
        actual = machine._read_mem(int(address), int(size))
        if actual != int(value):
            mismatches.append(
                f"memory[{address:#x}]: expected {value:#x}, got {actual:#x}"
            )
    for csr, value in expect.get("csrs", {}).items():  # type: ignore[union-attr]
        actual = machine.read_csr(str(csr))
        if actual != int(value):
            mismatches.append(
                f"{csr}: expected {value:#x}, got {actual:#x}"
            )
    for attribute in ("pc", "ecode", "ecause", "halted"):
        if attribute in expect:
            actual = (machine.exception_code if attribute == "ecode"
                      else getattr(machine, attribute))
            wanted = expect[attribute]
            if actual != wanted:
                mismatches.append(
                    f"{attribute}: expected {wanted}, got {actual}"
                )
    return mismatches


def run_conformance_suite(vectors: Iterable[Mapping[str, object]] = CONFORMANCE_VECTORS
                          ) -> Tuple[int, int]:
    """Run every vector; return ``(passed, total)``."""
    passed = 0
    total = 0
    for vector in vectors:
        total += 1
        if not check_vector(vector):
            passed += 1
    return passed, total


def conformance_report() -> str:
    """Return a human-readable conformance report."""
    lines = [CONFORMANCE_VERSION, ""]
    for vector in CONFORMANCE_VECTORS:
        mismatches = check_vector(vector)
        status = "PASS" if not mismatches else "FAIL"
        lines.append(f"[{status}] {vector['name']}")
        lines.extend(f"        {mismatch}" for mismatch in mismatches)
    passed, total = run_conformance_suite()
    lines.extend(("", f"{passed}/{total} conformance vectors passed"))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    print(conformance_report())
    raise SystemExit(0 if run_conformance_suite()[0] == len(CONFORMANCE_VECTORS) else 1)
