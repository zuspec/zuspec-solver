#include <stdint.h>
#include <string.h>
#include "zsp_propagator.h"
#include "zsp_ctx.h"

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

#define PROP_WS(p) ((PropWatchSect *)((char *)(p) + sizeof(Propagator)))

static int32_t i32_min(int32_t a, int32_t b) { return a < b ? a : b; }
static int32_t i32_max(int32_t a, int32_t b) { return a > b ? a : b; }
static int64_t i64_min(int64_t a, int64_t b) { return a < b ? a : b; }
static int64_t i64_max(int64_t a, int64_t b) { return a > b ? a : b; }

/* ------------------------------------------------------------------ */
/* Common constructor helpers                                          */
/* ------------------------------------------------------------------ */

/* Register this propagator into the watcher chain for var_id at slot i. */
static void _register_watcher(SolveCtx *ctx, uint32_t prop_ref,
                               uint32_t var_id, uint32_t slot) {
    PropWatchSect *ws = PROP_WS(
        (Propagator *)zsp_pool_ptr(&ctx->pool, prop_ref));
    ws->next_watchers[slot]     = ctx->watcher_heads[var_id];
    ctx->watcher_heads[var_id]  = prop_ref;
}

/* Allocate a propagator block of `size` bytes from the static pool,
   fill the header fields, fill the PropWatchSect, and enqueue it.
   Returns pool offset or EXPR_NULL. */
static uint32_t _alloc_prop(SolveCtx *ctx,
                             PropResult (*fire)(Propagator *, SolveCtx *),
                             uint8_t priority,
                             uint32_t n_watches, const uint32_t *var_ids,
                             uint32_t total_bytes) {
    uint32_t ref = zsp_pool_alloc(&ctx->pool, total_bytes, 8u);
    if (ref == EXPR_NULL) return EXPR_NULL;

    Propagator *p    = (Propagator *)zsp_pool_ptr(&ctx->pool, ref);
    memset(p, 0, total_bytes);

    p->fire       = fire;
    p->queue_next = EXPR_NULL;
    p->prop_id    = (uint16_t)ctx->n_props++;
    p->priority   = priority;
    p->flags      = 0;

    PropWatchSect *ws = PROP_WS(p);
    ws->n_watches = n_watches;
    for (uint32_t i = 0; i < n_watches; i++) {
        ws->var_ids[i]      = var_ids[i];
        ws->next_watchers[i] = EXPR_NULL;
    }
    for (uint32_t i = 0; i < n_watches; i++) {
        _register_watcher(ctx, ref, var_ids[i], i);
    }

    prop_enqueue(ctx, ref);
    return ref;
}

