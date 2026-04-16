/*
 * zsp_contradiction.c -- Contradiction analysis module.
 *
 * Compiled only when ZSP_CONTRADICTION_ANALYSIS is defined.
 * Implements UNSAT core extraction and MUS minimization via QuickXplain.
 */

#ifdef ZSP_CONTRADICTION_ANALYSIS

#include <string.h>
#include <stdlib.h>
#include <time.h>
#include "zsp_contradiction.h"
#include "zsp_search.h"
#include "zsp_block_alloc.h"

#define CONTRA_CTX_BUF_SIZE  (1024u * 1024u)
#define CONTRA_BLOCK_SIZE    4096u
#define MAX_CORE_SIZE        256u

/* ---- GatedProblem ---- */

typedef struct {
    SolveProblem *sp;
    void         *buf;
    uint32_t      n_hard;
    uint32_t     *cid_map; /* cid_map[assumption_idx] = constraint_id */
} GatedProblem;

/**
 * Build an assumption-gated copy of the original problem.
 * Each hard constraint becomes a soft constraint.
 * cid_map is indexed by the assumption index the compiler will assign.
 */
static int _build_gated_problem(SolveProblem *orig, GatedProblem *out) {
    memset(out, 0, sizeof(*out));
    uint32_t orig_pool_used = zsp_pool_used(&orig->pool);
    uint32_t n_hard = orig->n_constraints;

    size_t buf_size = sizeof(SolveProblem) + orig_pool_used + n_hard * 64 + 4096;
    void *buf = malloc(buf_size);
    if (!buf) return -1;
    SolveProblem *sp = solve_problem_init(buf, buf_size);
    if (!sp) { free(buf); return -1; }

    uint32_t *cid_map = NULL;
    if (n_hard > 0) {
        cid_map = (uint32_t *)malloc(n_hard * sizeof(uint32_t));
        if (!cid_map) { free(buf); return -1; }
    }

    /* Bulk-copy pool data to preserve ExprRef offsets */
    uint8_t *dst = (uint8_t *)&sp->pool + sizeof(zsp_pool_t);
    uint8_t *src = (uint8_t *)&orig->pool + sizeof(zsp_pool_t);
    memcpy(dst, src, orig_pool_used);
    sp->pool.used = orig_pool_used;

    sp->n_vars       = orig->n_vars;
    sp->vars_head    = orig->vars_head;
    sp->n_alldiffs   = orig->n_alldiffs;
    sp->allDiff_head = orig->allDiff_head;
    sp->n_dists      = orig->n_dists;
    sp->dists_head   = orig->dists_head;
    sp->n_constraints    = 0;
    sp->constraints_head = EXPR_NULL;
    sp->n_softs          = 0;
    sp->softs_head       = EXPR_NULL;

    /* Convert each hard constraint to soft. Walk original constraint list
     * and add as soft with unique priority. */
    uint32_t aidx = 0;
    ExprRef cref = orig->constraints_head;
    while (cref != EXPR_NULL) {
        ConstraintSpec *cs = (ConstraintSpec *)POOL_PTR(orig, cref);
        ExprRef sref = problem_add_soft_constraint(sp, cs->root, aidx);
        if (sref == EXPR_NULL) { free(cid_map); free(buf); return -1; }
        SoftSpec *ss = (SoftSpec *)POOL_PTR(sp, sref);
        ss->constraint_id = cs->constraint_id;
        if (cid_map) cid_map[aidx] = cs->constraint_id;
        aidx++;
        cref = cs->next;
    }

    /* Reverse cid_map to match assumption index order.
     *
     * The constraint list is LIFO, so we walk it in reverse-add order.
     * problem_add_soft_constraint also uses LIFO, so the softs_head list
     * is doubly-reversed (back to original add order). When solver_compile
     * walks softs_head, assumption index 0 gets the LAST constraint we
     * walked (= first originally added). Reversing cid_map aligns the
     * assumption indices with the correct constraint_ids. */
    if (cid_map && n_hard > 1) {
        for (uint32_t i = 0; i < n_hard / 2; i++) {
            uint32_t tmp = cid_map[i];
            cid_map[i] = cid_map[n_hard - 1 - i];
            cid_map[n_hard - 1 - i] = tmp;
        }
    }

    out->sp = sp;  out->buf = buf;  out->n_hard = n_hard;  out->cid_map = cid_map;
    return 0;
}

