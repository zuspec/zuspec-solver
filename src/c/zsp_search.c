#include <stdint.h>
#include <limits.h>
#include <string.h>
#include "zsp_search.h"
#include "zsp_ctx.h"
#include "zsp_propagator.h"
#include "zsp_trail.h"
#include "zsp_shave.h"

/* ------------------------------------------------------------------ */
/* Luby sequence                                                       */
/* ------------------------------------------------------------------ */

/* Returns the n-th element (1-indexed) of the Luby sequence. */
static uint32_t _luby(uint32_t n) {
    /* Find k such that 2^(k-1) <= n < 2^k */
    uint32_t p = 1;
    uint32_t k = 1;
    while (p < n + 1) { p *= 2; k++; }
    if (p == n + 1) return p / 2;
    return _luby(n - (p / 2) + 1);
}

/* ------------------------------------------------------------------ */
/* Fast xorshift64 RNG                                                 */
/* ------------------------------------------------------------------ */

static uint64_t _rand64(SolveCtx *ctx) {
    uint64_t x = ctx->rng_state;
    if (x == 0) x = 0xDEADBEEF12345678ULL;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    ctx->rng_state = x;
    return x;
}

/* Return a random integer in [lo, hi] (inclusive), 64-bit range. */
static int64_t _rand_range64(SolveCtx *ctx, int64_t lo, int64_t hi) {
    uint64_t span = (uint64_t)(hi - lo) + 1u;
    return lo + (int64_t)(_rand64(ctx) % span);
}

/* ------------------------------------------------------------------ */
/* Variable selection — MRV (minimum remaining values)               */
/*                                                                     */
/* Returns the var_id of the unassigned variable with the smallest    */
/* domain, or EXPR_NULL if all variables are assigned.                */
/* ------------------------------------------------------------------ */

static uint32_t _select_unassigned(SolveCtx *ctx) {
    uint32_t best      = EXPR_NULL;
    int64_t  best_dom  = INT64_MAX;

    for (uint32_t i = 0; i < ctx->n_vars; i++) {
        Variable *v = &ctx->vars[i];
        int64_t lo = var_lo64(ctx, v);
        int64_t hi = var_hi64(ctx, v);
        if (lo == hi) continue;  /* singleton → assigned */
        int64_t dom = hi - lo;
        if (dom < best_dom) {
            best_dom = dom;
            best     = i;
            if (dom == 1) break;  /* binary — can't improve */
        }
    }
    return best;
}

/* ------------------------------------------------------------------ */
/* Value selection                                                     */
/* ------------------------------------------------------------------ */

static int64_t _pick_value(SolveCtx *ctx, uint32_t var_id,
                            const SolveOpts *opts) {
    int64_t lo = var_lo64(ctx, &ctx->vars[var_id]);
    int64_t hi = var_hi64(ctx, &ctx->vars[var_id]);

    /* Phase saving: try the last saved value if still in domain. */
    if (opts && opts->use_phase_save && ctx->phase_save) {
        int64_t ps = ctx->phase_save[var_id];
        if (ps >= lo && ps <= hi) return ps;
    }

    return _rand_range64(ctx, lo, hi);
}

/* ------------------------------------------------------------------ */
/* solver_solve                                                        */
/* ------------------------------------------------------------------ */