/* ------------------------------------------------------------------ */
/* BoundsLE_32:  x ≤ y                                                */
/*   ws.var_ids[0] = x, ws.var_ids[1] = y                            */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_le_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];
    Variable      *x   = &ctx->vars[xid];
    Variable      *y   = &ctx->vars[yid];

    PropResult r;
    if ((r = ctx_tighten_ub32(ctx, xid, y->hi)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb32(ctx, yid, x->lo)) != PROP_OK) return r;

    if (x->hi <= y->lo) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_bounds_le_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id,
                                uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_le_32, priority, 2, ids,
                       sizeof(BoundsLE_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsLT_32:  x < y                                                */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_lt_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];
    Variable      *x   = &ctx->vars[xid];
    Variable      *y   = &ctx->vars[yid];

    PropResult r;
    if ((r = ctx_tighten_ub32(ctx, xid, y->hi - 1)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb32(ctx, yid, x->lo + 1)) != PROP_OK) return r;

    if (x->hi < y->lo) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_bounds_lt_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id,
                                uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_lt_32, priority, 2, ids,
                       sizeof(BoundsLT_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsEQ_32:  x == y                                               */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_eq_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];
    Variable      *x   = &ctx->vars[xid];
    Variable      *y   = &ctx->vars[yid];

    PropResult r;
    int32_t lo = i32_max(x->lo, y->lo);
    int32_t hi = i32_min(x->hi, y->hi);
    if (lo > hi) return PROP_CONFLICT;

    if ((r = ctx_tighten_lb32(ctx, xid, lo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub32(ctx, xid, hi)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb32(ctx, yid, lo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub32(ctx, yid, hi)) != PROP_OK) return r;

    if (x->lo == x->hi && y->lo == y->hi) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_bounds_eq_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id,
                                uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_eq_32, priority, 2, ids,
                       sizeof(BoundsEQ_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsNE_32:  x != y  (singleton-only propagation)                */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_ne_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];
    Variable      *x   = &ctx->vars[xid];
    Variable      *y   = &ctx->vars[yid];

    PropResult r;
    /* if x is singleton, remove from y */
    if (x->lo == x->hi) {
        int32_t v = x->lo;
        if (y->lo == v) {
            if ((r = ctx_tighten_lb32(ctx, yid, v + 1)) != PROP_OK) return r;
        } else if (y->hi == v) {
            if ((r = ctx_tighten_ub32(ctx, yid, v - 1)) != PROP_OK) return r;
        }
    }
    /* if y is singleton, remove from x */
    if (y->lo == y->hi) {
        int32_t v = y->lo;
        if (x->lo == v) {
            if ((r = ctx_tighten_lb32(ctx, xid, v + 1)) != PROP_OK) return r;
        } else if (x->hi == v) {
            if ((r = ctx_tighten_ub32(ctx, xid, v - 1)) != PROP_OK) return r;
        }
    }
    /* entailed if domains don't overlap */
    if (x->hi < y->lo || y->hi < x->lo) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_bounds_ne_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id,
                                uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_ne_32, priority, 2, ids,
                       sizeof(BoundsNE_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsAdd_32:  r = a + b                                           */
/*   var_ids[0]=r, var_ids[1]=a, var_ids[2]=b                        */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_add_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];
    uint32_t       bid = ws->var_ids[2];
    Variable      *r   = &ctx->vars[rid];
    Variable      *a   = &ctx->vars[aid];
    Variable      *b   = &ctx->vars[bid];

    PropResult res;
    /* Use 64-bit intermediate arithmetic to avoid int32 overflow when
     * summing large unsigned 32-bit bounds (e.g. 0x7FFFFFFF + 0x02000000). */
    int64_t alo = a->lo, ahi = a->hi, blo = b->lo, bhi = b->hi;
    int64_t rlo = r->lo, rhi = r->hi;

    /* r in [a.lo+b.lo, a.hi+b.hi], clamped to int32 range */
    int64_t fwd_lo = alo + blo;
    int64_t fwd_hi = ahi + bhi;
    if (fwd_lo > INT32_MAX) fwd_lo = INT32_MAX;
    if (fwd_lo < INT32_MIN) fwd_lo = INT32_MIN;
    if (fwd_hi > INT32_MAX) fwd_hi = INT32_MAX;
    if (fwd_hi < INT32_MIN) fwd_hi = INT32_MIN;
    if ((res = ctx_tighten_lb32(ctx, rid, (int32_t)fwd_lo)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, rid, (int32_t)fwd_hi)) != PROP_OK) return res;

    /* Re-read r bounds after possible tightening */
    rlo = r->lo; rhi = r->hi;

    /* a in [r.lo-b.hi, r.hi-b.lo] */
    int64_t a_new_lo = rlo - bhi;
    int64_t a_new_hi = rhi - blo;
    if (a_new_lo < INT32_MIN) a_new_lo = INT32_MIN;
    if (a_new_hi > INT32_MAX) a_new_hi = INT32_MAX;
    if ((res = ctx_tighten_lb32(ctx, aid, (int32_t)a_new_lo)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, aid, (int32_t)a_new_hi)) != PROP_OK) return res;

    /* b in [r.lo-a.hi, r.hi-a.lo] */
    alo = a->lo; ahi = a->hi;  /* re-read after tightening */
    int64_t b_new_lo = rlo - ahi;
    int64_t b_new_hi = rhi - alo;
    if (b_new_lo < INT32_MIN) b_new_lo = INT32_MIN;
    if (b_new_hi > INT32_MAX) b_new_hi = INT32_MAX;
    if ((res = ctx_tighten_lb32(ctx, bid, (int32_t)b_new_lo)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, bid, (int32_t)b_new_hi)) != PROP_OK) return res;

    if (r->lo == r->hi && a->lo == a->hi && b->lo == b->hi) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_bounds_add_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                  uint32_t b_id, uint8_t priority) {
    uint32_t ids[3] = { r_id, a_id, b_id };
    return _alloc_prop(ctx, _fire_bounds_add_32, priority, 3, ids,
                       sizeof(BoundsAdd_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsMul_32:  r = a * b  (conservative: only when one is fixed)  */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_mul_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];
    uint32_t       bid = ws->var_ids[2];
    Variable      *r   = &ctx->vars[rid];
    Variable      *a   = &ctx->vars[aid];
    Variable      *b   = &ctx->vars[bid];

    PropResult res;
    if (a->lo == a->hi) {
        int32_t k = a->lo;
        if (k > 0) {
            if ((res = ctx_tighten_lb32(ctx, rid, k * b->lo)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, rid, k * b->hi)) != PROP_OK) return res;
            /* Backward: b = r / k */
            if ((res = ctx_tighten_lb32(ctx, bid, r->lo / k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, bid, r->hi / k)) != PROP_OK) return res;
        } else if (k < 0) {
            if ((res = ctx_tighten_lb32(ctx, rid, k * b->hi)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, rid, k * b->lo)) != PROP_OK) return res;
            /* Backward: b = r / k (reversed due to negative k) */
            if ((res = ctx_tighten_lb32(ctx, bid, r->hi / k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, bid, r->lo / k)) != PROP_OK) return res;
        }
    }
    if (b->lo == b->hi) {
        int32_t k = b->lo;
        if (k > 0) {
            if ((res = ctx_tighten_lb32(ctx, rid, a->lo * k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, rid, a->hi * k)) != PROP_OK) return res;
            /* Backward: a = r / k */
            if ((res = ctx_tighten_lb32(ctx, aid, r->lo / k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, aid, r->hi / k)) != PROP_OK) return res;
        } else if (k < 0) {
            if ((res = ctx_tighten_lb32(ctx, rid, a->hi * k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, rid, a->lo * k)) != PROP_OK) return res;
            /* Backward: a = r / k (reversed due to negative k) */
            if ((res = ctx_tighten_lb32(ctx, aid, r->hi / k)) != PROP_OK) return res;
            if ((res = ctx_tighten_ub32(ctx, aid, r->lo / k)) != PROP_OK) return res;
        }
    }
    return PROP_OK;
}

