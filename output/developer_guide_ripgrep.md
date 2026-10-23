# Developer Guide for ripgrep

## Quick Start Orientation

Welcome to the ripgrep repository! This guide will help you get started with the codebase and understand how to contribute effectively. Here’s a quick overview of the key components and how to get started.

## Mental Model of the Codebase (How the Pieces Fit)

Ripgrep is a powerful text search tool built in Rust. The codebase is organized into several modules, each with a specific responsibility. Here’s a high-level overview:

- **crates/core/main.rs**: The entry point of the application. Orchestrates the search process by calling other modules.
- **crates/core/flags/hiargs.rs**: Parses command-line arguments and provides a structured representation.
- **crates/core/search.rs**: Contains the core logic for searching files and handling search results.
- **crates/ignore/src/walk.rs**: Handles file and directory traversal, including ignoring files and directories based on patterns.
- **crates/printer/src/standard.rs**: Provides functionality for printing search results in a standard format.
- **crates/regex/src/matcher.rs**: Provides regular expression matching functionality.

## Where to Start Reading (Ordered File List with Reasons)

1. **crates/core/main.rs**
   - **Reason**: This is the entry point of the application. It’s a good place to understand how the search process is orchestrated.
   - **Key Symbols**: `main`, `run`, `search`, `search_parallel`

2. **crates/core/flags/hiargs.rs**
   - **Reason**: This module parses command-line arguments. Understanding how arguments are parsed and structured is crucial for adding new features or modifying existing ones.
   - **Key Symbols**: `parse`, `parse_low`, `set_log_levels`

3. **crates/core/search.rs**
   - **Reason**: This module contains the core logic for searching files and handling search results. It’s essential to understand how the search process works.
   - **Key Symbols**: `search`, `search_parallel`

4. **crates/ignore/src/walk.rs**
   - **Reason**: This module handles file and directory traversal. Understanding how files are ignored based on patterns is important for customizing search behavior.
   - **Key Symbols**: `Worker::run_one`, `Walk::new`

5. **crates/printer/src/standard.rs**
   - **Reason**: This module provides functionality for printing search results. Understanding how results are formatted is useful for customizing output.
   - **Key Symbols**: `StandardImpl::start_color_match`, `SummarySink::start_hyperlink`

6. **crates/regex/src/matcher.rs**
   - **Reason**: This module provides regular expression matching functionality. Understanding how regular expressions are used for searching is essential for adding new search features.
   - **Key Symbols**: `Match::new`, `LineTerminator::byte`, `ByteSet::empty`

## How to Add a Feature (Concrete Steps Citing Real Symbols)

To add a new feature, follow these steps:

1. **Identify the Entry Point**: Decide where the new feature should be attached. For example, if you want to add a new search option, you might attach it to the `search` or `search_parallel` functions in `crates/core/main.rs`.

2. **Modify the Entry Point**: Add the new feature to the identified entry point. For example, if you want to add a new search option, you might modify the `search` function to include the new option.

3. **Test the Feature**: Write tests to ensure that the new feature works as expected. Use the `rgtest` macro from `tests/macros.rs` to define tests. For example:

   ```rust
   rgtest!(test_new_feature, "pattern", "file.txt", "expected_output");
   ```

4. **Verify the Feature**: Run the tests to verify that the new feature works as expected. Use the `cargo test` command to run the tests.

## Testing and Verification Entry Points

- **Tests**: Use the `rgtest` macro from `tests/macros.rs` to define tests. For example:

  ```rust
  rgtest!(test_new_feature, "pattern", "file.txt", "expected_output");
  ```

- **Cargo Test**: Run the tests using the `cargo test` command.

## Common Pitfalls

- **High Fan-In**: The `main` function has a high fan-in, which can make it complex and harder to maintain. Be mindful of the complexity and try to keep the `main` function as simple as possible.
- **Orphan Modules**: There are no orphan modules in the repository, as each module has a clear responsibility and is used by other modules.
- **No Cycles**: There are no cycles in the call graph, which ensures that the system is modular and easy to understand.

By following this guide, you should be well on your way to contributing to the ripgrep repository. Happy coding!