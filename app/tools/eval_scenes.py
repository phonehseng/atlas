"""Accuracy check for scene jumps: known moments (and moments that don't happen) in titles on this PC.
Runs the same search the assistant uses (no player needed), times it, and scores each answer.
Set MARQUEE_HOME to a private copy of the dev home first.
Run: .venv\\Scripts\\python tools\\eval_scenes.py [--only N,N] [--frames DIR]"""
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
                      MARQUEE_DEV_USER="1fd488d22e3045f2a576d03ee7766859")
assert os.environ.get("MARQUEE_HOME"), "set MARQUEE_HOME to a private copy of .devhome"


def T(s):
    p = [int(x) for x in s.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


FURY, EDGE, HP = "Fury", "Let You Down", "Chamber of Secrets"
# (title, request, accepted [start, end] ranges or None when it doesn't happen)
CASES = [
    (FURY, "go to the part where wardaddy stabs the german officer on the horse", [("2:00", "3:45")]),
    (FURY, "skip to the scene where the tiger tank attacks", [("1:18:30", "1:24:30")]),
    (FURY, "take me to the part where they eat breakfast with the german women", [("1:02:30", "1:12:30")]),
    (FURY, "jump to when he says best job I ever had", [("1:25:30", "1:26:10"), ("1:40:30", "1:41:00")]),
    # the scene runs 37:30-40:45; he's forced to fire at 40:20-40:33 (frames checked)
    (FURY, "go to the part where norman is forced to shoot the prisoner", [("37:00", "40:50")]),
    (FURY, "find the scene where they fight at the crossroads at night", [("1:44:00", "2:05:00")]),
    (FURY, "go to the part where spiderman gets shot", None),
    # "I want this fused here" 22:15, then the implant montage until the end credits (frames checked)
    (EDGE, "jump to the scene where he gets the cyberware implanted", [("22:00", "22:50")]),
    # she's hurt in the crash (12:20) but dies in hospital: the call and the crematorium, 18:38-19:40 (frames checked)
    (EDGE, "go to the part where david's mom dies", [("18:30", "19:45")]),
    (HP, "go to the part where he says \"why can't we get through\"", [("22:50", "23:40")]),
    (HP, "go to the part where he says why couldn't we get through the barrier", [("22:50", "23:40")]),
    (HP, "skip to the part where the spiders chase them", [("1:43:00", "1:51:30")]),
    # eight more with known answers
    (HP, "go to the part where they have the dueling club", [("1:04:20", "1:08:30")]),
    (HP, "take me to where dobby is set free with a sock", [("2:24:30", "2:26:30")]),
    (HP, "jump to the part where harry fights the basilisk", [("2:08:00", "2:16:30")]),
    (HP, "go to where the flying car crashes into the whomping willow", [("26:30", "28:45")]),
    (EDGE, "go to the part where the car crashes", [("12:00", "12:55")]),
    (EDGE, "skip to where the dragon attacks", None),
    (FURY, "go to the part where they ride horses on the beach", None),
    (HP, "go to the part where spiderman gets shot", None),
    # a live failure: the CLIP index has it (Fremen firing a shoulder launcher; a harvester under rocket attack)
    ("Dune: Part Two", "go to the scene where they fire a rocket launcher", [("1:27:00", "1:30:00"), ("35:00", "38:00")]),
]


def main():
    from marquee import server
    from marquee.assistant import scenes
    from marquee.jellyfin import FIELDS, Jellyfin

    class App:
        player = None
        window = None

        class seerr:
            ready = False
    app = App()
    app.jf = Jellyfin()
    server.APP = app
    only = None
    if "--only" in sys.argv:
        only = {int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")}
    frames = Path(sys.argv[sys.argv.index("--frames") + 1]) if "--frames" in sys.argv else None
    if "--prepare" in sys.argv:  # the story summaries are made ahead (title page opened), not timed here
        from marquee.assistant import scene_search
        for title in sorted({c[0] for c in CASES}):
            it = app.jf.items(searchTerm=title, Recursive="true", IncludeItemTypes="Movie,Episode", Limit=1, Fields=FIELDS)[0]
            t0 = time.time()
            d = scene_search.summaries(app, it, build=True, deadline=time.time() + 900)
            print(f"prepared {title}: {len(d['stretches']) if d else 0} stretches, embeddings "
                  f"{bool(d and d.get('vec_sum'))}, {time.time() - t0:.0f} s", flush=True)
    sc = scenes.Scenes.__new__(scenes.Scenes)
    sc.app = app
    items = {}
    rows, tally = [], {"right": 0, "picker_right": 0, "wrong_jump": 0, "picker_wrong": 0, "none_right": 0, "missed": 0}
    for n, (title, text, ranges) in enumerate(CASES, 1):
        if only and n not in only:
            continue
        if title not in items:
            items[title] = app.jf.items(searchTerm=title, Recursive="true", IncludeItemTypes="Movie,Episode", Limit=1,
                                        Fields=FIELDS)[0]
        item = items[title]
        query = __import__("marquee.assistant.intents", fromlist=["parse"]).parse(text)["query"]
        t0 = time.time()
        f = sc.find(item, query)
        secs = time.time() - t0
        ok = lambda t: ranges is not None and any(T(a) - 3 <= t <= T(b) for a, b in ranges)  # noqa: E731
        times = [c["jump"] for c in f["cands"]]
        if f["kind"] == "winner":
            verdict = "right" if ok(times[0]) else "wrong_jump"
        elif f["kind"] == "choices":
            verdict = "picker_right" if any(ok(t) for t in times) else "picker_wrong"
        else:
            verdict = "none_right" if ranges is None else "missed"
        tally[verdict] += 1
        shown = ", ".join(scenes.clock(t) for t in times) or "-"
        rows.append((n, title, query, f["kind"], shown, verdict, secs))
        print(f"{n:2}. {verdict:12} {secs:5.1f}s  {f['kind']:8} {shown:28} {title[:12]:12} {query}", flush=True)
        if frames and times:
            frames.mkdir(parents=True, exist_ok=True)
            try:
                jpeg = scenes._frame_for(app, item, times[0] + 1, 480)
                (frames / f"{n:02d}.jpg").write_bytes(jpeg)
            except Exception as e:
                print("   frame failed:", e)
    good = tally["right"] + tally["none_right"]
    print(f"\nright jumps {tally['right']}, right 'not found' {tally['none_right']}, picker containing the answer "
          f"{tally['picker_right']}, wrong jumps {tally['wrong_jump']}, wrong pickers {tally['picker_wrong']}, "
          f"missed {tally['missed']}  ->  {good}/{len(rows)} fully right, "
          f"{good + tally['picker_right']}/{len(rows)} right or offered; median "
          f"{sorted(r[6] for r in rows)[len(rows) // 2]:.1f} s")


if __name__ == "__main__":
    main()
