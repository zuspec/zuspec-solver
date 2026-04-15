"""Integration test: comprehensive operator coverage.

Mirrors t_constraint_operators.v from Verilator test_regress. Each test
translates a constraint from that file into the zuspec-solver C API.
"""
from __future__ import annotations

import ctypes
import pytest

EXPR_NULL = 0xFFFF_FFFF
SOLVE_OK = 0
BIN_ADD = 0; BIN_SUB = 1; BIN_MUL = 2; BIN_DIV = 3; BIN_MOD = 4
BIN_BAND = 5; BIN_BOR = 6; BIN_BXOR = 7; BIN_LSHIFT = 8; BIN_RSHIFT = 9
BIN_EQ = 10; BIN_NEQ = 11; BIN_LT = 12; BIN_LTE = 13
BIN_GT = 14; BIN_GTE = 15
UN_NEG = 0; UN_NOT = 1; UN_INVERT = 2

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
        ("problem_add_var", c.c_uint32,
         [c.c_void_p, c.c_uint32, c.c_uint8, c.c_uint8, c.c_int64, c.c_int64]),
        ("problem_add_constraint", c.c_uint32, [c.c_void_p, c.c_uint32]),
        ("expr_var", c.c_uint32, [c.c_void_p, c.c_uint32]),
        ("expr_const", c.c_uint32, [c.c_void_p, c.c_int64, c.c_uint8]),
        ("expr_binary", c.c_uint32,
         [c.c_void_p, c.c_int32, c.c_uint32, c.c_uint32]),
        ("expr_unary", c.c_uint32, [c.c_void_p, c.c_int32, c.c_uint32]),
        ("expr_ite", c.c_uint32,
         [c.c_void_p, c.c_uint32, c.c_uint32, c.c_uint32]),
        ("solver_create", c.c_void_p, [c.c_void_p, c.c_size_t, c.c_void_p]),
        ("solver_destroy", None, [c.c_void_p]),
        ("solver_compile", c.c_int, [c.c_void_p, c.c_void_p]),
        ("solver_solve", c.c_int, [c.c_void_p, c.c_void_p]),
        ("solver_get_value", c.c_int64, [c.c_void_p, c.c_uint32]),
    ]:
        getattr(lib, name).restype = rt
        getattr(lib, name).argtypes = at


def _solve(lib, sp, n_vars, seed=42):
    ctx_buf = (ctypes.c_uint8 * _CTX)()
    ba = lib.zsp_block_alloc_create(None, _CTX)
    ctx = lib.solver_create(ctx_buf, _CTX, ba)
    assert ctx
    crc = lib.solver_compile(ctx, sp)
    assert crc == 0, f"compile returned {crc}"
    opts = SolveOpts(seed=seed)
    rc = lib.solver_solve(ctx, ctypes.byref(opts))
    assert rc == SOLVE_OK, f"solve returned {rc}"
    vals = [lib.solver_get_value(ctx, i) for i in range(n_vars)]
    lib.solver_destroy(ctx)
    lib.zsp_block_alloc_destroy(ba)
    return vals


def _sp(lib):
    buf = (ctypes.c_uint8 * _SP)()
    sp = lib.solve_problem_init(buf, _SP)
    return sp, buf


def test_arith_identity(libzsp):
    """x + x - x == x (identity). Mirrors 'constraint arith'."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    # var 0: x [0, 1000], var 1: tmp1 (x+x), var 2: tmp2 (tmp1-x)
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 1000)
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 2000)
    libzsp.problem_add_var(sp, 2, 32, 1, 0, 2000)

    # tmp1 == x + x
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 1),
            libzsp.expr_binary(sp, BIN_ADD,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 0))))
    # tmp2 == tmp1 - x
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 2),
            libzsp.expr_binary(sp, BIN_SUB,
                               libzsp.expr_var(sp, 1), libzsp.expr_var(sp, 0))))
    # tmp2 == x
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ,
                           libzsp.expr_var(sp, 2), libzsp.expr_var(sp, 0)))

    for seed in range(1, 11):
        vals = _solve(libzsp, sp, 3, seed=seed)
        assert vals[2] == vals[0], f"x+x-x={vals[2]} != x={vals[0]}"


def test_mul_neq(libzsp):
    """x * 9 != b * 3. Mirrors 'constraint mul'."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    # var 0: x, var 1: b, var 2: x*9, var 3: b*3
    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)
    libzsp.problem_add_var(sp, 1, 32, 0, 0, 100)
    libzsp.problem_add_var(sp, 2, 32, 1, 0, 900)
    libzsp.problem_add_var(sp, 3, 32, 1, 0, 300)
    libzsp.problem_add_var(sp, 4, 32, 1, 9, 9)   # const 9
    libzsp.problem_add_var(sp, 5, 32, 1, 3, 3)   # const 3

    # tmp2 == x * 9
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 2),
            libzsp.expr_binary(sp, BIN_MUL,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 4))))
    # tmp3 == b * 3
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 3),
            libzsp.expr_binary(sp, BIN_MUL,
                               libzsp.expr_var(sp, 1), libzsp.expr_var(sp, 5))))
    # tmp2 != tmp3
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_NEQ,
                           libzsp.expr_var(sp, 2), libzsp.expr_var(sp, 3)))

    for seed in range(1, 11):
        vals = _solve(libzsp, sp, 6, seed=seed)
        assert vals[0] * 9 != vals[1] * 3