uint32_t prop_add_bounds_mul_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                  uint32_t b_id, uint8_t priority) {
    uint32_t ids[3] = { r_id, a_id, b_id };
    return _alloc_prop(ctx, _fire_bounds_mul_32, priority, 3, ids,
                       sizeof(BoundsMul_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsDiv_32:  r = a / b  (conservative, b > 0)                   */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_div_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];
    uint32_t       bid = ws->var_ids[2];
    Variable      *a   = &ctx->vars[aid];
    Variable      *b   = &ctx->vars[bid];

    PropResult res;
    if (b->lo == b->hi && b->lo > 0) {
        int32_t k = b->lo;
        /* integer division: floor(a.lo/k) .. floor(a.hi/k) */
        int32_t rlo = a->lo / k;
        int32_t rhi = a->hi / k;
        if (rlo > rhi) { int32_t t = rlo; rlo = rhi; rhi = t; }
        if ((res = ctx_tighten_lb32(ctx, rid, rlo)) != PROP_OK) return res;
        if ((res = ctx_tighten_ub32(ctx, rid, rhi)) != PROP_OK) return res;
    }
    (void)aid; (void)bid;
    return PROP_OK;
}

uint32_t prop_add_bounds_div_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                  uint32_t b_id, uint8_t priority) {
    uint32_t ids[3] = { r_id, a_id, b_id };
    return _alloc_prop(ctx, _fire_bounds_div_32, priority, 3, ids,
                       sizeof(BoundsDiv_32_t));
}

/* ------------------------------------------------------------------ */
/* BoundsMod_32:  r = a % b  (conservative, b > 0)                   */
/* ------------------------------------------------------------------ */

static PropResult _fire_bounds_mod_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       bid = ws->var_ids[2];
    Variable      *b   = &ctx->vars[bid];

    PropResult res;
    if (b->lo == b->hi && b->lo > 0) {
        int32_t k = b->lo;
        if ((res = ctx_tighten_lb32(ctx, rid, 0))     != PROP_OK) return res;
        if ((res = ctx_tighten_ub32(ctx, rid, k - 1)) != PROP_OK) return res;
    }
    return PROP_OK;
}

uint32_t prop_add_bounds_mod_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                  uint32_t b_id, uint8_t priority) {
    uint32_t ids[3] = { r_id, a_id, b_id };
    return _alloc_prop(ctx, _fire_bounds_mod_32, priority, 3, ids,
                       sizeof(BoundsMod_32_t));
}

