"""Unit tests for stream joint solve (Phase B2).

Tests:
- Basic joint solve with shared stream fields
- Domain intersection for shared fields
- Private (non-shared) fields on each side
- UNSAT joint solve
- Stream inference with ICL
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from zuspec.solver.stream_solver import (
    ConstraintSystemSpec,
    MergedSystem,
    StreamJointSolveResult,
    StreamSolver,
    build_stream_joint_system,
)
from zuspec.solver.icl import (
    FlowFieldDescriptor,
    build_icl_table,
)
from zuspec.solver.structural_solver import InferenceFeasibilityError


class TestBuildStreamJointSystem:

    def test_shared_fields_unified(self):
        """Shared stream fields get one variable ID."""
        producer = ConstraintSystemSpec(
            variables={"payload": (0, 0xFFFF, 16), "tag": (0, 15, 4),
                       "internal": (0, 255, 8)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"payload": (0, 0xFFFF, 16), "tag": (0, 15, 4),
                       "mode": (0, 3, 2)},
            constraints=[],
        )

        merged = build_stream_joint_system(
            producer, consumer, ["payload", "tag"]
        )

        # 2 shared + 1 producer-only + 1 consumer-only = 4 variables
        assert len(merged.variables) == 4
        assert len(merged.shared_field_names) == 2

        # Shared fields have the same ID for both sides
        assert merged.var_id_map["producer.payload"] == merged.var_id_map["consumer.payload"]
        assert merged.var_id_map["producer.tag"] == merged.var_id_map["consumer.tag"]

    def test_domain_intersection(self):
        """Shared field domain is the intersection of both sides."""
        producer = ConstraintSystemSpec(
            variables={"tag": (0, 10, 4)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"tag": (5, 15, 4)},
            constraints=[],
        )

        merged = build_stream_joint_system(producer, consumer, ["tag"])

        # Domain should be [5, 10] (intersection)
        tag_var = merged.variables[0]
        assert tag_var.name == "tag"
        assert tag_var.lo == 5
        assert tag_var.hi == 10

    def test_private_fields_separate(self):
        """Non-shared fields get separate IDs with namespace prefixes."""
        producer = ConstraintSystemSpec(
            variables={"shared_f": (0, 100, 8), "p_field": (0, 50, 8)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"shared_f": (0, 100, 8), "c_field": (0, 30, 8)},
            constraints=[],
        )

        merged = build_stream_joint_system(producer, consumer, ["shared_f"])

        assert "producer.p_field" in merged.var_id_map
        assert "consumer.c_field" in merged.var_id_map
        assert merged.var_id_map["producer.p_field"] != merged.var_id_map["consumer.c_field"]

    def test_constraint_remapping(self):
        """Constraints are remapped to the unified ID space."""
        producer = ConstraintSystemSpec(
            variables={"tag": (0, 15, 4)},
            constraints=[{"variables": ["tag"], "op": "lt", "bound": 10}],
        )
        consumer = ConstraintSystemSpec(
            variables={"tag": (0, 15, 4)},
            constraints=[{"variables": ["tag"], "op": "gt", "bound": 2}],
        )

        merged = build_stream_joint_system(producer, consumer, ["tag"])

        # Both constraints should reference the same unified tag ID
        assert len(merged.producer_constraints) == 1
        assert len(merged.consumer_constraints) == 1

        p_remap = merged.producer_constraints[0].remapped_var_ids
        c_remap = merged.consumer_constraints[0].remapped_var_ids
        assert p_remap["tag"] == c_remap["tag"]


class TestStreamSolver:

    def test_joint_solve_basic(self):
        """Two actions share 2 stream fields; joint solve satisfies both."""
        producer = ConstraintSystemSpec(
            variables={"payload": (0, 255, 8), "tag": (0, 7, 3)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"payload": (0, 255, 8), "tag": (0, 7, 3)},
            constraints=[],
        )

        def mock_solve(merged_system):
            result = {}
            for var in merged_system.variables:
                result[var.name] = (var.lo + var.hi) // 2
            return result

        solver = StreamSolver(solve_fn=mock_solve)
        result = solver.joint_solve(producer, consumer, ["payload", "tag"])

        assert "payload" in result.shared_values
        assert "tag" in result.shared_values

    def test_joint_solve_unsat(self):
        """Contradictory constraints on shared field; returns UNSAT."""
        producer = ConstraintSystemSpec(
            variables={"tag": (10, 20, 8)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"tag": (30, 40, 8)},
            constraints=[],
        )

        solver = StreamSolver(solve_fn=lambda m: None)

        # Domain intersection is empty: [30, 20] which is invalid
        with pytest.raises(InferenceFeasibilityError, match="Empty domain"):
            solver.joint_solve(producer, consumer, ["tag"])

    def test_joint_solve_with_private_fields(self):
        """Each side has non-shared rand fields; all solved correctly."""
        producer = ConstraintSystemSpec(
            variables={"shared": (0, 100, 8), "p_internal": (0, 50, 8)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"shared": (0, 100, 8), "c_mode": (0, 3, 2)},
            constraints=[],
        )

        def mock_solve(merged_system):
            return {v.name: v.lo for v in merged_system.variables}

        solver = StreamSolver(solve_fn=mock_solve)
        result = solver.joint_solve(producer, consumer, ["shared"])

        assert "shared" in result.shared_values
        assert "p_internal" in result.producer_values
        assert "c_mode" in result.consumer_values

    def test_stream_inference_with_icl(self):
        """Unbound stream input; infer partner from ICL; joint solve."""
        descriptors = [
            FlowFieldDescriptor("tx_action", "output.stream", "stream_t",
                                "output", "stream"),
            FlowFieldDescriptor("rx_action", "input.stream", "stream_t",
                                "input", "stream"),
        ]
        icl_table = build_icl_table(descriptors)

        consumer = ConstraintSystemSpec(
            variables={"data": (0, 255, 8)},
            constraints=[],
        )

        def get_producer_system(action_type):
            return ConstraintSystemSpec(
                variables={"data": (0, 255, 8)},
                constraints=[],
            )

        def mock_solve(merged_system):
            return {v.name: 42 for v in merged_system.variables}

        solver = StreamSolver(icl_table=icl_table, solve_fn=mock_solve)
        producer_type, result = solver.infer_and_solve(
            "rx_action", "input.stream",
            consumer, ["data"], get_producer_system,
        )

        assert producer_type == "tx_action"
        assert "data" in result.shared_values

    def test_no_solve_fn_raises(self):
        """No solve function configured raises error."""
        producer = ConstraintSystemSpec(
            variables={"tag": (0, 10, 4)},
            constraints=[],
        )
        consumer = ConstraintSystemSpec(
            variables={"tag": (0, 10, 4)},
            constraints=[],
        )
        solver = StreamSolver(solve_fn=None)
        with pytest.raises(InferenceFeasibilityError, match="No solve function"):
            solver.joint_solve(producer, consumer, ["tag"])
