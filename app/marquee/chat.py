"""Ask Marquee: the chat page. The model talks and names titles; the server finds those titles (library first, then
Seerr, matching the year) and sends them back as cards to show under the reply. "Remember that…" becomes a taste
memory note the page can undo.

Classic mode ("ai_provider" = classic) never calls a model: the page becomes a rule-based finder (mood, genre and era
chips plus a search box) and "I love / hate X" is still remembered by rules.

Nothing here runs while a video plays. History is kept on this PC (%APPDATA%\\Marquee\\chat.json)."""
import difflib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import requests

from . import ai, settings, taste

FILE = settings.DIR / "chat.json"
MAX_HISTORY = 80
TURNS_SENT = 8
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="chat")
_requests_cache = {"at": 0, "items": None, "busy": False}
BOLD = re.compile(r"\*\*([^*\n]{1,90}?)\*\*")
TITLE_YEAR = re.compile(r"^(.*?)\s*[\(\[]\s*((?:19|20)\d{2})\s*(?:[-–][^)\]]*)?[\)\]]\s*$")
MEMORY_CUE = re.compile(r"\b(remember|keep in mind|note that|don'?t forget|i\s+(?:really\s+)?(?:love|like|hate|adore|enjoy|"
                        r"dislike|prefer|can'?t stand|don'?t like|do not like)|i'?m (?:not )?(?:into|a fan)|"
                        r"never (?:show|recommend)|stop (?:showing|recommending)|my favou?rite)\b", re.I)


# ------------------------------------------------------------------ plumbing
def _server():
    from . import server
    return server


def _app():
    return _server().APP


def _cat():
    try:
        return _server().catalog()
    except Exception:
        return None


def playing():
    try:
        return bool(getattr(_app().player, "playing", False))
    except Exception:
        return False


def classic():
    return ai.setting() == "classic" or ai.mode() == "classic"


def engine_label():
    a = ai.available()
    if not a.get("llm"):
        return "Classic finder (no AI)"
    p = ai._provider(False) or {}
    model = (p.get("model") or "").split(":")[0]
    return f"{a['name']}" + (f" · {model}" if model and model != "atlas" else "")


# ------------------------------------------------------------------ history (on this PC)
def history():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _append(*msgs):
    with _lock:
        h = history() + list(msgs)
        h = h[-MAX_HISTORY:]
        settings.DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)


def mark_action(card_id, state):
    """A card under an answer was used (request sent, marked watched...): remember it, so it shows that later."""
    with _lock:
        h = history()
        for m in h:
            for c in m.get("actions") or []:
                if c.get("id") == card_id:
                    c["state"] = str(state)[:40]
                    c["state_at"] = time.time()
        settings.DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)


def clear():
    with _lock:
        try:
            FILE.unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ cards
def _poster_local(url):
    try:
        from .catalog import tmdb_img
        return tmdb_img(url) if url else None
    except Exception:
        return url


def library_card(r, why=None):
    cat = _cat()
    c = cat.card(r)
    return {"source": "library", "id": r["id"], "type": r["type"], "title": r.get("name"), "year": r.get("year"),
            "poster": c.get("poster"), "genres": r.get("genres") or [], "played": c.get("played"),
            "rating": r.get("rating"), "anime": c.get("anime"), "status": 5, "why": why}


def seerr_card(c, why=None):
    return {"source": "seerr", "id": c["id"], "type": c["type"], "title": c.get("title"), "year": c.get("year"),
            "poster": _poster_local(c.get("poster")), "backdrop": _poster_local(c.get("backdrop")),
            "status": c.get("status"), "anime": c.get("anime"), "rating": c.get("rating"),
            "overview": (c.get("overview") or "")[:300], "genres": c.get("genres") or [], "why": why}


def _fit_cards(cards, budget=6.0):
    """Add a fit score to each card; Seerr titles get their genres/people from the cached TMDB details first."""
    cat = _cat()
    p = taste.profile()
    if taste.is_empty(p):
        return cards

    def one(c):
        try:
            if c["source"] == "library":
                r = cat.get(c["id"])
                facts = {**cat.card(r), "people": (r.get("extra") or {}).get("people") or []} if r else c
            else:
                from . import details
                d = details.tmdb_detail(c["type"], c["id"]) or {}
                facts = {**c, **{k: v for k, v in d.items() if v}, "type": c["type"], "kind": c["type"]}
            c["fit"] = taste.score(facts, p)["score"]
        except Exception:
            pass
        return c
    futs = [_pool.submit(one, c) for c in cards]
    wait(futs, timeout=budget)
    return cards


