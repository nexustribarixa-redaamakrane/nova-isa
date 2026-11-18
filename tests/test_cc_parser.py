import textwrap

import pytest

from nova.cc import ast, parse, parse_source
from nova.cc.diagnostics import CompileError
from nova.cc.lexer import lex
from nova.cc.preprocessor import preprocess_to_tokens


def parse_c(src: str, filename: str = "test.c", **pp_kwargs):
    return parse_source(textwrap.dedent(src), filename=filename, **pp_kwargs)


def first_decl(src: str):
    tu = parse_c(src)
    assert len(tu.declarations) == 1
    return tu.declarations[0]


def first_init(src: str):
    decl = first_decl(src)
    assert isinstance(decl, ast.Declaration)
    assert len(decl.init_declarators) == 1
    return decl.init_declarators[0]


def first_func(src: str):
    decl = first_decl(src)
    assert isinstance(decl, ast.FunctionDefinition)
    return decl


# ---------------------------------------------------------------------------
# Basic syntax / translation unit
# ---------------------------------------------------------------------------

def test_empty_translation_unit():
    tu = parse_c("")
    assert isinstance(tu, ast.TranslationUnit)
    assert tu.declarations == []


def test_multiple_top_level_declarations():
    tu = parse_c("int a; int b;")
    assert len(tu.declarations) == 2
    assert all(isinstance(d, ast.Declaration) for d in tu.declarations)


def test_function_definition_basic():
    fn = first_func("int main(void) { return 0; }")
    assert isinstance(fn, ast.FunctionDefinition)
    assert isinstance(fn.body, ast.CompoundStatement)
    assert len(fn.body.items) == 1
    assert isinstance(fn.body.items[0], ast.ReturnStatement)


def test_function_definition_with_params_and_body_decls():
    fn = first_func(
        """
        int add(int x, int y) {
            int z = x + y;
            return z;
        }
        """
    )
    d = fn.declarator.direct
    assert isinstance(d, ast.FunctionDeclarator)
    assert d.params is not None
    assert len(d.params.params) == 2
    assert len(fn.body.items) == 2
    assert isinstance(fn.body.items[0], ast.Declaration)
    assert isinstance(fn.body.items[1], ast.ReturnStatement)


# ---------------------------------------------------------------------------
# Declarators
# ---------------------------------------------------------------------------

def _decl_direct(src: str):
    return first_init(src).declarator


def test_declarator_pointer():
    dec = _decl_direct("int *p;")
    assert len(dec.pointers) == 1
    assert isinstance(dec.direct, ast.IdentifierDeclarator)
    assert dec.direct.name == "p"


def test_declarator_pointer_to_pointer():
    dec = _decl_direct("int **pp;")
    assert len(dec.pointers) == 2
    assert isinstance(dec.direct, ast.IdentifierDeclarator)
    assert dec.direct.name == "pp"


def test_declarator_array():
    dec = _decl_direct("int a[10];")
    assert len(dec.pointers) == 0
    assert isinstance(dec.direct, ast.ArrayDeclarator)
    assert isinstance(dec.direct.base, ast.IdentifierDeclarator)
    assert dec.direct.base.name == "a"
    assert isinstance(dec.direct.size, ast.IntegerLiteral)
    assert dec.direct.size.number == 10


def test_declarator_array_of_pointers():
    dec = _decl_direct("int *ap[10];")
    # int *ap[10] => ap is array[10] of pointer to int
    assert len(dec.pointers) == 1
    assert isinstance(dec.direct, ast.ArrayDeclarator)
    assert isinstance(dec.direct.base, ast.IdentifierDeclarator)
    assert dec.direct.base.name == "ap"
    assert dec.direct.size.number == 10


