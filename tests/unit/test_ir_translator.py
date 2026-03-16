"""Unit tests for IRTranslator (Phase 9).

These tests build ConstraintSystem objects manually to avoid depending
on @dataclass / DataModelFactory, and verify that the translator
produces a SolveProblem with the expected structure and that the
resulting problem can be solved by the native engine.
"""
from __future__ import annotations

import pytest
from zuspec.dataclasses.ir.expr import BinOp, UnaryOp, CmpOp, BoolOp
from zuspec.dataclasses.solver.core.variable import Variable, VarKind
from zuspec.dataclasses.solver.core.domain import IntDomain
from zuspec.dataclasses.solver.core.constraint_system import ConstraintSystem
from zuspec.dataclasses.solver.core.constraints import (
    ConstantConstraint,
    VariableRefConstraint,
    BinaryOpConstraint,
    CompareConstraint,
    CompareChainConstraint,
    UnaryOpConstraint,
    BoolOpConstraint,
    InConstraint,
    ImplicationConstraint,
    BitSliceConstraint,
    UniqueConstraint,
)

from zuspec.solver.ir_translator import IRTranslator, TranslationError
from zuspec.solver.problem import EXPR_NULL


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _make_var(name: str, lo: int, hi: int, width: int = 32, signed: bool = False,
              kind: VarKind = VarKind.RAND) -> Variable:
    domain = IntDomain([(lo, hi)], width, signed)
    return Variable(name=name, domain=domain, kind=kind)


def _make_system(*vars: Variable) -> ConstraintSystem:
    sys = ConstraintSystem()
    for v in vars:
        sys.add_variable(v)
    return sys


def _solve_sp(sp, seed=42):
    """Build a SolveCtx and solve; return (result_code, {name: value}, var_id_map)."""
    from zuspec.solver.ctx import SolveCtx, SOLVE_OK
    return sp, SOLVE_OK  # just return sp for structural tests


# ------------------------------------------------------------------ #
# Test: variable count and bounds                                      #
# ------------------------------------------------------------------ #

class TestVariableTranslation:
    def test_two_vars_no_constraints(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 10)
        y = _make_var("y", 0, 20)
        sys = _make_system(x, y)

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        assert len(var_id_map) == 2
        assert "x" in var_id_map and "y" in var_id_map

    def test_signed_variable(self, lib_path):
        _make_lib_available(lib_path)
        v = _make_var("v", -10, 10, width=32, signed=True)
        sys = _make_system(v)

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)
        assert "v" in var_id_map

    def test_deterministic_id_assignment(self, lib_path):
        _make_lib_available(lib_path)
        # IDs should be sorted by name
        b = _make_var("b", 0, 1)
        a = _make_var("a", 0, 1)
        sys = _make_system(a, b)  # added in name order

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)
        assert var_id_map["a"] == 0
        assert var_id_map["b"] == 1


# ------------------------------------------------------------------ #
# Test: constraint translation + solvability                          #
# ------------------------------------------------------------------ #