# ------------------------------------------------------------------ resolving names -> titles
def _clean(s):
    s = re.sub(r"^(?:the|a|an)\s+", "", taste.norm(s))
    return s


def split_title(raw):
    raw = raw.strip().strip("\"'“”").strip()
    m = TITLE_YEAR.match(raw)
    if m:
        return m.group(1).strip(" -–:"), int(m.group(2))
    return raw, None


def names_in(text):
    """Titles the reply named: **Title (Year)** first, in order; duplicates dropped."""
    out, seen = [], set()
    for raw in BOLD.findall(text or ""):
        title, year = split_title(raw)
        if len(title) < 2 or len(title) > 80:
            continue
        k = _clean(title)
        if k and k not in seen:
            seen.add(k)
            out.append((title, year))
    return out


def resolve(title, year=None, kind=None):
    """A named title -> a card: the library first (title + year), then a Seerr search. None if nothing matches well."""
    cat = _cat()
    want = _clean(title)
    if not want:
        return None
    if cat is not None:
        with cat.lock:
            best = None
            for r in cat._of("Movie,Series"):
                name = _clean(r.get("name"))
                if name != want:
                    continue
                if year and r.get("year") and abs(int(r["year"]) - int(year)) > 1:
                    continue
                if kind and {"movie": "Movie", "tv": "Series"}.get(kind) != r["type"]:
                    continue
                best = r
                break
        if best:
            return library_card(best)
    app = _app()
    if not (app and app.seerr.ready):
        return None
    try:
        results = [c for c in app.seerr.search(title) if c.get("type") in ("movie", "tv")]
    except Exception:
        return None

    def ok(c, exact):
        name = _clean(c.get("title"))
        same = name == want if exact else difflib.SequenceMatcher(None, name, want).ratio() >= .88
        if not same:
            return False
        if year and c.get("year"):
            return abs(int(c["year"]) - int(year)) <= 1
        return True
    pick = next((c for c in results if ok(c, True)), None) or next((c for c in results if year and ok(c, False)), None)
    if not pick:
        return None
    owned = cat.by_tmdb(pick["type"], pick["id"]) if cat is not None else None
    if owned:
        return library_card(owned)
    return seerr_card(pick)


def resolve_all(names, limit=6):
    futs = [_pool.submit(resolve, t, y) for t, y in names[:limit + 3]]
    cards, keys = [], set()
    for (t, y), f in zip(names[:limit + 3], futs):
        try:
            c = f.result(timeout=12)
        except Exception:
            c = None
        if not c:
            continue
        k = (c["source"], str(c["id"]))
        if k in keys:
            continue
        keys.add(k)
        c["named"] = t
        cards.append(c)
        if len(cards) >= limit:
            break
    return cards


# ------------------------------------------------------------------ context for the model
def _requests_titles():
    """The viewer's Seerr requests (titles), cached for half an hour; filled in the background the first time."""
    rc = _requests_cache
    if rc["items"] is not None and time.time() - rc["at"] < 1800:
        return rc["items"]
    if not rc["busy"]:
        rc["busy"] = True

        def fill():
            try:
                app = _app()
                items = app.seerr.my_requests() if app and app.seerr.ready else []
                rc["items"] = [f"{r['title']} ({r.get('year') or '?'})" for r in items if r.get("title")][:20]
            except Exception:
                rc["items"] = rc["items"] or []
            rc["at"] = time.time()
            rc["busy"] = False
        threading.Thread(target=fill, daemon=True).start()
    return rc["items"] or []


def _like_target(message):
    """"something like Dune" -> ("Dune", None)."""
    m = re.search(r"\b(?:like|similar to|same vibe as|reminds? me of|in the vein of)\s+[\"“]?([A-Z0-9][^,.?!\"”]{1,60})",
                  message)
    if not m:
        return None
    t = re.sub(r"\s+(?:but|and|or|with|that|which|for)\b.*$", "", m.group(1)).strip()
    return split_title(t)