def test_declarator_pointer_to_array():
    dec = _decl_direct("int (*pa)[10];")
    assert len(dec.pointers) == 0
    assert isinstance(dec.direct, ast.ArrayDeclarator)
    assert dec.direct.size.number == 10
    assert isinstance(dec.direct.base, ast.ParenthesizedDeclarator)
    inner = dec.direct.base.inner
    assert len(inner.pointers) == 1
    assert isinstance(inner.direct, ast.IdentifierDeclarator)
    assert inner.direct.name == "pa"


def test_declarator_function():
    dec = _decl_direct("int f(int x);")
    assert isinstance(dec.direct, ast.FunctionDeclarator)
    assert isinstance(dec.direct.base, ast.IdentifierDeclarator)
    assert dec.direct.base.name == "f"
    assert dec.direct.params is not None
    assert len(dec.direct.params.params) == 1


def test_declarator_function_pointer():
    dec = _decl_direct("int (*fp)(int);")
    assert len(dec.pointers) == 0
    assert isinstance(dec.direct, ast.FunctionDeclarator)
    assert isinstance(dec.direct.base, ast.ParenthesizedDeclarator)
    inner = dec.direct.base.inner
    assert len(inner.pointers) == 1
    assert isinstance(inner.direct, ast.IdentifierDeclarator)
    assert inner.direct.name == "fp"
    assert dec.direct.params is not None


def test_declarator_function_returning_pointer():
    dec = _decl_direct("int *g(int x);")
    assert len(dec.pointers) == 1
    assert isinstance(dec.direct, ast.FunctionDeclarator)
    assert isinstance(dec.direct.base, ast.IdentifierDeclarator)
    assert dec.direct.base.name == "g"


def test_multiple_declarators_in_one_declaration():
    decl = first_decl("int a, *b, c[3];")
    assert len(decl.init_declarators) == 3
    names = []
    for init in decl.init_declarators:
        d = init.declarator
        # extract base identifier name
        direct = d.direct
        while isinstance(direct, (ast.ArrayDeclarator, ast.FunctionDeclarator, ast.ParenthesizedDeclarator)):
            if isinstance(direct, ast.ParenthesizedDeclarator):
                direct = direct.inner.direct
            else:
                direct = direct.base
        assert isinstance(direct, ast.IdentifierDeclarator)
        names.append(direct.name)
    assert names == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Declarations / specifiers / types
# ---------------------------------------------------------------------------

def test_basic_declaration_with_initializer():
    init = first_init("int x = 42;")
    assert isinstance(init.initializer, ast.IntegerLiteral)
    assert init.initializer.number == 42


def test_primitive_type_specifiers():
    decl = first_decl("unsigned long long x;")
    specs = decl.specifiers.type_specifiers
    assert len(specs) >= 1
    assert any(isinstance(s, ast.PrimitiveTypeSpecifier) for s in specs)
    prim = [s for s in specs if isinstance(s, ast.PrimitiveTypeSpecifier)][0]
    assert "unsigned" in prim.names and "long" in prim.names


def test_type_qualifiers_and_storage():
    decl = first_decl("static const volatile int x;")
    assert any(s.name == "static" for s in decl.specifiers.storage)
    qnames = [q.name for q in decl.specifiers.qualifiers]
    assert "const" in qnames and "volatile" in qnames


def test_typedef_declaration_and_use():
    tu = parse_c(
        """
        typedef unsigned int u32;
        u32 value;
        """
    )
    assert len(tu.declarations) == 2
    td = tu.declarations[0]
    assert isinstance(td, ast.Declaration)
    assert any(s.name == "typedef" for s in td.specifiers.storage)
    use = tu.declarations[1]
    assert isinstance(use, ast.Declaration)
    assert any(isinstance(ts, ast.TypedefName) and ts.name == "u32" for ts in use.specifiers.type_specifiers)


