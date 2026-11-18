"""Tests for the Nova64 type system and semantic analysis (Phase 1 Step 4)."""

import textwrap

import pytest

from nova.cc import analyze, ast, parse_source
from nova.cc import types as t
from nova.cc.diagnostics import CompileError, SourceLocation
from nova.cc.sema import Analyzer, SemanticError, Symbol


def sema(src: str, filename: str = "test.c", **kwargs):
    """Parse and semantically analyze a C source string."""
    tu = parse_source(textwrap.dedent(src), filename=filename, **kwargs)
    return analyze(tu)


def first_decl(src: str):
    tu = sema(src)
    assert len(tu.declarations) == 1
    return tu.declarations[0]


def object_types(src: str):
    """Return resolved types for all object declarations in source order."""
    tu = sema(src)
    result = []
    for decl in tu.declarations:
        if isinstance(decl, ast.Declaration):
            for idecl in decl.init_declarators:
                result.append(idecl.resolved_type)
    return result


def first_object_type(src: str):
    types_found = object_types(src)
    assert types_found
    return types_found[-1]


def expect_error(src: str, message: str = ""):
    with pytest.raises(CompileError) as exc:
        sema(src)
    if message:
        assert message in exc.value.message
    # Diagnostics must be source-located.
    assert exc.value.location.file == "test.c"
    assert exc.value.location.line >= 1
    assert exc.value.location.column >= 1
    return exc.value


# ---------------------------------------------------------------------------
# Type system: primitive types and LP64 ABI
# ---------------------------------------------------------------------------

def test_type_context_target_defaults_to_nova64():
    ctx = t.TypeContext()
    assert ctx.target == "nova64"


def test_integer_widths_and_alignment_follow_lp64_abi():
    ctx = t.TypeContext()
    expected = {
        "char_t": (1, 1, False),
        "schar_t": (1, 1, True),
        "uchar_t": (1, 1, False),
        "short_t": (2, 2, True),
        "ushort_t": (2, 2, False),
        "int_t": (4, 4, True),
        "uint_t": (4, 4, False),
        "long_t": (8, 8, True),
        "ulong_t": (8, 8, False),
        "llong_t": (8, 8, True),
        "ullong_t": (8, 8, False),
        "bool_t": (1, 1, False),
    }
    for name, (size, align, signed) in expected.items():
        ty = getattr(ctx, name)
        assert ty.sizeof() == size, name
        assert ty.alignof() == align, name
        assert t.is_signed_integer(ty, ctx) == signed, name


def test_plain_char_is_unsigned_in_nova_abi():
    ctx = t.TypeContext()
    assert ctx.char_t.signed is False
    assert t.type_eq(ctx.char_t, ctx.uchar_t) is False


def test_pointer_and_size_types_are_64_bit():
    ctx = t.TypeContext()
    p = ctx.pointer_to(ctx.int_t)
    assert p.sizeof() == 8
    assert p.alignof() == 8
    assert ctx.size_t.sizeof() == 8
    assert ctx.ptrdiff_t.sizeof() == 8


def test_floating_types_represented_but_lowering_unsupported():
    ctx = t.TypeContext()
    for ty in (ctx.float_t, ctx.double_t, ctx.ldouble_t):
        assert ty.is_floating()
        assert ty.lowering_supported is False
    assert t.is_arithmetic_type(ctx.double_t)


def test_void_is_incomplete_without_size():
    ctx = t.TypeContext()
    assert ctx.void_t.is_incomplete()
    assert ctx.void_t.sizeof() is None
    assert not t.is_complete_type(ctx.void_t)


# ---------------------------------------------------------------------------
# Type system: derived types and utilities
# ---------------------------------------------------------------------------

