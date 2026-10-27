"""Optional AI touches: a short welcome line on the home page and a few picks from your library.

Where the words come from (setting "ai_provider"):
    auto     your own key if set, else Atlas when it runs on this PC, else the built-in AI if it was downloaded,
             else Classic
    custom   any OpenAI-compatible chat API (your URL, key and model)        ("openai" is the old name)
    atlas    Atlas on this PC (found automatically; its key is read from its own folder when needed)
    builtin  the built-in AI (marquee.localllm: llama.cpp + a small Qwen3-VL model on this PC)
    classic  no AI at all, ever: prepared lines and rule-made picks (marquee.scripted)  ("off" is the old name)

Used sparingly: results are cached for hours, and nothing is asked while a video plays (a local model shares the
GPU). The built-in model is never even started during playback unless the caller passes allow_during_playback."""
import json
import os
import re
from pathlib import Path

import requests

from . import settings
from . import taste as taste_profile

ATLAS_URL = "http://127.0.0.1:8765/v1"
ATLAS_HOMES = [Path(os.environ.get("ATLAS_HOME", "")), Path("C:/LocalAI")]
PROVIDERS = ("auto", "atlas", "custom", "builtin", "classic")
ALIASES = {"openai": "custom", "off": "classic"}


def _atlas_key():
    for home in ATLAS_HOMES:
        env = home / ".env"
        if str(home) and env.is_file():
            m = re.search(r"^LOCALAI_API_KEY\s*=\s*[\"']?([^\"'\r\n]+)", env.read_text(encoding="utf-8", errors="ignore"), re.M)
            if m:
                return m.group(1).strip()
    return None


def atlas_detected():
    return bool(_atlas_key())


# Small, fast instruct models Atlas can pass straight through (no tools): a few seconds instead of half a
# minute for the full assistant. The first one installed wins; "atlas" itself is the fallback.
FAST_MODELS = ("qwen3:4b-instruct", "qwen3.5:9b", "qwen2.5:7b-instruct", "llama3.2:3b", "gemma3:4b")
_atlas_models = {"at": 0, "pick": None}


def _atlas_model():
    import time
    if time.time() - _atlas_models["at"] > 3600:
        _atlas_models["at"] = time.time()
        try:
            ids = [m["id"] for m in requests.get(f"{ATLAS_URL}/models", timeout=5,
                                                 headers={"Authorization": f"Bearer {_atlas_key()}"}).json()["data"]]
            _atlas_models["pick"] = next((i for f in FAST_MODELS for i in ids if i.startswith(f)), None)
        except Exception:
            _atlas_models["pick"] = None
    return _atlas_models["pick"] or "atlas"


def setting(s=None):
    """The ai_provider setting, with the old names mapped to the new ones."""
    p = (s or settings.load()).get("ai_provider") or "auto"
    p = ALIASES.get(p, p)
    return p if p in PROVIDERS else "auto"


def _builtin(allow_during_playback):
    try:
        from . import localllm
        return localllm.provider(allow_during_playback)
    except Exception as e:
        print("built-in AI unavailable:", e)
        return None


_atlas_seen = {"at": 0.0, "up": False}


def _atlas_up():
    """Is Atlas actually answering on its port right now? (A quick local connect, remembered for 15 s.) Its key
    file can exist while it's switched off; Auto then moves on to the next provider instead of failing."""
    import socket
    import time
    now = time.time()
    if now - _atlas_seen["at"] < 15:
        return _atlas_seen["up"]
    try:
        with socket.create_connection(("127.0.0.1", int(ATLAS_URL.split(":")[2].split("/")[0])), timeout=0.3):
            up = True
    except OSError:
        up = False
    _atlas_seen.update(at=now, up=up)
    return up


