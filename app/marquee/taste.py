"""Your taste profile: what Atlas has learned about what you like to watch, kept in plain JSON on this PC
(%APPDATA%\\Marquee\\taste.json) so it can be shown, edited and reset. Works with and without AI.

What goes in
    onboarding   the Launchpad's answers (genres, vibes, things you can't stand): a moderate starting weight
    watch history  from the local catalog: finishing a title (>85%) counts for it, giving up in the first 20%
                 counts mildly against it, rewatches and favourites count strongly for it
    chat         "remember I hate jump scares" becomes a memory note (with AI the model words it; Classic uses rules)
    letterboxd   the public RSS diary (letterboxd.com/<user>/rss/) or the official export ZIP: ratings, likes, reviews
    manual       notes typed in Settings > Taste & memory

How it changes
    Every signal nudges a weight with an exponential moving average: w <- w + rate * (target - w). Watching a genre
    pulls it toward +0.8; each finished title also lets the genres it *didn't* have fade a little (x0.97), so
    behaviour slowly outweighs what was said: someone who said "romance" but only finishes horror sees horror climb
    past romance within two to four weeks of watching. When that happens a "Why" note says so.

API for other modules (see tools/INTEGRATION.md)
    profile() -> dict            the whole profile (a copy)
    summary(max_chars=2200) -> str   compact text for prompts (about 600 tokens at most)
    score(card) -> {score 0-100, verdict, reasons[], different, top}
    rank(cards, key="fit") -> cards sorted best fit first (each gets card[key] = score)
    top_genres(n) / avoid_genres(n) -> [genre names]
    add_note(text, source, ...) / edit_note(id, ...) / delete_note(id)
    reset() (backs up first) / undo_reset()
    update_from_history() -> what changed (run after each finished play and daily; start_background() does it)
"""
import copy
import json
import math
import re
import threading
import time
import uuid

from . import settings

FILE = settings.DIR / "taste.json"
BACKUP = settings.DIR / "taste.backup.json"
VERSION = 1
_lock = threading.RLock()
_cache = {"data": None, "mtime": None}

# ------------------------------------------------------------------ vocabulary
# Canonical genre names (TMDB's film names). TV-only names map onto them.
GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy",
          "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction", "Thriller", "War", "Western",
          "Reality"]
GENRE_ALIASES = {
    "sci-fi & fantasy": ["Science Fiction", "Fantasy"], "action & adventure": ["Action", "Adventure"],
    "war & politics": ["War"], "kids": ["Family"], "soap": ["Drama"], "sci-fi": ["Science Fiction"],
    "science fiction": ["Science Fiction"], "musical": ["Music"], "music": ["Music"], "tv movie": [],
    "news": [], "talk": [], "reality": ["Reality"], "biography": ["History"], "sport": ["Drama"],
}
# Launchpad chips -> what they mean in the profile.
CHIPS = {
    "Anime": {"anime": 1, "genres": {"Animation": .3}},
    "Action": {"genres": {"Action": 1}}, "Comedy": {"genres": {"Comedy": 1}}, "Romance": {"genres": {"Romance": 1}},
    "Horror": {"genres": {"Horror": 1}}, "Sci-fi": {"genres": {"Science Fiction": 1}},
    "Fantasy": {"genres": {"Fantasy": 1}}, "Drama": {"genres": {"Drama": 1}}, "Thriller": {"genres": {"Thriller": 1}},
    "Mystery": {"genres": {"Mystery": 1}}, "Documentary": {"genres": {"Documentary": 1}},
    "Animation": {"genres": {"Animation": 1}}, "Family": {"genres": {"Family": 1}}, "Crime": {"genres": {"Crime": 1}},
    "Reality": {"genres": {"Reality": 1}}, "K-drama": {"languages": {"ko": 1}, "genres": {"Drama": .5}},
    "Musicals": {"genres": {"Music": 1}}, "War": {"genres": {"War": 1}}, "Westerns": {"genres": {"Western": 1}},
    "Superheroes": {"themes": {"Superheroes": 1}, "genres": {"Action": .4}},
    "True crime": {"themes": {"True crime": 1}, "genres": {"Crime": .5, "Documentary": .5}},
    "Adventure": {"genres": {"Adventure": 1}}, "History": {"genres": {"History": 1}},
}
VIBES = {  # moods -> genres they lean on (for scoring titles, which only carry genres)
    "Feel-good": ["Comedy", "Family", "Animation", "Romance"], "Mind-bending": ["Science Fiction", "Mystery", "Thriller"],
    "Dark": ["Thriller", "Crime", "Horror", "Drama"], "Cosy": ["Comedy", "Family", "Animation", "Romance"],
    "Epic": ["Adventure", "Fantasy", "Science Fiction", "Action", "War"], "Short episodes": [], "Binge-able": [],
    "Funny": ["Comedy"], "Slow-burn": ["Drama", "Mystery", "Thriller"], "Scary": ["Horror"],
    "Romantic": ["Romance"], "Thought-provoking": ["Drama", "Science Fiction", "Documentary"],
}
# Words in free text (onboarding, chat, notes, reviews) -> genres / vibes / themes. Classic mode relies on these.
WORDS = [
    (r"\banime\b|\bshonen\b|\bseinen\b|\bmanga\b", {"anime": 1}),
    (r"\bk-?dramas?\b|\bkorean\b", {"languages": {"ko": 1}, "genres": {"Drama": .5}}),
    (r"super-?hero|\bmarvel\b|\bdc\b|\bmcu\b", {"themes": {"Superheroes": 1}}),
    (r"true crime", {"themes": {"True crime": 1}, "genres": {"Crime": .5, "Documentary": .5}}),
    (r"jump ?scares?", {"themes": {"Jump scares": 1}}),
    (r"\bgore\b|\bgory\b|\bsplatter\b", {"themes": {"Gore": 1}}),
    (r"musicals?\b|sing-?along", {"genres": {"Music": 1}}),
    (r"funny|comed|sitcom|hilarious|rom-?coms?|laugh", {"genres": {"Comedy": 1}}),
    (r"horror|scary|slasher|haunt|creepy|spooky|zombie", {"genres": {"Horror": 1}}),
    (r"sci-?fi|science fiction|space opera|\bspace\b|alien|cyberpunk|dystop|time travel", {"genres": {"Science Fiction": 1}}),
    (r"romanc|romantic|love stor|rom-?coms?", {"genres": {"Romance": 1}}),
    (r"\baction\b|martial arts?|explosions?|car chases?", {"genres": {"Action": 1}}),
    (r"thriller|suspense|tense", {"genres": {"Thriller": 1}}),
    (r"myster|whodunn?it|detective", {"genres": {"Mystery": 1}}),
    (r"\bcrime\b|heist|gangster|mafia|\bmob\b", {"genres": {"Crime": 1}}),
    (r"cartoon|animated|animation|pixar|ghibli", {"genres": {"Animation": 1}}),
    (r"documentar|\bdocos?\b|\bdocus?\b", {"genres": {"Documentary": 1}}),
    (r"\bwar\b|military|soldier", {"genres": {"War": 1}}),
    (r"western|cowboy", {"genres": {"Western": 1}}),
    (r"fantasy|magic|dragons?|wizard|witch|fairy ?tale", {"genres": {"Fantasy": 1}}),
    (r"\bdramas?\b|tearjerk", {"genres": {"Drama": 1}}),
    (r"adventure|quest|treasure", {"genres": {"Adventure": 1}}),
    (r"histor|period piece|biopic", {"genres": {"History": 1}}),
    (r"family|\bkids?\b|children", {"genres": {"Family": 1}}),
    (r"reality (?:tv|shows?)|\breality\b", {"genres": {"Reality": 1}}),
    (r"feel[- ]?good|uplifting|wholesome|heartwarming", {"moods": {"Feel-good": 1}}),
    (r"mind[- ]?(?:bend|blow)|twisty|twists?\b|trippy|cerebral", {"moods": {"Mind-bending": 1}}),
    (r"\bdark\b|gritty|bleak|brutal", {"moods": {"Dark": 1}}),
    (r"\bcos[yz]\b|comfort", {"moods": {"Cosy": 1}}),
    (r"\bepic\b|blockbuster|spectacle", {"moods": {"Epic": 1}}),
    (r"slow[- ]?burn|slow[- ]paced|atmospheric", {"moods": {"Slow-burn": 1}}),
    (r"short episodes?|half[- ]hour|bite-?sized", {"moods": {"Short episodes": 1}}),
    (r"binge", {"moods": {"Binge-able": 1}}),
    (r"thought[- ]provoking|makes? (?:me|you) think", {"moods": {"Thought-provoking": 1}}),
]
NEG = re.compile(r"\b(hate|hates|hated|can'?t stand|cannot stand|(?:don'?t|doesn'?t|do not|does not) like|dislikes?|disliked|not (?:a )?fan of|"
                 r"not into|no more|never (?:show|recommend)|avoid|sick of|tired of|bored? (?:of|by)|boring|"
                 r"not keen on|(?:isn'?t|aren'?t|not) for (?:me|them)|avoids|scares? me too much|too scary)\b", re.I)
