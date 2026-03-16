#include <string.h>
#include "zsp_ctx.h"
#include "zsp_propagator.h"
#include "zsp_problem.h"

/* ------------------------------------------------------------------ */
/* Internal helpers                                                    */
/* ------------------------------------------------------------------ */

/** Number of 64-bit limbs needed for `width` bits. */
static uint32_t _n_limbs(uint16_t width) {
    return (uint32_t)((width + 63u) / 64u);
}

/** Initialise a tier-0 Variable (width ≤ 32). */
static void _init_tier0(Variable *v, uint16_t width, uint8_t flags,
                         int64_t lo, int64_t hi) {
    v->lo           = (int32_t)lo;
    v->hi           = (int32_t)hi;
    v->holes_offset = 0;
    v->width        = width;
    v->flags        = (flags & ~(VAR_TIER1 | VAR_TIER2));
    v->_pad         = 0;
}

/** Initialise a tier-1 Variable (33–64 bits); allocates WideBounds64. */
static int _init_tier1(SolveCtx *ctx, Variable *v, uint16_t width,
                        uint8_t flags, int64_t lo, int64_t hi) {
    uint32_t ref = zsp_pool_alloc(&ctx->pool,
                                  (uint32_t)sizeof(WideBounds64),
                                  (uint32_t)_Alignof(WideBounds64));
    if (ref == EXPR_NULL) return -1;

    WideBounds64 *wb = (WideBounds64 *)zsp_pool_ptr(&ctx->pool, ref);
    wb->lo = lo;
    wb->hi = hi;

    v->lo           = 0;
    v->hi           = 0;
    v->holes_offset = ref;
    v->width        = width;
    v->flags        = (uint8_t)((flags & ~VAR_TIER2) | VAR_TIER1);
    v->_pad         = 0;
    return 0;
}

/** Initialise a tier-2 Variable (> 64 bits); allocates WideBoundsN. */
static int _init_tier2(SolveCtx *ctx, Variable *v, uint16_t width,
                        uint8_t flags, int64_t lo, int64_t hi) {
    uint32_t n = _n_limbs(width);
    /* Header + 2 limb arrays */
    uint32_t total = (uint32_t)sizeof(WideBoundsN) + 2u * n * (uint32_t)sizeof(uint64_t);
    uint32_t ref = zsp_pool_alloc(&ctx->pool, total,
                                  (uint32_t)_Alignof(WideBoundsN));
    if (ref == EXPR_NULL) return -1;

    WideBoundsN *wn = (WideBoundsN *)zsp_pool_ptr(&ctx->pool, ref);
    wn->n_limbs = n;
    wn->_pad    = 0;

    uint64_t *lo_limbs = (uint64_t *)(wn + 1);
    uint64_t *hi_limbs = lo_limbs + n;

    /* Zero-initialise all limbs first */
    memset(lo_limbs, 0, 2u * n * sizeof(uint64_t));

    /* Store lo / hi in the first limb (for the common ≤128-bit case) */
    if (lo >= 0 || (flags & VAR_SIGNED)) {
        lo_limbs[0] = (uint64_t)lo;
        hi_limbs[0] = (uint64_t)hi;
        /* Sign-extend negative values across remaining limbs */
        if ((flags & VAR_SIGNED) && lo < 0) {
            for (uint32_t i = 1; i < n; i++) lo_limbs[i] = ~(uint64_t)0;
        }
        if ((flags & VAR_SIGNED) && hi < 0) {
            for (uint32_t i = 1; i < n; i++) hi_limbs[i] = ~(uint64_t)0;
        }
    } else {
        lo_limbs[0] = (uint64_t)lo;
        hi_limbs[0] = (uint64_t)hi;
    }

    v->lo           = 0;
    v->hi           = 0;
    v->holes_offset = ref;
    v->width        = width;
    v->flags        = (uint8_t)((flags & ~VAR_TIER1) | VAR_TIER2);
    v->_pad         = 0;
    return 0;
}

/* ------------------------------------------------------------------ */
/* _compile_constraint — DAG → propagator translation                  */
/* ------------------------------------------------------------------ */

/* Helper: is this ExprRef an ExprVar?  Returns var_id via out_id. */
static int _is_var(SolveProblem *sp, ExprRef r, uint32_t *out_id) {
    if (r == EXPR_NULL) return 0;
    ExprKind k = *(ExprKind *)zsp_pool_ptr(&sp->pool, r);
    if (k != EXPR_VAR) return 0;
    ExprVar *ev = (ExprVar *)zsp_pool_ptr(&sp->pool, r);
    *out_id = ev->var_id;
    return 1;
}

/* Helper: is this ExprRef an ExprConst?  Returns value via out_val. */
static int _is_const(SolveProblem *sp, ExprRef r, int64_t *out_val) {
    if (r == EXPR_NULL) return 0;
    ExprKind k = *(ExprKind *)zsp_pool_ptr(&sp->pool, r);
    if (k != EXPR_CONST) return 0;
    ExprConst *ec = (ExprConst *)zsp_pool_ptr(&sp->pool, r);
    *out_val = ec->value;
    return 1;
}