def test_derived_type_descriptions_and_completeness():
    ctx = t.TypeContext()
    ptr_fn = ctx.pointer_to(ctx.function(ctx.int_t, [t.ParameterType("x", ctx.int_t)]))
    arr_ptr = ctx.array_of(ctx.pointer_to(ctx.int_t), 4)
    ptr_arr = ctx.pointer_to(ctx.array_of(ctx.int_t, 4))
    assert ptr_fn.describe() == "pointer to function(int) returning int"
    assert arr_ptr.describe() == "array[4] of pointer to int"
    assert ptr_arr.describe() == "pointer to array[4] of int"
    assert t.is_complete_type(ptr_fn)
    assert t.is_complete_type(ctx.array_of(ctx.int_t, None)) is False
    assert arr_ptr.sizeof() == 32  # 4 * 8-byte pointers


def test_type_equality_and_compatibility():
    ctx = t.TypeContext()
    assert t.type_eq(ctx.int_t, ctx.int_t)
    assert not t.type_eq(ctx.int_t, ctx.long_t)
    assert t.compatible_types(ctx.array_of(ctx.int_t, None), ctx.array_of(ctx.int_t, 4))
    assert not t.compatible_types(ctx.array_of(ctx.int_t, 4), ctx.array_of(ctx.int_t, 5))
    fn1 = ctx.function(ctx.int_t, [t.ParameterType("a", ctx.int_t)])
    fn2 = ctx.function(ctx.int_t, [t.ParameterType("b", ctx.int_t)])
    assert t.compatible_types(fn1, fn2)
    assert not t.compatible_types(fn1, ctx.function(ctx.void_t, [t.ParameterType("a", ctx.int_t)]))


def test_qualifier_handling():
    ctx = t.TypeContext()
    const_int = ctx.int_t.add_qualifiers(t.Qualifiers(const=True))
    assert const_int.qualifiers.const
    assert t.type_eq(const_int, ctx.int_t) is False
    assert t.type_eq(t.strip_qualifiers(const_int), ctx.int_t)
    assert t.compatible_types(t.strip_qualifiers(const_int), ctx.int_t)


def test_integer_promotions_and_usual_arithmetic_conversions():
    ctx = t.TypeContext()
    assert t.type_eq(t.integer_promotions(ctx.char_t, ctx), ctx.int_t)
    assert t.type_eq(t.integer_promotions(ctx.short_t, ctx), ctx.int_t)
    assert t.type_eq(t.usual_arithmetic_conversions(ctx.int_t, ctx.uint_t, ctx), ctx.uint_t)
    assert t.type_eq(t.usual_arithmetic_conversions(ctx.int_t, ctx.long_t, ctx), ctx.long_t)
    assert t.type_eq(t.usual_arithmetic_conversions(ctx.int_t, ctx.double_t, ctx), ctx.double_t)


def test_decay_conversions():
    ctx = t.TypeContext()
    arr = ctx.array_of(ctx.int_t, 4)
    assert isinstance(t.decay(arr, ctx), t.PointerType)
    assert isinstance(t.decay(ctx.function(ctx.void_t, []), ctx), t.PointerType)
    assert t.type_eq(t.decay(ctx.int_t, ctx), ctx.int_t)


# ---------------------------------------------------------------------------
# Declarator type construction
# ---------------------------------------------------------------------------

def test_function_returning_pointer_vs_pointer_to_function():
    assert first_object_type("int *f(void);").describe() == "function() returning pointer to int"
    assert first_object_type("int (*fp)(int);").describe() == "pointer to function(int) returning int"


def test_array_of_pointers_vs_pointer_to_array():
    assert first_object_type("int *ap[10];").describe() == "array[10] of pointer to int"
    assert first_object_type("int (*pa)[10];").describe() == "pointer to array[10] of int"


def test_nested_arrays_keep_dimension_order():
    assert first_object_type("int a[2][3];").describe() == "array[2] of array[3] of int"


def test_pointer_to_const_vs_const_pointer():
    # Pointer to const int: the pointed-to type is qualified.
    assert "const" in first_object_type("const int *p;").pointee.describe()
    # Const pointer: the pointer object itself is qualified.
    assert first_object_type("int * const p;").qualifiers.const


def test_typedef_alias_preserves_underlying_type():
    assert first_object_type("typedef unsigned long ulong_t; ulong_t x;").describe() == "unsigned long"


# ---------------------------------------------------------------------------
# Valid semantic programs
# ---------------------------------------------------------------------------

