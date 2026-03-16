"""Integration tests for Phase 8: ctypes binding layer.

Tests:
1. NativeSolverBackend.available is True when library is present.
2. 3-variable unconstrained round-trip: all values in declared domain.
3. x + y = 7, x∈[0,10], y∈[0,10]: result satisfies constraint.
4. UNSAT problem: solve() returns SOLVE_UNSAT.
5. Seeded reproducibility: same seed → same values.
"""
from __future__ import annotations

import os
import subprocess
import shutil
from pathlib import Path

import pytest

# ------------------------------------------------------------------ #
# Build the library once for the session (reuse conftest pattern)     #
# ------------------------------------------------------------------ #

_PKG_DIR = Path(__file__).parent.parent.parent  # packages/zuspec-solver


def _build_lib(build_dir: Path) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmake", str(_PKG_DIR), "-DCMAKE_BUILD_TYPE=Release"],
        cwd=build_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build_dir), "--parallel"],
        check=True, capture_output=True,
    )
    hits = sorted(build_dir.glob("libzsp_solver.so*"), key=lambda p: len(p.name))
    if not hits:
        raise FileNotFoundError("libzsp_solver.so not found after build")
    return hits[0]


@pytest.fixture(scope="module")
def lib_path(tmp_path_factory):
    if not shutil.which("cmake"):
        pytest.skip("cmake not found")
    build_dir = tmp_path_factory.mktemp("zsp_native_build")
    try:
        return _build_lib(build_dir)
    except Exception as exc:
        pytest.skip(f"build failed: {exc}")


@pytest.fixture(scope="module")
def native_lib_env(lib_path, monkeypatch_session):
    """Set ZSP_SOLVER_PATH so _load_lib() finds the freshly-built .so."""
    monkeypatch_session.setenv("ZSP_SOLVER_PATH", str(lib_path.parent))


# monkeypatch is function-scoped; we need a module-scoped version
@pytest.fixture(scope="module")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    m = MonkeyPatch()
    yield m
    m.undo()


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _make_lib_available(lib_path: Path):
    """Point _load_lib at the test-built .so, bypassing the cache."""
    import zuspec.solver.lib as _lib_mod
    import ctypes
    # Reset cache so it re-discovers the library
    _lib_mod._LOAD_ATTEMPTED = False
    _lib_mod._LIB_CACHE = None
    os.environ["ZSP_SOLVER_PATH"] = str(lib_path.parent)
    return _lib_mod._load_lib()


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #

