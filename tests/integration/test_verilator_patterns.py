"""Integration tests: Verilator constraint test patterns.

Each test manually translates a key Verilator ``test_regress`` constraint
pattern into the zuspec-solver C API and verifies correctness of the
solution.  This validates that the solver can handle the operator mix,
control flow, and lifecycle patterns that a Verilator integration backend
would produce.
"""
from __future__ import annotations

import ctypes
import pytest

EXPR_NULL     = 0xFFFF_FFFF
SOLVE_OK      = 0
SOLVE_UNSAT   = 1
BIN_ADD  = 0;  BIN_SUB  = 1;  BIN_MUL  = 2;  BIN_DIV  = 3;  BIN_MOD  = 4
BIN_BAND = 5;  BIN_BOR  = 6;  BIN_BXOR = 7;  BIN_LSHIFT = 8; BIN_RSHIFT = 9
BIN_EQ   = 10; BIN_NEQ  = 11; BIN_LT   = 12; BIN_LTE  = 13
BIN_GT   = 14; BIN_GTE  = 15; BIN_AND  = 16; BIN_OR   = 17

_SP  = 65536
_CTX = 1 << 20


class SolveOpts(ctypes.Structure):
    _fields_ = [
        ("seed",            ctypes.c_uint64),
        ("max_conflicts",   ctypes.c_uint32),
        ("max_restarts",    ctypes.c_uint32),
        ("use_phase_save",  ctypes.c_uint8),
        ("_pad",            ctypes.c_uint8 * 3),
        ("max_shave_iters", ctypes.c_uint32),
    ]


class DistEntry(ctypes.Structure):
    _fields_ = [
        ("lo",           ctypes.c_int64),
        ("hi",           ctypes.c_int64),
        ("weight",       ctypes.c_uint32),
        ("is_per_value", ctypes.c_uint8),
        ("_dpad",        ctypes.c_uint8 * 3),
    ]


def _wire(lib):
    """Wire ctypes argtypes/restypes for the C API functions used here."""
    c = ctypes
    lib.zsp_block_alloc_create.restype  = c.c_void_p
    lib.zsp_block_alloc_create.argtypes = [c.c_void_p, c.c_size_t]
    lib.zsp_block_alloc_destroy.restype  = None
    lib.zsp_block_alloc_destroy.argtypes = [c.c_void_p]

    lib.solve_problem_init.restype  = c.c_void_p
    lib.solve_problem_init.argtypes = [c.c_void_p, c.c_size_t]
    lib.problem_add_var.restype  = c.c_uint32
    lib.problem_add_var.argtypes = [c.c_void_p, c.c_uint32,
                                    c.c_uint8, c.c_uint8,
                                    c.c_int64, c.c_int64]
    lib.problem_add_constraint.restype  = c.c_uint32
    lib.problem_add_constraint.argtypes = [c.c_void_p, c.c_uint32]
    lib.problem_add_all_different.restype  = c.c_uint32
    lib.problem_add_all_different.argtypes = [c.c_void_p, c.c_uint32, c.c_void_p]
    lib.problem_add_dist.restype  = c.c_uint32
    lib.problem_add_dist.argtypes = [c.c_void_p, c.c_uint32,
                                     c.c_uint32, c.c_void_p]

    lib.expr_var.restype  = c.c_uint32
    lib.expr_var.argtypes = [c.c_void_p, c.c_uint32]
    lib.expr_const.restype  = c.c_uint32
    lib.expr_const.argtypes = [c.c_void_p, c.c_int64, c.c_uint8]
    lib.expr_binary.restype  = c.c_uint32
    lib.expr_binary.argtypes = [c.c_void_p, c.c_uint32,
                                c.c_uint32, c.c_uint32]
    lib.expr_ite.restype  = c.c_uint32
    lib.expr_ite.argtypes = [c.c_void_p, c.c_uint32,
                             c.c_uint32, c.c_uint32]

    lib.solver_create.restype  = c.c_void_p
    lib.solver_create.argtypes = [c.c_void_p, c.c_size_t, c.c_void_p]
    lib.solver_compile.restype  = c.c_int
    lib.solver_compile.argtypes = [c.c_void_p, c.c_void_p]
    lib.solver_solve.restype  = c.c_int
    lib.solver_solve.argtypes = [c.c_void_p, c.c_void_p]
    lib.solver_get_value.restype  = c.c_int64
    lib.solver_get_value.argtypes = [c.c_void_p, c.c_uint32]
    lib.solver_reset.restype  = None
    lib.solver_reset.argtypes = [c.c_void_p]
    lib.solver_set_seed.restype  = None
    lib.solver_set_seed.argtypes = [c.c_void_p, c.c_uint64]
    lib.solver_pin_var.restype  = c.c_int
    lib.solver_pin_var.argtypes = [c.c_void_p, c.c_uint32, c.c_int64]
    lib.solver_checkpoint.restype  = c.c_int
    lib.solver_checkpoint.argtypes = [c.c_void_p]
    lib.solver_restore.restype  = None
    lib.solver_restore.argtypes = [c.c_void_p, c.c_uint32]
    lib.solver_exclude_value.restype  = c.c_int
    lib.solver_exclude_value.argtypes = [c.c_void_p, c.c_uint32, c.c_int64]


