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
/* Helper: should a variable use 64-bit propagators?                   */
/*                                                                     */
/* Returns true when the variable's actual storage is tier-1 or above, */
/* regardless of its declared width.  This covers unsigned 32-bit vars */
/* that have been promoted to tier-1 to avoid int32 overflow.          */
/* ------------------------------------------------------------------ */
static int _var_needs_wide(const SolveCtx *ctx, uint32_t var_id) {
    return !VAR_IS_TIER0(ctx->vars[var_id].flags);
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

/* ------------------------------------------------------------------ */
/* _flatten_or — collect var-const comparison clauses from an OR tree */
/* ------------------------------------------------------------------ */

#define MAX_OR_CLAUSES 4

typedef struct {
    uint32_t var_id;
    uint32_t op;
    int64_t  constant;
} OrClause;

/**
 * Recursively flatten a BIN_OR tree of var-const comparisons.
 * Returns number of clauses extracted, or -1 if the tree contains
 * unsupported nodes.
 */
static int _flatten_or(SolveProblem *sp, ExprRef ref,
                        OrClause *out, int max_clauses) {
    if (ref == EXPR_NULL) return -1;
    ExprKind k = *(ExprKind *)zsp_pool_ptr(&sp->pool, ref);
    if (k != EXPR_BINARY) return -1;

    ExprBinary *e = (ExprBinary *)zsp_pool_ptr(&sp->pool, ref);

    if (e->op == BIN_OR) {
        int n_left = _flatten_or(sp, e->lhs, out, max_clauses);
        if (n_left < 0) return -1;
        int n_right = _flatten_or(sp, e->rhs, out + n_left,
                                   max_clauses - n_left);
        if (n_right < 0) return -1;
        return n_left + n_right;
    }

    /* Leaf: must be a var-const or const-var comparison */
    uint32_t vid; int64_t cv;
    if (_is_var(sp, e->lhs, &vid) && _is_const(sp, e->rhs, &cv)) {
        if (max_clauses < 1) return -1;
        out[0].var_id = vid;
        out[0].op = e->op;
        out[0].constant = cv;
        return 1;
    }
    if (_is_const(sp, e->lhs, &cv) && _is_var(sp, e->rhs, &vid)) {
        if (max_clauses < 1) return -1;
        out[0].var_id = vid;
        /* Flip the operator since constant is on the left */
        switch (e->op) {
        case BIN_LT:  out[0].op = BIN_GT;  break;
        case BIN_LTE: out[0].op = BIN_GTE; break;
        case BIN_GT:  out[0].op = BIN_LT;  break;
        case BIN_GTE: out[0].op = BIN_LTE; break;
        default:      out[0].op = e->op;   break;
        }
        out[0].constant = cv;
        return 1;
    }

    return -1;  /* not a var-const comparison */
}

/* Returns 1 if the constraint was compiled, 0 if it could not be handled. */
static int _compile_constraint(SolveCtx *ctx, SolveProblem *sp, ExprRef root) {
    if (root == EXPR_NULL) return 1; /* vacuously handled */

    ExprKind k = *(ExprKind *)zsp_pool_ptr(&sp->pool, root);

    if (k == EXPR_BINARY) {
        ExprBinary *e = (ExprBinary *)zsp_pool_ptr(&sp->pool, root);
        uint32_t lid, rid2;

        /* OR tree of var-const comparisons → DisjClause propagator */
        if (e->op == BIN_OR) {
            OrClause clauses[MAX_OR_CLAUSES];
            int n = _flatten_or(sp, root, clauses, MAX_OR_CLAUSES);
            if (n >= 2 && n <= (int)MAX_OR_CLAUSES) {
                uint32_t vids[MAX_OR_CLAUSES];
                uint32_t ops[MAX_OR_CLAUSES];
                int64_t  cvs[MAX_OR_CLAUSES];
                for (int i = 0; i < n; i++) {
                    vids[i] = clauses[i].var_id;
                    ops[i]  = clauses[i].op;
                    cvs[i]  = clauses[i].constant;
                }
                uint32_t ref = prop_add_disj_clause(ctx, (uint32_t)n,
                                                     vids, ops, cvs, 0);
                return (ref != EXPR_NULL) ? 1 : 0;
            }
            return 0;  /* couldn't flatten — fall through */
        }

        /* Binary comparison: var op var */
        if (_is_var(sp, e->lhs, &lid) && _is_var(sp, e->rhs, &rid2)) {
            /* Use 64-bit propagators if either variable is promoted to tier-1+ */
            int wide = _var_needs_wide(ctx, lid) || _var_needs_wide(ctx, rid2);
            uint16_t w = wide ? 64 : (ctx->vars[lid].width > ctx->vars[rid2].width
                         ? ctx->vars[lid].width : ctx->vars[rid2].width);
            if (w <= 32) {
                switch (e->op) {
                case BIN_LTE: prop_add_bounds_le_32(ctx, lid, rid2, 0); return 1;
                case BIN_LT:  prop_add_bounds_lt_32(ctx, lid, rid2, 0); return 1;
                case BIN_EQ:  prop_add_bounds_eq_32(ctx, lid, rid2, 0); return 1;
                case BIN_NEQ: prop_add_bounds_ne_32(ctx, lid, rid2, 0); return 1;
                case BIN_GT:  prop_add_bounds_lt_32(ctx, rid2, lid, 0); return 1;
                case BIN_GTE: prop_add_bounds_le_32(ctx, rid2, lid, 0); return 1;
                default: break;
                }
            } else {
                switch (e->op) {
                case BIN_LTE: prop_add_bounds_le_64(ctx, lid, rid2, 0); return 1;
                case BIN_LT:  prop_add_bounds_lt_64(ctx, lid, rid2, 0); return 1;
                case BIN_EQ:  prop_add_bounds_eq_64(ctx, lid, rid2, 0); return 1;
                case BIN_NEQ: prop_add_bounds_ne_64(ctx, lid, rid2, 0); return 1;
                case BIN_GT:  prop_add_bounds_lt_64(ctx, rid2, lid, 0); return 1;
                case BIN_GTE: prop_add_bounds_le_64(ctx, rid2, lid, 0); return 1;
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
        /* r = a op b  (EQ with one side a var, other side a binary expr)
         * Handles both  var == BinOp(a, b)  and  BinOp(a, b) == var/const
         * Also handles const-var operand order: var == const * var */
        if (e->op == BIN_EQ) {
            ExprRef var_side = EXPR_NULL, expr_side = EXPR_NULL;
            /* Determine which side is the "result" var and which is the expr */
            if (e->lhs != EXPR_NULL && e->rhs != EXPR_NULL) {
                ExprKind lk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->lhs);
                ExprKind rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->rhs);
                if (lk == EXPR_VAR && rk == EXPR_BINARY) {
                    var_side = e->lhs; expr_side = e->rhs;
                } else if (lk == EXPR_BINARY && rk == EXPR_VAR) {
                    var_side = e->rhs; expr_side = e->lhs;
                } else if (lk == EXPR_BINARY && rk == EXPR_CONST) {
                    /* BinOp(...) == const: handled below via var_const_cmp
                     * after introducing temp var in IR translator */
                    var_side = EXPR_NULL; expr_side = EXPR_NULL;
                }
            }
            if (var_side != EXPR_NULL && expr_side != EXPR_NULL) {
                ExprVar *ev = (ExprVar *)zsp_pool_ptr(&sp->pool, var_side);
                uint32_t r_id = ev->var_id;
                ExprBinary *binop = (ExprBinary *)zsp_pool_ptr(&sp->pool, expr_side);
                uint32_t a_id, b_id;
                int64_t cv;
                int has_var_var = _is_var(sp, binop->lhs, &a_id) && _is_var(sp, binop->rhs, &b_id);
                int has_const_var = _is_const(sp, binop->lhs, &cv) && _is_var(sp, binop->rhs, &b_id);
                int has_var_const = _is_var(sp, binop->lhs, &a_id) && _is_const(sp, binop->rhs, &cv);
                if (has_var_var || has_const_var || has_var_const) {
                    /* For const-var: treat as var-const with commutative ops,
                     * or swap for non-commutative ops */
                    if (has_const_var) {
                        /* const op var: for Add/Mul (commutative), just swap */
                        a_id = b_id;
                        /* Create a temp const-var by adding the const as a
                         * compile-time bound tightening: r = cv op b */
                        /* Actually, for MUL: r = cv * b is the same as r = b * cv
                         * For ADD: r = cv + b is the same as r = b + cv
                         * So we handle commutative ops by just rewriting */
                        /* We need both operands to be var IDs for the propagator.
                         * Fall through if we can't handle it. */
                        has_var_var = 0; /* force fallthrough for now */
                    }
                    if (has_var_var) {
                        /* Use 64-bit propagators if any operand is promoted */
                        int wide = _var_needs_wide(ctx, r_id) ||
                                   _var_needs_wide(ctx, a_id) ||
                                   _var_needs_wide(ctx, b_id);
                        uint16_t w = wide ? 64 : ctx->vars[r_id].width;
                        if (w <= 32) {
                            switch (binop->op) {
                            case BIN_ADD: prop_add_bounds_add_32(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_MUL: prop_add_bounds_mul_32(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_DIV: prop_add_bounds_div_32(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_MOD: prop_add_bounds_mod_32(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BAND: prop_add_bounds_band_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BOR:  prop_add_bounds_bor_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BXOR: prop_add_bounds_bxor_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_LSHIFT: prop_add_bounds_shl_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_RSHIFT: prop_add_bounds_lshr_64(ctx, r_id, a_id, b_id, 0); return 1;
                            default: break;
                            }
                        } else {
                            switch (binop->op) {
                            case BIN_ADD: prop_add_bounds_add_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_MUL: prop_add_bounds_mul_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_DIV: prop_add_bounds_div_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_MOD: prop_add_bounds_mod_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BAND: prop_add_bounds_band_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BOR:  prop_add_bounds_bor_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_BXOR: prop_add_bounds_bxor_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_LSHIFT: prop_add_bounds_shl_64(ctx, r_id, a_id, b_id, 0); return 1;
                            case BIN_RSHIFT: prop_add_bounds_lshr_64(ctx, r_id, a_id, b_id, 0); return 1;
                            default: break;
                            }
                        }
                    }
                }
            }
        }

        /* BinOp(var, var) op const  — handle the pattern where an arithmetic
         * expression is compared to a constant (e.g. s012345 + p6 == 200) */
        {
            int64_t cv;
            ExprRef expr_side = EXPR_NULL;
            BinOp cmp_op = e->op;
            int flipped = 0;
            if (e->lhs != EXPR_NULL && _is_const(sp, e->rhs, &cv)) {
                ExprKind lk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->lhs);
                if (lk == EXPR_BINARY) expr_side = e->lhs;
            } else if (e->rhs != EXPR_NULL && _is_const(sp, e->lhs, &cv)) {
                ExprKind rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->rhs);
                if (rk == EXPR_BINARY) { expr_side = e->rhs; flipped = 1; }
            }
            if (expr_side != EXPR_NULL) {
                ExprBinary *binop = (ExprBinary *)zsp_pool_ptr(&sp->pool, expr_side);
                uint32_t a_id, b_id;
                if (_is_var(sp, binop->lhs, &a_id) && _is_var(sp, binop->rhs, &b_id)) {
                    if (binop->op == BIN_ADD && (cmp_op == BIN_EQ ||
                        cmp_op == BIN_LT || cmp_op == BIN_LTE ||
                        cmp_op == BIN_GT || cmp_op == BIN_GTE)) {
                        /* (a + b) cmp cv: tighten bounds on a and b */
                        /* EQ: a+b == cv => a in [cv-b_hi, cv-b_lo], b in [cv-a_hi, cv-a_lo] */
                        BinOp eff = cmp_op;
                        if (flipped) {
                            switch (cmp_op) {
                            case BIN_LT:  eff = BIN_GT;  break;
                            case BIN_LTE: eff = BIN_GTE; break;
                            case BIN_GT:  eff = BIN_LT;  break;
                            case BIN_GTE: eff = BIN_LTE; break;
                            default: break;
                            }
                        }
                        /* Create a temporary variable for the sum, set its bounds
                         * from the comparison, and add an ADD propagator. */
                        /* For now, use the IR translator to handle this by
                         * introducing a temp var. See below. */
                    }
                }
            }
        }
    }
    /* ---- EXPR_ITE at constraint root ---- */
    if (k == EXPR_ITE) {
        ExprITE *ite = (ExprITE *)zsp_pool_ptr(&sp->pool, root);

        /* Check if this is an ITE-as-value inside an EQ: handled above.
         * Here we handle ITE as a constraint: if(cond) then else else.
         * Both branches are constraint expressions. */

        /* Determine if cond is a variable or expression */
        uint32_t cond_var_id;
        int cond_is_var = _is_var(sp, ite->cond, &cond_var_id);

        if (!cond_is_var) {
            /* Cond is a constant or expression -- check if it's a constant */
            int64_t cond_val;
            if (_is_const(sp, ite->cond, &cond_val)) {
                /* Static condition: compile only the active branch */
                if (cond_val != 0)
                    return _compile_constraint(ctx, sp, ite->then_e);
                else
                    return _compile_constraint(ctx, sp, ite->else_e);
            }
            /* Cond is a complex expression: not yet handled */
            return 0;
        }

        /* Cond is a variable: compile both branches with guard gating.
         * Then-branch fires when cond_var == 1.
         * Else-branch fires when cond_var == 0, which we track with a
         * helper not_cond variable: not_cond = 1 - cond. */

        /* Compile then-branch constraints */
        int then_rc = _compile_constraint(ctx, sp, ite->then_e);
        if (then_rc < 0) return then_rc;

        if (then_rc > 0) {
            /* Then-branch was compiled successfully.
             * The most recently added propagator is for the then-branch.
             * Set its guard to cond_var. */
            if (ctx->n_props > 0) {
                uint32_t last_prop_id = ctx->n_props - 1;
                if (ctx->prop_guard_vars && last_prop_id < ctx->n_prop_refs_capacity)
                    ctx->prop_guard_vars[last_prop_id] = cond_var_id;
            }
        }

        /* Compile else-branch if present */
        if (ite->else_e != EXPR_NULL) {
            int else_rc = _compile_constraint(ctx, sp, ite->else_e);
            if (else_rc < 0) return else_rc;

            if (else_rc > 0 && ctx->n_props > 0) {
                /* Else-branch: create a NOT-cond variable and use as guard.
                 * We need not_cond_var where not_cond = 1 - cond.
                 * For a boolean cond in [0,1], use a NE propagator approach:
                 * Add a temp variable for not_cond, constrain not_cond + cond == 1. */

                /* For simplicity, use a DisjClause-based approach instead:
                 * The else propagator should fire when cond == 0.
                 * We can achieve this by negating: create a variable that is
                 * 1 when cond is 0 and 0 when cond is 1.
                 * Use the Implication approach: set guard to cond_var but
                 * invert the semantics in the guard check.
                 * 
                 * Actually, simpler approach for boolean guard:
                 * Mark the else-propagator's guard with a special encoding.
                 * Use (cond_var_id | 0x80000000) to indicate negated guard.
                 * But that's hacky. Instead, just allocate a not_cond var
                 * and add an equality: not_cond + cond == 1 */

                /* Allocate not_cond as a new variable if we have capacity */
                uint32_t not_cond_id = ctx->n_vars;
                if (not_cond_id < ctx->n_vars_capacity) {
                    Variable *nv = &ctx->vars[not_cond_id];
                    /* Boolean: signed, [0,1] so it stays tier-0 */
                    nv->lo = 0; nv->hi = 1;
                    nv->width = 1; nv->flags = VAR_SIGNED;
                    nv->holes_offset = 0; nv->_pad = 0;
                    ctx->n_vars = not_cond_id + 1;

                    /* Ensure watcher head is initialized */
                    if (ctx->watcher_heads)
                        ctx->watcher_heads[not_cond_id] = EXPR_NULL;

                    /* Set unassigned bit */
                    if (not_cond_id < 64)
                        ctx->unassigned_mask |= (1ULL << not_cond_id);

                    /* Add constraint: not_cond + cond == 1 via add propagator.
                     * We need a temp "one" variable. Simpler: use NE propagator
                     * between cond and not_cond, plus bounds.
                     * Actually simplest: just use the add propagator.
                     * Create a const-1 variable. */
                    uint32_t one_id = ctx->n_vars;
                    if (one_id < ctx->n_vars_capacity) {
                        Variable *ov = &ctx->vars[one_id];
                        ov->lo = 1; ov->hi = 1;
                        ov->width = 1; ov->flags = VAR_SIGNED;
                        ov->holes_offset = 0; ov->_pad = 0;
                        ctx->n_vars = one_id + 1;
                        if (ctx->watcher_heads)
                            ctx->watcher_heads[one_id] = EXPR_NULL;
                        /* one_id is singleton, don't set unassigned bit */

                        /* one == cond + not_cond */
                        prop_add_bounds_add_32(ctx, one_id, cond_var_id,
                                               not_cond_id, 0);
                    }

                    /* Set guard on else-propagator */
                    uint32_t last_prop_id = ctx->n_props - 2;
                    /* Actually we just added the add propagator, so the else
                     * propagator is further back. Track it properly. */
                    /* The else branch compiled a propagator, then we added
                     * the add propagator. The else propagator is at
                     * n_props - 2 (before the add prop we just created). */
                    if (ctx->prop_guard_vars && last_prop_id < ctx->n_prop_refs_capacity)
                        ctx->prop_guard_vars[last_prop_id] = not_cond_id;
                }
            }
        }

        return (then_rc > 0) ? 1 : 0;
    }

    /* ---- r == extend(a): zero/sign extend compilation ---- */
    if (k == EXPR_BINARY) {
        ExprBinary *e_ext = (ExprBinary *)zsp_pool_ptr(&sp->pool, root);
        if (e_ext->op == BIN_EQ && e_ext->lhs != EXPR_NULL && e_ext->rhs != EXPR_NULL) {
            ExprKind ext_lk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e_ext->lhs);
            ExprKind ext_rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e_ext->rhs);
            ExprRef ext_var_side = EXPR_NULL, ext_ext_side = EXPR_NULL;
            if (ext_lk == EXPR_VAR && ext_rk == EXPR_EXTEND) {
                ext_var_side = e_ext->lhs; ext_ext_side = e_ext->rhs;
            } else if (ext_lk == EXPR_EXTEND && ext_rk == EXPR_VAR) {
                ext_var_side = e_ext->rhs; ext_ext_side = e_ext->lhs;
            }
            if (ext_var_side != EXPR_NULL && ext_ext_side != EXPR_NULL) {
                ExprVar *ev = (ExprVar *)zsp_pool_ptr(&sp->pool, ext_var_side);
                uint32_t r_id = ev->var_id;
                ExprExtend *ext = (ExprExtend *)zsp_pool_ptr(&sp->pool, ext_ext_side);
                uint32_t operand_id;
                if (_is_var(sp, ext->operand, &operand_id)) {
                    if (!ext->sign_extend) {
                        /* Zero-extend: r in [0, (1<<from_bits)-1] */
                        int64_t max_val = (ext->from_bits < 64)
                            ? ((int64_t)1 << ext->from_bits) - 1
                            : INT64_MAX;
                        if (ctx_tighten_lb64(ctx, r_id, 0) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_ub64(ctx, r_id, max_val) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_lb64(ctx, operand_id, 0) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_ub64(ctx, operand_id, max_val) == PROP_CONFLICT)
                            return -1;
                    } else {
                        /* Sign-extend: r in [-2^(from-1), 2^(from-1)-1] */
                        int64_t min_val = -((int64_t)1 << (ext->from_bits - 1));
                        int64_t max_val = ((int64_t)1 << (ext->from_bits - 1)) - 1;
                        if (ctx_tighten_lb64(ctx, r_id, min_val) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_ub64(ctx, r_id, max_val) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_lb64(ctx, operand_id, min_val) == PROP_CONFLICT)
                            return -1;
                        if (ctx_tighten_ub64(ctx, operand_id, max_val) == PROP_CONFLICT)
                            return -1;
                    }
                    /* Link r and operand via EQ propagator */
                    prop_add_bounds_eq_64(ctx, r_id, operand_id, 0);
                    return 1;
                }
            }
        }
    }


    /* ---- r == concat(hi, lo): bit concatenation ---- */
    if (k == EXPR_BINARY) {
        ExprBinary *e_cat = (ExprBinary *)zsp_pool_ptr(&sp->pool, root);
        if (e_cat->op == BIN_EQ && e_cat->lhs != EXPR_NULL && e_cat->rhs != EXPR_NULL) {
            ExprKind cat_lk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e_cat->lhs);
            ExprKind cat_rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e_cat->rhs);
            ExprRef cat_var_side = EXPR_NULL, cat_cat_side = EXPR_NULL;
            if (cat_lk == EXPR_VAR && cat_rk == EXPR_CONCAT) {
                cat_var_side = e_cat->lhs; cat_cat_side = e_cat->rhs;
            } else if (cat_lk == EXPR_CONCAT && cat_rk == EXPR_VAR) {
                cat_var_side = e_cat->rhs; cat_cat_side = e_cat->lhs;
            }
            if (cat_var_side != EXPR_NULL && cat_cat_side != EXPR_NULL) {
                ExprVar *ev = (ExprVar *)zsp_pool_ptr(&sp->pool, cat_var_side);
                uint32_t r_id = ev->var_id;
                ExprConcat *cat = (ExprConcat *)zsp_pool_ptr(&sp->pool, cat_cat_side);
                uint32_t hi_id, lo_id;
                if (_is_var(sp, cat->hi, &hi_id) && _is_var(sp, cat->lo, &lo_id)) {
                    prop_add_bounds_concat_64(ctx, r_id, hi_id, lo_id,
                                              cat->lo_width, 0);
                    return 1;
                }
            }
        }
    }

    /* ---- EXPR_ITE as value inside EQ: r == (cond ? a : b) ---- */
    if (k == EXPR_BINARY) {
        ExprBinary *e = (ExprBinary *)zsp_pool_ptr(&sp->pool, root);
        if (e->op == BIN_EQ) {
            /* Check for var == ITE pattern */
            ExprRef var_side = EXPR_NULL, ite_side = EXPR_NULL;
            if (e->lhs != EXPR_NULL && e->rhs != EXPR_NULL) {
                ExprKind lk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->lhs);
                ExprKind rk = *(ExprKind *)zsp_pool_ptr(&sp->pool, e->rhs);
                if (lk == EXPR_VAR && rk == EXPR_ITE) {
                    var_side = e->lhs; ite_side = e->rhs;
                } else if (lk == EXPR_ITE && rk == EXPR_VAR) {
                    var_side = e->rhs; ite_side = e->lhs;
                }
            }
            if (var_side != EXPR_NULL && ite_side != EXPR_NULL) {
                ExprVar *ev = (ExprVar *)zsp_pool_ptr(&sp->pool, var_side);
                uint32_t r_id = ev->var_id;
                ExprITE *ite = (ExprITE *)zsp_pool_ptr(&sp->pool, ite_side);

                uint32_t cond_id, a_id, b_id;
                if (_is_var(sp, ite->cond, &cond_id) &&
                    _is_var(sp, ite->then_e, &a_id) &&
                    _is_var(sp, ite->else_e, &b_id)) {
                    prop_add_ite_value_64(ctx, r_id, cond_id, a_id, b_id, 0);
                    return 1;
                }
            }
        }
    }

    /* EXPR_UNARY, EXPR_IN_SET etc. are not yet handled natively */
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

    /* Allocate a contiguous Variable[n + VAR_SLACK] in the static pool.
       The extra slack allows incremental variable addition (Phase S2). */
    #define VAR_SLACK 64u
    uint32_t capacity = n + VAR_SLACK;
    uint32_t vars_ref = zsp_pool_alloc(&ctx->pool,
                                        capacity * (uint32_t)sizeof(Variable),
                                        (uint32_t)_Alignof(Variable));
    if (vars_ref == EXPR_NULL) return -1;

    ctx->vars           = (Variable *)zsp_pool_ptr(&ctx->pool, vars_ref);
    ctx->n_vars         = n;
    ctx->n_vars_capacity = capacity;

    /* Zero-initialise the whole array (including slack) */
    memset(ctx->vars, 0, capacity * sizeof(Variable));

    /* Initialise unassigned_mask -- set after variable init below */
    ctx->unassigned_mask = 0;

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
        if (vs->width < 32) {
            /* Narrow (< 32 bits): always fits in tier-0 int32 storage */
            _init_tier0(v, vs->width, flags, vs->lo, vs->hi);
            rc = 0;
        } else if (vs->width == 32 && (flags & VAR_SIGNED)) {
            /* Signed 32-bit: full range fits in int32 */
            _init_tier0(v, vs->width, flags, vs->lo, vs->hi);
            rc = 0;
        } else if (vs->width == 32 && !(flags & VAR_SIGNED)) {
            /* Unsigned 32-bit: promote to tier-1 so values > 0x7FFFFFFF
             * are stored correctly without int32 overflow */
            rc = _init_tier1(ctx, v, vs->width, flags, vs->lo, vs->hi);
        } else if (vs->width <= 64) {
            rc = _init_tier1(ctx, v, vs->width, flags, vs->lo, vs->hi);
        } else {
            rc = _init_tier2(ctx, v, vs->width, flags, vs->lo, vs->hi);
        }

        if (rc != 0) return -1;
        ref = vs->next;
    }

    /* Build unassigned_mask: set bits for non-singleton variables */
    if (n <= 64) {
        uint64_t mask = 0;
        for (uint32_t i = 0; i < n; i++) {
            Variable *v = &ctx->vars[i];
            int64_t lo = var_lo64(ctx, v);
            int64_t hi = var_hi64(ctx, v);
            if (lo != hi) mask |= (1ULL << i);
        }
        ctx->unassigned_mask = mask;
    }

    /* ---- Allocate per-variable watcher head array (with slack) ---- */
    uint32_t wh_ref = zsp_pool_alloc(&ctx->pool,
                                      capacity * (uint32_t)sizeof(uint32_t),
                                      (uint32_t)_Alignof(uint32_t));
    if (wh_ref == EXPR_NULL) return -1;
    ctx->watcher_heads = (uint32_t *)zsp_pool_ptr(&ctx->pool, wh_ref);
    for (uint32_t i = 0; i < capacity; i++) ctx->watcher_heads[i] = EXPR_NULL;

    /* ---- Allocate prop_refs array for checkpoint/restore ---- */
    #define PROP_SLACK 128u
    uint32_t pr_cap = sp->n_constraints + sp->n_alldiffs + PROP_SLACK;
    uint32_t pr_ref = zsp_pool_alloc(&ctx->pool,
                                      pr_cap * (uint32_t)sizeof(uint32_t),
                                      (uint32_t)_Alignof(uint32_t));
    if (pr_ref == EXPR_NULL) return -1;
    ctx->prop_refs = (uint32_t *)zsp_pool_ptr(&ctx->pool, pr_ref);
    ctx->n_prop_refs_capacity = pr_cap;
    for (uint32_t i = 0; i < pr_cap; i++) ctx->prop_refs[i] = EXPR_NULL;

    /* ---- Allocate guard-variable side table ---- */
    uint32_t gv_ref = zsp_pool_alloc(&ctx->pool,
                                      pr_cap * (uint32_t)sizeof(uint32_t),
                                      (uint32_t)_Alignof(uint32_t));
    if (gv_ref == EXPR_NULL) return -1;
    ctx->prop_guard_vars = (uint32_t *)zsp_pool_ptr(&ctx->pool, gv_ref);
    for (uint32_t i = 0; i < pr_cap; i++) ctx->prop_guard_vars[i] = EXPR_NULL;

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


    /* ---- Walk AllDiffSpec list -> create AllDifferent propagators ---- */
    ExprRef adref = sp->allDiff_head;
    while (adref != EXPR_NULL) {
        AllDiffSpec *ad = (AllDiffSpec *)zsp_pool_ptr(&sp->pool, adref);
        uint32_t *vids = (uint32_t *)(ad + 1);
        uint32_t pref = prop_add_all_different(ctx, ad->n_vars, vids, 1);
        if (pref == EXPR_NULL) return -1;
        adref = ad->next;
    }
    /* ---- Save initial variable state for solver_reset() ---- */
    {
        uint32_t iv_ref = zsp_pool_alloc(&ctx->pool,
                                          n * (uint32_t)sizeof(Variable),
                                          (uint32_t)_Alignof(Variable));
        if (iv_ref != EXPR_NULL) {
            ctx->initial_vars = (Variable *)zsp_pool_ptr(&ctx->pool, iv_ref);
            memcpy(ctx->initial_vars, ctx->vars, n * sizeof(Variable));
            ctx->initial_n_vars = n;

            /* For tier-1 vars, also save the WideBounds64 contents.
             * The initial_vars[] have correct holes_offset values, so we
             * can restore from there. The WideBounds64 data at those offsets
             * will be overwritten during solving. Save a copy. */
            for (uint32_t i = 0; i < n; i++) {
                Variable *v = &ctx->vars[i];
                if (VAR_IS_TIER1(v->flags) && v->holes_offset != 0) {
                    /* The initial_vars[i].holes_offset points to the same
                     * WideBounds64 in the pool. We need a separate copy. */
                    uint32_t wb_ref = zsp_pool_alloc(&ctx->pool,
                                                      (uint32_t)sizeof(WideBounds64),
                                                      (uint32_t)_Alignof(WideBounds64));
                    if (wb_ref != EXPR_NULL) {
                        WideBounds64 *src = (WideBounds64 *)zsp_pool_ptr(&ctx->pool, v->holes_offset);
                        WideBounds64 *dst = (WideBounds64 *)zsp_pool_ptr(&ctx->pool, wb_ref);
                        *dst = *src;
                        /* Point initial_vars[i].holes_offset to the saved copy */
                        ctx->initial_vars[i].holes_offset = wb_ref;
                    }
                }
            }
        } else {
            ctx->initial_vars   = NULL;
            ctx->initial_n_vars = 0;
        }
    }

    /* Return count of uncompiled constraints (0 = all compiled, >0 = partial,
       negative values reserved for hard errors above). */
    return n_uncompiled;
}

