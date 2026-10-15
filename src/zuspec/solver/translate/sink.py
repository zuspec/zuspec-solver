"""
sink.py -- what the constraint walk writes into.

A sink is the subset of dv-solve's growable builder
(``dv_solve.builder.SolveProblemBuilder``) that :mod:`.walk` calls. Each
``expr_*`` returns a handle that a later call passes back; what a handle is is
the sink's business (the builder's are node offsets in its blob, a C-source
sink's would be the names of the C variables holding ``dvs_builder_*``
results). So the builder is a sink as it stands, and a new output is a new
class with these methods, not a second walk.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Protocol, Sequence, Tuple


class Sink(Protocol):
    def add_var(self, var_id: int, width: int, is_signed: bool,
                lo: int, hi: int) -> None: ...

    def expr_const(self, value: int, is_signed: bool = ..., width: int = ...): ...

    def expr_var(self, var_id: int): ...

    def expr_binary(self, op: int, lhs, rhs): ...

    def expr_unary(self, op: int, operand): ...

    def expr_ite(self, cond, then, els): ...

    def expr_cast(self, operand, width: int, is_signed: bool): ...

    def expr_in_range(self, value, lo, hi): ...

    def expr_in_ranges(self, value, pairs: Sequence[Tuple[object, object]]): ...

    def add_constraint(self, expr) -> None: ...

    def add_soft_constraint(self, expr, priority: int) -> None: ...

    def add_dist(self, var_id: int, entries: List[Dict]) -> None: ...

    def add_all_different(self, var_ids: Iterable[int]) -> None: ...


def builder_sink():
    """A fresh dv-solve growable builder. The caller ``finalize_bytes()``s it
    and then ``destroy()``s it."""
    try:
        from dv_solve.builder import SolveProblemBuilder
    except Exception as exc:  # pragma: no cover - environment-dependent
        from .errors import TranslateError
        raise TranslateError(
            "dv-solve is required to build a solve blob "
            "(cannot import dv_solve.builder): %s" % exc)
    return SolveProblemBuilder()