def _build(lib, setup_fn):
    """Create problem, call setup_fn(lib, sp), compile, return (ctx, ba, bufs)."""
    sp_buf  = (ctypes.c_uint8 * _SP)()
    ctx_buf = (ctypes.c_uint8 * _CTX)()
    sp = lib.solve_problem_init(sp_buf, _SP)
    assert sp
    setup_fn(lib, sp)
    ba  = lib.zsp_block_alloc_create(None, 0)
    ctx = lib.solver_create(ctx_buf, _CTX, ba)
    rc  = lib.solver_compile(ctx, sp)
    assert rc >= 0, f"compile failed: {rc}"
    return ctx, ba, sp_buf, ctx_buf


def _solve(lib, ctx, seed=42):
    opts = SolveOpts(seed=seed)
    r = lib.solver_solve(ctx, ctypes.byref(opts))
    assert r == SOLVE_OK, f"solve returned {r}"


# ================================================================== #
# 1. Operators (t_constraint_operators.v)                             #
# ================================================================== #

def test_operators(libzsp):
    """Arithmetic + comparison: a + b == 200, a < b, a >= 50."""
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        # var 0 = a [0,200], var 1 = b [0,200], var 2 = sum [200,200]
        lib.problem_add_var(sp, 0, 8, 0, 0, 200)
        lib.problem_add_var(sp, 1, 8, 0, 0, 200)
        lib.problem_add_var(sp, 2, 8, 0, 200, 200)
        # sum == a + b
        va = lib.expr_var(sp, 0); vb = lib.expr_var(sp, 1)
        vs = lib.expr_var(sp, 2)
        add_e = lib.expr_binary(sp, BIN_ADD, va, vb)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_EQ, vs, add_e))
        # a < b
        va2 = lib.expr_var(sp, 0); vb2 = lib.expr_var(sp, 1)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_LT, va2, vb2))
        # a >= 50
        va3 = lib.expr_var(sp, 0); c50 = lib.expr_const(sp, 50, 0)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_GTE, va3, c50))

    ctx, ba, *_ = _build(lib, setup)
    _solve(lib, ctx)

    a = lib.solver_get_value(ctx, 0)
    b = lib.solver_get_value(ctx, 1)
    assert a + b == 200
    assert a < b
    assert a >= 50
    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 2. State variable (t_constraint_state.v)                            #
# ================================================================== #

def test_state_variable(libzsp):
    """Non-rand member used in constraint (pinned via solver_pin_var)."""
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        # var 0 = state_val [0,255] (will be pinned), var 1 = rand_f [0,255]
        lib.problem_add_var(sp, 0, 8, 0, 0, 255)
        lib.problem_add_var(sp, 1, 8, 0, 0, 255)
        # constraint: rand_f > state_val
        v0 = lib.expr_var(sp, 0); v1 = lib.expr_var(sp, 1)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_GT, v1, v0))

    ctx, ba, *_ = _build(lib, setup)

    # Pin state_val = 100
    rc = lib.solver_pin_var(ctx, 0, 100)
    assert rc == 0

    _solve(lib, ctx)
    sv = lib.solver_get_value(ctx, 0)
    rf = lib.solver_get_value(ctx, 1)
    assert sv == 100
    assert rf > 100

    # Reset, pin to different value
    lib.solver_reset(ctx)
    rc = lib.solver_pin_var(ctx, 0, 200)
    assert rc == 0
    _solve(lib, ctx, seed=99)
    assert lib.solver_get_value(ctx, 0) == 200
    assert lib.solver_get_value(ctx, 1) > 200

    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 3. Conditional constraints (t_constraint_cond.v)                     #
