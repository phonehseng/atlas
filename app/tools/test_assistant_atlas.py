"""Ask Atlas end to end: a real (hidden, silent) mpv on a local file, this PC's Jellyfin/Seerr read-only, the
vision model and the fast model. Nothing is reported to Jellyfin (no resume points, nothing marked watched).
Run: .venv\\Scripts\\python tools\\test_assistant_atlas.py"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if not os.environ.get("MARQUEE_DEV_KEY"):
    cfg = json.loads(Path(r"D:\Videos\MediaSorter\config.json").read_text(encoding="utf-8-sig"))
    seerr = json.loads(Path(r"D:\Videos\Apps\Seerr\config\settings.json").read_text(encoding="utf-8-sig"))
    os.environ.update(MARQUEE_DEV_SERVER="http://127.0.0.1:8096", MARQUEE_DEV_KEY=cfg["jellyfin"]["api_key"],
                      MARQUEE_DEV_USER=os.environ.get("MARQUEE_DEV_USER", ""), MARQUEE_HOME=str(ROOT / ".devhome"),
                      MARQUEE_DEV_SEERR="http://127.0.0.1:5055", MARQUEE_DEV_SEERR_KEY=seerr["main"]["apiKey"],
                      MARQUEE_DEV_SEERR_USER="1")

import marquee.player  # noqa: E402,F401  (puts libmpv on PATH)
import mpv  # noqa: E402

from marquee import server  # noqa: E402
from marquee.assistant import blueprint, bp, vision  # noqa: E402
from marquee.jellyfin import FIELDS, Jellyfin  # noqa: E402
from marquee.seerr import Seerr  # noqa: E402

BANNERS, UI = [], []


class Player:
    def __init__(self, app, item, path):
        self.app, self.item, self.playing, self.next_item, self.pip = app, item, True, None, False
        self.mpv = mpv.MPV(vo="null", ao="null", mute=True, pause=True, hwdec="auto-copy", keep_open="always",
                           ytdl=False, load_scripts=False, osc=False, input_default_bindings=False, sid="auto",
                           slang="eng,en")
        self.mpv.register_message_handler("marquee-atlas", lambda *a: BANNERS.append(
            (round(time.time() % 1000, 1),) + tuple(x.decode() if isinstance(x, bytes) else x for x in a)))
        self.mpv.loadfile(path)
        self.mpv.wait_for_property("duration", lambda v: v, timeout=30)

    def goto(self, t):
        with self.mpv.prepare_and_wait_for_event("playback-restart", timeout=15):
            self.mpv.command("seek", str(t), "absolute+exact")

    def pause(self, mode="toggle"):
        self.mpv.pause = (not self.mpv.pause) if mode == "toggle" else mode in ("on", True, "pause")

    def seek(self, s, relative=False):
        self.mpv.command("seek", str(s), "relative" if relative else "absolute")

    def set_volume(self, v):
        self.mpv.volume = v

    def toast(self, m):
        pass

    def show_card(self, *a, **k):
        pass

    def state(self):
        return {"playing": True, "time": self.mpv.time_pos, "duration": self.mpv.duration, "paused": self.mpv.pause}


class App:
    window, fullscreen = None, False

    def __init__(self):
        self.jf, self.seerr = Jellyfin(), Seerr()

    def notify_ui(self, ev, data):
        UI.append((ev, data))


def wait_answer(c, ident, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = c.get("/api/assistant/answer").get_json()
        if cur.get("id") == ident and cur.get("state") == "answer":
            return cur, time.time() - t0
        time.sleep(0.25)
    return None, time.time() - t0


def main():
    app = App()
    hp = app.jf.items(searchTerm="Chamber of Secrets", Recursive="true", IncludeItemTypes="Movie", Limit=1, Fields=FIELDS)[0]
    path = hp["MediaSources"][0]["Path"]
    app.player = Player(app, hp, path)
    server.APP = app
    if "assistant" not in server.app.blueprints:
        server.app.register_blueprint(bp)
    c = server.app.test_client()
    print("vision:", vision.provider() and {k: v for k, v in vision.provider().items() if k != "key"})
    print("all vision models:", vision.ollama_vision_models(), "\n")
    p = app.player

    def ask(route, text, at, resume_after=None, kind=""):
        p.goto(at)
        p.mpv.pause = False
        t0 = time.time()
        if route == "look":
            r = c.post("/api/assistant/look", json={"question": text}).get_json()
            ident = r["id"]
        else:
            r = c.post("/api/assistant", json={"text": text}).get_json()
            ident = next((a["id"] for a in r["actions"] if a.get("type") == "pending"), None)
        returned = time.time() - t0
        paused_after_ask = p.mpv.pause
        if resume_after is not None:
            time.sleep(resume_after)
            p.mpv.pause = False   # the viewer presses play while it thinks
        if ident is None:
            print(f"> {text}  @{at}s  [{r.get('source')}] {r.get('reply')}\n")
            return
        ans, secs = wait_answer(c, ident)
        print(f"> {text}  @{at}s via {route}: returned in {returned * 1000:.0f} ms, paused={paused_after_ask}, "
              f"answer after {secs:.1f} s, still playing={not p.mpv.pause}\n  {ans and ans.get('text')}\n")

    ask("look", "who's that on my screen right now", 3000, resume_after=0.5)
    ask("text", "is that the same woman from Downton Abbey", 3000)
    ask("text", "where have I seen him before", 4800)
    ask("text", "what is that thing", 3600)
    ask("text", "is Lockhart the same guy from Hamlet", 3000)
    # asking again replaces the pending question
    p.goto(4200)
    first = c.post("/api/assistant/look", json={"question": "who is that"}).get_json()["id"]
    second = c.post("/api/assistant/look", json={"question": "what is he wearing"}).get_json()["id"]
    ans, secs = wait_answer(c, second)
    time.sleep(2)
    print(f"> replaced: first id {first} answered={any(b[1] == 'answer' and b[4] == str(first) for b in BANNERS)}, "
          f"second -> {ans and ans['text']} ({secs:.1f} s)\n")
    for at in (840, 7700):
        t0 = time.time()
        ask("text", "is there a jumpscare soon", at)
        from marquee.assistant import loudness
        print("  events:", loudness.scan(app, hp, at)[:6], f"(cached rescan {time.time() - t0:.1f} s total)\n")
    ask("text", "when does the flying car show up", 3000)
    # the OSC button: script-message marquee-ask-atlas <question>
    blueprint.hook_player(app)
    p.goto(4800)
    n0 = len(BANNERS)
    p.mpv.command("script-message", "marquee-ask-atlas", "who is crying")
    t0 = time.time()
    while time.time() - t0 < 120 and not any(b[1] == "answer" for b in BANNERS[n0:]):
        time.sleep(0.25)
    print("> script-message marquee-ask-atlas 'who is crying':", [b[1:3] for b in BANNERS[n0:]], f"{time.time() - t0:.1f} s\n")
    print("banner protocol sample:", BANNERS[:3])
    p.mpv.terminate()
    p.playing = False
    p.item = None

    # ---- the credits screen
    for name, item in (("movie", hp), ("episode", _episode(app))):
        if not item:
            continue
        t0 = time.time()
        r = c.post(f"/api/assistant/ending/{item['Id']}/prepare", json={}).get_json()
        first = c.get(f"/api/assistant/ending/{item['Id']}").get_json()
        while first["state"] == "pending" and time.time() - t0 < 120:
            time.sleep(0.5)
            first = c.get(f"/api/assistant/ending/{item['Id']}").get_json()
        from marquee.assistant import ending
        notes = ending._jobs.get(item["Id"]).notes if ending._jobs.get(item["Id"]) else {}
        label = item.get("SeriesName", "") + " " + item["Name"]
        print(f"ending ({name}: {label}) ready in {time.time() - t0:.1f} s, credits_at {first.get('credits_at')}, notes {notes}")
        print("  comment:", first["comment"])
        for pk in first["picks"]:
            print(f"  pick {pk['n']}: [{pk['kind']}] {pk['title']} - {pk['why']}")
        t1 = time.time()
        c.get(f"/api/assistant/ending/{item['Id']}")
        print(f"  cached GET {1000 * (time.time() - t1):.0f} ms\n")
    return 0


def _episode(app):
    for s in app.jf.items(Recursive="true", IncludeItemTypes="Series", SortBy="Random", Limit=20):
        eps = app.jf.get(f"/Shows/{s['Id']}/Episodes", userId=app.jf.user, Limit=3, Fields=FIELDS).get("Items", [])
        if len(eps) >= 2 and eps[0].get("Overview"):
            return eps[0]
    return None


if __name__ == "__main__":
    sys.exit(main())
