#ifndef ZSP_PROPAGATOR_H
#define ZSP_PROPAGATOR_H

#include <stdint.h>
#include "zsp_pool.h"   /* EXPR_NULL */

#ifdef __cplusplus
extern "C" {
#endif

/* forward declarations */
typedef struct Propagator Propagator;
typedef struct SolveCtx   SolveCtx;

/* ------------------------------------------------------------------ */
/* PropResult                                                          */
/* ------------------------------------------------------------------ */
typedef enum {
    PROP_OK       = 0,   /* propagated; may have tightened some bounds  */
    PROP_CONFLICT = 1,   /* empty domain detected                       */
    PROP_ENTAILED = 2,   /* constraint permanently satisfied            */
} PropResult;

/* ------------------------------------------------------------------ */
/* Propagator flags                                                    */
/* ------------------------------------------------------------------ */
#define PROP_FLAG_ENTAILED  0x01u
#define PROP_FLAG_IN_QUEUE  0x02u

/* ------------------------------------------------------------------ */
/* Event types (for watcher registration — Phase 7+)                  */
/* ------------------------------------------------------------------ */
#define EVT_LB   0x01u
#define EVT_UB   0x02u
#define EVT_SING 0x04u
#define EVT_HOLE 0x08u
#define EVT_ANY  (EVT_LB | EVT_UB | EVT_SING | EVT_HOLE)

/* ------------------------------------------------------------------ */
/* PropWatchSect                                                       */
/*                                                                     */
/* Fixed-size block immediately after the 16-byte Propagator header.  */
/* Contains watched-variable IDs and per-variable linked-list chains. */
/*                                                                     */
/* MAX_PROP_WATCHES = 4 supports up to 4-variable propagators.        */
/* (BoundsAdd needs 3: r, a, b; BitSlice/Extend need 2.)             */
/* ------------------------------------------------------------------ */
#define MAX_PROP_WATCHES 4u

typedef struct {
    uint32_t n_watches;                     /* number of watched variables */
    uint32_t var_ids[MAX_PROP_WATCHES];     /* watched variable IDs        */
    uint32_t next_watchers[MAX_PROP_WATCHES]; /* per-var watcher chain      */
} PropWatchSect;  /* 4 + 16 + 16 = 36 bytes */

/* ------------------------------------------------------------------ */
/* Propagator header — 16 bytes                                        */
/*                                                                     */
/* Immediately after the header lies a PropWatchSect (36 bytes),      */
/* followed by template-specific data.                                 */
/* ------------------------------------------------------------------ */
struct Propagator {
    PropResult (*fire)(Propagator *self, SolveCtx *ctx);  /* 8 bytes */
    uint32_t    queue_next;  /* pool offset to next in FIFO queue    */
    uint16_t    prop_id;
    uint8_t     priority;    /* 0=high (fires first), 15=low         */
    uint8_t     flags;       /* PROP_FLAG_* bits                     */
};  /* 16 bytes */

/* ------------------------------------------------------------------ */
/* PropQueue — 16-level priority FIFO                                  */
/*                                                                     */
/* Priority 0 fires first (cheap bounds propagators).                 */
/* non_empty_mask bit i set ↔ level i has at least one entry.        */
/* ------------------------------------------------------------------ */
typedef struct {
    uint16_t  non_empty_mask;
    uint16_t  _pad[3];
    uint32_t  heads[16];    /* pool offsets to queue head per level */
    uint32_t  tails[16];    /* pool offsets to queue tail per level */
} PropQueue;  /* 2 + 6 + 64 + 64 = 136 bytes */

/* ------------------------------------------------------------------ */
/* Template structs                                                    */
/*                                                                     */
/* Layout (all templates):                                            */
/*   [Propagator hdr (16)] [PropWatchSect ws (36)] [template data]   */
/*                                                                     */
/* var_ids[0..n-1] are the primary variable references.  For binary   */
/* templates: var_ids[0]=lhs, var_ids[1]=rhs.  For ternary (Add etc.) */
/* var_ids[0]=result, var_ids[1]=operand_a, var_ids[2]=operand_b.    */
/* ------------------------------------------------------------------ */

typedef struct { Propagator hdr; PropWatchSect ws; } BoundsLE_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsLT_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsEQ_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsNE_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsAdd_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsMul_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsDiv_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsMod_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } UnaryNeg_32_t;

/* BoundsLE/LT/EQ/NE/Add/Mul/Div/Mod/UnaryNeg _64 variants */
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsLE_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsLT_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsEQ_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsNE_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsAdd_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsMul_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsDiv_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } BoundsMod_64_t;
typedef struct { Propagator hdr; PropWatchSect ws; } UnaryNeg_64_t;

/* InSet_32: x ∈ {elems[0], …, elems[n_elems-1]}
   Template data: n_elems (4), _pad (4), then int32_t elems[] */
typedef struct {
    Propagator  hdr;
    PropWatchSect ws;
    uint32_t    n_elems;
    uint32_t    _pad;
    /* int32_t elems[n_elems] follow immediately */
} InSet_32_t;

/* InSet_64 */
typedef struct {
    Propagator  hdr;
    PropWatchSect ws;
    uint32_t    n_elems;
    uint32_t    _pad;
    /* int64_t elems[n_elems] follow immediately */
} InSet_64_t;

