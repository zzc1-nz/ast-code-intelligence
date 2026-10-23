"""Ollama HTTP client with first-class token accounting.

Token accounting policy (documented, not estimated away)
--------------------------------------------------------
* PRIMARY source is the Ollama server itself: `/api/generate` and `/api/chat`
  return `prompt_eval_count` (input tokens) and `eval_count` (output tokens).
  Every call that returns a token counter is stored with token_source='server'.
* FALLBACK (only when the server omits the counters, e.g. on some streaming
  paths): a character-ratio estimator, stored with token_source='estimate'
  and counted separately in every report so the two are never mixed silently.
  The estimator is deliberately conservative:
      tokens ~= ceil(chars / 3.5) for code-ish text (English+code mix).

Every call is appended to the `llm_calls` table by the caller (see
`pipeline.py`), so the reports can be regenerated from SQLite at any time.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

import httpx

from .config import LLMConfig

CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    token_source: str          # "server" | "estimate"
    latency_ms: int
    model: str
    prompt_chars: int
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class OllamaClient:
    def __init__(self, cfg: LLMConfig | None = None):
        self.cfg = cfg or LLMConfig()
        self.base = self.cfg.normalized_base()
        self._client = httpx.Client(
            base_url=self.base,
            timeout=httpx.Timeout(self.cfg.timeout_s, connect=10.0),
        )

    # -- health ------------------------------------------------------------
    def is_up(self) -> bool:
        try:
            r = self._client.get("/api/tags", timeout=5.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def list_models(self) -> list[dict]:
        try:
            r = self._client.get("/api/tags", timeout=10.0)
            r.raise_for_status()
            return r.json().get("models", [])
        except Exception:  # noqa: BLE001
            return []

    def has_model(self, name: str | None = None) -> bool:
        target = (name or self.cfg.model).split(":")[0]
        for m in self.list_models():
            if m.get("name", "").split(":")[0] == target:
                return True
        return False

    # -- generation --------------------------------------------------------
    def generate(self, prompt: str, system: str | None = None, *, num_predict: int | None = None,
                 num_ctx: int | None = None, json_mode: bool = False) -> LLMResult:
        """Single-shot generation with retries. Never raises: errors land in .error."""
        t0 = time.perf_counter()
        payload = {
            "model": self.cfg.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature,
                "seed": self.cfg.seed,
                "num_ctx": num_ctx or self.cfg.num_ctx,
                "num_predict": num_predict or self.cfg.num_predict,
                "top_p": 0.9,
            },
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        last_err: str | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                r = self._client.post("/api/generate", json=payload)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(1.0 + attempt)
                    continue
                data = r.json()
                text = data.get("response", "") or ""
                in_tok = data.get("prompt_eval_count")
                out_tok = data.get("eval_count")
                if in_tok is None or out_tok is None:
                    source = "estimate"
                    in_tok = estimate_tokens(prompt) + (estimate_tokens(system) if system else 0)
                    out_tok = estimate_tokens(text)
                else:
                    source = "server"
                return LLMResult(
                    text=text,
                    input_tokens=int(in_tok),
                    output_tokens=int(out_tok),
                    token_source=source,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    model=self.cfg.model,
                    prompt_chars=len(prompt) + (len(system) if system else 0),
                )
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(1.0 + attempt)

        return LLMResult(
            text="",
            input_tokens=0,
            output_tokens=0,
            token_source="server",
            latency_ms=int((time.perf_counter() - t0) * 1000),
            model=self.cfg.model,
            prompt_chars=len(prompt),
            error=last_err or "unknown error",
        )

    def warmup(self) -> tuple[bool, str]:
        """Load the model into VRAM once so later latencies are steady-state."""
        if not self.is_up():
            return False, "ollama server not reachable"
        if not self.has_model():
            return False, f"model {self.cfg.model} not found"
        res = self.generate("Reply with the single word: ready", num_predict=8)
        if res.error:
            return False, res.error
        return True, res.text.strip()[:60]

    def close(self) -> None:
        self._client.close()
