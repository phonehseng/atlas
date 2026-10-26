"""Scene jumps end to end: a hidden, silent mpv with the real controls script on a local, indexed title, the media
index, subtitles and the fast model. Nothing is reported to Jellyfin. Set MARQUEE_HOME to a private copy of the dev
home first (the index may write to it).  Run: .venv\\Scripts\\python tools\\test_assistant_scenes.py [title]"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if not os.environ.get("MARQUEE_DEV_KEY"):
    cfg = json.loads(Path(r"D:\Videos\MediaSorter\config.json").read_text(encoding="utf-8-sig"))
    os.environ.update(MARQUEE_DEV_SERVER="http://127.0.0.1:8096", MARQUEE_DEV_KEY=cfg["jellyfin"]["api_key"],
                      MARQUEE_DEV_USER=os.environ.get("MARQUEE_DEV_USER", ""))
assert os.environ.get("MARQUEE_HOME"), "set MARQUEE_HOME to a private copy of .devhome"
os.environ.pop("MARQUEE_DEV_PUSH", None)

import marquee.player  # noqa: E402,F401
import mpv  # noqa: E402

from marquee import server  # noqa: E402
from marquee.assistant import blueprint, bp, frame  # noqa: E402
from marquee.jellyfin import FIELDS, Jellyfin  # noqa: E402

OSC = str(ROOT / "marquee" / "osc" / "marquee.lua")
MSGS, LOG = [], []
OUT = Path(os.environ.get("SCENE_TEST_OUT", ROOT / ".devhome" / "scene-test"))


class Player:
    def __init__(self, app, item, path):
        self.app, self.item, self.playing, self.next_item, self.pip = app, item, True, None, False
        self.mpv = mpv.MPV(vo="null", ao="null", mute=True, pause=True, hwdec="auto-copy", keep_open="always",
                           ytdl=False, osc=False, input_default_bindings=False, scripts=OSC,
                           log_handler=lambda lv, comp, msg: LOG.append((lv, comp, msg.strip())), loglevel="warn")
        for target in ("marquee-atlas", "marquee-scene-pick", "marquee-scene-undo"):
            self.mpv.register_message_handler(target, (lambda tg: lambda *a: MSGS.append(
                (tg,) + tuple(x.decode() if isinstance(x, bytes) else x for x in a)))(target))
        self.mpv.loadfile(path)
        self.mpv.wait_for_property("duration", lambda v: v, timeout=30)

    def goto(self, t):
        with self.mpv.prepare_and_wait_for_event("playback-restart", timeout=15):
            self.mpv.command("seek", str(t), "absolute+exact")

    def pause(self, mode="toggle"):
        self.mpv.pause = (not self.mpv.pause) if mode == "toggle" else mode in ("on", True, "pause")

    def seek(self, s, relative=False):
        with self.mpv.prepare_and_wait_for_event("playback-restart", timeout=15):
            self.mpv.command("seek", str(s), "relative+exact" if relative else "absolute+exact")

    def set_volume(self, v):
        self.mpv.volume = v

    def toast(self, m):
        MSGS.append(("toast", m))

    def show_card(self, *a, **k):
        pass

    def state(self):
        return {"playing": True, "time": self.mpv.time_pos, "duration": self.mpv.duration, "paused": self.mpv.pause}


class App:
    window, fullscreen = None, False

    def __init__(self):
        self.jf = Jellyfin()

        class NoSeerr:
            ready = False
        self.seerr = NoSeerr()

    def notify_ui(self, ev, data):
        MSGS.append(("ui:" + ev, data))


def clock(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "Fury"
    app = App()
    item = app.jf.items(searchTerm=title, Recursive="true", IncludeItemTypes="Movie,Episode", Limit=1, Fields=FIELDS)[0]
    path = item["MediaSources"][0]["Path"]
    app.player = Player(app, item, path)
    server.APP = app
    if "assistant" not in server.app.blueprints:
        server.app.register_blueprint(bp)
    blueprint.hook_player(app)
    c = server.app.test_client()
    p = app.player
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{item['Name']}: {path}\n")
    tests = sys.argv[2:] or [
        "go to the part where wardaddy stabs the german officer on the horse",
        "skip to the scene where the tiger tank attacks",
        "take me to the part where they eat breakfast with the german women",
        "jump to when he says best job I ever had",
        "go to the part where norman is forced to shoot the prisoner",
        "find the scene where they fight at the crossroads at night",
        "go to the part where spiderman gets shot",
    ]
    start_at = 1800.0
    for n, text in enumerate(tests, 1):
        p.goto(start_at)
        p.mpv.pause = False
        mark = len(MSGS)
        t0 = time.time()
        r = c.post("/api/assistant", json={"text": text}).get_json()
        # wait for the outcome: an answer banner, a picker, or a timeout
        outcome = None
        while time.time() - t0 < 120 and outcome is None:
            for m in MSGS[mark:]:
                if m[0] == "marquee-scene-pick" or (m[0] == "marquee-atlas" and m[1] == "answer"):
                    outcome = m
            time.sleep(0.2)
        secs = time.time() - t0
        landed = p.mpv.time_pos
        print(f"{n}. \"{text}\"  ({r.get('reply')}, {secs:.1f} s)")
        if outcome and outcome[0] == "marquee-scene-pick":
            args = outcome[1:]
            items = [args[i:i + 3] for i in range(4, len(args), 3)]
            print(f"   picker (paused={p.mpv.pause}): " + "; ".join(f"{a[0]} {a[1]}" for a in items))
            for k, a in enumerate(items, 1):
                if a[2]:
                    _save_bgra(a[2], int(args[2]), int(args[3]), OUT / f"{n}-choice{k}.png")
            # choose the first one, like pressing 1 (script-message from the controls)
            p.mpv.command("script-message", "marquee-scene-choose", args[0], "1")
            time.sleep(1.5)
            landed = p.mpv.time_pos
            print(f"   chose 1 -> {clock(landed)}  banner: {next((m[2] for m in MSGS[mark:] if m[0] == 'marquee-atlas' and m[1] == 'answer'), None)}")
        elif outcome:
            print(f"   landed {clock(landed)}  banner: {outcome[2]}")
        else:
            print("   no outcome")
        try:
            jpeg = frame.from_mpv(p.mpv, 480)
            (OUT / f"{n}-landed.jpg").write_bytes(jpeg)
        except Exception as e:
            print("   frame failed:", e)
        if n == 1:  # undo: "go back" right after the jump
            u = c.post("/api/assistant", json={"text": "go back"}).get_json()
            time.sleep(1)
            print(f"   'go back' -> {u.get('reply')} (now {clock(p.mpv.time_pos)}, was {clock(start_at)})")
    # the details page (nothing playing): the time comes back with "Play from" actions
    p.playing = False
    t0 = time.time()
    r = c.post("/api/assistant", json={"text": tests[0], "item_id": item["Id"]}).get_json()
    print(f"\ndetails page: \"{tests[0]}\" -> {r.get('reply')} {[(a['label'], a.get('text')) for a in r.get('actions', [])]}"
          f" ({time.time() - t0:.1f} s)")
    p.playing = True
    errs = [x for x in LOG if x[0] in ("error", "fatal") or "marquee.lua" in x[1]]
    print("\nOSC script errors:", errs[:5] or "none")
    p.mpv.terminate()
    return 0


def _save_bgra(f, w, h, out):
    from PIL import Image
    Image.frombuffer("RGBA", (w, h), Path(f).read_bytes(), "raw", "BGRA", 0, 1).convert("RGB").save(out)


if __name__ == "__main__":
    sys.exit(main())