# ================================================================== #

def test_conditional(libzsp):
    """if/else constraint blocks: pin mode, verify each branch.

    mode in [0,1].
    When mode==0: x == ITE(mode_is_0, lo_val, hi_val) picks lo_val in [0,10].
    When mode==1: picks hi_val in [100,200].
    Tests the ITE value propagator with a determined condition.
    """
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        # var 0 = mode [0,1], var 1 = lo_val [0,10]
        # var 2 = hi_val [100,200], var 3 = x [0,255]
        lib.problem_add_var(sp, 0, 1, 0, 0, 1)     # mode (cond)
        lib.problem_add_var(sp, 1, 8, 0, 0, 10)    # lo_val
        lib.problem_add_var(sp, 2, 8, 0, 100, 200)  # hi_val
        lib.problem_add_var(sp, 3, 8, 0, 0, 255)   # x
        # x == ITE(mode, lo_val, hi_val)
        # When mode=1 (true): x = lo_val; mode=0 (false): x = hi_val
        vcond = lib.expr_var(sp, 0)
        vlo = lib.expr_var(sp, 1); vhi = lib.expr_var(sp, 2)
        vx = lib.expr_var(sp, 3)
        ite_e = lib.expr_ite(sp, vcond, vlo, vhi)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_EQ, vx, ite_e))

    # Test mode=0 (false branch -> hi_val)
    ctx, ba, *_ = _build(lib, setup)
    lib.solver_pin_var(ctx, 0, 0)
    _solve(lib, ctx, seed=1)
    x = lib.solver_get_value(ctx, 3)
    assert 100 <= x <= 200, f"mode=0: x={x}, expected [100,200]"
    lib.zsp_block_alloc_destroy(ba)

    # Test mode=1 (true branch -> lo_val)
    ctx, ba, *_ = _build(lib, setup)
    lib.solver_pin_var(ctx, 0, 1)
    _solve(lib, ctx, seed=2)
    x = lib.solver_get_value(ctx, 3)
    assert 0 <= x <= 10, f"mode=1: x={x}, expected [0,10]"
    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 4. Foreach (t_constraint_foreach.v) -- unrolled per-element         #
# ================================================================== #

def test_foreach(libzsp):
    """Unrolled foreach: arr[i] < arr[i+1] for i in 0..3."""
    lib = libzsp; _wire(lib)
    N = 4

    def setup(lib, sp):
        for i in range(N):
            lib.problem_add_var(sp, i, 8, 0, 0, 100)
        for i in range(N - 1):
            va = lib.expr_var(sp, i)
            vb = lib.expr_var(sp, i + 1)
            lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_LT, va, vb))

    ctx, ba, *_ = _build(lib, setup)
    _solve(lib, ctx)

    vals = [lib.solver_get_value(ctx, i) for i in range(N)]
    for i in range(N - 1):
        assert vals[i] < vals[i + 1], f"vals={vals}"
    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 5. Solve-before (t_constraint_solve_before.v) -- checkpoint + pin   #
# ================================================================== #

