"""Native solver back-end.

Implements the ``SolverBackend`` protocol from
``zuspec.dataclasses.solver.backend.base`` using the C native solver.
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, Optional, Tuple

# Per-class cache: type -> (ConstraintSystem, SolveProblem, var_id_map, fully_native)
# Keyed by the Python class object so each distinct @dataclass gets one entry.
# The constraint system is deterministic for a given class; caching it avoids
# re-parsing source, rebuilding the IR, and re-translating on every solve call.
_CLASS_CACHE: Dict[type, Tuple] = {}

# Constraint-template cache: reuse compiled SolveCtx across invocations
# of the same action type. Keyed by type -> (SolveCtx, problem_buf).
# Uses LRU eviction with a configurable max size.
_CTX_CACHE_MAX = 32
_CTX_CACHE: Dict[type, Tuple] = {}
_CTX_CACHE_ORDER: list = []  # LRU order (most recent at end)


def _ctx_cache_get(cls: type) -> Optional[Tuple]:
    """Retrieve a cached SolveCtx for cls, updating LRU order."""
    entry = _CTX_CACHE.get(cls)
    if entry is not None:
        # Move to end (most recently used)
        if cls in _CTX_CACHE_ORDER:
            _CTX_CACHE_ORDER.remove(cls)
        _CTX_CACHE_ORDER.append(cls)
    return entry


def _ctx_cache_put(cls: type, entry: Tuple) -> None:
    """Store a SolveCtx in the cache, evicting LRU if full."""
    if cls not in _CTX_CACHE:
        if len(_CTX_CACHE) >= _CTX_CACHE_MAX:
            # Evict least recently used
            evict = _CTX_CACHE_ORDER.pop(0)
            old_entry = _CTX_CACHE.pop(evict, None)
            if old_entry is not None:
                # Destroy the cached context
                ctx_obj = old_entry[0]
                if hasattr(ctx_obj, 'destroy'):
                    ctx_obj.destroy()
    else:
        if cls in _CTX_CACHE_ORDER:
            _CTX_CACHE_ORDER.remove(cls)
    _CTX_CACHE[cls] = entry
    _CTX_CACHE_ORDER.append(cls)


class NativeSolverBackend:
    """Back-end that drives the native C constraint solver."""

    @property
    def name(self) -> str:
        return "native"

    @property
    def available(self) -> bool:
        """True when libzsp_solver.so can be found and loaded."""
        from .lib import _load_lib
        return _load_lib() is not None

    def randomize(
        self,
        obj: Any,
        seed: Optional[int] = None,
        timeout_ms: Optional[int] = 1000,
    ) -> None:
        """Randomize all rand/randc fields in *obj* using the native solver.

        The constraint system and translated SolveProblem are cached per class
        so that repeated calls only pay for SolveCtx creation + solve (C-level,
        microseconds) rather than re-parsing and re-translating the IR on every
        call.
        """
        from zuspec.dataclasses.solver._core_solve import (
            _extract_struct_type,
            _apply_solution,
            _solve_constraint_system,
            RandomizationError,
        )
        from zuspec.dataclasses.solver.frontend.constraint_system_builder import (
            ConstraintSystemBuilder,
            BuildError,
        )
        from .ir_translator import IRTranslator, TranslationError
        from .ctx import SolveCtx, CompileIncompleteError, CompileUnsatError, SOLVE_OK, SOLVE_UNSAT

        cls = type(obj)

        try:
            # ---- Build / retrieve cached (system, sp, var_id_map) --------
            cache_entry = _CLASS_CACHE.get(cls)
            if cache_entry is None:
                struct_type = _extract_struct_type(obj)
                builder = ConstraintSystemBuilder()
                try:
                    system = builder.build_from_struct(struct_type)
                except BuildError as exc:
                    raise RandomizationError(
                        f"Failed to build constraint system: {exc}"
                    ) from exc

                translator = IRTranslator()
                try:
                    builder, var_id_map = translator.translate(system)
                    problem_buf, _buf_size = builder.finalize()
                except TranslationError as exc:
                    # Translation failed -- fall back to Python, but don't cache
                    # so that each call re-tries (in case the error is transient).
                    warnings.warn(
                        f"Native solver: translation failed ({exc}); falling back to Python",
                        stacklevel=3,
                    )
                    result = _solve_constraint_system(system, seed, timeout_ms)
                    if not result.success:
                        raise RandomizationError(f"No solution found: {result.error}")
                    _apply_solution(obj, result.assignment, system)
                    return

                # Test-compile once to detect unsupported constraints before caching.
                try:
                    _probe = SolveCtx(problem_buf)
                    _probe.destroy()
                    cache_entry = (system, problem_buf, var_id_map, True)  # True = fully native
                except CompileUnsatError:
                    raise RandomizationError(
                        "No solution found: constraints are unsatisfiable"
                    )
                except CompileIncompleteError as exc:
                    warnings.warn(
                        f"Native solver: {exc}; falling back to Python",
                        stacklevel=3,
                    )
                    cache_entry = (system, problem_buf, var_id_map, False)  # False = needs Python

                _CLASS_CACHE[cls] = cache_entry

            system, problem_buf, var_id_map, fully_native = cache_entry

            # ---- Python fallback path (cached) ---------------------------
            if not fully_native:
                result = _solve_constraint_system(system, seed, timeout_ms)
                if not result.success:
                    raise RandomizationError(f"No solution found: {result.error}")
                _apply_solution(obj, result.assignment, system)
                return

            # ---- Native solve path (hot path) ----------------------------
            try:
                ctx_mgr = SolveCtx(problem_buf)
            except CompileUnsatError:
                raise RandomizationError(
                    "No solution found: constraints are unsatisfiable"
                )

            with ctx_mgr as ctx:
                actual_seed = seed if seed is not None else 0
                rc = ctx.solve(seed=actual_seed)
                if rc == SOLVE_UNSAT:
                    raise RandomizationError(
                        "No solution found: constraints are unsatisfiable"
                    )
                if rc != SOLVE_OK:
                    raise RandomizationError(
                        f"Solver returned unexpected result code {rc}"
                    )
                assignment = {
                    name: ctx.get_value(vid)
                    for name, vid in var_id_map.items()
                }

            _apply_solution(obj, assignment, system)

        except RandomizationError:
            raise
        except Exception as exc:
            raise RandomizationError(f"Randomization failed: {exc}") from exc

    def randomize_with(
        self,
        obj: Any,
        with_block: Any,
        seed: Optional[int] = None,
        timeout_ms: Optional[int] = 1000,
    ) -> None:
        """Randomize *obj* with extra inline constraints.

        Delegates to the Python solver since inline constraints use source-level
        parsing that is already handled by RandomizeWithContext.
        """
        raise NotImplementedError(
            "NativeSolverBackend.randomize_with() is not yet implemented."
        )