/* ------------------------------------------------------------------ */
/* solver_add_constraint — incremental constraint addition            */
/* ------------------------------------------------------------------ */

int solver_add_constraint(SolveCtx *ctx, SolveProblem *aux_sp) {
    int n_uncompiled = 0;

    /* ---- Add new variables ---- */
    ExprRef vref = aux_sp->vars_head;
    while (vref != EXPR_NULL) {
        VarSpec *vs = (VarSpec *)zsp_pool_ptr(&aux_sp->pool, vref);
        uint32_t id = vs->var_id;

        if (id >= ctx->n_vars_capacity) return -1;  /* no room */

        if (id >= ctx->n_vars) {
            /* New variable: initialise it */
            Variable *v = &ctx->vars[id];
            uint8_t flags = 0;
            if (vs->is_signed) flags |= VAR_SIGNED;

            int rc;
            if (vs->width < 32) {
                _init_tier0(v, vs->width, flags, vs->lo, vs->hi);
                rc = 0;
            } else if (vs->width == 32 && (flags & VAR_SIGNED)) {
                _init_tier0(v, vs->width, flags, vs->lo, vs->hi);
                rc = 0;
            } else if (vs->width == 32 && !(flags & VAR_SIGNED)) {
                rc = _init_tier1(ctx, v, vs->width, flags, vs->lo, vs->hi);
            } else if (vs->width <= 64) {
                rc = _init_tier1(ctx, v, vs->width, flags, vs->lo, vs->hi);
            } else {
                rc = _init_tier2(ctx, v, vs->width, flags, vs->lo, vs->hi);
            }
            if (rc != 0) return -1;

            /* Update n_vars to include this and any gaps */
            if (id + 1 > ctx->n_vars)
                ctx->n_vars = id + 1;
        }
        vref = vs->next;
    }

    /* ---- Compile new constraints ---- */
    ExprRef cref = aux_sp->constraints_head;
    while (cref != EXPR_NULL) {
        ConstraintSpec *cs = (ConstraintSpec *)zsp_pool_ptr(&aux_sp->pool, cref);
        int r = _compile_constraint(ctx, aux_sp, cs->root);
        if (r < 0) return -2;  /* UNSAT at compile time */
        if (r == 0) n_uncompiled++;
        cref = cs->next;
    }

    /* ---- Compile new AllDifferent constraints ---- */
    ExprRef adref = aux_sp->allDiff_head;
    while (adref != EXPR_NULL) {
        AllDiffSpec *ad = (AllDiffSpec *)zsp_pool_ptr(&aux_sp->pool, adref);
        uint32_t *vids = (uint32_t *)(ad + 1);
        uint32_t pref = prop_add_all_different(ctx, ad->n_vars, vids, 1);
        if (pref == EXPR_NULL) return -1;
        adref = ad->next;
    }

    /* ---- Run propagation to fixpoint ---- */
    PropResult pr = solver_propagate(ctx);
    if (pr == PROP_CONFLICT) return -2;

    return n_uncompiled;
}