POS = re.compile(r"\b(love|loves|loved|like|likes|liked|enjoy|enjoys|enjoyed|adore|into|fan of|favou?rite|"
                 r"obsessed|can'?t get enough|more of|really dig|big on)\b", re.I)

# ------------------------------------------------------------------ learning rates (see the module docstring)
RATE_STATED = .5        # onboarding / chat statements start at +/-0.5 ("moderate")
TARGET_FINISH = .8      # where watched genres head
RATE_FINISH = .12
RATE_STRONG = .22       # rewatch or favourite
TARGET_STRONG = 1.0
TARGET_ABANDON = -.5
RATE_ABANDON = .08
FADE_OTHERS = .97       # genres a finished title didn't have fade by this factor
EPISODE_SCALE = .35     # one episode is a smaller signal than a film
ABANDON_AFTER = 2 * 86400  # stopped in the first 20% and not back for two days = gave up


def _now():
    return time.time()


def _empty():
    return {"version": VERSION, "rev": 0, "created": _now(), "updated": _now(), "onboarded": False,
            "genres": {}, "moods": {}, "themes": {}, "languages": {}, "anime": 0.0,
            "kinds": {"movie": 0.0, "tv": 0.0}, "eras": {}, "runtime": {"short": 0.0, "long": 0.0},
            "people": {}, "titles": {"liked": [], "disliked": []}, "notes": [], "why": [],
            "history": {"seen": {}, "plays": 0, "last_run": 0}, "letterboxd": {}}


# ------------------------------------------------------------------ storage
def exists():
    return FILE.is_file()


def _load():
    with _lock:
        try:
            mtime = FILE.stat().st_mtime
        except OSError:
            _cache.update(data=None, mtime=None)
            return _empty()
        if _cache["data"] is not None and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            data = _empty()
        base = _empty()
        base.update(data)
        _cache.update(data=base, mtime=mtime)
        return base


def _save(p, bump=True):
    with _lock:
        if bump:
            p["rev"] = int(p.get("rev") or 0) + 1
        p["updated"] = _now()
        settings.DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)
        _cache.update(data=p, mtime=FILE.stat().st_mtime)
        return p


def _edit(fn, bump=True):
    """Load, change, save under the lock. fn(profile) may return a value."""
    with _lock:
        p = copy.deepcopy(_load())
        out = fn(p)
        _save(p, bump)
        return out


def profile():
    return copy.deepcopy(_load())


def rev():
    return int(_load().get("rev") or 0)


# ------------------------------------------------------------------ small helpers
def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def _gentry(p, g):
    return p["genres"].setdefault(g, {"w": 0.0, "stated": 0.0, "plays": 0, "since": _now()})


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def canon_genres(names):
    """Library / TMDB genre names -> canonical ones (TV names split: "Sci-Fi & Fantasy" -> Science Fiction, Fantasy)."""
    out = []
    for n in names or []:
        if not n:
            continue
        low = n.strip().lower()
        hits = GENRE_ALIASES.get(low)
        if hits is None:
            hits = [g for g in GENRES if g.lower() == low]
        for g in hits:
            if g not in out:
                out.append(g)
    return out


def parse_text(text):
    """Rules: free text -> {"genres": {g: polarity}, "moods":…, "themes":…, "languages":…, "anime": +-1}.
    Polarity comes from the nearest like/hate words before each match, clause by clause ("I love horror but hate
    gore" -> Horror +1, Gore -1)."""
    found = {"genres": {}, "moods": {}, "themes": {}, "languages": {}, "anime": 0}
    pol = 1
    for clause in re.split(r"[.;!?\n]|,|\band\b|\bor\b|\bbut\b|\bexcept\b|\bthough\b|\balthough\b|\bwhile\b",
                           text or "", flags=re.I):
        if not clause.strip():
            continue
        # a clause with its own like/hate word sets the polarity; "horror, sci-fi and fantasy" inherit it
        if NEG.search(clause):
            pol = -1
        elif POS.search(clause) or re.search(r"\b(great|good|best|amazing|fun|awesome)\b", clause, re.I):
            pol = 1
        for rx, what in WORDS:
            if re.search(rx, clause, re.I):
                for k, v in what.items():
                    if k == "anime":
                        found["anime"] = pol
                    else:
                        for name, w in v.items():
                            if abs(pol * w) >= abs(found[k].get(name, 0)):
                                found[k][name] = pol * w
    return found


def _apply_parsed(p, parsed, rate=RATE_STATED, stated=True):
    """Fold parsed preferences in (stated preferences move the weight halfway toward +/-1 at most)."""
    for g, v in parsed.get("genres", {}).items():
        e = _gentry(p, g)
        target = _clamp(v)
        e["w"] = round(_clamp(e["w"] + rate * (target - e["w"]) if abs(v) < 1 else
                              (max(e["w"], rate * target) if target > 0 else min(e["w"], rate * target))), 4)
        if stated:
            e["stated"] = round(_clamp(target * rate), 3)
    for k in ("moods", "themes", "languages"):
        for name, v in parsed.get(k, {}).items():
            old = float(p[k].get(name, 0))
            p[k][name] = round(max(old, rate * v) if v > 0 else min(old, rate * v), 4)
    if parsed.get("anime"):
        p["anime"] = round(max(p["anime"], rate) if parsed["anime"] > 0 else min(p["anime"], -rate), 4)