static void _gated_free(GatedProblem *g) {
    free(g->cid_map); free(g->buf); memset(g, 0, sizeof(*g));
}

/* ---- SolverInstance ---- */

typedef struct {
    SolveCtx *ctx;  void *ctx_buf;  zsp_block_alloc_t *ba;
} SolverInstance;

static int _solver_create(SolverInstance *si, SolveProblem *sp) {
    memset(si, 0, sizeof(*si));
    si->ctx_buf = malloc(CONTRA_CTX_BUF_SIZE);
    if (!si->ctx_buf) return -1;
    si->ba = zsp_block_alloc_create(NULL, CONTRA_BLOCK_SIZE);
    if (!si->ba) { free(si->ctx_buf); si->ctx_buf = NULL; return -1; }
    si->ctx = solver_create(si->ctx_buf, CONTRA_CTX_BUF_SIZE, si->ba);
    if (!si->ctx) {
        zsp_block_alloc_destroy(si->ba); free(si->ctx_buf);
        memset(si, 0, sizeof(*si)); return -1;
    }
    int rc = solver_compile(si->ctx, sp);
    if (rc == -1) {
        zsp_block_alloc_destroy(si->ba); free(si->ctx_buf);
        memset(si, 0, sizeof(*si)); return -1;
    }
    return rc;
}

static void _solver_destroy(SolverInstance *si) {
    if (si->ctx) solver_destroy(si->ctx);
    if (si->ba) zsp_block_alloc_destroy(si->ba);
    free(si->ctx_buf); memset(si, 0, sizeof(*si));
}

/* ---- _is_sat: raw SAT/UNSAT check with current assumption mask ---- */

static int _is_sat(SolverInstance *si, uint32_t *calls) {
    SolveCtx *ctx = si->ctx;
    solver_reset(ctx);

    /* Pin deactivated assumptions to 0 */
    for (uint32_t i = 0; i < ctx->n_assumptions; i++) {
        if (!(ctx->assumption_active_mask & (1ULL << i))) {
            uint32_t av = ctx->assumption_var_ids[i];
            Variable *v = &ctx->vars[av];
            v->lo = 0; v->hi = 0;
            if (av < 64)
                ctx->unassigned_mask &= ~(1ULL << av);
        }
    }

    /* Disable assumption relaxation for raw result */
    uint32_t saved_n = ctx->n_assumptions;
    ctx->n_assumptions = 0;

    SolveOpts opts;
    memset(&opts, 0, sizeof(opts));
    opts.max_conflicts = 50000;
    opts.max_restarts  = 200;

    if (calls) (*calls)++;
    SolveResult res = solver_solve(ctx, &opts);
    ctx->n_assumptions = saved_n;

    return (res == SOLVE_OK) ? 1 : 0;
}

/* ---- Helpers ---- */

static void _activate_set(SolveCtx *ctx, const uint32_t *indices, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        if (indices[i] < 64)
            ctx->assumption_active_mask |= (1ULL << indices[i]);
    }
}

/* ---- QuickXplain ---- */

static void _quickxplain(SolverInstance *si,
                          const uint32_t *bg, uint32_t n_bg,
                          const uint32_t *cand, uint32_t n_cand,
                          uint32_t *out_mus, uint32_t *out_n,
                          uint32_t max_out,
                          uint32_t *calls, uint32_t budget) {
    if (budget > 0 && *calls >= budget) return;
    if (*out_n >= max_out || n_cand == 0) return;

    if (n_cand == 1) {
        /* Check if bg alone (without candidate) is SAT */
        si->ctx->assumption_active_mask = 0;
        _activate_set(si->ctx, bg, n_bg);
        if (_is_sat(si, calls) == 1) {
            /* bg is SAT, so this candidate is necessary for UNSAT */
            if (*out_n < max_out)
                out_mus[(*out_n)++] = cand[0];
        }
        return;
    }

    uint32_t mid = n_cand / 2;
    const uint32_t *c1 = cand, *c2 = cand + mid;
    uint32_t n_c1 = mid, n_c2 = n_cand - mid;

    /* Test if bg + c1 is SAT */
    si->ctx->assumption_active_mask = 0;
    _activate_set(si->ctx, bg, n_bg);
    _activate_set(si->ctx, c1, n_c1);

    if (_is_sat(si, calls) == 1) {
        /* bg + c1 is SAT -> find necessary in c2 with bg+c1 as background */
        uint32_t *new_bg = (uint32_t *)malloc((n_bg + n_c1) * sizeof(uint32_t));
        if (!new_bg) return;
        memcpy(new_bg, bg, n_bg * sizeof(uint32_t));
        memcpy(new_bg + n_bg, c1, n_c1 * sizeof(uint32_t));

        uint32_t mus_before = *out_n;
        _quickxplain(si, new_bg, n_bg + n_c1, c2, n_c2,
                      out_mus, out_n, max_out, calls, budget);

        /* Find necessary in c1 with bg+mus_from_c2 as background */
        uint32_t n_mus2 = *out_n - mus_before;
        uint32_t *new_bg2 = (uint32_t *)malloc((n_bg + n_mus2) * sizeof(uint32_t));
        if (new_bg2) {
            memcpy(new_bg2, bg, n_bg * sizeof(uint32_t));
            memcpy(new_bg2 + n_bg, out_mus + mus_before,
                   n_mus2 * sizeof(uint32_t));
            _quickxplain(si, new_bg2, n_bg + n_mus2, c1, n_c1,
                          out_mus, out_n, max_out, calls, budget);
            free(new_bg2);
        }
        free(new_bg);
    } else {
        /* bg + c1 is UNSAT -> MUS is within c1 */
        _quickxplain(si, bg, n_bg, c1, n_c1,
                      out_mus, out_n, max_out, calls, budget);
    }
}

