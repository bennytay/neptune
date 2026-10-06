"""Xacro expanded without a ROS installation, as far as the file alone determines it (ADR 0039 §4).

The expander follows xacro 2.x (ROS 2) on what one file can decide: properties (lazy, with
``default``, ``scope`` and block properties), arguments with their declared defaults, ``${...}``
expressions (``neptune.adapters.urdf.expression``), ``$(arg)`` and ``$(eval)``, macros with
plain, defaulted, forwarded (``^``, ``^|default``) and block (``*``, ``**``) parameters,
``xacro:call``, ``xacro:insert_block``, ``xacro:if`` and ``xacro:unless``.

What the file alone cannot decide is never guessed:

- ``$(find pkg)``, ``$(env)``, ``$(optenv)``, ``$(dirname)``, ``$(cwd)`` and ``$(arg)`` of an
  argument with no declared default need a ROS installation or the environment: the attribute or
  text keeps its declared form and is marked ``not_covered``.
- ``xacro:include`` is never followed. The adapter is given one source (ADR 0024), and reading
  another file would make the output depend on bytes no chunk id covers. The include is reported
  and dropped; what it would have defined stays undefined.
- An undefined property, macro or block, an invalid expression or call, and a construct this
  expander does not evaluate keep their declared form, marked ``unknown``, or are dropped (a call,
  a block, a conditional's content) and reported.

Each problem cites the source element it is about. Work, nesting, output size and macro depth are
bounded; past a bound the expansion stops with ``ExpansionLimit`` and yields nothing.
"""

import copy
import keyword
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from neptune.adapters.urdf.expression import (
    MAX_STRING,
    ExpressionError,
    Undefined,
    Unsupported,
    Value,
    boolean,
    evaluate,
    literal,
)
from neptune.adapters.urdf.xmltree import Element
from neptune.model.finding import FindingCategory, Severity

PREFIX: Final = "xacro:"
NAMESPACE_ATTRIBUTE: Final = "xmlns:xacro"
NOT_COVERED: Final = "not_covered"
UNKNOWN: Final = "unknown"
MAX_MACRO_DEPTH: Final = 64
# The expander recurses: through elements, calls and conditionals (about NODE_COST Python frames
# each) and through properties defined by other properties (at most PROPERTY_COST frames each,
# an expression's own depth included). Counting them against MAX_NESTING keeps every expansion
# inside Python's recursion limit, whatever the stack it starts from, so where a bound is met
# depends on the document alone.
NODE_COST: Final = 4
PROPERTY_COST: Final = 72
MAX_NESTING: Final = 640
MAX_STEPS: Final = 500_000

_TOKENS: Final = (
    ("dollars", re.compile(r"\$\$+[{(]")),
    ("expression", re.compile(r"\$\{[^}]*\}")),
    ("extension", re.compile(r"\$\([^)]*\)")),
    ("text", re.compile(r"[^$]+|\$[^{($]+|\$$")),
)
_ENVIRONMENT: Final = frozenset({"cwd", "dirname", "env", "find", "optenv"})


@dataclass(frozen=True)
class Problem:
    """Something the expansion could not do, about one source element."""

    name: str  # the finding's name, after the adapter's id
    category: FindingCategory
    severity: Severity
    element: Element
    message: str
    details: dict[str, str | int]


@dataclass(frozen=True)
class Argument:
    """An argument the source declares: the text the expansion used, or ``None`` if none."""

    name: str
    value: str | None
    element: Element


@dataclass
class Expansion:
    root: Element
    arguments: list[Argument]
    problems: list[Problem]


class ExpansionLimit(Exception):
    """The expansion passed a bound; nothing of it is kept."""

    def __init__(self, what: str, limit: int, element: Element) -> None:
        super().__init__(f"the expansion {what}")
        self.what = what
        self.limit = limit
        self.element = element


class Unresolved(Exception):
    """A value the expansion cannot give; ``state`` is ``not_covered`` or ``unknown``."""

    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


def is_xacro(root: Element) -> bool:
    """Whether a document is Xacro: it declares the ``xacro`` prefix or uses it anywhere."""
    stack = [root]
    while stack:
        element = stack.pop()
        if element.tag.startswith(PREFIX) or any(
            name == NAMESPACE_ATTRIBUTE or name.startswith(PREFIX) for name, _ in element.attributes
        ):
            return True
        stack.extend(element.elements())
    return False