/* ------------------------------------------------------------------ */
/* UnaryNeg_32:  r = -a                                               */
/* ------------------------------------------------------------------ */

static PropResult _fire_unary_neg_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];
    Variable      *r   = &ctx->vars[rid];
    Variable      *a   = &ctx->vars[aid];

    PropResult res;
    if ((res = ctx_tighten_lb32(ctx, rid, -a->hi)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, rid, -a->lo)) != PROP_OK) return res;
    if ((res = ctx_tighten_lb32(ctx, aid, -r->hi)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, aid, -r->lo)) != PROP_OK) return res;

    if (r->lo == r->hi && a->lo == a->hi) return PROP_ENTAILED;
    return PROP_OK;
}

uint32_t prop_add_unary_neg_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                uint8_t priority) {
    uint32_t ids[2] = { r_id, a_id };
    return _alloc_prop(ctx, _fire_unary_neg_32, priority, 2, ids,
                       sizeof(UnaryNeg_32_t));
}

/* ------------------------------------------------------------------ */
/* InSet_32:  x ∈ {elems[0], …}                                      */
/* ------------------------------------------------------------------ */

static PropResult _fire_in_set_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws    = PROP_WS(self);
    uint32_t       xid   = ws->var_ids[0];
    Variable      *x     = &ctx->vars[xid];
    InSet_32_t    *iself = (InSet_32_t *)self;
    int32_t       *elems = (int32_t *)((char *)self + sizeof(InSet_32_t));
    uint32_t       n     = iself->n_elems;

    PropResult res;
    /* Find min/max in set that overlap [x->lo, x->hi] */
    int32_t new_lo = INT32_MAX, new_hi = INT32_MIN;
    for (uint32_t i = 0; i < n; i++) {
        if (elems[i] >= x->lo && elems[i] <= x->hi) {
            if (elems[i] < new_lo) new_lo = elems[i];
            if (elems[i] > new_hi) new_hi = elems[i];
        }
    }
    if (new_lo == INT32_MAX) return PROP_CONFLICT;  /* no valid element */

    if ((res = ctx_tighten_lb32(ctx, xid, new_lo)) != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, xid, new_hi)) != PROP_OK) return res;
    return PROP_OK;
}

uint32_t prop_add_in_set_32(SolveCtx *ctx, uint32_t x_id,
                              uint32_t n_elems, const int32_t *elems,
                              uint8_t priority) {
    uint32_t ids[1] = { x_id };
    uint32_t sz = (uint32_t)(sizeof(InSet_32_t) + n_elems * sizeof(int32_t));
    uint32_t ref = _alloc_prop(ctx, _fire_in_set_32, priority, 1, ids, sz);
    if (ref == EXPR_NULL) return EXPR_NULL;

    InSet_32_t *p = (InSet_32_t *)zsp_pool_ptr(&ctx->pool, ref);
    p->n_elems = n_elems;
    int32_t   *dst = (int32_t *)((char *)p + sizeof(InSet_32_t));
    memcpy(dst, elems, n_elems * sizeof(int32_t));
    return ref;
}

/* ------------------------------------------------------------------ */
/* Implication_32:  guard → (var ≤/≥ bound)                          */
/*   var_ids[0]=guard, var_ids[1]=var                                 */
/* ------------------------------------------------------------------ */

static PropResult _fire_implication_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect    *ws    = PROP_WS(self);
    Implication_32_t *iself = (Implication_32_t *)self;
    uint32_t          gid   = ws->var_ids[0];
    uint32_t          vid   = ws->var_ids[1];
    Variable         *g     = &ctx->vars[gid];

    /* guard is definitively false → entailed */
    if (g->hi == 0) return PROP_ENTAILED;

    /* guard is definitively true → enforce bound */
    if (g->lo == 1) {
        PropResult r;
        if (iself->is_ub)
            r = ctx_tighten_ub32(ctx, vid, iself->bound);
        else
            r = ctx_tighten_lb32(ctx, vid, iself->bound);
        return r;
    }
    return PROP_OK;
}

