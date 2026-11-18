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


# ---------------------------------------------------------------------------
# Part A hardening: macro expansion correctness
# ---------------------------------------------------------------------------

def test_macro_prescan_argument_expansion():
    src = """
    #define ID(x) x
    #define ADD(a,b) ID(a) + ID(b)
    #define ONE 1
    #define TWO 2
    ADD(ONE, TWO);
    """
    assert norm_ws(pp(src)) == norm_ws("1+2;")


def test_macro_prescan_disabled_during_rescan_of_replacement():
    src = """
    #define f(a) a*g
    #define g(a) f(a)
    f(2)(9)
    """
    assert norm_ws(pp(src)) == norm_ws("2*f(9)")


def test_direct_recursive_expansion_object_like_hardening():
    src = """
    #define A A
    A
    """
    assert norm_ws(pp(src)) == norm_ws("A")


def test_indirect_recursive_expansion_three_macros():
    src = """
    #define A B
    #define B C
    #define C A
    A
    """
    assert norm_ws(pp(src)) == norm_ws("A")


def test_nested_function_like_macros():
    src = """
    #define INC(x) ((x)+1)
    #define DBL(x) ((x)*2)
    DBL(INC(3))
    """
    assert norm_ws(pp(src)) == norm_ws("((((3)+1))*2)")


def test_function_like_invocation_formed_across_expanded_tokens():
    src = """
    #define f(x) x+1
    #define g f
    #define apply(m) m(1)
    apply(g)
    """
    assert norm_ws(pp(src)) == norm_ws("1+1")


def test_function_like_invocation_formed_by_rescanning_with_following_tokens():
    src = """
    #define F(x) x
    #define CALL F
    CALL(42)
    """
    assert norm_ws(pp(src)) == norm_ws("42")


def test_no_expansion_without_parens_for_function_like_macro():
    src = """
    #define F(x) x
    F;
    F (7);
    """
    out = norm_ws(pp(src))
    assert "F;" in out.replace(" ", "")
    assert "7" in out


def test_stringification_whitespace_normalization_hardening():
    src = r"""
    #define S(x) #x
    S(a    +    b)
    """
    toks = pp_tokens(src)
    assert len(toks) == 1 and toks[0].kind is TokenKind.STRING
    assert toks[0].string == "a + b"


def test_stringification_trims_leading_trailing_whitespace():
    src = r"""
    #define S(x) #x
    S(   hello world   )
    """
    toks = pp_tokens(src)
    assert toks[0].string == "hello world"


def test_stringification_adjacent_tokens_no_space():
    src = r"""
    #define S(x) #x
    S(a+b)
    """
    toks = pp_tokens(src)
    assert toks[0].string == "a+b"


def test_stringification_escapes_quotes_and_backslashes():
    src = '#define S(x) #x\nS("hi")\n'
    toks = pp_tokens(src)
    assert toks[0].kind is TokenKind.STRING
    assert toks[0].string == '"hi"'
    assert toks[0].value == '"\\"hi\\""'


def test_stringification_raw_argument_not_expanded():
    src = """
    #define A 100
    #define S(x) #x
    S(A)
    """
    toks = pp_tokens(src)
    assert toks[0].string == "A"


def test_token_pasting_identifiers_hardening():
    src = """
    #define CAT(a,b) a ## b
    int CAT(foo, bar) = 1;
    """
    toks = pp_tokens(src)
    assert "foobar" in values(toks)


def test_token_pasting_numbers():
    src = """
    #define CAT(a,b) a ## b
    int x = CAT(12, 34);
    """
    toks = pp_tokens(src)
    assert "1234" in values(toks)


def test_token_pasting_punctuators():
    src = """
    #define MK(a,b) a ## b
    MK(+, =)
    """
    toks = pp_tokens(src)
    assert "+=" in values(toks)


def test_token_pasting_multiple_consecutive():
    src = """
    #define CAT3(a,b,c) a ## b ## c
    CAT3(1,2,3)
    """
    toks = pp_tokens(src)
    assert "123" in values(toks)


