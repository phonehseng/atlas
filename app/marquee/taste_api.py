"""Routes for the taste profile, the "Is this for you?" fit comment, the Launchpad, Letterboxd import and the
Ask Atlas chat (a Flask blueprint on the local UI server). Register with:

    from .taste_api import bp as taste_bp; app.register_blueprint(taste_bp)

Registering also starts the small background job that learns from finished plays (marquee.taste.start_background)."""
import json
import re
import threading
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from . import ai, chat, letterboxd, settings, taste

bp = Blueprint("marquee_taste", __name__)
_fit_mem = {}
_fit_locks = {}
_fit_guard = threading.Lock()


@bp.record_once
def _start(state):
    taste.start_background()


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


def _body():
    return request.get_json(force=True, silent=True) or {}


def _ai_info():
    a = ai.available()
    return {"llm": bool(a.get("llm")), "name": a.get("name"), "mode": a.get("mode"), "classic": not a.get("llm")}


# ------------------------------------------------------------------ profile
@bp.get("/api/taste")
def taste_view():
    return ok(profile=taste.view(), ai=_ai_info())


@bp.get("/api/taste/state")
def taste_state():
    s = settings.load()
    need = not s.get("launchpad_done") and not (taste.exists() and taste.profile().get("onboarded"))
    return ok(launchpad=need, exists=taste.exists(), onboarded=taste.profile().get("onboarded", False),
              learning=taste.learning_on(), rev=taste.rev(), ai=_ai_info(), chips=list(taste.CHIPS),
              vibes=["Feel-good", "Mind-bending", "Dark", "Cosy", "Epic", "Short episodes", "Binge-able"])


@bp.post("/api/taste/update")
def taste_update():
    return ok(result=taste.update_from_history())


@bp.post("/api/taste/learning")
def taste_learning():
    on = bool(_body().get("on", True))
    settings.save({"taste_learning": on})
    return ok(learning=on)


@bp.post("/api/taste/reset")
def taste_reset():
    taste.reset()
    _fit_mem.clear()
    return ok(profile=taste.view())


@bp.post("/api/taste/reset/undo")
def taste_reset_undo():
    if not taste.undo_reset():
        return fail("Nothing to undo", 404)
    _fit_mem.clear()
    return ok(profile=taste.view())


# ------------------------------------------------------------------ memory notes
@bp.post("/api/taste/notes")
def note_add():
    b = _body()
    try:
        note = taste.add_note(b.get("text", ""), source=b.get("source") if b.get("source") in ("manual", "chat") else "manual")
    except ValueError as e:
        return fail(e)
    return ok(note=note)


@bp.post("/api/taste/notes/restore")
def note_restore():
    note = _body().get("note") or {}
    if not note.get("id") or not note.get("text"):
        return fail("Nothing to restore")
    return ok(note=taste.restore_note(note))


@bp.route("/api/taste/notes/<note_id>", methods=["PATCH", "PUT"])
def note_edit(note_id):
    b = _body()
    try:
        return ok(note=taste.edit_note(note_id, b.get("text"), b.get("confidence")))
    except KeyError as e:
        return fail(e, 404)


@bp.delete("/api/taste/notes/<note_id>")
def note_delete(note_id):
    note = next((n for n in taste.profile()["notes"] if n["id"] == note_id), None)
    if not note:
        return fail("No such note", 404)
    taste.delete_note(note_id)
    return ok(note=note)


# ------------------------------------------------------------------ Launchpad (first run)
CHIP_OF_GENRE = {"Science Fiction": "Sci-fi", "Music": "Musicals", "Western": "Westerns"}


def _chips_from(parsed):
    likes, dislikes, vibes = [], [], []
    for g, v in parsed.get("genres", {}).items():
        chip = CHIP_OF_GENRE.get(g, g)
        if chip in taste.CHIPS:
            (likes if v > 0 else dislikes).append(chip)
    if parsed.get("anime"):
        (likes if parsed["anime"] > 0 else dislikes).append("Anime")
    for t, v in parsed.get("themes", {}).items():
        if t in taste.CHIPS:
            (likes if v > 0 else dislikes).append(t)
    if parsed.get("languages", {}).get("ko"):
        (likes if parsed["languages"]["ko"] > 0 else dislikes).append("K-drama")
    for m, v in parsed.get("moods", {}).items():
        if v > 0 and m in ("Feel-good", "Mind-bending", "Dark", "Cosy", "Epic", "Short episodes", "Binge-able"):
            vibes.append(m)
    return {"likes": list(dict.fromkeys(likes)), "dislikes": list(dict.fromkeys(dislikes)), "vibes": vibes}