# ------------------------------------------------------------------ memory notes
def add_note(text, source="manual", confidence=None, tags=None, kind=None, rate=None, title=None):
    """A plain-language memory ("Doesn't like musicals"). Also nudges the weights its words point at.
    Returns the note. Duplicate wording just refreshes the old note."""
    text = re.sub(r"\s+", " ", (text or "")).strip().strip('"').strip()
    if not text:
        raise ValueError("A note needs some words")
    text = text[:240]
    text = text[0].upper() + text[1:]
    parsed = tags or parse_text(text)
    if kind is None:
        kind = "dislike" if NEG.search(text) else "like" if POS.search(text) else "info"
    conf = confidence if confidence is not None else {"manual": .9, "chat": .8, "onboarding": .7,
                                                        "letterboxd": .6, "watch history": .5}.get(source, .6)

    def go(p):
        for n in p["notes"]:
            if norm(n["text"]) == norm(text):
                n["date"] = _now()
                n["confidence"] = max(n.get("confidence", 0), conf)
                return n
        note = {"id": uuid.uuid4().hex[:10], "text": text, "source": source, "date": _now(),
                "confidence": round(conf, 2), "kind": kind, "tags": parsed}
        if title:
            note["title"] = title
        p["notes"].insert(0, note)
        _apply_parsed(p, parsed, rate if rate is not None else RATE_STATED * conf / .8)
        return note
    return _edit(go)


def edit_note(note_id, text=None, confidence=None):
    def go(p):
        for n in p["notes"]:
            if n["id"] == note_id:
                if text is not None and text.strip():
                    n["text"] = re.sub(r"\s+", " ", text).strip()[:240]
                    n["tags"] = parse_text(n["text"])
                    n["kind"] = "dislike" if NEG.search(n["text"]) else "like" if POS.search(n["text"]) else "info"
                    n["source"] = n["source"] if n["source"] == "manual" else n["source"] + ", edited"
                if confidence is not None:
                    n["confidence"] = round(_clamp(float(confidence), 0, 1), 2)
                n["date"] = _now()
                return n
        raise KeyError("No such note")
    return _edit(go)


def delete_note(note_id):
    """Removes the note (its earlier nudge to the weights stays, and fades with watching like everything else)."""
    def go(p):
        before = len(p["notes"])
        gone = [n for n in p["notes"] if n["id"] == note_id]
        p["notes"] = [n for n in p["notes"] if n["id"] != note_id]
        # undo the stated part of the nudge so deleting "I hate horror" really lets horror back in
        for n in gone:
            for g, v in (n.get("tags") or {}).get("genres", {}).items():
                e = p["genres"].get(g)
                if e:
                    e["w"] = round(_clamp(e["w"] - RATE_STATED * v * .8), 4)
                    if e.get("stated") and (e["stated"] > 0) == (v > 0):
                        e["stated"] = 0.0
            for k in ("moods", "themes", "languages"):
                for name, v in (n.get("tags") or {}).get(k, {}).items():
                    if name in p[k]:
                        p[k][name] = round(_clamp(p[k][name] - RATE_STATED * v * .8), 4)
        return before != len(p["notes"])
    return _edit(go)


def restore_note(note):
    """Undo for a deleted (or just-saved-then-undone) note: put it back as it was."""
    def go(p):
        if not any(n["id"] == note.get("id") for n in p["notes"]):
            p["notes"].insert(0, note)
            _apply_parsed(p, note.get("tags") or parse_text(note["text"]))
        return note
    return _edit(go)


def extract_memory(text):
    """Rules (Classic mode, and a fallback with AI): does this chat message state a lasting preference?
    Returns a note sentence or None. "remember I hate jump scares" -> "Hates jump scares"."""
    t = (text or "").strip()
    m = re.match(r"^\s*(?:please\s+)?(?:remember|note|keep in mind|don'?t forget|fyi)\b[\s,:]*(?:that\s+)?(.+)$", t, re.I)
    body = m.group(1) if m else None
    if not body:
        m2 = re.match(r"^\s*i\s+(?:really\s+|absolutely\s+|kind of\s+|kinda\s+)?(love|like|hate|adore|enjoy|can'?t stand|"
                      r"don'?t like|do not like|dislike|am not into|'m not into|am into|'m into|prefer)\b\s+(.+)$", t, re.I)
        if not m2:
            return None
        if re.search(r"\?\s*$|\b(recommend|suggest|something|anything|give me|show me|find me|tonight)\b", t, re.I):
            return None  # "I like horror, recommend something" is a request, not a memory
        body = "I " + m2.group(1) + " " + m2.group(2)
    body = body.strip().rstrip(".!")
    # first person -> a note about the viewer
    rep = [(r"^i\s+(?:really\s+)?(?:absolutely\s+)?hate\b", "Hates"), (r"^i\s+(?:can'?t|cannot) stand\b", "Can't stand"),
           (r"^i\s+(?:don'?t|do not) like\b", "Doesn't like"), (r"^i\s+dislike\b", "Dislikes"),
           (r"^i\s+(?:really\s+)?love\b", "Loves"), (r"^i\s+(?:really\s+)?like\b", "Likes"), (r"^i\s+adore\b", "Adores"),
           (r"^i\s+enjoy\b", "Enjoys"), (r"^i\s+prefer\b", "Prefers"), (r"^i(?:'m| am) not into\b", "Isn't into"),
           (r"^i(?:'m| am) into\b", "Is into"), (r"^i(?:'m| am)\b", "Is"), (r"^my\b", "Their"), (r"^i\b", "")]
    for rx, sub in rep:
        if re.match(rx, body, re.I):
            body = re.sub(rx, sub, body, count=1, flags=re.I).strip()
            break
    body = re.sub(r"\bme\b", "them", body)
    body = re.sub(r"\bmy\b", "their", body)
    return body[:1].upper() + body[1:] if len(body) > 2 else None


