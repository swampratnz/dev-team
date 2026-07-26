"""Mutation-lite: flip the first comparison or boolean operator in a source file.

An opt-in, advisory signal (:attr:`~dev_team.engine.EngineConfig.mutation_check`)
that fills the gap :doc:`../docs/BENCHMARKS.md` names next to the adopted
fail-to-pass check: a test suite can exercise a code path without ever pinning
its *behaviour* (e.g. asserting no exception, never asserting on the
comparison or boolean condition that makes the logic correct). A single
flipped comparison (``==``↔``!=``, ``<``↔``>=``, ``>``↔``<=``,
``is``↔``is not``, ``in``↔``not in``) or boolean operator (``and``↔``or``)
that still passes the existing suite is the textbook signature of that gap.

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
# — every ``cmpop`` has a logical-opposite flip here. Arithmetic-operator
# flips (``+``/``-``, ``*``/``/``) remain out of scope for this v1 (see
# ROADMAP growth path in the proposal).
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

_Mutant = Union[ast.Compare, ast.BoolOp]


class _FlipMutant(ast.NodeTransformer):
    """Replaces one specific ``Compare``/``BoolOp`` node's operator with its flip."""

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


def _mutation_candidates(tree: ast.AST) -> List[_Mutant]:
    """Every flippable ``Compare``/``BoolOp`` node in ``tree``.

    A chained comparison (``a < b < c``, more than one op) is not a
    candidate — conservative by design, mirroring
    :func:`dev_team.engine._is_test_path`'s "skip the ambiguous case" stance.
    Every single-op ``Compare`` node otherwise qualifies: :data:`_FLIPS` now
    maps every :class:`ast.cmpop` subtype to its logical opposite, so there
    is no longer an "operator outside ``_FLIPS``" case to guard against
    (unlike the chained-comparison case above). Every ``BoolOp`` node
    qualifies unconditionally too.
    """

    candidates: List[_Mutant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                continue
            candidates.append(node)
        elif isinstance(node, ast.BoolOp):
            candidates.append(node)
    return candidates


def mutate_first_mutant(source: str) -> Optional[str]:
    """Flip the first mutable comparison or boolean operator in ``source``.

    Walks the parsed AST for every single-operator comparison using one of
    ``==``/``!=``/``<``/``>=``/``>``/``<=``/``is``/``is not``/``in``/
    ``not in`` and every boolean operator (``and``/``or``), picks the one
    earliest in source order (by line, then column), flips it to its
    logical opposite, and returns the unparsed mutated source.

    Returns ``None`` — a silent skip, never an error — when ``source`` does
    not parse, or contains no flippable comparison or boolean operator. This
    is the common case (a diff that's pure new functions, imports, or
    dataclass fields) and must never be treated as a failure.
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
