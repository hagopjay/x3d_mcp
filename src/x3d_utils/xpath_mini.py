"""A small evaluator for the XPath subset that X3dToTurtle.xslt uses in its attribute rules.

scene_rdf reproduces the Consortium stylesheet without running XSLT. The stylesheet decides two things per
attribute with XPath tests: whether the attribute is written at all (its default-value and implicit-event
tests) and which X3D field type it has (which decides number, list or string). Those tests are copied
verbatim into X3dToTurtle-tables.json by evaluation/build_scene_rdf_tables.py and evaluated here.

The subset: string literals, numbers, variables ($name), '.', parentheses, or/and, the comparisons
= != < <= > >= (and eq ne lt le gt ge), and the functions local-name(), local-name(..), string(.),
string-length(), normalize-space(), starts-with(), ends-with(), contains(), translate(), not(), true(),
false().
Anything else is refused when the expression is parsed, so a stylesheet update that needs more fails the
table build instead of silently changing answers.
"""

import re

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<str>'[^']*'|"[^"]*")
    | (?P<num>\d+(?:\.\d*)?|\.\d+)
    | (?P<var>\$[A-Za-z_][\w.-]*)
    | (?P<op>!=|<=|>=|=|<|>|\(|\)|,|\.\.|\.)
    | (?P<name>[A-Za-z_][\w-]*(?::[A-Za-z_][\w-]*)?)
    )""", re.X)

_WORD_OPS = {"or", "and", "eq", "ne", "lt", "le", "gt", "ge"}
_CMP = {"=": "=", "eq": "=", "!=": "!=", "ne": "!=", "<": "<", "lt": "<", "<=": "<=", "le": "<=",
        ">": ">", "gt": ">", ">=": ">=", "ge": ">="}
_FUNCS = {"local-name", "string", "string-length", "normalize-space", "starts-with", "ends-with",
          "contains", "translate", "not", "true", "false"}
_XML_SPACE = re.compile(r"[ \t\r\n]+")


class XPathSubsetError(ValueError):
    """The expression uses XPath outside the supported subset."""


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise XPathSubsetError(f"cannot read XPath at: {text[pos:pos + 40]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "name" and val in _WORD_OPS:
            kind = "op"
        out.append((kind, val))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokens(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise XPathSubsetError(f"expected {value!r}, found {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.or_expr()
        if self.i != len(self.toks):
            raise XPathSubsetError(f"unexpected {self.peek()[1]!r}")
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.peek() == ("op", "or"):
            self.take()
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.cmp_expr()
        while self.peek() == ("op", "and"):
            self.take()
            node = ("and", node, self.cmp_expr())
        return node

    def cmp_expr(self):
        node = self.primary()
        kind, val = self.peek()
        if kind == "op" and val in _CMP:
            self.take()
            node = ("cmp", _CMP[val], node, self.primary())
        return node

    def primary(self):
        kind, val = self.take()
        if kind == "str":
            return ("lit", val[1:-1])
        if kind == "num":
            return ("num", float(val))
        if kind == "var":
            return ("var", val[1:])
        if (kind, val) == ("op", "("):
            node = self.or_expr()
            self.take(")")
            return node
        if (kind, val) == ("op", "."):
            return ("dot",)
        if kind == "name":
            if val not in _FUNCS:
                raise XPathSubsetError(f"function {val}() is outside the supported subset")
            self.take("(")
            args = []
            if self.peek()[1] != ")":
                while True:
                    if self.peek() == ("op", ".."):
                        self.take()
                        args.append(("parent",))
                    else:
                        args.append(self.or_expr())
                    if self.peek()[1] == ",":
                        self.take()
                        continue
                    break
            self.take(")")
            return ("fn", val, tuple(args))
        raise XPathSubsetError(f"unexpected token {val!r}")


def compile_expr(text: str):
    """Parse an expression into a nested tuple. Raises XPathSubsetError outside the subset."""
    return _Parser(text).parse()


def _normalize(s: str) -> str:
    return _XML_SPACE.sub(" ", s).strip(" ")


def _str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return "" if v is None else str(v)


def _bool(v) -> bool:
    if isinstance(v, str):
        return len(v) > 0
    if isinstance(v, float):
        return v != 0
    return bool(v)


def evaluate(node, ctx: dict):
    """Evaluate a compiled expression. ctx: 'name' (attribute local name), 'parent' (element local name),
    'value' (attribute string value) and 'vars' (variable name -> value)."""
    op = node[0]
    if op == "or":
        return _bool(evaluate(node[1], ctx)) or _bool(evaluate(node[2], ctx))
    if op == "and":
        return _bool(evaluate(node[1], ctx)) and _bool(evaluate(node[2], ctx))
    if op == "lit":
        return node[1]
    if op == "num":
        return node[1]
    if op == "var":
        try:
            return ctx["vars"][node[1]]
        except KeyError:
            raise XPathSubsetError(f"unbound variable ${node[1]}") from None
    if op == "dot":
        return True                     # the context attribute exists
    if op == "cmp":
        # `.` alone is the attribute's existence (true) in a boolean test, and its string value when it is
        # compared with a string or a number (XPath compares a node by its string value, number() of it, or
        # boolean() of it when the other side is a boolean).
        left, right, rel = node[2], node[3], node[1]
        a = None if left == ("dot",) else evaluate(left, ctx)
        b = None if right == ("dot",) else evaluate(right, ctx)
        if left == ("dot",):
            a = True if isinstance(b, bool) else ctx["value"]
        if right == ("dot",):
            b = True if isinstance(a, bool) else ctx["value"]
        if isinstance(a, bool) or isinstance(b, bool):
            a, b = _bool(a), _bool(b)
        elif isinstance(a, float) or isinstance(b, float):
            try:
                a, b = float(a), float(b)
            except ValueError:
                return rel == "!="          # NaN: unequal to everything
        else:
            a, b = _str(a), _str(b)
        return {"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[rel]
    if op == "fn":
        name, args = node[1], node[2]
        if name == "local-name":
            if not args:
                return ctx["name"]
            if args[0] == ("parent",):
                return ctx["parent"]
            raise XPathSubsetError("local-name() takes no argument or '..'")
        if name == "string":
            if not args or args[0] == ("dot",):
                return ctx["value"]
            return _str(evaluate(args[0], ctx))
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "not":
            return not _bool(evaluate(args[0], ctx))
        vals = [_str(evaluate(a, ctx)) for a in args]
        if name == "string-length":
            return float(len(vals[0] if vals else ctx["value"]))
        if name == "normalize-space":
            return _normalize(vals[0] if vals else ctx["value"])
        if name == "starts-with":
            return vals[0].startswith(vals[1])
        if name == "ends-with":
            return vals[0].endswith(vals[1])
        if name == "contains":
            return vals[1] in vals[0]
        if name == "translate":
            src, frm, to = vals
            table = {}
            for i, ch in enumerate(frm):
                table.setdefault(ord(ch), to[i] if i < len(to) else None)
            return src.translate(table)
    raise XPathSubsetError(f"cannot evaluate {node!r}")


def variables(node, acc=None) -> set:
    """Names of the variables an expression reads."""
    acc = set() if acc is None else acc
    if isinstance(node, tuple):
        if len(node) == 2 and node[0] == "var":
            acc.add(node[1])
        for part in node:
            if isinstance(part, tuple):
                variables(part, acc)
    return acc
