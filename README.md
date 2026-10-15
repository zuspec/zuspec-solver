# zuspec-solver

Translates Zuspec IR solve problems for a constraint solver. Pure Python.

[zuspec-ir-core](https://pypi.org/project/zuspec-ir-core/) collects an
action's random fields and constraints into a `ScSolveProblem`.
`zuspec.solver.translate` walks that problem once and hands each solver
construct to a *sink*. The sink is the dv-solve builder, which produces a
relocatable [dv-solve](https://pypi.org/project/dv-solve/) problem blob, or
anything else with the same methods. So every backend that solves a Zuspec
model shares one encoding and one list of refused constructs.

```python
from zuspec.solver.translate import build_solve_blob, TranslateError

blob, writeback = build_solve_blob(problem)   # problem: ScSolveProblem
# writeback: object field slot -> solver var_id
```

A constraint the solver cannot take raises `TranslateError` with its source
location. It is never dropped.

Used by [zuspec-be-bc](https://pypi.org/project/zuspec-be-bc/), the backend
[pssc](https://pypi.org/project/pssc/) runs PSS on.

## License

Apache-2.0
