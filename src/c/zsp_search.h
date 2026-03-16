#ifndef ZSP_SEARCH_H
#define ZSP_SEARCH_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* forward declaration */
typedef struct SolveCtx SolveCtx;

/* ------------------------------------------------------------------ */
/* SolveResult                                                         */
/* ------------------------------------------------------------------ */
typedef enum {
    SOLVE_OK      = 0,   /* satisfying assignment found               */
    SOLVE_UNSAT   = 1,   /* problem is unsatisfiable                  */
    SOLVE_TIMEOUT = 2,   /* conflict/restart budget exhausted         */
} SolveResult;

/* ------------------------------------------------------------------ */
/* DecisionRecord                                                      */
/*                                                                     */
/* One entry per active decision level.  decisions[L] records what    */
/* was decided when entering level L+1.                               */
/* ------------------------------------------------------------------ */
typedef struct {
    uint32_t var_id;        /* variable assigned at this decision      */
    int64_t  tried_value;   /* value tried (used on backtrack)         */
} DecisionRecord;

/* ------------------------------------------------------------------ */
/* SolveOpts — tuning knobs                                           */
/*                                                                     */
/* Pass NULL for all-default behaviour.                               */
/* ------------------------------------------------------------------ */
typedef struct {
    uint64_t seed;              /* RNG seed (0 = use ctx->rng_state)     */
    uint32_t max_conflicts;     /* max conflicts per restart (0=unlimited)*/
    uint32_t max_restarts;      /* max total restarts (0=unlimited)       */
    uint8_t  use_phase_save;    /* 1 = remember last assigned value       */
    uint8_t  _pad[3];
} SolveOpts;

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

/**
 * Run the search until a satisfying assignment is found, the problem is
 * proved unsatisfiable, or the budget is exhausted.
 *
 * Requires that solver_compile() has already been called on ctx.
 *
 * @return SOLVE_OK, SOLVE_UNSAT, or SOLVE_TIMEOUT.
 */
SolveResult solver_solve(SolveCtx *ctx, const SolveOpts *opts);

/**
 * Return the assigned value of variable var_id after a successful solve.
 *
 * Valid only after solver_solve() returns SOLVE_OK.
 * Returns the lower bound (== upper bound if fully assigned).
 */
int64_t solver_get_value(const SolveCtx *ctx, uint32_t var_id);

#ifdef __cplusplus
}
#endif

#endif /* ZSP_SEARCH_H */