# ------------------------------------------------------------------ onboarding
def onboard(likes=(), vibes=(), dislikes=(), texts=(), extra=None):
    """The Launchpad's answers -> a fresh starting profile (keeps notes/history already there)."""
    def go(p):
        for chip in likes:
            spec = CHIPS.get(chip)
            if spec:
                _apply_parsed(p, {**{"genres": {}, "moods": {}, "themes": {}, "languages": {}, "anime": 0}, **spec})
        for chip in dislikes:
            spec = CHIPS.get(chip)
            if spec:
                neg = {k: ({n: -v for n, v in val.items()} if isinstance(val, dict) else -val) for k, val in spec.items()}
                _apply_parsed(p, {**{"genres": {}, "moods": {}, "themes": {}, "languages": {}, "anime": 0}, **neg})
        for v in vibes:
            p["moods"][v] = round(max(float(p["moods"].get(v, 0)), RATE_STATED), 4)
        notes = []
        if likes:
            notes.append(("Said they love " + _list([c.lower() if c not in ("K-drama",) else c for c in likes]), "like"))
        if vibes:
            notes.append(("In the mood for " + _list([v.lower() for v in vibes]) + " picks", "like"))
        if dislikes:
            notes.append(("Can't stand " + _list([c.lower() if c not in ("K-drama",) else c for c in dislikes]), "dislike"))
        for t in texts or []:
            t = (t or "").strip()
            if t:
                parsed = parse_text(t)
                _apply_parsed(p, parsed)
                notes.append(((extract_memory(t) or ("Said: “" + t[:160] + "”")), "dislike" if NEG.search(t) else "like" if POS.search(t) else "info"))
        for text, kind in (extra or {}).get("notes", []):
            notes.append((text, kind))
        for text, kind in notes:
            if not any(norm(n["text"]) == norm(text) for n in p["notes"]):
                p["notes"].insert(0, {"id": uuid.uuid4().hex[:10], "text": text[:240], "source": "onboarding",
                                      "date": _now(), "confidence": .7, "kind": kind, "tags": parse_text(text)})
        for t in texts or []:
            for r in _titles_named(t):
                remember_title(p, r.get("name"), r.get("year"), "tv" if r["type"] == "Series" else "movie",
                               (r.get("provider") or {}).get("Tmdb"), True, "onboarding", item_id=r["id"], strength=1.0)
        p["onboarded"] = True
        p["onboard"] = {"likes": list(likes), "vibes": list(vibes), "dislikes": list(dislikes), "at": _now()}
        return p
    out = _edit(go)
    try:
        update_from_history()  # what's already been watched counts from the start
    except Exception as e:
        print("taste: history pass failed:", e)
    return out


def _titles_named(text):
    """Library films/shows named in free text ("I really love Dune") -> catalog rows (whole words, 4+ letters)."""
    cat = _catalog()
    if cat is None or not text:
        return []
    hay = " " + norm(text) + " "
    out = []
    with cat.lock:
        for r in cat._of("Movie,Series"):
            n = norm(r.get("name"))
            if len(n) >= 4 and f" {n} " in hay and not any(norm(o.get("name")) == n for o in out):
                out.append(r)
    return out[:8]


def _list(xs):
    xs = [x for x in xs if x]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


# ------------------------------------------------------------------ reset (with a backup so it can be undone)
def reset():
    with _lock:
        if FILE.is_file():
            BACKUP.write_text(FILE.read_text(encoding="utf-8"), encoding="utf-8")
        p = _empty()
        p["rev"] = rev() + 1
        p["onboarded"] = False
        _save(p, bump=False)
        return True


def undo_reset():
    with _lock:
        if not BACKUP.is_file():
            return False
        data = json.loads(BACKUP.read_text(encoding="utf-8"))
        data["rev"] = rev() + 1
        _save(data, bump=False)
        return True


# ------------------------------------------------------------------ titles (liked / disliked)
def _title_key(t):
    return (t.get("kind") or "", str(t.get("tmdb") or ""), norm(t.get("title")), str(t.get("year") or ""))


def remember_title(p, title, year=None, kind="movie", tmdb=None, liked=True, source="watch history", rating=None,
                   item_id=None, strength=None):
    """Add/refresh a liked or disliked title in p (call inside _edit)."""
    lst, other = (p["titles"]["liked"], p["titles"]["disliked"]) if liked else (p["titles"]["disliked"], p["titles"]["liked"])
    match = lambda t: (tmdb and str(t.get("tmdb") or "") == str(tmdb) and t.get("kind") == kind) or \
        (norm(t.get("title")) == norm(title) and (not year or not t.get("year") or str(t.get("year")) == str(year)))
    other[:] = [t for t in other if not match(t)]
    for t in lst:
        if match(t):
            t.update({k: v for k, v in (("rating", rating), ("tmdb", tmdb), ("id", item_id), ("year", year)) if v})
            t["date"] = _now()
            if source not in (t.get("source") or ""):
                t["source"] = (t.get("source") + ", " if t.get("source") else "") + source
            if strength:
                t["strength"] = max(t.get("strength", 0), strength)
            return t
    t = {"title": title, "year": int(year) if str(year or "").isdigit() else None, "kind": kind, "tmdb": tmdb,
         "id": item_id, "source": source, "date": _now()}
    if rating is not None:
        t["rating"] = rating
    if strength:
        t["strength"] = strength
    lst.insert(0, t)
    del lst[200:]
    return t


def _people_nudge(p, people, target, rate):
    for person in people[:5]:
        name = person.get("name")
        if not name:
            continue
        e = p["people"].setdefault(name, {"w": 0.0, "role": person.get("role") or person.get("type") or "", "n": 0,
                                          "titles": []})
        e["w"] = round(_clamp(e["w"] + rate * (target - e["w"])), 4)
        if target > 0:
            e["n"] = e.get("n", 0) + 1
        if person.get("title") and person["title"] not in e.setdefault("titles", []):
            e["titles"] = (e["titles"] + [person["title"]])[-6:]
    if len(p["people"]) > 300:  # keep the strongest
        keep = sorted(p["people"].items(), key=lambda kv: -abs(kv[1]["w"]) - kv[1].get("n", 0) * .1)[:300]
        p["people"] = dict(keep)


def _decade(year):
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    return f"{y // 10 * 10}s" if 1900 < y < 2100 else None


def apply_signal(p, info, target, rate, source="watch history", fade=True, note_title=True):
    """One watch/rating signal about a title. info: {title, year, kind ('movie'|'tv'), genres[], people[{name, role}],
    runtime (seconds), anime, tmdb, id}. Returns {genre: (before, after)} for the genres it moved."""
    moved = {}
    genres = canon_genres(info.get("genres"))
    for g in genres:
        e = _gentry(p, g)
        before = e["w"]
        e["w"] = round(_clamp(e["w"] + rate * (target - e["w"])), 4)
        if target > 0:
            e["plays"] = e.get("plays", 0) + 1
        moved[g] = (before, e["w"])
    if fade and target > 0 and genres:
        f = 1 - (1 - FADE_OTHERS) * (rate / RATE_FINISH)
        for g, e in p["genres"].items():
            if g not in genres and e["w"] > 0:
                before = e["w"]
                e["w"] = round(e["w"] * f, 4)
                moved.setdefault(g, (before, e["w"]))
    # vibes lean on genres, so they follow the same evidence (gently)
    for mood, gs in VIBES.items():
        if mood in p["moods"] and gs:
            share = len(set(gs) & set(genres)) / len(gs)
            if share:
                p["moods"][mood] = round(_clamp(p["moods"][mood] + rate * .5 * share * (target - p["moods"][mood])), 4)
    kind = info.get("kind") or "movie"
    p["kinds"][kind] = round(_clamp(p["kinds"].get(kind, 0) + rate * .5 * (target - p["kinds"].get(kind, 0))), 4)
    if info.get("anime"):
        p["anime"] = round(_clamp(p["anime"] + rate * (target - p["anime"])), 4)
    d = _decade(info.get("year"))
    if d:
        p["eras"][d] = round(_clamp(float(p["eras"].get(d, 0)) + rate * .6 * (target - float(p["eras"].get(d, 0)))), 4)
    rt = (info.get("runtime") or 0) / 60
    if kind == "movie" and rt:
        bucket = "short" if rt < 100 else "long" if rt > 150 else None
        if bucket:
            p["runtime"][bucket] = round(_clamp(p["runtime"][bucket] + rate * .6 * (target - p["runtime"][bucket])), 4)
    people = [{**x, "title": info.get("title")} for x in (info.get("people") or [])]
    if people:
        _people_nudge(p, people, target, rate * .8)
    if note_title and info.get("title"):
        if target >= .8:
            remember_title(p, info["title"], info.get("year"), kind, info.get("tmdb"), True, source,
                           info.get("rating"), info.get("id"), strength=round(target * rate * 10, 2))
        elif target < 0:
            remember_title(p, info["title"], info.get("year"), kind, info.get("tmdb"), False, source,
                           info.get("rating"), info.get("id"))
    return moved


