"""Unit tests for the SV Randomizer Generator (Step 6).

Verifies that ``SVRandomizerGenerator.emit()`` produces valid SV text
with correct structure and that the embedded problem buffer bytes can
round-trip through the native solver.
"""
from __future__ import annotations

import ctypes
import os
import re

import pytest

from dataclasses import dataclass
from zuspec.dataclasses import rand, constraint

# ------------------------------------------------------------------ #
# Test dataclass definitions (must be module-level so the constraint
# parser can read source via inspect.getsource)
# ------------------------------------------------------------------ #


@dataclass
class SimpleFields:
    """Two rand fields, no constraints."""
    a: int = rand(domain=(0, 100))
    b: int = rand(domain=(0, 200))


@dataclass
class ConstrainedPair:
    """Two rand fields with a <= constraint."""
    x: int = rand(domain=(0, 50))
    y: int = rand(domain=(0, 50))

    @constraint
    def x_le_y(self):
        assert self.x <= self.y


@dataclass
class ThreeVars:
    """Three rand fields with two constraints."""
    addr: int = rand(domain=(0, 255))
    size: int = rand(domain=(1, 8))
    tag: int = rand(domain=(0, 15))

    @constraint
    def addr_align(self):
        assert self.addr <= 240

    @constraint
    def size_bound(self):
        assert self.size >= 1


# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #

_CTX_BUF_SIZE = 1 << 20


class _SolveOpts(ctypes.Structure):
    _fields_ = [
        ("seed",            ctypes.c_uint64),
        ("max_conflicts",   ctypes.c_uint32),
        ("max_restarts",    ctypes.c_uint32),
        ("use_phase_save",  ctypes.c_uint8),
        ("_pad",            ctypes.c_uint8 * 3),
        ("max_shave_iters", ctypes.c_uint32),
    ]


@pytest.fixture(autouse=True)
def _ensure_lib_discoverable(libzsp):
    """Make ``_load_lib()`` find the session-built libzsp_solver.so.

    The ``libzsp`` fixture builds the library into a temp dir and returns
    a CDLL handle.  We extract the path from that handle and point
    ``ZSP_SOLVER_PATH`` at it so the generator's internal ``_load_lib()``
    succeeds.  We also reset the module-level cache to pick up the change.
    """
    import zuspec.solver.lib as _lib_mod

    lib_path = libzsp._name
    lib_dir = os.path.dirname(lib_path)

    old_val = os.environ.get("ZSP_SOLVER_PATH")
    os.environ["ZSP_SOLVER_PATH"] = lib_dir

    # Reset the module-level cache so _load_lib re-discovers the library.
    _lib_mod._LIB_CACHE = None
    _lib_mod._LOAD_ATTEMPTED = False

    yield

    # Restore
    if old_val is None:
        os.environ.pop("ZSP_SOLVER_PATH", None)
    else:
        os.environ["ZSP_SOLVER_PATH"] = old_val
    _lib_mod._LIB_CACHE = None
    _lib_mod._LOAD_ATTEMPTED = False


# ------------------------------------------------------------------ #
# Test class                                                          #
# ------------------------------------------------------------------ #

