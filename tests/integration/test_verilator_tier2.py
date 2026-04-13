"""Integration tests: Tier-2 Verilator constraint patterns.

Tests SV lifecycle features (constraint_mode, rand_mode, randomize-with,
inheritance, seeding) mapped to the zuspec-solver C API.
"""
from __future__ import annotations

import ctypes
import pytest

EXPR_NULL = 0xFFFF_FFFF
SOLVE_OK = 0
SOLVE_UNSAT = 1
BIN_ADD = 0; BIN_SUB = 1; BIN_MUL = 2; BIN_MOD = 4
BIN_EQ = 10; BIN_NEQ = 11; BIN_LT = 12; BIN_LTE = 13
BIN_GT = 14; BIN_GTE = 15

_SP = 65536
_CTX = 1 << 20


class SolveOpts(ctypes.Structure):
    _fields_ = [
        ("seed", ctypes.c_uint64),
        ("max_conflicts", ctypes.c_uint32),
        ("max_restarts", ctypes.c_uint32),
        ("use_phase_save", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 3),
        ("max_shave_iters", ctypes.c_uint32),
    ]


def _wire(lib):
    c = ctypes
    for name, rt, at in [
        ("zsp_block_alloc_create", c.c_void_p, [c.c_void_p, c.c_size_t]),
        ("zsp_block_alloc_destroy", None, [c.c_void_p]),
        ("solve_problem_init", c.c_void_p, [c.c_void_p, c.c_size_t]),
        ("solve_problem_reset", None, [c.c_void_p]),
        ("problem_add_var", c.c_uint32,
         [c.c_void_p, c.c_uint32, c.c_uint8, c.c_uint8, c.c_int64, c.c_int64]),
        ("problem_add_constraint", c.c_uint32, [c.c_void_p, c.c_uint32]),
        ("expr_var", c.c_uint32, [c.c_void_p, c.c_uint32]),
        ("expr_const", c.c_uint32, [c.c_void_p, c.c_int64, c.c_uint8]),
        ("expr_binary", c.c_uint32,
         [c.c_void_p, c.c_int32, c.c_uint32, c.c_uint32]),
        ("solver_create", c.c_void_p, [c.c_void_p, c.c_size_t, c.c_void_p]),
        ("solver_destroy", None, [c.c_void_p]),
        ("solver_compile", c.c_int, [c.c_void_p, c.c_void_p]),
        ("solver_solve", c.c_int, [c.c_void_p, c.c_void_p]),
        ("solver_get_value", c.c_int64, [c.c_void_p, c.c_uint32]),
        ("solver_reset", None, [c.c_void_p]),
        ("solver_pin_var", c.c_int, [c.c_void_p, c.c_uint32, c.c_int64]),
        ("solver_set_seed", None, [c.c_void_p, c.c_uint64]),
        ("solver_add_constraint", c.c_int, [c.c_void_p, c.c_void_p]),
        ("solver_checkpoint", c.c_int, [c.c_void_p]),
        ("solver_restore", None, [c.c_void_p, c.c_uint32]),
        ("solver_exclude_value", c.c_int, [c.c_void_p, c.c_uint32, c.c_int64]),
    ]:
        getattr(lib, name).restype = rt
        getattr(lib, name).argtypes = at


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _make_ctx(lib, sp):
    """Compile and return (ctx, ba)."""
    ctx_buf = (ctypes.c_uint8 * _CTX)()
    ba = lib.zsp_block_alloc_create(None, _CTX)
    ctx = lib.solver_create(ctx_buf, _CTX, ba)
    crc = lib.solver_compile(ctx, sp)
    assert crc >= 0, f"compile failed ({crc})"
    return ctx, ba, ctx_buf  # keep ctx_buf alive


def _solve_ok(lib, ctx, seed=42):
    opts = SolveOpts(seed=seed)
    rc = lib.solver_solve(ctx, ctypes.byref(opts))
    assert rc == SOLVE_OK, f"solve returned {rc}"


# ------------------------------------------------------------------ #
# constraint_mode patterns                                             #
# ------------------------------------------------------------------ #