# ------------------------------------------------------------------ watch history (from the local catalog)
def _catalog():
    try:
        from . import server
        if server.APP is None:
            return None
        return server.catalog()
    except Exception:
        return None


def _info_from_row(cat, r):
    """A catalog row (film or show) -> what apply_signal() wants."""
    extra = r.get("extra") or {}
    people = [{"name": x.get("name"), "role": x.get("type")} for x in extra.get("people") or []
              if x.get("type") == "Director"][:2]
    people += [{"name": x.get("name"), "role": "Actor"} for x in extra.get("people") or [] if x.get("type") == "Actor"][:3]
    path = (r.get("path") or "").lower()
    return {"title": r.get("name"), "year": r.get("year"), "kind": "tv" if r["type"] == "Series" else "movie",
            "genres": r.get("genres") or [], "people": people, "runtime": r.get("runtime") or 0,
            "anime": r.get("library") == "anime" or "\\anime" in path or "/anime" in path,
            "tmdb": (r.get("provider") or {}).get("Tmdb"), "id": r["id"]}


def learning_on():
    return settings.load().get("taste_learning", True) is not False


def update_from_history(cat=None, now=None):
    """Read the catalog's watch history and fold in anything new since last time. Cheap; safe to call often.
    Returns {"plays": n, "abandoned": n, "moved": {genre: [before, after]}, "why": [new why notes]}."""
    if not learning_on():
        return {"paused": True}
    cat = cat or _catalog()
    if cat is None:
        return {"plays": 0}
    now = now or _now()
    events = []
    with cat.lock:
        for rid, u in list(cat.ud.items()):
            r = cat.items.get(rid)
            if not r or r["type"] not in ("Movie", "Episode"):
                continue
            last = u.get("last_played") or 0
            runtime = r.get("runtime") or 0
            frac = 1.0 if u.get("played") else ((u.get("position") or 0) / runtime if runtime else 0)
            if r["type"] == "Episode":
                show = cat.items.get(r.get("series_id"))
                if not show:
                    continue
                info = _info_from_row(cat, show)
                info["episode"] = rid
            else:
                info = _info_from_row(cat, r)
            events.append((rid, last, frac, bool(u.get("played")), int(u.get("play_count") or 0),
                           bool(u.get("favorite")) or bool((cat.ud.get(r.get("series_id")) or {}).get("favorite")),
                           r["type"], info))
    # Titles deleted to save space keep counting: their watch history lives on in the catalog's removed records.
    have = {e[0] for e in events}
    try:
        removed = cat.removed_history() if hasattr(cat, "removed_history") else []
    except Exception as e:
        print("taste: removed history unavailable:", e)
        removed = []
    for h in removed:
        rid = h.get("id")
        if not rid or rid in have or not h.get("played"):
            continue
        ep = h.get("type") == "Episode"
        info = {"title": (h.get("series") if ep else h.get("name")) or h.get("name"), "year": None,
                "kind": "tv" if ep else "movie", "genres": h.get("genres") or [], "people": [],
                "runtime": h.get("runtime") or 0, "anime": False, "tmdb": h.get("tmdb"),
                "id": f"removed-tv:{h.get('tmdb')}" if ep else rid}
        if ep:
            info["episode"] = rid
        events.append((rid, h.get("last_played") or h.get("removed_at") or 0, 1.0, True, int(h.get("play_count") or 1),
                       bool(h.get("favorite")), "Episode" if ep else "Movie", info))

    def go(p):
        seen = p["history"].setdefault("seen", {})
        moved, plays, gave_up = {}, 0, 0
        before = {g: e["w"] for g, e in p["genres"].items()}
        for rid, last, frac, played, count, fav, typ, info in sorted(events, key=lambda e: e[1]):
            prev = seen.get(rid) or {}
            scale = EPISODE_SCALE if typ == "Episode" else 1.0
            if played and frac >= .85 and last and last > (prev.get("at") or 0) + 1:
                strong = count > 1 or fav or prev.get("done")  # rewatch / favourite
                target, rate = (TARGET_STRONG, RATE_STRONG) if strong else (TARGET_FINISH, RATE_FINISH)
                apply_signal(p, info, target, rate * scale, note_title=typ != "Episode" or count > 0)
                seen[rid] = {"at": last, "done": True}
                plays += 1
            elif not played and 0.005 < frac < .2 and last and now - last > ABANDON_AFTER and not prev.get("gave_up"):
                apply_signal(p, info, TARGET_ABANDON, RATE_ABANDON * scale, fade=False, note_title=typ != "Episode")
                seen[rid] = {"at": last, "gave_up": True}
                gave_up += 1
        for g, e in p["genres"].items():
            if abs(e["w"] - before.get(g, 0)) > .0005:
                moved[g] = [round(before.get(g, 0), 3), round(e["w"], 3)]
        p["history"]["plays"] = p["history"].get("plays", 0) + plays
        p["history"]["last_run"] = now
        why = _why_notes(p) if plays or gave_up else []
        if plays or gave_up:
            p["rev"] = int(p.get("rev") or 0) + 1
        return {"plays": plays, "abandoned": gave_up, "moved": moved, "why": why}
    with _lock:
        res = _edit(go, bump=False)
    return res


# ------------------------------------------------------------------ titles marked watched by hand (marquee.watched)
def note_seen(ids, at=None):
    """These plays are already counted (a title marked watched, then downloaded): the history job skips them."""
    ids = [i for i in ids or [] if i]
    if not ids:
        return

    def go(p):
        seen = p["history"].setdefault("seen", {})
        for i in ids:
            if not (seen.get(i) or {}).get("done"):
                seen[i] = {"at": at or _now(), "done": True}
    with _lock:
        _edit(go, bump=False)