/* ---- Deletion-based MUS for small cores ---- */

static void _deletion_mus(SolverInstance *si,
                           const uint32_t *core, uint32_t core_n,
                           uint32_t *out_mus, uint32_t *out_n,
                           uint32_t max_out, uint32_t *calls) {
    uint8_t necessary[MAX_CORE_SIZE];
    memset(necessary, 1, core_n);

    for (uint32_t i = 0; i < core_n; i++) {
        si->ctx->assumption_active_mask = 0;
        for (uint32_t j = 0; j < core_n; j++) {
            if (j != i && necessary[j] && core[j] < 64)
                si->ctx->assumption_active_mask |= (1ULL << core[j]);
        }
        if (_is_sat(si, calls) == 0)
            necessary[i] = 0;  /* Still UNSAT without i -> redundant */
    }

    *out_n = 0;
    for (uint32_t i = 0; i < core_n && *out_n < max_out; i++) {
        if (necessary[i])
            out_mus[(*out_n)++] = core[i];
    }
}

/* ---- contra_quick_core ---- */

int contra_quick_core(SolveCtx *ctx, SolveProblem *sp,
                       uint32_t *out_ids, uint32_t *out_n) {
    (void)ctx;
    if (!sp || !out_ids || !out_n) return -1;
    uint32_t capacity = *out_n;
    *out_n = 0;

    GatedProblem gated;
    if (_build_gated_problem(sp, &gated) != 0) return -1;

    SolverInstance si;
    int compile_rc = _solver_create(&si, gated.sp);
    if (compile_rc == -1) { _gated_free(&gated); return -1; }

    if (compile_rc == -2) {
        uint32_t n = gated.n_hard < capacity ? gated.n_hard : capacity;
        for (uint32_t i = 0; i < n; i++) out_ids[i] = gated.cid_map[i];
        *out_n = n;
        _solver_destroy(&si); _gated_free(&gated);
        return 0;
    }

    si.ctx->assumption_active_mask =
        (gated.n_hard < 64) ? ((1ULL << gated.n_hard) - 1) : ~0ULL;
    if (_is_sat(&si, NULL) == 1) {
        *out_n = 0; /* Problem is SAT */
    } else {
        uint32_t n = gated.n_hard < capacity ? gated.n_hard : capacity;
        for (uint32_t i = 0; i < n; i++) out_ids[i] = gated.cid_map[i];
        *out_n = n;
    }

    _solver_destroy(&si); _gated_free(&gated);
    return 0;
}

/* ---- contra_analyze_unsat ---- */