def test_token_pasting_empty_arg_placemarker_left():
    src = """
    #define CAT(a,b) a ## b
    CAT(,foo)
    """
    assert norm_ws(pp(src)) == norm_ws("foo")


def test_token_pasting_empty_arg_placemarker_right():
    src = """
    #define CAT(a,b) a ## b
    CAT(foo,)
    """
    assert norm_ws(pp(src)) == norm_ws("foo")


def test_token_pasting_invalid_diagnosed_extra():
    src = """
    #define BAD(a,b) a ## b
    BAD(@, b)
    """
    with pytest.raises(CompileError):
        pp(src)


def test_variadic_empty_argument_hardening():
    src = """
    #define LOG(fmt, ...) fmt __VA_ARGS__
    LOG("hi",)
    """
    toks = pp_tokens(src)
    assert toks[0].kind is TokenKind.STRING and toks[0].string == "hi"


def test_variadic_multiple_arguments():
    src = """
    #define F(x, ...) x __VA_ARGS__
    F(1, 2, 3, 4)
    """
    out = values(pp_tokens(src))
    assert out[0] == "1"
    assert "2" in out and "3" in out and "4" in out


def test_variadic_nested_parentheses_in_args():
    src = """
    #define F(x, ...) __VA_ARGS__
    F(0, (1,2), (3,(4,5)))
    """
    out = norm_ws(pp(src))
    assert "(1,2)" in out.replace(" ", "")


def test_variadic_stringification_of_va_args():
    src = r"""
    #define S(...) #__VA_ARGS__
    S(a, b, c)
    """
    toks = pp_tokens(src)
    assert toks[0].kind is TokenKind.STRING
    assert "a" in toks[0].string and "b" in toks[0].string


def test_variadic_token_pasting_with_va_args():
    src = """
    #define MAKE(name, ...) int name ## _impl(__VA_ARGS__)
    MAKE(foo, 1, 2)
    """
    out = values(pp_tokens(src))
    assert "foo_impl" in out


def test_gnu_comma_paste_elision_extension_empty():
    src = '#define F(fmt, ...) printf(fmt, ## __VA_ARGS__)\nF("x")\n'
    assert norm_ws(pp(src)) == norm_ws('printf("x")')


def test_gnu_comma_paste_elision_extension_nonempty():
    src = '#define F(fmt, ...) printf(fmt, ## __VA_ARGS__)\nF("x", 1)\n'
    out = norm_ws(pp(src))
    assert 'printf' in out and '"x"' in out and '1' in out


# ---------------------------------------------------------------------------
# #line and built-ins
# ---------------------------------------------------------------------------

def test_line_directive_sets_line_number():
    src = '#line 200\n__LINE__\n'
    toks = pp_tokens(src)
    assert toks[0].kind is TokenKind.INT and toks[0].number == 200


def test_line_directive_sets_line_and_file():
    src = '#line 42 "other.c"\n__LINE__\n__FILE__\n'
    toks = pp_tokens(src)
    assert toks[0].number == 42
    assert toks[1].kind is TokenKind.STRING and toks[1].string == "other.c"


def test_line_directive_propagates_to_subsequent_lines():
    src = '#line 10 "x.c"\n__LINE__\n__LINE__\n'
    toks = pp_tokens(src)
    assert toks[0].number == 10
    assert toks[1].number == 11


def test_builtin_stdc_defined_in_default_mode():
    toks = pp_tokens("__STDC__\n")
    assert toks[0].kind is TokenKind.INT and toks[0].number == 1


def test_builtin_stdc_hosted_defined_in_default_mode():
    toks = pp_tokens("__STDC_HOSTED__\n")
    assert toks[0].kind is TokenKind.INT and toks[0].number == 0


def test_builtin_stdc_version_in_c11_mode():
    toks = pp_tokens("__STDC_VERSION__\n", language="c11")
    assert toks[0].kind is TokenKind.INT and toks[0].number == 201112


def test_builtin_stdc_version_in_c99_mode():
    toks = pp_tokens("__STDC_VERSION__\n", language="c99")
    assert toks[0].kind is TokenKind.INT and toks[0].number == 199901


