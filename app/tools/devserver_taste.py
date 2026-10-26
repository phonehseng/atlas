"""Development harness for the taste profile, the chat page and the Launchpad, before they are wired into the app.

Like tools/devserver.py (run tools/dev.ps1 first; MARQUEE_DEV_NO_ARRIVALS=1 recommended), plus:
  - registers marquee.taste_api's blueprint
  - http://127.0.0.1:8600/harness : the real index.html + app.js with the integration lines from tools/INTEGRATION.md
    ("## Taste, chat and launchpad") applied in memory, so the pages can be tried exactly as they'll be wired.
The patches are applied to the current files at every request; a patch whose anchor isn't found is reported in the
console and on the page (the UI agent may have moved that code)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from flask import Response  # noqa: E402

from marquee import server  # noqa: E402
from marquee.app import App  # noqa: E402
from marquee.taste_api import bp as taste_bp  # noqa: E402

WEB = ROOT / "marquee" / "web"

INDEX_PATCHES = [
    ('<link rel="stylesheet" href="app.css">',
     '<link rel="stylesheet" href="app.css">\n  <link rel="stylesheet" href="taste.css">\n'
     '  <link rel="stylesheet" href="chat.css">\n  <link rel="stylesheet" href="launchpad.css">'),
    ('<a href="#/requests" data-route="requests">Requests</a>',
     '<a href="#/requests" data-route="requests">Requests</a>\n      <a href="#/chat" data-route="chat">Chat</a>'),
    ('<script src="app.js"></script>',
     '<script src="taste.js"></script>\n<script src="chat.js"></script>\n<script src="launchpad.js"></script>\n'
     '<script src="harness-app.js"></script>'),
]
APP_PATCHES = [
    # 1. the Chat page route
    ("requests: renderRequests, settings: renderSettings",
     "requests: renderRequests, settings: renderSettings,\n"
     "  chat: page => window.MarqueeChat ? window.MarqueeChat.mount(page) : renderHome(page, S.renderSeq)"),
    # 2. Settings > Taste & memory (a full-width panel)
    ("${remotePanel(s)}",
     '<section class="panel glass taste-panel" id="taste-panel" style="--i:6"></section>\n    ${remotePanel(s)}'),
    ("  loadAudioDevices();\n  loadRemote();\n",
     "  loadAudioDevices();\n  loadRemote();\n"
     "  window.MarqueeTaste && window.MarqueeTaste.renderSettings($('#taste-panel'));\n"),
    # 3. "Is this for you?" on the details page (library titles and titles to request), under the overview
    ("  if (D.extras) applyExtras();\n}\n",
     "  if (D.extras) applyExtras();\n"
     "  if (window.MarqueeFit && $('.dx-ov', box)) { const slot = document.createElement('div'); $('.dx-ov', box).after(slot);"
     " window.MarqueeFit.render(slot, D.kind === 'tmdb' ? { type: D.key.split(':')[0], id: +D.key.split(':')[1] } : { id: D.id }); }\n}\n"),
    # 5. first run: the Launchpad over Home
    ("  $('#view').replaceChildren();\n  route();\n}",
     "  $('#view').replaceChildren();\n  route();\n  window.MarqueeLaunchpad && window.MarqueeLaunchpad.maybeShow();\n}"),
    # 6. when the taste profile changes (Launchpad finished, reset), Home redraws
    ("addEventListener('hashchange', route);",
     "addEventListener('hashchange', route);\n"
     "addEventListener('marquee:taste-changed', () => { S.home = null; S.picks = undefined; S.discoverAi = undefined;"
     " if (S.route === 'home') route(); });"),
    # 7. learn from a finished play right away
    ("if (name === 'playback-ended') {",
     "if (name === 'playback-ended') { fetch('/api/taste/update', { method: 'POST' }).catch(() => {});"),
    # 8. a Home row ordered by the taste profile
    ("  addPicksRow(rows, seq);\n",
     "  addPicksRow(rows, seq);\n"
     "  api('/api/taste/home-row').then(r => { if (stale(seq) || !r.row || !document.contains(rows) || $('[data-key=\"taste\"]', rows)) return;"
     " const tmp = document.createElement('div'); tmp.innerHTML = rowHTML(r.row, 0); const el = tmp.firstElementChild;"
     " const top = $$('.row', rows).filter(x => ['continue', 'nextup'].includes(x.dataset.key)); const after = top[top.length - 1];"
     " if (after) after.after(el); else rows.prepend(el); watchRows(rows); }, () => {});\n"),
]
missing = []


def patched(text, patches, label):
    for old, new in patches:
        if old in text:
            text = text.replace(old, new, 1)
        else:
            msg = f"{label}: anchor not found: {old[:70]!r}"
            if msg not in missing:
                missing.append(msg)
                print("HARNESS", msg)
    return text


@server.app.get("/harness")
def harness():
    html = patched((WEB / "index.html").read_text(encoding="utf-8"), INDEX_PATCHES, "index.html")
    from flask import request
    if request.args.get("notopbar"):  # narrow-width checks of these pages alone (the top bar is the UI agent's)
        html = html.replace("</head>", "<style>.topbar{display:none!important}</style></head>")
    if missing:
        html = html.replace("</body>", "<script>console.warn(%r)</script></body>" % "\n".join(missing))
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-cache"})


@server.app.get("/harness-app.js")
def harness_app():
    js = patched((WEB / "app.js").read_text(encoding="utf-8"), APP_PATCHES, "app.js")
    return Response(js, mimetype="text/javascript", headers={"Cache-Control": "no-cache"})


class NoPlayer:
    playing = False
    mpv = None
    on_credits = []
    on_state = []

    def play(self, item_id, start=None, card=None):
        print("would play", item_id, start)

    def play_file(self, card, start=None):
        print("would play file", card.get("path"), start)

    def to_mini(self):
        pass

    def restore_full(self):
        pass

    def settings_changed(self):
        pass


server.app.register_blueprint(taste_bp)
app = App()
app.player = NoPlayer()
app.notify_ui = lambda *a, **k: None
app.arrivals = None
server.APP = app
port = int(os.environ.get("MARQUEE_DEV_PORT", "8600"))
print(f"http://127.0.0.1:{port}/harness")
server.serve(port)