def context(message):
    """(text block, candidate pool) for the system prompt: taste, owned titles, requestable ideas, requests."""
    cat = _cat()
    intent = ai.read_query(message)
    parsed = taste.parse_text(message)
    genres = list(dict.fromkeys(intent["genres"] + [g for g, v in parsed["genres"].items() if v > 0]))
    genres = taste.canon_genres(genres) or genres
    kind = intent["kind"]
    like = _like_target(message)
    lib_lines = []
    p = taste.profile()
    if cat is not None:
        with cat.lock:
            rows = cat._of("Movie,Series")
            facts = [(r, {**cat.card(r), "people": (r.get("extra") or {}).get("people") or []}) for r in rows]
            watched = {r["id"]: cat._played(r) for r in rows}
        scored = []
        for r, f in facts:
            s = taste.score(f, p)["score"] if not taste.is_empty(p) else 50
            rel = len(set(taste.canon_genres(r.get("genres"))) & set(genres)) if genres else 0
            if kind == "movie" and r["type"] != "Movie" or kind == "tv" and r["type"] != "Series":
                rel -= 2
            scored.append((rel * 30 + s + random.random() * 6, r))
        scored.sort(key=lambda x: -x[0])
        for _, r in scored[:70]:
            tag = "seen" if watched.get(r["id"]) else "unwatched"
            lib_lines.append(f"{r.get('name')} ({r.get('year') or '?'}; {'series' if r['type'] == 'Series' else 'film'}; "
                             f"{'/'.join((r.get('genres') or [])[:2])}; {tag})")
    req_lines = []
    app = _app()
    if app and app.seerr.ready:
        pool = []
        try:
            if like:
                hit = resolve(like[0], like[1])
                if hit:
                    tmdb = hit["id"] if hit["source"] == "seerr" else ((cat.get(hit["id"]) or {}).get("provider") or {}).get("Tmdb")
                    k = hit["type"] if hit["source"] == "seerr" else ("tv" if hit["type"] == "Series" else "movie")
                    if tmdb:
                        pool += [{**c, "why": f"like {hit['title']}"} for c in app.seerr.recommended(k, tmdb, limit=14)]
            want = [g for g in genres if g in ai.TMDB_MOVIE or g in ai.TMDB_TV] or (taste.top_genres(2) if not like else [])
            if want:
                pool += [{**c, "genres": want} for c in app.seerr.by_genre(want, kind, limit=24)]
            if not pool:
                pool += app.seerr.cards("/discover/trending")[:16]
        except Exception as e:
            print("chat: seerr ideas failed:", type(e).__name__, e)
        seen = set()
        ranked = taste.rank([c for c in pool if not (c["type"], c["id"]) in seen and not seen.add((c["type"], c["id"]))])
        for c in ranked[:24]:
            st = {2: "requested", 3: "requested", 4: "partly in library", 5: "in library"}.get(c.get("status"), "requestable")
            req_lines.append(f"{c['title']} ({c.get('year') or '?'}; {'series' if c['type'] == 'tv' else 'film'}; {st})")
    reqs = _requests_titles()
    summary = taste.summary(1800) or "Nothing known yet (new viewer)."
    ref = (f"\n\nTHE VIEWER WANTS TITLES SIMILAR TO: {like[0]}" + (f" ({like[1]})" if like[1] else "") +
           ". Don't recommend that title itself; sequels and the same director are fine.") if like else ""
    block = (f"VIEWER TASTE PROFILE:\n{summary}\n\n"
             f"LIBRARY (owned, can watch now):\n" + ("\n".join(lib_lines) or "(empty)") + "\n\n"
             f"COULD REQUEST (not owned yet, popular picks that fit):\n" + ("\n".join(req_lines) or "(none found)") +
             ("\n\nALREADY REQUESTED: " + ", ".join(reqs) if reqs else "") + ref)
    return block


