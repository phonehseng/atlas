"""Who's on screen (matched only against this title's own cast list) and "is that the same actor from X?".

The face in the frame is compared by the vision model with a numbered sheet of this title's cast photos from
Jellyfin. There is no open-ended face recognition: the only possible answers are people in the cast list."""
import io
import json
import re

from .. import settings
from . import vision

CACHE = settings.DIR / "assistant" / "cast"
CELL_W, CELL_H, LABEL_H, COLS = 150, 190, 34, 4


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def people(app, item, limit=16):
    """Actors of the playing item (episode: its guests and the show's regulars), with photos first."""
    jf = app.jf
    if not item or str(item.get("Id", "")).startswith("file:"):
        return []
    full = jf.item(item["Id"])
    out, seen = [], set()
    sources = [full.get("People") or []]
    if full.get("SeriesId"):
        try:
            sources.append(jf.item(full["SeriesId"]).get("People") or [])
        except Exception:
            pass
    for group in sources:
        for p in group:
            if p.get("Type") in ("Actor", "GuestStar") and p.get("Id") not in seen:
                seen.add(p.get("Id"))
                out.append(p)
    with_photo = [p for p in out if p.get("PrimaryImageTag")]
    return (with_photo + [p for p in out if not p.get("PrimaryImageTag")])[:limit]


def headshot(app, p):
    """The person's Jellyfin photo, cached on disk (JPEG bytes) or None."""
    if not p.get("PrimaryImageTag"):
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{p['Id']}_{p['PrimaryImageTag'][:12]}.jpg"
    if f.exists():
        return f.read_bytes()
    jf = app.jf
    try:
        r = jf.s.get(jf.image(p["Id"], "Primary", fillHeight=CELL_H * 2, quality=85),
                     headers={"Authorization": jf.auth_header()}, timeout=15)
        r.raise_for_status()
        f.write_bytes(r.content)
        return r.content
    except Exception as e:
        print("cast photo failed:", p.get("Name"), e)
        return None


def contact_sheet(app, cast):
    """A numbered grid of cast photos with names: JPEG bytes, and the people in it (number = index + 1)."""
    from PIL import Image, ImageDraw, ImageFont
    shown = []
    for p in cast:
        data = headshot(app, p)
        if data:
            shown.append((p, data))
    if not shown:
        return None, []
    rows = (len(shown) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CELL_W, rows * (CELL_H + LABEL_H)), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 15)
        small = ImageFont.truetype("segoeui.ttf", 12)
    except Exception:
        font = small = ImageFont.load_default()
    for n, (p, data) in enumerate(shown):
        x, y = (n % COLS) * CELL_W, (n // COLS) * (CELL_H + LABEL_H)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        scale = max(CELL_W / img.width, CELL_H / img.height)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
        left, top = (img.width - CELL_W) // 2, max(0, (img.height - CELL_H) // 4)
        sheet.paste(img.crop((left, top, left + CELL_W, top + CELL_H)), (x, y))
        draw.rectangle((x, y, x + 26, y + 22), fill="black")
        draw.text((x + 5, y + 2), str(n + 1), fill="white", font=font)
        draw.text((x + 4, y + CELL_H + 1), (p.get("Name") or "")[:22], fill="black", font=small)
        draw.text((x + 4, y + CELL_H + 16), (p.get("Role") or "")[:22], fill=(90, 90, 90), font=small)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=88)
    return buf.getvalue(), [p for p, _ in shown]


def who_on_screen(app, item, frame_jpeg, question="Who is that?"):
    """(answer, person or None, model ms)."""
    cast = people(app, item)
    if not cast:
        return "I don't have a cast list for this one, so I can't tell who that is.", None, None
    sheet, shown = contact_sheet(app, cast)
    if not sheet:
        return "Jellyfin has no cast photos for this title, so I can't match faces.", None, None
    title = item.get("SeriesName") or item.get("Name") or "this video"
    listing = "; ".join(f"{i + 1}: {p['Name']} as {p.get('Role') or '?'}" for i, p in enumerate(shown))
    prompt = (f"Image 1 is a frame from {title}. Image 2 shows this title's cast, numbered: {listing}. "
              f"Question: {question} First work out which character is the main person in image 1 (from the scene and "
              f"the faces), then give that cast member's number. Only choose from this list; use 0 if no face is "
              f"clearly visible or none matches. Reply with JSON only: "
              f'{{"character": "...", "number": <0-{len(shown)}>, "confidence": "high"|"medium"|"low"}}')
    text, ms, _ = vision.ask(prompt, [frame_jpeg, sheet], max_tokens=120, json_mode=True)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    n = data.get("number")
    if n is None:  # prose instead of JSON: take an explicit "number": n, else the character it named
        m2 = re.search(r'"?number"?\s*[:=]\s*(\d{1,2})', text)
        n = int(m2.group(1)) if m2 else 0
    try:
        n = int(n)
    except Exception:
        n = 0
    said = norm(data.get("character") or "")
    if not 1 <= n <= len(shown) and said:
        n = next((i + 1 for i, p in enumerate(shown) if said and (said in norm(p.get("Role")) or norm(p.get("Role")) in said)
                  and norm(p.get("Role"))), 0)
    if not 1 <= n <= len(shown):
        return "I can't match anyone on screen to this title's cast right now.", None, ms
    p = shown[n - 1]
    conf = str(data.get("confidence") or "").lower()
    hedge = "probably " if conf in ("low", "medium") else ""
    role = f" as {p['Role']}" if p.get("Role") else ""
    return f"That's {hedge}{p['Name']}{role}.", p, ms