def _provider(allow_during_playback=True):
    """The language model to use, as {"url", "key", "model", "name", "kind", "vision"}, or None (Classic: no model).
    Cheap to call: it never loads or starts anything. allow_during_playback only matters for the built-in AI: the
    default (True) is for explicit requests (Ask Atlas, the ending comment); background work passes False."""
    s = settings.load()
    p = setting(s)
    if p == "classic":
        return None
    if p in ("auto", "custom") and s["ai_url"] and s["ai_key"]:
        return {"url": s["ai_url"].rstrip("/"), "key": s["ai_key"], "model": s["ai_model"] or "gpt-4o-mini",
                "name": "AI", "kind": "custom", "vision": True}
    # Atlas (a separate local assistant) only when chosen in Settings; Auto never picks it
    if p == "atlas" and _atlas_key():
        return {"url": ATLAS_URL, "key": _atlas_key(), "model": s["ai_model"] or _atlas_model(), "name": "Atlas",
                "kind": "atlas", "vision": False}  # Atlas' OpenAI endpoint drops images (the assistant uses Ollama)
    if p in ("auto", "builtin"):
        return _builtin(allow_during_playback)
    return None


def mode():
    """What's answering right now: "custom" | "atlas" | "builtin" | "classic"."""
    p = _provider()
    return p["kind"] if p else "classic"


def available():
    """For the UI. "on" stays true in Classic mode: the welcome line, picks and search answers still come, from
    prepared lines and rules instead of a model ("llm": false)."""
    p = _provider()
    if not p:
        return {"on": True, "name": "Marquee", "mode": "classic", "llm": False, "vision": False}
    return {"on": True, "name": p["name"], "mode": p["kind"], "llm": True, "vision": bool(p.get("vision"))}


def vision_capable():
    """Does the current provider accept images in chat()? (Built-in: yes, through its vision projector.)"""
    p = _provider()
    return bool(p and p.get("vision"))


def vision_provider(allow_during_playback=True):
    """An OpenAI-compatible provider that takes image_url parts, or None. For the assistant's vision code."""
    p = _provider(allow_during_playback)
    return p if p and p.get("vision") else None


def _playing():
    try:
        from . import server
        return bool(getattr(getattr(server.APP, "player", None), "playing", False))
    except Exception:
        return False


def chat(prompt, system, max_tokens=220, timeout=60, allow_during_playback=False, images=None, temperature=0.8):
    """One short completion, or None (Classic mode, or nothing may run now). images: JPEG bytes, only for a
    provider with vision. Never starts the built-in model while a video plays unless allow_during_playback."""
    p = _provider(allow_during_playback)
    if not p:
        return None
    if p["kind"] == "builtin" and _playing() and not allow_during_playback:
        return None
    content = prompt
    if images:
        import base64
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
            for i in images]
    r = requests.post(f"{p['url']}/chat/completions", timeout=timeout, headers={"Authorization": f"Bearer {p['key']}"},
                      json={"model": p["model"], "max_tokens": max_tokens, "temperature": temperature, "stream": False,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]})
    if r.status_code == 503 and p["kind"] == "builtin":
        return None  # busy (a video started) or it couldn't start
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _taste(app):
    jf = app.jf
    recent = jf.items(Recursive="true", Filters="IsPlayed", SortBy="DatePlayed", SortOrder="Descending",
                      IncludeItemTypes="Movie,Episode", Limit=12)
    unwatched = jf.items(Recursive="true", Filters="IsUnplayed", IncludeItemTypes="Movie,Series", SortBy="Random", Limit=40)
    def label(i):
        return (i.get("SeriesName") or i.get("Name")) + (f" ({i['ProductionYear']})" if i.get("ProductionYear") else "")
    seen = []
    for i in recent:
        if label(i) not in seen:
            seen.append(label(i))
    wishlist = []
    if app.seerr.ready:
        try:
            wishlist = [c["title"] for c in app.seerr.cards("/discover/movies/upcoming")[:8]]
        except Exception:
            pass
    return seen, unwatched, wishlist


HOLIDAYS = [((1, 1), "New Year's Day"), ((2, 14), "Valentine's Day"), ((3, 17), "St. Patrick's Day"),
            ((7, 4), "the Fourth of July"), ((10, 31), "Halloween"), ((12, 25), "Christmas"), ((12, 31), "New Year's Eve")]


