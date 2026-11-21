#!/usr/bin/env python3
"""Build and boot the NPK kernel inside QEMU using the Nova bytecode host.

QEMU does not implement Nova ISA v1.0. This script generates an x86-64
multiboot ELF that interprets the real ``npk.nob`` image, boots it in
``qemu-system-x86_64``, and compares QEMU guest memory with the Python
reference boot. It is a kernel-workflow validation tool, not an ISA change.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from nova import npk  # noqa: E402
from nova.npk import qemu_translator as qt  # noqa: E402
from nova.npk.boot import boot_kernel  # noqa: E402
from nova.npk.layout import BOOT_MARKER_ADDRESS  # noqa: E402

_DONE_MAGIC = 0x4E564D51
_ERROR_MAGIC = 0xDEADBEEF
_COMPARE_BASE = 0x90000
_COMPARE_SIZE = 0x10000


class QemuBootError(RuntimeError):
    """Raised when the QEMU boot does not match the reference machine."""


class _Qmp:
    """Minimal QMP client for dump-guest-memory and quit."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.stream = self.sock.makefile("rwb")
        self.next_id = 1
        greeting = self._read_line()
        if b"QMP" not in greeting:
            raise QemuBootError(f"unexpected QMP greeting: {greeting!r}")
        self.command("qmp_capabilities")

    def _read_line(self) -> bytes:
        line = self.stream.readline()
        if not line:
            raise QemuBootError("QMP connection closed")
        return line

    def command(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        command_id = self.next_id
        self.next_id += 1
        payload: Dict[str, Any] = {"execute": name, "id": command_id}
        if args:
            payload["arguments"] = args
        self.stream.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.stream.flush()
        while True:
            line = self._read_line()
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise QemuBootError(f"QMP {name} failed: {message['error']}")
            return message.get("return")

    def close(self) -> None:
        try:
            self.command("quit")
        except (OSError, QemuBootError):
            pass
        self.stream.close()
        self.sock.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_guest_bytes(data: bytes, address: int, size: int) -> bytes:
    """Extract bytes from a QEMU ELF guest-memory dump."""
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise QemuBootError("guest memory dump is not an ELF64 file")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize = struct.unpack_from("<H", data, 54)[0]
    phnum = struct.unpack_from("<H", data, 56)[0]
    end = address + size
    for index in range(phnum):
        offset = phoff + index * phentsize
        p_type = struct.unpack_from("<I", data, offset)[0]
        if p_type != 1:  # PT_LOAD
            continue
        p_offset, p_vaddr = struct.unpack_from("<QQ", data, offset + 8)[0:2]
        p_filesz = struct.unpack_from("<Q", data, offset + 32)[0]
        if p_vaddr <= address and end <= p_vaddr + p_filesz:
            start = p_offset + (address - p_vaddr)
            return data[start:start + size]
    raise QemuBootError(f"guest dump has no segment covering 0x{address:x}")


def _read_u64(data: bytes, address: int) -> int:
    return struct.unpack("<Q", _read_guest_bytes(data, address, 8))[0]


def _reference_region(machine) -> bytes:
    return bytes(machine._read_byte(addr)
                 for addr in range(_COMPARE_BASE, _COMPARE_BASE + _COMPARE_SIZE))


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, help="existing npk.nob image")
    parser.add_argument("--build-dir", type=Path, help="kernel build output")
    parser.add_argument("--qemu", default=None, help="qemu-system-x86_64 path")
    parser.add_argument("--timeout", type=float, default=90.0,
                        help="boot timeout in seconds")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    qemu = args.qemu or shutil.which("qemu-system-x86_64")
    if not qemu or not Path(qemu).exists():
        raise QemuBootError("qemu-system-x86_64 was not found")

    temp = tempfile.TemporaryDirectory(prefix="npk-qemu-")
    work = Path(temp.name)
    build_dir = args.build_dir or (work / "kernel")
    kernel_path = args.kernel or npk.build_kernel(build_dir)
    image = npk.load_kernel_image(kernel_path)

    reference = boot_kernel(image)
    expected = _reference_region(reference.machine)
    elf_path = qt.build_qemu_elf(image, work / "npk_qemu.elf")
    dump_path = work / "guest.elf"

    port = _free_port()
    cmd = [
        qemu, "-M", "pc", "-m", "64M", "-display", "none",
        "-monitor", "none", "-serial", "none",
        "-qmp", f"tcp:127.0.0.1:{port},server=on,wait=off",
        "-kernel", str(elf_path),
    ]
    if args.verbose:
        print("QEMU:", " ".join(cmd))

    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
    qmp: Optional[_Qmp] = None
    try:
        deadline = time.monotonic() + min(10.0, args.timeout)
        while time.monotonic() < deadline:
            try:
                qmp = _Qmp("127.0.0.1", port, timeout=2.0)
                break
            except (OSError, QemuBootError):
                time.sleep(0.1)
        if qmp is None:
            raise QemuBootError("could not connect to QEMU QMP")

        deadline = time.monotonic() + args.timeout
        result: Optional[bytes] = None
        steps = 0
        attempt = 0
        while time.monotonic() < deadline:
            dump_path = work / f"guest_{attempt}.elf"
            attempt += 1
            qmp.command("dump-guest-memory", {
                "paging": False,
                "protocol": f"file:{dump_path.as_posix()}",
            })
            if not dump_path.exists() or dump_path.stat().st_size == 0:
                time.sleep(0.5)
                continue
            data = dump_path.read_bytes()
            marker = _read_u64(data, qt.QEMU_LAYOUT["done"])
            if marker == _ERROR_MAGIC:
                raise QemuBootError("Nova translator reached its error handler")
            if marker == _DONE_MAGIC:
                steps = _read_u64(data, qt.QEMU_LAYOUT["steps"])
                result = _read_guest_bytes(
                    data, qt.QEMU_LAYOUT["shadow"] + _COMPARE_BASE, _COMPARE_SIZE)
                break
            time.sleep(0.5)

        if result is None:
            raise QemuBootError("QEMU kernel did not halt before timeout")
        if steps != reference.steps:
            raise QemuBootError(
                f"step count mismatch: QEMU={steps}, reference={reference.steps}")
        if result != expected:
            first = next((i for i, (a, b) in enumerate(zip(result, expected))
                          if a != b), 0)
            address = _COMPARE_BASE + first
            raise QemuBootError(
                f"guest memory differs from reference at 0x{address:05x}: "
                f"QEMU=0x{result[first]:02x}, reference=0x{expected[first]:02x}")
        if args.verbose:
            marker = struct.unpack_from("<Q", result, BOOT_MARKER_ADDRESS - _COMPARE_BASE)[0]
            print(f"boot marker: 0x{marker:08x}")
            print(f"steps: {steps}")
        print("QEMU boot: PASS")
        return 0
    finally:
        if qmp is not None:
            qmp.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QemuBootError as exc:
        print(f"QEMU boot: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