SYSTEM = """You are Marquee, the friendly film and TV guide inside a home media app. You chat about movies and shows and recommend what to watch next.

Rules:
- Mix: titles from LIBRARY (they can watch now) and, when they fit, one or two from COULD REQUEST or any other real, well-known film or show they could request. Never invent titles.
- Write every title in bold with its year, like **Arrival (2016)**. Recommend 2 to 5 titles, each with a short reason tied to the viewer's taste.
- Don't recommend titles marked "seen" unless asked for a rewatch.
- Keep replies under 120 words, warm and casual. No emojis, no headings, no tables.
- Say where a title is: "in your library" or "you can request it".
- If (and only if) the viewer just told you something lasting about their taste (a like, a dislike, "remember ..."), add one final line exactly like: REMEMBER: <short note in third person, e.g. Hates jump scares>. Never add it for one-off requests like "something scary tonight"."""


def _messages(message, hist):
    msgs = [{"role": "system", "content": SYSTEM + "\n\n" + context(message)}]
    for m in hist[-TURNS_SENT:]:
        if m.get("role") in ("user", "assistant") and m.get("text"):
            msgs.append({"role": m["role"], "content": m["text"][:1200]})
    msgs.append({"role": "user", "content": message})
    return msgs


# ------------------------------------------------------------------ streaming
MARK = "REMEMBER:"


def visible(text):
    """What the page may show of a partial reply: no hidden REMEMBER line, no <think> block, and nothing that might
    still turn into one of those."""
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S)
    lines = text.split("\n")
    out = []
    for k, line in enumerate(lines):
        bare = line.strip().lstrip("*_ ").upper()
        last = k == len(lines) - 1
        if bare.startswith(MARK) or bare.startswith("REMEMBER :"):
            break
        if last and bare and MARK.startswith(bare[:len(MARK)]) and len(bare) < len(MARK):
            break  # maybe the start of "REMEMBER:"
        if last and line.endswith("<") or last and re.search(r"<t?h?i?n?k?$", line):
            line = re.sub(r"<[a-z]*$", "", line)
        out.append(line)
    return "\n".join(out)


def memory_line(text):
    m = re.search(r"^[\s*_]*REMEMBER\s*:\s*(.+)$", text or "", re.M | re.I)
    if not m:
        return None
    note = m.group(1).strip().strip("*_\"' ").rstrip(".")
    return note if 3 <= len(note) <= 200 and note.lower() not in ("none", "n/a", "nothing") else None


def _stream_llm(msgs, timeout=90):
    """Yield text pieces from the provider (OpenAI-compatible streaming). Falls back to one ai.chat() call."""
    p = ai._provider(allow_during_playback=False)
    if not p:
        return
    try:
        with requests.post(f"{p['url']}/chat/completions", stream=True, timeout=(10, timeout),
                           headers={"Authorization": f"Bearer {p['key']}"},
                           json={"model": p["model"], "messages": msgs, "stream": True, "max_tokens": 420,
                                 "temperature": .7}) as r:
            if r.status_code == 503 and p["kind"] == "builtin":
                raise RuntimeError("The built-in AI is busy")
            r.raise_for_status()
            got = False
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                piece = delta.get("content")
                if piece:
                    got = True
                    yield piece
            if got:
                return
    except requests.RequestException as e:
        print("chat: streaming failed, trying once without:", type(e).__name__, e)
    hist = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[1:-1])
    text = ai.chat((f"Conversation so far:\n{hist}\n\n" if hist else "") + "user: " + msgs[-1]["content"],
                   msgs[0]["content"], max_tokens=420, timeout=timeout, temperature=.7)
    if text:
        yield text


