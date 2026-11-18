"""Abstract syntax tree definitions for :mod:`nova.cc`.

The AST is the parsed, syntax-level representation produced by the C parser.
Semantic analysis and type construction happen in later phases; declarators are
represented structurally so they can be converted into semantic types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

from .diagnostics import SourceLocation


@dataclass
class Node:
    location: SourceLocation


# ---------------------------------------------------------------------------
# Translation unit / top-level
# ---------------------------------------------------------------------------


@dataclass
class TranslationUnit(Node):
    declarations: List["ExternalDeclaration"] = field(default_factory=list)


ExternalDeclaration = Union["FunctionDefinition", "Declaration"]


# ---------------------------------------------------------------------------
# Type/specifier syntax
# ---------------------------------------------------------------------------


@dataclass
class TypeQualifier(Node):
    name: str  # const, volatile, restrict, _Atomic (parsed if seen)


@dataclass
class StorageClassSpecifier(Node):
    name: str  # typedef, extern, static, auto, register


@dataclass
class PrimitiveTypeSpecifier(Node):
    names: List[str]  # e.g. ["unsigned", "long", "long"], ["int"], ["_Bool"]


@dataclass
class StructMember(Node):
    specifiers: "DeclarationSpecifiers"
    declarators: List["StructDeclarator"]


@dataclass
class StructDeclarator(Node):
    declarator: Optional["Declarator"]
    bit_width: Optional["Expression"] = None


@dataclass
class StructOrUnionSpecifier(Node):
    keyword: str  # struct or union
    name: Optional[str]
    members: Optional[List[StructMember]] = None


@dataclass
class Enumerator(Node):
    name: str
    value: Optional["Expression"] = None


@dataclass
class EnumSpecifier(Node):
    name: Optional[str]
    enumerators: Optional[List[Enumerator]] = None


@dataclass
class TypedefName(Node):
    name: str


TypeSpecifier = Union[PrimitiveTypeSpecifier, StructOrUnionSpecifier, EnumSpecifier, TypedefName]


@dataclass
class DeclarationSpecifiers(Node):
    storage: List[StorageClassSpecifier] = field(default_factory=list)
    qualifiers: List[TypeQualifier] = field(default_factory=list)
    type_specifiers: List[TypeSpecifier] = field(default_factory=list)
    function_specifiers: List[str] = field(default_factory=list)  # inline, _Noreturn


# ---------------------------------------------------------------------------
# Declarators
# ---------------------------------------------------------------------------


@dataclass
class Pointer(Node):
    qualifiers: List[TypeQualifier] = field(default_factory=list)


@dataclass
class Declarator(Node):
    # Pointers applied in inside-out order as parsed (left side of direct declarator)
    pointers: List[Pointer] = field(default_factory=list)
    direct: Optional["DirectDeclarator"] = None


@dataclass
class DirectDeclarator(Node):
    pass


@dataclass
class IdentifierDeclarator(DirectDeclarator):
    name: str = ""


@dataclass
class AbstractDeclaratorNode(DirectDeclarator):
    """Placeholder for abstract declarators with no identifier (e.g. in type names)."""
    pass


@dataclass
class ParenthesizedDeclarator(DirectDeclarator):
    inner: Declarator = field(default_factory=lambda: Declarator(location=SourceLocation()))


@dataclass
class ArrayDeclarator(DirectDeclarator):
    base: DirectDeclarator = field(default_factory=lambda: AbstractDeclaratorNode(location=SourceLocation()))
    size: Optional["Expression"] = None
    qualifiers: List[TypeQualifier] = field(default_factory=list)
    static: bool = False


@dataclass
class ParameterDeclaration(Node):
    specifiers: DeclarationSpecifiers = field(default_factory=lambda: DeclarationSpecifiers(location=SourceLocation()))
    declarator: Optional[Declarator] = None
    abstract_declarator: Optional[Declarator] = None


@dataclass
class ParameterList(Node):
    params: List[ParameterDeclaration] = field(default_factory=list)
    variadic: bool = False


@dataclass
class FunctionDeclarator(DirectDeclarator):
    base: DirectDeclarator = field(default_factory=lambda: AbstractDeclaratorNode(location=SourceLocation()))
    params: Optional[ParameterList] = None
    identifiers: Optional[List[str]] = None  # old-style K&R identifiers (parsed, limited support)


@dataclass
class InitDeclarator(Node):
    declarator: Declarator = field(default_factory=lambda: Declarator(location=SourceLocation()))
    initializer: Optional["Initializer"] = None


Initializer = Union["Expression", "InitializerList"]


@dataclass
class Designator(Node):
    # .field or [index]
    field: Optional[str] = None
    index: Optional["Expression"] = None


@dataclass
class DesignatedInitializer(Node):
    designators: List[Designator] = field(default_factory=list)
    initializer: Initializer = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class InitializerList(Node):
    items: List[Union[Initializer, DesignatedInitializer]] = field(default_factory=list)


@dataclass
class Declaration(Node):
    specifiers: DeclarationSpecifiers = field(default_factory=lambda: DeclarationSpecifiers(location=SourceLocation()))
    init_declarators: List[InitDeclarator] = field(default_factory=list)


@dataclass
class TypeName(Node):
    specifiers: DeclarationSpecifiers = field(default_factory=lambda: DeclarationSpecifiers(location=SourceLocation()))
    declarator: Optional[Declarator] = None


@dataclass
class TypedefDeclaration(Node):
    declaration: Declaration = field(default_factory=lambda: Declaration(location=SourceLocation()))


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass
class Expression(Node):
    pass


@dataclass
class IntegerLiteral(Expression):
    value: str = "0"
    number: Optional[int] = None


@dataclass
class CharacterLiteral(Expression):
    value: str = "'\\0'"
    number: Optional[int] = None


@dataclass
class StringLiteral(Expression):
    value: str = ""
    parts: List[str] = field(default_factory=list)  # adjacent literals


@dataclass
class IdentifierExpression(Expression):
    name: str = ""


@dataclass
class UnaryExpression(Expression):
    op: str = ""
    operand: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    postfix: bool = False


@dataclass
class BinaryExpression(Expression):
    op: str = ""
    left: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    right: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class AssignmentExpression(Expression):
    op: str = "="
    left: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    right: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class ConditionalExpression(Expression):
    condition: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    then_expr: Optional[Expression] = None  # GNU extension allows omitted middle; keep optional
    else_expr: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class CastExpression(Expression):
    type_name: TypeName = field(default_factory=lambda: TypeName(location=SourceLocation()))
    operand: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class CallExpression(Expression):
    callee: Expression = field(default_factory=lambda: IdentifierExpression(location=SourceLocation(), name=""))
    arguments: List[Expression] = field(default_factory=list)


@dataclass
class SubscriptExpression(Expression):
    array: Expression = field(default_factory=lambda: IdentifierExpression(location=SourceLocation(), name=""))
    index: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class MemberAccessExpression(Expression):
    base: Expression = field(default_factory=lambda: IdentifierExpression(location=SourceLocation(), name=""))
    member: str = ""
    pointer: bool = False  # False for '.', True for '->'


@dataclass
class PostfixExpression(Expression):
    # Generic postfix wrapper if needed (e.g. compound literal in future); currently specific nodes used.
    operand: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    op: str = ""


@dataclass
class CommaExpression(Expression):
    expressions: List[Expression] = field(default_factory=list)


@dataclass
class SizeofExpression(Expression):
    operand: Union[Expression, TypeName, None] = None


@dataclass
class CompoundLiteral(Expression):
    type_name: TypeName = field(default_factory=lambda: TypeName(location=SourceLocation()))
    initializer: "InitializerList" = field(default_factory=lambda: InitializerList(location=SourceLocation()))


@dataclass
class GenericAssociation(Node):
    type_name: Optional[TypeName] = None  # None means default
    expression: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class GenericSelection(Expression):
    controlling: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    associations: List[GenericAssociation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass
class Statement(Node):
    pass


@dataclass
class ExpressionStatement(Statement):
    expression: Optional[Expression] = None


@dataclass
class CompoundStatement(Statement):
    items: List[Union[Statement, Declaration]] = field(default_factory=list)


@dataclass
class DeclarationStatement(Statement):
    declaration: Declaration = field(default_factory=lambda: Declaration(location=SourceLocation()))


@dataclass
class IfStatement(Statement):
    condition: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    then_branch: Statement = field(default_factory=lambda: ExpressionStatement(location=SourceLocation()))
    else_branch: Optional[Statement] = None


@dataclass
class SwitchStatement(Statement):
    condition: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    body: Statement = field(default_factory=lambda: CompoundStatement(location=SourceLocation()))


@dataclass
class CaseStatement(Statement):
    value: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    body: Optional[Statement] = None


@dataclass
class DefaultStatement(Statement):
    body: Optional[Statement] = None


@dataclass
class WhileStatement(Statement):
    condition: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))
    body: Statement = field(default_factory=lambda: ExpressionStatement(location=SourceLocation()))


@dataclass
class DoWhileStatement(Statement):
    body: Statement = field(default_factory=lambda: ExpressionStatement(location=SourceLocation()))
    condition: Expression = field(default_factory=lambda: IntegerLiteral(location=SourceLocation(), value="0", number=0))


@dataclass
class ForStatement(Statement):
    init: Optional[Union[Expression, Declaration]] = None
    condition: Optional[Expression] = None
    step: Optional[Expression] = None
    body: Statement = field(default_factory=lambda: ExpressionStatement(location=SourceLocation()))


@dataclass
class BreakStatement(Statement):
    pass


@dataclass
class ContinueStatement(Statement):
    pass


@dataclass
class ReturnStatement(Statement):
    value: Optional[Expression] = None


@dataclass
class GotoStatement(Statement):
    label: str = ""


@dataclass
class LabeledStatement(Statement):
    label: str = ""
    body: Statement = field(default_factory=lambda: ExpressionStatement(location=SourceLocation()))


@dataclass
class LabelStatement(LabeledStatement):
    """Ordinary label: `name:`."""
    pass


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@dataclass
class FunctionDefinition(Node):
    specifiers: DeclarationSpecifiers = field(default_factory=lambda: DeclarationSpecifiers(location=SourceLocation()))
    declarator: Declarator = field(default_factory=lambda: Declarator(location=SourceLocation()))
    body: CompoundStatement = field(default_factory=lambda: CompoundStatement(location=SourceLocation()))
    old_style_declarations: List[Declaration] = field(default_factory=list)  # K&R style, rarely used
