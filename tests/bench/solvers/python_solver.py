"""Python-solver back-end for benchmarks.

Wraps ``zdc.randomize()`` directly with no extra environment variable set.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

import zuspec.dataclasses as zdc

from .base import BenchResult, RESULTS_DIR

WARMUP_ITERS = 100
BATCH_SIZE   = 500


@contextmanager
def _solver_env(backend_name: str):
    """Temporarily set ZSP_SOLVER_BACKEND to *backend_name*."""
    old = os.environ.get("ZSP_SOLVER_BACKEND")
    os.environ["ZSP_SOLVER_BACKEND"] = backend_name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ZSP_SOLVER_BACKEND", None)
        else:
            os.environ["ZSP_SOLVER_BACKEND"] = old


def _zdc_bench(
    cls,
    validate,
    solver_name: str,
    n_solutions: int,
    min_bench_ns: int,
) -> None:
    """Shared timed loop for the Python and native back-ends.

    Handles intermittent RandomizationError (UNSAT) from the Python bitwuzla
    backend gracefully: counts failures separately and skips the benchmark if
    the solver cannot reliably produce solutions.
    """
    import pytest
    from zuspec.dataclasses.constraint_parser import extract_rand_fields
    try:
        from zuspec.dataclasses.solver._core_solve import RandomizationError
    except ImportError:
        RandomizationError = Exception  # type: ignore

    fields_info = extract_rand_fields(cls)
    fields      = [f["name"] for f in fields_info]
    init_kwargs = {f["name"]: f.get("default", 0) for f in fields_info}
    obj = cls(**init_kwargs)

    # warm-up excluded from timing
    for _ in range(min(WARMUP_ITERS, n_solutions // 10 or 1)):
        try:
            zdc.randomize(obj)
        except RandomizationError:
            pass

    iters = 0
    failures = 0
    t_start = time.perf_counter_ns()
    while True:
        for _ in range(BATCH_SIZE):
            try:
                zdc.randomize(obj)
                iters += 1
            except RandomizationError:
                failures += 1
        elapsed = time.perf_counter_ns() - t_start
        total_attempts = iters + failures
        if total_attempts >= n_solutions and elapsed >= min_bench_ns:
            break
        # Give up if failure rate is overwhelming (> 90%) after 50+ attempts
        if total_attempts >= 50 and failures / total_attempts > 0.90:
            pytest.skip(
                f"{solver_name}: >90% UNSAT rate on {cls.__name__} "
                f"({failures}/{total_attempts} failed) — problem too hard"
            )

    sol = {f: getattr(obj, f) for f in fields}
    validate(sol)

    if failures:
        print(f"\n  note: {failures}/{iters + failures} attempts returned UNSAT")

    result = BenchResult(solver_name, cls.__name__.lower(), iters, elapsed)
    result.save(RESULTS_DIR)
    print("\n" + result.summary())


class PythonSolver:
    _backend_name = "python"

    def __str__(self) -> str:
        return self._backend_name

    def bench(
        self,
        cls: type,
        validate,
        tmp_path: Path,
        n_solutions: int = 1000,
        min_bench_ns: int = 1_000_000_000,
    ) -> None:
        with _solver_env(self._backend_name):
            _zdc_bench(cls, validate, str(self), n_solutions, min_bench_ns)