# ---------------------------------------------------------------------------
# Directives, includes, conditionals
# ---------------------------------------------------------------------------

def test_traditional_include_guard(tmp_path):
    (tmp_path / "guard.h").write_text(
        "#ifndef GUARD_H\n#define GUARD_H\nint guarded;\n#endif\n", encoding="utf-8")
    src = '#include "guard.h"\n#include "guard.h"\n'
    out = values(pp_tokens(src, directory=str(tmp_path)))
    assert out.count("guarded") == 1


def test_computed_include_string_form(tmp_path):
    (tmp_path / "a.h").write_text("int a;\n", encoding="utf-8")
    src = '#define H "a.h"\n#include H\n'
    out = values(pp_tokens(src, directory=str(tmp_path)))
    assert "a" in out


def test_include_missing_file_diagnostic(tmp_path):
    src = '#include "no_such_file.h"\n'
    with pytest.raises(CompileError):
        pp(src, directory=str(tmp_path))


def test_conditional_nesting_hardening():
    src = """
    #define A 1
    #if A
      #if 0
        int bad;
      #else
        int good;
      #endif
    #endif
    """
    out = values(pp_tokens(src))
    assert "good" in out and "bad" not in out


def test_conditional_unterminated_diagnostic_hardening():
    src = """
    #if 1
    int x;
    """
    with pytest.raises(CompileError):
        pp(src)


def test_error_directive_reports_location():
    src = """
    #error boom
    """
    with pytest.raises(CompileError) as excinfo:
        pp(src, filename="err.c")
    assert "boom" in str(excinfo.value)


def test_unknown_pragma_ignored_hardening():
    src = """
    #pragma something_unknown
    int x;
    """
    out = values(pp_tokens(src))
    assert "x" in out


# ---------------------------------------------------------------------------
# Phase 1 Step 4 hardening: source mapping, computed includes, _Pragma, modes
# ---------------------------------------------------------------------------


def test_splice_maps_token_locations_to_physical_lines():
    # '=' and the rest of the statement are on physical line 2 even though they
    # form one logical line with line 1 after backslash-newline splicing.
    toks = pp_tokens("int x\\\n = 1;\n", filename="splice.c")
    by_value = {t.value: t for t in toks}
    assert by_value["int"].location.line == 1
    assert by_value["int"].location.column == 1
    assert by_value["="].location.line == 2
    assert by_value["="].location.column == 2
    assert by_value["1"].location.line == 2
    assert by_value["1"].location.column == 4


def test_splice_segments_record_logical_offsets():
    from nova.cc.preprocessor import splice_lines

    logical = splice_lines("ab\\\ncd\\\nef\n", "t.c")
    # A trailing newline yields a final empty logical line.
    line = logical[0]
    assert line.text == "abcdef"
    assert [s.phys_line for s in line.segments] == [1, 2, 3]
    assert [s.logical_start for s in line.segments] == [0, 2, 4]


def test_leading_space_information_preserved():
    toks = pp_tokens("int x = 1;", filename="t.c")
    by_value = {t.value: t for t in toks}
    assert by_value["int"].leading_space is False
    assert by_value["x"].leading_space is True
    assert by_value["="].leading_space is True
    assert by_value[";"].leading_space is False


def test_preprocessing_output_is_deterministic():
    src = """
    #define A 1 + 2
    int x = A;
    """
    assert pp(src) == pp(src)
    assert values(pp_tokens(src)) == values(pp_tokens(src))


def test_computed_include_quoted_header_name(tmp_path):
    (tmp_path / "hdr.h").write_text("int from_header;\n", encoding="utf-8")
    src = '#define H "hdr.h"\n#include H\n'
    out = pp(src, filename=str(tmp_path / "t.c"), include_paths=[str(tmp_path)])
    assert "from_header" in out


def test_computed_include_angle_header_name(tmp_path):
    (tmp_path / "hdr.h").write_text("int from_header;\n", encoding="utf-8")
    src = "#define H <hdr.h>\n#include H\n"
    out = pp(src, filename=str(tmp_path / "t.c"), include_paths=[str(tmp_path)])
    assert "from_header" in out