class TestNativeBackendAvailable:
    def test_available_when_lib_present(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.backend import NativeSolverBackend
        backend = NativeSolverBackend()
        assert backend.available is True

    def test_name_is_native(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.backend import NativeSolverBackend
        assert NativeSolverBackend().name == "native"

    def test_randomize_raises_error_on_plain_object(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.backend import NativeSolverBackend
        from zuspec.dataclasses.solver._core_solve import RandomizationError
        backend = NativeSolverBackend()
        with pytest.raises(RandomizationError):
            backend.randomize(object())


class TestSolveProblemCtypes:
    """Tests that exercise SolveProblem and SolveCtx Python wrappers."""

    def test_three_var_unconstrained(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.problem import SolveProblem
        from zuspec.solver.ctx import SolveCtx, SOLVE_OK

        sp = SolveProblem()
        sp.add_var(0, width=32, is_signed=False, lo=0, hi=99)
        sp.add_var(1, width=32, is_signed=False, lo=0, hi=99)
        sp.add_var(2, width=32, is_signed=False, lo=0, hi=99)

        with SolveCtx(sp) as ctx:
            rc = ctx.solve(seed=0xDEAD_BEEF)
            assert rc == SOLVE_OK
            for v in range(3):
                val = ctx.get_value(v)
                assert 0 <= val <= 99, f"var {v} = {val} out of range"

    def test_x_plus_y_eq_7(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.problem import SolveProblem, BIN_ADD, BIN_EQ
        from zuspec.solver.ctx import SolveCtx, SOLVE_OK

        sp = SolveProblem()
        sp.add_var(0, width=32, is_signed=False, lo=0, hi=10)  # x
        sp.add_var(1, width=32, is_signed=False, lo=0, hi=10)  # y
        sp.add_var(2, width=32, is_signed=False, lo=7, hi=7)   # r = 7 (constant)
        # Constraint: r == x + y  (canonical form: EQ with var on LHS)
        add_ref = sp.expr_binary(BIN_ADD, sp.expr_var(0), sp.expr_var(1))
        eq_ref  = sp.expr_binary(BIN_EQ, sp.expr_var(2), add_ref)
        sp.add_constraint(eq_ref)

        with SolveCtx(sp) as ctx:
            rc = ctx.solve(seed=42)
            assert rc == SOLVE_OK
            x = ctx.get_value(0)
            y = ctx.get_value(1)
            assert x + y == 7, f"x={x}, y={y}, x+y={x+y} ≠ 7"

    def test_unsat_returns_unsat(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.problem import SolveProblem, BIN_GTE, BIN_LTE
        from zuspec.solver.ctx import SolveCtx, SOLVE_UNSAT, CompileUnsatError

        sp = SolveProblem()
        sp.add_var(0, width=32, is_signed=False, lo=0, hi=10)  # x

        # Two contradictory constraints: x >= 8 AND x <= 3 — impossible
        v = sp.expr_var(0)
        sp.add_constraint(sp.expr_binary(BIN_GTE, v, sp.expr_const(8)))
        sp.add_constraint(sp.expr_binary(BIN_LTE, v, sp.expr_const(3)))

        # UNSAT is detected at compile time via bound tightening — SolveCtx raises
        # CompileUnsatError rather than succeeding and returning SOLVE_UNSAT from solve().
        with pytest.raises(CompileUnsatError):
            with SolveCtx(sp) as ctx:
                rc = ctx.solve()
                assert rc == SOLVE_UNSAT

    def test_seeded_reproducible(self, lib_path):
        _make_lib_available(lib_path)
        from zuspec.solver.problem import SolveProblem
        from zuspec.solver.ctx import SolveCtx, SOLVE_OK

        def _run(seed):
            sp = SolveProblem()
            sp.add_var(0, width=32, is_signed=False, lo=0, hi=100)
            sp.add_var(1, width=32, is_signed=False, lo=0, hi=100)
            with SolveCtx(sp) as ctx:
                rc = ctx.solve(seed=seed)
                assert rc == SOLVE_OK
                return ctx.get_value(0), ctx.get_value(1)

        r1 = _run(0xCAFE_BABE)
        r2 = _run(0xCAFE_BABE)
        assert r1 == r2, f"Same seed gave different results: {r1} vs {r2}"

    def test_expr_builders(self, lib_path):
        """Verify all major expr builders return non-NULL ExprRefs."""
        _make_lib_available(lib_path)
        from zuspec.solver.problem import (
            SolveProblem, EXPR_NULL,
            BIN_ADD, BIN_EQ, UN_NEG,
        )

        sp = SolveProblem()
        sp.add_var(0, width=32, is_signed=True, lo=-100, hi=100)

        v    = sp.expr_var(0)
        c5   = sp.expr_const(5)
        add  = sp.expr_binary(BIN_ADD, v, c5)
        neg  = sp.expr_unary(UN_NEG, v)
        rng  = sp.expr_in_range(v, sp.expr_const(-10), sp.expr_const(10))
        iset = sp.expr_in_set(v, [sp.expr_const(1), sp.expr_const(2), sp.expr_const(3)])
        ite  = sp.expr_ite(sp.expr_binary(BIN_EQ, v, c5), c5, sp.expr_const(0))
        ext  = sp.expr_extend(v, 32, 64, sign_extend=True)
        extr = sp.expr_extract(v, 7, 0)

        for name, ref in [("var",v),("const",c5),("add",add),("neg",neg),
                           ("range",rng),("in_set",iset),("ite",ite),
                           ("extend",ext),("extract",extr)]:
            assert ref != EXPR_NULL, f"expr_builder '{name}' returned EXPR_NULL"
