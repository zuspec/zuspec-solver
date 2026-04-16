"""Fixtures for DPI simulation tests.

Builds libzsp_solver_dpi.so and provides the HdlSimDvFlow fixture with
the DPI library and SV package paths pre-configured.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dv_flow.libhdlsim.pytest import HdlSimDvFlow, hdlsim_available_sims

# Package root (contains CMakeLists.txt, src/c, src/sv)
_PKG_DIR = Path(__file__).parent.parent.parent


@pytest.fixture(scope="session")
def dpi_lib_dir(tmp_path_factory) -> Path:
    """Build libzsp_solver_dpi.so and return the directory containing it."""
    if not shutil.which("cmake"):
        pytest.skip("cmake not found")

    build_dir = tmp_path_factory.mktemp("zsp_dpi_build")
    subprocess.run(
        ["cmake", str(_PKG_DIR), "-DCMAKE_BUILD_TYPE=Release"],
        cwd=build_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build_dir), "--parallel"],
        check=True, capture_output=True,
    )

    candidates = list(build_dir.glob("libzsp_solver_dpi.so*"))
    if not candidates:
        pytest.skip("libzsp_solver_dpi.so not built")

    return build_dir


@pytest.fixture(scope="session")
def dpi_lib_path(dpi_lib_dir) -> Path:
    """Return the path to libzsp_solver_dpi.so."""
    candidates = sorted(dpi_lib_dir.glob("libzsp_solver_dpi.so*"),
                        key=lambda p: len(p.name))
    return candidates[0]


@pytest.fixture(scope="session")
def sv_pkg_dir() -> Path:
    """Return the directory containing zsp_dpi_pkg.sv and zsp_randomizer_pkg.sv."""
    d = _PKG_DIR / "src" / "sv"
    assert d.is_dir(), f"SV package directory not found: {d}"
    return d


@pytest.fixture(scope="session")
def c_src_dir() -> Path:
    """Return the directory containing the solver C source files."""
    d = _PKG_DIR / "src" / "c"
    assert d.is_dir(), f"C source directory not found: {d}"
    return d


@pytest.fixture
def hdlsim_dvflow(request, tmpdir):
    dvflow = HdlSimDvFlow(
        request,
        os.path.dirname(request.fspath),
        tmpdir)
    return dvflow