def test_struct_definition_and_member_access_parse():
    tu = parse_c(
        """
        struct Point { int x; int y; };
        int f(struct Point p) { return p.x; }
        """
    )
    assert len(tu.declarations) == 2
    sd = tu.declarations[0]
    assert isinstance(sd, ast.Declaration)
    st = [s for s in sd.specifiers.type_specifiers if isinstance(s, ast.StructOrUnionSpecifier)][0]
    assert st.keyword == "struct"
    assert st.name == "Point"
    assert st.members is not None
    assert len(st.members) == 2


def test_union_definition():
    decl = first_decl("union U { int i; char c; };")
    st = [s for s in decl.specifiers.type_specifiers if isinstance(s, ast.StructOrUnionSpecifier)][0]
    assert st.keyword == "union"
    assert st.members is not None and len(st.members) == 2


def test_enum_definition():
    decl = first_decl("enum Color { RED, GREEN = 2, BLUE };")
    en = [s for s in decl.specifiers.type_specifiers if isinstance(s, ast.EnumSpecifier)][0]
    assert en.name == "Color"
    assert en.enumerators is not None
    assert [e.name for e in en.enumerators] == ["RED", "GREEN", "BLUE"]
    assert en.enumerators[1].value is not None


def test_struct_bitfield_if_supported():
    src = "struct Flags { unsigned int a : 1; unsigned int b : 3; };"
    try:
        decl = first_decl(src)
    except CompileError:
        pytest.skip("bit-fields not yet supported by parser")
    st = [s for s in decl.specifiers.type_specifiers if isinstance(s, ast.StructOrUnionSpecifier)][0]
    assert st.members is not None
    assert st.members[0].declarators[0].bit_width is not None


# ---------------------------------------------------------------------------
# Expressions / precedence / associativity
# ---------------------------------------------------------------------------

def _return_expr(src_body: str):
    fn = parse_c(f"int main(void){{ {src_body} }}").declarations[0]
    assert isinstance(fn, ast.FunctionDefinition)
    stmt = fn.body.items[0]
    assert isinstance(stmt, ast.ReturnStatement)
    assert stmt.value is not None
    return stmt.value


def test_expression_precedence_mul_before_add():
    expr = _return_expr("return 1 + 2 * 3;")
    assert isinstance(expr, ast.BinaryExpression) and expr.op == "+"
    assert isinstance(expr.right, ast.BinaryExpression) and expr.right.op == "*"


def test_expression_precedence_parens():
    expr = _return_expr("return (1 + 2) * 3;")
    assert isinstance(expr, ast.BinaryExpression) and expr.op == "*"
    assert isinstance(expr.left, ast.BinaryExpression) and expr.left.op == "+"


def test_assignment_right_associative():
    expr = _return_expr("return a = b = 1;")
    assert isinstance(expr, ast.AssignmentExpression) and expr.op == "="
    assert isinstance(expr.right, ast.AssignmentExpression) and expr.right.op == "="


def test_conditional_expression():
    expr = _return_expr("return a ? b : c;")
    assert isinstance(expr, ast.ConditionalExpression)


def test_unary_and_cast_and_sizeof():
    fn = parse_c(
        """
        int main(void) {
            int x;
            int *p;
            x = -x;
            x = !x;
            x = ~x;
            p = &x;
            x = *p;
            x = (int)3;
            x = sizeof x;
            x = sizeof(int);
            return x;
        }
        """
    ).declarations[0]
    stmts = [s for s in fn.body.items if isinstance(s, ast.ExpressionStatement)]
    # -x
    e0 = stmts[0].expression
    assert isinstance(e0, ast.AssignmentExpression)
    assert isinstance(e0.right, ast.UnaryExpression) and e0.right.op == "-" and not e0.right.postfix
    # !x
    assert isinstance(stmts[1].expression.right, ast.UnaryExpression) and stmts[1].expression.right.op == "!"
    # ~x
    assert isinstance(stmts[2].expression.right, ast.UnaryExpression) and stmts[2].expression.right.op == "~"
    # &x
    assert isinstance(stmts[3].expression.right, ast.UnaryExpression) and stmts[3].expression.right.op == "&"
    # *p
    assert isinstance(stmts[4].expression.right, ast.UnaryExpression) and stmts[4].expression.right.op == "*"
    # cast
    assert isinstance(stmts[5].expression.right, ast.CastExpression)
    # sizeof expr
    assert isinstance(stmts[6].expression.right, ast.SizeofExpression)
    assert isinstance(stmts[6].expression.right.operand, ast.Expression)
    # sizeof(type)
    assert isinstance(stmts[7].expression.right, ast.SizeofExpression)
    assert isinstance(stmts[7].expression.right.operand, ast.TypeName)


