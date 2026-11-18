import os
import textwrap
from pathlib import Path

import pytest

from nova.cc.diagnostics import CompileError
from nova.cc.lexer import TokenKind
from nova.cc.preprocessor import preprocess, preprocess_to_tokens, tokens_to_text


def pp(source: str, filename: str = "test.c", **kwargs) -> str:
    return preprocess(textwrap.dedent(source), filename=filename, **kwargs)


def pp_tokens(source: str, filename: str = "test.c", **kwargs):
    return [t for t in preprocess_to_tokens(textwrap.dedent(source), filename=filename, **kwargs)
            if t.kind is not TokenKind.EOF]


def values(tokens):
    return [t.value for t in tokens]


def norm_ws(text: str) -> str:
    return " ".join(text.split())


# 1. Object-like macro expansion
def test_object_like_macro_expansion():
    src = """
    #define VERSION 1
    #define NAME NOVA
    int x = VERSION;
    int y = NAME;
    """
    toks = pp_tokens(src)
    vals = values(toks)
    assert vals[:5] == ["int", "x", "=", "1", ";"]
    assert vals[5:10] == ["int", "y", "=", "NOVA", ";"]


# 2. Function-like macro expansion
def test_function_like_macro_expansion():
    src = """
    #define ADD(a,b) ((a) + (b))
    #define SQUARE(x) ((x) * (x))
    int v = ADD(1, 2);
    int w = SQUARE(3);
    """
    out = pp(src)
    flat = "".join(out.split())
    assert "v=((1)+(2));" in flat
    assert "w=((3)*(3));" in flat


# 3. Nested macro expansion
def test_nested_macro_expansion():
    src = """
    #define A B
    #define B C
    #define C 42
    int x = A;
    """
    out = pp(src)
    assert "42" in out


# 4. Macro argument substitution
def test_macro_argument_substitution_with_nested_parens():
    src = """
    #define CALL(f, x) f(x)
    #define WRAP(y) (y)
    int a = CALL(WRAP, (1,2));
    """
    out = pp(src)
    # (1,2) passed as single argument x to CALL -> f((1,2)) after substitution of x? CALL(f,x)=f(x)
    # with args f=WRAP, x=(1,2) => WRAP((1,2)) then expanded => (((1,2))) in effect
    assert "WRAP" not in out.split() or "(( 1 , 2 ))" in " ".join(out.split()) or "((1, 2))" in " ".join(out.split())


# 5. Stringification
def test_stringification():
    src = r"""
    #define STR(x) #x
    char *s = STR(hello world);
    char *t = STR(42);
    """
    toks = pp_tokens(src)
    strings = [t.string for t in toks if t.kind is TokenKind.STRING]
    assert "hello world" in strings
    assert "42" in strings


# 6. Token pasting
def test_token_pasting():
    src = """
    #define CAT(a,b) a##b
    int foobar = 1;
    int x = CAT(foo, bar);
    """
    out = pp(src)
    assert "foobar" in out
    # result of CAT used as identifier token
    toks = pp_tokens(src)
    vals = values(toks)
    assert vals.count("foobar") >= 2  # declaration and use


# 7. Variadic macros
def test_variadic_macros_va_args():
    src = """
    #define LOG(fmt, ...) log(fmt, __VA_ARGS__)
    LOG("%d", 1, 2);
    """
    out = pp(src)
    flat = " ".join(out.split())
    assert "log" in flat
    assert "%d" in out
    assert "1" in flat and "2" in flat


# 8. #undef
def test_undef():
    src = """
    #define X 1
    int a = X;
    #undef X
    int b = X;
    """
    out = pp(src)
    toks = pp_tokens(src)
    vals = values(toks)
    assert "1" in vals
    # after undef, X remains identifier
    assert vals.count("X") >= 1


