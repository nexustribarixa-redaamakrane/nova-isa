"""``nova-cc`` compiler driver.

The driver is a thin orchestration layer over the existing compiler, assembler,
object-file, and linker APIs.  It never re-implements a stage: C sources are
preprocessed by :mod:`nova.cc.preprocessor`, parsed by
:mod:`nova.cc.parser`, analysed by :mod:`nova.cc.sema`, lowered by
:mod:`nova.cc.codegen`, assembled by :func:`nova.assembler.assemble_object`,
and linked by :func:`nova.link.link_objects`.

Invocation forms::

    python -m nova.cc [options] input...
    nova-cc [options] input...

Supported compilation modes are preprocessing only (``-E``), assembly only
(``-S``), object generation (``-c``), and the default compile-and-link mode.
See ``docs/NOVA_CC.md`` for the user-facing reference.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import ISA_VERSION, LINKER_VERSION, TOOLCHAIN_VERSION
from ..assembler import AssemblyError, assemble_object
from ..link import DEFAULT_ENTRY_SYMBOL, DEFAULT_IMAGE_BASE, LinkError, link_objects
from ..nob import NobError, encode_nob
from ..object import ObjectError, encode_object, load_object
from ..runtime import INCLUDE_DIR, runtime_objects
from .codegen import generate
from .diagnostics import CompileError
from .parser import parse
from .preprocessor import preprocess, preprocess_to_tokens
from .sema import analyze

__all__ = [
    "SUPPORTED_LANGUAGES",
    "SUPPORTED_TARGETS",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 70

#: Language modes accepted by ``-std=``.  Every mode reaches the existing
#: preprocessor; the frontend accepts the Phase 1 freestanding C subset.
SUPPORTED_LANGUAGES = (
    "nova-c11",
    "c11",
    "gnu11",
    "c99",
    "gnu99",
    "c89",
    "c90",
)

#: Code-generation targets accepted by ``--target=``.
SUPPORTED_TARGETS = ("nova64",)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")

_HELP_EPILOG = """
supported language modes (-std=):
  nova-c11 (default), c11, gnu11, c99, gnu99, c89, c90
supported targets (--target=):
  nova64
