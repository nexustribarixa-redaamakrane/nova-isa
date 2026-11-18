import pytest

from nova.cc import CompileError, TokenKind, lex


def kinds_values(source: str):
    return [(t.kind, t.value) for t in lex(source, "test.c")]


def test_lex_keywords_identifiers_and_punctuators():
    toks = lex("int main(void) { return foo_bar + 1; }", "main.c")
    assert [(t.kind, t.value) for t in toks[:-1]] == [
        (TokenKind.KEYWORD, "int"),
        (TokenKind.IDENT, "main"),
        (TokenKind.PUNCT, "("),
        (TokenKind.KEYWORD, "void"),
        (TokenKind.PUNCT, ")"),
        (TokenKind.PUNCT, "{"),
        (TokenKind.KEYWORD, "return"),
        (TokenKind.IDENT, "foo_bar"),
        (TokenKind.PUNCT, "+"),
        (TokenKind.INT, "1"),
        (TokenKind.PUNCT, ";"),
        (TokenKind.PUNCT, "}"),
    ]
    assert toks[-1].kind is TokenKind.EOF


def test_lex_integer_constants_and_suffixes():
    toks = lex("0 42 077 0x1f 0X10 0b1010 7u 8UL 9llu", "nums.c")
    ints = [t for t in toks if t.kind is TokenKind.INT]
    assert [t.number for t in ints] == [0, 42, 63, 31, 16, 10, 7, 8, 9]
    assert ints[5].value == "0b1010"
    assert ints[6].value.endswith("u")
    assert ints[7].value.endswith("UL")
    assert ints[8].value.endswith("llu")


def test_lex_char_and_string_literals():
    source = "'a' '\\n' '\\x41' '\\101' \"hi\\n\" \"a\\\"b\""
    toks = lex(source, "lit.c")
    chars = [t for t in toks if t.kind is TokenKind.CHAR]
    strings = [t for t in toks if t.kind is TokenKind.STRING]
    assert [t.number for t in chars] == [ord("a"), 10, 0x41, 0o101]
    assert [t.string for t in strings] == ["hi\n", 'a"b']


def test_lex_multi_char_punctuators_longest_match():
    source = "a==b!=c<=d>=e&&f||g++ + -- -> << >> <<= >>= += -= *= /= %= &= ^= |= ... ##"
    values = [t.value for t in lex(source) if t.kind is TokenKind.PUNCT]
    assert values == [
        "==", "!=", "<=", ">=", "&&", "||", "++", "+", "--", "->",
        "<<", ">>", "<<=", ">>=", "+=", "-=", "*=", "/=", "%=", "&=", "^=", "|=", "...", "##",
    ]


def test_lex_skips_comments_and_tracks_locations():
    source = "int/* block\ncomment */x;\n// line comment\nreturn y;\n"
    toks = [t for t in lex(source, "loc.c") if t.kind is not TokenKind.EOF]
    assert [(t.value, t.location.line, t.location.column) for t in toks] == [
        ("int", 1, 1),
        ("x", 2, 11),
        (";", 2, 12),
        ("return", 4, 1),
        ("y", 4, 8),
        (";", 4, 9),
    ]


def test_lex_hash_and_preprocessing_tokens():
    toks = [t for t in lex("#include <stdio.h>\n#define X 1\n") if t.kind is not TokenKind.EOF]
    assert toks[0].kind is TokenKind.PUNCT and toks[0].value == "#"
    assert toks[1].kind is TokenKind.IDENT and toks[1].value == "include"
    assert any(t.value == "define" for t in toks)


def test_lex_reports_unterminated_block_comment():
    with pytest.raises(CompileError, match="unterminated block comment"):
        lex("/* nope", "bad.c")


def test_lex_reports_unterminated_string():
    with pytest.raises(CompileError, match="unterminated string literal"):
        lex('"unterminated', "bad.c")


def test_lex_reports_unterminated_char():
    with pytest.raises(CompileError, match="unterminated character constant"):
        lex("'a", "bad.c")


def test_lex_rejects_floating_point_constant():
    with pytest.raises(CompileError, match="floating-point"):
        lex("1.0", "bad.c")


def test_lex_rejects_invalid_hex_constant():
    with pytest.raises(CompileError, match="invalid hexadecimal constant"):
        lex("0x", "bad.c")


def test_lex_reports_location_in_error():
    with pytest.raises(CompileError) as excinfo:
        lex("int x;\n@\n", "err.c")
    assert excinfo.value.location.file == "err.c"
    assert excinfo.value.location.line == 2
    assert excinfo.value.location.column == 1
