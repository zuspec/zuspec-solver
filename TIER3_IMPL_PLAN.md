# Tier 3 Implementation Plan: Arrays, Iterative Constraints, and System Functions

**Date:** 2026-04-13
**Target codebase:** `packages/zuspec-solver`
**Scope:** Native solver support for Verilator's array, foreach, reduction,
and system-function constraint patterns.

## Design Principles

The zuspec-solver is a **scalar constraint solver**. It operates on flat
variable IDs with integer domains. It does not model SV classes, types,
or memory layout. The **Verilator integration layer** (code-gen or DPI
shim) is responsible for:

- Flattening struct members and array elements into scalar variables.
- Unrolling `foreach` loops into per-element constraints at known sizes.
- Orchestrating multi-phase solving for dynamic-size arrays.

The solver's job is to provide the primitives that make this flattening
efficient and correct. This plan adds those primitives.

---

## Part A: Iterative Constraint Support (foreach / arrays)

### A.1 Problem Analysis

Every `foreach` constraint in SV boils down to one of two cases at
randomize-time:

1. **Fixed-size array**: The array length is known before `randomize()`.
   The integration layer unrolls the foreach at compile time, creating
   one constraint per element. No solver changes needed -- just emit
   N copies of the constraint body with `i` replaced by 0..N-1.

2. **Dynamic-size array**: The array length is itself a `rand` variable
   (or constrained by one). The Verilator pattern is:
   ```sv
   rand int unsigned m_length;
   rand byte m_data[];
   constraint c_length { m_length inside {[1:16]}; }
   constraint c_data   { m_data.size == m_length; }
   constraint c_elem   { foreach (m_data[i]) m_data[i] > 0; }
   ```
   This requires **phased solving**: solve `m_length` first, allocate
   element variables, then add per-element constraints and re-solve.

### A.2 Phased Solving Protocol

The solver already has the building blocks: `solver_compile`,
`solver_solve`, `solver_pin_var`, `solver_add_constraint`,
`solver_checkpoint`, `solver_restore`, and `solver_reset`.

The integration layer executes this protocol:

```
Phase 1:  Build SolveProblem with scalar vars + size vars + size constraints.
          Compile. Solve. Read size values.

Phase 2:  For each dynamic array, use the solved size to:
            - Add element variables via solver_add_constraint (aux problem).
            - Add per-element constraints (unrolled foreach).
            - Pin the size variable to its Phase-1 value.
          Re-solve with the augmented problem.

Phase 3:  Read all element values. Done.
```

#### What the solver needs

The existing `solver_add_constraint()` API handles Phase 2, but it has
a limitation: it only compiles constraint expressions that were built
into the auxiliary `SolveProblem`. It cannot add new propagators
referencing variables from Phase 1 without a corresponding VarSpec.

**Change needed:** `solver_add_constraint()` must accept VarSpec entries
whose `var_id` already exists in the context (a "re-declaration"). For
existing vars, it should skip re-initialization and just use the current
domain. This is already partially handled (the code checks `id >= ctx->n_vars`),
but we need to ensure the constraint compiler can reference both old
and new variables.

**New API:**
```c
// Resize a dynamic array's element variables.
// Creates n_elems new variables (elem_var_base .. elem_var_base+n_elems-1)
// with the given width/signedness/bounds.
// Returns 0 on success, -1 on capacity overflow.
int solver_add_array_vars(SolveCtx *ctx,
                          uint32_t elem_var_base,
                          uint32_t n_elems,
                          uint8_t  width,
                          uint8_t  is_signed,
                          int64_t  lo,
                          int64_t  hi);
```

This is a convenience wrapper that bulk-creates variables without
building a full auxiliary SolveProblem. It is much cheaper for the
common "add N identical element variables" pattern.

### A.3 Array Element Indexing with Rand Index

Pattern: `selected_value == data[idx]` where `idx` is rand.

