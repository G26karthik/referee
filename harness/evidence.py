"""Invariant 1: a model supplies a QUOTE, the harness re-finds it. Nothing a model writes
about the paper is kept unless its words occur in the parsed text.

Matching is on a FLATTENED projection (whitespace removed, case and typographic variants
folded) because a PDF breaks sentences wherever a column ends; what is returned is sliced
from the original text, so a reader is shown the paper's own words. A quote must occur
exactly once: an ambiguous quote addresses nothing. A line-break hyphen ("gener-\\nation")
is dropped only after an exact search fails, so a verbatim quote never resolves through a
normalisation.
"""
from __future__ import annotations

import ast
import bisect
import operator
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

QUOTE_MIN = 8          # shorter than this, a quote ("the ") addresses nothing in particular
_FOLD = str.maketrans({"−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
                       "‘": "'", "’": "'", "“": '"', "”": '"', "­": ""})


def flatten(text: str) -> tuple[str, list[int]]:
    """(flat, index into `text` of each flat char)."""
    chars, offs = [], []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", ch.translate(_FOLD)).lower():
            if not c.isspace():
                chars.append(c)
                offs.append(i)
    return "".join(chars), offs


def flat(text: str) -> str:
    return flatten(text)[0]


class Paper:
    """The parsed paper as the harness searches it: page texts plus visual rows."""

    def __init__(self, pages: list[str], rows: list[list[str]] | None = None):
        self.pages, self.rows = pages, rows or [[] for _ in pages]
        self.text = "\n".join(pages)
        self.starts = [0]
        for p in pages[:-1]:
            self.starts.append(self.starts[-1] + len(p) + 1)
        self.flat, self.offs = flatten(self.text)
        # The line-break-hyphen projection: indices into `flat` of the chars it keeps.
        keep = [i for i, c in enumerate(self.flat)
                if not (c == "-" and self.offs[i] + 1 < len(self.text)
                        and self.text[self.offs[i] + 1].isspace())]
        self.soft, self.soft_idx = "".join(self.flat[i] for i in keep), keep

    def page_of(self, char_index: int) -> int:
        return bisect.bisect_right(self.starts, char_index)

    def find(self, quote: str) -> tuple[dict | None, str]:
        """({"quote": verbatim text, "page": n}, "") for a unique occurrence, else (None, why)."""
        q = flat(quote)
        if len(q) < QUOTE_MIN:
            return None, f"a {len(q)}-character quote addresses nothing in particular"
        # The quote gets the same projection: a hyphen it copied before a line break goes too.
        soft_q = flat(re.sub(r"-\s+", "", quote or ""))
        for hay, idx, needle in ((self.flat, None, q), (self.soft, self.soft_idx, soft_q)):
            hits, at = [], hay.find(needle)
            while at >= 0 and len(hits) < 2:
                a, b = (at, at + len(needle) - 1) if idx is None else (idx[at], idx[at + len(needle) - 1])
                lo, hi = self.offs[a], self.offs[b] + 1
                # Word-bounded in the original text: "not significant" is not in "cannot significantly".
                if not ((lo and self.text[lo - 1].isalnum() and self.text[lo].isalnum())
                        or (hi < len(self.text) and self.text[hi].isalnum() and self.text[hi - 1].isalnum())):
                    hits.append((lo, hi))
                at = hay.find(needle, at + 1)
            if len(hits) > 1:
                return None, "the quote occurs more than once, so it addresses no single place"
            if hits:
                lo, hi = hits[0]
                return {"quote": self.text[lo:hi], "page": self.page_of(lo)}, ""
        return None, "the quote does not occur (as whole words) in the parsed paper"

    def cell(self, row_quote: str, value: str, column_quote: str, page: int = 0) -> tuple[dict | None, str]:
        """({"page", "row"}, '') when exactly one printed row holds both the row label and the
        value, on a page that prints the column header (and on `page`, if given, which
        disambiguates a number repeated across tables); (None, why) otherwise."""
        r, c = flat(row_quote), flat(column_quote)
        if len(r) < 3 or len(c) < 2:
            return None, "a table cell needs its row label and its column header"
        hits = [(p, line) for p, lines in enumerate(self.rows, 1)
                if (not page or p == page) and c in flat(self.pages[p - 1])
                for line in lines if r in flat(line) and value_in(line, value)]
        if len(hits) != 1:
            return None, (f"{len(hits)} printed rows hold both {row_quote!r} and {value!r} on a page "
                          f"printing {column_quote!r}; exactly one must (give its page to disambiguate)")
        return {"page": hits[0][0], "row": hits[0][1]}, ""


_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?(?!\.?\w)")


def _canon(tok: str) -> str:
    return tok.replace(",", "").replace("−", "-").lstrip("+")


def value_in(text: str, value: str) -> bool:
    """Is `value` printed in `text` as a standalone number (not a subscript, an index, or
    part of a fused token like "0.12.3")?"""
    v = _canon(value.strip())
    return bool(v) and any(_canon(m.group()) == v for m in _NUMBER.finditer(text or ""))