# 9. #ifdef and #ifndef
def test_ifdef_ifndef():
    src = """
    #define FEAT
    #ifdef FEAT
    int a = 1;
    #endif
    #ifndef FEAT
    int b = 2;
    #endif
    #ifndef OTHER
    int c = 3;
    #endif
    """
    out = "".join(pp(src).split())
    assert "inta=1;" in out
    assert "intb=2;" not in out
    assert "intc=3;" in out


# 10. #if, #elif, #else
def test_if_elif_else():
    src = """
    #define V 2
    #if V == 1
    int a = 1;
    #elif V == 2
    int b = 2;
    #else
    int c = 3;
    #endif
    """
    flat = "".join(pp(src).split())
    assert "intb=2;" in flat
    assert "inta=1;" not in flat
    assert "intc=3;" not in flat


# 11. defined
def test_defined_operator():
    src = """
    #define X 1
    #if defined(X) && defined X
    int ok = 1;
    #endif
    #if defined(Y)
    int bad = 1;
    #endif
    """
    flat = "".join(pp(src).split())
    assert "intok=1;" in flat
    assert "intbad=1;" not in flat


# 12. Undefined identifiers in conditional expressions
def test_undefined_identifiers_in_if_are_zero():
    src = """
    #if UNKNOWN + 1 == 1
    int ok = 1;
    #else
    int bad = 1;
    #endif
    """
    flat = "".join(pp(src).split())
    assert "intok=1;" in flat
    assert "intbad=1;" not in flat


# 13. Integer-expression precedence
def test_if_expression_precedence_and_ops():
    src = """
    #if 1 + 2 * 3 == 7
    int a = 1;
    #endif
    #if (1 + 2) * 3 == 9
    int b = 1;
    #endif
    #if (1 << 3) == 8 && (8 >> 1) == 4
    int c = 1;
    #endif
    #if (5 & 3) == 1 && (5 | 2) == 7 && (5 ^ 1) == 4
    int d = 1;
    #endif
    #if !0 && ~0
    int e = 1;
    #endif
    #if (1 ? 2 : 3) == 2
    int f = 1;
    #endif
    """
    flat = "".join(pp(src).split())
    for name in ["a", "b", "c", "d", "e", "f"]:
        assert f"int{name}=1;" in flat


# 14. Nested includes
def test_nested_includes(tmp_path: Path):
    (tmp_path / "a.h").write_text('#include "b.h"\nint A = B;\n', encoding="utf-8")
    (tmp_path / "b.h").write_text('#define B 7\n', encoding="utf-8")
    main = tmp_path / "main.c"
    main.write_text('#include "a.h"\n', encoding="utf-8")
    out = preprocess(main.read_text(encoding="utf-8"), filename=str(main), directory=tmp_path)
    assert "intA=7;" in "".join(out.split())


# 15. Include search paths
def test_include_search_paths(tmp_path: Path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "lib.h").write_text("#define LIB 99\n", encoding="utf-8")
    out = preprocess('#include "lib.h"\nint x = LIB;\n', filename="main.c",
                     directory=tmp_path, include_paths=[inc])
    assert "intx=99;" in "".join(out.split())


# 16. Include guards
def test_include_guards(tmp_path: Path):
    (tmp_path / "g.h").write_text(textwrap.dedent("""\
        #ifndef G_H
        #define G_H
        int g = 1;
        #endif
        """), encoding="utf-8")
    main = '#include "g.h"\n#include "g.h"\n'
    out = preprocess(main, filename=str(tmp_path / "main.c"), directory=tmp_path)
    assert "".join(out.split()).count("intg=1;") == 1


# 17. #pragma once
def test_pragmas_once(tmp_path: Path):
    (tmp_path / "o.h").write_text("#pragma once\nint o = 1;\n", encoding="utf-8")
    main = '#include "o.h"\n#include "o.h"\n'
    out = preprocess(main, filename=str(tmp_path / "main.c"), directory=tmp_path)
    assert "".join(out.split()).count("into=1;") == 1