"""


class _UsageError(Exception):
    """A command-line problem detected after argument parsing."""


class _DriverError(Exception):
    """A user-facing driver failure with an already-clear message."""


class _MacroAction(argparse.Action):
    """Record ``-D``/``-U`` in command-line order."""

    def __init__(self, option_strings, dest, kind: str, **kwargs):
        self.kind = kind
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        actions = getattr(namespace, "macro_actions", None)
        if actions is None:
            actions = []
            setattr(namespace, "macro_actions", actions)
        actions.append((self.kind, values))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-cc",
        description=(
            "Compile freestanding C for Nova64.  Sources are preprocessed, "
            "analysed, lowered to Nova assembly, assembled to .nobj, and "
            "optionally linked to a .nob executable."
        ),
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-E", dest="mode", action="store_const", const="preprocess",
                      help="preprocess only and write .i output")
    mode.add_argument("-S", dest="mode", action="store_const", const="assembly",
                      help="compile to Nova assembly and write .s output")
    mode.add_argument("-c", dest="mode", action="store_const", const="object",
                      help="compile and assemble to .nobj, without linking")
    parser.set_defaults(mode="link")
    parser.add_argument("-o", dest="output", metavar="FILE",
                        help="write the output to FILE")
    parser.add_argument("-I", dest="include_dirs", action="append", default=[],
                        metavar="DIR", help="add DIR to the include search path")
    parser.add_argument("-D", dest="macro_actions", action=_MacroAction, kind="define",
                        metavar="NAME[=VALUE]", help="define a preprocessor macro")
    parser.add_argument("-U", dest="macro_actions", action=_MacroAction, kind="undefine",
                        metavar="NAME", help="undefine a preprocessor macro")
    parser.add_argument("-std", dest="std", default="nova-c11", metavar="MODE",
                        help="select the C language mode (default: nova-c11)")
    parser.add_argument("-O", dest="optimization", default=None, metavar="LEVEL",
                        help="optimization level; only -O0 is supported")
    parser.add_argument("--target", dest="target", default="nova64", metavar="TARGET",
                        help="code-generation target (only nova64)")
    parser.add_argument("--long-calls", dest="long_calls", action="store_true",
                        help="use layout-independent long calls (LA ra/BL) instead of CALL")
    parser.add_argument("--long-branches", dest="long_branches", action="store_true",
                        help="remove the intra-function branch range limit "
                             "(inverted short branch plus BL)")
    parser.add_argument("-e", "--entry", dest="entry", default=None, metavar="SYMBOL",
                        help=f"linker entry symbol (default: {DEFAULT_ENTRY_SYMBOL})")
    parser.add_argument("--runtime", dest="runtime", action="store_true",
                        help="link the freestanding Nova runtime (startup, "
                             "syscall wrappers, and libnova)")
    parser.add_argument("--nostartfiles", dest="nostartfiles", action="store_true",
                        help="with --runtime, omit the runtime _start object")
    parser.add_argument("--nostdlib", dest="nostdlib", action="store_true",
                        help="with --runtime, omit the libnova C objects "
                             "(syscall wrappers are still linked)")
    parser.add_argument("--version", action="version",
                        version=(f"nova-cc (Nova C compiler) {TOOLCHAIN_VERSION} "
                                 f"[{ISA_VERSION}; {LINKER_VERSION}]"))
    parser.add_argument("inputs", nargs="*", metavar="INPUT",
                        help="C source files (.c) and, when linking, .nobj objects")
    return parser


# -- option validation -------------------------------------------------------

def _parse_macro_actions(actions: Optional[Sequence[Tuple[str, str]]]
                         ) -> Tuple[Dict[str, str], List[str]]:
    """Return disjoint ``-D`` and ``-U`` sets honouring command-line order."""
    defines: Dict[str, str] = {}
    undefines: List[str] = []
    for kind, text in actions or ():
        name, sep, value = text.partition("=")
        name = name.strip()
        if not _IDENTIFIER_RE.fullmatch(name):
            raise _UsageError(f"invalid macro name {name!r} in -{kind[0].upper()}")
        if kind == "define":
            defines[name] = value if sep else "1"
            if name in undefines:
                undefines.remove(name)
        else:
            if sep:
                raise _UsageError(f"-U expects a macro name, got {text!r}")
            defines.pop(name, None)
            if name not in undefines:
                undefines.append(name)
    return defines, undefines


def _validate(args: argparse.Namespace) -> Tuple[Dict[str, str], List[str]]:
    if not args.inputs:
        raise _UsageError("no input files")
    if args.std not in SUPPORTED_LANGUAGES:
        raise _UsageError(
            f"unsupported language mode {args.std!r}; supported modes: "
            + ", ".join(SUPPORTED_LANGUAGES)
        )
    if args.target not in SUPPORTED_TARGETS:
        raise _UsageError(
            f"unsupported target {args.target!r}; only 'nova64' is supported"
        )
    if args.optimization not in (None, "0"):
        raise _UsageError(
            f"unsupported optimization level '-O{args.optimization}'; "
            "only -O0 is supported"
        )
    if args.entry is not None and not args.entry:
        raise _UsageError("entry symbol cannot be empty")
    if args.entry is not None and args.mode != "link":
        raise _UsageError("-e/--entry is only meaningful when linking")
    if args.runtime and args.mode != "link":
        raise _UsageError("--runtime is only meaningful when linking")
    if args.nostartfiles and not args.runtime:
        raise _UsageError("--nostartfiles is only meaningful with --runtime")
    if args.nostdlib and not args.runtime:
        raise _UsageError("--nostdlib is only meaningful with --runtime")

    defines, undefines = _parse_macro_actions(args.macro_actions)
    objects = [Path(p) for p in args.inputs
               if Path(p).suffix.lower() == ".nobj"]
    if args.mode != "link" and objects:
        raise _UsageError(
            f"object input {str(objects[0])!r} is only supported when linking"
        )
    if args.output and len(args.inputs) > 1 and args.mode != "link":
        raise _UsageError(
            "-o with multiple inputs requires link mode (omit -E/-S/-c)"
        )
    return defines, undefines


# -- path helpers ------------------------------------------------------------

def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(a))) == \
            os.path.normcase(os.path.abspath(str(b)))


def _default_output(source: Path, suffix: str) -> Path:
    return source.with_suffix(suffix)


def _check_output(output: Path, inputs: Sequence[Path]) -> None:
    if output.exists() and output.is_dir():
        raise _DriverError(f"output '{output}' is a directory")
    for item in inputs:
        if _same_path(output, item):
            raise _DriverError(
                f"refusing to overwrite input file '{item}' with output '{output}'"
            )


def _write_atomic(path: Path, data: bytes) -> None:
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        raise _DriverError(f"cannot write '{path}': directory '{parent}' does not exist")
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
        )
    except OSError as exc:
        raise _DriverError(
            f"cannot write '{path}': {exc.strerror or exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass  # not every filesystem supports fsync on a regular file
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise _DriverError(f"cannot write '{path}': {exc.strerror or exc}") from exc
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# -- compilation stages ------------------------------------------------------

def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise _DriverError(
            f"cannot read '{path}': {exc.strerror or exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise _DriverError(f"'{path}' is not valid UTF-8: {exc}") from exc


def _preprocessor_options(path: Path, args: argparse.Namespace,
                          defines: Dict[str, str],
                          undefines: Sequence[str]) -> dict:
    return {
        "directory": str(path.parent),
        "include_paths": list(args.include_dirs),
        "system_include_paths": [str(INCLUDE_DIR)],
        "defines": defines,
        "undefines": list(undefines),
        "language": args.std,
    }


def _stage_error(stage: str, exc: CompileError) -> _DriverError:
    return _DriverError(f"{exc.location}: error: {stage}: {exc.message}")


def _preprocess_source(path: Path, args: argparse.Namespace,
                       defines: Dict[str, str],
                       undefines: Sequence[str]) -> str:
    source = _read_source(path)
    options = _preprocessor_options(path, args, defines, undefines)
    try:
        return preprocess(source, filename=str(path), **options)
    except CompileError as exc:
        raise _stage_error("preprocessing", exc) from exc


def _compile_assembly(path: Path, args: argparse.Namespace,
                      defines: Dict[str, str],
                      undefines: Sequence[str], *,
                      long_calls: Optional[bool] = None,
                      long_branches: Optional[bool] = None) -> str:
    if long_calls is None:
        long_calls = bool(args.long_calls)
    if long_branches is None:
        long_branches = bool(args.long_branches)
    source = _read_source(path)
    options = _preprocessor_options(path, args, defines, undefines)
    try:
        tokens = preprocess_to_tokens(source, filename=str(path), **options)
    except CompileError as exc:
        raise _stage_error("preprocessing", exc) from exc
    try:
        tree = parse(tokens, filename=str(path))
    except CompileError as exc:
        raise _stage_error("parsing", exc) from exc
    try:
        tree = analyze(tree, target=args.target)
    except CompileError as exc:
        raise _stage_error("semantic analysis", exc) from exc
    try:
        return generate(tree, target=args.target,
                        long_calls=bool(long_calls),
                        long_branches=bool(long_branches))
    except CompileError as exc:
        raise _stage_error("code generation", exc) from exc


_SIGNED_BRANCH_RE = re.compile(
    r"\bB(?:EQ|NE|LT|GE|LE|GT|LTU|GEU|LEU|GTU)\b"
)


def _range_notes(message: str, *, long_calls: bool,
                 long_branches: bool) -> str:
    """Suggest only the flag that removes the reported range limitation."""
    notes = []
    if re.search(r"\bCALL\b", message) and not long_calls:
        notes.append(
            "\nnote: recompile with --long-calls to remove the direct "
            "CALL distance limit"
        )
    if _SIGNED_BRANCH_RE.search(message) and not long_branches:
        notes.append(
            "\nnote: recompile with --long-branches to remove the direct "
            "branch distance limit"
        )
    return "".join(notes)


def _assemble(path: Path, assembly: str, *, long_calls: bool = False,
              long_branches: bool = False) -> object:
    try:
        return assemble_object(assembly, source_path=str(path))
    except AssemblyError as exc:
        message = str(exc)
        if "outside the signed 14-bit range" in message:
            message += _range_notes(message, long_calls=long_calls,
                                    long_branches=long_branches)
        raise _DriverError(f"{path}: error: assembly: {message}") from exc


# -- modes -------------------------------------------------------------------

def _run(args: argparse.Namespace, defines: Dict[str, str],
         undefines: Sequence[str]) -> int:
    inputs = [Path(item) for item in args.inputs]
    mode = args.mode
    explicit_output = Path(args.output) if args.output else None

    if mode == "link":
        if explicit_output is not None:
            output = explicit_output
        else:
            output = Path("a.nob")
        _check_output(output, inputs)

        objects = []
        # Linking the runtime makes the final image large enough that ordinary
        # fourteen-bit call/branch displacements from user code cannot be
        # assumed.  --runtime is an explicit opt-in to a layout-independent
        # link, so it implies the long forms; plain links keep compact calls.
        long_calls = bool(args.long_calls or args.runtime)
        long_branches = bool(args.long_branches or args.runtime)
        for path in inputs:
            if path.suffix.lower() == ".nobj":
                try:
                    objects.append(load_object(path))
                except OSError as exc:
                    raise _DriverError(
                        f"cannot read object '{path}': {exc.strerror or exc}"
                    ) from exc
                except ObjectError as exc:
                    raise _DriverError(f"{path}: error: object: {exc}") from exc
            else:
                assembly = _compile_assembly(path, args, defines, undefines,
                                             long_calls=long_calls,
                                             long_branches=long_branches)
                objects.append(_assemble(path, assembly,
                                         long_calls=long_calls,
                                         long_branches=long_branches))
        if args.runtime:
            objects.extend(runtime_objects(
                with_startup=not args.nostartfiles,
                with_library=not args.nostdlib,
            ))
        entry = args.entry or DEFAULT_ENTRY_SYMBOL
        try:
            image = link_objects(objects, entry_symbol=entry,
                                 image_base=DEFAULT_IMAGE_BASE)
        except LinkError as exc:
            message = str(exc)
            if "signed 14-bit displacement" in message:
                match = re.search(r"relocation of '([^']+)'", message)
                name = match.group(1) if match else ""
                if name.startswith(".L"):
                    if not args.long_branches:
                        message += (
                            "\nnote: recompile with --long-branches to remove the "
                            "direct branch distance limit"
                        )
                elif not args.long_calls:
                    message += (
                        "\nnote: recompile with --long-calls to remove the direct "
                        "CALL distance limit"
                    )
            raise _DriverError(f"link: {message}") from exc
        try:
            data = encode_nob(image_base=image.image_base, entry=image.entry,
                              image=image.image)
        except NobError as exc:
            raise _DriverError(f"link: cannot encode .nob output: {exc}") from exc
        _write_atomic(output, data)
        return EXIT_OK

    suffix = {"preprocess": ".i", "assembly": ".s", "object": ".nobj"}[mode]
    if explicit_output is not None:
        if len(inputs) != 1:
            raise _UsageError("-o with multiple inputs requires link mode")
        outputs = [explicit_output]
    else:
        outputs = [_default_output(path, suffix) for path in inputs]

    seen: Dict[str, Path] = {}
    for path, output in zip(inputs, outputs):
        _check_output(output, inputs)
        key = os.path.normcase(str(output.resolve()))
        if key in seen:
            raise _DriverError(
                f"inputs '{seen[key]}' and '{path}' both produce '{output}'"
            )
        seen[key] = path

    pending: List[Tuple[Path, bytes]] = []
    for path, output in zip(inputs, outputs):
        if mode == "preprocess":
            text = _preprocess_source(path, args, defines, undefines)
            pending.append((output, text.encode("utf-8")))
        elif mode == "assembly":
            text = _compile_assembly(path, args, defines, undefines)
            pending.append((output, text.encode("utf-8")))
        else:
            assembly = _compile_assembly(path, args, defines, undefines)
            obj = _assemble(path, assembly, long_calls=bool(args.long_calls),
                            long_branches=bool(args.long_branches))
            try:
                data = encode_object(obj)
            except ObjectError as exc:
                raise _DriverError(f"{path}: error: object encoding: {exc}") from exc
            pending.append((output, data))

    # Write only after every input compiled, so a failure cannot leave a
    # partially completed set of requested artifacts.
    for output, data in pending:
        _write_atomic(output, data)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run ``nova-cc`` and return a process exit status."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help/--version and argparse usage errors
        return int(exc.code or 0)

    try:
        defines, undefines = _validate(args)
        return _run(args, defines, undefines)
    except (_UsageError, _DriverError) as exc:
        print(f"nova-cc: error: {exc}", file=sys.stderr)
        if isinstance(exc, _UsageError):
            print("try 'nova-cc --help' for usage", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_ERROR
    except CompileError as exc:  # defensive: stages normally wrap this
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("nova-cc: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - internal errors stay distinct
        print(f"nova-cc: internal compiler error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL
