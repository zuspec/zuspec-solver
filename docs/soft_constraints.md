# Soft Constraints

Soft (relaxable) constraints allow the solver to drop lower-priority
requirements when they conflict with hard constraints.

## API

### Problem Construction

```c
// Add a soft constraint with a priority.
// priority: 0 = highest (kept first), larger = relaxed first.
ExprRef problem_add_soft_constraint(SolveProblem *sp, ExprRef root,
                                    uint32_t priority);

// Builder equivalent:
ExprRef builder_add_soft_constraint(SolveProblemBuilder *b, ExprRef root,
                                    uint32_t priority);
```

### After Solve

```c
// Returns 1 if the soft constraint was satisfied, 0 if relaxed, -1 on error.
int solver_soft_active(const SolveCtx *ctx, uint32_t assumption_idx);
```

Note: assumption indices correspond to the order soft constraints appear
in the linked list (reverse of addition order due to prepend semantics).

## Semantics

1. Each soft constraint generates a boolean assumption variable pinned to 1.
2. The constraint body is compiled with the assumption as a guard.
3. On conflict, the solver relaxes the lowest-priority active assumption
   (highest priority number) by pinning it to 0.
4. The solver resets and retries with the relaxed assumption.
5. If hard constraints alone are UNSAT, the solver returns SOLVE_UNSAT
   (soft relaxation cannot help).

## Priority Model

- Priority 0 is the highest (most important to keep).
- Higher numeric values are relaxed first.
- When multiple soft constraints conflict, they are relaxed one at a time
  in decreasing priority order until a solution is found.

## Example

```c
// Hard: x >= 10
// Soft (pri 0): x == 5   -- will be relaxed
// Soft (pri 1): y == 7   -- compatible, will be kept

problem_add_soft_constraint(sp, eq_x_5, 0);
problem_add_soft_constraint(sp, eq_y_7, 1);

solver_solve(ctx, NULL);
// solver_soft_active(ctx, 0) == 0  (x==5 relaxed)
// solver_soft_active(ctx, 1) == 1  (y==7 kept)
```

## Diagnosing Relaxed Soft Constraints

When `solver_solve()` relaxes soft constraints, use
`contra_explain_soft()` to understand why:

```c
#include "zsp_contradiction.h"

SolveResult res = solver_solve(ctx, &opts);
if (res == SOLVE_OK) {
    /* Check which softs were relaxed */
    for (uint32_t i = 0; i < n_softs; i++) {
        if (!solver_soft_active(ctx, i))
            printf("Soft %u was relaxed\n", i);
    }

    /* Get detailed diagnostics */
    ContraSoftDiagResult diag;
    contra_explain_soft(ctx, sp, NULL, &diag);

    for (uint32_t i = 0; i < diag.n_entries; i++) {
        ContraSoftDiagEntry *e = &diag.entries[i];
        printf("Soft %u (priority %u) relaxed:\n",
               e->soft_constraint_id, e->soft_priority);
        printf("  Conflicts with %u hard constraints\n",
               e->n_conflict_hard);
        if (e->proof_text)
            printf("  %s\n", e->proof_text);
        if (e->n_hard_relax > 0)
            printf("  %u relaxation suggestions available\n",
                   e->n_hard_relax);
        if (e->n_alternatives > 0)
            printf("  %u alternative softs could substitute\n",
                   e->n_alternatives);
    }

    contra_soft_diag_free(&diag);
}
```

The diagnostic result includes:
- **Conflict hard IDs**: which hard constraints forced the relaxation.
- **Proof text**: human-readable explanation of the conflict.
- **Relaxation suggestions**: minimum changes to hard constraints that
  would allow the soft to be satisfied.
- **Alternative softs**: other relaxed softs that could substitute.