def _movable_holidays(year):
    """Easter (Western) and US Thanksgiving for a year."""
    import datetime as dt
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    nov1 = dt.date(year, 11, 1)
    thanks = nov1 + dt.timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    return [(dt.date(year, month, day + 1), "Easter"), (thanks, "Thanksgiving")]


def upcoming_holiday(today=None, within=14):
    """(name, days_left) of the nearest holiday within `within` days, or None."""
    import datetime as dt
    today = today or dt.datetime.now()
    d0 = today.date() if isinstance(today, dt.datetime) else today
    found = []
    for year in (d0.year, d0.year + 1):
        found += [(dt.date(year, m, d), name) for (m, d), name in HOLIDAYS] + _movable_holidays(year)
    day, name = min((x for x in found if x[0] >= d0), key=lambda x: x[0])
    left = (day - d0).days
    return (name, left) if left <= within else None


def season_note(today=None):
    """Time of year for the greeting: the season, part of day, and a holiday within two weeks."""
    import datetime as dt
    today = today or dt.datetime.now()
    season = ["winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer", "autumn", "autumn",
              "autumn", "winter"][today.month - 1]
    part = "late night" if today.hour < 5 else "morning" if today.hour < 12 else "afternoon" if today.hour < 18 else "evening"
    note = f"It's a {today:%A} {part} in {season} ({today:%B %d})"
    h = upcoming_holiday(today)
    if h:
        note += f"; {h[0]} is {'today' if h[1] == 0 else f'in {h[1]} days'}"
    return note


def watching_now(app):
    """Shows in progress with the season they're on (Next Up), e.g. 'South Park, season 3'."""
    out = []
    for i in app.jf.next_up(limit=6):
        if i.get("SeriesName"):
            out.append(f"{i['SeriesName']} (up next: season {i.get('ParentIndexNumber')}, episode {i.get('IndexNumber')})")
    return out


def welcome(app):
    if mode() == "classic":
        from . import scripted
        return scripted.welcome(app)
    try:
        seen, unwatched, wishlist = _taste(app)
        name = settings.load()["user_name"] or "there"
        text = chat(
            f"Viewer: {name}. {season_note()}. Recently watched: {', '.join(seen[:8]) or 'nothing yet'}. "
            f"Shows in progress: {', '.join(watching_now(app)) or 'none'}. "
            f"In their library but not watched: {', '.join(i['Name'] for i in unwatched[:15])}. "
            f"Coming soon: {', '.join(wishlist) or 'unknown'}."
            + (f"\nTaste profile:\n{taste_profile.summary()}" if taste_profile.summary() else ""),
            "You write the one-line greeting at the top of a home movie app. One or two short, playful sentences "
            "(max 30 words). Weave in the time of year or a holiday that's coming up when it fits naturally (spooky "
            "picks near Halloween, cosy ones in winter), or the season of a show they're partway through, and "
            "tease one specific title. No emojis, no quotes, no hashtags.", max_tokens=90)
        text = re.sub(r"(\*{1,2}|_{2}|`|^#+\s*)", "", text or "", flags=re.M)  # small models add markdown
        return re.sub(r"\s+", " ", text).strip(' "') or None
    except Exception as e:
        print("ai welcome failed:", e)
        return None