uint32_t prop_add_implication_32(SolveCtx *ctx,
                                   uint32_t guard_id, uint32_t var_id,
                                   int32_t bound, uint8_t is_ub,
                                   uint8_t priority) {
    uint32_t ids[2] = { guard_id, var_id };
    uint32_t ref = _alloc_prop(ctx, _fire_implication_32, priority, 2, ids,
                                sizeof(Implication_32_t));
    if (ref == EXPR_NULL) return EXPR_NULL;

    Implication_32_t *p = (Implication_32_t *)zsp_pool_ptr(&ctx->pool, ref);
    p->bound = bound;
    p->is_ub = is_ub;
    return ref;
}

/* ------------------------------------------------------------------ */
/* Reification_32:  guard ↔ (x ≤ y)  (stub — only one direction)    */
/* ------------------------------------------------------------------ */

static PropResult _fire_reification_32(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       gid = ws->var_ids[0];
    uint32_t       xid = ws->var_ids[1];
    uint32_t       yid = ws->var_ids[2];
    Variable      *g   = &ctx->vars[gid];
    Variable      *x   = &ctx->vars[xid];
    Variable      *y   = &ctx->vars[yid];

    /* if guard=1, enforce x ≤ y */
    if (g->lo == 1) {
        PropResult r;
        if ((r = ctx_tighten_ub32(ctx, xid, y->hi)) != PROP_OK) return r;
        if ((r = ctx_tighten_lb32(ctx, yid, x->lo)) != PROP_OK) return r;
    }
    /* if guard=0, x > y must hold — tighten lb of x above y.hi */
    if (g->hi == 0) {
        PropResult r;
        if ((r = ctx_tighten_lb32(ctx, xid, y->hi + 1)) != PROP_OK) return r;
    }
    return PROP_OK;
}

uint32_t prop_add_reification_32(SolveCtx *ctx, uint32_t guard_id,
                                   uint32_t x_id, uint32_t y_id,
                                   uint8_t priority) {
    uint32_t ids[3] = { guard_id, x_id, y_id };
    return _alloc_prop(ctx, _fire_reification_32, priority, 3, ids,
                       sizeof(Reification_32_t));
}

/* ------------------------------------------------------------------ */
/* BitSlice_32:  r = a[hi_bit:lo_bit]                                 */
/* ------------------------------------------------------------------ */

static PropResult _fire_bit_slice_32(Propagator *self, SolveCtx *ctx) {
    BitSlice_32_t *bself = (BitSlice_32_t *)self;
    PropWatchSect *ws    = PROP_WS(self);
    uint32_t       rid   = ws->var_ids[0];

    uint32_t width = (uint32_t)(bself->hi_bit - bself->lo_bit + 1);
    int32_t  max_v = (width < 32) ? (int32_t)((1u << width) - 1) : INT32_MAX;

    PropResult res;
    if ((res = ctx_tighten_lb32(ctx, rid, 0))     != PROP_OK) return res;
    if ((res = ctx_tighten_ub32(ctx, rid, max_v)) != PROP_OK) return res;
    return PROP_OK;
}

uint32_t prop_add_bit_slice_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                uint8_t hi_bit, uint8_t lo_bit,
                                uint8_t priority) {
    uint32_t ids[2] = { r_id, a_id };
    uint32_t ref = _alloc_prop(ctx, _fire_bit_slice_32, priority, 2, ids,
                                sizeof(BitSlice_32_t));
    if (ref == EXPR_NULL) return EXPR_NULL;

    BitSlice_32_t *p = (BitSlice_32_t *)zsp_pool_ptr(&ctx->pool, ref);
    p->hi_bit = hi_bit;
    p->lo_bit = lo_bit;
    return ref;
}

/* ================================================================== */
/* _64 variants                                                       */
/* ================================================================== */

static PropResult _fire_bounds_le_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];

    PropResult r;
    if ((r = ctx_tighten_ub64(ctx, xid, var_hi64(ctx, &ctx->vars[yid]))) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, yid, var_lo64(ctx, &ctx->vars[xid]))) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_bounds_le_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_le_64, priority, 2, ids, sizeof(BoundsLE_64_t));
}

static PropResult _fire_bounds_lt_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];

    PropResult r;
    if ((r = ctx_tighten_ub64(ctx, xid, var_hi64(ctx, &ctx->vars[yid]) - 1)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, yid, var_lo64(ctx, &ctx->vars[xid]) + 1)) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_bounds_lt_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_lt_64, priority, 2, ids, sizeof(BoundsLT_64_t));
}