@bp.post("/api/taste/onboard/chat")
def onboard_chat():
    """A free-text Launchpad answer: the chips it implies, plus a short friendly reply (AI) or an honest one (rules)."""
    b = _body()
    text = (b.get("text") or "").strip()[:600]
    turn = int(b.get("turn") or 1)
    step = b.get("step") or "likes"
    picked = _chips_from(taste.parse_text(text))
    if step == "dislikes":  # "Anything you can't stand?" -> everything named is a dislike
        allp = taste.parse_text(text)
        picked["dislikes"] = list(dict.fromkeys(picked["dislikes"] + picked["likes"] + [
            c for c in _chips_from({k: ({n: -abs(v) for n, v in val.items()} if isinstance(val, dict) else -abs(val or 0))
                                    for k, val in allp.items()})["dislikes"]]))
        picked["likes"] = []
    reply, question = None, None
    if not chat.classic() and not chat.playing():
        try:
            out = ai.chat(
                f"Step: {'what they love to watch' if step != 'dislikes' else 'what they cannot stand'}. "
                f"Turn {turn} of 3. Chips they ticked so far: {', '.join(b.get('picks') or []) or 'none'}.\n"
                f"They wrote: {text}",
                "You are Atlas, a movie app meeting a new viewer to learn their taste. Reply with JSON only: "
                '{"reply": "one warm, specific sentence reacting to what they said (max 22 words)", '
                '"question": "one short follow-up question to learn more (max 14 words), or empty if turn 3"}. '
                "If they are listing things they dislike, just acknowledge you'll steer clear of them (never praise "
                "a dislike) and ask what they'd rather see instead. No emojis. Don't recommend titles yet.", max_tokens=120, timeout=25, temperature=.6)
            m = re.search(r"\{.*\}", out or "", re.S)
            if m:
                d = json.loads(m.group(0))
                reply = (d.get("reply") or "").strip() or None
                question = (d.get("question") or "").strip() or None if turn < 3 else None
        except Exception as e:
            print("launchpad reply failed:", type(e).__name__, e)
    if not reply:
        names = picked["likes"] + picked["vibes"] if step != "dislikes" else picked["dislikes"]
        reply = (("Picked up: " + ", ".join(names) + ". I've ticked them below.") if names else
                 "Got it, I'll keep that in mind." if text else "No problem.")
        question = None
    return ok(reply=reply, question=question, picked=picked, ai=not chat.classic())


@bp.post("/api/taste/onboard")
def onboard():
    b = _body()
    likes = [c for c in b.get("likes") or [] if c in taste.CHIPS]
    dislikes = [c for c in b.get("dislikes") or [] if c in taste.CHIPS and c not in likes]
    vibes = [v for v in b.get("vibes") or [] if isinstance(v, str)][:10]
    texts = [t for t in b.get("texts") or [] if isinstance(t, str)][:6]
    taste.onboard(likes, vibes, dislikes, texts)
    settings.save({"launchpad_done": True})
    _fit_mem.clear()
    return ok(profile=taste.view(), summary=_onboard_summary())


def _onboard_summary():
    top = taste.top_genres(4)
    avoid = taste.avoid_genres(2)
    p = taste.profile()
    bits = []
    if top:
        bits.append("you love " + taste._list([g.lower() for g in top]))
    if p["anime"] > .2:
        bits.append("anime is on the menu")
    if avoid:
        bits.append("I'll steer clear of " + taste._list([g.lower() for g in avoid]))
    return ("Got it: " + "; ".join(bits) + ".") if bits else "All set. I'll learn as you watch."


@bp.post("/api/taste/skip")
def onboard_skip():
    if not taste.exists():
        taste._edit(lambda p: None)
    settings.save({"launchpad_done": True})
    return ok()


@bp.post("/api/taste/launchpad/rerun")
def launchpad_rerun():
    settings.save({"launchpad_done": False})
    taste._edit(lambda p: p.update(onboarded=False), bump=False)
    return ok()


# ------------------------------------------------------------------ "Is this for you?"
def _facts_for(key):
    """('item', catalog id) or ('tmdb', kind, id) -> (facts dict, cache key)."""
    from . import server
    cat = server.catalog()
    m = re.fullmatch(r"(movie|tv)[/:](\d+)", key)
    if m:
        kind, tmdb = m.group(1), int(m.group(2))
        owned = cat.by_tmdb(kind, tmdb)
        if owned:
            key = owned["id"]
        else:
            from . import details
            d = details.tmdb_detail(kind, tmdb) or {}
            if not d:
                try:
                    d = server.APP.seerr.details(kind, tmdb)
                except Exception:
                    d = {}
            if not d:
                return None, None
            return {**d, "type": kind, "kind": kind, "tmdb": tmdb}, f"{kind}:{tmdb}"
    r = cat.get(key)
    if not r:
        return None, None
    if r["type"] in ("Episode", "Season") and r.get("series_id"):
        r = cat.get(r["series_id"]) or r
    extra = r.get("extra") or {}
    facts = {**cat.card(r), "people": extra.get("people") or [], "tmdb": (r.get("provider") or {}).get("Tmdb")}
    return facts, r["id"]


