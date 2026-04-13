# Verilator Constraint-Random Test Plan for zuspec-solver

**Date:** 2026-04-13
**Source:** `../vlsolve/verilator/test_regress/t/t_constraint_*.v` and `t_randomize_*.v`
**Existing coverage:** `tests/integration/test_verilator_patterns.py` (8 tests),
plus unit tests across `tests/unit/test_*.py` (341 tests)

This document catalogs every meaningful constraint-random test from the
Verilator regression suite and maps each to either existing zuspec-solver
coverage or a new test to add. Tests are grouped by solver feature area.

---

## 1. Arithmetic & Comparison Operators

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint.v` | `one > 0 && one < 2` (singleton solve) | `test_verilator_patterns::test_operators` | No |
| `t_constraint_operators.v` | `x + x - x == x` (identity) | `test_verilator_patterns::test_operators` | **Yes** -- add identity/cancellation cases |
| `t_constraint_operators.v` | `(x % 5) / 2 != (b % 99) / 7` (chained div/mod NEQ) | partial in `test_propagators_64` | **Yes** -- `test_chained_divmod_neq` |
| `t_constraint_operators.v` | `x * 9 != b * 3` (mul NEQ) | partial | **Yes** -- `test_mul_neq` |
| `t_constraint_operators.v` | `y * y == 4; y > 0; y < 4` (quadratic, signed) | None | **Yes** -- `test_quadratic_signed` |
| `t_constraint_operators.v` | `e ** 5 < 10000` (power) | None | **Yes** -- `test_power_operator` |
| `t_constraint_operators.v` | `cmps` / `cmpu` (all comparison operators ORed) | `test_propagators` covers individual ops | No (low value) |

## 2. Bitwise & Shift Operators

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_operators.v` | `{c, b} != 'h1111` (concat in constraint) | `test_concat` | No |
| `t_constraint_operators.v` | `!(-~c == 'h22)` (unary chain) | `test_bitwise::test_bnot` | **Yes** -- `test_unary_chain_neg_not` |
| `t_constraint_operators.v` | `(b ^ c) & (b >>> c \| b >> c \| b << c) > 0` (mixed bitwise/shift) | `test_bitwise`, `test_shift` cover individual ops | **Yes** -- `test_mixed_bitwise_shift` |
| `t_constraint_shift_width.v` | `address % (1 << size) == 0` (variable shift alignment, 37-bit addr) | `test_verilator_patterns::test_alignment` | **Yes** -- `test_variable_shift_alignment` (variable shift amount, wide address) |
| `t_constraint_shift_width.v` | `address % (1 << 10) == 0` (constant shift, wide addr) | `test_alignment` bench | No |
| `t_constraint_shift_width.v` | implication + shift + mixed width | None | **Yes** -- `test_implication_shift_mixed_width` |

## 3. Width & Extension

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_operators.v` | `s64'(x) != u64'(tiny)` (sign-extend vs zero-extend comparison) | `test_extend` | **Yes** -- `test_sext_vs_zext_neq` |
| `t_constraint_operators.v` | `d[15:8] == 8'h55` (bit-slice / extract) | `test_concat` partial | **Yes** -- `test_bit_select_constraint` |
| `t_constraint_shift_width.v` | 37-bit address, 4-bit size (cross-width constraint) | None | **Yes** -- `test_cross_width_37bit` |
| `t_randomize_unpacked_wide.v` | Wide (>64 bit) variable randomization | `test_variable::test_tier2_128bit` | **Yes** -- `test_wide_variable_solve` |

## 4. Conditional / ITE / Implication

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_operators.v` | `tiny == 1 -> x != 10` (implication) | `test_e2e::test_implication_constraint`, `test_ite` | No |
| `t_constraint_operators.v` | `(tiny == 1 ? b : c) != 17` (ITE value) | `test_ite` | No |
| `t_constraint_operators.v` | `if (one) out0 == 'h333` (if/else constraint blocks, nested) | `test_verilator_patterns::test_conditional` | **Yes** -- `test_nested_if_else_blocks` |
| `t_constraint_operators.v` | `if (one && zero)` / `if (~one && zero)` (boolean logic in conditions) | None | **Yes** -- `test_boolean_logic_conditions` |
| `t_constraint_cond.v` | `if (i) { (d==0) ? y==0 : 1'b1 }` (ITE inside if-block with ternary) | `test_ite` partial | **Yes** -- `test_ite_nested_in_if` |
| `t_constraint_foreach.v` | `if (posit==1) { foreach... } else { foreach... }` (conditional foreach) | `test_verilator_patterns::test_foreach` | **Yes** -- `test_conditional_foreach` |