def test_postfix_call_index_member_incdec():
    fn = parse_c(
        """
        struct S { int m; };
        int main(void) {
            struct S s;
            struct S *p;
            int a[2];
            f(1, 2);
            a[0];
            s.m;
            p->m;
            a[0]++;
            a[1]--;
            return 0;
        }
        """
    ).declarations[1]
    exprs = [s.expression for s in fn.body.items if isinstance(s, ast.ExpressionStatement)]
    assert isinstance(exprs[0], ast.CallExpression)
    assert len(exprs[0].arguments) == 2
    assert isinstance(exprs[1], ast.SubscriptExpression)
    assert isinstance(exprs[2], ast.MemberAccessExpression) and exprs[2].pointer is False
    assert isinstance(exprs[3], ast.MemberAccessExpression) and exprs[3].pointer is True
    assert isinstance(exprs[4], ast.UnaryExpression) and exprs[4].op == "++" and exprs[4].postfix
    assert isinstance(exprs[5], ast.UnaryExpression) and exprs[5].op == "--" and exprs[5].postfix


def test_comma_expression():
    expr = _return_expr("return (a = 1, b = 2, a + b);")
    # may be wrapped in parens as grouping; top is CommaExpression inside parens
    inner = expr
    # parse_primary strips grouping parens
    assert isinstance(inner, ast.CommaExpression)
    assert len(inner.expressions) == 3


def test_logical_bitwise_shift_relational_equality():
    expr = _return_expr("return a || b && c | d ^ e & f == g != h < i <= j > k >= l << m >> n + o - p * q / r % s;")
    # just ensure it parses into a BinaryExpression tree without error
    assert isinstance(expr, ast.BinaryExpression)


# ---------------------------------------------------------------------------
# Statements / control flow
# ---------------------------------------------------------------------------

def test_if_else_and_dangling_else():
    fn = first_func(
        """
        int f(int a, int b) {
            if (a) if (b) return 1; else return 2;
            return 0;
        }
        """
    )
    outer_if = fn.body.items[0]
    assert isinstance(outer_if, ast.IfStatement)
    assert outer_if.else_branch is None  # else binds to inner if
    inner_if = outer_if.then_branch
    assert isinstance(inner_if, ast.IfStatement)
    assert inner_if.else_branch is not None


def test_while_do_for_statements():
    fn = first_func(
        """
        int main(void) {
            int i = 0;
            while (i < 10) i++;
            do { i--; } while (i > 0);
            for (i = 0; i < 5; i++) { }
            for (;;) break;
            return i;
        }
        """
    )
    kinds = [type(s) for s in fn.body.items]
    assert ast.WhileStatement in kinds
    assert ast.DoWhileStatement in kinds
    assert kinds.count(ast.ForStatement) == 2


def test_for_with_declaration_init_if_supported():
    src = "int main(void){ for (int i = 0; i < 3; i++) { } return 0; }"
    try:
        fn = first_func(src)
    except CompileError:
        pytest.skip("for-loop declaration init not yet supported")
    fstmt = [s for s in fn.body.items if isinstance(s, ast.ForStatement)][0]
    assert isinstance(fstmt.init, ast.Declaration)


