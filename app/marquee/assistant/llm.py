"""The language-model half: only for questions, summaries and requests the rules didn't understand.

Uses the same provider as the rest of Marquee (Atlas on this PC with a small fast model, or any OpenAI-compatible
URL and key). The model answers with one JSON object: a tool call or a short answer. Prompts are kept tiny."""
import json
import re
import time

import requests

from .. import ai

TOOLS = (
    "pause{mode:pause|resume|toggle}; seek{value:seconds,relative:bool}; volume{value:int,relative:bool}; "
    "mute{on:bool}; subtitles{on:bool,lang?:iso639-2}; audio{lang:iso639-2}; next_episode{}; previous_episode{}; "
    "play{title,season?,episode?}; find{query}; scene{query}; what_said{}; summarize{seconds}; cast{role?}; "
    "time_left{}; stop{}; look{} (needs to see the picture: faces, objects, places); who_screen{}; "
    "same_as{other:title}; loud_soon{} (jump scares, loud moments ahead)"
)
SYSTEM = ("You are the voice assistant of a home movie player. Reply with ONE JSON object only, no prose. "
          'Either {"tool":"<name>","args":{...}} to control the player, or {"answer":"<max 40 words>"} to answer. '
          "Tools: " + TOOLS + ". Going to a described moment is scene{query}. Use the context to answer questions about what is playing; "
          "if the context doesn't say, answer briefly from general knowledge or say you don't know.")


_seen = {"atlas_at": 0.0, "atlas_up": True, "ollama_at": 0.0, "ollama_model": None}


def _atlas_up():
    """Is Atlas' API listening right now? (checked at most every 20 s)"""
    import socket
    if time.time() - _seen["atlas_at"] > 20:
        _seen["atlas_at"] = time.time()
        try:
            socket.create_connection(("127.0.0.1", 8765), timeout=0.3).close()
            _seen["atlas_up"] = True
        except OSError:
            _seen["atlas_up"] = False
    return _seen["atlas_up"]


def _ollama_fast():
    """A small fast model in Ollama itself (what Atlas would pass through), for when Atlas isn't running."""
    if time.time() - _seen["ollama_at"] > 300 or _seen["ollama_model"] is None and time.time() - _seen["ollama_at"] > 20:
        _seen["ollama_at"] = time.time()
        try:
            names = [m["name"] for m in requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json().get("models", [])]
            _seen["ollama_model"] = next((n for f in ai.FAST_MODELS for n in names if n.startswith(f)), None)
        except Exception:
            _seen["ollama_model"] = None
    return _seen["ollama_model"]


def provider():
    try:
        p = ai._provider()
    except Exception:
        return None
    if p and p.get("name") == "Atlas" and p.get("url") == ai.ATLAS_URL and not _atlas_up():
        m = _ollama_fast()
        if m:  # Atlas is off but its models are there: talk to Ollama directly
            return {"url": "http://127.0.0.1:11434/v1", "key": "ollama", "model": m, "name": "Atlas"}
        return None  # neither is running: behave as if no AI is set up (rules and classic search still work)
    return p


def available():
    return provider() is not None


def complete(messages, max_tokens=200, temperature=0.2, timeout=60, json_mode=False):
    """One chat completion. Returns (text, milliseconds, model)."""
    p = provider()
    if not p:
        raise RuntimeError("No AI provider is set up (Settings > AI)")
    body = {"model": p["model"], "max_tokens": max_tokens, "temperature": temperature, "stream": False,
            "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.perf_counter()
    r = requests.post(f"{p['url']}/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {p['key']}"})
    if r.status_code == 400 and json_mode:  # an endpoint that doesn't know response_format: plain prompt is enough
        body.pop("response_format")
        r = requests.post(f"{p['url']}/chat/completions", json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {p['key']}"})
    r.raise_for_status()
    text = (r.json()["choices"][0]["message"].get("content") or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, int((time.perf_counter() - t0) * 1000), p["model"]


def route(question, context):
    """Ask the model what to do with a request the rules didn't catch: {"tool", "args"} or {"answer"}."""
    text, ms, model = complete([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": f"Context: {context}\nUser: {question}"}],
                               max_tokens=160, json_mode=True)
    data = _json(text)
    if not isinstance(data, dict) or not (data.get("tool") or data.get("answer")):
        data = {"answer": text.strip()[:300] or "Sorry, I didn't get that."}
    data["_ms"], data["_model"] = ms, model
    return data


def summarize(title, minutes, subtitle_text):
    text, ms, model = complete(
        [{"role": "system", "content": "Summarize this stretch of a film or episode from its subtitles in 2-4 short "
                                       "plain sentences. Name characters when the lines do. No preamble."},
         {"role": "user", "content": f"{title}, last {minutes} min:\n{subtitle_text}"}],
        max_tokens=180, temperature=0.3, timeout=90)
    return text.strip(), ms, model


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None