## 5. State Variables (non-rand in constraints)

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_state.v` | `rf == state` (non-rand member in constraint) | `test_verilator_patterns::test_state_variable` | No |
| `t_constraint_state.v` | `a > foo.x; a < bar.y` (cross-object non-rand refs) | `test_verilator_patterns::test_state_variable` partial | **Yes** -- `test_cross_object_state_refs` |
| `t_constraint_state.v` | Re-randomize after changing state value | `test_pin_var` | **Yes** -- `test_re_randomize_after_state_change` |

## 6. Solve-Before (Phased Solving)

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_solve_before.v` | `solve mode before data` + conditional constraints | `test_verilator_patterns::test_solve_before` | No |
| `t_constraint_solve_before.v` | Multi-level `solve a before b; solve b before c` | None | **Yes** -- `test_solve_before_chain` |
| `t_randomize_solve_before_foreach.v` | `solve...before` combined with `foreach` | None | **Yes** -- `test_solve_before_foreach` |

## 7. Distribution Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_dist.v` | `:= 0` weight (exclude from dist) | `test_verilator_patterns::test_dist`, `test_dist` unit | No |
| `t_constraint_dist.v` | `z dist {que}` (dist from queue variable) | None | **Yes** -- `test_dist_from_container` |
| `t_constraint_dist_weight.v` | `:= scalar` weights (75/25 bias) | `test_dist` unit | No |
| `t_constraint_dist_weight.v` | `:/ range` weights | `test_dist` unit | No |
| `t_constraint_dist_weight.v` | Zero-weight entry exclusion | `test_dist` unit | No |
| `t_constraint_dist_weight.v` | All-zero-weight dist (domain-only, no bias) | None | **Yes** -- `test_dist_all_zero_weight` |
| `t_constraint_dist_weight.v` | Variable weights (`w1`, `w2`) in dist | None | **Yes** -- `test_dist_variable_weight` |
| `t_randomize_dist_conditional.v` | `if (mode) x dist {...} else x dist {...}` | None | **Yes** -- `test_dist_conditional` |
| `t_randomize_dist_foreach.v` | `foreach (arr[i]) arr[i] dist {...}` | None | **Yes** -- `test_dist_foreach` |

## 8. Soft Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_soft.v` | `soft x == 5` (only soft, no hard) | `test_soft::test_soft_all_satisfiable` | No |
| `t_randomize_soft.v` | Two competing soft on same var (last-wins) | None | **Yes** -- `test_soft_last_wins` |
| `t_randomize_soft.v` | Soft overridden by hard | `test_soft::test_soft_one_relaxed` | No |
| `t_randomize_soft.v` | Soft range intersecting hard range | None | **Yes** -- `test_soft_range_intersection` |
| `t_randomize_soft_relaxation.v` | Priority-based relaxation preserving maximum compatible set | `test_soft::test_soft_priority_ordering` | **Yes** -- `test_soft_max_compatible_set` |
| `t_randomize_soft_cross_object.v` | Soft constraints referencing sub-object fields | None | **Yes** -- `test_soft_cross_object` |

## 9. Unique / AllDifferent

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_unq_arr_derived.v` | `unique {pmp_reg}` with enum type + inheritance | `test_all_different`, `test_verilator_patterns::test_unique` | **Yes** -- `test_unique_enum_array` |
| `t_randomize_unique_elem.v` | `unique {arr[2], arr[3], arr[4], arr[5], arr[6]}` (subset of array) | None | **Yes** -- `test_unique_array_subset` |
| `t_randomize_unique_elem.v` | `unique {val[0]}` (single element -- trivially true) | None | **Yes** -- `test_unique_single_element` |

## 10. Randc (Cyclic Randomization)

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randc.v` | Narrow (1-4 bit) randc -- full cycle verification | `test_randc` unit | No |
| `t_randc.v` | Wide (8-32 bit) randc -- uniqueness over 100 iterations | `test_randc` partial | **Yes** -- `test_randc_wide_uniqueness` |
| `t_randc.v` | Enum randc (non-contiguous domain) | None | **Yes** -- `test_randc_enum_domain` |
| `t_randc_constraint.v` | Randc with range constraint `value >= 3; value <= 10` | `test_randc` partial | **Yes** -- `test_randc_constrained_range` |
| `t_randc_constraint.v` | Randc with exclude constraint `val != 0` | `test_randc` partial | **Yes** -- `test_randc_exclude_value` |
| `t_randc_constraint.v` | Randc + inheritance | None | **Yes** -- `test_randc_inheritance` |
| `t_randc_enum_constraint.v` | Randc enum with `inside` constraint | None | **Yes** -- `test_randc_enum_inside` |
| `t_randc_wide_constraint.v` | Wide randc with constraints | None | **Yes** -- `test_randc_wide_constrained` |
| `t_randc_extends.v` | Randc field in derived class | None | **Yes** -- `test_randc_extends` |