def test_implication(libzsp):
    """tiny == 1 -> x != 10. Mirrors 'constraint impl'."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    libzsp.problem_add_var(sp, 0, 1, 0, 0, 1)     # tiny
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # x

    # ITE(tiny == 1, x != 10, true)
    cond = libzsp.expr_binary(sp, BIN_EQ,
                               libzsp.expr_var(sp, 0),
                               libzsp.expr_const(sp, 1, 0))
    then_e = libzsp.expr_binary(sp, BIN_NEQ,
                                 libzsp.expr_var(sp, 1),
                                 libzsp.expr_const(sp, 10, 0))
    else_e = libzsp.expr_const(sp, 1, 0)
    ite = libzsp.expr_ite(sp, cond, then_e, else_e)
    libzsp.problem_add_constraint(sp, ite)

    for seed in range(1, 21):
        vals = _solve(libzsp, sp, 2, seed=seed)
        if vals[0] == 1:
            assert vals[1] != 10, f"tiny=1 but x=10"


def test_ite_value(libzsp):
    """(tiny == 1 ? b : c) != 17. Mirrors 'constraint cond'."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    libzsp.problem_add_var(sp, 0, 1, 0, 0, 1)      # tiny
    libzsp.problem_add_var(sp, 1, 32, 0, 0, 255)    # b
    libzsp.problem_add_var(sp, 2, 32, 0, 0, 255)    # c
    libzsp.problem_add_var(sp, 3, 32, 1, 0, 255)    # result of ITE

    # result = tiny ? b : c  (ITE as value)
    cond = libzsp.expr_binary(sp, BIN_EQ,
                               libzsp.expr_var(sp, 0),
                               libzsp.expr_const(sp, 1, 0))
    then_eq = libzsp.expr_binary(sp, BIN_EQ,
                                  libzsp.expr_var(sp, 3),
                                  libzsp.expr_var(sp, 1))
    else_eq = libzsp.expr_binary(sp, BIN_EQ,
                                  libzsp.expr_var(sp, 3),
                                  libzsp.expr_var(sp, 2))
    ite = libzsp.expr_ite(sp, cond, then_eq, else_eq)
    libzsp.problem_add_constraint(sp, ite)

    # result != 17
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_NEQ,
                           libzsp.expr_var(sp, 3),
                           libzsp.expr_const(sp, 17, 0)))

    for seed in range(1, 21):
        vals = _solve(libzsp, sp, 4, seed=seed)
        result = vals[3]
        if vals[0] == 1:
            assert result == vals[1]
        else:
            assert result == vals[2]
        assert result != 17


def test_sub_constraint(libzsp):
    """r == a - b. Verify subtraction works."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # a
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 100)   # b
    libzsp.problem_add_var(sp, 2, 32, 1, -100, 100) # r

    # r == a - b
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 2),
            libzsp.expr_binary(sp, BIN_SUB,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 1))))
    # a > b (so r > 0)
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT,
                           libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 1)))

    for seed in range(1, 11):
        vals = _solve(libzsp, sp, 3, seed=seed)
        assert vals[2] == vals[0] - vals[1]
        assert vals[2] > 0


def test_mod_constraint(libzsp):
    """r == x % 5. Verify modulo."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    libzsp.problem_add_var(sp, 0, 32, 1, 0, 100)   # x
    libzsp.problem_add_var(sp, 1, 32, 1, 0, 4)     # r
    libzsp.problem_add_var(sp, 2, 32, 1, 5, 5)     # const 5

    # r == x % 5
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 1),
            libzsp.expr_binary(sp, BIN_MOD,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 2))))
    # r == 0 (x divisible by 5)
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ,
                           libzsp.expr_var(sp, 1),
                           libzsp.expr_const(sp, 0, 0)))

    for seed in range(1, 11):
        vals = _solve(libzsp, sp, 3, seed=seed)
        assert vals[0] % 5 == 0, f"x={vals[0]} not divisible by 5"


def test_bitwise_and_or_xor(libzsp):
    """(b ^ c) & (b >> c | b << c) > 0. Mixed bitwise/shift."""
    _wire(libzsp)
    sp, buf = _sp(libzsp)

    # Simplified: r1 = b ^ c, r2 = b >> 1, r3 = r1 & r2, r3 > 0
    libzsp.problem_add_var(sp, 0, 8, 0, 1, 255)     # b (>0 for non-trivial)
    libzsp.problem_add_var(sp, 1, 8, 0, 1, 255)     # c
    libzsp.problem_add_var(sp, 2, 8, 0, 0, 255)     # r1 = b ^ c
    libzsp.problem_add_var(sp, 3, 8, 0, 0, 255)     # r2 = b >> 1
    libzsp.problem_add_var(sp, 4, 8, 0, 0, 255)     # r3 = r1 & r2
    libzsp.problem_add_var(sp, 5, 32, 1, 1, 1)      # const 1

    # r1 == b ^ c
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 2),
            libzsp.expr_binary(sp, BIN_BXOR,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 1))))
    # r2 == b >> 1
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 3),
            libzsp.expr_binary(sp, BIN_RSHIFT,
                               libzsp.expr_var(sp, 0), libzsp.expr_var(sp, 5))))
    # r3 == r1 & r2
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_EQ, libzsp.expr_var(sp, 4),
            libzsp.expr_binary(sp, BIN_BAND,
                               libzsp.expr_var(sp, 2), libzsp.expr_var(sp, 3))))
    # r3 > 0
    libzsp.problem_add_constraint(sp,
        libzsp.expr_binary(sp, BIN_GT, libzsp.expr_var(sp, 4),
                           libzsp.expr_const(sp, 0, 0)))

    for seed in range(1, 11):
        vals = _solve(libzsp, sp, 6, seed=seed)
        b, c = vals[0], vals[1]
        r1 = (b ^ c) & 0xFF
        r2 = (b >> 1) & 0xFF
        r3 = (r1 & r2) & 0xFF
        assert r3 > 0, f"b={b} c={c} r3={r3}"
