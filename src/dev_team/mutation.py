"""Mutation-lite: flip the first comparison operator in a source file.

An opt-in, advisory signal (:attr:`~dev_team.engine.EngineConfig.mutation_check`)
that fills the gap :doc:`../docs/BENCHMARKS.md` names next to the adopted
fail-to-pass check: a test suite can exercise a code path without ever pinning
its *behaviour* (e.g. asserting no exception, never asserting on the
comparison that makes the logic correct). A single flipped comparison
(``==``↔``!=``, ``<``↔``>=``, ``>``↔``<=``) that still passes the
existing suite is the textbook signature of that gap.

This module is a pure, dependency-free AST transform — no subprocess, no
network, no model call. It never mutates anything on disk itself; the caller
(:meth:`dev_team.engine.DeliveryEngine._mutation_check`) is responsible for
writing the mutated source to a real file, evaluating gates, and restoring
the original content.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Optional, Type

# The comparison-operator flips this mutator knows: each maps to its logical
# opposite, so a mutant that still passes the suite means the suite never
# distinguished the two. Identity/membership operators (``is``, ``in``, ...)
# are deliberately excluded — flipping them is a different mutation class,
# out of scope for this v1 (see ROADMAP growth path in the proposal).
_FLIPS: Dict[Type[ast.cmpop], Type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
}


class _FlipComparison(ast.NodeTransformer):
    """Replaces one specific ``Compare`` node's operator with its flip."""

    def __init__(self, target: ast.Compare) -> None:
        self._target = target

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if node is self._target:
            flipped = _FLIPS[type(node.ops[0])]()
            node.ops = [ast.copy_location(flipped, node.ops[0])]
        return node


def _mutable_comparisons(tree: ast.AST) -> List[ast.Compare]:
    """Every ``Compare`` node in ``tree`` with a single, flippable operator.

    A chained comparison (``a < b < c``, more than one op) and a comparison
    using an operator outside :data:`_FLIPS` (``is``, ``in``, ...) are not
    candidates — conservative by design, mirroring
    :func:`dev_team.engine._is_test_path`'s "skip the ambiguous case" stance.
    """

    candidates: List[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if len(node.ops) != 1:
            continue
        if type(node.ops[0]) not in _FLIPS:
            continue
        candidates.append(node)
    return candidates


def mutate_first_comparison(source: str) -> Optional[str]:
    """Flip the first mutable comparison operator in ``source``.

    Walks the parsed AST for every single-operator comparison using one of
    ``==``/``!=``/``<``/``>=``/``>``/``<=``, picks the one earliest in source
    order (by line, then column), flips it to its logical opposite, and
    returns the unparsed mutated source.

    Returns ``None`` — a silent skip, never an error — when ``source`` does
    not parse, or contains no flippable comparison. This is the common case
    (a diff that's pure new functions, imports, or dataclass fields) and must
    never be treated as a failure.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    candidates = _mutable_comparisons(tree)
    if not candidates:
        return None

    target = min(candidates, key=lambda node: (node.lineno, node.col_offset))
    mutated = _FlipComparison(target).visit(tree)
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated)
