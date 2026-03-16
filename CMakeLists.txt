cmake_minimum_required(VERSION 3.14)
project(zuspec_solver C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Build position-independent code (required for shared library)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ------------------------------------------------------------------ #
# Source list                                                          #
# ------------------------------------------------------------------ #

set(ZSP_SOURCES
    src/c/zsp_block_alloc.c
    src/c/zsp_compile.c
    src/c/zsp_conflict.c
    src/c/zsp_ctx.c
    src/c/zsp_nogood.c
    src/c/zsp_pool.c
    src/c/zsp_problem.c
    src/c/zsp_propagate.c
    src/c/zsp_prop_templates.c
    src/c/zsp_search.c
    src/c/zsp_stack.c
    src/c/zsp_trail.c
)

# ------------------------------------------------------------------ #
# Shared library target                                                #
# ------------------------------------------------------------------ #

add_library(zsp_solver SHARED ${ZSP_SOURCES})

target_include_directories(zsp_solver PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src/c>
)

# Use AddressSanitizer in Debug builds to catch memory errors in CI
if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    target_compile_options(zsp_solver PRIVATE -fsanitize=address -g -O0)
    target_link_options(zsp_solver PRIVATE -fsanitize=address)
endif()

# ------------------------------------------------------------------ #
# Installation (for pip-installed package)                             #
# ------------------------------------------------------------------ #

install(TARGETS zsp_solver
    LIBRARY DESTINATION lib
)
