"""Native solver back-end.

Implements the ``SolverBackend`` protocol from
``zuspec.dataclasses.solver.backend.base`` using the C native solver.
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, Optional, Tuple

# Hot-path imports: fetched once at module load, not inside the function.
# Using lazy-import pattern so circular imports are avoided (these modules
# import from each other), but each symbol is looked up only once.
_core_solve = None
_python_backend_mod = None


def _ensure_imports():
    global _core_solve, _python_backend_mod
    if _core_solve is None:
        import zuspec.dataclasses.solver._core_solve as _cs
        import zuspec.dataclasses.solver.backend.python_backend as _pb
        _core_solve = _cs
        _python_backend_mod = _pb


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
        _ensure_imports()
        cs = _core_solve

        cls = type(obj)

        # ---- Absolute hot path: bound class with cached assignment -----------
        # This branch is taken on all but the very first call for a given
        # (class, bound-values) combination.  No imports, no field scan, just
        # a set membership test + struct-type attribute lookup + cache lookup.
        if cls in cs._BOUND_CLASSES:
            struct_type = cs._extract_struct_type(obj)
            cs.randomize_bound_cached(obj, struct_type, seed, timeout_ms)
            return

        try:
            from zuspec.dataclasses.solver.frontend.constraint_system_builder import (
                ConstraintSystemBuilder,
                BuildError,
            )
            from .ir_translator import IRTranslator, TranslationError
            from .ctx import SolveCtx, CompileIncompleteError, CompileUnsatError, SOLVE_OK, SOLVE_UNSAT

            struct_type = cs._extract_struct_type(obj)

            if cls not in cs._NONBOUND_CLASSES:
                has_bound = _python_backend_mod._has_bound_object_fields(obj, struct_type)
                if has_bound:
                    cs._BOUND_CLASSES.add(cls)
                    cs.randomize_bound_cached(obj, struct_type, seed, timeout_ms)
                    return
                cs._NONBOUND_CLASSES.add(cls)

            # ---- Build / retrieve cached (system, sp, var_id_map) --------
            cache_entry = _CLASS_CACHE.get(cls)
            if cache_entry is None:
                builder = ConstraintSystemBuilder()
                try:
                    system = builder.build_from_struct(struct_type)
                except BuildError as exc:
                    raise cs.RandomizationError(
                        f"Failed to build constraint system: {exc}"
                    ) from exc

                translator = IRTranslator()
                try:
                    native_builder, var_id_map = translator.translate(system)
                    problem_buf, _buf_size = native_builder.finalize()
                except TranslationError as exc:
                    warnings.warn(
                        f"Native solver: translation failed ({exc}); falling back to Python",
                        stacklevel=3,
                    )
                    result = cs._solve_constraint_system(system, seed, timeout_ms)
                    if not result.success:
                        raise cs.RandomizationError(f"No solution found: {result.error}")
                    cs._apply_solution(obj, result.assignment, system)
                    return

                # Test-compile once to detect unsupported constraints before caching.
                try:
                    _probe = SolveCtx(problem_buf)
                    _probe.destroy()
                    cache_entry = (system, problem_buf, var_id_map, True)
                except CompileUnsatError:
                    raise cs.RandomizationError(
                        "No solution found: constraints are unsatisfiable"
                    )
                except CompileIncompleteError as exc:
                    warnings.warn(
                        f"Native solver: {exc}; falling back to Python",
                        stacklevel=3,
                    )
                    cache_entry = (system, problem_buf, var_id_map, False)

                _CLASS_CACHE[cls] = cache_entry

            system, problem_buf, var_id_map, fully_native = cache_entry

            # ---- Python fallback path (cached) ---------------------------
            if not fully_native:
                result = cs._solve_constraint_system(system, seed, timeout_ms)
                if not result.success:
                    raise cs.RandomizationError(f"No solution found: {result.error}")
                cs._apply_solution(obj, result.assignment, system)
                return


            # ---- Native solve path (hot path) ----------------------------
            try:
                ctx_mgr = SolveCtx(problem_buf)
            except CompileUnsatError:
                raise cs.RandomizationError(
                    "No solution found: constraints are unsatisfiable"
                )

            with ctx_mgr as ctx:
                import random as _random
                actual_seed = seed if seed is not None else _random.randint(0, 2**31 - 1)
                rc = ctx.solve(seed=actual_seed)
                if rc == SOLVE_UNSAT:
                    raise cs.RandomizationError(
                        "No solution found: constraints are unsatisfiable"
                    )
                if rc != SOLVE_OK:
                    raise cs.RandomizationError(
                        f"Solver returned unexpected result code {rc}"
                    )
                assignment = {
                    name: ctx.get_value(vid)
                    for name, vid in var_id_map.items()
                }

            cs._apply_solution(obj, assignment, system)

        except cs.RandomizationError:
            raise
        except Exception as exc:
            raise cs.RandomizationError(f"Randomization failed: {exc}") from exc

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
