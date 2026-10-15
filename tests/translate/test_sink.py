"""The walk writes only through the sink protocol.

A recording sink stands in for dv-solve's builder: if the walk reached past the
protocol (a builder-only method, a blob offset it computed itself), a sink with
another output -- C source for be-sw -- could not take its place.
"""

from zuspec.ir.core import expr as E
from zuspec.ir.core import constraint as C
from zuspec.ir.core import scenario as SC

from zuspec.solver.translate import Sink, TranslateError, translate


class Recorder:
    """Every call as a tuple; an expression handle is the call's index."""

    def __init__(self):
        self.calls = []

    def _rec(self, *call):
        self.calls.append(call)
        return len(self.calls) - 1

    def __getattr__(self, name):
        if name.startswith("_") or not hasattr(Sink, name):
            raise AttributeError(name)
        return lambda *a, **k: self._rec(name, *a, *sorted(k.items()))


def _field(i):
    return E.ExprRefField(base=E.TypeExprRefSelf(), index=i)


def _problem(*cons, nvars=2):
    vs = [SC.ScSolveVar(name="f%d" % i, var_id=i, slot=i + 3, width=8)
          for i in range(nvars)]
    return SC.ScSolveProblem(vars=vs, constraints=list(cons))


def test_walk_drives_any_sink():
    # f0 < f1, where the fields sit at slots 3 and 4: the walk maps each slot
    # to its var_id and says so through the sink alone.
    con = C.ConstraintExpr(expr=E.ExprBin(lhs=_field(3), op=E.BinOp.Lt, rhs=_field(4)))
    rec = Recorder()
    wb = translate(_problem(con), rec)
    assert wb == {3: 0, 4: 1}
    assert [c[0] for c in rec.calls] == [
        "add_var", "add_var", "expr_var", "expr_var", "expr_binary", "add_constraint"]
    assert rec.calls[2] == ("expr_var", 0) and rec.calls[3] == ("expr_var", 1)
    assert rec.calls[4] == ("expr_binary", 12, 2, 3)        # BIN_LT on the two handles
    assert rec.calls[5] == ("add_constraint", 4)


def test_refusal_is_raised_not_dropped():
    # A field that is not a rand var of the problem: refused, with nothing
    # added to the sink for it.
    con = C.ConstraintExpr(expr=E.ExprBin(lhs=_field(9), op=E.BinOp.Eq,
                                          rhs=E.ExprConstant(value=1)))
    rec = Recorder()
    try:
        translate(_problem(con), rec)
    except TranslateError as e:
        assert "slot 9" in str(e)
    else:
        raise AssertionError("an unknown slot was accepted")
    assert all(c[0] != "add_constraint" for c in rec.calls)