# ------------------------------------------------------------------ search
GENRE_WORDS = {
    "Comedy": r"funny|comed|hilarious|laugh|sitcom|silly|goofy|light-?hearted|rom-?com",
    "Horror": r"horror|scary|spooky|creepy|frighten|terrif|haunt|slasher|zombie|ghost|halloween|jump ?scare",
    "Science Fiction": r"sci-?fi|science fiction|space|alien|future|futurist|dystop|robot|cyberpunk|time travel",
    "Romance": r"romanc|romantic|love stor|date night|rom-?com",
    "Action": r"\baction|fight|explosion|explosive|adrenaline|car chase|martial art",
    "Thriller": r"thriller|suspense|tense|edge of (?:my|your|the) seat|nail-?bit",
    "Mystery": r"myster|whodunn?it|detective|puzzle", "Crime": r"crime|heist|gangster|mafia|\bmob\b|\bcops?\b",
    "Animation": r"animat|cartoon|\banime\b|pixar", "Family": r"family|\bkids?\b|children|all ages",
    "Documentary": r"documentar|\bdocu|true story|real life", "War": r"\bwar\b|military|soldier",
    "Western": r"western|cowboy", "Fantasy": r"fantasy|magic|dragon|wizard|witch|fairy ?tale",
    "Drama": r"drama|emotional|\bsad\b|tearjerk|\bcry\b|crying", "Adventure": r"adventure|quest|journey|treasure",
    "Music": r"musical|\bmusic\b|concert", "History": r"histor|period piece|biopic",
}
# Moods and phrases that mean a mix of genres ("feel good" isn't a genre, but comedies and family films are).
MOODS = {
    "feel good": (r"feel[- ]?good|uplifting|wholesome|cheer (?:me|us) up|\bcos[yz]\b|comfort", ["Comedy", "Family", "Animation", "Romance"]),
    "mind-bending": (r"mind[- ]?(?:bend|blow|f)|twist|trippy|cerebral|make[s]? (?:me|you) think|thought[- ]provoking",
                     ["Science Fiction", "Mystery", "Thriller"]),
    "chill": (r"\bchill|easy watch|background|relax|\blazy\b|switch (?:my|your) brain off", ["Comedy", "Animation"]),
    "intense": (r"intense|gripping|\bdark\b|gritty|brutal", ["Thriller", "Crime", "Drama", "Action"]),
    "date night": (r"date night|romantic evening", ["Romance", "Comedy"]),
    "epic": (r"\bepic\b|big screen|blockbuster|spectacle", ["Adventure", "Action", "Science Fiction", "Fantasy"]),
    "festive": (r"christmas|xmas|holiday|festive", ["Family", "Comedy", "Animation"]),
    "spooky": (r"halloween|spooky season", ["Horror", "Thriller", "Mystery"]),
}
# The TV names TMDB/Jellyfin use for shows, so "sci-fi" also finds a series tagged "Sci-Fi & Fantasy".
TV_GENRES = {"Science Fiction": "Sci-Fi & Fantasy", "Fantasy": "Sci-Fi & Fantasy", "Action": "Action & Adventure",
             "Adventure": "Action & Adventure", "War": "War & Politics", "Family": "Kids"}
PROMPT_HINT = re.compile(r"\?|\b(something|anything|recommend|suggest|give me|i'?d like|i like|like \w+|similar|"
                         r"shows? (that|with|about)|movies? (that|with|about)|films? (that|with|about)|good|best|"
                         r"tonight|mood|feel|for (a|me)|about|watch next|surprise)\b", re.I)
TMDB_MOVIE = {"Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80, "Documentary": 99, "Drama": 18,
              "Family": 10751, "Fantasy": 14, "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749,
              "Science Fiction": 878, "Thriller": 53, "War": 10752, "Western": 37}
TMDB_TV = {"Action": 10759, "Adventure": 10759, "Animation": 16, "Comedy": 35, "Crime": 80, "Documentary": 99,
           "Drama": 18, "Family": 10751, "Fantasy": 10765, "Science Fiction": 10765, "Mystery": 9648, "War": 10768,
           "Western": 37}