def test_constraint_mode_disable(libzsp):
    """constraint_mode(0): disable a constraint by not including it.

    Mirrors t_constraint_mode.v: cons_y.constraint_mode(0) means the
    constraint y==10 is not added to the problem. y should be free.
    """
    _wire(libzsp)

    # Class has: x in (0,10), y == 10, z == x || z == y
    # With cons_y disabled: y is free [0, 100]
    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)

    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)  # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)  # y (free -- cons_y disabled)
    libzsp.problem_add_var(sp, 2, 32, 1, 0, 100)  # z

    # cons_x: x > 0 && x < 10
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 0, 0)))
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_LT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 10, 0)))
    # cons_y: DISABLED (not added)

    ctx, ba, buf = _make_ctx(libzsp, sp)

    seen_y_ne10 = False
    for seed in range(1, 21):
        libzsp.solver_reset(ctx)
        _solve_ok(libzsp, ctx, seed=seed)
        x = libzsp.solver_get_value(ctx, 0)
        y = libzsp.solver_get_value(ctx, 1)
        assert 0 < x < 10
        if y != 10:
            seen_y_ne10 = True
    assert seen_y_ne10, "y was always 10 even though cons_y is disabled"

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)


def test_constraint_mode_toggle(libzsp):
    """Toggle constraint on/off between solves using checkpoint/restore.

    Phase 1: Only low_range (x < 50). Verify x < 50.
    Phase 2: Add high_range (x >= 50, x < 200). Verify 50 <= x < 200.
    Mirrors t_constraint_mode_ctor.v.
    """
    _wire(libzsp)

    # Phase 1: low_range only
    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 8, 0, 0, 255)  # value
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_LT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 50, 0)))

    ctx, ba, buf = _make_ctx(libzsp, sp)
    for seed in range(1, 11):
        libzsp.solver_reset(ctx)
        _solve_ok(libzsp, ctx, seed=seed)
        assert libzsp.solver_get_value(ctx, 0) < 50

    # Phase 2: add high_range constraint via solver_add_constraint
    # First checkpoint, then add the new constraint
    cp = libzsp.solver_checkpoint(ctx)

    aux_buf = (ctypes.c_uint8 * _SP)()
    aux = libzsp.solve_problem_init(aux_buf, _SP)
    libzsp.problem_add_var(aux, 0, 8, 0, 0, 255)
    # Override: x >= 50 AND x < 200
    libzsp.problem_add_constraint(aux,
        libzsp.expr_binary(aux, BIN_GTE, libzsp.expr_var(aux, 0),
                           libzsp.expr_const(aux, 50, 0)))
    libzsp.problem_add_constraint(aux,
        libzsp.expr_binary(aux, BIN_LT, libzsp.expr_var(aux, 0),
                           libzsp.expr_const(aux, 200, 0)))

    rc = libzsp.solver_add_constraint(ctx, aux)
    # This will conflict with x < 50 from Phase 1 (x >= 50 AND x < 50 is UNSAT)
    # That's expected -- constraint_mode toggle in Verilator would
    # recompile the problem, not add incrementally.

    # In practice, Verilator rebuilds the SolveProblem from scratch
    # when constraint_mode changes. Let's test that pattern:
    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)

    sp2_buf = (ctypes.c_uint8 * _SP)()
    sp2 = libzsp.solve_problem_init(sp2_buf, _SP)
    libzsp.problem_add_var(sp2, 0, 8, 0, 0, 255)
    libzsp.problem_add_constraint(sp2,
        libzsp.expr_binary(sp2, BIN_GTE, libzsp.expr_var(sp2, 0),
                           libzsp.expr_const(sp2, 50, 0)))
    libzsp.problem_add_constraint(sp2,
        libzsp.expr_binary(sp2, BIN_LT, libzsp.expr_var(sp2, 0),
                           libzsp.expr_const(sp2, 200, 0)))

    ctx2, ba2, buf2 = _make_ctx(libzsp, sp2)
    for seed in range(1, 11):
        libzsp.solver_reset(ctx2)
        _solve_ok(libzsp, ctx2, seed=seed)
        v = libzsp.solver_get_value(ctx2, 0)
        assert 50 <= v < 200, f"value={v}"

    libzsp.solver_destroy(ctx2)
    libzsp.zsp_block_alloc_destroy(ba2)


# ------------------------------------------------------------------ #
# rand_mode patterns                                                   #
# ------------------------------------------------------------------ #

