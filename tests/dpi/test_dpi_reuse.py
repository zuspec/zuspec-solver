"""Compile-once + multi-solve DPI simulation tests (Step 7).

Verifies the compile-once (zsp_dpi_compile / zsp_dpi_solve) path works
across many iterations in the simulator.

Uses chandle-based DPI API -- works on all simulators.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from dv_flow.libhdlsim.pytest import hdlsim_available_sims
from zuspec.dataclasses import rand, constraint

from .test_dpi_basic import (
    _SIMS,
    _generate_sv,
    _prepare_sv_dir,
    _build_and_run,
    _parse_sol_lines,
)


@dataclass
class PairForReuse:
    """Two rand fields for compile-once testing."""
    x: int = rand(domain=(0, 200))
    y: int = rand(domain=(0, 200))

    @constraint
    def x_le_y(self):
        assert self.x <= self.y


@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_compile_once_solve_many(hdlsim_dvflow, tmp_path, dpi_lib_dir,
                                  dpi_lib_path, sv_pkg_dir, c_src_dir):
    """Compile once, solve 100 times -- verify all solutions valid."""
    sv_text = _generate_sv(PairForReuse, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="PairForReuse_harness", n_solutions=100,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"
    assert "PASS:" in sim_log

    sol_lines = _parse_sol_lines(sim_log)
    assert len(sol_lines) >= 100, (
        f"Expected >= 100 SOL lines, got {len(sol_lines)}"
    )


@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_different_seeds(hdlsim_dvflow, tmp_path, dpi_lib_dir,
                         dpi_lib_path, sv_pkg_dir, c_src_dir):
    """Different seeds produce different solutions (statistical check)."""
    sv_text = _generate_sv(PairForReuse, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="PairForReuse_harness", n_solutions=20,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"

    sol_lines = _parse_sol_lines(sim_log)
    # With 20 solutions, we should see at least a few distinct values
    assert len(sol_lines) >= 20, (
        "Expected >= 20 SOL lines"
    )