def _token(text: str, position: int) -> tuple[str, str] | None:
    """The kind and text of the token at ``position``, as xacro's lexer reads it."""
    for kind, pattern in _TOKENS:
        match = pattern.match(text, position)
        if match:
            return kind, match.group(0)
    return None


def _short(text: str) -> str:
    return text if len(text) <= 80 else text[:77] + "..."


# --- Scopes ------------------------------------------------------------------------------------

_BLOCK_OUTPUT: Final = "block_output"  # a macro's block parameter, already expanded by the caller
_BLOCK_SOURCE: Final = "block_source"  # a block property, expanded where it is inserted

Stored: TypeAlias = Value | Element


@dataclass(eq=False)
class _Entry:
    value: Stored
    element: Element
    lazy: bool = False
    kind: str = "value"
    evaluating: bool = False
    failure: Unresolved | None = None


class _Symbols:
    """A scope of properties and macro parameters, looked up through its parents."""

    def __init__(self, parent: "_Symbols | None") -> None:
        self.parent = parent
        self.entries: dict[str, _Entry] = {}

    def find(self, name: str) -> "tuple[_Symbols, _Entry] | None":
        scope: _Symbols | None = self
        while scope is not None:
            if name in scope.entries:
                return scope, scope.entries[name]
            scope = scope.parent
        return None

    def top(self) -> "_Symbols":
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope


@dataclass(frozen=True)
class _Macro:
    params: tuple[str, ...]
    defaults: dict[str, tuple[bool, str | None]]  # name -> (forwarded with ^, default text)
    body: Element


class _Macros:
    def __init__(self, parent: "_Macros | None") -> None:
        self.parent = parent
        self.macros: dict[str, _Macro] = {}

    def find(self, name: str) -> _Macro | None:
        scope: _Macros | None = self
        while scope is not None:
            if name in scope.macros:
                return scope.macros[name]
            scope = scope.parent
        return None


def _split_params(text: str) -> list[str]:
    """A macro's ``params``, split on whitespace outside quotes and ``${…}`` / ``$(…)``."""
    tokens, current, closing = [], [], ""
    index = 0
    while index < len(text):
        char = text[index]
        if closing:
            current.append(char)
            if char == closing:
                closing = ""
        elif char in "'\"":
            current.append(char)
            closing = char
        elif char == "$" and text[index + 1 : index + 2] in ("{", "("):
            current.append(char + text[index + 1])
            closing = "}" if text[index + 1] == "{" else ")"
            index += 1
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _parse_param(token: str) -> tuple[str, tuple[bool, str | None] | None]:
    """``name``, ``name:=default``, ``name:=^``, ``name:=^|default`` (or ``=``)."""
    for separator in (":=", "="):
        if separator in token:
            name, _, default = token.partition(separator)
            if default.startswith("^|"):
                return name, (True, default[2:])
            if default.startswith("^"):
                return name, (True, default[1:] or None)
            return name, (False, default)
    return token, None


# --- The expander ------------------------------------------------------------------------------

# The least markup the writer adds around a name: ``<tag/>`` and `` name=""``. Counting it with the
# names and values keeps the count at or under the serialised size, so a bound is never met early,
# and makes long tag and attribute names count, so a macro repeating them is stopped too.
_ATTRIBUTE_MARKUP: Final = 4


def _markup(tag: str) -> int:
    return len(tag) + 3


