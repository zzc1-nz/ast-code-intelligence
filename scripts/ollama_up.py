"""Start the Ollama server with all state on D:\\ and register the local GGUF.

Ollama is a standalone binary (downloaded zip, no installer) so we control
exactly where the models live:

    OLLAMA_MODELS=D:\codegraph\models\ollama
    OLLAMA_HOST=127.0.0.1:11434

The model is created from a GGUF + Modelfile so the Q4_K_M quantisation we
downloaded from ModelScope is used verbatim (no re-download from the registry).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("CODEGRAPH_ROOT", Path(__file__).resolve().parents[1]))
OLLAMA_DIR = ROOT / "ollama"
BIN = OLLAMA_DIR / "ollama.exe"
MODELS = ROOT / "models" / "ollama"
GGUF = ROOT / "models" / "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
MODELFILE = ROOT / "models" / "Modelfile"
MODEL_NAME = "qwen2.5-coder:7b"
HOST = "127.0.0.1:11434"


def env() -> dict:
    e = os.environ.copy()
    e["OLLAMA_MODELS"] = str(MODELS)
    e["OLLAMA_HOST"] = HOST
    e["OLLAMA_KEEP_ALIVE"] = "30m"
    # 8 GB laptop GPU with ~1.6 GB taken by the desktop: keep one model resident
    e["OLLAMA_MAX_LOADED_MODELS"] = "1"
    e["OLLAMA_NUM_PARALLEL"] = "1"
    e["OLLAMA_FLASH_ATTENTION"] = "1"
    return e


def is_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def list_models() -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://{HOST}/api/tags", timeout=10) as r:
            return json.loads(r.read()).get("models", [])
    except Exception:  # noqa: BLE001
        return []


def start_server() -> None:
    if is_up():
        print("server already running")
        return
    if not BIN.exists():
        print(f"ERROR: ollama binary not found at {BIN}", file=sys.stderr)
        sys.exit(1)
    MODELS.mkdir(parents=True, exist_ok=True)
    log = OLLAMA_DIR / "server.log"
    print(f"starting {BIN} (models -> {MODELS})")
    with open(log, "ab") as lf:
        subprocess.Popen(
            [str(BIN), "serve"],
            env=env(),
            stdout=lf,
            stderr=lf,
            cwd=str(OLLAMA_DIR),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    for _ in range(60):
        time.sleep(1)
        if is_up():
            print("server is up")
            return
    print("ERROR: server did not come up in 60s; see", log, file=sys.stderr)
    sys.exit(1)


def create_model() -> None:
    if not GGUF.exists():
        print(f"ERROR: GGUF not found: {GGUF}", file=sys.stderr)
        sys.exit(1)
    if not MODELFILE.exists():
        print(f"ERROR: Modelfile not found: {MODELFILE}", file=sys.stderr)
        sys.exit(1)
    print(f"creating {MODEL_NAME} from {GGUF.name}")
    r = subprocess.run(
        [str(BIN), "create", MODEL_NAME, "-f", str(MODELFILE)],
        env=env(), capture_output=True, text=True,
    )
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip()[-1500:])
    if r.returncode != 0:
        sys.exit(r.returncode)


def warmup() -> None:
    payload = json.dumps({
        "model": MODEL_NAME,
        "prompt": "Reply with the single word: ready",
        "stream": False,
        "options": {"num_predict": 8, "temperature": 0, "seed": 42},
    }).encode()
    req = urllib.request.Request(
        f"http://{HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    print(f"warmup: {time.time() - t0:.1f}s  reply={d.get('response', '').strip()[:40]!r}  "
          f"in={d.get('prompt_eval_count')} out={d.get('eval_count')}")
    print(f"load_duration={d.get('load_duration', 0) / 1e9:.1f}s "
          f"eval_rate={d.get('eval_count', 0) / max(1e-9, d.get('eval_duration', 1) / 1e9):.1f} tok/s")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    if action == "up":
        start_server()
        models = [m.get("name") for m in list_models()]
        print("models:", models)
        if not any(m and m.split(":")[0] == "qwen2.5-coder" for m in models):
            create_model()
        print("models now:", [m.get("name") for m in list_models()])
        warmup()
    elif action == "status":
        print("up:", is_up())
        print("models:", [m.get("name") for m in list_models()])
        print("env OLLAMA_MODELS:", env()["OLLAMA_MODELS"])
    elif action == "stop":
        subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True)
        print("stopped")
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
