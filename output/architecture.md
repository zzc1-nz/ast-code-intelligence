## 1. Entry point and control flow

Execution starts in `crates/core/main.rs` at line 44 with the `main` function. The `main` function calls `run`, passing the result of parsing command-line arguments. The `run` function then dispatches to various functions based on the parsed arguments, such as `search`, `search_parallel`, `files`, `files_parallel`, `types`, and `generate`. These functions perform the actual search operations and handle the results.

## 2. Module responsibilities

- **crates/core/main.rs**: Main entry point and orchestrator of the search operations. It parses command-line arguments and dispatches to other modules based on the arguments.
- **crates/core/flags/hiargs.rs**: Contains the `HiArgs` struct and related functions that handle the parsing and manipulation of command-line arguments.
- **crates/core/search.rs**: Contains the search logic, including single-threaded and multi-threaded search implementations.
- **crates/ignore/src/walk.rs**: Handles directory walking and file path processing.
- **crates/printer/src/stats.rs**: Tracks and aggregates search statistics.
- **crates/regex/src/matcher.rs**: Provides regex matching functionality.
- **crates/printer/src/path.rs**: Manages path printing with optional color and hyperlink support.

## 3. Data / control dependencies

- **crates/core/main.rs** depends on **crates/core/flags/hiargs.rs** for parsing and manipulating command-line arguments.
- **crates/core/search.rs** depends on **crates/ignore/src/walk.rs** for directory walking and **crates/regex/src/matcher.rs** for regex matching.
- **crates/ignore/src/walk.rs** depends on **crates/ignore/src/lib.rs** for file type matching logic.
- **crates/regex/src/matcher.rs** depends on **crates/regex/src/config.rs** for regex configurations.

There are no cycles or high fan-in in the dependency graph, and there are no orphan modules.

## 4. Extension points

A new feature could most naturally attach to the `crates/core/search.rs` module, as it contains the core search logic. For example, a new search mode or a new type of search result could be added here.

## 5. Risks

- **crates/core/main.rs** at line 63 and 154 has high fan-in, as it depends on multiple modules for parsing arguments and dispatching search operations.
- **crates/core/messages.rs** at line 129 has high fan-in, as it is used by multiple modules for error and message handling.
- **crates/regex/src/matcher.rs** at line 80 and 212 has high fan-in, as it depends on multiple modules for regex matching and error handling.