def test_simple_valid_program():
    tu = sema(
        """
        int add(int a, int b) { return a + b; }
        int main(void) { int x = add(1, 2); return x; }
        """
    )
    assert len(tu.declarations) == 2
    assert tu.semantic_ok is True


def test_scope_shadowing_and_redeclaration():
    sema(
        """
        int x;
        int main(void) {
            int x = 1;
            { int x = 2; x = x + 1; }
            x = x + 2;
            return x;
        }
        """
    )


def test_compatible_redeclarations_and_tentative_definitions():
    sema("int x; int x; extern int x; int f(int a); int f(int a) { return a; }")


def test_struct_union_enum_and_member_access():
    sema(
        """
        struct Point { int x; int y; };
        union Value { int i; char c; };
        enum Color { RED, GREEN = 5, BLUE };
        int f(void) {
            struct Point p = {1, 2};
            union Value v;
            v.i = RED;
            return p.x + v.i + BLUE;
        }
        """
    )


def test_forward_declared_tag_and_flexible_array_member():
    sema(
        """
        struct Node;
        struct Node *next;
        struct Node { int value; struct Node *next; int data[]; };
        """
    )


def test_self_referential_struct_completes_pointer_field_pointee():
    tu = sema(
        """
        struct Node { int value; struct Node *next; };
        int f(void) {
            struct Node a = {1, 0};
            struct Node b = {2, 0};
            a.next = &b;
            return a.next->value;
        }
        """
    )
    rec = tu.scope.lookup_tag("Node").type
    assert rec.complete
    field = rec.field_map()["next"]
    assert isinstance(field.type, t.PointerType)
    # The field must reference the completed tag object, not a pre-completion
    # shallow copy left incomplete forever.
    assert field.type.pointee is rec
    assert field.type.pointee.complete


def test_control_flow_and_labels():
    sema(
        """
        int main(void) {
            int i = 0;
            while (i < 10) {
                if (i == 3) { i++; continue; }
                if (i == 7) break;
                i++;
            }
            switch (i) { case 1: break; default: break; }
            goto done;
        done:
            return i;
        }
        """
    )


def test_pointers_arrays_and_subscripts():
    sema(
        """
        int main(void) {
            int a[4] = {1, 2, 3, 4};
            int *p = a;
            p = p + 1;
            return p[0] + *(a + 2);
        }
        """
    )


def test_compound_literal_and_generic_selection():
    sema(
        """
        struct S { int x; };
        int main(void) {
            struct S s = (struct S){3};
            return _Generic(s.x, int: 1, default: 0);
        }
        """
    )


def test_bitfields_and_unnamed_member_layout():
    sema("struct Flags { unsigned int a : 1; unsigned int b : 3; unsigned int : 0; };")


# ---------------------------------------------------------------------------
# Required negative semantic tests
# ---------------------------------------------------------------------------

def test_undeclared_identifier():
    err = expect_error("int main(void) { return y; }")
    assert "undeclared identifier 'y'" in err.message


def test_duplicate_incompatible_declarations():
    expect_error("int x; double x;", "conflicting types")


def test_invalid_typedef_use():
    expect_error("typedef int T; int T;")


def test_invalid_pointer_conversion():
    expect_error("int main(void) { int x; int *p = x; return 0; }", "incompatible")


def test_invalid_assignment_to_const():
    expect_error("int main(void) { const int x = 1; x = 2; return x; }", "modifiable lvalue")


def test_calling_non_function():
    expect_error("int main(void) { int x = 1; return x(); }", "not a function")


def test_incorrect_function_argument_count():
    err = expect_error("int f(int a) { return a; } int main(void) { return f(); }")
    assert "incorrect number of arguments" in err.message


def test_invalid_return_type():
    expect_error("int *f(void) { return 1; }", "incompatible return type")


def test_invalid_member_access():
    expect_error("struct S { int a; }; int main(void) { struct S s; return s.b; }", "no member named 'b'")


def test_incomplete_object_type():
    expect_error("struct S; struct S s;", "incomplete type")