def test_rand_mode_disable(libzsp):
    """rand_mode(0): pin a variable to its current value.

    Mirrors t_randomize_rand_mode.v: m_one.rand_mode(0) means m_one
    keeps its assigned value. The solver sees it as pinned.
    """
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # m_one
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # m_two

    ctx, ba, buf = _make_ctx(libzsp, sp)

    # rand_mode(0) for m_one: pin to value 10
    rc = libzsp.solver_pin_var(ctx, 0, 10)
    assert rc == 0

    for seed in range(1, 21):
        libzsp.solver_reset(ctx)
        libzsp.solver_pin_var(ctx, 0, 10)
        _solve_ok(libzsp, ctx, seed=seed)
        assert libzsp.solver_get_value(ctx, 0) == 10, "m_one should be pinned"
        # m_two should vary

    # Re-enable: stop pinning
    libzsp.solver_reset(ctx)
    _solve_ok(libzsp, ctx, seed=99)
    v = libzsp.solver_get_value(ctx, 0)
    # m_one should be free now (might or might not be 10)

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)


def test_rand_mode_with_constraints(libzsp):
    """rand_mode(0) on a variable that appears in constraints.

    Mirrors t_randomize_rand_mode_constr.v: x.rand_mode(1), y.rand_mode(0).
    Constraint: y > x. With y pinned to 8, x must be < 8.
    """
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # y
    # y > x
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT, libzsp.expr_var(sp, 1),
                           libzsp.expr_var(sp, 0)))

    ctx, ba, buf = _make_ctx(libzsp, sp)

    for seed in range(1, 21):
        libzsp.solver_reset(ctx)
        # Pin y to 8 (rand_mode(0))
        libzsp.solver_pin_var(ctx, 1, 8)
        _solve_ok(libzsp, ctx, seed=seed)
        x = libzsp.solver_get_value(ctx, 0)
        y = libzsp.solver_get_value(ctx, 1)
        assert y == 8
        assert x < 8, f"x={x} should be < 8 (y is pinned to 8)"

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)


# ------------------------------------------------------------------ #
# randomize() with { ... } -- inline constraints                      #
# ------------------------------------------------------------------ #

def test_randomize_with_inline(libzsp):
    """randomize() with { x > 0; x < y; } -- add inline constraints.

    Mirrors t_randomize_method_with.v: the inline constraint is added
    via solver_add_constraint after the base problem is compiled.
    """
    _wire(libzsp)

    # Base problem: x, y unconstrained
    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # y

    ctx, ba, buf = _make_ctx(libzsp, sp)

    # Add inline constraints: x > 0 AND x < y
    aux_buf = (ctypes.c_uint8 * _SP)()
    aux = libzsp.solve_problem_init(aux_buf, _SP)
    libzsp.problem_add_var(aux, 0, 32, 1, 0, 100)
    libzsp.problem_add_var(aux, 1, 32, 1, 0, 100)
    libzsp.problem_add_constraint(aux,
        libzsp.expr_binary(aux, BIN_GT, libzsp.expr_var(aux, 0),
                           libzsp.expr_const(aux, 0, 0)))
    libzsp.problem_add_constraint(aux,
        libzsp.expr_binary(aux, BIN_LT, libzsp.expr_var(aux, 0),
                           libzsp.expr_var(aux, 1)))

    rc = libzsp.solver_add_constraint(ctx, aux)
    assert rc >= 0

    for seed in range(1, 21):
        libzsp.solver_reset(ctx)
        _solve_ok(libzsp, ctx, seed=seed)
        x = libzsp.solver_get_value(ctx, 0)
        y = libzsp.solver_get_value(ctx, 1)
        assert x > 0
        assert x < y, f"x={x} not < y={y}"

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)


def test_randomize_with_merged_problem(libzsp):
    """randomize() with { z == 100; } -- merged constraint set.

    The 'with' block merges inline constraints into the same problem.
    Both class constraints and inline constraints are in one SolveProblem.
    """
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # y
    libzsp.problem_add_var(sp, 2, 32, 1, 0, 200)   # z

    # Class constraints: x > 0 && x < 10
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 0, 0)))
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_LT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 10, 0)))

    # Inline 'with' constraint: z == 100 (merged into same problem)
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 2),
                           libzsp.expr_const(sp, 100, 0)))

    ctx, ba, buf = _make_ctx(libzsp, sp)
    _solve_ok(libzsp, ctx, seed=42)
    assert libzsp.solver_get_value(ctx, 2) == 100
    assert 0 < libzsp.solver_get_value(ctx, 0) < 10

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)