static PropResult _fire_bounds_eq_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];

    int64_t lo = i64_max(var_lo64(ctx, &ctx->vars[xid]), var_lo64(ctx, &ctx->vars[yid]));
    int64_t hi = i64_min(var_hi64(ctx, &ctx->vars[xid]), var_hi64(ctx, &ctx->vars[yid]));
    if (lo > hi) return PROP_CONFLICT;

    PropResult r;
    if ((r = ctx_tighten_lb64(ctx, xid, lo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, xid, hi)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, yid, lo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, yid, hi)) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_bounds_eq_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_eq_64, priority, 2, ids, sizeof(BoundsEQ_64_t));
}

static PropResult _fire_bounds_ne_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       xid = ws->var_ids[0];
    uint32_t       yid = ws->var_ids[1];
    int64_t xlo = var_lo64(ctx, &ctx->vars[xid]), xhi = var_hi64(ctx, &ctx->vars[xid]);
    int64_t ylo = var_lo64(ctx, &ctx->vars[yid]), yhi = var_hi64(ctx, &ctx->vars[yid]);

    PropResult r;
    if (xlo == xhi) {
        int64_t v = xlo;
        if (ylo == v) { if ((r = ctx_tighten_lb64(ctx, yid, v+1)) != PROP_OK) return r; }
        else if (yhi == v) { if ((r = ctx_tighten_ub64(ctx, yid, v-1)) != PROP_OK) return r; }
    }
    if (ylo == yhi) {
        int64_t v = ylo;
        if (xlo == v) { if ((r = ctx_tighten_lb64(ctx, xid, v+1)) != PROP_OK) return r; }
        else if (xhi == v) { if ((r = ctx_tighten_ub64(ctx, xid, v-1)) != PROP_OK) return r; }
    }
    return PROP_OK;
}
uint32_t prop_add_bounds_ne_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority) {
    uint32_t ids[2] = { x_id, y_id };
    return _alloc_prop(ctx, _fire_bounds_ne_64, priority, 2, ids, sizeof(BoundsNE_64_t));
}

static PropResult _fire_bounds_add_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];
    uint32_t       bid = ws->var_ids[2];
    int64_t rlo = var_lo64(ctx,&ctx->vars[rid]), rhi = var_hi64(ctx,&ctx->vars[rid]);
    int64_t alo = var_lo64(ctx,&ctx->vars[aid]), ahi = var_hi64(ctx,&ctx->vars[aid]);
    int64_t blo = var_lo64(ctx,&ctx->vars[bid]), bhi = var_hi64(ctx,&ctx->vars[bid]);

    PropResult r;
    if ((r = ctx_tighten_lb64(ctx, rid, alo+blo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, rid, ahi+bhi)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, aid, rlo-bhi)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, aid, rhi-blo)) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, bid, rlo-ahi)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, bid, rhi-alo)) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_bounds_add_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority) {
    uint32_t ids[3] = { r_id, a_id, b_id };
    return _alloc_prop(ctx, _fire_bounds_add_64, priority, 3, ids, sizeof(BoundsAdd_64_t));
}

/* Stubs for Mul/Div/Mod _64 (conservative: no propagation) */
static PropResult _fire_noop(Propagator *self, SolveCtx *ctx) { (void)self;(void)ctx; return PROP_OK; }
uint32_t prop_add_bounds_mul_64(SolveCtx *c, uint32_t r, uint32_t a, uint32_t b, uint8_t p) { uint32_t ids[3]={r,a,b}; return _alloc_prop(c,_fire_noop,p,3,ids,sizeof(BoundsMul_64_t)); }
uint32_t prop_add_bounds_div_64(SolveCtx *c, uint32_t r, uint32_t a, uint32_t b, uint8_t p) { uint32_t ids[3]={r,a,b}; return _alloc_prop(c,_fire_noop,p,3,ids,sizeof(BoundsDiv_64_t)); }
uint32_t prop_add_bounds_mod_64(SolveCtx *c, uint32_t r, uint32_t a, uint32_t b, uint8_t p) { uint32_t ids[3]={r,a,b}; return _alloc_prop(c,_fire_noop,p,3,ids,sizeof(BoundsMod_64_t)); }

