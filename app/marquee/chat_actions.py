"""Ask Atlas, doing things: "pull up Dune", "play the next episode of South Park", "request seasons 5-8 of Doctor
Who", "add Fury to my list", "what's downloading", "when will Dune: Part Three be ready", "why isn't X here yet",
"is everything ok", "why is Sonarr not working", "is the VPN on".

How it reads a message: rules first (plain regular expressions, instant, the same in Classic mode). With AI on, a
message the rules don't recognise but that sounds like a command gets one small JSON step from the model (just the
intent and its arguments); everything after that is code, so small 4B models are enough.

What it may do by itself: open a title's page, start playback (the same as pressing Play), add to My List (with Undo).
What it never does by itself: requests (a confirmation card; only the Request button sends, through the same
/api/storage/request the request sheet uses), marking watched (a confirmation card), deleting (it opens the existing
hold-to-delete sheet), restarting a service (a Fix button the person presses, then confirms).

Stack answers come from marquee.stack (the Media Hub's own checks, or Atlas's read-only checks without a hub) and
never claim a status that wasn't reported. Events go to the page as chat.stream() events: "action" {card} and
"ui" {do, ...} besides the usual delta / cards / done."""
import difflib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import ai, settings, taste

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="chat-act")

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
       "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
       "eighteen": 18, "nineteen": 19, "twenty": 20, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
       "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
N = r"(?:\d{1,2}|" + "|".join(sorted(NUM, key=len, reverse=True)) + r")"
FILLER = re.compile(r"^(?:(?:hey|hi|ok|okay|yo)\s+)?(?:atlas[\s,]+)?(?:(?:please|pls|can you|could you|would you|will you|"
                    r"can u|i want to|i wanna|i'd like to|i would like to|let's|lets|just|go ahead and|now|quickly|"
                    r"kindly)\s+)+", re.I)
TAIL = re.compile(r"(?:\s+(?:for me|please|pls|thanks|thank you|now|right now|on atlas|in atlas|real quick))+\s*$", re.I)
VAGUE = re.compile(r"^(?:something|anything|some|a (?:movie|film|show|series)|an? (?:good|funny|scary|new)|me (?:something|"
                   r"a)|what|whatever|stuff|more)\b", re.I)
SERVICES = {
    "jellyfin": "jellyfin", "seerr": "seerr", "overseerr": "seerr", "jellyseerr": "seerr", "requests app": "seerr",
    "sonarr": "sonarr", "radarr": "radarr", "prowlarr": "prowlarr", "indexers": "prowlarr", "indexer": "prowlarr",
    "qbittorrent": "qbittorrent", "qbit": "qbittorrent", "qbt": "qbittorrent", "torrent client": "qbittorrent",
    "torrents": "qbittorrent", "bazarr": "bazarr", "subtitles": "bazarr", "lidarr": "lidarr", "trawl": "trawl",
    "byparr": "byparr", "flaresolverr": "byparr", "cleanuparr": "cleanuparr", "autobrr": "autobrr", "jfa-go": "jfago",
    "jfago": "jfago", "requestrr": "requestrr", "unpackerr": "unpackerr", "mediasorter": "mediasorter",
    "media sorter": "mediasorter", "qui": "qui", "recyclarr": "recyclarr", "gluetun": "gluetun",
    "media hub": "hub", "the hub": "hub", "hub": "hub", "the stack": "all", "my stack": "all", "the server": "all",
    "my server": "all", "media server": "all", "everything": "all", "the vpn": "vpn", "vpn": "vpn",
    "downloads": "downloads", "my downloads": "downloads", "the downloads": "downloads", "downloading": "downloads",
}
SERVICE_RX = "|".join(sorted((re.escape(k) for k in SERVICES), key=len, reverse=True))
KINDS = ("open", "play", "request", "mylist", "watched", "delete", "downloads", "eta", "doctor", "stack")
HINT = re.compile(r"\b(request|download|grab|play|watch|resume|continue|open|pull up|show|bring up|mark|delete|remove|"
                  r"add|my list|queue|ready|eta|stuck|why|working|broken|down|fix|restart|vpn|status|health|season|4k|"
                  r"1080|720)\b", re.I)


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


def _norm(s):
    s = taste.norm(s) if s else ""
    return re.sub(r"^(?:the|a|an)\s+", "", s)


def _num(tok):
    tok = (tok or "").lower().strip()
    return int(tok) if tok.isdigit() else NUM.get(tok)


def _fmt_seasons(nums):
    """[5, 6, 7, 8] -> "Seasons 5–8"; [2] -> "Season 2"; [1, 3, 4] -> "Seasons 1, 3–4"."""
    nums = sorted(set(nums))
    if not nums:
        return "No seasons"
    runs, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        runs.append(f"{start}–{prev}" if prev > start else str(start))
        if n is not None:
            start = prev = n
    return ("Season " if len(nums) == 1 else "Seasons ") + ", ".join(runs)


def _ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def _eta(sec):
    if sec is None or sec < 0 or sec >= 8640000:
        return None
    if sec < 90:
        return "under 2 min"
    if sec < 3600:
        return f"about {round(sec / 60)} min"
    if sec < 86400:
        h, m = divmod(round(sec / 60), 60)
        return f"about {h} h {m:02d} min" if m else f"about {h} h"
    return f"about {round(sec / 86400)} days"


def _timeleft(s):
    """Sonarr/Radarr "timeleft" ("01:02:03" or "1.02:03:04") -> seconds."""
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


# ------------------------------------------------------------------ reading the message (rules)
def _clean(text):
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = t.strip(" .!?").replace("’", "'")
    t = FILLER.sub("", t)
    t = TAIL.sub("", t)
    return t.strip(" .!?,")


def _title_bits(raw):
    """"Doctor Who (2005)", "the show Dune", "dune 2021" -> (title, year, kind)."""
    t = (raw or "").strip().strip("\"'“”‘’").strip()
    kind = None
    m = re.match(r"^(?:the\s+)?(show|series|tv show|movie|film)\s+(?:called\s+)?(.+)$", t, re.I)
    if m:
        kind = "movie" if m.group(1).lower() in ("movie", "film") else "tv"
        t = m.group(2)
    m = re.match(r"^(.+?)\s+(?:the\s+)?(show|series|tv show|movie|film)$", t, re.I)
    if m and len(m.group(1)) > 1:
        kind = "movie" if m.group(2).lower() in ("movie", "film") else "tv"
        t = m.group(1)
    year = None
    m = re.match(r"^(.*?)\s*[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$", t)
    if m:
        t, year = m.group(1), int(m.group(2))
    else:
        m = re.match(r"^(.+?)\s+(?:from\s+)?((?:19|20)\d{2})$", t)
        if m and int(m.group(2)) <= time.localtime().tm_year + 2 and len(m.group(1).split()) >= 1:
            t, year = m.group(1), int(m.group(2))
    t = t.strip(" -–:,").strip("\"'“”").strip()
    return t, year, kind


def parse_seasons(text):
    """(spec, smart, rest_of_text). spec: None | "all" | "rest" | "latest" | [numbers]."""
    t = " " + text + " "
    spec, smart = None, False
    if re.search(r"\b(?:smart(?:ly)?|as i watch|one (?:season )?at a time|bit by bit)\b", t, re.I):
        smart = True
        t = re.sub(r"\s*(?:,\s*)?\b(?:smart(?:ly)?|as i watch|one (?:season )?at a time|bit by bit)\b", " ", t, flags=re.I)
    rules = [
        (r"\b(?:the\s+)?(?:rest|remaining(?:\s+seasons?)?|missing\s+seasons?|other\s+seasons?|seasons?\s+i\s+don'?t\s+have)"
         r"(?:\s+of(?:\s+the\s+seasons)?)?\b", lambda m: "rest"),
        (r"\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+)?seasons?(?:\s+of)?\b|\b(?:the\s+)?(?:whole|entire|complete)\s+"
         r"(?:show|series|thing)(?:\s+of)?\b", lambda m: "all"),
        (r"\b(?:the\s+)?(?:latest|newest|new|last|current|most recent)\s+season(?:\s+of)?\b", lambda m: "latest"),
        (rf"\b(?:seasons?|s)\s*({N})\s*(?:-|–|to|through|thru|until|till)\s*(?:seasons?\s*|s)?({N})\b(?:\s+of)?",
         lambda m: list(range(_num(m.group(1)), _num(m.group(2)) + 1)) if _num(m.group(1)) and _num(m.group(2)) and
         _num(m.group(1)) <= _num(m.group(2)) <= 60 else None),
        (rf"\bseasons?\s+((?:{N})(?:\s*(?:,|and|&|\+)\s*(?:seasons?\s*)?(?:{N}))+)\b(?:\s+of)?",
         lambda m: [_num(x) for x in re.split(r"\s*(?:,|and|&|\+)\s*(?:seasons?\s*)?", m.group(1)) if _num(x)]),
        (rf"\b(?:season|series)\s+({N})\b(?:\s+of)?", lambda m: [_num(m.group(1))] if _num(m.group(1)) else None),
        (r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\s+season(?:\s+of)?\b",
         lambda m: [_num(m.group(1))]),
        (r"\bs(\d{1,2})\b(?!\s*e\d)", lambda m: [int(m.group(1))]),
    ]
    for rx, fn in rules:
        m = re.search(rx, t, re.I)
        if m:
            val = fn(m)
            if val:
                spec = val
                t = t[:m.start()] + " " + t[m.end():]
                break
    return spec, smart, re.sub(r"\s+", " ", t).strip()