int contra_analyze_unsat(SolveCtx *ctx, SolveProblem *sp,
                          const ContraOpts *opts, ContraResult *result) {
    (void)ctx;
    if (!result) return -1;
    memset(result, 0, sizeof(*result));

    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    uint32_t budget = opts ? opts->max_solver_calls : 0;

    GatedProblem gated;
    if (_build_gated_problem(sp, &gated) != 0) return -1;

    SolverInstance si;
    int compile_rc = _solver_create(&si, gated.sp);
    if (compile_rc == -1) { _gated_free(&gated); return -1; }

    uint32_t solver_calls = 0;

    if (compile_rc == -2) {
        result->core_size = gated.n_hard;
        result->mus_size  = gated.n_hard;
        result->mus_constraint_ids = (uint32_t *)malloc(
            gated.n_hard * sizeof(uint32_t));
        if (result->mus_constraint_ids) {
            for (uint32_t i = 0; i < gated.n_hard; i++)
                result->mus_constraint_ids[i] = gated.cid_map[i];
        }
        _solver_destroy(&si); _gated_free(&gated);
        goto done;
    }

    /* Phase 1: verify UNSAT */
    {
        si.ctx->assumption_active_mask =
            (gated.n_hard < 64) ? ((1ULL << gated.n_hard) - 1) : ~0ULL;
        solver_calls++;
        if (_is_sat(&si, NULL) == 1) {
            /* Problem is actually SAT */
            _solver_destroy(&si); _gated_free(&gated);
            goto done;
        }

        uint32_t core_indices[MAX_CORE_SIZE];
        uint32_t core_n = 0;
        for (uint32_t i = 0; i < gated.n_hard && core_n < MAX_CORE_SIZE; i++)
            core_indices[core_n++] = i;
        result->core_size = core_n;

        /* Phase 2: MUS extraction */
        if (opts && opts->skip_minimization) {
            result->mus_size = core_n;
            result->mus_constraint_ids = (uint32_t *)malloc(
                core_n * sizeof(uint32_t));
            if (result->mus_constraint_ids) {
                for (uint32_t i = 0; i < core_n; i++)
                    result->mus_constraint_ids[i] = gated.cid_map[core_indices[i]];
            }
        } else {
            uint32_t mus_indices[MAX_CORE_SIZE];
            uint32_t mus_n = 0;

            if (core_n <= 4) {
                _deletion_mus(&si, core_indices, core_n,
                              mus_indices, &mus_n, MAX_CORE_SIZE, &solver_calls);
            } else {
                _quickxplain(&si, NULL, 0, core_indices, core_n,
                              mus_indices, &mus_n, MAX_CORE_SIZE,
                              &solver_calls, budget);
            }

            result->mus_size = mus_n;
            if (mus_n > 0) {
                result->mus_constraint_ids = (uint32_t *)malloc(
                    mus_n * sizeof(uint32_t));
                if (result->mus_constraint_ids) {
                    for (uint32_t i = 0; i < mus_n; i++)
                        result->mus_constraint_ids[i] =
                            gated.cid_map[mus_indices[i]];
                }
            }
        }
    }

    _solver_destroy(&si); _gated_free(&gated);

done:
    result->n_solver_calls = solver_calls;
    {
        struct timespec t1;
        clock_gettime(CLOCK_MONOTONIC, &t1);
        result->elapsed_sec = (t1.tv_sec - t0.tv_sec) +
                              (t1.tv_nsec - t0.tv_nsec) / 1e9;
    }
    return 0;
}

/* ---- Free/stub functions ---- */

void contra_result_free(ContraResult *result) {
    if (!result) return;
    free(result->mus_constraint_ids); free(result->proof_text);
    free(result->proof_json); free(result->relaxations);
    memset(result, 0, sizeof(*result));
}

int contra_explain_soft(SolveCtx *ctx, SolveProblem *sp,
                         const ContraOpts *opts, ContraSoftDiagResult *result) {
    (void)ctx; (void)sp; (void)opts;
    if (result) memset(result, 0, sizeof(*result));
    return -1;
}

void contra_soft_diag_free(ContraSoftDiagResult *result) {
    if (!result) return;
    for (uint32_t i = 0; i < result->n_entries; i++) {
        ContraSoftDiagEntry *e = &result->entries[i];
        free(e->conflict_hard_ids); free(e->proof_text);
        free(e->hard_relax); free(e->alternative_soft_ids);
    }
    free(result->entries); memset(result, 0, sizeof(*result));
}

int contra_compute_relaxations(SolveCtx *ctx, SolveProblem *sp,
                                const uint32_t *mus_ids, uint32_t mus_size,
                                const ContraOpts *opts, ContraRelaxSuggestion *out) {
    (void)ctx; (void)sp; (void)mus_ids; (void)mus_size; (void)opts;
    if (out && mus_size > 0) memset(out, 0, mus_size * sizeof(*out));
    return -1;
}

#endif /* ZSP_CONTRADICTION_ANALYSIS */