def watched_signal(info, seen_ids, at=None, source="marked watched"):
    """A title marked watched (from the title page, the chat or the local API): the same signal as finishing it,
    once (seen_ids already counted are skipped), plus the remembered title. Returns {genre: [before, after]}."""
    seen_ids = [i for i in seen_ids or [] if i]
    out = {}

    def go(p):
        seen = p["history"].setdefault("seen", {})
        fresh = not seen_ids or any(not (seen.get(i) or {}).get("done") for i in seen_ids)
        if fresh:
            moved = apply_signal(p, info, TARGET_FINISH, RATE_FINISH, source=source, note_title=False)
            out.update({g: [round(a, 3), round(b, 3)] for g, (a, b) in moved.items() if abs(a - b) > .0005})
            for i in seen_ids:
                seen[i] = {"at": at or _now(), "done": True}
            p["history"]["plays"] = p["history"].get("plays", 0) + 1
        if info.get("title"):
            remember_title(p, info["title"], info.get("year"), info.get("kind") or "movie", info.get("tmdb"), True,
                           source, info.get("rating"), info.get("id"), strength=round(TARGET_FINISH * RATE_FINISH * 10, 2))
    with _lock:
        _edit(go)
    return out


def forget_title(title=None, year=None, kind=None, tmdb=None):
    """Unmarked: drop the remembered (liked) title; what it taught the genres and people stays."""
    def go(p):
        n = 0
        for lst in (p["titles"]["liked"],):
            keep = []
            for t in lst:
                same = (tmdb and str(t.get("tmdb") or "") == str(tmdb) and (not kind or t.get("kind") == kind)) or \
                    (not tmdb and title and norm(t.get("title")) == norm(title) and
                     (not year or not t.get("year") or str(t.get("year")) == str(year)))
                if same:
                    n += 1
                else:
                    keep.append(t)
            lst[:] = keep
        return n
    with _lock:
        return _edit(go)


def _why_notes(p):
    """When behaviour overtakes what was said, explain it once (kept in p["why"], newest first)."""
    out = []
    stated = {g: e.get("stated", 0) for g, e in p["genres"].items() if e.get("stated")}
    ranked = sorted(p["genres"].items(), key=lambda kv: -kv[1]["w"])
    top = [g for g, e in ranked[:3] if e["w"] > .25]
    for g in top:
        e = p["genres"][g]
        if stated.get(g, 0) <= 0 and e.get("plays", 0) >= 3:
            for s, sv in stated.items():
                if sv > 0 and p["genres"][s]["w"] < e["w"]:
                    key = f"{g}>{s}"
                    if not any(w.get("key") == key for w in p["why"]):
                        text = (f"{g} has moved ahead of {s}: you said you liked {s.lower()}, but you've been "
                                f"finishing {g.lower()} ({e.get('plays', 0)} so far), so {g.lower()} now counts for "
                                f"more ({e['w']:+.2f} vs {p['genres'][s]['w']:+.2f}).")
                        w = {"key": key, "text": text, "date": _now()}
                        p["why"].insert(0, w)
                        out.append(w)
    for g, e in p["genres"].items():
        if e["w"] < -.2 and stated.get(g, 0) > 0:
            key = f"{g}<0"
            if not any(w.get("key") == key for w in p["why"]):
                w = {"key": key, "text": f"{g} has slipped below zero: you said you liked it, but you've been "
                                         f"giving up on {g.lower()} titles early.", "date": _now()}
                p["why"].insert(0, w)
                out.append(w)
    del p["why"][30:]
    return out


# ------------------------------------------------------------------ background job
_bg = {"started": False, "last_daily": 0}


def start_background(interval=300):
    """Every few minutes: pick up finished plays from the catalog (cheap). Once a day: the same plus housekeeping."""
    if _bg["started"]:
        return
    _bg["started"] = True

    def loop():
        time.sleep(45)
        while True:
            try:
                if exists():
                    res = update_from_history()
                    if res.get("plays") or res.get("abandoned"):
                        print("taste: learned from", res.get("plays"), "plays,", res.get("abandoned"), "abandoned")
            except Exception as e:
                print("taste update failed:", type(e).__name__, e)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="taste-learn").start()


# ------------------------------------------------------------------ reading the profile
def top_genres(n=3, min_w=.15):
    p = _load()
    ranked = sorted(p["genres"].items(), key=lambda kv: -kv[1]["w"])
    return [g for g, e in ranked if e["w"] >= min_w][:n]


def avoid_genres(n=3, max_w=-.15):
    p = _load()
    ranked = sorted(p["genres"].items(), key=lambda kv: kv[1]["w"])
    return [g for g, e in ranked if e["w"] <= max_w][:n]


def _strong(d, lo=.15):
    return sorted([(k, v) for k, v in d.items() if abs(float(v)) >= lo], key=lambda kv: -abs(float(kv[1])))


def favourite_people(n=8):
    p = _load()
    ranked = sorted(p["people"].items(), key=lambda kv: -(kv[1]["w"] + .05 * kv[1].get("n", 0)))
    return [{"name": k, **v} for k, v in ranked if v["w"] > .2 and v.get("n", 0) >= 2][:n]


def is_empty(p=None):
    p = p or _load()
    return not (p["genres"] or p["notes"] or p["titles"]["liked"] or p["moods"])


def summary(max_chars=2200):
    """Compact plain text for prompts (about 600 tokens at most). Empty string when nothing is known yet."""
    p = _load()
    if is_empty(p):
        return ""
    lines = []
    likes = [(g, e["w"]) for g, e in sorted(p["genres"].items(), key=lambda kv: -kv[1]["w"]) if e["w"] >= .15][:7]
    dis = [(g, e["w"]) for g, e in sorted(p["genres"].items(), key=lambda kv: kv[1]["w"]) if e["w"] <= -.15][:5]
    if likes:
        lines.append("Likes genres: " + ", ".join(f"{g} ({w:+.1f})" for g, w in likes))
    if dis:
        lines.append("Avoids genres: " + ", ".join(f"{g} ({w:+.1f})" for g, w in dis))
    moods = _strong(p["moods"])
    if moods:
        lines.append("Vibes: " + ", ".join(f"{'likes' if v > 0 else 'avoids'} {k.lower()}" for k, v in moods[:6]))
    themes = _strong(p["themes"])
    if themes:
        lines.append("Themes: " + ", ".join(f"{'likes' if v > 0 else 'avoids'} {k.lower()}" for k, v in themes[:5]))
    prefs = []
    if abs(p["anime"]) >= .15:
        prefs.append("enjoys anime" if p["anime"] > 0 else "not into anime")
    k = p["kinds"]
    if abs(k.get("movie", 0) - k.get("tv", 0)) > .2:
        prefs.append("mostly films" if k["movie"] > k["tv"] else "mostly series")
    eras = [e for e, v in sorted(p["eras"].items(), key=lambda kv: -kv[1]) if v > .2][:3]
    if eras:
        prefs.append("often watches " + "/".join(eras) + " titles")
    if p["runtime"].get("short", 0) > .25:
        prefs.append("likes shorter films")
    if p["runtime"].get("long", 0) > .25:
        prefs.append("happy with long films")
    langs = [l for l, v in p["languages"].items() if v > .2]
    if langs:
        prefs.append("likes titles in " + ", ".join(langs))
    if prefs:
        lines.append("Habits: " + "; ".join(prefs))
    people = favourite_people(6)
    if people:
        lines.append("Favourite people: " + ", ".join(f"{x['name']} ({(x.get('role') or '').lower() or 'cast'})" for x in people))
    liked = sorted(p["titles"]["liked"], key=lambda t: -(t.get("strength") or 0) - (t.get("rating") or 0))[:10]
    if liked:
        lines.append("Loved: " + ", ".join(f"{t['title']}" + (f" ({t['year']})" if t.get("year") else "") for t in liked))
    if p["titles"]["disliked"]:
        lines.append("Didn't like: " + ", ".join(f"{t['title']}" + (f" ({t['year']})" if t.get("year") else "")
                                                for t in p["titles"]["disliked"][:6]))
    notes = sorted(p["notes"], key=lambda n: (-n.get("confidence", 0), -n.get("date", 0)))[:10]
    if notes:
        lines.append("Notes: " + " | ".join(n["text"] for n in notes))
    out = "\n".join(lines)
    return out[:max_chars]


