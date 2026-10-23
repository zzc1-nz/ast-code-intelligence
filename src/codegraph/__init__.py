"""codegraph - AST-driven local code intelligence engine.

Pipeline:
    source files -> Tree-sitter AST -> symbols / call / import / inherit edges
                 -> SQLite knowledge graph (FTS5 indexed)
                 -> AST-grounded prompts -> local Ollama model -> summaries,
                    architecture analysis, developer guide, review.
"""

__version__ = "0.1.0"

SEED = 42