def parse_value(raw: str) -> float | None:
    try:
        return float(_canon((raw or "").strip().rstrip("%")))
    except ValueError:
        return None


def half_width(raw: str) -> float:
    """Half the rounding interval the printed digits imply: "59.3" -> 0.05."""
    try:
        exp = Decimal(_canon((raw or "").strip().rstrip("%"))).as_tuple().exponent
    except (InvalidOperation, ValueError):
        return 0.0
    return 0.5 * 10 ** exp if isinstance(exp, int) else 0.0


def command(s: str) -> str:
    """A shell command as documented: continuations joined, prompt and whitespace normalized."""
    s = re.sub(r"\\\s*\n\s*", " ", s or "").strip().strip("`").strip()
    return re.sub(r"\s+", " ", re.sub(r"^\$\s+", "", s))


def documented(path: Path | None, cmd: str) -> bool:
    """Is `cmd` a WHOLE documented command in this file — a full line or an inline code
    span, exact up to whitespace (case-sensitive: `--epochs 200` is not `--epochs 2000`)?"""
    want = command(cmd)
    if not path or len(want) < QUOTE_MIN:
        return False
    text = re.sub(r"\\\s*\n\s*", " ", path.read_text(encoding="utf-8", errors="replace"))
    return any(command(c) == want for c in text.splitlines() + re.findall(r"`([^`\n]+)`", text))


def has_word(path: Path | None, key: str) -> bool:
    """Does a code file contain `key` as a whole identifier or quoted string?"""
    if not path or not key or path.suffix.lower() not in (".py", ".sh", ".r", ".jl", ".m", ".ipynb"):
        return False
    return bool(re.search(rf"(?<![\w.]){re.escape(key)}(?!\w)", path.read_text(encoding="utf-8", errors="replace")))


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def interval(expression: str, printed: dict[str, str]) -> tuple[float, float]:
    """The range of `expression` over each operand's printed rounding interval (corners of
    the box; exact for the monotone formulas papers print). An operand printed as 61.4 is
    any value in [61.35, 61.45]: judging its rounded value alone would convict rounding."""
    import itertools
    names = list(printed)
    if not names or not any(re.search(rf"\b{re.escape(n)}\b", expression) for n in names):
        raise ValueError("the expression uses none of the paper's printed operands")
    if len(names) > 8:            # ponytail: 2^8 corners; a longer formula is split by the planner
        raise ValueError("more than 8 operands")
    vals = []
    for signs in itertools.product((-1, 1), repeat=len(names)):
        env = {n: Fraction(_canon(printed[n].strip().rstrip("%"))) + s * Fraction(str(half_width(printed[n])))
               for n, s in zip(names, signs)}
        vals.append(float(evaluate(expression, env)))
    return min(vals), max(vals)


def evaluate(expression: str, names: dict[str, Fraction]) -> Fraction:
    """Exact rational arithmetic over named operands: + - * / ** (integer powers), parens,
    integer constants. Anything else raises ValueError — a model expression is never `eval`ed,
    and a decimal constant (a number the paper did not print) is not admitted."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return Fraction(node.value)
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            base, exp = ev(node.left), ev(node.right)
            if exp.denominator != 1 or abs(exp) > 64:
                raise ValueError("only small integer powers are exact")
            return base ** int(exp)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")
    try:
        return ev(ast.parse(expression, mode="eval"))
    except (SyntaxError, ZeroDivisionError) as e:
        raise ValueError(str(e)) from e


_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


def relation(expr: str) -> tuple[str, str, str, list[str]]:
    """`lhs OP rhs` (one of < <= > >=) over named outputs -> (lhs, op, rhs, names): how a
    comparison the paper states in prose, or shows only in a figure, is checked."""
    try:
        node = ast.parse(expr or "", mode="eval").body
    except SyntaxError as e:
        raise ValueError(str(e)) from e
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP):
        raise ValueError("a relation is `lhs OP rhs` with exactly one of < <= > >=")
    names = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)})
    lhs, rhs = ast.unparse(node.left), ast.unparse(node.comparators[0])
    probe = {n: Fraction(2 * i + 3, 7) for i, n in enumerate(names)}
    for side in (lhs, rhs):                               # the same grammar as ARITHMETIC
        evaluate(side, probe)
    if not names:
        raise ValueError("a relation compares named outputs")
    return lhs, _CMP[type(node.ops[0])], rhs, names


def margin(expr: str, values: dict) -> float:
    """How far one result satisfies the relation: > 0 holds, < 0 the reverse holds."""
    lhs, op, rhs, names = relation(expr)
    env = {n: Fraction(values[n]) for n in names}
    d = evaluate(lhs, env) - evaluate(rhs, env)
    return float(-d if op in ("<", "<=") else d)