class _Expander:
    def __init__(self, max_elements: int, max_depth: int, max_chars: int, chars_bound: str) -> None:
        self.max_elements = max_elements
        self.max_depth = max_depth
        self.max_chars = max_chars
        self.chars_bound = chars_bound
        self.problems: list[Problem] = []
        self.arguments: dict[str, Argument] = {}
        self.elements = 0
        self.steps = 0
        self.chars = 0
        self.macro_depth = 0
        self.nesting = 0

    # Reporting

    def report(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        element: Element,
        message: str,
        **details: str | int,
    ) -> None:
        self.problems.append(Problem(code, category, severity, element, message, details))

    def invalid(self, element: Element, message: str, severity: Severity = Severity.ERROR) -> None:
        self.report("xacro_invalid", FindingCategory.CORRUPT, severity, element, message)

    def step(self, element: Element) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise ExpansionLimit("takes more steps than the limit", MAX_STEPS, element)

    def produced(self, chars: int, element: Element) -> None:
        """Count ``chars`` of output; past the bound the expansion stops before it grows more."""
        self.chars += chars
        if self.chars > self.max_chars:
            raise ExpansionLimit(f"is larger than {self.chars_bound}", self.max_chars, element)

    # Values

    def resolve(self, symbols: _Symbols, name: str) -> Stored:
        found = symbols.find(name)
        if found is None:
            raise Undefined(name)
        scope, entry = found
        if entry.failure is not None:
            raise entry.failure
        if entry.lazy:
            if entry.evaluating:
                self.invalid(
                    entry.element,
                    f"the property {_short(name)!r} is defined in terms of itself",
                    Severity.WARNING,
                )
                raise Unresolved(UNKNOWN)
            assert isinstance(entry.value, str)
            if self.nesting + PROPERTY_COST > MAX_NESTING:
                self.invalid(
                    entry.element,
                    f"the property {_short(name)!r} is defined through too many other"
                    " properties here to evaluate",
                    Severity.WARNING,
                )
                raise Unresolved(UNKNOWN)
            entry.evaluating = True
            self.nesting += PROPERTY_COST
            try:
                entry.value = literal(self.text(entry.value, scope, entry.element))
                entry.lazy = False
            except Unresolved as unresolved:
                entry.failure = unresolved
                raise
            finally:
                entry.evaluating = False
                self.nesting -= PROPERTY_COST
        return entry.value

    def lookup(self, symbols: _Symbols) -> Callable[[str], Value]:
        def look(name: str) -> Value:
            value = self.resolve(symbols, name)
            if isinstance(value, Element):
                raise Unsupported(f"using the block {name!r} as a value")
            return value

        return look

    def expression(self, text: str, symbols: _Symbols, element: Element) -> Value:
        inner = str(self.text(text, symbols, element))
        try:
            return evaluate(inner, self.lookup(symbols))
        except Undefined as exc:
            self.report(
                "xacro_undefined",
                FindingCategory.MISSING,
                Severity.WARNING,
                element,
                f"${{{_short(text)}}} uses {exc}; it is left as declared",
                name=_short(exc.name),
            )
        except Unsupported as exc:
            self.report(
                "xacro_unsupported",
                FindingCategory.UNSUPPORTED,
                Severity.WARNING,
                element,
                f"${{{_short(text)}}} uses {exc}, which is not evaluated; it is left as declared",
            )
        except ExpressionError as exc:
            self.invalid(
                element, f"${{{_short(text)}}} cannot be evaluated: {exc}", Severity.WARNING
            )
        raise Unresolved(UNKNOWN)

    def extension(self, text: str, symbols: _Symbols, element: Element) -> str:
        inner = str(self.text(text, symbols, element)).strip()
        command, _, rest = inner.partition(" ")
        rest = rest.strip()
        if command in _ENVIRONMENT or (command == "arg" and self.argument(rest) is None):
            what = f"$({command})" if command != "arg" else f"$(arg {_short(rest)})"
            self.report(
                "xacro_not_covered",
                FindingCategory.MISSING,
                Severity.WARNING,
                element,
                f"{what} needs a ROS installation or the environment; the value is not covered",
                substitution=command,
            )
            raise Unresolved(NOT_COVERED)
        if command == "arg":
            value = self.argument(rest)
            assert value is not None
            return value
        if command == "eval":
            return str(self.evaluate_arguments(rest, element))
        self.report(
            "xacro_unsupported",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            element,
            f"$({_short(command)}) is not a substitution Neptune evaluates; left as declared",
        )
        raise Unresolved(UNKNOWN)

    def argument(self, name: str) -> str | None:
        found = self.arguments.get(name)
        return None if found is None else found.value

    def evaluate_arguments(self, text: str, element: Element) -> Value:
        def look(name: str) -> Value:
            if name not in self.arguments:
                raise Undefined(name)
            value = self.arguments[name].value
            if value is None:
                self.report(
                    "xacro_not_covered",
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    element,
                    f"$(eval) uses the argument {_short(name)!r}, which has no declared default;"
                    " the value is not covered",
                    substitution="arg",
                )
                raise Unresolved(NOT_COVERED)
            return literal(value)

        try:
            return evaluate(text, look)
        except ExpressionError as exc:
            self.invalid(element, f"$(eval) cannot be evaluated: {exc}", Severity.WARNING)
        raise Unresolved(UNKNOWN)

    def text(self, text: str, symbols: _Symbols, element: Element) -> Value:
        """Xacro's ``eval_text``: one token keeps its type, several are joined as text."""
        results: list[Value] = []
        position = 0
        while position < len(text):
            found = _token(text, position)
            if found is None:
                self.invalid(element, f"{_short(text)!r} is not valid xacro text", Severity.WARNING)
                raise Unresolved(UNKNOWN)
            kind, token = found
            position += len(token)
            if kind == "dollars":
                results.append(token[1:])
            elif kind == "expression":
                results.append(self.expression(token[2:-1], symbols, element))
            elif kind == "extension":
                results.append(self.extension(token[2:-1], symbols, element))
            else:
                results.append(token)
        if len(results) == 1:
            return results[0]
        joined = "".join(str(result) for result in results)
        if len(joined) > MAX_STRING:
            self.invalid(element, "a value is longer than the limit", Severity.WARNING)
            raise Unresolved(UNKNOWN)
        return joined

    # Elements

    def expand(self, root: Element) -> Element:
        out = Element(root.tag, [])
        self.produced(_markup(root.tag), root)
        symbols, macros = _Symbols(None), _Macros(None)
        self.attributes(root, out, symbols)
        self.children(out, root, macros, symbols, 1)
        return out

    def attributes(self, source: Element, out: Element, symbols: _Symbols) -> None:
        for name, value in source.attributes:
            if name == NAMESPACE_ATTRIBUTE or name.startswith(PREFIX):
                continue
            try:
                result = str(self.text(value, symbols, source))
            except Unresolved as unresolved:
                result = value
                out.unresolved[name] = unresolved.state
            self.produced(len(name) + len(result) + _ATTRIBUTE_MARKUP, source)
            out.attributes.append((name, result))

    def children(
        self,
        out: Element,
        source: Element,
        macros: _Macros,
        symbols: _Symbols,
        depth: int,
    ) -> None:
        """Expand ``source``'s children into ``out``; problems cite ``source``."""
        for node in source.children:
            if isinstance(node, str):
                try:
                    text = str(self.text(node, symbols, source))
                except Unresolved as unresolved:
                    text = node
                    out.unresolved["#text"] = unresolved.state
                self.produced(len(text), source)
                out.children.append(text)
            else:
                self.node(out, node, macros, symbols, depth)

    def node(
        self, out: Element, node: Element, macros: _Macros, symbols: _Symbols, depth: int
    ) -> None:
        self.step(node)
        if self.nesting + NODE_COST > MAX_NESTING:
            raise ExpansionLimit(
                "nests elements, calls and conditionals too deeply", MAX_NESTING, node
            )
        self.nesting += NODE_COST
        try:
            self.expand_node(out, node, macros, symbols, depth)
        finally:
            self.nesting -= NODE_COST

    def expand_node(
        self, out: Element, node: Element, macros: _Macros, symbols: _Symbols, depth: int
    ) -> None:
        if not node.tag.startswith(PREFIX):
            if depth >= self.max_depth:
                raise ExpansionLimit("nests deeper than max_depth", self.max_depth, node)
            self.elements += 1
            if self.elements > self.max_elements:
                raise ExpansionLimit("has more elements than max_elements", self.max_elements, node)
            self.produced(_markup(node.tag), node)
            element = Element(node.tag, [])
            self.attributes(node, element, symbols)
            self.children(element, node, macros, symbols, depth + 1)
            out.children.append(element)
            return
        directive = node.tag[len(PREFIX) :]
        if directive == "property":
            self.property(node, symbols)
        elif directive == "macro":
            self.macro(node, macros)
        elif directive == "arg":
            self.declare_argument(node, symbols)
        elif directive == "include":
            self.report(
                "xacro_include_not_followed",
                FindingCategory.MISSING,
                Severity.ERROR,
                node,
                "xacro:include names another file, which is never read while this source is;"
                " what it would define is not covered",
            )
        elif directive in ("if", "unless"):
            self.conditional(out, node, directive == "if", macros, symbols, depth)
        elif directive == "insert_block":
            self.insert_block(out, node, macros, symbols, depth)
        elif directive == "call":
            name = node.attribute("macro")
            try:
                resolved = None if name is None else str(self.text(name, symbols, node))
            except Unresolved:
                resolved = None
            if resolved:
                self.call(out, node, resolved, macros, symbols, depth, skip="macro")
            else:
                self.invalid(node, "xacro:call names no macro it can resolve; it is dropped")
        elif directive in ("element", "attribute"):
            self.report(
                "xacro_unsupported",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                node,
                f"xacro:{directive} is not expanded by Neptune; it is dropped",
            )
        else:
            self.call(out, node, directive, macros, symbols, depth)

    def property(self, node: Element, symbols: _Symbols) -> None:
        raw_name = node.attribute("name")
        try:
            name = None if raw_name is None else str(self.text(raw_name, symbols, node))
        except Unresolved:
            return
        if not name or not name.isidentifier() or keyword.iskeyword(name) or name[:2] == "__":
            self.invalid(node, "xacro:property needs a name that is a Python identifier")
            return
        value, default = node.attribute("value"), node.attribute("default")
        if value is not None and default is not None:
            self.invalid(node, f"the property {name!r} has both a value and a default")
            return
        if default is not None:
            if symbols.find(name) is not None:
                return
            value = default
        scope = node.attribute("scope")
        lazy = node.attribute("lazy_eval") not in ("false", "False", "0")
        target = symbols
        if scope == "global":
            target, lazy = symbols.top(), False
        elif scope == "parent":
            if symbols.parent is None:
                return  # xacro warns and ignores it at the top scope
            target, lazy = symbols.parent, False
        if value is None:
            target.entries["**" + name] = _Entry(node, node, kind=_BLOCK_SOURCE)
            return
        entry = _Entry(value, node)
        if not lazy:
            try:
                entry.value = self.text(value, symbols, node)
            except Unresolved as unresolved:
                entry.failure = unresolved
        entry.value = literal(entry.value) if isinstance(entry.value, str) else entry.value
        entry.lazy = lazy and isinstance(entry.value, str) and entry.failure is None
        target.entries[name] = entry

    def macro(self, node: Element, macros: _Macros) -> None:
        name = node.attribute("name")
        if name and name.startswith(PREFIX):
            name = name[len(PREFIX) :]
        if not name or name == "call" or "." in name:
            self.invalid(node, "xacro:macro needs a name, without '.', other than 'call'")
            return
        params: list[str] = []
        defaults: dict[str, tuple[bool, str | None]] = {}
        for token in _split_params(node.attribute("params") or ""):
            param, default = _parse_param(token)
            params.append(param)
            if default is not None:
                defaults[param] = default
        macros.macros[name] = _Macro(tuple(params), defaults, node)

    def declare_argument(self, node: Element, symbols: _Symbols) -> None:
        name, default = node.attribute("name"), node.attribute("default")
        if not name:
            self.invalid(node, "xacro:arg needs a name")
            return
        if name in self.arguments:
            return  # the first declaration wins, as in xacro
        value: str | None = None
        if default is not None:
            try:
                value = str(self.text(default, symbols, node))
            except Unresolved:
                value = None
        else:
            self.report(
                "xacro_not_covered",
                FindingCategory.MISSING,
                Severity.WARNING,
                node,
                f"the argument {_short(name)!r} declares no default; only the environment"
                " gives it a value, so it is not covered",
                substitution="arg",
            )
        self.arguments[name] = Argument(name, value, node)

    def conditional(
        self,
        out: Element,
        node: Element,
        keep_if: bool,
        macros: _Macros,
        symbols: _Symbols,
        depth: int,
    ) -> None:
        condition = node.attribute("value")
        if condition is None:
            self.invalid(node, "the conditional has no value; its content is dropped")
            return
        try:
            keep = boolean(self.text(condition, symbols, node))
        except Unresolved:
            self.report(
                "xacro_condition_undecided",
                FindingCategory.MISSING,
                Severity.ERROR,
                node,
                "the condition cannot be decided from this file; its content is dropped",
            )
            return
        except ExpressionError:
            self.invalid(node, "the condition is not a boolean; its content is dropped")
            return
        if keep == keep_if:
            self.children(out, node, macros, symbols, depth)

    def insert_block(
        self, out: Element, node: Element, macros: _Macros, symbols: _Symbols, depth: int
    ) -> None:
        name = node.attribute("name") or ""
        many = symbols.find("**" + name)
        found = many or symbols.find("*" + name)
        if not name or found is None:
            self.report(
                "xacro_undefined",
                FindingCategory.MISSING,
                Severity.ERROR,
                node,
                f"the block {_short(name)!r} is not defined; nothing is inserted",
                name=_short(name),
            )
            return
        entry = found[1]
        block = entry.value
        assert isinstance(block, Element)
        if entry.kind == _BLOCK_SOURCE:
            self.children(out, block, macros, symbols, depth)
            return
        for child in block.children if many is not None else [block]:
            copied = copy.deepcopy(child)
            if isinstance(copied, Element):
                self.count(copied, node, depth)
            else:  # a block's own text is output too, once per insertion
                self.step(node)
                self.produced(len(copied), node)
            out.children.append(copied)

    def count(self, element: Element, node: Element, depth: int) -> None:
        """Count a copied block's elements and nesting against the limits."""
        stack = [(element, depth)]
        while stack:
            current, level = stack.pop()
            self.step(node)
            if level >= self.max_depth:
                raise ExpansionLimit("nests deeper than max_depth", self.max_depth, node)
            self.elements += 1
            if self.elements > self.max_elements:
                raise ExpansionLimit("has more elements than max_elements", self.max_elements, node)
            self.produced(_markup(current.tag) + len(current.text()), node)
            for name, value in current.attributes:
                self.produced(len(name) + len(value) + _ATTRIBUTE_MARKUP, node)
            stack.extend((child, level + 1) for child in current.elements())

    def call(
        self,
        out: Element,
        node: Element,
        name: str,
        macros: _Macros,
        symbols: _Symbols,
        depth: int,
        skip: str = "",
    ) -> None:
        macro = macros.find(name)
        if macro is None:
            self.report(
                "xacro_undefined",
                FindingCategory.MISSING,
                Severity.ERROR,
                node,
                f"the macro {_short(name)!r} is not defined in this file; the call is dropped",
                name=_short(name),
            )
            return
        if self.macro_depth >= MAX_MACRO_DEPTH:
            raise ExpansionLimit("nests macro calls deeper than the limit", MAX_MACRO_DEPTH, node)
        params = list(macro.params)
        scoped = _Symbols(symbols)
        for attribute, value in node.attributes:
            if attribute == skip or attribute.startswith("xmlns"):
                continue
            if attribute not in params:
                self.invalid(node, f"{_short(attribute)!r} is not a parameter of {name!r}")
                return
            params.remove(attribute)
            scoped.entries[attribute] = self.parameter(value, symbols, node)
        holder = Element(node.tag, [])
        self.children(holder, node, macros, symbols, depth)
        blocks = holder.elements()
        for param in [param for param in params if param.startswith("*")]:
            if not blocks:
                self.invalid(node, f"the call to {name!r} gives too few blocks")
                return
            params.remove(param)
            scoped.entries[param] = _Entry(blocks.pop(0), node, kind=_BLOCK_OUTPUT)
        if blocks:
            self.invalid(node, f"the call to {name!r} gives a block it has no parameter for")
            return
        for param in list(params):
            if param not in macro.defaults:
                continue
            forward, default = macro.defaults[param]
            params.remove(param)
            if forward and symbols.find(param) is not None:
                try:
                    scoped.entries[param] = _Entry(self.resolve(symbols, param), macro.body)
                except Unresolved as unresolved:
                    scoped.entries[param] = _Entry("", macro.body, failure=unresolved)
                except Undefined:  # only a name defined in terms of an undefined one
                    scoped.entries[param] = _Entry("", macro.body, failure=Unresolved(UNKNOWN))
            elif default is not None:
                scoped.entries[param] = self.parameter(default, symbols, macro.body)
            else:
                self.invalid(node, f"the call to {name!r} forwards {param!r}, which is undefined")
                return
        if params:
            missing = ", ".join(_short(param) for param in params)
            self.invalid(node, f"the call to {name!r} does not give {missing}")
            return
        self.macro_depth += 1
        try:
            self.children(out, macro.body, _Macros(macros), scoped, depth)
        finally:
            self.macro_depth -= 1

    def parameter(self, value: str, symbols: _Symbols, element: Element) -> _Entry:
        try:
            result = self.text(value, symbols, element)
        except Unresolved as unresolved:
            return _Entry("", element, failure=unresolved)
        return _Entry(literal(result), element)


def expand(
    root: Element,
    *,
    max_elements: int,
    max_depth: int,
    max_chars: int,
    chars_bound: str = "max_bytes",
) -> Expansion:
    """Expand a Xacro document's root; ``ExpansionLimit`` past a bound.

    ``chars_bound`` names the setting ``max_chars`` comes from, for the limit's message.
    """
    expander = _Expander(max_elements, max_depth, max_chars, chars_bound)
    out = expander.expand(root)
    arguments = sorted(expander.arguments.values(), key=lambda argument: argument.name)
    return Expansion(out, arguments, expander.problems)
