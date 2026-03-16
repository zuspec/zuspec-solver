# Zuspec Solver

- Solver helpers -> optimized version of 
- Full solver (PoC?)
  - be-sw could use this for 'live' solving
- Platform solver
  - Helper libraries for compiled solving
  - be-sw uses this

Scalable solver:
- Dynamically build-up problem to solve
  - Creates a problem-solver object
  - Construct expression objects (structs) to represent constraints
    - Essentially want a type AST
  -> Need a way to statically capture without running builder logic (?)
    - DOES NOT need to be pretty. Just compiles
  - Find ways to use JIT
    - For expensive propagators
    - For hot operations
    - Replace OTF 
  - Works on compound data objects (no separate vars)
  - API accepts 

  - Solver object responsible for finding a solution
- Fixed value-generation function
  - Given these 
- 

struct S {
    int a;
    int b;

    // a < b;
}

static S *p = 0;

struct expr_s {
    int type;
    union {

    };
};

type_def -> solver_t
            -> solver1 -> solution
            -> solver2 -> solution (reuse? re-initialize?)

- type_def is expression and type descriptors that define the solve problem
- solver_t is a solve-problem-specific data structure
  - Do learnings need to be reflected here?
  - Maybe... certainly represents "best"
- solver is created from solver_t when a solution is desired

// Assume we're generating all of this.
// - programmatically, by creating data structures
// - statically/generatively, via codegen -> Save runtime from needing tp build up constraints
// DON'T CARE about readability or ease of human construction

// Building a solve-context definition
// - Array of 
// RefRoot is: root_index, offset, size/type (supports interpreter in type checking)
// RefField is: root_expr, offset, size/type

- "problem" setup:
  - state and random variables
  - constraints

- "solution" setup:
  - potentially all internal

static __expr_1 = {

}


constraints[] = {
    EXPR_LT(
        EXPR_REF(self, &self->a), 
        EXPR_REF(self, &self->b)&self->b)&p->a, &p->b)
        // Root variable, offset

}

struct S_c {

}




The Zuspec Solver project contains several solver engines used 