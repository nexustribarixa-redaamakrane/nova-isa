"""Tests for the hardware-independent Nova architecture conformance suite."""

import struct

from nova import (
    CONFORMANCE_VECTORS,
    CONFORMANCE_VERSION,
    conformance_report,
    run_conformance_suite,
)
from nova.isa import decode_strict


def test_conformance_suite_is_versioned_and_populated():
    assert CONFORMANCE_VERSION == "Nova architecture conformance suite v1.0"
    assert len(CONFORMANCE_VECTORS) >= 15
    assert len({vector["name"] for vector in CONFORMANCE_VECTORS}) == len(CONFORMANCE_VECTORS)


def test_every_vector_is_machine_code_not_just_assembly():
    for vector in CONFORMANCE_VECTORS:
        assert vector["code"], vector["name"]
        assert all(isinstance(word, int) and 0 <= word <= 0xFFFFFFFF
                   for word in vector["code"])
        assert all(decode_strict(word) is not None for word in vector["code"]), vector["name"]
        assert struct.calcsize("<I") == 4


def test_all_conformance_vectors_pass():
    passed, total = run_conformance_suite()
    assert passed == total == len(CONFORMANCE_VECTORS)


def test_report_is_deterministic_and_lists_every_vector():
    first = conformance_report()
    assert first == conformance_report()
    for vector in CONFORMANCE_VECTORS:
        assert f"[PASS] {vector['name']}" in first
    assert f"{len(CONFORMANCE_VECTORS)}/{len(CONFORMANCE_VECTORS)}" in first