def test_invalid_array_size():
    expect_error("int a[-1];", "array size must be non-negative")


def test_invalid_break_placement():
    expect_error("int main(void) { break; return 0; }", "'break' statement not in loop or switch")


def test_invalid_continue_placement():
    expect_error("int main(void) { continue; return 0; }", "'continue' statement not in loop")


def test_invalid_goto_target():
    expect_error("int main(void) { goto nowhere; return 0; }", "undeclared label 'nowhere'")


def test_duplicate_labels():
    expect_error("int main(void) { L: L: return 0; }", "duplicate label")


def test_duplicate_case_values():
    expect_error(
        "int main(void) { switch (1) { case 1: break; case 1: break; } return 0; }",
        "duplicate case value",
    )


def test_case_outside_switch():
    expect_error("int main(void) { case 1: return 0; }", "'case' statement not in switch")


def test_multiple_default_labels():
    expect_error(
        "int main(void) { switch (1) { default: break; default: break; } return 0; }",
        "multiple default labels",
    )


def test_excess_array_initializer():
    expect_error("int a[2] = {1, 2, 3};", "excess elements")


def test_excess_struct_initializer():
    expect_error("struct S { int a; }; struct S s = {1, 2};", "excess elements")


def test_unknown_designated_initializer_field():
    expect_error("struct S { int a; }; struct S s = {.b = 1};", "field 'b' not found")


def test_void_object_declaration():
    expect_error("void v;", "incomplete type 'void'")


def test_duplicate_member_names():
    expect_error("struct S { int a; int a; };", "duplicate member")


def test_tag_kind_conflict():
    expect_error("struct S { int a; }; union S { int b; };", "different kind")


def test_duplicate_enumerator():
    expect_error("enum E { A, A };", "redeclaration of enumerator")


def test_invalid_bitfield_type():
    expect_error("struct S { double d : 3; };", "bit-field has invalid type")


def test_oversized_bitfield():
    expect_error("struct S { int x : 40; };", "exceeds type width")


def test_flexible_array_member_restrictions():
    expect_error("union U { int n; int data[]; };", "flexible array member not allowed")
    expect_error("struct S { int data[]; int n; };", "must be the last member")


def test_function_returning_or_array_of_function():
    expect_error("int f(void)[3];", "function cannot return array type")
    expect_error("int a[3](void);", "array element cannot be a function type")


# ---------------------------------------------------------------------------
# AST annotations for later compiler stages
# ---------------------------------------------------------------------------

def test_declarations_have_resolved_types():
    tu = sema("int x = 1; typedef unsigned long u64; u64 y;")
    assert tu.declarations[0].init_declarators[0].resolved_type.describe() == "int"
    assert tu.declarations[2].init_declarators[0].resolved_type.describe() == "unsigned long"


def test_expression_annotations_and_constant_values():
    fn = first_decl(
        """
        int main(void) {
            int x = 3;
            x = x + 1;
            return x;
        }
        """
    )
    assign = fn.body.items[1].expression
    assert isinstance(assign, ast.AssignmentExpression)
    assert assign.resolved_type.describe() == "int"
    assert assign.left.value_category == "lvalue"
    assert assign.left.resolved_symbol.name == "x"
    assert assign.right.value_category == "rvalue"


def test_integer_constant_expression_folding_annotation():
    fn = first_decl("int main(void) { int x = 2 * 3 + 4; return x; }")
    init = fn.body.items[0].init_declarators[0].initializer
    assert isinstance(init, ast.BinaryExpression)
    assert init.const_value == 10
    assert init.resolved_type.describe() == "int"


def test_function_annotations_and_symbol():
    fn = first_decl("int f(int a) { return a; }")
    assert isinstance(fn, ast.FunctionDefinition)
    assert isinstance(fn.symbol, Symbol)
    assert fn.symbol.kind == "function"
    assert fn.resolved_type.describe() == "function(int) returning int"
    ret = fn.body.items[0]
    assert ret.value.resolved_type.describe() == "int"
    assert ret.value.resolved_symbol.name == "a"