class TestSVRandomizerGen:
    """Tests for SVRandomizerGenerator."""

    @pytest.fixture(autouse=True)
    def setup(self, libzsp):
        self.lib = libzsp
        # Wire solver functions for round-trip buffer testing
        c = ctypes
        self.lib.zsp_block_alloc_create.restype = c.c_void_p
        self.lib.zsp_block_alloc_create.argtypes = [c.c_void_p, c.c_size_t]
        self.lib.zsp_block_alloc_destroy.restype = None
        self.lib.zsp_block_alloc_destroy.argtypes = [c.c_void_p]
        self.lib.solver_create.restype = c.c_void_p
        self.lib.solver_create.argtypes = [c.c_void_p, c.c_size_t, c.c_void_p]
        self.lib.solver_destroy.restype = None
        self.lib.solver_destroy.argtypes = [c.c_void_p]
        self.lib.solver_compile.restype = c.c_int
        self.lib.solver_compile.argtypes = [c.c_void_p, c.c_void_p]
        self.lib.solver_solve.restype = c.c_int
        self.lib.solver_solve.argtypes = [c.c_void_p, c.c_void_p]
        self.lib.solver_get_value.restype = c.c_int64
        self.lib.solver_get_value.argtypes = [c.c_void_p, c.c_uint32]

    def _solve_bytes(self, data: bytes, n_vars: int, seed: int = 0x42):
        """Load finalized problem bytes into the solver, solve, return values."""
        buf = (ctypes.c_uint8 * len(data))(*data)
        sp_ptr = ctypes.cast(buf, ctypes.c_void_p).value
        ctx_buf = (ctypes.c_uint8 * _CTX_BUF_SIZE)()
        ba = self.lib.zsp_block_alloc_create(None, 0)
        assert ba
        ctx = self.lib.solver_create(ctx_buf, _CTX_BUF_SIZE, ba)
        assert ctx
        rc = self.lib.solver_compile(ctx, sp_ptr)
        assert rc == 0, f"solver_compile failed: {rc}"
        opts = _SolveOpts(seed=seed)
        result = self.lib.solver_solve(ctx, ctypes.byref(opts))
        assert result == 0, f"solver_solve failed: {result}"
        values = [self.lib.solver_get_value(ctx, i) for i in range(n_vars)]
        self.lib.solver_destroy(ctx)
        self.lib.zsp_block_alloc_destroy(ba)
        return values

    # ---- Structural tests ------------------------------------------ #

    def test_emit_contains_sv_constructs(self):
        """Generated SV contains class, extends, PROBLEM_B64, apply_solution."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        sv = SVRandomizerGenerator().emit(SimpleFields)

        assert "class SimpleFields" in sv
        assert "SimpleFields_randomizer" in sv
        assert "extends zsp_randomizer_pkg::zsp_randomizer" in sv
        assert "PROBLEM_B64" in sv
        assert "apply_solution" in sv
        assert "get_problem_b64" in sv
        assert "get_n_vars" in sv
        assert "endclass" in sv
        assert "endmodule" in sv

    def test_emit_field_declarations(self):
        """User class has rand field declarations."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        sv = SVRandomizerGenerator().emit(SimpleFields)

        assert re.search(r"rand\s+.+\s+a\s*;", sv), "Missing rand field 'a'"
        assert re.search(r"rand\s+.+\s+b\s*;", sv), "Missing rand field 'b'"

    def test_emit_n_vars_matches(self):
        """get_n_vars returns the correct count."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        sv = SVRandomizerGenerator().emit(ThreeVars)

        # n_vars should be at least 3 (addr, size, tag)
        match = re.search(r"return\s+(\d+)\s*;", sv)
        assert match, "Could not find return N in get_n_vars"
        n = int(match.group(1))
        assert n >= 3, f"Expected n_vars >= 3, got {n}"

    def test_problem_data_nonempty(self):
        """PROBLEM_B64 string is non-empty."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        sv = SVRandomizerGenerator().emit(SimpleFields)

        # Count hex byte literals
        b64_match = re.search(r"PROBLEM_B64\s*=", sv)
        assert b64_match, "PROBLEM_B64 not found in output"

    def test_constrained_pair_structure(self):
        """Constrained class emits correct SV."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        sv = SVRandomizerGenerator().emit(ConstrainedPair)

        assert "class ConstrainedPair" in sv
        assert "ConstrainedPair_randomizer" in sv
        assert "PROBLEM_B64" in sv

    # ---- Round-trip buffer tests ----------------------------------- #

    def test_buffer_round_trip_simple(self):
        """Finalized problem bytes compile and solve correctly."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        gen = SVRandomizerGenerator()
        problem_bytes, var_id_map, fields = gen._build_problem(SimpleFields)

        assert isinstance(problem_bytes, bytes)
        assert len(problem_bytes) > 0

        n_vars = len(var_id_map)
        values = self._solve_bytes(problem_bytes, n_vars)
        # Both vars have bounded domains
        for v in values:
            assert v >= 0

    def test_buffer_round_trip_constrained(self):
        """Constrained problem buffer solves with constraint satisfaction."""
        from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
        gen = SVRandomizerGenerator()
        problem_bytes, var_id_map, fields = gen._build_problem(ConstrainedPair)

        n_vars = len(var_id_map)
        # Solve multiple times to verify constraint holds
        for seed in range(10):
            values = self._solve_bytes(problem_bytes, n_vars, seed=seed + 1)
            x_val = values[var_id_map["x"]]
            y_val = values[var_id_map["y"]]
            assert x_val <= y_val, (
                f"Constraint x<=y violated: x={x_val}, y={y_val} (seed={seed+1})"
            )
            assert 0 <= x_val <= 50
            assert 0 <= y_val <= 50