## 11. Constraint Mode

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_mode.v` | `cons_y.constraint_mode(0)` (disable individual constraint) | None | **Yes** -- `test_constraint_mode_disable` |
| `t_constraint_mode.v` | `constraint_mode(1)` (re-enable all) | None | **Yes** -- `test_constraint_mode_enable_all` |
| `t_constraint_mode.v` | Constraint mode on sub-object (`bar.cons_x.constraint_mode(0)`) | None | **Yes** -- `test_constraint_mode_subobject` |
| `t_constraint_mode_static.v` | Static constraint mode (`constraint_mode` on class, not instance) | None | **Yes** -- `test_constraint_mode_static` |

## 12. Rand Mode

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_rand_mode.v` | `m_one.rand_mode(0)` -- field keeps assigned value | `test_pin_var` (similar) | **Yes** -- `test_rand_mode_disable_field` |
| `t_randomize_rand_mode.v` | `rand_mode(0)` on class (disable all) | None | **Yes** -- `test_rand_mode_disable_all` |
| `t_randomize_rand_mode_constr.v` | rand_mode interacting with constraints | None | **Yes** -- `test_rand_mode_with_constraints` |
| `t_randomize_randmode_subobj.v` | rand_mode on sub-object fields | None | **Yes** -- `test_rand_mode_subobject` |
| `t_rand_member_mode_deriv.v` | rand_mode in derived class | None | **Yes** -- `test_rand_mode_derived` |

## 13. Inheritance & Class Hierarchy

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_inheritance.v` | Constraint inherited across 3+ levels | None | **Yes** -- `test_constraint_inheritance_chain` |
| `t_constraint_inheritance_with.v` | `randomize() with {}` overriding inherited constraint | None | **Yes** -- `test_inheritance_with_override` |
| `t_constraint_pure.v` | `pure constraint` in virtual class | None | **Yes** -- `test_pure_constraint` |
| `t_constraint_nested_class.v` | Constraint referencing nested class member (`b.a.x == LEN`) | None | **Yes** -- `test_nested_class_constraint` |

## 14. Randomize-With (Inline Constraints)

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_with_constraint.v` | `randomize() with (m_x) { m_x > 0; m_x < y; }` | None | **Yes** -- `test_randomize_with_scope` |
| `t_randomize_method_with.v` | `randomize() with { ... }` on method call | None | **Yes** -- `test_randomize_with_basic` |
| `t_randomize_method_with_scoping.v` | Scoping rules for inline with-constraints | None | **Yes** -- `test_randomize_with_scoping` |
| `t_randomize_param_with.v` | Parameterized class + randomize with | None | **Yes** -- `test_randomize_with_param` |
| `t_randomize_this_with.v` | `this.randomize() with {}` | None | **Yes** -- `test_randomize_this_with` |