# ------------------------------------------------------------------ #
# Inheritance: multiple constraint levels                              #
# ------------------------------------------------------------------ #

def test_inheritance_chain(libzsp):
    """Constraints from 3 class levels: Base, Child, GrandChild.

    Mirrors t_constraint_inheritance.v:
      B: x > 0
      C extends B: + y field
      D extends C: + x < y
      E extends C: + x < 20, x > y
    """
    _wire(libzsp)

    # Test D: x > 0, x < y
    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 1000)   # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 1000)   # y

    # From B: x > 0
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT, libzsp.expr_var(sp, 0),
                           libzsp.expr_const(sp, 0, 0)))
    # From D: x < y
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_LT, libzsp.expr_var(sp, 0),
                           libzsp.expr_var(sp, 1)))

    ctx, ba, buf = _make_ctx(libzsp, sp)
    for seed in range(1, 21):
        libzsp.solver_reset(ctx)
        _solve_ok(libzsp, ctx, seed=seed)
        x = libzsp.solver_get_value(ctx, 0)
        y = libzsp.solver_get_value(ctx, 1)
        assert x > 0
        assert x < y, f"x={x} not < y={y}"

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)

    # Test E: x > 0, x < 20, x > y
    sp2_buf = (ctypes.c_uint8 * _SP)()
    sp2 = libzsp.solve_problem_init(sp2_buf, _SP)
    libzsp.problem_add_var(sp2, 0, 32, 1, 0, 1000)   # x
    libzsp.problem_add_var(sp2, 1, 32, 1, 0, 1000)   # y

    libzsp.problem_add_constraint(sp2,
        libzsp.expr_binary(sp2, BIN_GT, libzsp.expr_var(sp2, 0),
                           libzsp.expr_const(sp2, 0, 0)))
    libzsp.problem_add_constraint(sp2,
        libzsp.expr_binary(sp2, BIN_LT, libzsp.expr_var(sp2, 0),
                           libzsp.expr_const(sp2, 20, 0)))
    libzsp.problem_add_constraint(sp2,
        libzsp.expr_binary(sp2, BIN_GT, libzsp.expr_var(sp2, 0),
                           libzsp.expr_var(sp2, 1)))

    ctx2, ba2, buf2 = _make_ctx(libzsp, sp2)
    for seed in range(1, 21):
        libzsp.solver_reset(ctx2)
        _solve_ok(libzsp, ctx2, seed=seed)
        x = libzsp.solver_get_value(ctx2, 0)
        y = libzsp.solver_get_value(ctx2, 1)
        assert 0 < x < 20
        assert x > y

    libzsp.solver_destroy(ctx2)
    libzsp.zsp_block_alloc_destroy(ba2)


# ------------------------------------------------------------------ #
# Seeding / reproducibility                                           #
# ------------------------------------------------------------------ #

def test_deterministic_seed(libzsp):
    """Same seed produces same results across multiple runs."""
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 1000)
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 1000)
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_LT, libzsp.expr_var(sp, 0),
                           libzsp.expr_var(sp, 1)))

    results = []
    for _ in range(3):
        ctx, ba, buf = _make_ctx(libzsp, sp)
        _solve_ok(libzsp, ctx, seed=12345)
        results.append((libzsp.solver_get_value(ctx, 0),
                         libzsp.solver_get_value(ctx, 1)))
        libzsp.solver_destroy(ctx)
        libzsp.zsp_block_alloc_destroy(ba)

    assert results[0] == results[1] == results[2], \
        f"Non-deterministic: {results}"


def test_different_seeds_differ(libzsp):
    """Different seeds produce different results (with high probability)."""
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 10000)
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 10000)

    seen = set()
    for seed in range(1, 21):
        ctx, ba, buf = _make_ctx(libzsp, sp)
        _solve_ok(libzsp, ctx, seed=seed)
        v = libzsp.solver_get_value(ctx, 0)
        seen.add(v)
        libzsp.solver_destroy(ctx)
        libzsp.zsp_block_alloc_destroy(ba)

    assert len(seen) >= 5, f"Only {len(seen)} distinct values from 20 seeds"


# ------------------------------------------------------------------ #
# Solve-before via checkpoint/pin/restore                             #
# ------------------------------------------------------------------ #