def _ai_comment(fit, facts):
    f = fit["facts"]
    people = [x["name"] for x in f.get("people") or [] if x.get("role") == "Director"][:2]
    cast = [x["name"] for x in f.get("people") or [] if x.get("role") == "Actor"][:3]
    prompt = (f"Viewer taste:\n{taste.summary(1600)}\n\n"
              f"Title: {f.get('title')} ({f.get('year') or '?'}), {'series' if f.get('kind') == 'tv' else 'film'}.\n"
              f"Genres: {', '.join(f.get('genres') or []) or 'unknown'}."
              + (f" Directed by {', '.join(people)}." if people else "") + (f" Starring {', '.join(cast)}." if cast else "")
              + f"\nAbout: {(facts.get('overview') or '')[:320]}\n"
              f"Fit score: {fit['score']}/100 ({fit['verdict']}). Signals: {'; '.join(fit['reasons']) or 'none'}. "
              f"Different from their usual: {'yes' if fit['different'] else 'no'}.")
    text = ai.chat(prompt,
                   "You are Atlas, a home movie app. In ONE short sentence (max 28 words), tell the viewer whether "
                   "this title suits their taste and how it compares with what they usually watch; say so plainly if "
                   "it's different from their usual, and name one concrete link (a person, a title they loved, a mood) "
                   "when there is one (only titles the profile lists as loved). Second person, honest, warm. No spoilers, no emojis, no quotes, don't start "
                   "with the title.", max_tokens=80, timeout=25, temperature=.5)
    text = re.sub(r"\s+", " ", text or "").strip().strip('"')
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)  # no markdown in a plain sentence
    return text[:260] or None


@bp.get("/api/taste/fit/<path:key>")
def fit(key):
    """{score 0-100, verdict, reasons[], different, comment, source: ai|rules, ai_pending}. Fast by default (rules
    comment, or the cached AI one); ?ai=1 waits for the AI comment (cached per title + profile version)."""
    facts, ckey = _facts_for(key)
    if not facts:
        return fail("That title isn't known here", 404)
    res = taste.score(facts)
    out = {k: res[k] for k in ("score", "verdict", "reasons", "different")}
    out["empty"] = bool(res.get("empty"))
    out["comment"] = taste.classic_comment(res)
    out["source"] = "rules"
    out["title"] = res["facts"].get("title")
    llm = not chat.classic()
    cache_key = f"tastefit:{ckey}:{taste.rev()}"
    hit = _fit_mem.get(cache_key)
    if hit is None:
        try:
            from . import server
            hit = server.catalog().kv_get(cache_key, max_age=14 * 86400)[0]
        except Exception:
            hit = None
    if hit and hit.get("comment"):
        out.update(comment=hit["comment"], source="ai")
    elif llm and not res.get("empty") and not chat.playing():
        if request.args.get("ai") == "1":
            with _fit_guard:
                lock = _fit_locks.setdefault(cache_key, threading.Lock())
            with lock:
                hit = _fit_mem.get(cache_key)
                if not hit:
                    try:
                        text = _ai_comment(res, facts)
                    except Exception as e:
                        print("fit comment failed:", type(e).__name__, e)
                        text = None
                    hit = {"comment": text}
                    if text:
                        _fit_mem[cache_key] = hit
                        try:
                            from . import server
                            server.catalog().kv_set(cache_key, hit)
                        except Exception:
                            pass
            if hit.get("comment"):
                out.update(comment=hit["comment"], source="ai")
        else:
            out["ai_pending"] = True
    out["engine"] = ai.available()["name"] if out["source"] == "ai" else "Atlas"
    out["mine"] = taste.my_rating(facts)
    return ok(**out)


@bp.post("/api/taste/rate/<path:key>")
def rate_title(key):
    """Your own 1-5 stars for a title (0 clears); it steers the taste profile. key as for /api/taste/fit."""
    facts, _ = _facts_for(key)
    if not facts:
        return fail("That title isn't known here", 404)
    try:
        stars = int((request.get_json(silent=True) or {}).get("stars") or 0)
    except (TypeError, ValueError):
        return fail("stars must be 0-5")
    if not 0 <= stars <= 5:
        return fail("stars must be 0-5")
    r = taste.rate(facts, stars)
    _fit_mem.clear()
    return ok(rating=r, stars=stars)


# ------------------------------------------------------------------ a Home row that follows the profile
@bp.get("/api/taste/home-row")
def home_row():
    return ok(row=taste_row())