/*
 * Handle a var-const or const-var comparison by tightening bounds
 * directly at compile time (no propagator needed; constant bounds).
 * `flipped` is 1 when the constant is on the left side of the operator.
 * Returns 1 if handled, 0 otherwise.
 */
static int _compile_var_const_cmp(SolveCtx *ctx, BinOp op,
                                   uint32_t vid, int64_t cv, int flipped) {
    BinOp eff = op;
    if (flipped) {
        switch (op) {
        case BIN_LTE: eff = BIN_GTE; break;
        case BIN_LT:  eff = BIN_GT;  break;
        case BIN_GTE: eff = BIN_LTE; break;
        case BIN_GT:  eff = BIN_LT;  break;
        default:      eff = op;      break;
        }
    }

    /* Use 64-bit tighten functions to avoid truncating large constants. */
    switch (eff) {
    case BIN_EQ:
        if (ctx_tighten_lb64(ctx, vid, cv) == PROP_CONFLICT) return -1;
        if (ctx_tighten_ub64(ctx, vid, cv) == PROP_CONFLICT) return -1;
        return 1;
    case BIN_LTE:
        if (ctx_tighten_ub64(ctx, vid, cv) == PROP_CONFLICT) return -1;
        return 1;
    case BIN_LT:
        if (ctx_tighten_ub64(ctx, vid, cv - 1) == PROP_CONFLICT) return -1;
        return 1;
    case BIN_GTE:
        if (ctx_tighten_lb64(ctx, vid, cv) == PROP_CONFLICT) return -1;
        return 1;
    case BIN_GT:
        if (ctx_tighten_lb64(ctx, vid, cv + 1) == PROP_CONFLICT) return -1;
        return 1;
    default:
        return 0;
    }
}

/* Returns 1 if the constraint was compiled, 0 if it could not be handled. */
static int _compile_constraint(SolveCtx *ctx, SolveProblem *sp, ExprRef root) {
    if (root == EXPR_NULL) return 1; /* vacuously handled */

    ExprKind k = *(ExprKind *)zsp_pool_ptr(&sp->pool, root);

    if (k == EXPR_BINARY) {
        ExprBinary *e = (ExprBinary *)zsp_pool_ptr(&sp->pool, root);
        uint32_t lid, rid2;

        /* Binary comparison: var op var */
        if (_is_var(sp, e->lhs, &lid) && _is_var(sp, e->rhs, &rid2)) {
            uint16_t w = ctx->vars[lid].width > ctx->vars[rid2].width
                         ? ctx->vars[lid].width : ctx->vars[rid2].width;
            if (w <= 32) {
                switch (e->op) {
                case BIN_LTE: prop_add_bounds_le_32(ctx, lid, rid2, 0); return 1;
                case BIN_LT:  prop_add_bounds_lt_32(ctx, lid, rid2, 0); return 1;
                case BIN_EQ:  prop_add_bounds_eq_32(ctx, lid, rid2, 0); return 1;
                case BIN_NEQ: prop_add_bounds_ne_32(ctx, lid, rid2, 0); return 1;
                default: break;
                }
            } else {
                switch (e->op) {
                case BIN_LTE: prop_add_bounds_le_64(ctx, lid, rid2, 0); return 1;
                case BIN_LT:  prop_add_bounds_lt_64(ctx, lid, rid2, 0); return 1;
                case BIN_EQ:  prop_add_bounds_eq_64(ctx, lid, rid2, 0); return 1;
                case BIN_NEQ: prop_add_bounds_ne_64(ctx, lid, rid2, 0); return 1;
                default: break;
                }
            }
        }

        /* Binary comparison: var op const  or  const op var */
        {
            uint32_t vid2; int64_t cv2;
            if (_is_var(sp, e->lhs, &vid2) && _is_const(sp, e->rhs, &cv2)) {
                int r = _compile_var_const_cmp(ctx, e->op, vid2, cv2, 0);
                if (r != 0) return r;  /* 1 = compiled, -1 = UNSAT */
            } else if (_is_const(sp, e->lhs, &cv2) && _is_var(sp, e->rhs, &vid2)) {
                int r = _compile_var_const_cmp(ctx, e->op, vid2, cv2, 1);
                if (r != 0) return r;
            }
        }

        /* r = a op b  (EQ with RHS binary expression) */
        if (e->op == BIN_EQ && _is_var(sp, e->lhs, &lid)) {
            if (e->rhs != EXPR_NULL) {
                ExprKind rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->rhs);
                if (rk == EXPR_BINARY) {
                    ExprBinary *rhs_e = (ExprBinary *)zsp_pool_ptr(&sp->pool, e->rhs);
                    uint32_t aid, bid;
                    if (_is_var(sp, rhs_e->lhs, &aid) && _is_var(sp, rhs_e->rhs, &bid)) {
                        uint16_t w = ctx->vars[lid].width;
                        if (ctx->vars[aid].width > w) w = ctx->vars[aid].width;
                        if (ctx->vars[bid].width > w) w = ctx->vars[bid].width;
                        if (w <= 32) {
                            switch (rhs_e->op) {
                            case BIN_ADD: prop_add_bounds_add_32(ctx, lid, aid, bid, 0); return 1;
                            case BIN_MUL: prop_add_bounds_mul_32(ctx, lid, aid, bid, 0); return 1;
                            case BIN_DIV: prop_add_bounds_div_32(ctx, lid, aid, bid, 0); return 1;
                            case BIN_MOD: prop_add_bounds_mod_32(ctx, lid, aid, bid, 0); return 1;
                            default: break;
                            }
                        } else {
                            switch (rhs_e->op) {
                            case BIN_ADD: prop_add_bounds_add_64(ctx, lid, aid, bid, 0); return 1;
                            case BIN_MUL: prop_add_bounds_mul_64(ctx, lid, aid, bid, 0); return 1;
                            case BIN_DIV: prop_add_bounds_div_64(ctx, lid, aid, bid, 0); return 1;
                            case BIN_MOD: prop_add_bounds_mod_64(ctx, lid, aid, bid, 0); return 1;
                            default: break;
                            }
                        }
                    }
                }
            }
        }
    }
    /* EXPR_UNARY, EXPR_IN_SET, EXPR_ITE etc. are not yet handled natively */
    return 0;
}