/* Implication_32: guard → (var ≤/≥ bound)
   var_ids[0]=guard, var_ids[1]=var
   is_ub=1 → enforce UB (var ≤ bound); is_ub=0 → enforce LB (var ≥ bound) */
typedef struct {
    Propagator  hdr;
    PropWatchSect ws;
    int32_t     bound;
    uint8_t     is_ub;
    uint8_t     _pad[3];
} Implication_32_t;

/* Reification_32: guard ↔ (x ≤ y) */
typedef struct { Propagator hdr; PropWatchSect ws; } Reification_32_t;
typedef struct { Propagator hdr; PropWatchSect ws; } Reification_64_t;

/* BitSlice_32: r = a[hi_bit:lo_bit]  var_ids[0]=r, var_ids[1]=a */
typedef struct {
    Propagator  hdr;
    PropWatchSect ws;
    uint8_t     hi_bit;
    uint8_t     lo_bit;
    uint8_t     _pad[2];
} BitSlice_32_t;
typedef struct {
    Propagator  hdr;
    PropWatchSect ws;
    uint8_t     hi_bit;
    uint8_t     lo_bit;
    uint8_t     _pad[2];
} BitSlice_64_t;

/* ------------------------------------------------------------------ */
/* Queue management                                                    */
/* ------------------------------------------------------------------ */

/** Enqueue a propagator (no-op if already queued or entailed). */
void prop_enqueue(SolveCtx *ctx, uint32_t prop_ref);

/* ------------------------------------------------------------------ */
/* Domain-tightening functions                                        */
/*                                                                     */
/* These are the canonical way for propagators to modify variable     */
/* domains.  Each function:                                           */
/*   1. No-ops if the new bound is not tighter.                       */
/*   2. Records the old bound in the trail and applies the change.    */
/*   3. Returns PROP_CONFLICT if the domain becomes empty.            */
/*   4. Wakes all watchers on the variable.                           */
/* ------------------------------------------------------------------ */

PropResult ctx_tighten_lb32(SolveCtx *ctx, uint32_t var_id, int32_t new_lb);
PropResult ctx_tighten_ub32(SolveCtx *ctx, uint32_t var_id, int32_t new_ub);
PropResult ctx_tighten_lb64(SolveCtx *ctx, uint32_t var_id, int64_t new_lb);
PropResult ctx_tighten_ub64(SolveCtx *ctx, uint32_t var_id, int64_t new_ub);

/* ------------------------------------------------------------------ */
/* Propagation loop                                                    */
/* ------------------------------------------------------------------ */

/**
 * Run the propagation loop until fixedpoint or conflict.
 *
 * @return PROP_OK on fixedpoint, PROP_CONFLICT if any domain empties.
 */
PropResult solver_propagate(SolveCtx *ctx);

/* ------------------------------------------------------------------ */
/* Propagator constructors                                             */
/*                                                                     */
/* Each constructor:                                                   */
/*   - Allocates the propagator in ctx's static pool.                 */
/*   - Registers watchers for all variable IDs.                       */
/*   - Enqueues the propagator for an initial firing.                 */
/*   - Returns the pool offset (PropRef), or EXPR_NULL on failure.    */
/* ------------------------------------------------------------------ */

uint32_t prop_add_bounds_le_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_lt_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_eq_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_ne_32(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_add_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_mul_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_div_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_mod_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_unary_neg_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint8_t priority);

uint32_t prop_add_bounds_le_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_lt_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_eq_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_ne_64(SolveCtx *ctx, uint32_t x_id, uint32_t y_id, uint8_t priority);
uint32_t prop_add_bounds_add_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_mul_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_div_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_bounds_mod_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint32_t b_id, uint8_t priority);
uint32_t prop_add_unary_neg_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id, uint8_t priority);

/**
 * InSet: x ∈ {elems[0], …, elems[n_elems-1]}.
 * @param elems  Array of int32_t allowed values.
 */
uint32_t prop_add_in_set_32(SolveCtx *ctx, uint32_t x_id,
                             uint32_t n_elems, const int32_t *elems,
                             uint8_t priority);
uint32_t prop_add_in_set_64(SolveCtx *ctx, uint32_t x_id,
                             uint32_t n_elems, const int64_t *elems,
                             uint8_t priority);

/**
 * Implication: guard=true → (var ≤ bound) if is_ub, or (var ≥ bound) otherwise.
 * var_ids[0]=guard, var_ids[1]=var.
 */
uint32_t prop_add_implication_32(SolveCtx *ctx,
                                  uint32_t guard_id, uint32_t var_id,
                                  int32_t bound, uint8_t is_ub,
                                  uint8_t priority);

uint32_t prop_add_reification_32(SolveCtx *ctx, uint32_t guard_id,
                                   uint32_t x_id, uint32_t y_id,
                                   uint8_t priority);
uint32_t prop_add_reification_64(SolveCtx *ctx, uint32_t guard_id,
                                   uint32_t x_id, uint32_t y_id,
                                   uint8_t priority);

uint32_t prop_add_bit_slice_32(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                uint8_t hi_bit, uint8_t lo_bit, uint8_t priority);
uint32_t prop_add_bit_slice_64(SolveCtx *ctx, uint32_t r_id, uint32_t a_id,
                                uint8_t hi_bit, uint8_t lo_bit, uint8_t priority);

#ifdef __cplusplus
}
#endif

#endif /* ZSP_PROPAGATOR_H */