## 15. Arrays in Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_foreach.v` | `foreach(q[i]) x > i` (foreach tightening scalar) | `test_verilator_patterns::test_foreach` | No |
| `t_constraint_unpacked_array.v` | `foreach (data[i]) data[i] inside {set}` | `test_verilator_patterns::test_foreach` | **Yes** -- `test_foreach_inside_set` |
| `t_constraint_array_index.v` | Array element access in constraint body | None | **Yes** -- `test_array_element_constraint` |
| `t_constraint_array_index_simple.v` | Simple array indexing | None | No (subset of above) |
| `t_constraint_rand_array_index.v` | Rand variable as array index (`data[idx]`) + `solve idx before` | None | **Yes** -- `test_rand_array_index` |
| `t_constraint_array_sum_with.v` | `array.sum() with (int'(item == val)) == 3` | None | **Yes** -- `test_array_sum_with` |
| `t_constraint_dyn_array_reduction.v` | Dynamic array `.sum()`, `.product()` in constraints | None | **Yes** -- `test_dynamic_array_reduction` |
| `t_constraint_array_limit.v` | Array size limits in constraints | None | **Yes** -- `test_array_size_limit` |
| `t_randomize_array.v` | Packed/unpacked/dynamic array randomization | None | **Yes** -- `test_array_randomization_types` |
| `t_randomize_queue_constraints.v` | Queue element constraints + rand index | None | **Yes** -- `test_queue_element_constraints` |
| `t_randomize_queue_size.v` | Queue `.size()` in constraint | None | **Yes** -- `test_queue_size_constraint` |
| `t_constraint_assoc_arr_basic.v` | Associative array in constraint | None | **Yes** -- `test_assoc_array_constraint` |

## 16. Structs in Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_struct.v` | Packed struct member constraints | None | **Yes** -- `test_packed_struct_constraint` |
| `t_constraint_struct.v` | Unpacked struct member constraints | None | **Yes** -- `test_unpacked_struct_constraint` |
| `t_constraint_struct_complex.v` | Struct with arrays, dynamic arrays, queues, assoc arrays | None | **Yes** -- `test_struct_complex_arrays` |
| `t_constraint_struct_complex.v` | Array of structs with per-element constraints | None | **Yes** -- `test_struct_array_foreach` |
| `t_constraint_cls_arr_member.v` | Class array member in constraint | None | **Yes** -- `test_class_array_member` |

## 17. System Functions in Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_countones.v` | `$countones(x) == 1` | None | **Yes** -- `test_countones_constraint` |
| `t_constraint_sysfunc.v` | `$onehot(value)` | None | **Yes** -- `test_onehot_constraint` |
| `t_constraint_sysfunc.v` | `$onehot0(value)` | None | **Yes** -- `test_onehot0_constraint` |
| `t_constraint_sysfunc.v` | `$countbits(value, '1) == 3` | None | **Yes** -- `test_countbits_constraint` |
| `t_constraint_sysfunc.v` | `$clog2(data_width)` in constraint | None | **Yes** -- `test_clog2_constraint` |

## 18. UNSAT Detection

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_unsat.v` | `x > 100; x < 50` (trivially conflicting) | `test_search::test_unsatisfiable` | No |
| `t_constraint_unsat.v` | `randomize() with { addr == 128; }` violating `addr < 127` | None | **Yes** -- `test_unsat_inline_override` |
| `t_constraint_unsat.v` | Randomize returns 0 (not exception) on UNSAT | `test_e2e::test_unsat_raises` | No |

## 19. Pre/Post Randomize Callbacks

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_prepost.v` | `pre_randomize()` / `post_randomize()` hooks | None | **Yes** -- `test_pre_post_randomize` |
| `t_randomize_prepost_super.v` | `super.pre_randomize()` call chain | None | **Yes** -- `test_pre_post_super_chain` |
| `t_randomize_prepost_nested.v` | Nested class pre/post randomize | None | **Yes** -- `test_pre_post_nested` |

## 20. Seeding & Reproducibility

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_srandom.v` | `srandom(seed)` produces deterministic results | `test_reset::test_deterministic_seed` | No |
| `t_rand_stability_class.v` | Seed stability across class instances | None | **Yes** -- `test_seed_stability_class` |
| `t_rand_stability_process.v` | Seed stability across processes | None | **Yes** -- `test_seed_stability_process` |
| `t_srandom_class_dep.v` | `srandom` dependency between parent/child | None | **Yes** -- `test_srandom_class_dependency` |

## 21. std::randomize (Standalone)

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_std_randomize.v` | `std::randomize(addr, data)` (randomize local vars) | None | **Yes** -- `test_std_randomize_local_vars` |
| `t_std_randomize_with.v` | `std::randomize(x) with { x > 0; }` | None | **Yes** -- `test_std_randomize_with` |
| `t_std_randomize_mod.v` | `std::randomize` at module scope | None | **Yes** -- `test_std_randomize_module_scope` |
| `t_std_randomize_queue.v` | `std::randomize` with queue | None | **Yes** -- `test_std_randomize_queue` |

