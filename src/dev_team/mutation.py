"""Mutation-lite: flip the first comparison, boolean, or arithmetic operator
in a source file.

An opt-in, advisory signal (:attr:`~dev_team.engine.EngineConfig.mutation_check`)
that fills the gap :doc:`../docs/BENCHMARKS.md` names next to the adopted
fail-to-pass check: a test suite can exercise a code path without ever pinning
its *behaviour* (e.g. asserting no exception, never asserting on the
comparison or boolean condition that makes the logic correct). A single
flipped comparison (``==``↔``!=``, ``<``↔``>=``, ``>``↔``<=``,
``is``↔``is not``, ``in``↔``not in``), boolean operator (``and``↔``or``), or
arithmetic operator (``+``↔``-``, ``*``↔``/``) — on a plain expression
(``ast.BinOp``) or an augmented assignment (``ast.AugAssign``, e.g.
``x += 1``) alike — that still passes the existing suite is the textbook
signature of that gap.

This module is a pure, dependency-free AST transform — no subprocess, no
network, no model call. It never mutates anything on disk itself; the caller
(:meth:`dev_team.engine.DeliveryEngine._mutation_check`) is responsible for
writing the mutated source to a real file, evaluating gates, and restoring
the original content.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Optional, Type, Union

# The comparison-operator flips this mutator knows: each maps to its logical
# opposite, so a mutant that still passes the suite means the suite never
# distinguished the two. Covers equality/ordering (``==``/``!=``/``<``/``>=``/
# ``>``/``<=``) and identity/membership (``is``/``is not``/``in``/``not in``)
# — every ``cmpop`` has a logical-opposite flip here.
_FLIPS: Dict[Type[ast.cmpop], Type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

# The boolean-operator flips this mutator knows: ``and``/``or`` never chain
# ambiguously the way ``is``/``in`` do, so every syntactically valid
# ``BoolOp`` qualifies (unlike ``Compare``, there is no "chained, skip it"
# case to guard against).
_BOOL_FLIPS: Dict[Type[ast.boolop], Type[ast.boolop]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

# The arithmetic-operator flips this mutator knows: exactly the two pairs
# named as v1's deferred follow-up (``+``/``-``, ``*``/``/``). Deliberately
# excludes every other ``ast.operator`` (``FloorDiv``, ``Mod``, ``Pow``,
# bitwise ``&``/``|``/``^``, matrix ``@``) — a ``BinOp`` or ``AugAssign``
# using one of those alone is not a candidate, by construction of not being a
# key here. ``AugAssign.op`` is the same ``ast.operator`` subtype as
# ``BinOp.op``, so this one table covers both node kinds.
_ARITH_FLIPS: Dict[Type[ast.operator], Type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

_Mutant = Union[ast.Compare, ast.BoolOp, ast.BinOp, ast.AugAssign]


class _FlipMutant(ast.NodeTransformer):
    """Replaces one specific ``Compare``/``BoolOp``/``BinOp``/``AugAssign`` node's operator with its flip."""

    def __init__(self, target: _Mutant) -> None:
        self._target = target

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if node is self._target:
            flipped = _FLIPS[type(node.ops[0])]()
            node.ops = [ast.copy_location(flipped, node.ops[0])]
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if node is self._target:
            flipped = _BOOL_FLIPS[type(node.op)]()
            node.op = ast.copy_location(flipped, node.op)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if node is self._target:
            flipped = _ARITH_FLIPS[type(node.op)]()
            node.op = ast.copy_location(flipped, node.op)
        return node

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        self.generic_visit(node)
        if node is self._target:
            flipped = _ARITH_FLIPS[type(node.op)]()
            node.op = ast.copy_location(flipped, node.op)
        return node


def _mutation_candidates(tree: ast.AST) -> List[_Mutant]:
    """Every flippable ``Compare``/``BoolOp``/``BinOp``/``AugAssign`` node in ``tree``.

    A chained comparison (``a < b < c``, more than one op) is not a
    candidate — conservative by design, mirroring
    :func:`dev_team.engine._is_test_path`'s "skip the ambiguous case" stance.
    Every single-op ``Compare`` node otherwise qualifies: :data:`_FLIPS` now
    maps every :class:`ast.cmpop` subtype to its logical opposite, so there
    is no longer an "operator outside ``_FLIPS``" case to guard against
    (unlike the chained-comparison case above). Every ``BoolOp`` node
    qualifies unconditionally too. A ``BinOp`` or ``AugAssign`` qualifies
    only when its operator is a key in :data:`_ARITH_FLIPS`
    (``+``/``-``/``*``/``/``, covering both ``x + y`` and ``x += y`` style
    arithmetic) — every other :class:`ast.operator` (``%``, ``**``, ``//``,
    ``&``, ``|``, ``^``, ``@``) is out of scope and never selected.
    """

    candidates: List[_Mutant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                continue
            candidates.append(node)
        elif isinstance(node, ast.BoolOp):
            candidates.append(node)
        elif isinstance(node, ast.BinOp) and type(node.op) in _ARITH_FLIPS:
            candidates.append(node)
        elif isinstance(node, ast.AugAssign) and type(node.op) in _ARITH_FLIPS:
            candidates.append(node)
    return candidates


def mutate_first_mutant(source: str) -> Optional[str]:
    """Flip the first mutable comparison, boolean, or arithmetic operator in
    ``source``.

    Walks the parsed AST for every single-operator comparison using one of
    ``==``/``!=``/``<``/``>=``/``>``/``<=``/``is``/``is not``/``in``/
    ``not in``, every boolean operator (``and``/``or``), and every
    arithmetic operator using one of ``+``/``-``/``*``/``/`` — whether
    written as a ``BinOp`` expression (``x + y``) or an augmented assignment
    (``x += y``) — picks the one earliest in source order (by line, then
    column), flips it to its logical (or arithmetic) opposite, and returns
    the unparsed mutated source.

    Returns ``None`` — a silent skip, never an error — when ``source`` does
    not parse, or contains no flippable comparison, boolean, or arithmetic
    operator. This is the common case (a diff that's pure new functions,
    imports, or dataclass fields) and must never be treated as a failure.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    candidates = _mutation_candidates(tree)
    if not candidates:
        return None

    target = min(candidates, key=lambda node: (node.lineno, node.col_offset))
    mutated = _FlipMutant(target).visit(tree)
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated)