This is an **element-select** constraint: `r == array[i]` where `i` is
a variable. The Verilator test `t_constraint_rand_array_index.v` uses
`solve idx before selected_value` to make this tractable.

**Solver approach:** The integration layer handles this via phased
solving (solve `idx` first, then pin it and add the equality). No new
solver primitive is needed -- the checkpoint/pin/add-constraint protocol
handles it.

For the case where `solve...before` is NOT specified, the integration
layer can still decompose this into an ITE chain:
```
ITE(idx==0, r==data[0],
  ITE(idx==1, r==data[1],
    ITE(idx==2, r==data[2], ...)))
```
The solver already handles nested ITE. For small arrays (N <= 16) this
is practical; for larger arrays the phased approach is required.

**New expression node:**
```c
EXPR_ARRAY_SELECT = 10  // r = array_base[index_var]
```
```c
typedef struct {
    ExprKind kind;          // EXPR_ARRAY_SELECT
    uint32_t base_var_id;   // first element variable ID
    uint32_t n_elems;       // number of elements
    ExprRef  index;         // index expression (var or const)
} ExprArraySelect;
```

The compiler lowers `EXPR_ARRAY_SELECT` into an ITE chain at compile time
(for small N) or flags it for phased solving (for large N). This is more
efficient than having the integration layer build the ITE chain in the
SolveProblem pool.

---

## Part B: Array Reduction Methods

### B.1 Problem Analysis

SV array reduction methods in constraints:

| Method | Semantics | Frequency |
|---|---|---|
| `arr.sum()` | Sum of all elements | Very common |
| `arr.sum() with (expr)` | Sum of expr(item) for each element | Common |
| `arr.product()` | Product of all elements | Occasional |
| `arr.and()` / `.or()` / `.xor()` | Bitwise reduction | Occasional |
| `arr.sum() with (int'(item == val))` | Count occurrences | Common |

All reductions over **fixed-size** arrays can be unrolled at constraint
setup time by the integration layer. For example:

```sv
arr.sum() == 200   // arr has 4 elements
```
becomes:
```
tmp_sum == arr[0] + arr[1] + arr[2] + arr[3]
tmp_sum == 200
```

This requires chaining ADD propagators. The solver already supports
`r == a + b` via `prop_add_bounds_add_32/64`.

### B.2 Native Sum-Chain Propagator

Unrolling `arr.sum() == K` into pairwise ADD propagators creates a
deep chain of temporaries: `t1 = a[0]+a[1]; t2 = t1+a[2]; t3 = t2+a[3]; t3 == K`.
For N elements this creates N-1 temporary variables and N-1 propagators.
That works, but a dedicated **SumEq** propagator would be more efficient:

```c
EXPR_SUM = 11  // result == var_ids[0] + var_ids[1] + ... + var_ids[n-1]
```
```c
typedef struct {
    ExprKind kind;       // EXPR_SUM
    ExprRef  result;     // result variable ExprRef
    uint32_t n_vars;     // number of summand variables
    // uint32_t var_ids[n_vars] follow in pool
} ExprSum;
```

**Propagator: `prop_add_sum_eq_32/64`**

Watches N+1 variables (result + N summands). Propagation:
- Forward: `result_lo = sum(xi_lo)`, `result_hi = sum(xi_hi)`.
- Backward: for each `xi`: `xi_lo = result_lo - sum(xj_hi for j!=i)`,
  `xi_hi = result_hi - sum(xj_lo for j!=i)`.

This is O(N) per firing vs O(N) for the chain approach, but with better
constant factors (one propagator instead of N-1) and tighter bounds
(global backward propagation instead of one-hop).

**Also needed for `.sum() with (expr)`:** The integration layer evaluates
`expr` for each element, creating a temporary variable `ti = expr(item_i)`.
Then the sum constraint becomes `result == t0 + t1 + ... + t_{n-1}`.
The `with` clause transformation is entirely in the integration layer;
the solver sees only the sum-of-variables constraint.

### B.3 Product, AND, OR, XOR Reductions