def read_query(q):
    """Cheap, no-AI reading of a search: is it a title or a request in words, which genres, films or shows.
    Also: moods ("feel good", "mind-bending"), an era ("90s", "classic", "new"), a length ("short", "under 90
    minutes"), anime, and "haven't seen". The first keys (prompt, genres, kind) are what callers always used."""
    import datetime as dt
    low = q.lower()
    genres = [g for g, rx in GENRE_WORDS.items() if re.search(rx, low)]
    moods = [m for m, (rx, _) in MOODS.items() if re.search(rx, low)]
    for m in moods:
        genres += [g for g in MOODS[m][1] if g not in genres]
    kind = "movie" if re.search(r"\b(movies?|films?)\b", low) else "tv" if re.search(r"\b(shows?|series|anime)\b", low) else "any"
    years = None
    era = re.search(r"\b(?:the )?(19|20)?(\d)0'?s\b", low)
    if era:
        century = era.group(1) or ("20" if era.group(2) in "012" else "19")
        start = int(century + era.group(2) + "0")
        years = (start, start + 9)
    elif re.search(r"\b(classic|old(?:ies)?|old-school|vintage)\b", low):
        years = (1900, 1989)
    elif re.search(r"\b(new|newest|recent|latest|this year)\b", low):
        years = (dt.date.today().year - 2, dt.date.today().year + 1)
    minutes = None
    m = re.search(r"\b(?:under|less than|max(?:imum)?)\s*(\d{2,3})\s*(?:min|minutes|m)\b", low)
    if m:
        minutes = int(m.group(1))
    elif re.search(r"\b(?:an|one) hour\b", low):
        minutes = 65
    elif re.search(r"\b(short|quick|not too long|before bed)\b", low):
        minutes = 100
    anime = bool(re.search(r"\banime\b", low))
    unwatched = bool(re.search(r"haven'?t (?:seen|watched)|unwatched|not seen|new to me|never (?:seen|watched)", low))
    prompt = bool(PROMPT_HINT.search(q)) or bool(genres and kind != "any") or len(q.split()) >= 5 or \
        bool(moods or years or minutes or unwatched)
    tv = [TV_GENRES[g] for g in genres if g in TV_GENRES]
    return {"prompt": prompt, "genres": genres + [t for t in dict.fromkeys(tv) if t not in genres], "kind": kind,
            "moods": moods, "years": years, "max_minutes": minutes, "anime": anime, "unwatched": unwatched}


def search(app, q, allow_during_playback=False):
    """Ask the AI to pick and order titles from the library for a request in words; one short answer line.
    Classic mode: the same answer shape from rules and prepared lines (marquee.scripted.search)."""
    if mode() == "classic":
        from . import scripted
        return scripted.search(app, q)
    try:
        intent = read_query(q)
        jf = app.jf
        types = {"movie": "Movie", "tv": "Series"}.get(intent["kind"], "Movie,Series")
        pool = jf.items(Recursive="true", IncludeItemTypes=types, SortBy="Random", Limit=160,
                        **({"Genres": "|".join(intent["genres"])} if intent["genres"] else {}))
        if len(pool) < 20:
            pool += jf.items(Recursive="true", IncludeItemTypes=types, SortBy="Random", Limit=120)
        seen, recent = set(), []
        for i in jf.items(Recursive="true", Filters="IsPlayed", SortBy="DatePlayed", SortOrder="Descending",
                          IncludeItemTypes="Movie,Episode", Limit=10):
            name = i.get("SeriesName") or i.get("Name")
            if name not in seen:
                seen.add(name)
                recent.append(name)
        catalog, lines = {}, []
        for i in pool:
            if i["Name"] not in catalog:
                catalog[i["Name"]] = i
                lines.append(f"{i['Name']} ({i.get('ProductionYear', '')}; {'/'.join((i.get('Genres') or [])[:2])})")
        text = chat(
            f"Request: {q}\nRecently watched: {', '.join(recent) or 'nothing yet'}\n"
            f"Taste profile:\n{taste_profile.summary()}\nLibrary: " + "; ".join(lines),
            "You help pick what to watch from a home library. Reply with JSON only: {\"answer\": \"one friendly "
            "sentence, max 25 words\", \"titles\": [up to 12 exact titles from the library, best first], "
            "\"genres\": [TMDB genre names that fit the request], \"kind\": \"movie\" | \"tv\" | \"any\"}.",
            max_tokens=380, timeout=90, allow_during_playback=allow_during_playback)
        data = json.loads(re.search(r"\{.*\}", text or "", re.S).group(0))
        from .server import cards
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        by_norm = {norm(k): v for k, v in catalog.items()}
        wanted = [by_norm.get(norm(t)) for t in data.get("titles", [])]
        answer_low = norm(data.get("answer"))
        wanted += [v for k, v in by_norm.items() if len(k) > 3 and f" {k} " in f" {answer_low} "]  # titles it named
        picked, ids = [], set()
        for i in wanted:
            if i and i["Id"] not in ids:
                ids.add(i["Id"])
                picked.append(i)
        items = cards(picked)
        req = []
        if app.seerr.ready and data.get("genres"):
            try:
                req = app.seerr.by_genre([g for g in data["genres"] if g in TMDB_MOVIE or g in TMDB_TV],
                                         data.get("kind") or intent["kind"])
            except Exception:
                req = []
        return {"answer": (data.get("answer") or "").strip() or None, "items": items, "requestable": req}
    except Exception as e:
        print("ai search failed:", e)
        return {"answer": None, "items": [], "requestable": []}