def test_solve_before(libzsp):
    """Phased solving: solve 'addr' first, then solve 'data' with
    addr pinned. Uses checkpoint/restore."""
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        # var 0 = addr [0,255], var 1 = data [0,255]
        lib.problem_add_var(sp, 0, 8, 0, 0, 255)
        lib.problem_add_var(sp, 1, 8, 0, 0, 255)
        # constraint: data > addr
        va = lib.expr_var(sp, 0); vd = lib.expr_var(sp, 1)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_GT, vd, va))

    ctx, ba, *_ = _build(lib, setup)

    # Phase 1: checkpoint, pin data to something valid, solve addr
    cp = lib.solver_checkpoint(ctx)
    lib.solver_pin_var(ctx, 1, 255)  # force data to 255 so addr is free
    _solve(lib, ctx, seed=10)
    addr_val = lib.solver_get_value(ctx, 0)

    # Restore, pin addr, solve everything
    lib.solver_restore(ctx, cp)
    lib.solver_pin_var(ctx, 0, addr_val)
    _solve(lib, ctx, seed=20)

    addr_final = lib.solver_get_value(ctx, 0)
    data_final = lib.solver_get_value(ctx, 1)
    assert addr_final == addr_val
    assert data_final > addr_final

    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 6. Distribution (t_constraint_dist.v)                                #
# ================================================================== #

def test_dist(libzsp):
    """dist { [0:9] :/ 1, [10:19] :/ 3 }. Verify statistical bias."""
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        lib.problem_add_var(sp, 0, 8, 0, 0, 19)
        entries = (DistEntry * 2)()
        entries[0].lo = 0;  entries[0].hi = 9;  entries[0].weight = 1; entries[0].is_per_value = 0
        entries[1].lo = 10; entries[1].hi = 19; entries[1].weight = 3; entries[1].is_per_value = 0
        lib.problem_add_dist(sp, 0, 2, entries)

    ctx, ba, *_ = _build(lib, setup)

    count_hi = 0
    N = 500
    for i in range(N):
        lib.solver_reset(ctx)
        _solve(lib, ctx, seed=7000 + i)
        x = lib.solver_get_value(ctx, 0)
        assert 0 <= x <= 19
        if x >= 10:
            count_hi += 1

    ratio = count_hi / N
    assert 0.55 <= ratio <= 0.95, f"Expected ~75% in [10:19], got {ratio*100:.1f}%"
    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 7. Unique / AllDifferent (t_constraint_unq_arr_derived.v)            #
# ================================================================== #

def test_unique(libzsp):
    """AllDifferent on 5 variables in [0,9]."""
    lib = libzsp; _wire(lib)
    N = 5

    def setup(lib, sp):
        for i in range(N):
            lib.problem_add_var(sp, i, 8, 0, 0, 9)
        vids = (ctypes.c_uint32 * N)(*range(N))
        lib.problem_add_all_different(sp, N, vids)

    ctx, ba, *_ = _build(lib, setup)
    _solve(lib, ctx)

    vals = [lib.solver_get_value(ctx, i) for i in range(N)]
    assert len(set(vals)) == N, f"Not all different: {vals}"
    for v in vals:
        assert 0 <= v <= 9
    lib.zsp_block_alloc_destroy(ba)


# ================================================================== #
# 8. Shift-based alignment (t_constraint_shift_width.v)                #
# ================================================================== #

def test_alignment(libzsp):
    """addr & ((1 << size) - 1) == 0.
    Approximated: size=4, addr constrained to be multiple of 16 via
    addr & 0xF == 0."""
    lib = libzsp; _wire(lib)

    def setup(lib, sp):
        # var 0 = addr [0,255], var 1 = masked [0,0]
        lib.problem_add_var(sp, 0, 8, 0, 0, 255)
        lib.problem_add_var(sp, 1, 8, 0, 0, 0)   # must be 0
        # var 2 = mask_const [15,15]
        lib.problem_add_var(sp, 2, 8, 0, 15, 15)
        # masked == addr & mask_const
        va = lib.expr_var(sp, 0); vmask = lib.expr_var(sp, 2)
        vm = lib.expr_var(sp, 1)
        band_e = lib.expr_binary(sp, BIN_BAND, va, vmask)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_EQ, vm, band_e))

    ctx, ba, *_ = _build(lib, setup)

    for seed in range(50):
        lib.solver_reset(ctx)
        _solve(lib, ctx, seed=8000 + seed)
        addr = lib.solver_get_value(ctx, 0)
        assert addr % 16 == 0, f"seed={seed}: addr={addr} not aligned to 16"

    lib.zsp_block_alloc_destroy(ba)