def test_computed_include_nested_macro_expansion(tmp_path):
    (tmp_path / "feature.h").write_text("int feature_flag;\n", encoding="utf-8")
    src = "#define BASE feature\n#define HDR(x) <x.h>\n#include HDR(BASE)\n"
    out = pp(src, filename=str(tmp_path / "t.c"), include_paths=[str(tmp_path)])
    assert "feature_flag" in out


def test_computed_include_token_boundary_and_whitespace_variants(tmp_path):
    (tmp_path / "hdr.h").write_text("int variant;\n", encoding="utf-8")
    for operand in ("< h d r . h >", "<hdr . h>", "<hd r.h>"):
        src = "#define H " + operand + "\n#include H\n"
        out = pp(src, filename=str(tmp_path / "t.c"), include_paths=[str(tmp_path)])
        assert "variant" in out, operand


def test_computed_include_malformed_operands_are_source_located():
    for src, message in (
        ("#include <missing\n", "unterminated include file name"),
        ("#include\n", "empty #include directive"),
        ("#define H 42\n#include H\n", "invalid #include directive"),
    ):
        with pytest.raises(CompileError) as excinfo:
            pp(src, filename="bad_include.c")
        assert message in excinfo.value.message
        assert excinfo.value.location.file == "bad_include.c"


def test_include_path_safety_retained():
    # Absolute paths and parent traversal must never resolve, even with an
    # include path configured.
    for name in ('"/etc/passwd"', '"../secret.h"', '"sub/../../secret.h"'):
        with pytest.raises(CompileError):
            pp("#include " + name, filename="t.c", include_paths=["."])


def test_pragma_operator():
    out = pp('_Pragma("once")\nint x;\n', filename="t.c")
    assert "once" not in out
    assert "x" in out


def test_pragma_operator_from_macro_expansion():
    out = pp('#define DO_PRAGMA _Pragma("once")\nDO_PRAGMA\nint x;\n', filename="t.c")
    assert "DO_PRAGMA" not in out
    assert "x" in out


def test_pragma_operator_stringizes_macro_argument():
    out = pp('#define P(x) _Pragma(#x)\nP(once)\nint z;\n', filename="t.c")
    assert "once" not in out
    assert "z" in out


def test_pragma_operator_bad_operand_diagnostic():
    with pytest.raises(CompileError) as excinfo:
        pp("_Pragma(42)\n", filename="bad_pragma.c")
    assert "string literal" in excinfo.value.message


def test_language_modes_define_consistent_standard_macros():
    def macros(language: str):
        src = """
        #if defined(__STDC__)
        STD
        #endif
        #if defined(__STDC_VERSION__)
        VERSION
        #endif
        """
        return " ".join(pp(src, filename="t.c", language=language).split())

    assert macros("nova-c11") == "STD"
    assert macros("c99") == "STD VERSION"
    assert macros("c11") == "STD VERSION"
    assert macros("custom") == ""


def test_gnu_varargs_extension_still_documented_in_macro_behavior():
    # GNU ', ## __VA_ARGS__' remains an extension: the comma is removed when the
    # variadic argument is empty.
    out = pp(
        "#define LOG(f, ...) f(0, ## __VA_ARGS__)\nLOG(g)\nLOG(h, 1)\n",
        filename="t.c",
    )
    flat = "".join(out.split())
    assert "g(0)" in flat
    assert "h(0,1)" in flat


def test_command_line_undefines_remove_user_and_builtin_macros():
    src = """
    #ifdef FOO
    FOO
    #else
    NO_FOO
    #endif
    #ifdef __STDC__
    STDC
    #else
    NO_STDC
    #endif
    """
    out = norm_ws(pp(src, filename="t.c", defines={"FOO": "1"},
                     undefines=["FOO", "__STDC__"]))
    assert out == "NO_FOO NO_STDC"


def test_command_line_undefine_of_absent_macro_is_ignored():
    assert norm_ws(pp("int x;\n", undefines=["NEVER_DEFINED"])) == "int x;"