static PropResult _fire_unary_neg_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws  = PROP_WS(self);
    uint32_t       rid = ws->var_ids[0];
    uint32_t       aid = ws->var_ids[1];

    PropResult r;
    if ((r = ctx_tighten_lb64(ctx, rid, -var_hi64(ctx,&ctx->vars[aid]))) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, rid, -var_lo64(ctx,&ctx->vars[aid]))) != PROP_OK) return r;
    if ((r = ctx_tighten_lb64(ctx, aid, -var_hi64(ctx,&ctx->vars[rid]))) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, aid, -var_lo64(ctx,&ctx->vars[rid]))) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_unary_neg_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint8_t priority) {
    uint32_t ids[2] = { r_id, a_id };
    return _alloc_prop(ctx, _fire_unary_neg_64, priority, 2, ids, sizeof(UnaryNeg_64_t));
}

static PropResult _fire_in_set_64(Propagator *self, SolveCtx *ctx) {
    PropWatchSect *ws    = PROP_WS(self);
    uint32_t       xid   = ws->var_ids[0];
    InSet_64_t    *iself = (InSet_64_t *)self;
    int64_t       *elems = (int64_t *)((char *)self + sizeof(InSet_64_t));
    uint32_t       n     = iself->n_elems;
    int64_t xlo = var_lo64(ctx,&ctx->vars[xid]), xhi = var_hi64(ctx,&ctx->vars[xid]);

    int64_t new_lo = INT64_MAX, new_hi = INT64_MIN;
    for (uint32_t i = 0; i < n; i++) {
        if (elems[i] >= xlo && elems[i] <= xhi) {
            if (elems[i] < new_lo) new_lo = elems[i];
            if (elems[i] > new_hi) new_hi = elems[i];
        }
    }
    if (new_lo == INT64_MAX) return PROP_CONFLICT;

    PropResult r;
    if ((r = ctx_tighten_lb64(ctx, xid, new_lo)) != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, xid, new_hi)) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_in_set_64(SolveCtx *ctx, uint32_t x_id,
                              uint32_t n_elems, const int64_t *elems,
                              uint8_t priority) {
    uint32_t ids[1] = { x_id };
    uint32_t sz  = (uint32_t)(sizeof(InSet_64_t) + n_elems * sizeof(int64_t));
    uint32_t ref = _alloc_prop(ctx, _fire_in_set_64, priority, 1, ids, sz);
    if (ref == EXPR_NULL) return EXPR_NULL;
    InSet_64_t *p = (InSet_64_t *)zsp_pool_ptr(&ctx->pool, ref);
    p->n_elems = n_elems;
    int64_t *dst = (int64_t *)((char *)p + sizeof(InSet_64_t));
    memcpy(dst, elems, n_elems * sizeof(int64_t));
    return ref;
}

uint32_t prop_add_reification_64(SolveCtx *ctx, uint32_t guard_id,
                                   uint32_t x_id, uint32_t y_id,
                                   uint8_t priority) {
    uint32_t ids[3] = { guard_id, x_id, y_id };
    return _alloc_prop(ctx, _fire_noop, priority, 3, ids, sizeof(Reification_64_t));
}

static PropResult _fire_bit_slice_64(Propagator *self, SolveCtx *ctx) {
    BitSlice_64_t *bself = (BitSlice_64_t *)self;
    PropWatchSect *ws    = PROP_WS(self);
    uint32_t       rid   = ws->var_ids[0];
    uint32_t width = (uint32_t)(bself->hi_bit - bself->lo_bit + 1);
    int64_t  max_v = (width < 64) ? (int64_t)((1ULL << width) - 1) : INT64_MAX;

    PropResult r;
    if ((r = ctx_tighten_lb64(ctx, rid, 0))     != PROP_OK) return r;
    if ((r = ctx_tighten_ub64(ctx, rid, max_v)) != PROP_OK) return r;
    return PROP_OK;
}
uint32_t prop_add_bit_slice_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                uint8_t hi_bit, uint8_t lo_bit, uint8_t priority) {
    uint32_t ids[2] = { r_id, a_id };
    uint32_t ref = _alloc_prop(ctx, _fire_bit_slice_64, priority, 2, ids, sizeof(BitSlice_64_t));
    if (ref == EXPR_NULL) return EXPR_NULL;
    BitSlice_64_t *p = (BitSlice_64_t *)zsp_pool_ptr(&ctx->pool, ref);
    p->hi_bit = hi_bit; p->lo_bit = lo_bit;
    return ref;
}

/* ------------------------------------------------------------------ */
/* DisjClause: (v0 op0 c0) OR ... OR (vN opN cN)                     */
/*                                                                     */
/* Fire logic: check each clause against current bounds.              */
/* "definitely false" means every value in the domain violates it.    */
/* When all but one are definitely false, enforce the survivor.       */
/* ------------------------------------------------------------------ */