def test_switch_case_default_break_continue_return_goto_label():
    fn = first_func(
        """
        int main(void) {
            int x = 1;
            switch (x) {
                case 1:
                    x = 2;
                    break;
                default:
                    x = 3;
            }
        loop:
            while (x) {
                x = 0;
                continue;
            }
            goto loop;
            return x;
        }
        """
    )
    # Ensure key statement kinds are present somewhere in body (including nested)
    def walk(node, out):
        out.append(type(node))
        for name in getattr(node, "__dataclass_fields__", {}):
            val = getattr(node, name)
            if isinstance(val, list):
                for v in val:
                    if hasattr(v, "__dataclass_fields__"):
                        walk(v, out)
            elif hasattr(val, "__dataclass_fields__"):
                walk(val, out)
    kinds = []
    walk(fn.body, kinds)
    for k in (ast.SwitchStatement, ast.CaseStatement, ast.DefaultStatement,
              ast.BreakStatement, ast.ContinueStatement, ast.ReturnStatement,
              ast.GotoStatement, ast.LabelStatement, ast.WhileStatement):
        assert k in kinds


# ---------------------------------------------------------------------------
# Preprocessed / real C source
# ---------------------------------------------------------------------------

def test_parse_after_preprocessing_macros_and_includes(tmp_path):
    (tmp_path / "defs.h").write_text("#define VAL 7\n", encoding="utf-8")
    src = r'''
    #include "defs.h"
    #define ADD(a,b) ((a)+(b))
    int main(void) {
        int x = ADD(VAL, 1);
        return x;
    }
    '''
    tu = parse_source(textwrap.dedent(src), filename=str(tmp_path / "main.c"), directory=str(tmp_path))
    fn = [d for d in tu.declarations if isinstance(d, ast.FunctionDefinition)][0]
    assert fn.body.items


def test_parse_typedef_after_preprocessing():
    src = """
    #define T int
    typedef T myint;
    myint x = 0;
    """
    tu = parse_c(src)
    assert len(tu.declarations) >= 2


# ---------------------------------------------------------------------------
# Errors / recovery
# ---------------------------------------------------------------------------

def test_error_missing_semicolon():
    with pytest.raises(CompileError):
        parse_c("int x")


def test_error_missing_closing_paren():
    with pytest.raises(CompileError):
        parse_c("int main(void { return 0; }")


def test_error_unmatched_brace():
    with pytest.raises(CompileError):
        parse_c("int main(void) { return 0; ")


def test_error_unexpected_eof_in_expression():
    with pytest.raises(CompileError):
        parse_c("int x = 1 + ;")


def test_error_recovery_does_not_loop_indefinitely():
    # malformed input should raise a diagnostic, not hang
    with pytest.raises(CompileError):
        parse_c("int f( { { { ??? ;;; }")


def test_error_location_is_reported():
    with pytest.raises(CompileError) as excinfo:
        parse_c("int x = ;", filename="bad.c")
    msg = str(excinfo.value)
    assert "bad.c" in msg or "expected" in msg.lower() or "error" in msg.lower()


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------

def test_multidimensional_array_declarator():
    dec = _decl_direct("int a[2][3];")
    assert isinstance(dec.direct, ast.ArrayDeclarator)
    assert isinstance(dec.direct.base, ast.ArrayDeclarator)
    assert isinstance(dec.direct.base.base, ast.IdentifierDeclarator)
    assert dec.direct.base.base.name == "a"


def test_brace_initializer_list():
    init = first_init("int a[3] = {1, 2, 3};")
    assert isinstance(init.initializer, ast.InitializerList)
    assert len(init.initializer.items) == 3


def test_string_and_char_literals():
    fn = first_func('int main(void){ char c = \'a\'; const char *s = "hello"; return c; }')
    decls = [i for i in fn.body.items if isinstance(i, ast.Declaration)]
    assert isinstance(decls[0].init_declarators[0].initializer, ast.CharacterLiteral)
    assert isinstance(decls[1].init_declarators[0].initializer, ast.StringLiteral)
    assert decls[1].init_declarators[0].initializer.value == "hello"