These are lower frequency. The integration layer can unroll them into
pairwise chains using existing propagators:

- **product:** `t1 = a[0]*a[1]; t2 = t1*a[2]; ...` using MUL propagators.
- **and:** `t1 = a[0]&a[1]; t2 = t1&a[2]; ...` using BAND propagators.
- **or:** using BOR propagators.
- **xor:** using BXOR propagators.

No new solver primitives needed for these. The chain approach is
acceptable because these constraints are less frequent and typically on
small arrays.

### B.4 Occurrence Counting

Pattern: `arr.sum() with (int'(item == val)) == 3`

This counts how many elements equal `val`. The `with` clause produces a
boolean (0/1) per element. The sum of these booleans is the count.

Integration layer lowering:
```
for each element i:
    ti = (arr[i] == val) ? 1 : 0    // ITE or reification
result == t0 + t1 + ... + t_{n-1}   // SumEq propagator
result == 3
```

The `ti` variables are 1-bit (domain [0,1]). The `result` variable has
domain [0, N]. The SumEq propagator from B.2 handles this efficiently.

For the special case where `val` is itself a rand variable (not a
constant), each `ti` requires a ReificationEq propagator linking `ti`
to `(arr[i] == val)`. The solver already has `prop_add_reification_eq_32`.

---

## Part C: System Functions in Constraints

### C.1 Problem Analysis

System functions appearing in Verilator constraint tests:

| Function | Semantics | Test file |
|---|---|---|
| `$countones(x)` | popcount (number of 1-bits) | `t_constraint_countones.v` |
| `$onehot(x)` | exactly one bit set (== `$countones(x) == 1`) | `t_constraint_sysfunc.v` |
| `$onehot0(x)` | at most one bit set (== `$countones(x) <= 1`) | `t_constraint_sysfunc.v` |
| `$countbits(x, '1)` | count bits matching control (generalized popcount) | `t_constraint_sysfunc.v` |
| `$countbits(x, '0)` | count zero bits (== width - popcount) | `t_constraint_sysfunc.v` |
| `$clog2(x)` | ceil(log2(x)) | `t_constraint_sysfunc.v` |

These are all **pure functions of a single variable**. They can be
implemented as native propagators that constrain the relationship
between the input variable and the result.

### C.2 Native Countones / Popcount Propagator

This is the most important system function. `$onehot` and `$onehot0`
are just `countones == 1` and `countones <= 1`.

**New expression node:**
```c
EXPR_COUNTONES = 12  // result == popcount(operand)
```
```c
typedef struct {
    ExprKind kind;       // EXPR_COUNTONES
    ExprRef  operand;    // input variable
} ExprCountones;
```

**Propagator: `prop_add_countones_32`**

Watches 2 variables: result (r) and operand (x).

Propagation rules for an N-bit `x`:

- **Forward (x → r):**
  - `r_lo = max(r_lo, min_popcount(x_lo, x_hi, N))`
  - `r_hi = min(r_hi, max_popcount(x_lo, x_hi, N))`
  - Where `min_popcount` / `max_popcount` compute the minimum/maximum
    number of 1-bits achievable by any value in [x_lo, x_hi].
  - Simple bounds: `r_lo >= popcount(x_lo & x_hi)` (bits that must be 1),
    `r_hi <= N - clz(x_hi)` (coarse upper bound).

- **Backward (r → x):**
  - If `r == 0`: `x == 0`.
  - If `r == N`: `x == (1 << N) - 1`.
  - If `r == 1`: `x` must be a power of two. Tighten `x_lo >= 1`.
  - If `r_hi < N`: some bits of `x` can be forced to 0 via `x_hi`
    tightening.

- **Singleton specialization:**
  - If `r` is singleton `k` and `x` is singleton `v`: verify
    `popcount(v) == k`, else CONFLICT.
  - If `r` is singleton `k`: for narrow `x` (width <= 16), enumerate
    valid domain boundaries.