SolveResult solver_solve(SolveCtx *ctx, const SolveOpts *opts) {
    /* Seed or preserve RNG. */
    if (opts && opts->seed != 0) ctx->rng_state = opts->seed;
    if (ctx->rng_state == 0)     ctx->rng_state = 0xDEADBEEF12345678ULL;

    /* Allocate phase_save array on first call (lazily from static pool). */
    if (opts && opts->use_phase_save && !ctx->phase_save && ctx->n_vars > 0) {
        uint32_t ps_ref = zsp_pool_alloc(&ctx->pool,
                                          ctx->n_vars * (uint32_t)sizeof(int64_t),
                                          (uint32_t)_Alignof(int64_t));
        if (ps_ref != EXPR_NULL) {
            ctx->phase_save = (int64_t *)zsp_pool_ptr(&ctx->pool, ps_ref);
            for (uint32_t i = 0; i < ctx->n_vars; i++)
                ctx->phase_save[i] = var_lo64(ctx, &ctx->vars[i]);
        }
    }

    /* Default restart parameters: 100 conflicts per restart,
     * 10000 max restarts.  Caller can override via opts. */
    uint32_t max_conflicts  = (opts && opts->max_conflicts > 0)
                              ? opts->max_conflicts : 100;
    uint32_t max_restarts   = (opts && opts->max_restarts > 0)
                              ? opts->max_restarts  : 10000;
    uint32_t restart_count  = 0;
    uint32_t local_conflicts = 0;
    uint32_t luby_idx       = 1;  /* 1-indexed Luby sequence */
    uint32_t luby_limit     = (max_conflicts > 0)
                              ? _luby(luby_idx) * max_conflicts
                              : UINT32_MAX;

    /* Level-0 BCP */
    if (solver_propagate(ctx) == PROP_CONFLICT) return SOLVE_UNSAT;

    /* Check for domains that became empty before search (e.g. from
     * conflicting bounds imposed externally before solver_solve). */
    for (uint32_t i = 0; i < ctx->n_vars; i++) {
        if (var_lo64(ctx, &ctx->vars[i]) > var_hi64(ctx, &ctx->vars[i]))
            return SOLVE_UNSAT;
    }

    /* Pre-search bounds shaving (L2): tighten domains beyond what
     * individual propagator fixed-point can achieve. */
    uint32_t max_si = opts ? opts->max_shave_iters : 1000;
    if (max_si > 0) {
        PropResult sr = bounds_shave(ctx, max_si);
        if (sr == PROP_CONFLICT) return SOLVE_UNSAT;
    }

    for (;;) {
        /* ── Variable selection ── */
        uint32_t x_id = _select_unassigned(ctx);
        if (x_id == EXPR_NULL) return SOLVE_OK;   /* all assigned */
        int64_t v = _pick_value(ctx, x_id, opts);

        /* ── Record decision ── */
        uint32_t dec_idx = ctx->decision_level;   /* index before push */
        ctx->decisions[dec_idx].var_id      = x_id;
        ctx->decisions[dec_idx].tried_value = v;
        ctx->decisions[dec_idx].tried_lower = 0;

        /* ── Push level and assign ── */
        trail_push_level(ctx);
        PropResult pr = ctx_tighten_lb64(ctx, x_id, v);
        if (pr == PROP_OK) pr = ctx_tighten_ub64(ctx, x_id, v);
        if (pr == PROP_OK) {
            pr = solver_propagate(ctx);
        }

        /* ── Conflict loop ── */
        while (pr == PROP_CONFLICT) {
            ctx->conflict_count++;
            local_conflicts++;

            uint32_t cur = ctx->decision_level;

            /* Restart check */
            if (max_conflicts > 0 && local_conflicts >= luby_limit) {
                trail_backtrack(ctx, 0);
                local_conflicts = 0;
                restart_count++;
                luby_idx++;
                luby_limit = _luby(luby_idx) * max_conflicts;

                if (max_restarts > 0 && restart_count >= max_restarts)
                    return SOLVE_TIMEOUT;

                pr = solver_propagate(ctx);
                if (pr == PROP_CONFLICT) return SOLVE_UNSAT;
                break;  /* restart outer for-loop */
            }

            /* No further backtrack possible at this level.  Restart
             * with a different RNG state rather than giving up —
             * the problem may be satisfiable via a different search
             * path.  True UNSAT is concluded only if the level-0
             * propagation itself conflicts (domains genuinely empty)
             * or the restart budget is exhausted (SOLVE_TIMEOUT). */
            if (cur == 0) {
                trail_backtrack(ctx, 0);
                local_conflicts = 0;
                restart_count++;
                luby_idx++;
                luby_limit = _luby(luby_idx) * max_conflicts;

                if (max_restarts > 0 && restart_count >= max_restarts)
                    return SOLVE_TIMEOUT;

                pr = solver_propagate(ctx);
                if (pr == PROP_CONFLICT) return SOLVE_UNSAT;
                break;  /* restart outer for-loop */
            }

            /* Retrieve the decision that created this level */
            DecisionRecord *d   = &ctx->decisions[cur - 1];
            uint32_t        dv  = d->var_id;
            int64_t         val = d->tried_value;

            /* Backtrack to the previous level */
            trail_backtrack(ctx, cur - 1);

            /* Exclude `val` from dv's domain at the previous level */
            int64_t dlo = var_lo64(ctx, &ctx->vars[dv]);
            int64_t dhi = var_hi64(ctx, &ctx->vars[dv]);
            if (dlo > dhi) {
                /* Domain already empty after backtrack — propagate conflict up */
                pr = PROP_CONFLICT;
            } else if (val <= dlo) {
                pr = ctx_tighten_lb64(ctx, dv, dlo + 1);
            } else if (val >= dhi) {
                pr = ctx_tighten_ub64(ctx, dv, dhi - 1);
            } else {
                /* Middle value: two-phase domain bisection.
                 * Split at the midpoint of [dlo, dhi] (not at val)
                 * for systematic, logarithmic-depth exploration.
                 * Phase 1: explore lower half [dlo, mid].
                 * Phase 2: explore upper half [mid+1, dhi]. */
                int64_t mid = dlo + (dhi - dlo) / 2;
                if (!d->tried_lower) {
                    d->tried_lower = 1;
                    pr = ctx_tighten_ub64(ctx, dv, mid);
                } else {
                    d->tried_lower = 0;
                    pr = ctx_tighten_lb64(ctx, dv, mid + 1);
                }
            }

            if (pr == PROP_OK) {
                pr = solver_propagate(ctx);
                /* Save phase if enabled */
                if (pr == PROP_OK && opts && opts->use_phase_save && ctx->phase_save)
                    ctx->phase_save[dv] = (int32_t)val;
            }
            /* If pr == PROP_CONFLICT, loop continues → backtracks further */
        }
    }
}

/* ------------------------------------------------------------------ */
/* solver_get_value                                                    */
/* ------------------------------------------------------------------ */

int64_t solver_get_value(const SolveCtx *ctx, uint32_t var_id) {
    if (!ctx || var_id >= ctx->n_vars) return 0;
    return var_lo64(ctx, &ctx->vars[var_id]);
}