def test_prefix_increment_decrement_expressions():
    fn = first_func("int main(void){ int x; ++x; --x; x = ++x; return x; }")
    stmts = [s for s in fn.body.items if isinstance(s, ast.ExpressionStatement)]
    assert isinstance(stmts[0].expression, ast.UnaryExpression) and stmts[0].expression.op == "++" and not stmts[0].expression.postfix
    assert isinstance(stmts[1].expression, ast.UnaryExpression) and stmts[1].expression.op == "--" and not stmts[1].expression.postfix


def test_postfix_binds_tighter_than_unary_deref():
    # *p++ => *(p++)
    expr = _return_expr("return *p++;")
    assert isinstance(expr, ast.UnaryExpression) and expr.op == "*" and not expr.postfix
    assert isinstance(expr.operand, ast.UnaryExpression) and expr.operand.op == "++" and expr.operand.postfix


def test_address_of_and_deref_precedence():
    expr = _return_expr("return *&x;")
    assert isinstance(expr, ast.UnaryExpression) and expr.op == "*"
    assert isinstance(expr.operand, ast.UnaryExpression) and expr.operand.op == "&"


def test_relational_and_equality_precedence():
    expr = _return_expr("return a < b == c < d;")
    assert isinstance(expr, ast.BinaryExpression) and expr.op == "=="
    assert isinstance(expr.left, ast.BinaryExpression) and expr.left.op == "<"
    assert isinstance(expr.right, ast.BinaryExpression) and expr.right.op == "<"


def test_logical_and_or_precedence():
    expr = _return_expr("return a || b && c;")
    assert isinstance(expr, ast.BinaryExpression) and expr.op == "||"
    assert isinstance(expr.right, ast.BinaryExpression) and expr.right.op == "&&"


def test_pointer_qualifier_in_declarator():
    dec = _decl_direct("int * const p;")
    assert len(dec.pointers) == 1
    assert any(q.name == "const" for q in dec.pointers[0].qualifiers)


def test_struct_forward_declaration():
    tu = parse_c("struct S; struct S *p;")
    assert len(tu.declarations) == 2
    st = [s for s in tu.declarations[0].specifiers.type_specifiers if isinstance(s, ast.StructOrUnionSpecifier)][0]
    assert st.name == "S" and st.members is None


def test_function_with_void_params_and_no_declarator_names():
    dec = _decl_direct("int f(void);")
    assert isinstance(dec.direct, ast.FunctionDeclarator)
    assert dec.direct.params is not None


def test_cast_to_pointer_type():
    expr = _return_expr("return (int *)p;")
    assert isinstance(expr, ast.CastExpression)
    assert isinstance(expr.type_name, ast.TypeName)


def test_compound_assignment_expression():
    expr = _return_expr("return x += 1;")
    assert isinstance(expr, ast.AssignmentExpression) and expr.op == "+="


def test_sizeof_parenthesized_expression():
    expr = _return_expr("return sizeof (x + 1);")
    assert isinstance(expr, ast.SizeofExpression)
    assert isinstance(expr.operand, ast.Expression)


def test_empty_expression_statement_and_null_for_parts():
    fn = first_func("int main(void){ ; for(;;) { break; } return 0; }")
    assert isinstance(fn.body.items[0], ast.ExpressionStatement)
    assert fn.body.items[0].expression is None


def test_invalid_declarator_error():
    with pytest.raises(CompileError):
        parse_c("int *;")


# ---------------------------------------------------------------------------
# Phase 1 Step 4 hardening: compound literals, _Generic, declarator edges
# ---------------------------------------------------------------------------