For narrow variables (width <= 8), the propagator can use a 256-entry
lookup table for exact popcount.

### C.3 Native Clog2 Propagator

**New expression node:**
```c
EXPR_CLOG2 = 13  // result == ceil(log2(operand))
```

**Propagator: `prop_add_clog2_32`**

Watches 2 variables: result (r) and operand (x).

Propagation rules:
- Forward: `r_lo = clog2(x_lo)`, `r_hi = clog2(x_hi)`.
- Backward: if `r` singleton `k`: `x_lo >= (1 << (k-1)) + 1`,
  `x_hi <= (1 << k)`. Special case `k==0`: `x == 1`.

`clog2` is monotonic, making bounds propagation straightforward.

### C.4 Countbits Generalization

`$countbits(x, '1)` is identical to `$countones(x)`.
`$countbits(x, '0)` is `width - countones(x)`.

The integration layer rewrites `$countbits(x, '0)` as:
```
tmp == countones(x)
result == width - tmp
```

No additional propagator needed beyond Countones.

### C.5 Expression Node Summary

New ExprKind values:
```c
EXPR_ARRAY_SELECT = 10,  // result = base[index]
EXPR_SUM          = 11,  // result = sum of var_ids[]
EXPR_COUNTONES    = 12,  // result = popcount(operand)
EXPR_CLOG2        = 13,  // result = ceil(log2(operand))
```

---

## Part D: Dynamic Array Size Solving

### D.1 Problem Analysis

Verilator patterns for dynamic array size:

```sv
rand int m_data[];
constraint c { m_data.size() == 4; }             // constant size
constraint c { m_data.size() inside {[1:16]}; }  // range
constraint c { m_data.size() == m_length; }       // linked to rand var
```

The size itself is a scalar variable. The solver handles it as any
other variable. The challenge is what happens *after* the size is
solved: the integration layer must create element variables and
add element constraints.

### D.2 Integration Protocol

The integration layer executes:

```
1. Create a SolveProblem with:
   - All scalar variables (fields, size variables)
   - All scalar constraints (size ranges, cross-field constraints)
   - Allocate MAX_ELEMS element variables per dynamic array
     (with a sentinel domain that marks them inactive)
   - No element constraints yet

2. Compile and solve (Phase 1).
   - Read size values.

3. For each dynamic array:
   - Activate the first `size` element variables (tighten from
     sentinel domain to the real element domain).
   - Add per-element constraints via solver_add_constraint().

4. Re-solve (Phase 2).
   - Read all values.
```

**Alternative (simpler) approach:** Pre-allocate element variables up to
a configurable `max_array_size` (default 64). All elements exist from the
start. Elements beyond the solved size are pinned to 0 and their
constraints are guard-gated on `index < size`.

This approach requires:
- A `size_var` in the solver.
- For each element constraint: `ITE(i < size_var, constraint_i, true)`.
- The ReificationEq + guard-gating machinery already handles this.

Tradeoff: wastes some variables/propagators on unused elements, but
avoids the complexity of multi-phase solving. Suitable for small arrays
(N <= 64). For larger arrays, use the phased approach.

### D.3 solver_add_array_vars Implementation

```c
int solver_add_array_vars(SolveCtx *ctx,
                          uint32_t elem_var_base,
                          uint32_t n_elems,
                          uint8_t  width,
                          uint8_t  is_signed,
                          int64_t  lo,
                          int64_t  hi) {
    uint32_t end = elem_var_base + n_elems;
    if (end > ctx->n_vars_capacity) return -1;

    uint8_t flags = is_signed ? VAR_SIGNED : 0;
    for (uint32_t i = elem_var_base; i < end; i++) {
        Variable *v = &ctx->vars[i];
        if (width <= 32 && is_signed) {
            _init_tier0(v, width, flags, lo, hi);
        } else if (width <= 32) {
            _init_tier1(ctx, v, width, flags, lo, hi);
        } else {
            _init_tier1(ctx, v, width, flags, lo, hi);
        }
        if (ctx->watcher_heads) ctx->watcher_heads[i] = EXPR_NULL;
    }
    if (end > ctx->n_vars) ctx->n_vars = end;
    return 0;
}
```