def curate(app, pool):
    """From titles you could request (sequels, recommendations, trending), the AI picks up to 8 for this viewer
    with a short reason. One call; the caller caches it for hours. Classic mode: scored by rules instead."""
    if not pool:
        return []
    if mode() == "classic":
        from . import scripted
        return scripted.curate(app, pool)
    try:
        pool = taste_profile.rank(pool)
        seen, _, _ = _taste(app)
        by_name = {}
        for c in pool:
            by_name.setdefault(c["title"], c)
        text = chat(
            f"Taste profile:\n{taste_profile.summary()}\n"
            f"Recently watched: {', '.join(seen[:10]) or 'nothing yet'}.\nCould request: "
            + "; ".join(f"{t} [{c.get('year') or '?'} {'show' if c['type'] == 'tv' else 'film'}; from {c.get('because', 'trending')}]"
                         for t, c in list(by_name.items())[:40]),
            "Respect the taste profile: avoid what it says they avoid. "
            "Pick up to 8 titles from the 'could request' list this viewer would most enjoy next, best first. Each title "
            "says where it came from; base the reason on that (e.g. 'next in the Dune saga'), never invent links. Reply "
            'with JSON only: [{"title": "...", "why": "max 10 words"}]. Titles must match the list exactly.',
            max_tokens=400, timeout=150)
        out = []
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        lookup = {norm(k): v for k, v in by_name.items()}
        for d in json.loads(re.search(r"\[.*\]", text or "", re.S).group(0)):
            # small models sometimes copy the "(year)" or "[year film; from ...]" hint back with the title
            c = lookup.get(norm(re.sub(r"(\s*(\([^)]*\)|\[[^\]]*\]))+\s*$", "", d.get("title") or "")))
            if c and c not in out:
                out.append({**c, "why": d.get("why", "")})
        return out[:8]
    except Exception as e:
        print("ai curate failed:", e)
        return []


def picks(app):
    """A few titles from the library chosen for this viewer, with a one-line reason each (Classic: by rules)."""
    if mode() == "classic":
        from . import scripted
        return scripted.picks(app)
    try:
        seen, unwatched, _ = _taste(app)
        if not unwatched:
            return {"items": []}
        catalog = {i["Name"]: i for i in unwatched}
        unwatched = sorted(unwatched, key=lambda i: -taste_profile.score({**i, "type": i.get("Type"), "genres": i.get("Genres")})["score"])
        text = chat(
            f"Taste profile:\n{taste_profile.summary()}\n"
            f"Recently watched: {', '.join(seen[:10]) or 'nothing yet'}.\nUnwatched in their library: "
            + "; ".join(f"{i['Name']} [{', '.join((i.get('Genres') or [])[:2])}]" for i in unwatched),
            "Pick 6 titles from the unwatched list that this viewer would enjoy next. Reply with JSON only: "
            '[{"title": "...", "why": "max 12 words"}]. Titles must match the list exactly.', max_tokens=400)
        data = json.loads(re.search(r"\[.*\]", text or "", re.S).group(0))
        from .server import card
        out = []
        for d in data:
            i = catalog.get(d.get("title"))
            if i:
                out.append({**card(i), "why": d.get("why", "")})
        return {"items": out[:6]}
    except Exception as e:
        print("ai picks failed:", e)
        return {"items": []}