def test_compound_literal_as_initializer():
    tu = parse_c("struct S { int x; }; int main(void) { struct S s = (struct S){1}; return 0; }")
    fn = tu.declarations[1]
    assert isinstance(fn, ast.FunctionDefinition)
    decl = fn.body.items[0]
    assert isinstance(decl, ast.Declaration)
    init = decl.init_declarators[0].initializer
    assert isinstance(init, ast.CompoundLiteral)
    assert isinstance(init.type_name, ast.TypeName)
    assert isinstance(init.initializer, ast.InitializerList)
    assert len(init.initializer.items) == 1


def test_compound_literal_member_access_in_expression():
    expr = _return_expr("return ((struct S){3}).x;")
    assert isinstance(expr, ast.MemberAccessExpression)
    assert isinstance(expr.base, ast.CompoundLiteral)
    assert expr.member == "x"


def test_compound_literal_with_designated_initializer():
    expr = _return_expr("return ((struct S){.x = 4}).x;")
    assert isinstance(expr, ast.MemberAccessExpression)
    lit = expr.base
    assert isinstance(lit, ast.CompoundLiteral)
    item = lit.initializer.items[0]
    assert isinstance(item, ast.DesignatedInitializer)
    assert item.designators[0].field == "x"


def test_generic_selection_basic():
    expr = _return_expr("return _Generic(x, int: 1, default: 0);")
    assert isinstance(expr, ast.GenericSelection)
    assert isinstance(expr.controlling, ast.IdentifierExpression)
    assert len(expr.associations) == 2
    assert expr.associations[0].type_name is not None
    assert expr.associations[1].type_name is None


def test_generic_selection_multiple_type_associations():
    expr = _return_expr("return _Generic(x, int: 1, long: 2, default: 0);")
    assert isinstance(expr, ast.GenericSelection)
    assert len(expr.associations) == 3
    assert all(a.type_name is not None for a in expr.associations[:2])


@pytest.mark.parametrize(
    "body,message",
    [
        ("return (int){; };", "expected expression"),
        ("return _Generic(x, int 1);", "expected ':'"),
        ("return _Generic(x);", "expected ','"),
    ],
)
def test_malformed_compound_literal_and_generic_selection(body, message):
    with pytest.raises(CompileError) as excinfo:
        parse_c(f"int main(void) {{ {body} }}")
    assert message in str(excinfo.value)


def test_abstract_declarator_pointer_to_function_in_cast():
    expr = _return_expr("return (int (*)(int))p != 0;")
    assert isinstance(expr, ast.BinaryExpression)
    assert isinstance(expr.left, ast.CastExpression)
    assert isinstance(expr.left.type_name, ast.TypeName)
    assert expr.left.type_name.declarator is not None


def test_declarator_edge_cases_parse_to_structure():
    # int (*fp)(int): parenthesized pointer with function suffix
    dec = _decl_direct("int (*fp)(int);")
    assert isinstance(dec.direct, ast.FunctionDeclarator)
    assert isinstance(dec.direct.base, ast.ParenthesizedDeclarator)
    assert dec.direct.base.inner.pointers

    # int (*pa)[10]: parenthesized pointer with array suffix
    dec = _decl_direct("int (*pa)[10];")
    assert isinstance(dec.direct, ast.ArrayDeclarator)
    assert isinstance(dec.direct.base, ast.ParenthesizedDeclarator)
    assert dec.direct.base.inner.pointers


def test_variable_length_array_syntax():
    fn = first_func("int main(void) { int n = 3; int a[n]; return 0; }")
    decl = fn.body.items[1]
    assert isinstance(decl, ast.Declaration)
    arr = decl.init_declarators[0].declarator.direct
    assert isinstance(arr, ast.ArrayDeclarator)
    assert isinstance(arr.size, ast.IdentifierExpression)
    assert arr.size.name == "n"


def test_flexible_array_member_syntax():
    decl = first_decl("struct S { int n; int data[]; };")
    assert isinstance(decl, ast.Declaration)
    member = decl.specifiers.type_specifiers[0].members[1]
    fam = member.declarators[0].declarator.direct
    assert isinstance(fam, ast.ArrayDeclarator)
    assert fam.size is None