/* ------------------------------------------------------------------ */
/* solver_compile                                                      */
/* ------------------------------------------------------------------ */

int solver_compile(SolveCtx *ctx, SolveProblem *sp) {
    uint32_t n = sp->n_vars;
    if (n == 0) {
        ctx->vars   = NULL;
        ctx->n_vars = 0;
        return 0;
    }

    /* Allocate a contiguous Variable[n] in the static pool.
       Variables are indexed by var_id; we'll fill them below. */
    uint32_t vars_ref = zsp_pool_alloc(&ctx->pool,
                                        n * (uint32_t)sizeof(Variable),
                                        (uint32_t)_Alignof(Variable));
    if (vars_ref == EXPR_NULL) return -1;

    ctx->vars   = (Variable *)zsp_pool_ptr(&ctx->pool, vars_ref);
    ctx->n_vars = n;

    /* Zero-initialise the whole array */
    memset(ctx->vars, 0, n * sizeof(Variable));

    /* Walk the VarSpec linked list (stored in sp's pool) */
    ExprRef ref = sp->vars_head;

    while (ref != EXPR_NULL) {
        VarSpec *vs = (VarSpec *)zsp_pool_ptr(&sp->pool, ref);
        uint32_t id = vs->var_id;

        if (id >= n) {
            /* var_id out of range — pool corruption or misuse */
            return -1;
        }

        Variable *v = &ctx->vars[id];
        uint8_t  flags = 0;
        if (vs->is_signed) flags |= VAR_SIGNED;

        int rc;
        if (vs->width <= 32) {
            _init_tier0(v, vs->width, flags, vs->lo, vs->hi);
            rc = 0;
        } else if (vs->width <= 64) {
            rc = _init_tier1(ctx, v, vs->width, flags, vs->lo, vs->hi);
        } else {
            rc = _init_tier2(ctx, v, vs->width, flags, vs->lo, vs->hi);
        }

        if (rc != 0) return -1;
        ref = vs->next;
    }

    /* ---- Allocate per-variable watcher head array ---- */
    uint32_t wh_ref = zsp_pool_alloc(&ctx->pool,
                                      n * (uint32_t)sizeof(uint32_t),
                                      (uint32_t)_Alignof(uint32_t));
    if (wh_ref == EXPR_NULL) return -1;
    ctx->watcher_heads = (uint32_t *)zsp_pool_ptr(&ctx->pool, wh_ref);
    for (uint32_t i = 0; i < n; i++) ctx->watcher_heads[i] = EXPR_NULL;

    /* ---- Walk ConstraintSpec list → create propagators ---- */
    int n_uncompiled = 0;
    ExprRef cref = sp->constraints_head;
    while (cref != EXPR_NULL) {
        ConstraintSpec *cs = (ConstraintSpec *)zsp_pool_ptr(&sp->pool, cref);
        int r = _compile_constraint(ctx, sp, cs->root);
        if (r < 0) return -2;  /* -2 = UNSAT detected at compile time */
        if (r == 0) n_uncompiled++;
        cref = cs->next;
    }

    /* Return count of uncompiled constraints (0 = all compiled, >0 = partial,
       negative values reserved for hard errors above). */
    return n_uncompiled;
}