# ------------------------------------------------------------------ fit score
def _item_facts(card):
    """Normalise a library card / catalog row / Seerr card / TMDB detail into what scoring needs."""
    c = card or {}
    kind = c.get("kind") or ("tv" if c.get("type") in ("Series", "tv", "Episode", "Season") else "movie")
    people = []
    for x in c.get("people") or []:
        if x.get("type") in ("Director", "Actor") or x.get("role") in ("Director", "Actor"):
            people.append({"name": x.get("name"), "role": "Director" if (x.get("type") or x.get("role")) == "Director" else "Actor"})
    for x in c.get("crew") or []:
        people.append({"name": x.get("name"), "role": "Director" if x.get("job") in ("Director", "Creator") else x.get("job")})
    for x in (c.get("cast") or [])[:8]:
        people.append({"name": x.get("name"), "role": "Actor"})
    return {"title": c.get("title") or (c.get("series") if c.get("type") == "Episode" else None) or c.get("name"),
            "year": c.get("year"), "kind": kind, "genres": canon_genres(c.get("genres")),
            "people": people, "runtime": c.get("runtime") or 0, "anime": bool(c.get("anime")),
            "tmdb": c.get("tmdb") or (c.get("id") if c.get("type") in ("movie", "tv") else None),
            "id": c.get("id") if c.get("type") not in ("movie", "tv") else None,
            "rating": c.get("rating"), "overview": c.get("overview") or "",
            "language": c.get("language") or c.get("originalLanguage"), "collection": (c.get("collection") or {}).get("name")
            if isinstance(c.get("collection"), dict) else None}


# ------------------------------------------------------------------ your own star ratings
# A rating is the strongest signal there is: it moves the title's genres, people, era and kind like a rewatch does,
# towards a target set by the stars. Changing it later just moves things again (the averages follow the newest);
# clearing it forgets the rating but keeps what it already taught.
STAR_TARGET = {5: 1.0, 4: .7, 3: .15, 2: -.5, 1: -.9}
RATE_STARS = .25


def rating_key(facts):
    f = _item_facts(facts)
    return f"{f['kind']}:{f['tmdb']}" if f.get("tmdb") else f"id:{f.get('id') or norm(f.get('title'))}"


def my_rating(facts):
    r = (_load().get("ratings") or {}).get(rating_key(facts))
    return r["stars"] if r else 0


def rate(facts, stars):
    """Your 1-5 stars for a title (0 clears). Returns the stored rating (or None when cleared)."""
    f = _item_facts(facts)
    key = rating_key(facts)
    stars = int(stars or 0)
    out = {}

    def go(p):
        ratings = p.setdefault("ratings", {})
        if not stars:
            ratings.pop(key, None)
            return
        apply_signal(p, {**f, "rating": stars}, STAR_TARGET[max(1, min(5, stars))], RATE_STARS,
                     source="your rating", fade=stars >= 4)
        ratings[key] = out["r"] = {"stars": stars, "title": f.get("title"), "year": f.get("year"), "kind": f["kind"],
                                   "tmdb": f.get("tmdb"), "id": f.get("id"), "date": _now()}
    _edit(go)
    return out.get("r")


def score(card, p=None):
    """How well a title fits: {score 0-100, verdict, reasons[], different, top[], bridge}. Works on whatever is known;
    with an empty profile everything is a neutral 50 ("No taste profile yet")."""
    p = p or _load()
    f = _item_facts(card)
    reasons, raw = [], 0.0
    if is_empty(p):
        return {"score": 50, "verdict": "No taste profile yet", "reasons": [], "different": False, "top": [],
                "facts": f, "empty": True}
    gw = {g: p["genres"].get(g, {}).get("w", 0) for g in f["genres"]}
    top = top_genres(3)
    if gw:
        vals = list(gw.values())
        best, worst = max(vals), min(vals)
        g_part = .55 * (sum(vals) / len(vals)) + .45 * (best if best > -worst else worst)
        raw += g_part * 1.3
        for g, w in sorted(gw.items(), key=lambda kv: -kv[1]):
            e = p["genres"].get(g) or {}
            if w >= .3:
                plays = e.get("plays", 0)
                reasons.append(f"You go for {g.lower()}" + (f" ({plays} finished)" if plays >= 2 else ""))
                break
        for g, w in sorted(gw.items(), key=lambda kv: kv[1]):
            if w <= -.2:
                reasons.append(f"{g} isn't usually your thing")
                break
    # vibes and themes that point at these genres / words
    for mood, v in p["moods"].items():
        gs = VIBES.get(mood) or []
        if v and gs and set(gs) & set(f["genres"]):
            raw += .12 * v
            if v > .3 and len(reasons) < 4:
                reasons.append(f"Fits your {mood.lower()} mood")
                break
    text = f"{f['title'] or ''} {f['overview']}".lower()
    for theme, v in p["themes"].items():
        rx = {"Superheroes": r"super-?hero|marvel|avengers|spider-?man|batman|superman|x-men",
              "True crime": r"true story|real-life|murder case|serial killer", "Jump scares": r"\bhorror\b|haunt|demon|possess",
              "Gore": r"slasher|gore|massacre|cannibal|torture"}.get(theme)
        if rx and (re.search(rx, text) or (theme == "Jump scares" and "Horror" in f["genres"])):
            raw += .35 * v
            reasons.append((f"Has the {theme.lower()} you like" if v > 0 else f"Might have the {theme.lower()} you can't stand"))
    if f["anime"] and abs(p["anime"]) > .1:
        raw += .4 * p["anime"]
        reasons.append("It's anime, which you enjoy" if p["anime"] > 0 else "It's anime, not usually your pick")
    if f["language"] and p["languages"].get(f["language"]):
        raw += .3 * p["languages"][f["language"]]
    kw = p["kinds"].get(f["kind"], 0)
    raw += .08 * kw
    d = _decade(f["year"])
    if d and p["eras"].get(d, 0) > .2:
        raw += .1 * p["eras"][d]
    rt = (f["runtime"] or 0) / 60
    if f["kind"] == "movie" and rt:
        if rt > 150 and p["runtime"].get("long", 0) < -.2:
            raw -= .15
            reasons.append(f"It's long ({int(rt)} min), longer than you usually go")
        if rt < 100 and p["runtime"].get("short", 0) > .2:
            raw += .08
    # people
    bridge = None
    for person in f["people"]:
        e = p["people"].get(person.get("name"))
        if e and abs(e["w"]) > .15:
            raw += .45 * e["w"] if person.get("role") == "Director" else .25 * e["w"]
            if e["w"] > .15 and not bridge:
                others = [t for t in e.get("titles", []) if norm(t) != norm(f["title"])]
                role = "director" if person.get("role") == "Director" else "star"
                bridge = (f"shares a {role} with {others[-1]}" if others else f"has {person['name']}") + \
                    (", which you loved" if others else ", who you like")
                reasons.append(f"{'Directed by' if role == 'director' else 'With'} {person['name']}" +
                               (f" ({_list(others[-2:])})" if others else ""))
    # the title itself, or its collection
    for t in p["titles"]["liked"]:
        if (f["tmdb"] and str(t.get("tmdb") or "") == str(f["tmdb"])) or norm(t.get("title")) == norm(f["title"]):
            raw += .9
            reasons.insert(0, "You loved this one" + (f" ({t['rating']}★)" if t.get("rating") else ""))
            break
        if f["collection"] and norm(f["collection"].replace(" Collection", "")) and \
                norm(f["collection"].replace(" Collection", "")) in norm(t.get("title")):
            raw += .35
            bridge = bridge or f"is part of the same series as {t['title']}, which you loved"
            reasons.append(f"Same series as {t['title']}")
            break
    for t in p["titles"]["disliked"]:
        if (f["tmdb"] and str(t.get("tmdb") or "") == str(f["tmdb"])) or norm(t.get("title")) == norm(f["title"]):
            raw -= .9
            reasons.insert(0, "You didn't enjoy this one before")
            break
    # notes (their parsed words)
    for n in p["notes"]:
        tags = n.get("tags") or {}
        hit = [g for g, v in tags.get("genres", {}).items() if g in f["genres"]]
        if hit:
            v = tags["genres"][hit[0]]
            raw += .15 * v * n.get("confidence", .6)
            if len(reasons) < 5 and abs(v) >= .5 and n.get("source") in ("chat", "manual", "letterboxd"):
                reasons.append(("You told me: " if n["source"] != "letterboxd" else "From your Letterboxd: ") + n["text"])
    if f["rating"] and isinstance(f["rating"], (int, float)):
        raw += (float(f["rating"]) - 6.8) * .05
    s = int(round(50 + 50 * math.tanh(raw * 1.05)))
    s = max(1, min(99, s))
    different = bool(f["genres"]) and bool(top) and not (set(f["genres"]) & set(top))
    verdict = "Right up your alley" if s >= 68 else "A bit different for you" if s >= 45 else "Outside your usual, but…"
    if s >= 68 and different:
        verdict = "Different from your usual, but a good bet"
    reasons = list(dict.fromkeys(r for r in reasons if r))[:5]
    if different and top:
        reasons.append(f"Different from your usual ({_list([g.lower() for g in top[:2]])})")
    return {"score": s, "verdict": verdict, "reasons": reasons, "different": different, "top": top, "bridge": bridge,
            "facts": f}


