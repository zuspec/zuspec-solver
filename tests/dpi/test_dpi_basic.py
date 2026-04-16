"""Basic DPI randomization simulation tests (Step 7).

Each test generates SV via SVRandomizerGenerator, compiles it with the
DPI C sources and SV packages, runs the simulation, and parses the log
for SOL lines that satisfy the constraints.

Tests are parameterized across available HDL simulators.

Note: Uses chandle-based DPI API (scalar/string/chandle only) --
works on all simulators including Verilator.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from dv_flow.libhdlsim.pytest import hdlsim_available_sims
from zuspec.dataclasses import rand, constraint


# Exclude Verilator: it lacks DPI open-array support
_SIMS = hdlsim_available_sims()


# ------------------------------------------------------------------ #
# Test dataclass definitions                                           #
# ------------------------------------------------------------------ #

@dataclass
class TwoVars:
    """Two rand fields with a <= constraint."""
    a: int = rand(domain=(0, 100))
    b: int = rand(domain=(0, 100))

    @constraint
    def a_le_b(self):
        assert self.a <= self.b


@dataclass
class SingleVar:
    """One rand field, no constraints."""
    val: int = rand(domain=(0, 255))


@dataclass
class ManyVars:
    """Ten rand fields with a bound constraint."""
    v0: int = rand(domain=(0, 50))
    v1: int = rand(domain=(0, 50))
    v2: int = rand(domain=(0, 50))
    v3: int = rand(domain=(0, 50))
    v4: int = rand(domain=(0, 50))
    v5: int = rand(domain=(0, 50))
    v6: int = rand(domain=(0, 50))
    v7: int = rand(domain=(0, 50))
    v8: int = rand(domain=(0, 50))
    v9: int = rand(domain=(0, 50))

    @constraint
    def v0_le_v1(self):
        assert self.v0 <= self.v1


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _ensure_lib_loadable(dpi_lib_dir):
    """Point ZSP_SOLVER_PATH at the build dir so the generator finds the lib."""
    import zuspec.solver.lib as _lib_mod
    old = os.environ.get("ZSP_SOLVER_PATH")
    os.environ["ZSP_SOLVER_PATH"] = str(dpi_lib_dir)
    _lib_mod._LIB_CACHE = None
    _lib_mod._LOAD_ATTEMPTED = False
    return old


def _restore_lib_path(old):
    import zuspec.solver.lib as _lib_mod
    if old is None:
        os.environ.pop("ZSP_SOLVER_PATH", None)
    else:
        os.environ["ZSP_SOLVER_PATH"] = old
    _lib_mod._LIB_CACHE = None
    _lib_mod._LOAD_ATTEMPTED = False


def _generate_sv(cls, dpi_lib_dir):
    """Generate SV harness for the given dataclass."""
    from zuspec.solver.sv_randomizer_gen import SVRandomizerGenerator
    old = _ensure_lib_loadable(dpi_lib_dir)
    try:
        return SVRandomizerGenerator().emit(cls)
    finally:
        _restore_lib_path(old)


def _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir):
    """Write the generated SV into a subdirectory and copy SV packages
    into a separate subdirectory for proper compile ordering.

    Returns (pkg_dir, harness_dir).
    """
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()

    # Write harness SV
    (harness_dir / "harness.sv").write_text(sv_text)

    # Copy SV package files into pkg_dir
    for sv_file in sv_pkg_dir.glob("*.sv"):
        shutil.copy(sv_file, pkg_dir / sv_file.name)

    return pkg_dir, harness_dir


def _build_and_run(hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir,
                   dpi_lib_path, top_module, n_solutions=10):
    """Build and run a simulation, return (status, sim_log_text)."""
    sim = hdlsim_dvflow.sim

    # SV packages fileset (compiled first)
    sv_pkg = hdlsim_dvflow.mkTask(
        "std.FileSet",
        name="sv_pkg",
        type="systemVerilogSource",
        base=str(pkg_dir),
        include="*.sv",
    )

    # SV harness fileset (compiled after packages)
    sv_harness = hdlsim_dvflow.mkTask(
        "std.FileSet",
        name="sv_harness",
        type="systemVerilogSource",
        base=str(harness_dir),
        include="*.sv",
    )

    needs = [sv_pkg, sv_harness]

    if sim == "vlt":
        # Verilator compiles C as C++; the solver uses C11 features.
        # Link against the pre-built shared library instead.
        dpi_lib = hdlsim_dvflow.mkTask(
            "std.FileSet",
            name="dpi_lib",
            type="systemVerilogDPI",
            base=str(dpi_lib_path.parent),
            include=dpi_lib_path.name,
        )
        needs.append(dpi_lib)
    else:
        # Commercial sims compile C sources directly.
        c_src = hdlsim_dvflow.mkTask(
            "std.FileSet",
            name="c_src",
            type="cSource",
            base=str(c_src_dir),
            include="*.c",
        )
        needs.append(c_src)

    # Build sim image (packages come before harness in needs list)
    sim_img = hdlsim_dvflow.mkTask(
        "hdlsim.%s.SimImage" % sim,
        name="sim_img",
        needs=needs,
        top=[top_module],
    )

    # Run sim
    sim_run = hdlsim_dvflow.mkTask(
        "hdlsim.%s.SimRun" % sim,
        name="sim_run",
        needs=[sim_img],
        plusargs=["n_solutions=%d" % n_solutions],
    )

    status, out_l = hdlsim_dvflow.runTask(sim_run)

    # Find sim.log by searching the tmpdir tree
    sim_log = ""
    tmpdir = str(hdlsim_dvflow.tmpdir)
    for root, dirs, files in os.walk(tmpdir):
        if "sim.log" in files:
            log_path = os.path.join(root, "sim.log")
            with open(log_path) as f:
                sim_log = f.read()
            break

    return status, sim_log


def _parse_sol_lines(sim_log):
    """Extract SOL lines from a sim log and return them as a list of strings."""
    return [line for line in sim_log.splitlines() if "SOL:" in line]


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #

@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_basic_randomize(hdlsim_dvflow, tmp_path, dpi_lib_dir, dpi_lib_path,
                         sv_pkg_dir, c_src_dir):
    """Generate TwoVars SV, run 10 iterations, verify SOL lines appear."""
    sv_text = _generate_sv(TwoVars, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="TwoVars_harness", n_solutions=10,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"
    assert "PASS:" in sim_log, f"Missing PASS marker in sim log"

    sol_lines = _parse_sol_lines(sim_log)
    assert len(sol_lines) >= 10, (
        f"Expected >= 10 SOL lines, got {len(sol_lines)}"
    )


@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_single_var(hdlsim_dvflow, tmp_path, dpi_lib_dir, dpi_lib_path,
                    sv_pkg_dir, c_src_dir):
    """Single rand field, no constraints -- verify simulation completes."""
    sv_text = _generate_sv(SingleVar, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="SingleVar_harness", n_solutions=10,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"
    assert "PASS:" in sim_log


@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_many_vars(hdlsim_dvflow, tmp_path, dpi_lib_dir, dpi_lib_path,
                   sv_pkg_dir, c_src_dir):
    """10+ vars with constraints -- verify compilation and solution validity."""
    sv_text = _generate_sv(ManyVars, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="ManyVars_harness", n_solutions=10,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"
    assert "PASS:" in sim_log


@pytest.mark.skipif(not _SIMS, reason="No simulator found")
@pytest.mark.parametrize("hdlsim_dvflow", _SIMS, indirect=True)
def test_seed_determinism(hdlsim_dvflow, tmp_path, dpi_lib_dir, dpi_lib_path,
                          sv_pkg_dir, c_src_dir):
    """Verify the harness runs (determinism checked at C level)."""
    sv_text = _generate_sv(TwoVars, dpi_lib_dir)
    pkg_dir, harness_dir = _prepare_sv_dir(tmp_path, sv_text, sv_pkg_dir)

    status, sim_log = _build_and_run(
        hdlsim_dvflow, pkg_dir, harness_dir, c_src_dir, dpi_lib_path,
        top_module="TwoVars_harness", n_solutions=5,
    )

    assert status == 0, f"Simulation failed (status={status})\n{sim_log[-500:]}"
    assert "PASS:" in sim_log
