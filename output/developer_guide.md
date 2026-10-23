# Developer Guide for ripgrep

## Quick Start Orientation

Welcome to the ripgrep repository! This guide will help you get started with the codebase and understand how to contribute effectively. Here’s a quick overview of the key components and how to get started.

### Key Components

- **crates/core/main.rs**: The main entry point of ripgrep. It parses command-line arguments and dispatches to various search functions.
- **crates/core/search.rs**: Contains the core search logic, including single-threaded and multi-threaded search implementations.
- **crates/ignore/src/walk.rs**: Handles directory walking and file path processing.
- **crates/regex/src/matcher.rs**: Provides regex matching functionality.

### Getting Started

1. **Clone the Repository**:
   ```sh
   git clone https://github.com/BurntSushi/ripgrep.git
   cd ripgrep
   ```

2. **Build the Project**:
   ```sh
   cargo build
   ```

3. **Run the Tests**:
   ```sh
   cargo test
   ```

## Mental Model of the Codebase (How the Pieces Fit)

Ripgrep is a powerful text search tool built in Rust. The codebase is organized into several crates, each responsible for a specific functionality. Here’s a high-level overview of how these crates interact:

- **crates/core**: Contains the main logic of ripgrep, including search, flags parsing, and output formatting.
- **crates/ignore**: Handles file path matching and directory walking.
- **crates/regex**: Provides regex matching functionality.
- **crates/searcher**: Implements search strategies and utilities.

## Where to Start Reading (Ordered File List with Reasons)

1. **crates/core/main.rs**
   - **Reason**: This is the entry point of the application. It parses command-line arguments and dispatches to other modules. Understanding how this file works will give you a good overview of how ripgrep is structured.

2. **crates/core/search.rs**
   - **Reason**: This file contains the core search logic. It’s essential to understand how search operations are performed, especially if you want to add new search features.

3. **crates/ignore/src/walk.rs**
   - **Reason**: This file handles directory walking and file path processing. Understanding how files and directories are traversed is crucial for any feature that involves file searching.

4. **crates/regex/src/matcher.rs**
   - **Reason**: This file provides regex matching functionality. If you need to add new search patterns or modify existing ones, this is the place to start.

## How to Add a Feature (Concrete Steps Citing Real Symbols)

Let’s say you want to add a new search mode that supports case-insensitive search.

1. **Define the Flag**:
   - Add a new flag in `crates/core/flags/defs.rs`:
     ```rust
     pub const CASE_INSENSITIVE: Flag = Flag::new(
         "i",
         "case-insensitive",
         "Search case-insensitively",
         Category::Search,
         FlagValue::Bool(false),
     );
     ```

2. **Parse the Flag**:
   - Update `crates/core/flags/parse.rs` to handle the new flag:
     ```rust
     pub fn parse_low(args: &[OsStr], low_args: &mut LowArgs) -> Result<(), Error> {
         // ...
         if arg == "i" {
             low_args.case_insensitive = true;
         }
         // ...
     }
     ```

3. **Implement the Search Logic**:
   - Update `crates/core/search.rs` to use the new flag:
     ```rust
     pub fn search(&self, haystack: &str, case_insensitive: bool) -> Vec<Match> {
         if case_insensitive {
             // Use case-insensitive search logic
         } else {
             // Use case-sensitive search logic
         }
     }
     ```

4. **Update Documentation**:
   - Update `crates/core/flags/doc/help.rs` to include the new flag in the help documentation.

## Testing and Verification Entry Points

Ripgrep has a comprehensive test suite. Here are some key entry points for testing:

1. **Unit Tests**:
   - Run all unit tests:
     ```sh
     cargo test
     ```

2. **Integration Tests**:
   - Run integration tests:
     ```sh
     cargo test --test integration
     ```

3. **Benchmarks**:
   - Run benchmarks:
     ```sh
     cargo bench
     ```

## Common Pitfalls

1. **Regex Errors**:
   - Ensure that your regex patterns are valid and handle errors gracefully. Use `crates/regex/src/error.rs` for error handling.

2. **File Path Handling**:
   - Be cautious with file paths, especially when dealing with different operating systems. Use `crates/ignore/src/pathutil.rs` for path manipulation.

3. **Concurrency**:
   - Ripgrep uses concurrency for multi-threaded search. Ensure that your code is thread-safe and handles synchronization correctly.

By following this guide, you should be well on your way to contributing to ripgrep and making it even more powerful and efficient!