def stream(message, client_history=None, pick=None, intent=None):
    """The chat reply as events (dicts): status, delta {text}, cards {items}, memory {note}, done {engine}, error,
    plus action {card} and ui {do, ...} when the message is a command (marquee.chat_actions: "pull up Dune",
    "request seasons 5-8 of Doctor Who", "is everything ok"). pick/intent: the answer to a "which one?" card."""
    message = (message or "").strip()[:1000]
    if not message:
        yield {"kind": "error", "text": "Say something first"}
        return
    if playing():
        yield {"kind": "error", "text": "Chat waits while a video is playing."}
        return
    hist = history()
    now = time.time()
    try:
        from . import chat_actions
        llm = not classic()
        act = chat_actions.plan(message, hist, pick, intent, llm=llm)
    except Exception as e:
        print("chat: reading the command failed:", type(e).__name__, e)
        act = None
    if act:
        yield from chat_actions.run(act, message, hist, pick=pick, llm=llm)
        return
    if classic():
        yield from _classic_reply(message, hist)
        return
    yield {"kind": "status", "text": "Looking through your shelves…"}
    try:
        msgs = _messages(message, hist)
    except Exception as e:
        print("chat: context failed:", type(e).__name__, e)
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}]
    yield {"kind": "status", "text": "Thinking about your taste…"}
    full, shown = "", ""
    try:
        for piece in _stream_llm(msgs):
            full += piece
            v = visible(full)
            if len(v) > len(shown) and v.startswith(shown):
                yield {"kind": "delta", "text": v[len(shown):]}
                shown = v
            elif v != shown and not v.startswith(shown):
                yield {"kind": "replace", "text": v}
                shown = v
    except Exception as e:
        name = ai.available()["name"]
        print("chat: the model failed:", type(e).__name__, e)
        if not shown:  # nothing said yet: answer from the shelves instead (rules, no model), and say so
            yield {"kind": "notice", "text": f"{name} isn't answering right now, so these come from your shelves and Seerr."}
            yield from _classic_reply(message, hist)
            return
        yield {"kind": "error", "text": f"{name} stopped answering ({type(e).__name__}).", "retry": True}
        return
    answer = visible(full).strip()
    if answer != shown.strip():
        yield {"kind": "replace", "text": answer}
    if not answer:
        yield {"kind": "error", "text": "No answer came back. Try asking another way."}
        return
    names = names_in(answer)
    like = _like_target(message)
    if like:
        names = [n for n in names if _clean(n[0]) != _clean(like[0])] or names
    cards = []
    if names:
        yield {"kind": "status", "text": "Finding those titles…"}
        cards = _fit_cards(resolve_all(names))
        yield {"kind": "cards", "items": cards}
    note = None
    cue = MEMORY_CUE.search(message)
    text = memory_line(full) if cue else None
    if not text and cue:
        text = taste.extract_memory(message)
    if text:
        try:
            note = taste.add_note(text, source="chat")
            yield {"kind": "memory", "note": note}
        except ValueError:
            note = None
    engine = engine_label()
    _append({"role": "user", "text": message, "at": now},
            {"role": "assistant", "text": answer, "cards": cards, "memory": note, "engine": engine, "at": time.time()})
    yield {"kind": "done", "engine": engine}