def parse_quality(text):
    t = text
    q = None
    for rx, val in ((r"\b(?:in|at)?\s*(?:4k|2160p?|uhd|ultra[\s-]?hd)\b(?:\s+quality)?", "4k"),
                    (r"\b(?:in|at)?\s*(?:1080p?|full[\s-]?hd|fhd)\b(?:\s+quality)?", "1080"),
                    (r"\b(?:in|at)?\s*720p?\b(?:\s+quality)?", "720"),
                    (r"\b(?:in|at)\s+hd\b", "1080")):
        m = re.search(rx, t, re.I)
        if m:
            q = val
            t = (t[:m.start()] + " " + t[m.end():]).strip()
            break
    return q, re.sub(r"\s+", " ", t).strip()


def _strip_joiners(t):
    t = re.sub(r"^(?:of|for|from)\s+", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+(?:of|for|from|please)$", "", t, flags=re.I)
    return t.strip(" ,")


def _service(word):
    w = (word or "").lower().strip(" ?.!")
    w = re.sub(r"^(?:my|the)\s+", "", w)
    return SERVICES.get(w) or SERVICES.get("the " + w) or SERVICES.get("my " + w)


def parse(text):
    """Rule-based intent for a chat message, or None (then it's a normal recommendation chat)."""
    t = _clean(text)
    if not t or len(t) > 300:
        return None
    low = t.lower()

    # --- the stack
    if re.search(r"\bvpn\b", low) and re.search(r"\b(on|off|up|down|working|connected|leak|leaking|status|ok|okay|"
                                                r"running|check|safe|active)\b|^(?:is|are|check|how)", low):
        return {"intent": "stack", "topic": "vpn"}
    if re.search(r"^(?:is|are)\s+(?:everything|all|it all|the (?:stack|server)|my (?:stack|server)|things)\s+"
                 r"(?:ok|okay|fine|good|alright|all right|working|running|up|healthy)\b", low) or \
       re.search(r"^(?:is\s+)?(?:anything|something|what'?s?|what is)\s+(?:wrong|broken|down|failing)(?:\s+(?:with\s+)?"
                 r"(?:my\s+|the\s+)?(?:stack|server|media server|setup|system|things|it|anything))?$", low) or \
       re.search(r"^(?:how'?s|how is|check|status of|health of|what'?s the status of)\s+(?:my |the )?(?:stack|server|"
                 r"media server|setup|system|everything)\b", low) or \
       re.search(r"^(?:stack|server|system)\s+(?:status|health|check)$|^(?:health ?check|status check|run a health check)$", low):
        return {"intent": "stack", "topic": "all"}
    if re.search(r"\b(?:can'?t|cannot|can not|unable to|won'?t)\s+(?:reach|connect to|access|open|get to|load|use)\s+"
                 r"(?:my\s+)?(\w[\w\s-]*?)\s+(?:from|on|outside|away|when i'?m)\b", low) or \
       re.search(r"\b(?:remote access|from my phone|outside (?:my |the )?(?:home|house|network))\b", low) and \
       re.search(r"\b(?:why|can'?t|cannot|how|reach|connect|access|work)\b", low):
        m = re.search(rf"\b({SERVICE_RX})\b", low)
        return {"intent": "stack", "topic": "remote", "service": _service(m.group(1)) if m else "jellyfin"}
    if re.search(r"\bdownloads?\b.*\b(?:stuck|slow|stalled|not moving|frozen|hung|failing|not working|broken)\b|"
                 r"\b(?:why|what)\b.*\b(?:nothing|not)\s+(?:is\s+)?downloading\b|\bwhy is my download\b", low):
        return {"intent": "stack", "topic": "downloads"}
    m = re.search(rf"^(?:why\s+(?:is|are|isn'?t|aren'?t|does|doesn'?t|won'?t|did)|is|are|what'?s wrong with|what is wrong with|"
                  rf"check|fix|diagnose|troubleshoot|how'?s|how is)\s+(?:my\s+|the\s+)?({SERVICE_RX})\b(.*)$", low)
    if m and (re.search(r"\b(?:work|working|down|up|broken|failing|fail|ok|okay|running|wrong|error|errors|slow|"
                        r"responding|start|load|loading|connect)\b", m.group(2) or "") or
              low.startswith(("what", "check", "fix", "diagnose", "troubleshoot", "how"))):
        svc = _service(m.group(1))
        if svc in ("all",):
            return {"intent": "stack", "topic": "all"}
        if svc == "vpn":
            return {"intent": "stack", "topic": "vpn"}
        if svc == "downloads":
            return {"intent": "stack", "topic": "downloads"}
        return {"intent": "stack", "topic": "service", "service": svc}
    m = re.search(rf"^restart\s+(?:my\s+|the\s+)?({SERVICE_RX})$", low)
    if m and _service(m.group(1)) not in ("all", "vpn", "downloads"):
        return {"intent": "stack", "topic": "service", "service": _service(m.group(1)), "restart": True}

    # --- downloads and readiness
    if re.search(r"^(?:what'?s|what is|what are|whats|anything|is anything|show(?: me)?|list|check)\s+(?:my\s+|the\s+)?"
                 r"(?:currently\s+|still\s+)?(?:downloading|downloads?|download queue|queue|in the queue|being downloaded)\b", low) or \
       re.search(r"^(?:my\s+|the\s+)?(?:downloads?|download queue|queue)$", low):
        return {"intent": "downloads"}
    m = re.search(r"^(?:when\s+(?:will|is|does|should)|how long\s+(?:until|till|til|before|for|does|will)|eta\s+(?:for|on)|"
                  r"is)\s+(.+?)\s+(?:be\s+)?(?:ready|here|done|downloaded|finished|available|arrive|arriving|"
                  r"come|coming|take|finish|show up|land)(?:\s+(?:yet|to download|downloading|to arrive))?$", low)
    if m and not _service(m.group(1)):
        return {"intent": "eta", **_t(m.group(1), t, m.start(1), m.end(1))}
    m = re.search(r"^(?:why\s+(?:isn'?t|is not|hasn'?t|has not|haven'?t|didn'?t|is|has)|where(?:'s| is| are)|what happened to|"
                  r"what'?s up with|what'?s going on with|whats going on with)\s+(?:my\s+)?(?:request for\s+)?(.+?)"
                  r"(?:\s+(?:not\s+)?(?:here|there|in my library|in the library|available|showing up|downloaded|"
                  r"downloading|arrived|come|come in|shown up|finished|ready)(?:\s+yet)?|\s+taking so long|\s+stuck|"
                  r"\s+yet|\s+request)?$", low)
    if m and not _service(m.group(1)) and not VAGUE.match(m.group(1)) and not re.match(r"^(?:it|that|this|they)$", m.group(1)):
        if low.startswith(("why", "what happened", "what's up", "whats", "what's going")) or \
           re.search(r"\b(?:my|request|here|yet)\b", low):
            return {"intent": "doctor", **_t(m.group(1), t, m.start(1), m.end(1))}

    # --- My List, watched, delete
    m = re.search(r"^(?:add|put|save|stick)\s+(.+?)\s+(?:to|on|in|onto)\s+(?:my\s+)?(?:watch\s*)?list$", low)
    if m:
        return {"intent": "mylist", "on": True, **_t(m.group(1), t, m.start(1), m.end(1))}
    m = re.search(r"^(?:remove|take|delete)\s+(.+?)\s+(?:from|off|out of)\s+(?:my\s+)?(?:watch\s*)?list$", low)
    if m:
        return {"intent": "mylist", "on": False, **_t(m.group(1), t, m.start(1), m.end(1))}
    m = re.search(r"^mark\s+(.+?)\s+(?:as\s+)?(watched|seen|done|unwatched|unseen|not watched|not seen|new)$", low)
    if m:
        return {"intent": "watched", "on": m.group(2) in ("watched", "seen", "done"), **_watched_bits(t[m.start(1):m.end(1)])}
    m = re.search(r"^(?:i'?ve|i have|i|we'?ve|we have|we)\s+(?:already\s+|just\s+|finally\s+|also\s+)?(?:seen|watched|saw|"
                  r"finished(?:\s+watching)?)\s+(.+)$", low)
    if m and not VAGUE.match(m.group(1)) and not re.search(
            r"\b(?:recommend|suggest|similar|something|anything|what should|what to|what else|like it|like that|"
            r"any good|everything|all of (?:them|it))\b|^(?:it|that|this|them|those|these)\b", m.group(1)):
        bits = _watched_bits(re.sub(r"\s+(?:already|before|last (?:night|week|weekend|month|year)|yesterday|today|tonight|"
                                    r"recently|a while ago|(?:years|ages|months) ago|twice|again|too|as well)$", "",
                                    re.sub(r"\s+(?:and|but)\s+(?:i\b|it\b|loved|liked|hated|enjoyed|really|thought|was|"
                                           r"didn'?t|did ?n'?t)\b.*$", "", t[m.start(1):], flags=re.I), flags=re.I))
        if bits["title"]:
            return {"intent": "watched", "on": True, **bits}
    m = re.search(r"^(?:delete|erase|get rid of|remove)\s+(.+?)(?:\s+(?:from|off)\s+(?:my |this |the )?(?:pc|computer|"
                  r"library|disk|drive|hard drive|server|storage))?$", low)
    if m and not VAGUE.match(m.group(1)):
        return {"intent": "delete", **_t(m.group(1), t, m.start(1), m.end(1))}

    # --- requests
    m = re.search(r"^(?:request|download|grab|fetch|queue up|get|add)\s+(.+)$", low)
    if m and not VAGUE.match(m.group(1)) and not re.match(r"^(?:me\s+)?(?:a\s+)?(?:recommendation|suggestion|idea)", m.group(1)):
        rest = t[m.start(1):]
        verb = low.split()[0]
        spec, smart, rest = parse_seasons(rest)
        quality, rest = parse_quality(rest)
        rest = _strip_joiners(rest)
        if verb in ("get", "add") and not (spec or smart or quality) and not re.search(r"\b(?:request|download)\b", low):
            pass  # "get dune" alone is a request only if it resolves; "add x" without "to my list" too
        title, year, kind = _title_bits(rest)
        if spec is not None or smart:
            kind = "tv"
        if not title and not (spec or smart):
            return None
        return {"intent": "request", "title": title, "year": year, "kind": kind, "seasons": spec, "smart": smart,
                "quality": quality, "soft": verb in ("get", "add", "grab", "fetch")}

    # --- playing and opening
    m = re.search(r"^(?:play|watch|put on|start|resume|continue|keep watching|carry on with)\s+(?:watching\s+)?(.+)$", low)
    if m and not VAGUE.match(m.group(1)):
        rest = t[m.start(1):]
        nxt = bool(re.match(r"^(?:the\s+)?(?:next|latest|new|newest)\s+(?:episode|ep)\s+(?:of\s+)?", rest, re.I))
        rest = re.sub(r"^(?:the\s+)?(?:next|latest|new|newest)\s+(?:episode|ep)\s+(?:of\s+)?", "", rest, flags=re.I)
        se = re.search(rf"\s*(?:,\s*)?(?:s(\d{{1,2}})\s*e(\d{{1,3}})|season\s+({N})\s*,?\s*episode\s+({N}))\s*$", rest, re.I)
        season = episode = None
        if se:
            season = int(se.group(1) or 0) or _num(se.group(3))
            episode = int(se.group(2) or 0) or _num(se.group(4))
            rest = rest[:se.start()]
        title, year, kind = _title_bits(_strip_joiners(rest))
        if not title:
            return None
        return {"intent": "play", "title": title, "year": year, "kind": kind, "next": nxt or low.startswith(("resume",
                "continue", "keep watching", "carry on")), "season": season, "episode": episode}
    m = re.search(r"^(?:pull up|open|show me|bring up|go to|take me to|look up|find|show|load|display|search for)\s+"
                  r"(?:the\s+(?:page|details)\s+(?:for|of)\s+)?(.+)$", low)
    if m and not VAGUE.match(m.group(1)) and not re.search(r"\b(?:something|anything|recommend|ideas?|like)\b", m.group(1)):
        verb = low.split()[0]
        title, year, kind = _title_bits(_strip_joiners(t[m.start(1):]))
        if title:
            return {"intent": "open", "title": title, "year": year, "kind": kind,
                    "soft": verb in ("show", "find", "look", "search", "load", "display")}
    return None


def _watched_bits(raw):
    """"The Bear season 1" -> {title, year, kind: "tv", seasons: [1]} (seasons None for the whole title)."""
    spec, _, rest = parse_seasons(raw)
    title, year, kind = _title_bits(_strip_joiners(rest))
    out = {"title": title, "year": year, "kind": kind, "seasons": spec if isinstance(spec, list) else None}
    if out["seasons"]:
        out["kind"] = "tv"
    return out


def _t(raw_low, orig, a, b):
    """The title part of a match, from the original text (keeps its capitals)."""
    title, year, kind = _title_bits(_strip_joiners(orig[a:b]))
    return {"title": title, "year": year, "kind": kind}


# ------------------------------------------------------------------ the model's turn (only when the rules missed)
INTENT_SYSTEM = """You turn one message to a home media app into JSON. Reply with JSON only, no prose:
{"intent": "open|play|request|mylist|watched|delete|downloads|eta|doctor|stack|chat", "title": "", "year": null,
 "seasons": "", "quality": "", "service": "", "next": false}
- open: show a title's page. play: start watching ("next": true for the next episode). request: ask for a film/show to
  be downloaded ("seasons": like "5-8", "2", "all", "rest", "latest", "smart"; "quality": "4k", "1080", "720" or "").
- mylist: add to My List. watched: mark as watched. delete: delete from the PC. downloads: what is downloading.
  eta: when a title will be ready. doctor: why a requested title hasn't arrived. stack: whether the media server or
  one of its apps (jellyfin, seerr, sonarr, radarr, prowlarr, qbittorrent, vpn) works ("service").
- chat: anything else (recommendations, questions about films, small talk).
Use the exact title the person wrote, without seasons or quality."""


def model_intent(message):
    if not HINT.search(message or ""):
        return None
    try:
        out = ai.chat(f"Message: {message[:400]}", INTENT_SYSTEM, max_tokens=120, timeout=25, temperature=0)
    except Exception as e:
        print("chat intent (model) failed:", type(e).__name__, e)
        return None
    m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", out or "", flags=re.S), re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    intent = (d.get("intent") or "").strip().lower()
    if intent not in ("open", "play", "request", "mylist", "watched", "delete", "downloads", "eta", "doctor", "stack"):
        return None
    title, year, kind = _title_bits(str(d.get("title") or ""))
    if d.get("year") and str(d["year"]).isdigit():
        year = int(d["year"])
    out = {"intent": intent, "title": title, "year": year, "kind": kind, "by": "model"}
    if intent == "request":
        spec, smart, _ = parse_seasons(f"seasons {d.get('seasons')}" if re.match(r"^\s*\d", str(d.get("seasons") or ""))
                                       else str(d.get("seasons") or ""))
        q, _ = parse_quality(str(d.get("quality") or ""))
        out.update(seasons=spec, smart=smart or str(d.get("seasons")).lower() == "smart", quality=q)
        if spec or out["smart"]:
            out["kind"] = "tv"
    elif intent == "play":
        out.update(next=bool(d.get("next")), season=None, episode=None)
    elif intent == "mylist":
        out["on"] = True
    elif intent == "watched":
        out["on"] = True
    elif intent == "stack":
        svc = _service(str(d.get("service") or "")) or "all"
        out = {"intent": "stack", "topic": svc if svc in ("vpn", "downloads", "all") else "service", "service": svc,
               "by": "model"}
    if intent not in ("downloads", "stack") and not out.get("title"):
        return None
    return out


# ------------------------------------------------------------------ finding the title
def _lib_matches(title, year=None, kind=None):
    cat = _cat()
    want = _norm(title)
    if cat is None or not want:
        return []
    types = {"movie": "Movie", "tv": "Series"}.get(kind, "Movie,Series")
    out = []
    with cat.lock:
        for r in cat._of(types):
            if _norm(r.get("name")) != want:
                continue
            if year and r.get("year") and abs(int(r["year"]) - int(year)) > 1:
                continue
            out.append(r)
    out.sort(key=lambda r: -(r.get("year") or 0))
    return out


def _seerr_matches(title, year=None, kind=None, loose=False):
    app = _app()
    if not (app and app.seerr.ready):
        return [], []
    try:
        results = [c for c in app.seerr.search(title) if c.get("type") in ("movie", "tv")]
    except Exception:
        return [], []
    if kind:
        results = [c for c in results if c["type"] == kind]
    want = _norm(title)

    def year_ok(c):
        return not year or not c.get("year") or abs(int(c["year"]) - int(year)) <= 1
    exact = [c for c in results if _norm(c.get("title")) == want and year_ok(c)]
    close = [c for c in results if c not in exact and year_ok(c) and
             difflib.SequenceMatcher(None, _norm(c.get("title")), want).ratio() >= (.8 if loose else .88)]
    return exact, close or [c for c in results if year_ok(c)][:3]


def _poster(url):
    try:
        from .catalog import tmdb_img
        return tmdb_img(url) if url else None
    except Exception:
        return url


def _lib_option(r):
    c = _cat().card(r)
    return {"label": f"{r.get('name')}" + (f" ({r['year']})" if r.get("year") else ""),
            "sub": ("Series" if r["type"] == "Series" else "Film") + " · in your library", "poster": c.get("poster"),
            "pick": {"library": r["id"]}}


def _seerr_option(c, seasons=None):
    bits = ["Series" if c["type"] == "tv" else "Film"]
    if seasons:
        bits.append(f"{seasons} season{'s' if seasons != 1 else ''}")
    st = {2: "requested", 3: "downloading", 4: "partly here", 5: "in your library"}.get(c.get("status"))
    if st:
        bits.append(st)
    return {"label": f"{c['title']}" + (f" ({c['year']})" if c.get("year") else ""), "sub": " · ".join(bits),
            "poster": _poster(c.get("poster")), "pick": {"type": c["type"], "id": c["id"]}}


def _season_counts(cands):
    app = _app()

    def count(c):
        if c["type"] != "tv":
            return None
        try:
            d = app.seerr.call("GET", f"/tv/{c['id']}", timeout=8) or {}
            return len([s for s in d.get("seasons") or [] if s.get("seasonNumber", 0) > 0])
        except Exception:
            return None
    futs = [_pool.submit(count, c) for c in cands]
    return [f.result(timeout=12) if f else None for f in futs]


def resolve_for(intent, pick=None):
    """("library", row) | ("tmdb", card) | ("choose", {prompt, options}) | ("none", message)."""
    cat = _cat()
    if pick:
        if pick.get("library"):
            r = cat.get(pick["library"]) if cat else None
            return ("library", r) if r else ("none", "That title isn't in the library any more.")
        kind, tmdb = pick.get("type"), pick.get("id")
        owned = cat.by_tmdb(kind, tmdb) if cat else None
        if owned and intent["intent"] in ("open", "play", "mylist", "watched", "delete"):
            return "library", owned
        try:
            d = _app().seerr.details(kind, int(tmdb))
        except Exception:
            return "none", "Seerr didn't answer, so I can't look that up right now."
        return "tmdb", {**d, "type": kind, "id": int(tmdb)}
    title, year, kind = intent.get("title"), intent.get("year"), intent.get("kind")
    if not title:
        return "none", "Which title?"
    lib = _lib_matches(title, year, kind)
    local_only = intent["intent"] in ("play", "delete")
    if local_only or (intent["intent"] in ("open", "mylist", "watched") and lib):
        if len(lib) == 1:
            return "library", lib[0]
        if len(lib) > 1:
            return "choose", {"prompt": f"You have more than one “{title}”. Which one?",
                              "options": [_lib_option(r) for r in lib[:5]]}
        if local_only:
            # nothing exact: a close library match is fine for playing ("play dune part two")
            hits = [c for c in (cat.search(title, types=("Movie", "Series"), limit=5) if cat else [])]
            if len(hits) == 1 or (hits and _norm(hits[0].get("name")).startswith(_norm(title))):
                return "library", cat.get(hits[0]["id"])
            exact, close = _seerr_matches(title, year, kind)
            c = (exact or close or [None])[0]
            return "none", ({"text": f"“{c['title']}” isn't in your library yet.", "tmdb": c} if c else
                            f"I couldn't find “{title}” in your library.")
    exact, close = _seerr_matches(title, year, kind, loose=intent["intent"] == "open")
    if intent["intent"] == "request" and isinstance(intent.get("seasons"), list) and len(exact) > 1:
        counts = _season_counts(exact)
        need = max(intent["seasons"])
        fit = [(c, n) for c, n in zip(exact, counts) if n is None or n >= need]
        if fit:
            exact = [c for c, _ in fit]
            counts = [n for _, n in fit]
        if len(exact) == 1:
            return "tmdb", exact[0]
        return "choose", {"prompt": f"There's more than one {('show' if exact[0]['type'] == 'tv' else 'title')} called "
                                    f"“{exact[0]['title']}”. Which one?",
                          "options": [_seerr_option(c, n) for c, n in zip(exact[:4], counts[:4])]}
    if len(exact) == 1:
        return "tmdb", exact[0]
    if len(exact) > 1:
        # the one you already own wins for opening; otherwise ask
        if intent["intent"] in ("open", "mylist"):
            owned = [c for c in exact if c.get("status") in (4, 5)]
            if len(owned) == 1:
                return "tmdb", owned[0]
        counts = _season_counts(exact[:4]) if any(c["type"] == "tv" for c in exact[:4]) else [None] * len(exact[:4])
        return "choose", {"prompt": f"There's more than one “{exact[0]['title']}”. Which one?",
                          "options": [_seerr_option(c, n) for c, n in zip(exact[:4], counts)]}
    if close:
        if len(close) == 1 or intent["intent"] == "open" and intent.get("soft") is False:
            if difflib.SequenceMatcher(None, _norm(close[0]["title"]), _norm(title)).ratio() >= .88:
                return "tmdb", close[0]
        return "choose", {"prompt": f"I couldn't find “{title}” exactly. Did you mean one of these?",
                          "options": [_seerr_option(c) for c in close[:4]]}
    return "none", f"I couldn't find “{title}”" + (" (Seerr isn't connected)" if not _app().seerr.ready else "") + "."


def _card_of(kind, item):
    from . import chat
    if kind == "library":
        return chat.library_card(item)
    return chat.seerr_card(item)


def _subject(kind, item):
    if kind == "library":
        tmdb = (item.get("provider") or {}).get("Tmdb")
        return {"source": "library", "id": item["id"], "title": item.get("name"), "year": item.get("year"),
                "type": "tv" if item["type"] == "Series" else "movie", "tmdb": int(tmdb) if str(tmdb or "").isdigit() else None}
    return {"source": "seerr", "id": item["id"], "tmdb": item["id"], "title": item.get("title"), "year": item.get("year"),
            "type": item["type"]}


# ------------------------------------------------------------------ requests: what to send
def _taken(d, is4k):
    """(available season numbers, requested season numbers) from Seerr's TV details."""
    info = d.get("mediaInfo") or {}
    avail = {s["seasonNumber"] for s in info.get("seasons") or []
             if (s.get("status4k") if is4k else s.get("status")) == 5}
    partly = {s["seasonNumber"] for s in info.get("seasons") or []
              if (s.get("status4k") if is4k else s.get("status")) == 4}
    asked = set()
    for r in info.get("requests") or []:
        if r.get("status") in (3, 4) or bool(r.get("is4k")) != bool(is4k):  # declined / failed don't count
            continue
        asked |= {s["seasonNumber"] for s in r.get("seasons") or []}
    return avail, (asked - avail) | (partly & asked)


def _pick_quality(opts, quality, anime):
    """(server, profile, note) from the request options: the owner's defaults, or the asked-for resolution."""
    servers = opts.get("servers") or []
    choice = opts.get("choice") or {}
    if not servers:
        return None, None, None
    by_id = {s["id"]: s for s in servers}
    default_srv = by_id.get(choice.get("server")) or servers[0]

    def prof_of(srv, pid):
        return next((p for p in srv["profiles"] if p["id"] == pid), None)
    if not quality:
        srv = default_srv
        return srv, prof_of(srv, choice.get("profile")) or (srv["profiles"] or [None])[0], None
    want = {"4k": "4k", "1080": "1080", "720": "720"}[quality]

    def tier_ok(p):
        t = p["tier"].replace("anime-", "")
        return t == want and (p["tier"].startswith("anime") == bool(anime) or not any(
            x["tier"].replace("anime-", "") == want and x["tier"].startswith("anime") == bool(anime)
            for s in servers for x in s["profiles"]))
    pools = ([s for s in servers if s["is4k"]] + [s for s in servers if not s["is4k"]]) if want == "4k" else \
        ([default_srv] if not default_srv["is4k"] else []) + [s for s in servers if not s["is4k"] and s is not default_srv] + \
        [s for s in servers if s["is4k"]]
    for srv in pools:
        hits = [p for p in srv["profiles"] if tier_ok(p)]
        if hits:
            hits.sort(key=lambda p: (not p.get("default"), not p.get("featured"), not p.get("upgrades")))
            return srv, hits[0], None
    srv = default_srv
    prof = prof_of(srv, choice.get("profile")) or (srv["profiles"] or [None])[0]
    label = {"4k": "4K", "1080": "1080p", "720": "720p"}[quality]
    return srv, prof, f"There's no {label} option set up in Seerr, so this uses {prof['label'] if prof else 'the default'}."


def _default_mode(d, kind):
    s = settings.load()
    prefs = (s.get("request_prefs") or {})
    anime = d.get("originalLanguage") == "ja" and 16 in [g.get("id") for g in d.get("genres") or []]
    pref = prefs.get("anime" if anime else kind) or {}
    n = len([x for x in d.get("seasons") or [] if x.get("seasonNumber", 0) > 0])
    if pref.get("season_mode") in ("all", "latest", "smart") and (pref["season_mode"] != "smart" or n >= 2):
        return pref["season_mode"]
    return "smart" if n >= 2 and s.get("smart_request", True) else "all"


def build_request(kind, tmdb, intent):
    """Everything the confirmation card needs, worked out with storage's estimates and limit check. Nothing is sent:
    the body is checked with a dry run (Seerr's exact request body comes back in "payload")."""
    from . import storage
    app = _app()
    d = app.seerr.call("GET", f"/{kind}/{tmdb}", timeout=15) or {}
    title = d.get("title") or d.get("name") or "That title"
    date = d.get("releaseDate") or d.get("firstAirDate") or ""
    year = date[:4] or None
    poster = _poster(f"https://image.tmdb.org/t/p/w500{d['posterPath']}") if d.get("posterPath") else None
    anime = d.get("originalLanguage") == "ja" and 16 in [g.get("id") for g in d.get("genres") or []]
    quality = intent.get("quality")
    # Which server it would go to decides what "already here" means (a 4K server keeps its own copy).
    srv0, _, _ = _pick_quality(storage.request_options(app, kind, int(tmdb), "all"), quality, anime)
    is4k_wanted = bool(srv0 and srv0.get("is4k"))
    info = d.get("mediaInfo") or {}
    notes = []
    base = {"type": kind, "id": int(tmdb), "title": title, "year": year, "poster": poster, "key": f"{kind}:{tmdb}",
            "anime": anime}
    if kind == "movie":
        st = info.get("status4k") if is4k_wanted else info.get("status")
        if st == 5:
            extra = (" Seerr has no separate 4K server, so a 4K copy would have to come from Radarr upgrading the one "
                     "you have (change its quality profile there).") if quality == "4k" and not is4k_wanted else ""
            return {**base, "done": "available", "text": f"{title} is already in your library.{extra}"}
        if st in (2, 3) or (st == 4):
            return {**base, "done": "requested", "text": f"{title} is already requested and on its way."}
        seasons, mode = None, None
    else:
        all_nums = [s["seasonNumber"] for s in d.get("seasons") or [] if s.get("seasonNumber", 0) > 0]
        if not all_nums:
            return {**base, "done": "none", "text": f"TMDB doesn't list any seasons for {title} yet, so there's nothing to request."}
        avail, asked = _taken(d, is4k_wanted)
        spec = intent.get("seasons")
        smart = bool(intent.get("smart"))
        if spec is None and not smart:
            spec = _default_mode(d, kind)
            if spec == "smart":
                smart, spec = True, None
        wanted_label = None
        if isinstance(spec, list):
            missing = [n for n in spec if n not in all_nums]
            if missing:
                notes.append(f"{title} has {len(all_nums)} season{'s' if len(all_nums) != 1 else ''}, so "
                             f"{_fmt_seasons(missing).lower()} {'doesn' if len(missing) == 1 else 'don'}'t exist.")
            wanted = [n for n in spec if n in all_nums]
            mode = "pick"
        elif spec == "latest":
            wanted, mode = all_nums[-1:], "latest"
        elif spec in ("all", "rest"):
            wanted, mode = list(all_nums), "all"
            wanted_label = "the rest" if spec == "rest" else None
        else:
            wanted, mode = list(all_nums), "smart"
        here = sorted(n for n in wanted if n in avail)
        coming = sorted(n for n in wanted if n in asked)
        free = [n for n in wanted if n not in avail and n not in asked]
        if here:
            notes.append(f"{_fmt_seasons(here)} {'is' if len(here) == 1 else 'are'} already in your library.")
        if coming:
            notes.append(f"{_fmt_seasons(coming)} {'is' if len(coming) == 1 else 'are'} already requested.")
        if smart:
            start = sorted(spec)[0] if isinstance(spec, list) and spec and spec[0] in free else (free[0] if free else None)
            if start is None:
                return {**base, "done": "none", "text": " ".join(notes) or f"Nothing left to request for {title}.",
                        "notes": notes}
            seasons, mode = [start], "smart"
        else:
            if not free:
                return {**base, "done": "none", "text": (" ".join(notes) + f" Nothing left to request for {title}.").strip(),
                        "notes": notes}
            seasons = free
            if mode == "all" and (here or coming):
                mode = "pick"  # "all" would ask Seerr for seasons it already has
        if wanted_label and not here and not coming:
            notes.append("None of it is here or requested yet, so that's every season.")
    opts = storage.request_options(app, kind, int(tmdb), seasons if kind == "tv" else "all")
    srv, prof, qnote = _pick_quality(opts, quality, anime)
    if qnote:
        notes.append(qnote)
    est = (prof or {}).get("estimate")
    if not est:
        e = storage.estimate(app, kind, int(tmdb), (prof or {}).get("id"), seasons if kind == "tv" else "all",
                             (srv or {}).get("id"), (srv or {}).get("is4k"))
        est = e["estimate"]
    root = (opts.get("choice") or {}).get("root") if srv and srv["id"] == (opts.get("choice") or {}).get("server") \
        else (srv or {}).get("default_root")
    fit = storage.fit_check(app, est, root, opts.get("storage"))
    body = {"type": kind, "id": int(tmdb), "title": title, "anime": anime, "remember": False}
    if srv:
        body.update(server=srv["id"], is4k=bool(srv["is4k"]), root=root)
    if prof:
        body["profile"] = prof["id"]
    if kind == "tv":
        body.update(seasons=seasons, season_mode=mode)
    try:
        dry = storage.send_request(app, dict(body), dry_run=True)
        payload = dry.get("payload")
    except PermissionError as e:
        payload = None
        fit = {**fit, "blocked": True, "message": str(e), "level": "bad"}
    size = (est or {}).get("text") or (("≈ " + storage.fmt_size(est["typical"])) if est and est.get("typical") else None)
    typical = (est or {}).get("typical")
    qlabel = (prof or {}).get("label") or "Default quality"
    parts = [f"{title}" + (f" ({year})" if year else "")]
    if kind == "tv":
        parts.append(_fmt_seasons(seasons) + (" now, more as you watch" if mode == "smart" else ""))
    parts.append(re.sub(r"\s*\(upgrades over time\)", "", qlabel))
    if typical:
        parts.append("≈ " + storage.fmt_size(typical))
    return {**base, "done": None, "line": " · ".join(parts), "size_text": size, "typical": typical,
            "quality": qlabel, "server": srv and srv.get("name"), "seasons": seasons, "season_mode": mode,
            "notes": notes, "fit": {k: fit.get(k) for k in ("level", "message", "blocked")}, "body": body,
            "payload": payload, "explain": opts.get("explain"),
            "smart_note": "The next season is requested once you're partway through this one." if mode == "smart" else None}


# ------------------------------------------------------------------ answers
def _say(text):
    return {"kind": "delta", "text": text}


def _status(text):
    return {"kind": "status", "text": text}


class Reply:
    """Collects what one answer showed, for the page now and for the history file."""

    def __init__(self):
        self.text, self.cards, self.actions, self.subject = "", [], [], None
        self.used_llm = False

    def say(self, text):
        piece = (" " if self.text and not self.text.endswith(("\n", " ")) else "") + text
        self.text += piece
        return _say(piece)

    def card(self, card):
        card.setdefault("id", f"a{int(time.time() * 1000) % 10**10}{len(self.actions)}")
        self.actions.append(card)
        return {"kind": "action", "card": card}

    def titles(self, cards):
        self.cards += cards
        return {"kind": "cards", "items": cards}


def run(intent, message, hist, pick=None, llm=False):
    """The answer to an action intent, as chat events. Saves the exchange to the chat history."""
    from . import chat
    now = time.time()
    rep = Reply()
    try:
        for ev in _dispatch(intent, rep, hist, pick, llm):
            yield ev
    except Exception as e:
        print("chat action failed:", type(e).__name__, e)
        yield rep.say(f"Something went wrong doing that ({type(e).__name__}). Nothing was changed.")
    if not rep.text:
        yield rep.say("Done.")
    engine = chat.engine_label() if intent.get("by") == "model" or rep.used_llm else "Atlas (rules)"
    chat._append({"role": "user", "text": message, "at": now},
                 {"role": "assistant", "text": rep.text, "cards": rep.cards, "actions": rep.actions,
                  "subject": rep.subject, "intent": {k: v for k, v in intent.items() if k != "by"},
                  "engine": engine, "at": time.time()})
    yield {"kind": "done", "engine": engine}


def _last_subject(hist):
    for m in reversed(hist or []):
        if m.get("role") == "assistant" and m.get("subject"):
            return m["subject"]
    return None


def _dispatch(intent, rep, hist, pick, llm):
    kind = intent["intent"]
    if kind == "stack":
        yield from _stack_answer(intent, rep, llm)
        return
    if kind == "downloads":
        yield from _downloads_answer(rep)
        return
    if not intent.get("title") and not pick:
        last = _last_subject(hist)
        if last:
            pick = {"library": last["id"]} if last["source"] == "library" else {"type": last["type"], "id": last["tmdb"]}
        else:
            yield rep.say("Which title do you mean?")
            return
    if kind in ("eta", "doctor"):
        yield from _doctor_answer(intent, rep, pick)
        return
    yield _status("Finding it…")
    how, what = resolve_for(intent, pick)
    if how == "choose":
        yield rep.say(what["prompt"])
        yield rep.card({"type": "choose", "options": what["options"],
                        "intent": {k: v for k, v in intent.items() if k != "by"}})
        return
    if how == "none":
        if isinstance(what, dict):
            yield rep.say(what["text"])
            c = what.get("tmdb")
            if c:
                yield rep.titles([chat_card(c)])
                if kind == "play":
                    yield rep.card({"type": "buttons", "buttons": [
                        {"label": "Request it…", "do": "request-sheet", "key": f"{c['type']}:{c['id']}", "icon": "plus"}]})
        else:
            if intent.get("soft") and kind in ("open", "request"):
                yield rep.say(what + " Try the exact title, or ask me for ideas instead.")
            else:
                yield rep.say(what)
        return
    rep.subject = _subject(how, what)
    if kind == "open":
        yield from _open(rep, how, what)
    elif kind == "play":
        yield from _play(intent, rep, how, what)
    elif kind == "request":
        yield from _request(intent, rep, how, what)
    elif kind == "mylist":
        yield from _mylist(intent, rep, how, what)
    elif kind == "watched":
        yield from _watched(intent, rep, how, what)
    elif kind == "delete":
        yield from _delete(rep, how, what)


def chat_card(c):
    from . import chat
    return chat.seerr_card(c)


def _open(rep, how, what):
    card = _card_of(how, what)
    if how == "library":
        yield rep.say(f"Here's {what.get('name')}.")
        yield {"kind": "ui", "do": "open", "id": what["id"]}
    else:
        yield rep.say(f"Here's {what.get('title')}" + (f" ({what['year']})" if what.get("year") else "") + ".")
        yield {"kind": "ui", "do": "tmdb", "key": f"{what['type']}:{what['id']}"}
    yield rep.titles([card])


def _play(intent, rep, how, r):
    cat = _cat()
    name = r.get("name")
    if r["type"] == "Series":
        ep = None
        if intent.get("season") and intent.get("episode"):
            ep = next((e for e in cat.eps.get(r["id"], []) if e.get("season_no") == intent["season"]
                       and e.get("episode_no") == intent["episode"]), None)
            if not ep:
                yield rep.say(f"I can't find season {intent['season']}, episode {intent['episode']} of {name} in your library.")
                yield rep.titles([_card_of(how, r)])
                return
        else:
            ep = cat.next_up_for(r["id"])
        if not ep:
            yield rep.say(f"You've watched every episode of {name} that's here.")
            yield rep.titles([_card_of(how, r)])
            yield rep.card({"type": "buttons", "buttons": [{"label": "Open it", "do": "open", "id": r["id"], "icon": "info"}]})
            return
        info = cat.lookup(ep["id"]) or {}
        code = f"S{ep.get('season_no') or 0:02d}E{ep.get('episode_no') or 0:02d}"
        pos = info.get("position") or 0
        resume = f", from {int(pos // 60)}:{int(pos % 60):02d}" if pos > 30 else ""
        yield rep.say(f"Playing {name} {code} · {ep.get('name')}{resume}.")
        yield {"kind": "ui", "do": "play", "id": ep["id"], "label": f"{name} {code}"}
        yield rep.titles([_card_of(how, r)])
        return
    info = cat.lookup(r["id"]) or {}
    pos = info.get("position") or 0
    resume = f" from {int(pos // 3600)}:{int(pos % 3600 // 60):02d}:{int(pos % 60):02d}" if pos > 30 else ""
    yield rep.say(f"Playing {name}{resume}.")
    yield {"kind": "ui", "do": "play", "id": r["id"], "label": name}
    yield rep.titles([_card_of(how, r)])


def _tmdb_of(how, what):
    if how == "library":
        tmdb = (what.get("provider") or {}).get("Tmdb")
        return ("tv" if what["type"] == "Series" else "movie"), (int(tmdb) if str(tmdb or "").isdigit() else None)
    return what["type"], int(what["id"])


def _request(intent, rep, how, what):
    app = _app()
    if not app.seerr.ready:
        yield rep.say("Requests go through Seerr, and it isn't connected. Add it in Settings.")
        return
    kind, tmdb = _tmdb_of(how, what)
    if not tmdb:
        yield rep.say(f"I can't request {what.get('name')}: it has no TMDB id in your library.")
        return
    if intent.get("seasons") is not None and kind == "movie":
        yield rep.say(f"{what.get('title') or what.get('name')} is a film, so there are no seasons. Here it is as a whole.")
    yield _status("Working out the size…")
    r = build_request(kind, tmdb, intent)
    if r.get("done"):
        yield rep.say(r["text"])
        btns = []
        if r["done"] == "available":
            owned = _cat().by_tmdb(kind, tmdb)
            if owned:
                btns.append({"label": "Play", "do": "play-item", "id": owned["id"], "icon": "play"})
                btns.append({"label": "Open", "do": "open", "id": owned["id"], "icon": "info"})
        elif r["done"] == "requested":
            btns.append({"label": "Why isn't it here yet?", "do": "ask", "q": f"Why isn't {r['title']} here yet?"})
        else:
            btns.append({"label": "Open the request sheet", "do": "request-sheet", "key": r["key"], "icon": "plus"})
        if btns:
            yield rep.card({"type": "buttons", "buttons": btns})
        return
    say = "Here's the request. Nothing is sent until you tap Request."
    if r["fit"].get("blocked"):
        say = "This one is over your storage limit, so it can't be sent as it is."
    yield rep.say(say)
    yield rep.card({**r, "media": r["type"], "type": "request"})


def _mylist(intent, rep, how, what):
    cat = _cat()
    if how == "library":
        key = cat.mylist_key(item_id=what["id"])
        data = {}
        name = what.get("name")
    else:
        key = cat.mylist_key(kind=what["type"], tmdb=what["id"])
        data = {k: what.get(k) for k in ("title", "year", "poster", "backdrop", "overview", "status", "anime")
                if what.get(k) is not None}
        name = what.get("title")
    have = key in set(cat.mylist_keys())
    if intent.get("on", True):
        if have:
            yield rep.say(f"{name} is already on My List.")
        else:
            cat.mylist_add(key, data)
            yield rep.say(f"Added {name} to My List.")
            yield rep.card({"type": "mylist", "key": key, "title": name, "on": True})
    else:
        if not have:
            yield rep.say(f"{name} isn't on My List.")
        else:
            cat.mylist_remove(key)
            yield rep.say(f"Took {name} off My List.")
            yield rep.card({"type": "mylist", "key": key, "title": name, "on": False, "data": data})
    yield {"kind": "ui", "do": "mylist-changed"}
    yield rep.titles([_card_of(how, what)])


def _watched(intent, rep, how, r):
    """A confirmation card: library titles are marked here and in Jellyfin; titles that aren't here are kept as
    watched on this PC (POST /api/watched), and the taste profile learns from either."""
    on = intent.get("on", True)
    cat = _cat()
    seasons = intent.get("seasons") if isinstance(intent.get("seasons"), list) else None
    if how == "tmdb":
        owned = cat.by_tmdb(r["type"], r["id"])
        if owned:
            how, r = "library", owned
            rep.subject = _subject(how, r)
    if how == "library" and seasons and r["type"] == "Series":
        eps = [e for e in cat.eps.get(r["id"], []) if e.get("season_no") in set(seasons)]
        tmdb = (r.get("provider") or {}).get("Tmdb")
        if not eps and str(tmdb or "").isdigit():  # those seasons aren't here: remember them as watched instead
            how, r = "tmdb", {"type": "tv", "id": int(tmdb), "title": r.get("name"), "year": r.get("year"),
                              "poster": cat.card(r).get("poster")}
    sn = _fmt_seasons(seasons) if seasons else ""
    if how == "library":
        if r["type"] != "Series":
            seasons = None
        name = r.get("name")
        if seasons:
            eps = [e for e in cat.eps.get(r["id"], []) if e.get("season_no") in set(seasons)]
            played = bool(eps) and all((cat.ud.get(e["id"]) or {}).get("played") for e in eps)
            what = f"{sn} of {name}"
        else:
            played = cat._played(r)
            what = "every episode of " + name if r["type"] == "Series" else name
        if played == on:
            yield rep.say(f"{what[0].upper() + what[1:]} is already marked {'watched' if on else 'unwatched'}.")
            return
        yield rep.say(f"Mark {what} as {'watched' if on else 'unwatched'}? Tap to confirm.")
        yield rep.card({"type": "watched", "item_id": r["id"], "title": name, "on": on,
                        "poster": cat.card(r).get("poster"), "series": r["type"] == "Series", "seasons": seasons,
                        "season_text": sn or None})
        return
    kind, tmdb = r["type"], int(r["id"])
    if kind != "tv":
        seasons, sn = None, ""
    name = r.get("title") or r.get("name") or "it"
    label = name + (f" ({r['year']})" if r.get("year") else "")
    st = cat.watched_state(kind, tmdb) or {}
    gone = cat.removed_flag(kind, tmdb) or {}
    played = bool(st.get("whole") or gone.get("played") or (seasons and set(seasons) <= set(st.get("seasons") or [])))
    what = f"{sn} of {label}" if seasons else label
    if on and played:
        yield rep.say(f"{what[0].upper() + what[1:]} is already marked watched.")
        yield rep.titles([_card_of("tmdb", r)])
        return
    if not on and not (st or gone.get("played")):
        yield rep.say(f"{label} isn't marked watched.")
        return
    if on:
        yield rep.say(f"Mark {what} as watched? It isn't in your library, so Atlas keeps it as watched on this PC "
                      f"and your taste profile learns from it. Tap to confirm.")
    else:
        yield rep.say(f"Mark {what} as unwatched? Tap to confirm.")
    yield rep.card({"type": "watched", "tmdb": tmdb, "kind": kind, "title": label, "on": on,
                    "poster": _poster(r.get("poster")), "series": kind == "tv", "seasons": seasons,
                    "season_text": sn or None, "owned": False})
    yield rep.titles([_card_of("tmdb", r)])


def _delete(rep, how, r):
    cat = _cat()
    info = cat.lookup(r["id"]) or {}
    if r["type"] == "Movie" and not info.get("exists") and r.get("source") == "local":
        yield rep.say(f"{r.get('name')} has no file on this PC to delete.")
        return
    yield rep.say(f"Opening the delete sheet for {r.get('name')}. Hold the button there to delete; nothing goes until you do.")
    yield {"kind": "ui", "do": "delete", "id": r["id"], "name": r.get("name"), "type": r["type"]}
    yield rep.card({"type": "buttons", "buttons": [{"label": "Delete from this PC…", "do": "delete", "id": r["id"],
                                                    "name": r.get("name"), "type_": r["type"], "icon": "close",
                                                    "danger": True}]})


# ------------------------------------------------------------------ downloads, readiness and the request doctor
def _snap():
    from . import stack
    return stack.snapshot(_app())


def _dl_items(snap, words=None):
    dl = snap.get("downloads") or {}
    items, by_title = [], {}
    for q in dl.get("queue") or []:
        secs = _timeleft(q.get("timeleft"))
        if q.get("title") in by_title:  # a season pack shows once per episode in Sonarr's queue
            by_title[q["title"]]["count"] += 1
            continue
        it = {"title": q.get("title"), "progress": q.get("progress") or 0, "eta": _eta(secs), "count": 1,
              "status": q.get("status"), "warning": q.get("warning"), "app": q.get("app")}
        by_title[q.get("title")] = it
        items.append(it)
    seen = {_n(i["title"]) for i in items}
    for t in dl.get("torrents") or []:
        if t.get("progress", 0) >= 100 or _n(t.get("name")) in seen:
            continue
        items.append({"title": t.get("name"), "progress": t.get("progress") or 0, "eta": _eta(t.get("eta")),
                      "status": t.get("state"), "warning": None, "app": "qbittorrent",
                      "stalled": t.get("state") in ("stalledDL", "metaDL") and not t.get("dlspeed")})
    if words:
        items = [i for i in items if all(w in _n(i["title"]) for w in words)]
    items.sort(key=lambda i: (-(i["progress"] or 0)))
    return items


def _n(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _downloads_answer(rep):
    yield _status("Checking the downloads…")
    snap = _snap()
    items = _dl_items(snap)
    dl = snap.get("downloads") or {}
    if not items:
        msg = "Nothing is downloading right now."
        if dl.get("connection") == "disconnected":
            msg += " qBittorrent says it has no network connection, though."
        yield rep.say(msg)
        return
    stalled = [i for i in items if i.get("stalled") or i.get("warning")]
    text = f"{len(items)} download{'s' if len(items) != 1 else ''} on the way"
    if dl.get("summary"):
        text += f" ({dl['summary'].split('·', 1)[-1].strip()})" if "·" in dl["summary"] else ""
    text += "."
    if stalled:
        text += f" {len(stalled)} {'is' if len(stalled) == 1 else 'are'} stalled or need attention."
    yield rep.say(text)
    yield rep.card({"type": "downloads", "items": items[:12], "more": max(0, len(items) - 12),
                    "source": snap.get("source")})


def _doctor_answer(intent, rep, pick):
    yield _status("Looking for it…")
    cat = _cat()
    how, what = resolve_for({**intent, "intent": "open"}, pick)
    if how == "choose":
        yield rep.say(what["prompt"])
        yield rep.card({"type": "choose", "options": what["options"], "intent": intent})
        return
    if how == "none":
        yield rep.say(what if isinstance(what, str) else what["text"])
        return
    rep.subject = _subject(how, what)
    kind, tmdb = _tmdb_of(how, what)
    title = what.get("name") or what.get("title")
    status = None
    try:
        d = _app().seerr.call("GET", f"/{kind}/{tmdb}", timeout=10) if tmdb and _app().seerr.ready else {}
        status = ((d or {}).get("mediaInfo") or {}).get("status")
    except Exception:
        d = {}
    owned = how == "library" or (cat and tmdb and cat.by_tmdb(kind, tmdb))
    snap = _snap()
    doc = None
    for i in (snap.get("doctor") or {}).get("items") or []:
        if tmdb and str(i.get("tmdb")) == str(tmdb) and i.get("type") == kind:
            doc = i
    queue = _dl_items(snap, _n(title).split())
    lines = []
    if intent["intent"] == "eta" and queue:
        best = queue[0]
        lines.append(f"{title}: {len(queue)} download{'s' if len(queue) != 1 else ''} going, the furthest "
                     f"{best['progress']:.0f}% done" + (f", {best['eta']} left" if best.get("eta") else "") + ".")
        if any(q.get("stalled") for q in queue):
            lines.append("Some of them are stalled (nobody is sharing them right now).")
    if doc:
        who = "the Media Hub's request doctor" if snap.get("source") == "hub" and not (snap.get("doctor") or {}).get("local") \
            else "what Atlas can see"
        lines.append(f"According to {who} ({_ago((snap.get('doctor') or {}).get('time'))}): {doc.get('reason') or doc.get('state')}.")
        for f in doc.get("fixed") or []:
            lines.append(f"It already fixed something: {f}.")
    elif status == 5 or (owned and status in (None, 5)):
        if status == 5 or how == "library":
            lines.append(f"{title} is in your library" + (" already." if not queue else ", and more of it is downloading."))
    elif status in (2, 3, 4):
        lines.append(f"{title} is requested" + (" and partly here" if status == 4 else "") + ", but the request "
                     f"doctor hasn't reported on it yet" + (f" (its last run was {_ago((snap.get('doctor') or {}).get('time'))})"
                                                             if snap.get("doctor") else "") + ".")
    elif status == 1 or status is None:
        if not queue:
            lines.append(f"{title} hasn't been requested.")
    if not lines:
        lines.append(f"I don't have anything on {title} from the queue or the request doctor.")
    yield rep.say(" ".join(lines))
    btns = []
    if snap.get("source") == "hub" and (doc or status in (2, 3, 4)):
        btns.append({"label": "Check every request again", "do": "stack-action", "action": "requests", "arg": "",
                     "icon": "restart"})
    if status in (None, 1) and not owned and not queue and tmdb:
        btns.append({"label": "Request it…", "do": "request-sheet", "key": f"{kind}:{tmdb}", "icon": "plus"})
    if owned and how == "library":
        btns.append({"label": "Play", "do": "play-item", "id": what["id"], "icon": "play"})
    if queue:
        yield rep.card({"type": "downloads", "items": queue[:6], "more": max(0, len(queue) - 6),
                        "source": snap.get("source")})
    if doc:
        yield rep.card({"type": "doctor", "title": title, "doc_state": doc.get("state"), "reason": doc.get("reason"),
                        "fixed": doc.get("fixed") or [], "time": (snap.get("doctor") or {}).get("time"),
                        "buttons": btns})
    elif btns:
        yield rep.card({"type": "buttons", "buttons": btns})
    yield rep.titles([_card_of(how, what)])


# ------------------------------------------------------------------ "is everything ok", "why is X not working"
def _facts(snap, intent):
    """The checks' own findings for this question, as (lines for the model / template, problems with fixes)."""
    topic = intent.get("topic")
    svc = intent.get("service")
    apps = snap.get("apps") or []
    sysd = snap.get("system") or {}
    lines, probs = [], []
    if topic == "vpn":
        vc = sysd.get("vpncheck")
        if sysd.get("vpn") is True:
            lines.append("VPN adapter: connected.")
        elif sysd.get("vpn") is False:
            lines.append("VPN adapter: DISCONNECTED (torrents pause until it connects).")
        else:
            lines.append("VPN adapter: not checked here.")
        if vc:
            lines.append(f"VPN leak check ({_ago(vc.get('time'))}): {vc.get('status')} - {vc.get('text')}")
        q = next((a for a in apps if a["id"] == "qbittorrent"), None)
        if q:
            lines.append(f"qBittorrent: {q['status']} - {q.get('summary') or ''}")
        probs = [p for p in snap.get("problems") or [] if "vpn" in p["text"].lower() or p["app"] in ("gluetun", "qbittorrent")]
        return lines, probs
    if topic == "downloads":
        dl = snap.get("downloads") or {}
        items = _dl_items(snap)
        lines.append(f"Downloads: {len(items)} active. qBittorrent connection: {dl.get('connection') or 'unknown'}.")
        stalled = [i for i in items if i.get("stalled")]
        warn = [i for i in items if i.get("warning")]
        if stalled:
            lines.append(f"{len(stalled)} stalled (no one sharing / no speed): " + "; ".join(i["title"] for i in stalled[:5]))
        for i in warn[:5]:
            lines.append(f"Queue warning: {i['title']}: {i['warning']}")
        if sysd.get("vpn") is False:
            lines.append("The VPN is disconnected, so torrents are paused.")
        for a in apps:
            if a["id"] in ("prowlarr", "sonarr", "radarr", "qbittorrent", "trawl", "byparr") and a["status"] != "ok":
                lines.append(f"{a['name']}: {a['status']} - {a.get('summary') or ''}")
        probs = [p for p in snap.get("problems") or [] if p["app"] in ("qbittorrent", "prowlarr", "sonarr", "radarr",
                                                                        "trawl", "byparr", "gluetun", "mediasorter")]
        return lines, probs
    if topic == "remote":
        a = next((x for x in apps if x["id"] == (svc or "jellyfin")), None)
        if a:
            lines.append(f"{a['name']} on the server itself: {a['status']} - {a.get('summary') or ''}")
        addr = snap.get("addresses") or {}
        ts = addr.get("tailscale") or {}
        if ts:
            lines.append(f"Tailscale on the server: {'online' if ts.get('online') else 'offline' if ts.get('online') is False else 'unknown'}"
                         + (f", name {ts['name']}" if ts.get("name") else ""))
            for s in addr.get("services") or []:
                if s.get("name", "").lower().startswith((svc or "jellyfin")[:5]):
                    if s.get("anywhere"):
                        lines.append(f"Address for {s['name']} away from home: {s['anywhere']}")
                    if s.get("home"):
                        lines.append(f"Address at home: {s['home']}")
        else:
            lines.append("Remote access (Tailscale) status: not reported" + (" by this hub." if snap.get("hub") else
                                                                             " (Atlas can't check it without the Media Hub)."))
        probs = [p for p in snap.get("problems") or [] if p["app"] == (svc or "jellyfin")]
        return lines, probs
    if topic == "service" and svc and svc not in ("all", "hub"):
        a = next((x for x in apps if x["id"] == svc), None)
        if not a:
            lines.append(f"{svc}: not part of the checks Atlas has (not installed, or not known here).")
            return lines, []
        lines.append(f"{a['name']}: status {a['status']}" + (f", version {a['version']}" if a.get("version") else "") +
                     (f", answered in {a['ms']} ms" if a.get("ms") is not None else "") + f". {a.get('summary') or ''}")
        for p in a.get("problems") or []:
            lines.append(f"{a['name']} reports ({p['level']}): {p['text']}")
        if not a.get("problems"):
            lines.append(f"{a['name']} reports no problems.")
        probs = [{**p, "app": a["id"], "name": a["name"]} for p in a.get("problems") or []]
        if a.get("status") == "down" and not any(p.get("fix") for p in probs):
            probs += [{"level": "error", "text": f"{a['name']} is down", "app": a["id"], "name": a["name"], "fix": f}
                      for f in a.get("fixes") or []]
        if svc == "prowlarr" or any("indexer" in p["text"].lower() for p in a.get("problems") or []):
            pass
        return lines, probs
    if svc == "hub":
        h = snap.get("hub")
        lines.append(f"Media Hub: {'answering at ' + h['url'] if h else 'not found'}." +
                     (f" {snap['hub_error']}" if snap.get("hub_error") else ""))
        if h and h.get("watchdog"):
            w = h["watchdog"]
            lines.append(f"Watchdog: {'on' if w.get('on') else 'off'}, last check {_ago(w.get('checked'))}.")
        return lines, []
    s = snap.get("summary") or {}
    lines.append(f"Overall: {s.get('text')} ({s.get('apps')} apps checked; {s.get('errors')} errors, "
                 f"{s.get('warnings')} warnings).")
    for p in (snap.get("problems") or [])[:10]:
        lines.append(f"{p['name']} ({p['level']}): {p['text']}")
    d = sysd.get("disk")
    if d:
        lines.append(f"Disk: {d.get('free_gb')} GB free" + (f" of {d['total_gb']} GB" if d.get("total_gb") else "") + ".")
    if sysd.get("vpn") is not None:
        lines.append(f"VPN: {'connected' if sysd['vpn'] else 'disconnected'}.")
    vc = sysd.get("vpncheck")
    if vc:
        lines.append(f"VPN leak check: {vc.get('status')}.")
    h = snap.get("health")
    if h:
        lines.append(f"Last hourly health check ({_ago(h.get('time'))}): " +
                     (h.get("title") or f"{len(h.get('attention') or [])} need attention, {len(h.get('fixed') or [])} fixed"))
    doc = snap.get("doctor")
    if doc and doc.get("counts"):
        lines.append("Requests not here yet: " + ", ".join(f"{n} {k}" for k, n in doc["counts"].items()) + ".")
    return lines, [p for p in snap.get("problems") or [] if p["level"] in ("error", "warn")]


STACK_SYSTEM = """You are Atlas, a home media app, explaining the state of the person's media server.
Use ONLY the facts given (they come from the server's own health checks). Never guess a status that isn't listed;
if something wasn't checked, say so. Explain in plain, friendly words what is wrong (if anything), why it matters
for watching or downloading, and what they can do. Mention a Fix button only when one is listed (never mention that there is none). No markdown headings,
no tables, no emojis; at most 110 words."""


def _sentences(lines):
    return " ".join(x.strip() if x.strip().endswith((".", "!", "?")) else x.strip() + "." for x in lines if x and x.strip())


def _template(intent, snap, lines, probs):
    """Classic: the checks' own messages, lightly worded."""
    topic = intent.get("topic")
    s = snap.get("summary") or {}
    if topic == "all":
        if s.get("level") == "ok":
            out = f"Everything's running: {s.get('apps')} apps checked, no problems reported."
        elif s.get("level") == "unknown":
            out = "I don't know any part of your stack yet. Point Atlas at the Media Hub in Your stack."
        else:
            out = s.get("text", "") + "."
            names = list(dict.fromkeys(p["name"] for p in probs))
            if names:
                out += f" {', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else names[0]} reported " \
                       f"{'them' if len(probs) > 1 else 'it'}; the details are below."
        h = snap.get("health")
        if h and h.get("title"):
            out += f" The last health check ({_ago(h.get('time'))}) said: {h['title']}."
        return out
    if topic == "service":
        return _sentences(lines[:4])
    if topic == "vpn":
        return _sentences(lines[:3])
    if topic == "downloads":
        return _sentences(lines[:5]) + (" Nothing in the checks says downloads are stuck." if len(lines) <= 1 else "")
    if topic == "remote":
        tip = (" From your phone, use the 'away from home' address with Tailscale switched on; at home, the home "
               "address works without it.")
        return _sentences(lines[:4]) + tip
    return _sentences(lines[:5])


def _stack_answer(intent, rep, llm):
    from . import chat
    yield _status("Checking your stack…")
    snap = _snap()
    lines, probs = _facts(snap, intent)
    src = ("from the Media Hub's checks" if snap.get("source") == "hub" else
           "from Atlas's own read-only checks (no Media Hub found)")
    fixes = []
    seen = set()
    for p in probs:
        f = p.get("fix")
        if f and (f["action"], f.get("arg")) not in seen:
            seen.add((f["action"], f.get("arg")))
            fixes.append({"label": f["label"], "do": "stack-action", "action": f["action"], "arg": f.get("arg") or "",
                          "confirm": bool(f.get("confirm")), "icon": "restart"})
    if intent.get("restart"):
        a = next((x for x in snap.get("apps") or [] if x["id"] == intent.get("service")), None)
        if a and snap.get("source") == "hub" and ("restart", a["id"]) not in seen:
            fixes.insert(0, {"label": f"Restart {a['name']}", "do": "stack-action", "action": "restart", "arg": a["id"],
                             "confirm": True, "icon": "restart"})
    text = None
    if llm and not chat.playing():
        facts = "\n".join(lines) + ("\nFix buttons available: " + ", ".join(f["label"] for f in fixes) if fixes else "")
        q = {"all": "Is everything OK?", "vpn": "Is the VPN on and working?", "downloads": "Why are downloads stuck?",
             "remote": f"Why can't I reach {intent.get('service') or 'Jellyfin'} from my phone?",
             "service": f"Why isn't {intent.get('service')} working?"}.get(intent.get("topic"), "How is my server?")
        msgs = [{"role": "system", "content": STACK_SYSTEM},
                {"role": "user", "content": f"Facts ({src}):\n{facts}\n\nQuestion: {q}"}]
        got, shown = "", ""
        try:
            for piece in chat._stream_llm(msgs, timeout=60):
                got += piece
                v = chat.visible(got)
                if v.startswith(shown) and len(v) > len(shown):
                    yield _say(v[len(shown):])
                    shown = v
        except Exception as e:
            print("chat: stack explanation failed:", type(e).__name__, e)
        text = chat.visible(got).strip()
        if text:
            rep.text = text
            rep.used_llm = True
            if shown.strip() != text:
                yield {"kind": "replace", "text": text}
        elif shown:
            yield {"kind": "replace", "text": ""}
    if not text:
        rep.text = ""
        yield rep.say(_template(intent, snap, lines, probs))
    card = {"type": "stack", "level": (snap.get("summary") or {}).get("level"), "source": snap.get("source"),
            "summary": (snap.get("summary") or {}).get("text"), "checked": snap.get("checked"),
            "problems": [{"level": p["level"], "name": p.get("name"), "text": p["text"]} for p in probs[:8]],
            "facts": lines[:8] if intent.get("topic") != "all" else [], "buttons": fixes[:4], "via": src}
    yield rep.card(card)


def plan(message, hist, pick=None, intent=None, llm=False):
    """The action intent for this message, or None for an ordinary recommendation chat."""
    if intent and isinstance(intent, dict) and intent.get("intent") in KINDS:
        return {k: v for k, v in intent.items() if k in ("intent", "title", "year", "kind", "seasons", "smart",
                                                           "quality", "soft", "next", "season", "episode", "on",
                                                           "topic", "service")}
    got = parse(message)
    if got:
        return got
    if llm:
        return model_intent(message)
    return None