# 18. Include-cycle protection
def test_include_cycle_detection(tmp_path: Path):
    (tmp_path / "a.h").write_text('#include "b.h"\n', encoding="utf-8")
    (tmp_path / "b.h").write_text('#include "a.h"\n', encoding="utf-8")
    with pytest.raises(CompileError, match="include cycle"):
        preprocess('#include "a.h"\n', filename=str(tmp_path / "main.c"), directory=tmp_path)


# 19. Line splicing
def test_line_splicing():
    src = "#def\\\nine X 1\nint x = X;\n"
    out = preprocess(src, filename="splice.c")
    assert "intx=1;" in "".join(out.split())


# 20. Source-location preservation
def test_source_location_preservation():
    src = "#define N 1\nint a = N;\nint b = N;\n"
    toks = pp_tokens(src, filename="loc.c")
    ints = [t for t in toks if t.value == "1"]
    assert len(ints) == 2
    assert ints[0].location.file == "loc.c"
    assert ints[0].location.line == 2
    assert ints[1].location.line == 3


# 21. Invalid directive diagnostics
def test_invalid_directive_diagnostic():
    with pytest.raises(CompileError, match="unsupported preprocessing directive #bogus"):
        pp("#bogus\n", filename="bad.c")

def test_missing_include_diagnostic(tmp_path: Path):
    with pytest.raises(CompileError, match="include file not found"):
        preprocess('#include "nope.h"\n', filename=str(tmp_path / "main.c"), directory=tmp_path)


# 22. Macro recursion protection
def test_macro_recursion_protection_direct():
    src = """
    #define A A
    int x = A;
    """
    flat = "".join(pp(src).split())
    assert "intx=A;" in flat


def test_macro_recursion_protection_indirect():
    src = """
    #define A B
    #define B A
    int x = A;
    """
    flat = "".join(pp(src).split())
    # Should not infinitely recurse; result should contain A or B as identifier
    assert "intx=A;" in flat or "intx=B;" in flat


# Additional required/important cases

def test_redefinition_diagnostic():
    with pytest.raises(CompileError, match="redefinition of macro"):
        pp("#define X 1\n#define X 2\n")


def test_identical_redefinition_allowed():
    out = pp("#define X 1\n#define X 1\nint a = X;\n")
    assert "1" in out


def test_undef_missing_name_diagnostic():
    with pytest.raises(CompileError, match="macro name"):
        pp("#undef\n")


def test_endif_without_if_diagnostic():
    with pytest.raises(CompileError, match="#endif without matching #if"):
        pp("#endif\n")


def test_else_after_else_diagnostic():
    with pytest.raises(CompileError, match="duplicate #else"):
        pp("#if 1\n#else\n#else\n#endif\n")


def test_unterminated_conditional_diagnostic():
    with pytest.raises(CompileError, match="unterminated conditional directive"):
        pp("#if 1\nint x;\n")


def test_function_macro_arg_count_mismatch():
    with pytest.raises(CompileError, match="macro argument count mismatch"):
        pp("#define F(a,b) a+b\nint x = F(1);\n")


def test_stringification_in_function_macro():
    src = """
    #define SHOW(x) puts(#x)
    SHOW(1+2);
    """
    toks = pp_tokens(src)
    assert any(t.kind is TokenKind.STRING and t.string == "1+2" for t in toks)


def test_token_pasting_invalid_diagnostic():
    # pasting that produces an invalid token, e.g. trying to form something nonsensical
    with pytest.raises(CompileError):
        pp("#define BAD(a,b) a##b\nBAD(., /);\n")


def test_line_directive_accepted():
    # #line accepted as no-op (implementation choice) and should not error
    out = pp('#line 10 "x.c"\nint x = 1;\n')
    assert "intx=1;" in "".join(out.split())


def test_error_directive_active():
    with pytest.raises(CompileError, match="boom"):
        pp('#error boom\n')


def test_error_directive_inactive_not_triggered():
    out = pp("#if 0\n#error boom\n#endif\nint ok = 1;\n")
    assert "ok" in out and "#error" not in out