/** Is comparison (lo..hi) op constant definitely false? */
static int _clause_definitely_false(Variable *v, SolveCtx *ctx,
                                     uint32_t op, int64_t c) {
    int64_t lo = var_lo64(ctx, v);
    int64_t hi = var_hi64(ctx, v);
    /* Negate the op and check if negation is definitely true */
    switch (op) {
    case BIN_EQ:   return (lo > c || hi < c);        /* !(lo <= c <= hi) */
    case BIN_NEQ:  return (lo == hi && lo == c);      /* singleton == c */
    case BIN_LT:   return (lo >= c);                  /* all >= c => none < c */
    case BIN_LTE:  return (lo > c);
    case BIN_GT:   return (hi <= c);
    case BIN_GTE:  return (hi < c);
    default:       return 0;
    }
}

/** Enforce clause: tighten var's domain so (var op constant) can hold. */
static PropResult _enforce_clause(SolveCtx *ctx, uint32_t var_id,
                                   uint32_t op, int64_t c) {
    switch (op) {
    case BIN_EQ:
        if (ctx_tighten_lb64(ctx, var_id, c) == PROP_CONFLICT) return PROP_CONFLICT;
        if (ctx_tighten_ub64(ctx, var_id, c) == PROP_CONFLICT) return PROP_CONFLICT;
        return PROP_OK;
    case BIN_NEQ:
        /* Can only tighten if domain is singleton or c is at a bound */
        {
            int64_t lo = var_lo64(ctx, &ctx->vars[var_id]);
            int64_t hi = var_hi64(ctx, &ctx->vars[var_id]);
            if (lo == c) return ctx_tighten_lb64(ctx, var_id, c + 1);
            if (hi == c) return ctx_tighten_ub64(ctx, var_id, c - 1);
        }
        return PROP_OK;
    case BIN_LT:   return ctx_tighten_ub64(ctx, var_id, c - 1);
    case BIN_LTE:  return ctx_tighten_ub64(ctx, var_id, c);
    case BIN_GT:   return ctx_tighten_lb64(ctx, var_id, c + 1);
    case BIN_GTE:  return ctx_tighten_lb64(ctx, var_id, c);
    default:       return PROP_OK;
    }
}

static PropResult _fire_disj_clause(Propagator *self, SolveCtx *ctx) {
    DisjClause_t *dc = (DisjClause_t *)self;
    uint32_t n = dc->n_clauses;

    /* Count how many clauses are definitely false */
    uint32_t n_false = 0;
    uint32_t survivor = 0;  /* index of the last non-false clause */
    for (uint32_t i = 0; i < n; i++) {
        Variable *v = &ctx->vars[dc->clauses[i].var_id];
        if (_clause_definitely_false(v, ctx, dc->clauses[i].op,
                                     dc->clauses[i].constant)) {
            n_false++;
        } else {
            survivor = i;
        }
    }

    if (n_false == n) return PROP_CONFLICT;  /* all false */
    if (n_false < n - 1) return PROP_OK;     /* 2+ undecided */

    /* Exactly one survivor — enforce it */
    return _enforce_clause(ctx, dc->clauses[survivor].var_id,
                           dc->clauses[survivor].op,
                           dc->clauses[survivor].constant);
}

uint32_t prop_add_disj_clause(SolveCtx *ctx,
                               uint32_t n_clauses,
                               const uint32_t *var_ids,
                               const uint32_t *ops,
                               const int64_t *constants,
                               uint8_t priority) {
    if (n_clauses == 0 || n_clauses > MAX_DISJ_CLAUSES) return EXPR_NULL;

    uint32_t ref = _alloc_prop(ctx, _fire_disj_clause, priority,
                                n_clauses, var_ids, sizeof(DisjClause_t));
    if (ref == EXPR_NULL) return EXPR_NULL;

    DisjClause_t *dc = (DisjClause_t *)zsp_pool_ptr(&ctx->pool, ref);
    dc->n_clauses = n_clauses;
    for (uint32_t i = 0; i < n_clauses; i++) {
        dc->clauses[i].var_id   = var_ids[i];
        dc->clauses[i].op       = ops[i];
        dc->clauses[i].constant = constants[i];
    }
    return ref;
}
