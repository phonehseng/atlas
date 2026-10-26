"""Probe: can this libmpv use NVIDIA RTX Video Super Resolution (d3d11vpp scaling-mode=nvidia)?
Plays a few seconds of a file muted in a small window and prints what mpv reports."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = os.path.join(HERE, "..", "vendor", "mpv") + os.pathsep + os.environ["PATH"]
import mpv  # noqa: E402

path = sys.argv[1]
log = []
player = mpv.MPV(log_handler=lambda lvl, comp, msg: log.append(f"[{lvl}] {comp}: {msg.strip()}"),
                 loglevel="v", vo="gpu-next", gpu_api="d3d11", hwdec="d3d11va", mute="yes",
                 geometry="640x360+40+40", keep_open="yes", osc="no")
print("mpv", player.mpv_version)
player.play(path)
player.wait_until_playing(timeout=20)
time.sleep(2)
print("hwdec:", player.hwdec_current, "| video:", player.video_params.get("w"), "x", player.video_params.get("h"))
player.command("vf", "set", "d3d11vpp=scaling-mode=nvidia:scale=1.334")
time.sleep(4)
print("vf now:", player.vf)
print("output size:", (player.video_out_params or {}).get("w"), "x", (player.video_out_params or {}).get("h"))
player.terminate()
for line in log:
    if any(k in line.lower() for k in ("d3d11vpp", "nvidia", "super", "vf", "filter", "error", "hwdec", "vpp")):
        print(line[:200])