# ------------------------------------------------------------------ Classic: the finder (no AI)
ERAS = {"Classics": (1900, 1979), "80s": (1980, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
        "2010s": (2010, 2019), "New": (time.localtime().tm_year - 2, 2100)}
FIND_GENRES = ["Action", "Comedy", "Drama", "Horror", "Romance", "Sci-fi", "Fantasy", "Thriller", "Mystery",
               "Animation", "Family", "Crime", "Documentary", "Adventure", "War", "Western"]
FIND_MOODS = ["Feel-good", "Mind-bending", "Dark", "Cosy", "Epic", "Funny", "Scary", "Romantic"]


def find(q="", genres=(), moods=(), era=None, kind="any", limit=18):
    """Rule-based finder: library matches (best fit first) plus requestable picks from Seerr. Honest answer line."""
    cat = _cat()
    q = (q or "").strip()[:200]
    want = []
    for g in genres or []:
        want += taste.canon_genres([{"Sci-fi": "Science Fiction"}.get(g, g)])
    parsed = taste.parse_text(q) if q else {"genres": {}}
    want += [g for g, v in parsed["genres"].items() if v > 0]
    avoid = [g for g, v in parsed["genres"].items() if v < 0]
    for m in moods or []:
        want += taste.VIBES.get(m, [])
    want = list(dict.fromkeys(want))
    years = ERAS.get(era) if era else None
    if q and not years:
        years = ai.read_query(q).get("years")
    if q and kind == "any":
        kind = ai.read_query(q).get("kind") or "any"
    lib, titles = [], []
    if cat is not None:
        if q and not want:
            titles = [c for c in cat.search(q, types=("Movie", "Series")) if c]
        with cat.lock:
            rows = cat._of({"movie": "Movie", "tv": "Series"}.get(kind, "Movie,Series"))
            for r in rows:
                g = set(taste.canon_genres(r.get("genres")))
                if want and not (g & set(want)):
                    continue
                if avoid and g & set(avoid):
                    continue
                if years and not (r.get("year") and years[0] <= int(r["year"]) <= years[1]):
                    continue
                if not want and not years and not q:
                    continue
                if q and not want and not years and r["id"] not in {t["id"] for t in titles}:
                    continue
                lib.append({**cat.card(r), "people": (r.get("extra") or {}).get("people") or [],
                            "match": len(g & set(want))})
    p = taste.profile()
    for c in lib:
        c["fit"] = taste.score(c, p)["score"] if not taste.is_empty(p) else 50
    lib.sort(key=lambda c: (-c["match"], -c["fit"]))
    cards = [library_card(cat.get(c["id"])) | {"fit": c["fit"] if not taste.is_empty(p) else None} for c in lib[:limit]]
    req = []
    app = _app()
    tm = [g for g in want if g in ai.TMDB_MOVIE or g in ai.TMDB_TV]
    if app and app.seerr.ready and (tm or (q and not want)):
        try:
            pool = app.seerr.by_genre(tm, kind, limit=30) if tm else [c for c in app.seerr.search(q) if c.get("status") != 5]
            pool = [{**c, "genres": tm or c.get("genres") or []} for c in pool
                    if not years or (c.get("year") and years[0] <= int(c["year"]) <= years[1])]
            for c in taste.rank(pool)[:12]:
                req.append(seerr_card(c) | {"fit": c["fit"] if not taste.is_empty(p) else None})
        except Exception as e:
            print("finder: seerr failed:", type(e).__name__, e)
    bits = [", ".join(g.lower() for g in (list(genres) + list(moods))) or None, era and (era if era != "New" else "recent"),
            q and f"“{q}”"]
    what = " · ".join(b for b in bits if b) or "everything"
    if cards or req:
        answer = (f"{len(cards)} in your library" + (f" and {len(req)} you could request" if req else "") +
                  f" for {what}" + (", best fit for your taste first." if not taste.is_empty(p) else "."))
    else:
        answer = f"Nothing matched {what}. Try fewer filters."
    return {"answer": answer, "items": cards, "requestable": req}


LIKE_RE = re.compile(r"(?:^|\b)(?:something|anything|more|stuff|shows?|films?|movies?|series|ones?)?\s*"
                     r"(?:like|similar to|in the vein of|along the lines of)\s+(?P<t>.+?)\s*[?.!]*$", re.I)
ONLY_REQ_RE = re.compile(r"\b(not (?:in|on) my (?:library|shelves|pc)|(?:i )?don'?t (?:own|have)|to request|"
                         r"(?:only )?requestable|not downloaded|new to me)\b", re.I)
ONLY_OWN_RE = re.compile(r"\b(in my library|on my (?:shelves|pc)|(?:that )?i (?:own|have)|already (?:have|own))\b", re.I)


def _seen(cat, c):
    """Has the viewer watched this card's title (played here, marked watched, or watched and removed)?"""
    if c.get("played"):
        return True
    kind = "tv" if c.get("type") in ("Series", "tv") else "movie"
    tmdb = c.get("tmdb") or (c.get("id") if c.get("type") in ("movie", "tv") else None)
    if not tmdb and c.get("source") == "library" and cat is not None:
        tmdb = ((cat.get(c.get("id")) or {}).get("provider") or {}).get("Tmdb")
    try:
        return bool(tmdb and cat.title_flag(kind, tmdb))
    except Exception:
        return False


def _like_reply(seed_text, only=None):
    """"Something like X" without a model: Seerr's recommendations for X (titles to request) plus library titles
    that share its genres, best taste fit first - never X itself or anything already watched."""
    from . import details, watched
    cat = _cat()
    try:
        seed = watched.find(seed_text)
    except watched.Ambiguous as e:
        seed = e.candidates[0]
    except Exception:
        return None
    kind, tmdb, name = seed.get("kind"), seed.get("tmdb"), seed.get("title") or seed_text
    p = taste.profile()
    lib, req = [], []
    seed_genres = set()
    try:  # Home's series rule: only ever the next part of a film series, never a later one
        from . import homemix
        ctx = homemix.Ctx(_app(), cat) if cat is not None else None
    except Exception:
        ctx = None

    def in_order(card):
        if ctx is None:
            return True
        try:
            return bool(ctx.rule(card, swap=False))
        except Exception:
            return True
    if seed.get("item_id") and cat is not None:
        r = cat.get(seed["item_id"])
        seed_genres = set(taste.canon_genres((r or {}).get("genres")))
    if tmdb and only != "own":
        try:
            pool = [c for c in details.recommended(kind, tmdb) if str(c.get("id")) != str(tmdb)]
        except Exception:
            pool = []
        app = _app()
        if not seed_genres and app and app.seerr.ready:
            try:
                d = details.tmdb_detail(kind, tmdb) or {}
                seed_genres = set(taste.canon_genres(d.get("genres")))
            except Exception:
                pass
        for c in taste.rank([{**c, "genres": c.get("genres") or list(seed_genres)} for c in pool]):
            if not _seen(cat, c) and in_order(c):
                req.append(seerr_card(c, why=f"Like {name}") | {"fit": c.get("fit") if not taste.is_empty(p) else None})
    if only != "req" and cat is not None and seed_genres:
        with cat.lock:
            rows = cat._of("Movie" if kind == "movie" else "Series")
        for r in rows:
            g = set(taste.canon_genres(r.get("genres")))
            if r["id"] == seed.get("item_id") or len(g & seed_genres) < max(1, min(2, len(seed_genres))):
                continue
            c = library_card(r, why=f"Like {name}")
            if _seen(cat, c) or not in_order(cat.card(r)):
                continue
            c["fit"] = taste.score({**cat.card(r), "people": (r.get("extra") or {}).get("people") or []}, p)["score"] \
                if not taste.is_empty(p) else None
            lib.append(c)
        lib.sort(key=lambda c: -(c.get("fit") or 0))
    lib, req = lib[:4], req[:8]
    if not lib and not req:
        return {"answer": f"I couldn't find anything new like {name} right now.", "items": [], "requestable": []}
    parts = ([f"{len(req)} to request"] if req else []) + ([f"{len(lib)} on your shelves"] if lib else [])
    return {"answer": f"Like {name}: " + " and ".join(parts) + (", best fit for your taste first." if not taste.is_empty(p) else "."),
            "items": lib, "requestable": req}


def _last_query(hist):
    for h in reversed(hist or []):
        if h.get("role") == "user" and h.get("text"):
            return h["text"]
    return ""


def _classic_reply(message, hist):
    now = time.time()
    note = None
    text = taste.extract_memory(message)
    if text:
        note = taste.add_note(text, source="chat")
        yield {"kind": "memory", "note": note}
        answer = f"Saved to memory: {note['text']}."
        res = {"items": [], "requestable": []}
    else:
        only = "req" if ONLY_REQ_RE.search(message) else "own" if ONLY_OWN_RE.search(message) else None
        query = message
        m = LIKE_RE.search(message)
        if not m and only:  # "how about not in my library": the same question again, narrowed
            prev = _last_query(hist)
            m = LIKE_RE.search(prev)
            query = prev or message
        res = _like_reply(m.group("t"), only) if m else None
        if res is None:
            res = find(query)
            if only == "req":
                res["items"] = []
            elif only == "own":
                res["requestable"] = []
        answer = res["answer"]
    yield {"kind": "delta", "text": answer}
    cards = res["items"][:8] + res["requestable"][:4]
    if cards:
        yield {"kind": "cards", "items": cards}
    _append({"role": "user", "text": message, "at": now},
            {"role": "assistant", "text": answer, "cards": cards, "memory": note, "engine": engine_label(),
             "at": time.time()})
    yield {"kind": "done", "engine": engine_label()}


# ------------------------------------------------------------------ suggestion chips
def suggestions():
    p = taste.profile()
    out = []
    liked = [t for t in p["titles"]["liked"] if t.get("title")]
    if liked:
        out.append(f"Something like {random.choice(liked[:6])['title']}")
    top = taste.top_genres(3)
    if top:
        g = random.choice(top).lower()
        out.append(f"A {g} {'show' if p['kinds'].get('tv', 0) > p['kinds'].get('movie', 0) else 'film'} I haven't seen")
    out.append("What should I request next?")
    out.append("Surprise me with something different")
    out.append("Something cosy for tonight")
    if not liked:
        out.append("Remember that I love slow-burn mysteries")
    return out[:5]