---

## Part E: Struct Flattening

### E.1 Approach

The solver does not model structs. The integration layer flattens struct
members into scalar variables:

```sv
typedef struct {
    rand bit [7:0] byte_value;
    rand int int_value;
} MyStruct;

rand MyStruct s;
constraint c { s.byte_value inside {[0:100]}; }
```

becomes (in the solver):
```
var 0: s__byte_value  [0, 255]   width=8
var 1: s__int_value   [-2^31, 2^31-1]  width=32
constraint: var0 in [0, 100]  (compile-time bound tightening)
```

**No solver changes needed.** This is pure integration-layer work:
the Verilator backend walks the class/struct AST, assigns a flat
variable ID to each rand field (including nested struct members),
and emits constraints referencing those IDs.

For arrays of structs: each element's fields get their own variable IDs.
`rand MyStruct arr[3]` produces 6 variables (3 elements x 2 fields).

---

## Implementation Sprints

### Sprint T1: SumEq Propagator + solver_add_array_vars (Week 1) ✓ DONE

**Files to change:**

| File | Change |
|---|---|
| `src/c/zsp_problem.h` | Add `EXPR_SUM` to ExprKind. Add `ExprSum` struct. |
| `src/c/zsp_problem.c` | Add `expr_sum()` builder. |
| `src/c/zsp_propagator.h` | Add `SumEq_32_t` / `SumEq_64_t` structs and `prop_add_sum_eq_32/64`. |
| `src/c/zsp_prop_templates.c` | Implement `_fire_sum_eq_32` / `_fire_sum_eq_64`. |
| `src/c/zsp_compile.c` | Add `EXPR_SUM` case to `_compile_constraint`. |
| `src/c/zsp_search.h` | Add `solver_add_array_vars()` declaration. |
| `src/c/zsp_compile.c` | Implement `solver_add_array_vars()`. |
| `src/c/zsp_builder.h` / `.c` | Add `builder_expr_sum()`. |
| `src/zuspec/solver/problem.py` | Add `EXPR_SUM` constant. |
| `src/zuspec/solver/builder.py` | Add `expr_sum()` method. |

**Tests:**
- `tests/unit/test_sum_eq.py`:
  - `test_sum_4_vars_exact`: 4 vars, sum == 100. Verify.
  - `test_sum_4_vars_range`: sum in [10, 20]. Verify.
  - `test_sum_backward_tighten`: sum == 10, all vars [0, 10]. Verify backward propagation tightens each var's ub.
  - `test_sum_conflict`: sum == 100, all vars [0, 10], only 4 vars. max sum = 40 < 100. UNSAT.
  - `test_sum_with_existing_bounds`: vars have mixed domains. Verify sum respects all.
  - `test_sum_large_N`: 20 variables. Verify scales.
  - `test_sum_boolean_counting`: N boolean vars, sum == K. (occurrence counting pattern).
- `tests/unit/test_array_vars.py`:
  - `test_add_array_vars_basic`: Add 8 element vars, verify they exist.
  - `test_add_array_vars_with_constraints`: Add vars + element constraints, solve.
  - `test_add_array_vars_overflow`: Exceed capacity, verify -1 return.

### Sprint T2: Countones + Clog2 Propagators (Week 2) ✓ DONE

**Files to change:**

| File | Change |
|---|---|
| `src/c/zsp_problem.h` | Add `EXPR_COUNTONES`, `EXPR_CLOG2` to ExprKind. Add structs. |
| `src/c/zsp_problem.c` | Add `expr_countones()`, `expr_clog2()` builders. |
| `src/c/zsp_propagator.h` | Add `Countones_32_t`, `Clog2_32_t` structs and constructors. |
| `src/c/zsp_prop_templates.c` | Implement `_fire_countones_32`, `_fire_clog2_32`. |
| `src/c/zsp_compile.c` | Add `EXPR_COUNTONES` / `EXPR_CLOG2` cases. |
| `src/c/zsp_builder.h` / `.c` | Add builder methods. |
| `src/zuspec/solver/problem.py` | Add constants. |
| `src/zuspec/solver/builder.py` | Add methods. |