def taste_row():
    """Library titles that fit the taste profile best ({"key": "taste", ...} or None). Home's assemble step mixes
    requestable titles into it."""
    from . import server
    cat = server.catalog()
    p = taste.profile()
    if taste.is_empty(p):
        return None
    with cat.lock:
        rows = [r for r in cat._of("Movie,Series") if not cat._played(r)]
        facts = [(r, {**cat.card(r), "people": (r.get("extra") or {}).get("people") or []}) for r in rows]
    scored = sorted(((taste.score(f, p)["score"], r) for r, f in facts), key=lambda x: -x[0])
    items = []
    for s, r in scored[:20]:
        if s < 55:
            break
        items.append({**cat.card(r), "fit": s})
    top = taste.top_genres(2)
    title = "Picked for your taste" + (f": {taste._list([g.lower() for g in top])}" if top else "")
    return {"key": "taste", "title": title, "items": items, "style": "poster", "genres": top} if len(items) >= 3 else None


# ------------------------------------------------------------------ Letterboxd
@bp.post("/api/taste/letterboxd/rss")
def lbx_rss():
    name = (_body().get("username") or "").strip().lstrip("@")
    m = re.search(r"letterboxd\.com/([A-Za-z0-9_]+)", name)
    name = m.group(1) if m else name
    if not letterboxd.USER_RX.match(name):
        return fail("That doesn't look like a Letterboxd username")
    try:
        return ok(status=letterboxd.start("rss", name))
    except RuntimeError as e:
        return fail(e, 409)


@bp.post("/api/taste/letterboxd/zip")
def lbx_zip():
    f = request.files.get("file")
    data = f.read(61 * 2**20) if f else request.get_data(cache=False)[: 61 * 2**20]
    if not data:
        return fail("Choose your Letterboxd export ZIP")
    try:
        letterboxd.parse_zip(data)  # fail fast on a wrong file
        return ok(status=letterboxd.start("zip", data))
    except ValueError as e:
        return fail(e)
    except RuntimeError as e:
        return fail(e, 409)


@bp.get("/api/taste/letterboxd/status")
def lbx_status():
    return ok(status=letterboxd.status())


# ------------------------------------------------------------------ Ask Atlas (chat)
@bp.get("/api/chat/state")
def chat_state():
    return ok(mode="classic" if chat.classic() else "ai", engine=chat.engine_label(), ai=_ai_info(),
              suggestions=chat.suggestions(), history=chat.history()[-40:], playing=chat.playing(),
              finder={"genres": chat.FIND_GENRES, "moods": chat.FIND_MOODS, "eras": list(chat.ERAS)})


@bp.post("/api/chat/stream")
def chat_stream():
    """Newline-delimited JSON events: status, delta, replace, cards, memory, done, error."""
    b = _body()
    if chat.playing():
        return fail("Chat waits while a video is playing.", 409)
    msg = b.get("message") or ""

    def gen():
        yield json.dumps({"kind": "start", "at": time.time()}) + "\n" + " " * 2048 + "\n"  # nudge buffers open
        try:
            pick = b.get("pick") if isinstance(b.get("pick"), dict) else None
            intent = b.get("intent") if isinstance(b.get("intent"), dict) else None
            for ev in chat.stream(msg, pick=pick, intent=intent):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:
            print("chat failed:", type(e).__name__, e)
            yield json.dumps({"kind": "error", "text": "Something went wrong answering that. Try again."}) + "\n"
    r = Response(stream_with_context(gen()), mimetype="application/x-ndjson")
    r.headers["Cache-Control"] = "no-cache"
    r.headers["X-Accel-Buffering"] = "no"
    return r


@bp.post("/api/chat/find")
def chat_find():
    b = _body()
    if (b.get("q") or "").strip():
        text = taste.extract_memory(b["q"])
        if text:
            note = taste.add_note(text, source="chat")
            return ok(answer=f"Saved to memory: {note['text']}.", items=[], requestable=[], memory=note)
    return ok(**chat.find(b.get("q") or "", b.get("genres") or [], b.get("moods") or [], b.get("era"),
                          b.get("kind") or "any"))


@bp.get("/api/chat/command")
def chat_command():
    """Is this a command the rules understand ("pull up Dune", "is everything ok")? The Classic finder asks, and
    answers commands in a small conversation instead of filtering."""
    from . import chat_actions
    return ok(command=bool(chat_actions.parse(request.args.get("q") or "")))


@bp.post("/api/chat/action-state")
def chat_action_state():
    """{id, state}: a card under an answer was used ("sent", "watched", "cancelled"); kept in the history."""
    b = _body()
    if not b.get("id"):
        return fail("Which card?")
    chat.mark_action(b["id"], b.get("state") or "done")
    return ok()


@bp.delete("/api/chat/history")
def chat_clear():
    chat.clear()
    return ok()