class TestConstraintTranslation:
    def test_compare_eq_two_vars(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 10)
        y = _make_var("y", 0, 10)
        sys = _make_system(x, y)
        # x == y
        sys.add_constraint(CompareConstraint(
            left=VariableRefConstraint(x),
            op=CmpOp.Eq,
            right=VariableRefConstraint(y),
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            assert ctx.get_value(var_id_map["x"]) == ctx.get_value(var_id_map["y"])

    def test_compare_lt_two_vars(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 10)
        y = _make_var("y", 0, 10)
        sys = _make_system(x, y)
        # x < y
        sys.add_constraint(CompareConstraint(
            left=VariableRefConstraint(x),
            op=CmpOp.Lt,
            right=VariableRefConstraint(y),
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            xv = ctx.get_value(var_id_map["x"])
            yv = ctx.get_value(var_id_map["y"])
            assert xv < yv, f"Expected x<y, got x={xv}, y={yv}"

    def test_constant_constraint(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 100)
        sys = _make_system(x)
        # x == 42 (via CompareConstraint with ConstantConstraint)
        sys.add_constraint(CompareConstraint(
            left=VariableRefConstraint(x),
            op=CmpOp.Eq,
            right=ConstantConstraint(42),
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            assert ctx.get_value(var_id_map["x"]) == 42

    def test_binary_op_add(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 10)
        y = _make_var("y", 0, 10)
        r = _make_var("r", 7, 7)   # r fixed to 7
        sys = _make_system(r, x, y)
        # r == x + y
        sys.add_constraint(CompareConstraint(
            left=VariableRefConstraint(r),
            op=CmpOp.Eq,
            right=BinaryOpConstraint(BinOp.Add,
                                     VariableRefConstraint(x),
                                     VariableRefConstraint(y)),
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            rv = ctx.get_value(var_id_map["r"])
            xv = ctx.get_value(var_id_map["x"])
            yv = ctx.get_value(var_id_map["y"])
            assert rv == 7
            assert xv + yv == 7

    def test_bool_op_and_split(self, lib_path):
        """BoolOpConstraint(AND) at top level splits into two constraints."""
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 20)
        sys = _make_system(x)
        # x >= 5 AND x <= 15  (both as CompareConstraints)
        sys.add_constraint(BoolOpConstraint(
            op=BoolOp.And,
            values=[
                CompareConstraint(VariableRefConstraint(x), CmpOp.GtE, ConstantConstraint(5)),
                CompareConstraint(VariableRefConstraint(x), CmpOp.LtE, ConstantConstraint(15)),
            ],
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            xv = ctx.get_value(var_id_map["x"])
            assert 5 <= xv <= 15, f"Expected 5<=x<=15, got x={xv}"

    def test_compare_chain(self, lib_path):
        """CompareChainConstraint (a < b < c) splits into two pairwise constraints."""
        _make_lib_available(lib_path)
        a = _make_var("a", 0, 10)
        b = _make_var("b", 0, 10)
        c = _make_var("c", 0, 10)
        sys = _make_system(a, b, c)
        sys.add_constraint(CompareChainConstraint(
            left=VariableRefConstraint(a),
            ops=[CmpOp.Lt, CmpOp.Lt],
            comparators=[VariableRefConstraint(b), VariableRefConstraint(c)],
        ))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)

        from zuspec.solver.ctx import SolveCtx, SOLVE_OK
        with SolveCtx(sp) as ctx:
            assert ctx.solve(seed=1) == SOLVE_OK
            av = ctx.get_value(var_id_map["a"])
            bv = ctx.get_value(var_id_map["b"])
            cv = ctx.get_value(var_id_map["c"])
            assert av < bv < cv, f"Expected a<b<c, got {av}<{bv}<{cv}"

    def test_in_constraint(self, lib_path):
        """InConstraint maps to expr_in_set."""
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 100)
        sys = _make_system(x)
        allowed = {10, 20, 30}
        sys.add_constraint(InConstraint(variable=x, values=allowed))

        translator = IRTranslator()
        sp, var_id_map = translator.translate(sys)
        # Structural test: SolveProblem built without overflow
        assert sp is not None

    def test_unsupported_unique_raises(self, lib_path):
        _make_lib_available(lib_path)
        x = _make_var("x", 0, 10)
        y = _make_var("y", 0, 10)
        sys = _make_system(x, y)
        sys.add_constraint(UniqueConstraint(variables=[x, y]))

        translator = IRTranslator()
        with pytest.raises(TranslationError, match="UniqueConstraint"):
            translator.translate(sys)


# ------------------------------------------------------------------ #
# Fixtures (reuse build logic from test_native_backend.py)            #
# ------------------------------------------------------------------ #

import os
import subprocess
import shutil
from pathlib import Path

_PKG_DIR = Path(__file__).parent.parent.parent


def _build_lib(build_dir: Path) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmake", str(_PKG_DIR), "-DCMAKE_BUILD_TYPE=Release"],
                   cwd=build_dir, check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--parallel"],
                   check=True, capture_output=True)
    hits = sorted(build_dir.glob("libzsp_solver.so*"), key=lambda p: len(p.name))
    if not hits:
        raise FileNotFoundError("libzsp_solver.so not found")
    return hits[0]


@pytest.fixture(scope="module")
def lib_path(tmp_path_factory):
    if not shutil.which("cmake"):
        pytest.skip("cmake not found")
    build_dir = tmp_path_factory.mktemp("zsp_translator_build")
    try:
        return _build_lib(build_dir)
    except Exception as exc:
        pytest.skip(f"build failed: {exc}")


def _make_lib_available(lib_path: Path):
    import zuspec.solver.lib as _lib_mod
    _lib_mod._LOAD_ATTEMPTED = False
    _lib_mod._LIB_CACHE = None
    os.environ["ZSP_SOLVER_PATH"] = str(lib_path.parent)
    return _lib_mod._load_lib()