def test_string_literal_is_char_array_lvalue():
    fn = first_decl('int main(void) { char *s = "hello"; return 0; }')
    init = fn.body.items[0].init_declarators[0].initializer
    assert isinstance(init, ast.StringLiteral)
    assert init.value_category == "lvalue"
    assert isinstance(init.resolved_type, t.ArrayType)
    assert init.resolved_type.length == 6
    assert init.resolved_type.element.describe() == "char"


def test_struct_layout_annotations_follow_abi():
    ty = first_object_type("struct S { char a; int b; }; struct S s;")
    assert isinstance(ty, t.StructType)
    assert ty.complete
    assert ty.sizeof() == 8
    assert ty.alignof() == 4
    fields = {f.name: f for f in ty.fields}
    assert fields["a"].offset == 0
    assert fields["b"].offset == 4


def test_enum_constants_and_values():
    tu = sema("enum E { A, B = 5, C }; int x = B;")
    enum_type = tu.declarations[0].resolved_type
    assert isinstance(enum_type, t.EnumType)
    assert enum_type.complete
    init = tu.declarations[1].init_declarators[0].initializer
    assert init.resolved_symbol.name == "B"
    assert init.const_value == 5
    assert init.resolved_type.describe() == "int"


def test_type_context_exposed_on_translation_unit():
    tu = sema("int x;")
    assert isinstance(tu.type_context, t.TypeContext)
    assert tu.type_context.target == "nova64"


def test_analyze_api_accepts_explicit_target():
    tu = parse_source("int x;", filename="test.c")
    result = analyze(tu, target="nova64")
    assert result is tu


def test_semantic_error_is_source_located():
    err = expect_error("int main(void) { return undefined_name; }", "undeclared identifier")
    assert isinstance(err, SemanticError)
    assert isinstance(err.location, SourceLocation)
    assert str(err).startswith("test.c:1:")


def test_analyzer_can_be_used_directly():
    tu = parse_source("int x;", filename="test.c")
    analyzer = Analyzer(target="nova64")
    result = analyzer.analyze_translation_unit(tu)
    assert result is tu
    assert "x" in analyzer.file_scope.ordinary


# ---------------------------------------------------------------------------
# Milestone 5 semantic hardening regressions
# ---------------------------------------------------------------------------

def test_long_long_specifiers_are_not_collapsed():
    types_found = object_types("long long a; unsigned long long b; long c;")
    assert [ty.describe() for ty in types_found] == ["long long", "unsigned long long", "long"]
    assert types_found[0].sizeof() == 8
    assert t.is_signed_integer(types_found[0], t.TypeContext())
    assert not t.is_signed_integer(types_found[1], t.TypeContext())


def test_sizeof_rejects_invalid_and_incomplete_operands():
    expect_error("int n = sizeof(void);", "sizeof(void) is invalid")
    expect_error("int f(void); int n = sizeof(f);", "sizeof(function type) is invalid")
    expect_error("int n = sizeof(int[]);", "incomplete array type")
    expect_error("struct S; int n = sizeof(struct S);", "sizeof incomplete type")
    expect_error("union U; int n = sizeof(union U);", "sizeof incomplete type")


def test_sizeof_allows_completed_and_pointer_to_incomplete_types():
    tu = sema(
        """
        struct S;
        struct S *p;
        extern struct S later;
        struct S { int x; };
        int n = sizeof(struct S);
        int m = sizeof(struct S *);
        """
    )
    values = [d.init_declarators[0].initializer.const_value
              for d in tu.declarations if isinstance(d, ast.Declaration)
              and d.init_declarators and d.init_declarators[0].initializer is not None]
    assert 4 in values and 8 in values


def test_variable_length_arrays_are_diagnosed_explicitly():
    err = expect_error("int f(int n) { int a[n]; return 0; }",
                       "variable length arrays are not supported")
    assert err.location.line == 1


def test_static_function_definition_records_internal_linkage():
    tu = sema("static int helper(void) { return 1; } int f(void) { return helper(); }")
    helper = tu.declarations[0].symbol
    assert helper.name == "helper"
    assert helper.storage == "static"
    assert helper.defined