def tmdb_person_id(app, person):
    try:
        d = app.jf.get(f"/Items/{person['Id']}", userId=app.jf.user)
        return (d.get("ProviderIds") or {}).get("Tmdb")
    except Exception:
        return None


def credits(app, person):
    """[{title, year, character, type}] from Seerr (TMDB), else the titles in this library."""
    out = []
    tmdb = tmdb_person_id(app, person)
    if tmdb and getattr(app.seerr, "ready", False):
        try:
            d = app.seerr.call("GET", f"/person/{tmdb}/combined_credits")
            for c in d.get("cast") or []:
                out.append({"title": c.get("title") or c.get("name") or c.get("originalTitle") or c.get("originalName"),
                            "year": (c.get("releaseDate") or c.get("firstAirDate") or "")[:4],
                            "character": c.get("character") or "", "type": c.get("mediaType"),
                            "votes": c.get("voteCount") or 0, "episodes": c.get("episodeCount")})
        except Exception as e:
            print("credits from Seerr failed:", e)
    if not out:
        try:
            for i in app.jf.items(Recursive="true", PersonIds=person["Id"], IncludeItemTypes="Movie,Series", Limit=100):
                role = next((p.get("Role") for p in i.get("People") or [] if p.get("Id") == person["Id"]), "")
                out.append({"title": i.get("Name"), "year": str(i.get("ProductionYear") or ""), "character": role or "",
                            "type": "tv" if i.get("Type") == "Series" else "movie", "votes": 0})
        except Exception:
            pass
    return out


def same_as(app, person, other_title, current=None):
    """'Is that the same actor from <other_title>?' -> answer line."""
    creds = credits(app, person)
    if not creds:
        return f"I couldn't get {person['Name']}'s other roles."
    want = norm(other_title)
    want = re.sub(r"^the ", "", want)
    hits = [c for c in creds if c["title"] and (want in norm(c["title"]) or norm(c["title"]) in want and len(norm(c["title"])) > 3)]
    if hits:
        c = max(hits, key=lambda c: c.get("votes") or 0)
        role = f" {c['character']}" if c.get("character") else " a part"
        yr = f" ({c['year']})" if c.get("year") else ""
        return f"Yes - {person['Name']} played{role} in {c['title']}{yr}."
    also = ", ".join(known_for(app, person, 3, current))
    return f"No, {person['Name']} isn't in {other_title}" + (f". You may know them from {also}." if also else ".")


def known_for(app, person, n=4, current=None):
    """Their best-known other roles (not this title or its own series/franchise)."""
    stem = " ".join(norm(current).split()[:2]) if current else None
    creds = sorted([c for c in credits(app, person) if c.get("title") and not (stem and norm(c["title"]).startswith(stem))],
                   key=lambda c: -(c.get("votes") or 0))
    seen, out = set(), []
    for c in creds:
        if c["title"] not in seen:
            seen.add(c["title"])
            out.append(f"{c['title']}" + (f" ({c['character']})" if c.get("character") else ""))
        if len(out) >= n:
            break
    return out


STOP = {"the", "and", "voice", "young", "older", "himself", "herself", "uncredited", "same", "guy", "from",
        "that", "this", "with", "actor", "actress", "woman"}


def find_person(app, item, text):
    """A cast member named in the question by actor or character name (no picture needed)."""
    t = set(norm(text).split())
    best, score = None, 0
    for p in people(app, item, limit=60):
        words = {w for key in (p.get("Name"), p.get("Role")) for w in norm(key).split() if len(w) > 3 and w not in STOP}
        hit = len(words & t)
        if hit > score:
            best, score = p, hit
    return best
