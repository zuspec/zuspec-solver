"""IRTranslator: converts a ConstraintSystem into a native SolveProblem.

The translator maps each Python solver Variable to a C variable ID and
walks each Constraint tree to build the equivalent ExprRef DAG.

Unsupported constructs raise ``TranslationError`` so callers can fall back
to the Python solver back-end.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from zuspec.ir.core.expr import BinOp, UnaryOp, BoolOp, CmpOp
from zuspec.dataclasses.solver.core.variable import Variable, VarKind
from zuspec.dataclasses.solver.core.constraints import (
    ConstantConstraint,
    VariableRefConstraint,
    BinaryOpConstraint,
    UnaryOpConstraint,
    BoolOpConstraint,
    CompareConstraint,
    CompareChainConstraint,
    InConstraint,
    BitSliceConstraint,
    ImplicationConstraint,
    UniqueConstraint,
)
from zuspec.dataclasses.solver.core.variable import Variable, VarKind
from zuspec.dataclasses.solver.core.domain import IntDomain

if TYPE_CHECKING:
    from zuspec.dataclasses.solver.core.constraint_system import ConstraintSystem
    from zuspec.dataclasses.solver.core.constraint import Constraint

from .builder import SolveProblemBuilder
from .problem import (
    EXPR_NULL,
    BIN_ADD, BIN_SUB, BIN_MUL, BIN_DIV, BIN_MOD,
    BIN_BAND, BIN_BOR, BIN_BXOR, BIN_LSHIFT, BIN_RSHIFT,
    BIN_EQ, BIN_NEQ, BIN_LT, BIN_LTE, BIN_GT, BIN_GTE,
    BIN_AND, BIN_OR,
    UN_NEG, UN_NOT, UN_INVERT,
)

# ------------------------------------------------------------------ #
# Op mappings                                                          #
# ------------------------------------------------------------------ #

_BINOP_MAP: Dict[BinOp, int] = {
    BinOp.Add:     BIN_ADD,
    BinOp.Sub:     BIN_SUB,
    BinOp.Mult:    BIN_MUL,
    BinOp.Div:     BIN_DIV,
    BinOp.Mod:     BIN_MOD,
    BinOp.FloorDiv: BIN_DIV,
    BinOp.BitAnd:  BIN_BAND,
    BinOp.BitOr:   BIN_BOR,
    BinOp.BitXor:  BIN_BXOR,
    BinOp.LShift:  BIN_LSHIFT,
    BinOp.RShift:  BIN_RSHIFT,
    BinOp.Eq:      BIN_EQ,
    BinOp.NotEq:   BIN_NEQ,
    BinOp.Lt:      BIN_LT,
    BinOp.LtE:     BIN_LTE,
    BinOp.Gt:      BIN_GT,
    BinOp.GtE:     BIN_GTE,
    BinOp.And:     BIN_AND,
    BinOp.Or:      BIN_OR,
}

_CMPOP_MAP: Dict[CmpOp, int] = {
    CmpOp.Eq:    BIN_EQ,
    CmpOp.NotEq: BIN_NEQ,
    CmpOp.Lt:    BIN_LT,
    CmpOp.LtE:   BIN_LTE,
    CmpOp.Gt:    BIN_GT,
    CmpOp.GtE:   BIN_GTE,
}

_UNARYOP_MAP: Dict[UnaryOp, int] = {
    UnaryOp.USub:   UN_NEG,
    UnaryOp.Not:    UN_NOT,
    UnaryOp.Invert: UN_INVERT,
}


class TranslationError(Exception):
    """Raised when an IR construct cannot be translated to the native solver."""


class IRTranslator:
    """Translates a ``ConstraintSystem`` into a native ``SolveProblem``.

    Usage::

        translator = IRTranslator()
        sp, var_id_map = translator.translate(system)
        with SolveCtx(sp) as ctx:
            rc = ctx.solve(seed=seed)
    """

    def translate(
        self,
        system: "ConstraintSystem",
        buf_size: int = 65536,
    ) -> Tuple["SolveProblemBuilder", Dict[str, int]]:
        """Translate *system* into a ``SolveProblem``.

        Returns:
            A ``(SolveProblem, var_name_to_id)`` pair.  The second element
            maps variable names to their 0-based C variable IDs.

        Raises:
            TranslationError: When a constraint or domain type cannot be
                translated to the native C representation.
        """
        sp = SolveProblemBuilder(block_size=4096)
        self._system = system
        self._next_tmp = 0
        # Assign deterministic IDs (sorted by name)
        sorted_names = sorted(system.variables.keys())
        var_id_map: Dict[str, int] = {name: idx for idx, name in enumerate(sorted_names)}

        # Declare variables
        for name, vid in var_id_map.items():
            var = system.variables[name]
            lo, hi, width, is_signed = self._domain_bounds(var)
            ref = sp.add_var(vid, width=width, is_signed=is_signed, lo=lo, hi=hi)
            if ref == EXPR_NULL:
                raise TranslationError(
                    f"Builder allocation failed for variable '{name}'"
                )

        # Add randc exclusion constraints (before compile)
        for name, var in system.variables.items():
            if var.kind == VarKind.RANDC and var.randc_state is not None:
                vid = var_id_map[name]
                vref = sp.expr_var(vid)
                for used_val in var.randc_state.used_values:
                    ne_ref = sp.expr_binary(BIN_NEQ, vref, sp.expr_const(used_val))
                    sp.add_constraint(ne_ref)

        # Translate constraints
        for constraint in system.constraints:
            self._add_constraint(sp, var_id_map, constraint)

        return sp, var_id_map

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _domain_bounds(self, var: Variable) -> Tuple[int, int, int, bool]:
        """Return (lo, hi, width, is_signed) from a Variable's domain."""
        from zuspec.dataclasses.solver.core.domain import IntDomain
        domain = var.domain
        if not isinstance(domain, IntDomain):
            raise TranslationError(
                f"Variable '{var.name}' has unsupported domain type "
                f"{type(domain).__name__}; only IntDomain is supported."
            )
        if not domain._intervals:
            raise TranslationError(
                f"Variable '{var.name}' has an empty domain."
            )
        lo = domain._intervals[0][0]
        hi = domain._intervals[-1][1]
        width = max(1, min(domain.width, 64))
        return lo, hi, width, domain.signed

    def _add_constraint(
        self,
        sp: SolveProblem,
        var_id_map: Dict[str, int],
        constraint: "Constraint",
    ) -> None:
        """Translate *constraint* and add it to *sp* (may add >1 constraint)."""
        # BoolOp(AND, ...) at top level — split into individual constraints
        if isinstance(constraint, BoolOpConstraint) and constraint.op == BoolOp.And:
            for sub in constraint.values:
                self._add_constraint(sp, var_id_map, sub)
            return

        # CompareChain at top level — split into pairwise comparisons
        if isinstance(constraint, CompareChainConstraint):
            exprs = [constraint.left] + list(constraint.comparators)
            for i, op in enumerate(constraint.ops):
                lhs_ref = self._translate_expr(sp, var_id_map, exprs[i])
                rhs_ref = self._translate_expr(sp, var_id_map, exprs[i + 1])
                cbin = _CMPOP_MAP.get(op)
                if cbin is None:
                    raise TranslationError(f"Unsupported CmpOp in chain: {op}")
                sp.add_constraint(sp.expr_binary(cbin, lhs_ref, rhs_ref))
            return

        # Pre-process BoolOp(Or): lift BinOp leaves inside comparisons
        # so the C compiler's _flatten_or sees var-const comparisons.
        if isinstance(constraint, BoolOpConstraint) and constraint.op == BoolOp.Or:
            rewritten_values = []
            changed = False
            for val in constraint.values:
                if isinstance(val, CompareConstraint) and isinstance(val.left, BinaryOpConstraint):
                    self._introduce_temp_for_binop(sp, var_id_map, val.left)
                    tmp_name = f"__ir_tmp_{self._next_tmp - 1}"
                    rewritten_values.append(CompareConstraint(
                        left=VariableRefConstraint(
                            variable=Variable(tmp_name, IntDomain([], 64, True))),
                        op=val.op,
                        right=val.right))
                    changed = True
                else:
                    rewritten_values.append(val)
            if changed:
                constraint = BoolOpConstraint(op=constraint.op, values=rewritten_values)

        # Rewrite patterns the C compiler can't handle:
        # 1. var == BinOp(const, op, var) → replace const with const-variable
        # 2. Compare(BinOp(...), op, X) → introduce temp var for the BinOp result
        if isinstance(constraint, CompareConstraint):
            # Lift const operands inside RHS BinOps: var == const * var → var == cvar * var
            if constraint.op == CmpOp.Eq and isinstance(constraint.right, BinaryOpConstraint):
                rhs = self._lift_const_operands(sp, var_id_map, constraint.right)
                constraint = CompareConstraint(left=constraint.left, op=constraint.op, right=rhs)

            # Lift BinOp on the left side: BinOp(a,b) op X → tmp op X, tmp == BinOp(a,b)
            if isinstance(constraint.left, BinaryOpConstraint):
                tmp_ref = self._introduce_temp_for_binop(sp, var_id_map, constraint.left)
                # Now emit: tmp_var op right
                rhs_ref = self._translate_expr(sp, var_id_map, constraint.right)
                cbin = _CMPOP_MAP.get(constraint.op)
                if cbin is None:
                    raise TranslationError(f"Unsupported CmpOp: {constraint.op}")
                sp.add_constraint(sp.expr_binary(cbin, tmp_ref, rhs_ref))
                return

        # ImplicationConstraint → ITE at constraint root
        # if(cond) then_constraint; [else else_constraint]
        if isinstance(constraint, ImplicationConstraint):
            cond_ref = self._translate_expr(sp, var_id_map, constraint.condition)
            then_ref = self._translate_expr(sp, var_id_map, constraint.then_constraint)
            if constraint.else_constraint is not None:
                else_ref = self._translate_expr(sp, var_id_map, constraint.else_constraint)
            else:
                # No else: use a trivially-true const (1)
                else_ref = sp.expr_const(1, 0)
            ite_ref = sp.expr_ite(cond_ref, then_ref, else_ref)
            sp.add_constraint(ite_ref)
            return

        # UniqueConstraint → AllDifferent (handled directly, not via _translate_expr)
        if isinstance(constraint, UniqueConstraint):
            vids = [var_id_map[v.name] for v in constraint.unique_variables
                    if v.name in var_id_map]
            if len(vids) >= 2:
                sp.add_all_different(vids)
            return

        ref = self._translate_expr(sp, var_id_map, constraint)
        if ref == EXPR_NULL:
            raise TranslationError("Constraint translated to EXPR_NULL (pool overflow?)")
        sp.add_constraint(ref)

    def _translate_expr(
        self,
        sp: SolveProblem,
        var_id_map: Dict[str, int],
        constraint: "Constraint",
    ) -> int:
        """Recursively translate a Constraint tree to an ExprRef."""

        if isinstance(constraint, ConstantConstraint):
            return sp.expr_const(constraint.value)

        if isinstance(constraint, VariableRefConstraint):
            vid = var_id_map.get(constraint.variable.name)
            if vid is None:
                raise TranslationError(
                    f"Unknown variable '{constraint.variable.name}' in constraint"
                )
            return sp.expr_var(vid)

        if isinstance(constraint, BinaryOpConstraint):
            lhs = self._translate_expr(sp, var_id_map, constraint.left)
            rhs = self._translate_expr(sp, var_id_map, constraint.right)
            cbin = _BINOP_MAP.get(constraint.op)
            if cbin is None:
                raise TranslationError(
                    f"Unsupported BinOp: {constraint.op}"
                )
            return sp.expr_binary(cbin, lhs, rhs)

        if isinstance(constraint, CompareConstraint):
            lhs = self._translate_expr(sp, var_id_map, constraint.left)
            rhs = self._translate_expr(sp, var_id_map, constraint.right)
            cbin = _CMPOP_MAP.get(constraint.op)
            if cbin is None:
                raise TranslationError(
                    f"Unsupported CmpOp: {constraint.op}"
                )
            return sp.expr_binary(cbin, lhs, rhs)

        if isinstance(constraint, UnaryOpConstraint):
            operand = self._translate_expr(sp, var_id_map, constraint.operand)
            cun = _UNARYOP_MAP.get(constraint.op)
            if cun is None:
                raise TranslationError(
                    f"Unsupported UnaryOp: {constraint.op}"
                )
            return sp.expr_unary(cun, operand)

        if isinstance(constraint, BoolOpConstraint):
            if not constraint.values:
                raise TranslationError("BoolOpConstraint with no values")
            # Fold into binary chain: ((c0 op c1) op c2) ...
            cbin = BIN_AND if constraint.op == BoolOp.And else BIN_OR
            result = self._translate_expr(sp, var_id_map, constraint.values[0])
            for val in constraint.values[1:]:
                rhs = self._translate_expr(sp, var_id_map, val)
                result = sp.expr_binary(cbin, result, rhs)
            return result

        if isinstance(constraint, InConstraint):
            vid = var_id_map.get(constraint.variable.name)
            if vid is None:
                raise TranslationError(
                    f"Unknown variable '{constraint.variable.name}' in InConstraint"
                )
            vref = sp.expr_var(vid)
            elem_refs = [sp.expr_const(v) for v in sorted(constraint.values)]
            return sp.expr_in_set(vref, elem_refs)

        if isinstance(constraint, ImplicationConstraint):
            cond_ref = self._translate_expr(sp, var_id_map, constraint.condition)
            then_ref = self._translate_expr(sp, var_id_map, constraint.then_constraint)
            if constraint.else_constraint is not None:
                else_ref = self._translate_expr(sp, var_id_map, constraint.else_constraint)
            else:
                # No else: implication is cond → then, i.e. ITE(cond, then, 1)
                else_ref = sp.expr_const(1)
            return sp.expr_ite(cond_ref, then_ref, else_ref)

        if isinstance(constraint, BitSliceConstraint):
            vid = var_id_map.get(constraint.variable.name)
            if vid is None:
                raise TranslationError(
                    f"Unknown variable '{constraint.variable.name}' in BitSliceConstraint"
                )
            vref = sp.expr_var(vid)
            return sp.expr_extract(vref, constraint.upper, constraint.lower)

        if isinstance(constraint, CompareChainConstraint):
            # When nested inside another expr — AND of pairwise comparisons
            exprs = [constraint.left] + list(constraint.comparators)
            parts: List[int] = []
            for i, op in enumerate(constraint.ops):
                lhs = self._translate_expr(sp, var_id_map, exprs[i])
                rhs = self._translate_expr(sp, var_id_map, exprs[i + 1])
                cbin = _CMPOP_MAP.get(op)
                if cbin is None:
                    raise TranslationError(f"Unsupported CmpOp in chain: {op}")
                parts.append(sp.expr_binary(cbin, lhs, rhs))
            result = parts[0]
            for part in parts[1:]:
                result = sp.expr_binary(BIN_AND, result, part)
            return result

        if isinstance(constraint, UniqueConstraint):
            raise TranslationError(
                "UniqueConstraint should be handled in _add_constraint, not _translate_expr"
            )

        raise TranslationError(
            f"Unsupported constraint type: {type(constraint).__name__}"
        )

    def _lift_const_operands(self, sp, var_id_map, binop):
        """Replace ConstantConstraint operands in a BinaryOpConstraint with
        const-variables (domain=[c,c]) so the C compiler sees var op var."""
        """Replace ConstantConstraint operands in a BinaryOpConstraint with
        const-variables (domain=[c,c]) so the C compiler sees var op var."""
        left = binop.left
        right = binop.right
        changed = False

        if isinstance(left, ConstantConstraint):
            cv = left.value
            tmp_name = f"__const_{cv}_{self._next_tmp}"
            self._next_tmp += 1
            tmp_vid = len(var_id_map)
            var_id_map[tmp_name] = tmp_vid
            sp.add_var(tmp_vid, width=32, is_signed=0, lo=cv, hi=cv)
            left = VariableRefConstraint(
                variable=Variable(tmp_name, IntDomain([(cv, cv)], 32, False)))
            changed = True

        if isinstance(right, ConstantConstraint):
            cv = right.value
            tmp_name = f"__const_{cv}_{self._next_tmp}"
            self._next_tmp += 1
            tmp_vid = len(var_id_map)
            var_id_map[tmp_name] = tmp_vid
            sp.add_var(tmp_vid, width=32, is_signed=0, lo=cv, hi=cv)
            right = VariableRefConstraint(
                variable=Variable(tmp_name, IntDomain([(cv, cv)], 32, False)))
            changed = True

        if changed:
            return type(binop)(left=left, op=binop.op, right=right)
        return binop
    def _introduce_temp_for_binop(self, sp, var_id_map, binop):
        """Recursively decompose a BinOp tree into tmp == var op var chains.

        Returns the ExprRef of the final temp variable.
        For a simple BinOp(var_a, ADD, var_b), emits one constraint:
            tmp == var_a + var_b
        For a nested chain ((a+b)+c), emits:
            tmp1 == a + b
            tmp2 == tmp1 + c
        and returns the ExprRef for tmp2.
        """
        # Recursively resolve operands
        left_ref = (self._introduce_temp_for_binop(sp, var_id_map, binop.left)
                    if isinstance(binop.left, BinaryOpConstraint)
                    else self._translate_expr(sp, var_id_map, binop.left))
        right_ref = (self._introduce_temp_for_binop(sp, var_id_map, binop.right)
                     if isinstance(binop.right, BinaryOpConstraint)
                     else self._translate_expr(sp, var_id_map, binop.right))

        # Create temp variable with wide domain
        tmp_name = f"__ir_tmp_{self._next_tmp}"
        self._next_tmp += 1
        tmp_vid = len(var_id_map)
        var_id_map[tmp_name] = tmp_vid
        # Use signed 64-bit domain to avoid overflow issues
        sp.add_var(tmp_vid, width=64, is_signed=1, lo=-(1 << 62), hi=(1 << 62) - 1)

        tmp_ref = sp.expr_var(tmp_vid)

        # Emit: tmp == left binop right
        binop_code = _BINOP_MAP.get(binop.op)
        if binop_code is None:
            raise TranslationError(f"Unsupported BinOp in temp introduction: {binop.op}")
        sp.add_constraint(sp.expr_binary(BIN_EQ, tmp_ref,
                                          sp.expr_binary(binop_code, left_ref, right_ref)))
        return tmp_ref