**Tests:**
- `tests/unit/test_countones.py`:
  - `test_countones_exact_1`: 8-bit var, countones == 1. Verify result is power-of-2.
  - `test_countones_exact_3`: 8-bit var, countones == 3. Verify popcount.
  - `test_countones_zero`: countones == 0 -> x == 0.
  - `test_countones_max`: countones == 8 -> x == 0xFF.
  - `test_countones_backward`: x is singleton, verify r is correct.
  - `test_countones_with_bounds`: x in [0x10, 0xF0], countones == 2. Verify.
  - `test_countones_wide_33bit`: 33-bit variable (tier-1). countones == 1.
  - `test_onehot_pattern`: countones == 1 applied to constraint. (mirrors `$onehot`)
  - `test_onehot0_pattern`: countones <= 1. (mirrors `$onehot0`)
  - `test_countbits_zeros`: width - countones == K. (mirrors `$countbits(x, '0)`)
- `tests/unit/test_clog2.py`:
  - `test_clog2_powers_of_2`: x is power of 2, verify r.
  - `test_clog2_non_power`: x=5, verify r=3.
  - `test_clog2_backward`: r is singleton 3, verify x in [5, 8].
  - `test_clog2_one`: x=1, r=0.
  - `test_clog2_with_equality`: r == clog2(x), x in [1, 255]. Solve, verify.

### Sprint T3: ArraySelect + Integration Layer Helpers (Week 3)

**Files to change:**

| File | Change |
|---|---|
| `src/c/zsp_problem.h` | Add `EXPR_ARRAY_SELECT` to ExprKind. Add `ExprArraySelect` struct. |
| `src/c/zsp_problem.c` | Add `expr_array_select()` builder. |
| `src/c/zsp_compile.c` | Add `EXPR_ARRAY_SELECT` lowering (ITE chain for N <= 16). |
| `src/c/zsp_builder.h` / `.c` | Add builder method. |
| `src/zuspec/solver/problem.py` | Add constant. |
| `src/zuspec/solver/builder.py` | Add method. |
| `src/zuspec/solver/ir_translator.py` | Add handling for array reduction constraints (sum-of-temps pattern). |

**Tests:**
- `tests/unit/test_array_select.py`:
  - `test_select_const_index`: r == arr[2], arr is 4 elements. Verify r == arr[2].
  - `test_select_var_index`: r == arr[idx], idx in [0,3]. Verify r == arr[solved_idx].
  - `test_select_with_constraint`: r == arr[idx], r > 10. Verify valid.
  - `test_select_out_of_bounds`: idx constrained beyond array size. UNSAT or handled gracefully.

### Sprint T4: Reduction Unrolling + Dynamic Array Protocol (Week 4)

This sprint focuses on the integration layer, not the solver core.

**Files to change:**

| File | Change |
|---|---|
| `src/zuspec/solver/ir_translator.py` | Add `SumReductionConstraint` handling: unroll into SumEq. |
| `src/zuspec/solver/ir_translator.py` | Add product/and/or/xor reduction unrolling into pairwise chains. |
| `src/zuspec/solver/c_bench_harness.py` | Support array reduction in C harness generation. |
| New: `src/zuspec/solver/array_solver.py` | Phased solving helper for dynamic arrays. |

**Tests:**
- `tests/unit/test_array_reduction.py`:
  - `test_sum_fixed_array`: 4-element array, sum == 200. Verify.
  - `test_sum_with_expr`: sum-of-booleans counting pattern.
  - `test_product_fixed_array`: 4-element array, product <= 100.
  - `test_and_reduction`: arr.and() == 0x50.
  - `test_or_reduction`: arr.or() has bit 3 set.
  - `test_xor_reduction`: arr.xor() != 0.
