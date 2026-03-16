#include <stdint.h>
#include "zsp_propagator.h"
#include "zsp_ctx.h"
#include "zsp_trail.h"

/* ------------------------------------------------------------------ */
/* Internal: wake all watchers on a variable                          */
/* ------------------------------------------------------------------ */

static void _wake_var(SolveCtx *ctx, uint32_t var_id) {
    uint32_t prop_ref = ctx->watcher_heads[var_id];
    while (prop_ref != EXPR_NULL) {
        Propagator    *p  = (Propagator *)zsp_pool_ptr(&ctx->pool, prop_ref);
        PropWatchSect *ws = (PropWatchSect *)((char *)p + sizeof(Propagator));

        /* Find next in chain for this variable before potentially re-queuing */
        uint32_t next = EXPR_NULL;
        for (uint32_t i = 0; i < ws->n_watches; i++) {
            if (ws->var_ids[i] == var_id) {
                next = ws->next_watchers[i];
                break;
            }
        }

        prop_enqueue(ctx, prop_ref);
        prop_ref = next;
    }
}

/* ------------------------------------------------------------------ */
/* Queue management                                                    */
/* ------------------------------------------------------------------ */

void prop_enqueue(SolveCtx *ctx, uint32_t prop_ref) {
    Propagator *p = (Propagator *)zsp_pool_ptr(&ctx->pool, prop_ref);
    if (p->flags & (PROP_FLAG_IN_QUEUE | PROP_FLAG_ENTAILED)) return;

    p->flags    |= PROP_FLAG_IN_QUEUE;
    p->queue_next = EXPR_NULL;

    uint32_t lvl = p->priority;
    if (ctx->queue.heads[lvl] == EXPR_NULL) {
        ctx->queue.heads[lvl] = prop_ref;
        ctx->queue.tails[lvl] = prop_ref;
        ctx->queue.non_empty_mask |= (uint16_t)(1u << lvl);
    } else {
        Propagator *tail = (Propagator *)zsp_pool_ptr(&ctx->pool,
                                                       ctx->queue.tails[lvl]);
        tail->queue_next      = prop_ref;
        ctx->queue.tails[lvl] = prop_ref;
    }
}

/* ------------------------------------------------------------------ */
/* Domain-tightening                                                   */
/* ------------------------------------------------------------------ */

PropResult ctx_tighten_lb32(SolveCtx *ctx, uint32_t var_id, int32_t new_lb) {
    Variable *v = &ctx->vars[var_id];
    if (new_lb <= v->lo) return PROP_OK;

    trail_record_lb(ctx, var_id, (int64_t)new_lb);  /* applies change */

    if (v->lo > v->hi) return PROP_CONFLICT;
    if (ctx->watcher_heads) _wake_var(ctx, var_id);
    return PROP_OK;
}

PropResult ctx_tighten_ub32(SolveCtx *ctx, uint32_t var_id, int32_t new_ub) {
    Variable *v = &ctx->vars[var_id];
    if (new_ub >= v->hi) return PROP_OK;

    trail_record_ub(ctx, var_id, (int64_t)new_ub);  /* applies change */

    if (v->lo > v->hi) return PROP_CONFLICT;
    if (ctx->watcher_heads) _wake_var(ctx, var_id);
    return PROP_OK;
}

PropResult ctx_tighten_lb64(SolveCtx *ctx, uint32_t var_id, int64_t new_lb) {
    Variable *v = &ctx->vars[var_id];
    int64_t curr = var_lo64(ctx, v);
    if (new_lb <= curr) return PROP_OK;

    trail_record_lb(ctx, var_id, new_lb);

    /* check conflict */
    int64_t lo = var_lo64(ctx, v);
    int64_t hi = var_hi64(ctx, v);
    if (lo > hi) return PROP_CONFLICT;
    if (ctx->watcher_heads) _wake_var(ctx, var_id);
    return PROP_OK;
}

PropResult ctx_tighten_ub64(SolveCtx *ctx, uint32_t var_id, int64_t new_ub) {
    Variable *v = &ctx->vars[var_id];
    int64_t curr = var_hi64(ctx, v);
    if (new_ub >= curr) return PROP_OK;

    trail_record_ub(ctx, var_id, new_ub);

    int64_t lo = var_lo64(ctx, v);
    int64_t hi = var_hi64(ctx, v);
    if (lo > hi) return PROP_CONFLICT;
    if (ctx->watcher_heads) _wake_var(ctx, var_id);
    return PROP_OK;
}

/* ------------------------------------------------------------------ */
/* Propagation loop                                                    */
/* ------------------------------------------------------------------ */

PropResult solver_propagate(SolveCtx *ctx) {
    while (ctx->queue.non_empty_mask) {
        /* Lowest set bit = highest priority level with entries */
        uint32_t lvl = (uint32_t)__builtin_ctz(ctx->queue.non_empty_mask);

        /* Dequeue head */
        uint32_t    prop_ref = ctx->queue.heads[lvl];
        Propagator *p        = (Propagator *)zsp_pool_ptr(&ctx->pool, prop_ref);

        ctx->queue.heads[lvl] = p->queue_next;
        if (ctx->queue.heads[lvl] == EXPR_NULL) {
            ctx->queue.tails[lvl]      = EXPR_NULL;
            ctx->queue.non_empty_mask &= (uint16_t)~(1u << lvl);
        }
        p->queue_next  = EXPR_NULL;
        p->flags      &= (uint8_t)~PROP_FLAG_IN_QUEUE;

        if (p->flags & PROP_FLAG_ENTAILED) continue;

        PropResult r = p->fire(p, ctx);
        if (r == PROP_CONFLICT) return PROP_CONFLICT;
        if (r == PROP_ENTAILED) p->flags |= PROP_FLAG_ENTAILED;
    }
    return PROP_OK;
}