def test_conflicting_function_linkage_is_rejected():
    expect_error(
        "extern int f(void); static int f(void) { return 1; }",
        "conflicting linkage",
    )




def test_incomplete_array_is_completed_from_initializer():
    ty = first_object_type('char s[] = "abc"; int a[] = {1, 2, 3};')
    assert ty.describe() == "array[3] of int"
    strings = sema('char s[] = "abc";')
    decl = strings.declarations[0].init_declarators[0]
    assert decl.resolved_type.length == 4


def test_initializer_metadata_records_positions_and_conversions():
    tu = sema(
        """
        struct S { int a; int b; };
        int main(void) {
            struct S s = {.b = 7};
            int a[2] = {1, 2};
            return 0;
        }
        """
    )
    fn = tu.declarations[-1]
    s_init = fn.body.items[0].init_declarators[0].initializer
    b_init = s_init.items[0]
    assert b_init.field_index == 1
    assert b_init.designated_field == "b"
    a_init = fn.body.items[1].init_declarators[0].initializer
    assert [item.array_index for item in a_init.items] == [0, 1]
    small = first_decl("int main(void) { char c = 300; return 0; }")
    expr = small.body.items[0].init_declarators[0].initializer
    assert expr.implicit_cast_to.describe() == "char"
    assert expr.implicit_cast_kind == "initializer"


def test_pointer_assignment_rules_and_diagnostics():
    tu = sema(
        """
        int f(const int *dst, int *src, int n) {
            dst = src;
            src = 0;
            return 0;
        }
        """
    )
    fn = tu.declarations[0]
    dst_assign = fn.body.items[0].expression
    src_assign = fn.body.items[1].expression
    assert dst_assign.right.value_category == "lvalue"
    assert dst_assign.right.implicit_cast_to.describe() == "pointer to const int"
    assert src_assign.right.implicit_cast_to.describe() == "pointer to int"
    expect_error(
        "int f(const int *p) { int *q = p; return 0; }",
        "incompatible initializer type",
    )
    expect_error("int *p = 1;", "incompatible initializer type")


def test_null_pointer_constant_accepts_integer_zero_expressions():
    sema("int *p = 1 - 1; int *q = (0);")


def test_incompatible_call_argument_is_source_located():
    err = expect_error(
        """
        void f(int *p);
        int main(void) { f(3); return 0; }
        """,
        "incompatible argument 1",
    )
    assert err.location.line == 3


def test_floating_point_operations_are_rejected_with_source_location():
    expect_error("double x; int y = x;", "floating-point initializer expressions")
    expect_error("int f(double x) { return x; }", "floating-point return expressions")
    expect_error("double x; int main(void) { if (x) return 1; return 0; }", "floating-point condition types")


def test_named_zero_width_bitfield_is_rejected():
    expect_error("struct S { int x : 0; };", "named bit-field cannot have zero width")


def test_flexible_array_member_requires_another_named_member():
    expect_error("struct S { int data[]; };", "more than one named member")


def test_enum_values_must_fit_int():
    expect_error("enum E { A = 2147483648 };", "outside the range")


def test_incomplete_parameter_and_return_types_are_rejected():
    expect_error("struct S; void f(struct S x);", "parameter has incomplete type")
    expect_error("struct S; struct S f(void);", "incomplete type")


def test_atomic_qualifier_is_parsed():
    ty = first_object_type("_Atomic int x;")
    assert ty.describe() == "_Atomic int"
    assert ty.qualifiers.atomic


def test_function_symbol_records_parameter_scope():
    fn = first_decl("int f(int a) { return a; }")
    params_scope = fn.symbol.params_scope
    assert params_scope is not None
    assert params_scope.lookup_ordinary("a") is not None


def test_implicit_conversions_annotate_arithmetic_and_returns():
    fn = first_decl("long f(unsigned int a) { return a + 1L; }")
    add = fn.body.items[0].value
    assert add.left.implicit_cast_to.describe() == "long"

    ret = first_decl("int g(long x) { return x; }").body.items[0].value
    assert ret.implicit_cast_to.describe() == "int"
