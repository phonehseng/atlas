"""End-to-end check of the assistant against this PC's Jellyfin (read-only) and AI provider, with a stand-in
player (no window, no sound). Run after tools\\dev.ps1 has set the MARQUEE_DEV_* variables, or let this script
read them the same way:  .venv\\Scripts\\python tools\\test_assistant.py"""
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
                      MARQUEE_DEV_USER=os.environ.get("MARQUEE_DEV_USER", ""),
                      MARQUEE_HOME=str(ROOT / ".devhome"))

from marquee import server  # noqa: E402
from marquee.assistant import bp  # noqa: E402
from marquee.jellyfin import FIELDS, Jellyfin  # noqa: E402


class FakeMpv:
    def __init__(self, item):
        self.pause, self.time_pos, self.volume, self.mute, self.speed = False, 1500.0, 80, False, 1.0
        self.duration = (item.get("RunTimeTicks") or 0) / 1e7
        self.sub_visibility, self.sid, self.aid, self.slang = False, False, 1, ["eng", "en"]
        streams = (item.get("MediaSources") or [{}])[0].get("MediaStreams") or []
        self.track_list = []
        n = {"Audio": 0, "Subtitle": 0}
        for s in streams:
            if s.get("Type") in n:
                n[s["Type"]] += 1
                self.track_list.append({"type": "audio" if s["Type"] == "Audio" else "sub", "id": n[s["Type"]],
                                        "lang": s.get("Language"), "title": s.get("Title"), "ff-index": s.get("Index"),
                                        "forced": s.get("IsForced"), "default": s.get("IsDefault"), "selected": False})

    def seek(self, v, mode):
        self.time_pos = v + self.time_pos if mode == "relative" else v


class FakePlayer:
    def __init__(self, app, item):
        self.app, self.item, self.playing, self.next_item = app, item, True, None
        self.mpv = FakeMpv(item)
        self.log = []

    def play(self, item_id, start=None):
        self.log.append(("play", item_id, start))

    def stop(self):
        self.log.append(("stop",))

    def toast(self, msg):
        self.log.append(("toast", msg))

    def show_card(self, title, text):
        self.log.append(("card", title, text))


class FakeApp:
    def __init__(self):
        self.jf = Jellyfin()
        self.fullscreen = False
        self.window = None

        class NoSeerr:
            ready = False
        self.seerr = NoSeerr()


def pick_item(jf):
    """A movie with a text subtitle track (for the subtitle questions)."""
    for i in jf.items(Recursive="true", IncludeItemTypes="Movie", SortBy="Random", Limit=60, Fields=FIELDS):
        subs = [s for s in (i.get("MediaSources") or [{}])[0].get("MediaStreams") or []
                if s.get("Type") == "Subtitle" and (s.get("Codec") or "").lower() in ("subrip", "srt", "ass", "ssa")]
        if subs and (i.get("RunTimeTicks") or 0) > 3600e7:
            return i
    return None


def main():
    app = FakeApp()
    item = pick_item(app.jf)
    if not item:
        print("no movie with text subtitles found")
        return 1
    app.player = FakePlayer(app, item)
    server.APP = app
    if "assistant" not in server.app.blueprints:
        server.app.register_blueprint(bp)
    c = server.app.test_client()
    print(f"playing (stand-in): {item['Name']} ({item.get('ProductionYear')}) at 25:00\n")
    phrases = ["pause", "resume", "skip forward 30 seconds", "go back 10 seconds", "louder", "turn on subtitles",
               "what did that character just say", "who is that actor", "how much time is left",
               "go back to where they entered the ship", "play the next episode", "find something funny",
               "play Dune", "summarize the last 5 minutes", "why is everyone so angry right now",
               "is this movie based on a book"]
    if "--no-llm" in sys.argv:
        phrases = phrases[:-3]
    rows = []
    for p in phrases:
        t0 = time.perf_counter()
        r = c.post("/api/assistant", json={"text": p}).get_json()
        ms = int((time.perf_counter() - t0) * 1000)
        llm_ms = next((a.get("llm_ms") or a.get("ms") for a in r.get("actions", []) if a.get("type") in
                       ("summarize", "answer", "llm")), None)
        rows.append((p, r.get("source"), r.get("intent"), ms, llm_ms))
        print(f"> {p}\n  [{r.get('source')}/{r.get('intent')}, {ms} ms{f', model {llm_ms} ms' if llm_ms else ''}] "
              f"{r.get('reply')}\n")
    st = c.get("/api/assistant/status").get_json()
    print("status: llm", st["llm"], "| model", st["model"], "| voice", st["voice"]["mode"])
    print("player calls:", [x for x in app.player.log if x[0] != "toast"][:6])
    print("mpv after: pos %.0f, volume %s, subs %s/%s" % (app.player.mpv.time_pos, app.player.mpv.volume,
                                                          app.player.mpv.sub_visibility, app.player.mpv.sid))
    print("\nlatency (ms):")
    for p, src, intent, ms, llm_ms in rows:
        print(f"  {ms:6d}  {src:5} {intent or '-':16} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
