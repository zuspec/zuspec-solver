"""
zuspec.solver.translate -- Zuspec IR solve problems, translated for a solver.

ir-core collects a solve problem (``xf.pss_lower.constraints`` ->
``ScSolveProblem``); this package translates it, once, for whichever backend
solves it. :func:`build_solve_blob` is the dv-solve form every in-process
solver runs (be-bc's interpreter, its native engine, pssc's presolve).
"""
from __future__ import annotations

from typing import Dict, Tuple

from .errors import TranslateError
from .sink import Sink, builder_sink
from .walk import ProblemTranslator, slot_of, translate, writeback_slots

__all__ = [
    "TranslateError", "Sink", "builder_sink", "ProblemTranslator",
    "slot_of", "translate", "writeback_slots", "build_solve_blob",
]


def build_solve_blob(problem) -> Tuple[bytes, Dict[int, int]]:
    """*problem* as a relocatable dv-solve ``SolveProblem`` blob, with its
    write-back map (object field slot -> solver var_id)."""
    sp = builder_sink()
    try:
        wb = translate(problem, sp)
        return sp.finalize_bytes(), wb
    finally:
        sp.destroy()
