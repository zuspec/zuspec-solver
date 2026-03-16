"""Python wrapper for the C SolveCtx (solver session) API.

Usage::

    sp = SolveProblem()
    # ... add vars / constraints ...
    with SolveCtx(sp) as ctx:
        result = ctx.solve(seed=42)
        if result == SOLVE_OK:
            x = ctx.get_value(0)
"""
from __future__ import annotations

import ctypes
from typing import Optional

from .lib import _load_lib

# ------------------------------------------------------------------ #
# SolveResult constants (must match zsp_search.h)                     #
# ------------------------------------------------------------------ #
SOLVE_OK      = 0
SOLVE_UNSAT   = 1
SOLVE_TIMEOUT = 2

# Buffer sizes
_CTX_BUF_SIZE = 1 << 20  # 1 MiB — headroom for propagators + decisions


# ------------------------------------------------------------------ #
# SolveOpts ctypes struct                                              #
# Layout (must match zsp_search.h exactly):                           #
#   seed(uint64=8) + max_conflicts(uint32=4) + max_restarts(uint32=4) #
#   + use_phase_save(uint8=1) + _pad[3]  → total 20 bytes            #
# ------------------------------------------------------------------ #
class _SolveOpts(ctypes.Structure):
    _fields_ = [
        ("seed",           ctypes.c_uint64),
        ("max_conflicts",  ctypes.c_uint32),
        ("max_restarts",   ctypes.c_uint32),
        ("use_phase_save", ctypes.c_uint8),
        ("_pad",           ctypes.c_uint8 * 3),
    ]


class CompileUnsatError(Exception):
    """Raised when solver_compile detects UNSAT via bound tightening.

    Means the constraint system is provably unsatisfiable without needing
    to run the search loop.
    """


class CompileIncompleteError(Exception):
    """Raised when solver_compile could not handle one or more constraints.

    The native back-end catches this and transparently falls back to the
    Python solver so that all constraints are respected.
    """


class SolveCtx:
    """Thin ctypes wrapper around the C ``SolveCtx`` + block-allocator.

    The context is compiled against a ``SolveProblem`` and then solved.
    """

    def __init__(self, problem: "SolveProblem", ctx_buf_size: int = _CTX_BUF_SIZE) -> None:  # noqa: F821
        lib = _load_lib()
        if lib is None:
            raise RuntimeError("libzsp_solver.so not found — native solver unavailable")
        self._lib = lib

        # Block allocator owns all dynamic memory used by the context.
        self._ba = lib.zsp_block_alloc_create(None, ctx_buf_size)
        if self._ba is None:
            raise RuntimeError("zsp_block_alloc_create failed")

        # Context lives inside a caller-managed buffer.
        self._ctx_buf = (ctypes.c_uint8 * ctx_buf_size)()
        ctx = lib.solver_create(self._ctx_buf, ctx_buf_size, self._ba)
        if ctx is None:
            lib.zsp_block_alloc_destroy(self._ba)
            raise RuntimeError("solver_create failed")
        self._ctx = ctx  # c_void_p value

        # Compile constraints from the problem into this context.
        rc = lib.solver_compile(self._ctx, problem._sp)
        if rc == -2:
            lib.zsp_block_alloc_destroy(self._ba)
            raise CompileUnsatError("Domain became empty during compile-time bound tightening")
        if rc < 0:
            lib.zsp_block_alloc_destroy(self._ba)
            raise RuntimeError(f"solver_compile failed (rc={rc})")
        if rc > 0:
            lib.zsp_block_alloc_destroy(self._ba)
            raise CompileIncompleteError(
                f"{rc} constraint(s) could not be compiled natively"
            )

    # ------------------------------------------------------------------ #
    # Context manager support                                              #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "SolveCtx":
        return self

    def __exit__(self, *_) -> None:
        self.destroy()

    def destroy(self) -> None:
        """Release all native resources."""
        if self._ba is not None:
            self._lib.zsp_block_alloc_destroy(self._ba)
            self._ba = None

    # ------------------------------------------------------------------ #
    # Solve API                                                            #
    # ------------------------------------------------------------------ #

    def solve(
        self,
        seed: int = 0,
        max_conflicts: int = 0,
        max_restarts: int = 0,
        use_phase_save: bool = False,
    ) -> int:
        """Run the search loop; returns SOLVE_OK, SOLVE_UNSAT, or SOLVE_TIMEOUT."""
        opts = _SolveOpts(
            seed=seed,
            max_conflicts=max_conflicts,
            max_restarts=max_restarts,
            use_phase_save=1 if use_phase_save else 0,
        )
        return self._lib.solver_solve(self._ctx, ctypes.byref(opts))

    def get_value(self, var_id: int) -> int:
        """Return assigned value for var_id after a successful solve."""
        return self._lib.solver_get_value(self._ctx, ctypes.c_uint32(var_id))
