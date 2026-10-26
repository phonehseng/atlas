"""Probe: mpv video in a native window with a transparent WebView2 overlay window on top (glass controls).
Takes a screenshot after a few seconds so the composition can be checked."""
import ctypes
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = os.path.join(HERE, "..", "vendor", "mpv") + os.pathsep + os.environ["PATH"]
import mpv  # noqa: E402
import webview  # noqa: E402

VIDEO = sys.argv[1]
SHOT = sys.argv[2]
X, Y, W, H = 80, 80, 1280, 720

HTML = """<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:transparent;font:15px "Segoe UI",sans-serif;color:#fff;overflow:hidden}
.bar{position:absolute;left:24px;right:24px;bottom:24px;height:84px;border-radius:24px;
 background:linear-gradient(140deg,#14203f99,#23408f66);border:1px solid #ffffff55;
 box-shadow:inset 0 1px 0 #ffffff88,0 12px 30px #0006;display:flex;align-items:center;gap:16px;padding:0 24px;
 backdrop-filter:blur(22px) saturate(1.8)}
.pill{padding:10px 18px;border-radius:999px;background:#ffffff26;border:1px solid #ffffff55}
.track{flex:1;height:6px;border-radius:9px;background:#ffffff40}.track i{display:block;width:35%;height:100%;border-radius:9px;background:#ff8a1f}
</style></head><body><div class="bar"><span class="pill">▶ Play</span><div class="track"><i></i></div><span class="pill">4K · RTX VSR</span></div></body></html>"""


def main(window):
    print("gui up", flush=True)
    time.sleep(1.5)
    player = mpv.MPV(vo="gpu-next", gpu_api="d3d11", hwdec="d3d11va", mute="yes", osc="no", border="no",
                     geometry=f"{W}x{H}+{X}+{Y}", keep_open="yes", ontop="yes", title="probe-video")
    print("mpv created", flush=True)
    player.play(VIDEO)
    time.sleep(4)
    print("playing", player.time_pos, flush=True)
    player.seek(330)
    time.sleep(2)
    window.move(X, Y)
    window.resize(W, H)
    window.on_top = True
    form = window.native
    hwnd = form.Handle.ToInt64()
    import clr  # noqa: F401
    from System import Action
    from System.Drawing import Color

    def style():
        key = Color.FromArgb(255, 1, 2, 3)
        form.BackColor = key
        form.TransparencyKey = key
        form.webview.DefaultBackgroundColor = Color.Transparent

    form.Invoke(Action(style))

    class MARGINS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
    time.sleep(3)
    from PIL import ImageGrab
    player.pause = True
    time.sleep(1)
    ImageGrab.grab(bbox=(X, Y, X + W, Y + H), all_screens=True).save(SHOT)
    window.hide()
    time.sleep(1)
    ImageGrab.grab(bbox=(X, Y, X + W, Y + H), all_screens=True).save(SHOT.replace(".png", "-bare.png"))
    print("saved", SHOT)
    player.terminate()
    window.destroy()


ctypes.windll.shcore.SetProcessDpiAwareness(2)
win = webview.create_window("probe-overlay", html=HTML, transparent=True, frameless=True, width=W, height=H,
                            x=X, y=Y, on_top=True)
webview.start(main, win)