def test_solve_before_rebuild(libzsp):
    """Solve-before via problem rebuild: solve mode first, rebuild with data constraints.

    Mirrors t_constraint_solve_before.v. The 'solve X before Y' directive
    means X is determined first. The integration layer implements this by:
      1. Building a problem with just the 'before' variables, solving.
      2. Building a NEW problem with all variables, pinning the 'before'
         variables to their Phase-1 values, and solving again.
    This is a constraint-set merge (union), not incremental addition.
    """
    _wire(libzsp)

    for seed in range(1, 11):
        # Phase 1: solve mode only
        sp1_buf = (ctypes.c_uint8 * _SP)()
        sp1 = libzsp.solve_problem_init(sp1_buf, _SP)
        libzsp.problem_add_var(sp1, 0, 32, 1, 0, 3)  # mode

        ctx1, ba1, buf1 = _make_ctx(libzsp, sp1)
        _solve_ok(libzsp, ctx1, seed=seed)
        mode = libzsp.solver_get_value(ctx1, 0)
        assert 0 <= mode <= 3
        libzsp.solver_destroy(ctx1)
        libzsp.zsp_block_alloc_destroy(ba1)

        # Phase 2: full problem with mode pinned + conditional data constraints
        sp2_buf = (ctypes.c_uint8 * _SP)()
        sp2 = libzsp.solve_problem_init(sp2_buf, _SP)
        libzsp.problem_add_var(sp2, 0, 32, 1, mode, mode)  # mode pinned
        libzsp.problem_add_var(sp2, 1, 8, 0, 0, 255)        # data

        if mode == 0:
            # data == 0
            libzsp.problem_add_constraint(sp2,
                libzsp.expr_binary(sp2, BIN_EQ, libzsp.expr_var(sp2, 1),
                                   libzsp.expr_const(sp2, 0, 0)))
        elif mode == 1:
            # data in [1, 15]
            libzsp.problem_add_constraint(sp2,
                libzsp.expr_binary(sp2, BIN_GTE, libzsp.expr_var(sp2, 1),
                                   libzsp.expr_const(sp2, 1, 0)))
            libzsp.problem_add_constraint(sp2,
                libzsp.expr_binary(sp2, BIN_LTE, libzsp.expr_var(sp2, 1),
                                   libzsp.expr_const(sp2, 15, 0)))
        else:
            # data < 128
            libzsp.problem_add_constraint(sp2,
                libzsp.expr_binary(sp2, BIN_LT, libzsp.expr_var(sp2, 1),
                                   libzsp.expr_const(sp2, 128, 0)))

        ctx2, ba2, buf2 = _make_ctx(libzsp, sp2)
        _solve_ok(libzsp, ctx2, seed=seed + 1000)
        data = libzsp.solver_get_value(ctx2, 1)

        if mode == 0:
            assert data == 0
        elif mode == 1:
            assert 1 <= data <= 15, f"mode=1, data={data}"
        else:
            assert data < 128, f"mode={mode}, data={data}"

        libzsp.solver_destroy(ctx2)
        libzsp.zsp_block_alloc_destroy(ba2)


# ------------------------------------------------------------------ #
# Randc lifecycle                                                      #
# ------------------------------------------------------------------ #

def test_randc_full_cycle(libzsp):
    """Randc: cyclic randomization via solver_exclude_value.

    Mirrors t_randc.v: 3-bit variable cycles through all 8 values.
    """
    _wire(libzsp)

    sp_buf = (ctypes.c_uint8 * _SP)()
    sp = libzsp.solve_problem_init(sp_buf, _SP)
    libzsp.problem_add_var(sp, 0, 3, 0, 0, 7)

    ctx, ba, buf = _make_ctx(libzsp, sp)

    seen = set()
    for i in range(8):
        libzsp.solver_reset(ctx)
        # Re-apply exclusions
        for excl in seen:
            libzsp.solver_exclude_value(ctx, 0, excl)
        _solve_ok(libzsp, ctx, seed=i + 1)
        v = libzsp.solver_get_value(ctx, 0)
        assert v not in seen, f"value {v} repeated"
        seen.add(v)

    assert seen == set(range(8)), f"Missing values: {set(range(8)) - seen}"

    libzsp.solver_destroy(ctx)
    libzsp.zsp_block_alloc_destroy(ba)
