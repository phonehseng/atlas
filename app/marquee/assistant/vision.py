"""Looking at the picture: a vision-capable model, used only when someone asks about what's on screen.

On this PC: Ollama (127.0.0.1:11434) models whose /api/show capabilities include "vision" (qwen3.5:9b preferred,
fast), called through Ollama's own /api/chat with thinking off, because Atlas' OpenAI-compatible endpoint drops
image parts. For everyone else: their OpenAI-compatible URL and key, images sent as image_url data URIs (works with
any model that accepts images, e.g. gpt-4o-mini)."""
import base64
import re
import time

import requests

from .. import ai, settings
from . import config

OLLAMA = "http://127.0.0.1:11434"
PREFERRED = ("qwen3.5:9b-q8_0", "qwen3.5:9b", "qwen3.5", "qwen2.5vl", "gemma-4", "gemma3", "huihui_ai/gemma-4",
             "qwen3.6:35b-a3b", "llava")
_found = {"at": 0, "models": []}


def ollama_vision_models():
    """Installed Ollama models that can see images, best first (cached for an hour)."""
    if time.time() - _found["at"] < (3600 if _found["models"] else 60):  # a busy or stopped Ollama: look again soon
        return _found["models"]
    _found["at"] = time.time()
    models = []
    try:
        tags = [m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=3).json().get("models", [])]
        for name in tags:
            try:
                caps = requests.post(f"{OLLAMA}/api/show", json={"model": name}, timeout=10).json().get("capabilities") or []
            except Exception:
                caps = []
            if "vision" in caps and "coding" not in name:
                models.append(name)
    except Exception:
        models = []

    def rank(n):
        return next((i for i, p in enumerate(PREFERRED) if n.startswith(p) or p in n), len(PREFERRED))
    _found["models"] = sorted(models, key=rank)
    return _found["models"]


def provider():
    """{"kind": "ollama"|"openai", "model", "url", "key"?, "name"} or None. Follows Marquee's AI setting."""
    s = settings.load()
    mode = ai.setting(s)  # auto | atlas | custom | builtin | classic (old names openai/off mapped)
    if mode == "classic":
        return None  # Classic: no model of any kind, nothing is contacted
    want = config.load().get("vision_model") or ""
    if mode in ("auto", "atlas"):
        local = ollama_vision_models()
        if want and want in local:
            return {"kind": "ollama", "model": want, "url": OLLAMA, "name": "Atlas" if ai.atlas_detected() else "Ollama"}
        if local:
            return {"kind": "ollama", "model": local[0], "url": OLLAMA, "name": "Atlas" if ai.atlas_detected() else "Ollama"}
    if mode in ("auto", "custom") and s["ai_url"] and s["ai_key"]:
        return {"kind": "openai", "model": want or s["ai_model"] or "gpt-4o-mini", "url": s["ai_url"].rstrip("/"),
                "key": s["ai_key"], "name": "AI"}
    if mode in ("auto", "builtin"):
        # The built-in llama-server with its vision projector (OpenAI-compatible, takes image_url parts). Asking
        # about the screen is an explicit request, so it may start during playback.
        try:
            from .. import localllm
            p = localllm.provider(allow_during_playback=True)
        except Exception:
            p = None
        if p:
            return {"kind": "openai", "model": p["model"], "url": p["url"], "key": p["key"], "name": p["name"],
                    "builtin": True}
    return None


def available():
    return provider() is not None


def ask(prompt, images, system=None, max_tokens=220, timeout=180, json_mode=False, temperature=0.2):
    """images: list of JPEG bytes. Returns (text, milliseconds, model)."""
    p = provider()
    if not p:
        raise RuntimeError("No model that can see images is set up")
    b64 = [base64.b64encode(i).decode() for i in images]
    t0 = time.perf_counter()
    if p["kind"] == "ollama":
        msgs = ([{"role": "system", "content": system}] if system else []) + \
            [{"role": "user", "content": prompt, "images": b64}]
        r = requests.post(f"{p['url']}/api/chat", timeout=timeout,
                          json={"model": p["model"], "messages": msgs, "stream": False, "think": False,
                                "keep_alive": "10m", "options": {"num_predict": max_tokens, "temperature": temperature},
                                **({"format": "json"} if json_mode else {})})
        r.raise_for_status()
        text = r.json()["message"].get("content") or ""
    else:
        parts = [{"type": "text", "text": prompt}] + \
            [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{x}"}} for x in b64]
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": parts}]
        r = requests.post(f"{p['url']}/chat/completions", timeout=timeout, headers={"Authorization": f"Bearer {p['key']}"},
                          json={"model": p["model"], "messages": msgs, "max_tokens": max_tokens, "temperature": temperature,
                                **({"response_format": {"type": "json_object"}} if json_mode else {})})
        if r.status_code == 400:
            raise RuntimeError(f"{p['model']} doesn't accept images (pick a vision model in Settings > AI)")
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, int((time.perf_counter() - t0) * 1000), p["model"]
