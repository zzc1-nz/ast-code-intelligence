"""Configuration for codegraph.

The project root defaults to the directory containing this package's checkout
(i.e. the repo root), and can be overridden with `CODEGRAPH_ROOT`. Nothing is
hard-coded to a machine-specific path, so a fresh `git clone` works anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SEED = 42
PROMPT_TEMPLATE_VERSION = "v1"
SUMMARY_TEMPLATE_VERSION = "summary-v1"
ARCH_TEMPLATE_VERSION = "arch-v1"
GUIDE_TEMPLATE_VERSION = "guide-v1"
REVIEW_TEMPLATE_VERSION = "review-v1"

# src/codegraph/config.py -> parents[2] == repository root
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("CODEGRAPH_ROOT", str(_PACKAGE_ROOT)))
DEFAULT_DB = ROOT / ".codegraph" / "graph.db"
DEFAULT_REPORTS = ROOT / "reports"
DEFAULT_OUTPUT = ROOT / "output"


@dataclass
class LLMConfig:
    """Ollama connection settings."""

    base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: os.environ.get("CODEGRAPH_MODEL", "qwen2.5-coder:7b"))
    temperature: float = 0.0
    seed: int = SEED
    num_ctx: int = 8192
    num_predict: int = 512
    timeout_s: float = 300.0
    # Retry policy for transient Ollama errors
    max_retries: int = 2

    def normalized_base(self) -> str:
        b = self.base_url.rstrip("/")
        if b.endswith("/api"):
            b = b[: -len("/api")]
        return b


@dataclass
class IndexConfig:
    """Indexing / analysis behaviour."""

    root: Path = ROOT
    db_path: Path = DEFAULT_DB
    # File discovery
    max_file_bytes: int = 2_000_000
    include_exts: dict[str, str] = field(
        default_factory=lambda: {
            ".py": "python",
            ".rs": "rust",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".js": "javascript",
            ".jsx": "javascript",
            ".mjs": "javascript",
            ".cjs": "javascript",
        }
    )
    exclude_dirs: set[str] = field(
        default_factory=lambda: {
            ".git",
            ".hg",
            ".svn",
            "node_modules",
            "target",
            "build",
            "dist",
            ".venv",
            "venv",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".codegraph",
            ".uvcache",
            "repos",
            "models",
        }
    )


def ensure_dirs() -> None:
    for p in (ROOT, ROOT / ".codegraph", DEFAULT_REPORTS, DEFAULT_OUTPUT):
        p.mkdir(parents=True, exist_ok=True)