def rank(cards, key="fit"):
    """Sort cards best fit first; each card gets card[key] = 0-100. For ai.py / scripted.py and the UI."""
    p = _load()
    out = []
    for c in cards or []:
        try:
            s = score(c, p)["score"]
        except Exception:
            s = 50
        out.append({**c, key: s})
    out.sort(key=lambda c: -c[key])
    return out


def classic_comment(fit):
    """A one-sentence comment from rules and templates (Classic mode, or while the AI is busy)."""
    f = fit.get("facts") or {}
    title = f.get("title") or "This one"
    top = fit.get("top") or []
    genres = [g.lower() for g in (f.get("genres") or [])][:2]
    kind = "series" if f.get("kind") == "tv" else "film"
    s = fit.get("score", 50)
    if fit.get("empty"):
        return "Tell Atlas what you like (Settings > Taste & memory) and it will say whether this suits you."
    first = (fit.get("reasons") or [""])[0]
    if first.startswith("You loved this one"):
        return f"You loved {title} before, so it's an easy rewatch."
    if first.startswith("You didn't enjoy"):
        return f"You weren't keen on {title} last time; maybe give this one a miss."
    usual = _list([g.lower() for g in top[:2]]) or "your usual picks"
    what = f"a {_list(genres)} {kind}" if genres else f"a {kind}"
    if fit.get("different"):
        lead = f"You usually go for {usual}; this is {what}"
        if fit.get("bridge"):
            return f"{lead}, but it {fit['bridge']}."
        if s >= 55:
            return f"{lead}, but it's well liked and close enough to what you enjoy to be worth a try."
        warn = next((r for r in fit.get("reasons") or [] if "can't stand" in r or "isn't usually" in r), None)
        if warn and s < 45:
            return f"{lead}, and {warn[0].lower() + warn[1:]}, so it's probably not one for you."
        return f"{lead}, so it's outside your usual. Could be a nice change of pace."
    if s >= 68:
        extra = f" It also {fit['bridge']}." if fit.get("bridge") else ""
        return f"Right in your wheelhouse: {what}, the kind you keep finishing.{extra}"
    if s >= 45:
        return f"{title} is {what}, near what you like without being a sure thing." + \
            (f" It {fit['bridge']}." if fit.get("bridge") else "")
    neg = next((r for r in fit.get("reasons") or [] if "isn't usually" in r or "can't stand" in r), None)
    return f"{title} is {what}; {neg[0].lower() + neg[1:] if neg else 'not much like what you usually watch'}."


def verdict_for(s):
    return "Right up your alley" if s >= 68 else "A bit different for you" if s >= 45 else "Outside your usual, but…"


# ------------------------------------------------------------------ public view (Settings > Taste & memory)
def view():
    """Everything the Settings tab shows, in plain language."""
    p = _load()
    genres = [{"name": g, "w": round(e["w"], 3), "stated": e.get("stated", 0), "plays": e.get("plays", 0)}
              for g, e in sorted(p["genres"].items(), key=lambda kv: -kv[1]["w"]) if abs(e["w"]) >= .02]
    return {
        "exists": exists(), "onboarded": p.get("onboarded", False), "rev": p.get("rev", 0),
        "updated": p.get("updated"), "created": p.get("created"),
        "genres": genres,
        "moods": [{"name": k, "w": round(float(v), 3)} for k, v in _strong(p["moods"], .02)],
        "themes": [{"name": k, "w": round(float(v), 3)} for k, v in _strong(p["themes"], .02)],
        "people": favourite_people(12),
        "liked": p["titles"]["liked"][:40], "disliked": p["titles"]["disliked"][:40],
        "notes": p["notes"], "why": p["why"][:8],
        "habits": {"anime": p["anime"], "kinds": p["kinds"], "eras": dict(_strong(p["eras"], .05)),
                   "runtime": p["runtime"], "languages": p["languages"]},
        "plays": p["history"].get("plays", 0), "learning": learning_on(),
        "letterboxd": p.get("letterboxd") or {}, "backup": BACKUP.is_file(),
        "summary": summary(),
    }
