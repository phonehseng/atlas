"""Text embeddings for meaning-based search (scene finding): a small local embedding model in Ollama
(qwen3-embedding, nomic-embed-text, mxbai, bge... whatever is installed and says "embedding"), else the
OpenAI-compatible /embeddings of the user's own provider. None when neither is there."""
import time

import requests

from .. import settings

OLLAMA = "http://127.0.0.1:11434"
PREFERRED = ("qwen3-embedding", "nomic-embed-text", "mxbai-embed", "bge-m3", "snowflake-arctic-embed", "all-minilm")
_found = {"at": 0.0, "model": None}


def ollama_model():
    if time.time() - _found["at"] < (3600 if _found["model"] else 60):
        return _found["model"]
    _found["at"] = time.time()
    _found["model"] = None
    try:
        names = [m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=2).json().get("models", [])]
        ranked = sorted(names, key=lambda n: next((i for i, p in enumerate(PREFERRED) if n.startswith(p)), 99))
        for n in ranked:
            if next((i for i, p in enumerate(PREFERRED) if n.startswith(p)), 99) == 99:
                caps = requests.post(f"{OLLAMA}/api/show", json={"model": n}, timeout=5).json().get("capabilities") or []
                if "embedding" not in caps:
                    continue
            _found["model"] = n
            break
    except Exception:
        pass
    return _found["model"]


def provider():
    s = settings.load()
    if s["ai_provider"] == "off":
        return None
    if s["ai_provider"] in ("auto", "atlas"):
        m = ollama_model()
        if m:
            return {"kind": "ollama", "model": m}
    if s["ai_provider"] in ("auto", "openai") and s["ai_url"] and s["ai_key"]:
        return {"kind": "openai", "model": "text-embedding-3-small", "url": s["ai_url"].rstrip("/"), "key": s["ai_key"]}
    return None


def embed(texts, query=False, task="Given a description of a moment in a film, find the passage where it happens"):
    """Unit-length vectors (numpy array, one row per text), or None."""
    import numpy as np
    p = provider()
    if not p or not texts:
        return None
    if query and p["model"].startswith("qwen3-embedding"):
        texts = [f"Instruct: {task}\nQuery: {t}" for t in texts]
    if p["kind"] == "ollama":
        r = requests.post(f"{OLLAMA}/api/embed", json={"model": p["model"], "input": list(texts), "keep_alive": "10m"},
                          timeout=120)
        r.raise_for_status()
        v = np.array(r.json()["embeddings"], dtype=np.float32)
    else:
        r = requests.post(f"{p['url']}/embeddings", json={"model": p["model"], "input": list(texts)}, timeout=60,
                          headers={"Authorization": f"Bearer {p['key']}"})
        r.raise_for_status()
        v = np.array([d["embedding"] for d in r.json()["data"]], dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
