"""Try the media index from the command line, against the Jellyfin in the dev environment (tools\\dev.ps1).

  python tools\\index_try.py index <itemId> [<itemId> ...]      index now (in the foreground), print timings
  python tools\\index_try.py search <itemId> "red car" [...]      ranked hits; --sheet out.jpg saves their frames
  python tools\\index_try.py sensitive|chapters|intro <itemId>
  python tools\\index_try.py serve [port]                          the /api/index blueprint on its own
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from marquee import server  # noqa: E402
from marquee.index import analysis, api, deps, store  # noqa: E402
from marquee.jellyfin import Jellyfin  # noqa: E402


class _Player:
    playing = False


class _App:
    def __init__(self):
        self.jf = Jellyfin()
        self.player = _Player()


def main():
    server.APP = _App()
    w = api.worker
    cmd, args = sys.argv[1], sys.argv[2:]
    if not deps.availability()["available"]:
        print("installing:", deps.missing())
        deps.install()
        print(deps.INSTALL)
    if cmd == "index":
        for item in args:
            item = item.replace("-", "").lower()
            t0 = time.time()
            w._index(item, 0)
            d = store.load(item)
            if d is None:
                print(item, "failed:", w.errors.get(item))
                continue
            m = d["meta"]
            took = time.time() - t0
            print(json.dumps({"item": item, "name": m.get("name"), "video_min": round(m["duration"] / 60, 1),
                              "seconds": round(took, 1), "x_realtime": round(m["duration"] / took, 1),
                              "sec_per_video_hour": round(took / (m["duration"] / 3600)), "frames": len(d["t"]),
                              "cuts": len(d["cuts"]), "decoder": m.get("decoder"),
                              "provider": w.clip.provider if w.clip else None,
                              "file_kb": round(store.path(item).stat().st_size / 1024)}))
        return
    item = args[0].replace("-", "").lower()
    d = store.load(item)
    if cmd == "search":
        sheet = None
        if "--sheet" in args:
            i = args.index("--sheet")
            sheet = args[i + 1]
            del args[i:i + 2]
        clip = w.get_clip()
        tiles = []
        for q in args[1:]:
            hits = analysis.search(d, clip.query(q), k=5)
            print(q, "->", [(fmt(h["t"]), h["score"], h["z"]) for h in hits])
            tiles.append((q, hits))
        if sheet:
            contact_sheet(item, tiles, sheet)
    elif cmd == "sensitive":
        d = api._with_sens(d)
        for r in analysis.sensitive_ranges(d):
            print(r["category"], fmt(r["start"]), "-", fmt(r["end"]), r["peak"])
    elif cmd == "chapters":
        for c in analysis.chapters(d):
            print(c["name"], fmt(c["start"]), "-", fmt(c["end"]), "thumb", fmt(c["thumb_t"]))
    elif cmd == "intro":
        with server.app.test_request_context():
            print(api.intro(item).get_json())
    elif cmd == "serve":
        server.app.register_blueprint(api.bp)
        server.app.run("127.0.0.1", int(args[0]) if args else 8799)


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def contact_sheet(item, tiles, out):
    """One row per query: its top hits' frames, labelled with their times."""
    import io

    from PIL import Image, ImageDraw
    src, info = api.worker.source(item)
    dec = api.worker.get_decoder()
    tw, th = 320, 180
    cols = max(len(h) for _, h in tiles)
    img = Image.new("RGB", (cols * tw, len(tiles) * (th + 22)), "white")
    dr = ImageDraw.Draw(img)
    for r, (q, hits) in enumerate(tiles):
        for c, h in enumerate(hits):
            fr = Image.open(io.BytesIO(dec.frame_jpeg(src, info, h["t"], tw))).resize((tw, th))
            img.paste(fr, (c * tw, r * (th + 22) + 22))
            dr.text((c * tw + 4, r * (th + 22) + 4), f"{q[:22]} @ {fmt(h['t'])} z={h['z']}", fill="black")
    img.save(out, quality=85)
    print("sheet:", out)


if __name__ == "__main__":
    main()