- `tests/unit/test_dynamic_array.py`:
  - `test_dyn_size_constant`: size == 4, elements constrained.
  - `test_dyn_size_range`: size in [2, 8], elements constrained.
  - `test_dyn_size_linked`: size == m_length, m_length in [1, 16].
  - `test_dyn_foreach_element`: foreach with inside constraint.
  - `test_dyn_two_arrays`: two dynamic arrays with independent sizes.
- `tests/integration/test_verilator_arrays.py`:
  - `test_dyn_array_reduction`: mirrors `t_constraint_dyn_array_reduction.v`.
  - `test_array_sum_with`: mirrors `t_constraint_array_sum_with.v`.
  - `test_dyn_size_inline`: mirrors `t_constraint_dyn_size_inline.v`.
  - `test_foreach_classref`: mirrors `t_constraint_foreach_classref.v` (per-element field constraints).

### Sprint T5: Integration Tests + Benchmarks (Week 5)

**Tests:**
- `tests/integration/test_verilator_sysfunc.py`:
  - `test_countones`: mirrors `t_constraint_countones.v`.
  - `test_onehot`: mirrors `t_constraint_sysfunc.v` $onehot.
  - `test_onehot0`: mirrors `t_constraint_sysfunc.v` $onehot0.
  - `test_countbits_ones`: mirrors $countbits(x, '1).
  - `test_countbits_zeros`: mirrors $countbits(x, '0).
  - `test_clog2`: mirrors `t_constraint_sysfunc.v` $clog2.
  - `test_countones_wide`: mirrors `t_constraint_countones.v` Rand3 (33-bit).

**Benchmarks:**
- `tests/bench/test_array_sum.py`: 8-element array, sum == 1000. Throughput.
- `tests/bench/test_countones.py`: $onehot on 32-bit. Throughput.
- `tests/bench/test_dyn_array_phase.py`: Dynamic array size [4, 16], phased solve. Throughput.

---

## Summary: What Changes Where

### Solver core (C)
| Change | Files |
|---|---|
| SumEq propagator | `zsp_propagator.h`, `zsp_prop_templates.c`, `zsp_compile.c` |
| Countones propagator | `zsp_propagator.h`, `zsp_prop_templates.c`, `zsp_compile.c` |
| Clog2 propagator | `zsp_propagator.h`, `zsp_prop_templates.c`, `zsp_compile.c` |
| EXPR_SUM, EXPR_COUNTONES, EXPR_CLOG2, EXPR_ARRAY_SELECT | `zsp_problem.h`, `zsp_problem.c` |
| solver_add_array_vars | `zsp_search.h`, `zsp_compile.c` |
| ArraySelect ITE lowering | `zsp_compile.c` |

### Integration layer (Python)
| Change | Files |
|---|---|
| Sum/reduction unrolling | `ir_translator.py` |
| Dynamic array phased solving | `array_solver.py` (new) |
| Builder wrappers | `builder.py`, `problem.py` |

### What is NOT in the solver
- Class/struct types and layout
- foreach loop iteration
- Dynamic array memory management
- Associative array key management
- String types

These remain in the integration layer where they belong.

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| SumEq propagator overflow for large sums (32-bit) | Use 64-bit intermediate accumulators. Clamp at INT32_MAX/MIN. |
| Countones propagator weak on wide variables (>16 bit) | For wide vars, use coarse bounds only. For narrow (<=16), use lookup table for exact propagation. |
| ArraySelect ITE chain explodes for large N | Limit ITE expansion to N<=16. For larger N, require phased solving (solve index first). |
| Dynamic array phased solving adds latency | Benchmark Phase-1 + Phase-2 vs single-phase with pre-allocated elements. Choose the faster approach per array size. |
| Product propagator overflow | Use 64-bit or __int128 intermediates. Document that product constraints on large arrays may lose precision. |