## 22. Function Calls in Constraints

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_constraint_func_call.v` | `value >= get_min_value(mask)` (non-rand function call) | None | **Yes** -- `test_func_call_in_constraint` |
| `t_constraint_extern.v` | `extern constraint` declaration | None | **Yes** -- `test_extern_constraint` |

## 23. Complex / Integration Patterns

| Verilator test | Pattern | Existing coverage | New test needed |
|---|---|---|---|
| `t_randomize_complex.v` | Deep class hierarchy randomization | None | **Yes** -- `test_deep_class_hierarchy` |
| `t_randomize_from_randomized_class.v` | Randomize class that contains a randomized sub-class | None | **Yes** -- `test_nested_randomization` |
| `t_randomize_derived_this.v` | `this.randomize()` in derived class | None | **Yes** -- `test_derived_this_randomize` |
| `t_constraint_global_random.v` | Global (module-scope) `std::randomize` | None | **Yes** -- `test_global_randomize` |
| `t_constraint_json_only.v` | Mixed: dist + if/else + foreach + unique + solve-before | None | **Yes** -- `test_mixed_constraint_types` |
| `t_randomize_union.v` | Union type randomization | None | **Yes** -- `test_union_randomize` |
| `t_randomize_subobj_enum.v` | Enum field in sub-object | None | **Yes** -- `test_subobject_enum` |

---

## Summary

| Category | Already covered | New tests needed |
|---|---|---|
| 1. Arithmetic & Comparison | 2 | 4 |
| 2. Bitwise & Shift | 2 | 3 |
| 3. Width & Extension | 1 | 3 |
| 4. Conditional / ITE | 3 | 4 |
| 5. State Variables | 1 | 2 |
| 6. Solve-Before | 1 | 2 |
| 7. Distribution | 4 | 5 |
| 8. Soft Constraints | 2 | 4 |
| 9. Unique / AllDifferent | 2 | 3 |
| 10. Randc | 1 | 7 |
| 11. Constraint Mode | 0 | 4 |
| 12. Rand Mode | 0 | 5 |
| 13. Inheritance | 0 | 4 |
| 14. Randomize-With | 0 | 5 |
| 15. Arrays | 1 | 11 |
| 16. Structs | 0 | 5 |
| 17. System Functions | 0 | 5 |
| 18. UNSAT Detection | 2 | 1 |
| 19. Pre/Post Callbacks | 0 | 3 |
| 20. Seeding | 1 | 3 |
| 21. std::randomize | 0 | 4 |
| 22. Function Calls | 0 | 2 |
| 23. Complex / Integration | 0 | 7 |
| **Total** | **23** | **~95** |

### Priority Tiers

**Tier 1 -- Solver core (implement first):**
Tests that exercise the constraint solver engine directly. These can be
written today against the C API or Python `zuspec.solver` API.
- Categories 1-10, 18 (arithmetic, bitwise, width, ITE, state, solve-before, dist, soft, unique, randc, UNSAT)
- ~39 new tests

**Tier 2 -- SV integration layer:**
Tests that require SV-level features (constraint_mode, rand_mode,
randomize-with, pre/post callbacks, inheritance semantics). These need
the zuspec-be-sv or zuspec-fe-pss frontend to translate SV constructs.
- Categories 11-14, 19-22
- ~30 new tests

**Tier 3 -- Advanced data structures:**
Tests for arrays, queues, structs, and system functions in constraints.
These require IR and frontend support beyond scalar variables.
- Categories 15-17, 23
- ~26 new tests

### Verilator Tests Not Applicable

The following test categories from Verilator are out of scope for
zuspec-solver (they test Verilator's SV compilation, not constraint solving):
- `t_randcase*.v` -- `randcase` statement (procedural, not constraint)
- `t_randsequence*.v` -- `randsequence` (procedural)
- `t_randstate*.v` -- RNG state save/restore (Verilator-specific)
- `t_sys_random*.v` -- `$random` system task
- `t_urandom.v` -- `$urandom` system task
- `t_math_real_random.v` -- Real-valued random
- `t_x_rand_*.v` -- X-state random behavior
- `*_bad.v` / `*_unsup.v` -- Error/unsupported detection tests (32 files)
