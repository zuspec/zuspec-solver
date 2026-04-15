"""Integration test: combined AllDifferent + StateGraph + Checkpoint/Restore.

Validates the full pipeline from state-graph construction through
inference chain resolution to solver execution.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from zuspec.solver.state_graph import (
    FieldDescriptor, TransitionDescriptor,
    StateGraphBuilder, StateGraph,
)
from zuspec.solver.structural_solver import (
    solve_state_chain_graph_guided,
    InferredAction,
)
from zuspec.solver.flow_constraint_store import (
    ProjectedConstraint,
    project_to_producer,
    FlowObjectConstraintStore,
)


# Import conftest's libzsp fixture
sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))


EXPR_NULL     = 0xFFFF_FFFF
SOLVE_OK      = 0
BIN_EQ        = 10
BIN_LTE       = 13
BIN_GTE       = 15
_CTX_BUF_SIZE = 1 << 20


class _SolveOpts(ctypes.Structure):
    _fields_ = [
        ("seed",           ctypes.c_uint64),
        ("max_conflicts",  ctypes.c_uint32),
        ("max_restarts",   ctypes.c_uint32),
        ("use_phase_save", ctypes.c_uint8),
        ("_pad",           ctypes.c_uint8 * 3),
        ("max_shave_iters", ctypes.c_uint32),
    ]


def _setup_lib(lib):
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
    lib.expr_const.restype  = c.c_uint32
    lib.expr_const.argtypes = [c.c_void_p, c.c_int64, c.c_uint8]
    lib.expr_var.restype  = c.c_uint32
    lib.expr_var.argtypes = [c.c_void_p, c.c_uint32]
    lib.expr_binary.restype  = c.c_uint32
    lib.expr_binary.argtypes = [c.c_void_p, c.c_int32, c.c_uint32, c.c_uint32]
    lib.solver_create.restype  = c.c_void_p
    lib.solver_create.argtypes = [c.c_void_p, c.c_size_t, c.c_void_p]
    lib.solver_compile.restype  = c.c_int
    lib.solver_compile.argtypes = [c.c_void_p, c.c_void_p]
    lib.solver_add_constraint.restype  = c.c_int
    lib.solver_add_constraint.argtypes = [c.c_void_p, c.c_void_p]
    lib.solver_checkpoint.restype  = c.c_int
    lib.solver_checkpoint.argtypes = [c.c_void_p]
    lib.solver_restore.restype  = None
    lib.solver_restore.argtypes = [c.c_void_p, c.c_uint32]
    lib.solver_solve.restype  = c.c_int
    lib.solver_solve.argtypes = [c.c_void_p, c.c_void_p]
    lib.solver_get_value.restype  = c.c_int64
    lib.solver_get_value.argtypes = [c.c_void_p, c.c_uint32]


class TestCombinedPipeline:
    """End-to-end: state graph + inference + solver with AllDifferent + checkpoint."""

    def test_thermal_state_chain_with_solver(self, libzsp):
        """Build thermal state graph, infer 3-step path, solve each step."""
        lib = libzsp
        _setup_lib(lib)

        # 1. Build state graph: thermal_level in [0,3], +/-1 transitions
        fields = [FieldDescriptor("level", 0, 3)]
        def step(values):
            v = values[0]
            nexts = []
            if v + 1 <= 3: nexts.append((v + 1,))
            if v - 1 >= 0: nexts.append((v - 1,))
            return nexts

        builder = StateGraphBuilder(
            field_descriptors=fields,
            transition_descriptors=[TransitionDescriptor(0, step)],
            initial_predicate=lambda v: v[0] == 0,
        )
        g = builder.build()
        assert len(g.nodes) == 4

        # 2. Infer path from level=0 to level=3
        start = {g._value_to_index[(0,)]}
        goal = {g._value_to_index[(3,)]}
        actions = solve_state_chain_graph_guided(g, [start, goal])
        assert len(actions) == 3

        # 3. For each inferred action, solve with state constraints
        for action in actions:
            buf = (ctypes.c_uint8 * 65536)()
            sp = lib.solve_problem_init(buf, 65536)

            # var 0 = prev_level (fixed), var 1 = next_level (fixed),
            # var 2 = adjustment (random data field)
            prev_val = action.prev_state_values[0]
            next_val = action.next_state_values[0]
            lib.problem_add_var(sp, 0, 8, 0, prev_val, prev_val)
            lib.problem_add_var(sp, 1, 8, 0, next_val, next_val)
            lib.problem_add_var(sp, 2, 8, 1, -10, 10)  # random payload

            ba = lib.zsp_block_alloc_create(None, _CTX_BUF_SIZE)
            ctx_buf = (ctypes.c_uint8 * _CTX_BUF_SIZE)()
            ctx = lib.solver_create(ctx_buf, _CTX_BUF_SIZE, ba)
            lib.solver_compile(ctx, sp)

            opts = _SolveOpts(seed=42)
            result = lib.solver_solve(ctx, ctypes.byref(opts))
            assert result == SOLVE_OK

            got_prev = lib.solver_get_value(ctx, 0)
            got_next = lib.solver_get_value(ctx, 1)
            assert got_prev == prev_val
            assert got_next == next_val
            lib.zsp_block_alloc_destroy(ba)

    def test_alldiff_with_checkpoint_restore(self, libzsp):
        """AllDifferent + checkpoint/restore: solve, undo, re-solve."""
        lib = libzsp
        _setup_lib(lib)

        buf = (ctypes.c_uint8 * 65536)()
        sp = lib.solve_problem_init(buf, 65536)
        for i in range(4):
            lib.problem_add_var(sp, i, 8, 0, 0, 3)

        ba = lib.zsp_block_alloc_create(None, _CTX_BUF_SIZE)
        ctx_buf = (ctypes.c_uint8 * _CTX_BUF_SIZE)()
        ctx = lib.solver_create(ctx_buf, _CTX_BUF_SIZE, ba)
        lib.solver_compile(ctx, sp)

        # Checkpoint before adding AllDifferent
        cp = lib.solver_checkpoint(ctx)

        # Add AllDifferent
        aux_buf = (ctypes.c_uint8 * 65536)()
        aux_sp = lib.solve_problem_init(aux_buf, 65536)
        vids = (ctypes.c_uint32 * 4)(*range(4))
        lib.problem_add_all_different(aux_sp, 4, vids)
        lib.solver_add_constraint(ctx, aux_sp)

        # Solve with AllDifferent
        opts = _SolveOpts(seed=1)
        result = lib.solver_solve(ctx, ctypes.byref(opts))
        assert result == SOLVE_OK
        vals = [lib.solver_get_value(ctx, i) for i in range(4)]
        assert sorted(vals) == [0, 1, 2, 3], f"Not a permutation: {vals}"

        # Restore (removes AllDifferent)
        lib.solver_restore(ctx, cp)

        # Solve without AllDifferent -- should still work
        opts2 = _SolveOpts(seed=99)
        result2 = lib.solver_solve(ctx, ctypes.byref(opts2))
        assert result2 == SOLVE_OK
        lib.zsp_block_alloc_destroy(ba)

    def test_flow_projection_with_solver(self, libzsp):
        """Flow projection: consumer constraint projected to producer, solved."""
        lib = libzsp
        _setup_lib(lib)

        # Consumer wants input.addr > 100
        constraints = [{"field": "input.addr", "op": "gt", "bound": 100}]
        mapping = {"input.addr": "output.addr"}
        projected = project_to_producer(constraints, mapping)
        assert len(projected) == 1
        assert projected[0].op == "gt"
        assert projected[0].bound == 100

        # Build producer problem with output.addr constrained by projection
        buf = (ctypes.c_uint8 * 65536)()
        sp = lib.solve_problem_init(buf, 65536)
        lib.problem_add_var(sp, 0, 16, 0, 0, 255)  # output.addr

        # Apply projected constraint: output.addr > 100 → output.addr >= 101
        vref = lib.expr_var(sp, 0)
        cref = lib.expr_const(sp, 101, 0)
        lib.problem_add_constraint(sp, lib.expr_binary(sp, BIN_GTE, vref, cref))

        ba = lib.zsp_block_alloc_create(None, _CTX_BUF_SIZE)
        ctx_buf = (ctypes.c_uint8 * _CTX_BUF_SIZE)()
        ctx = lib.solver_create(ctx_buf, _CTX_BUF_SIZE, ba)
        lib.solver_compile(ctx, sp)

        opts = _SolveOpts(seed=42)
        result = lib.solver_solve(ctx, ctypes.byref(opts))
        assert result == SOLVE_OK
        addr = lib.solver_get_value(ctx, 0)
        assert addr > 100, f"Expected addr > 100, got {addr}"
        lib.zsp_block_alloc_destroy(ba)
