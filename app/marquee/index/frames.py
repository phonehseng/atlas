"""Frame sampling with ffmpeg: one small RGB frame every SAMPLE_EVERY seconds plus scene-change times, decoded
on the GPU when possible (CUDA/NVDEC, then D3D11VA, then software). HDR is tone-mapped to SDR after the
picture has been shrunk, so it costs next to nothing."""
import json
import os
import re
import subprocess
import threading

from .deps import np


SAMPLE_EVERY = 2.0      # seconds between sampled frames
SCENE_THRESHOLD = 0.3   # ffmpeg scene score for a cut
W, H = 384, 216         # decode size (the model sees 224x224 crops of the picture area)
SIZE = 224
NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000
FLAGS = NO_WINDOW | BELOW_NORMAL if os.name == "nt" else 0

_PTS = re.compile(rb"pts_time:\s*([0-9.]+)")


class DecodeError(RuntimeError):
    pass


class Decoder:
    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg
        probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        self.ffprobe = probe if os.path.isfile(probe) else None
        out = self._run([ffmpeg, "-hide_banner", "-filters"]) + self._run([ffmpeg, "-hide_banner", "-hwaccels"])
        self.filters = set(re.findall(r"^\s*\S+\s+(\w+)\s", out, re.M))
        self.hwaccels = set(out.split())
        self.modes = [m for m in ("cuda", "d3d11va") if m in self.hwaccels] + ["sw"]
        if "scale_cuda" not in self.filters and "cuda" in self.modes:
            self.modes.remove("cuda")
        self._good = {}  # (codec, hdr) -> mode that worked
        self.last_mode = None

    @staticmethod
    def _run(cmd, timeout=30):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=FLAGS if os.name == "nt" else 0,
                                  text=True, errors="ignore").stdout
        except Exception:
            return ""

    # ------------------------------------------------------------ probe
    def probe(self, src):
        info = {"duration": 0.0, "codec": "", "hdr": False, "width": 0, "height": 0}
        if self.ffprobe:
            out = self._run([self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name,width,height,color_transfer,duration:format=duration", "-of", "json", src],
                            timeout=60)
            try:
                d = json.loads(out)
                s = (d.get("streams") or [{}])[0]
                info.update(codec=s.get("codec_name", ""), width=s.get("width", 0), height=s.get("height", 0),
                            hdr=s.get("color_transfer") in ("smpte2084", "arib-std-b67"),
                            duration=float(d.get("format", {}).get("duration") or s.get("duration") or 0))
                return info
            except Exception:
                pass
        p = subprocess.run([self.ffmpeg, "-hide_banner", "-i", src], capture_output=True, text=True, errors="ignore",
                           creationflags=FLAGS)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
        if m:
            info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        v = re.search(r"Video: (\w+).*?(\d{3,5})x(\d{3,5})", p.stderr)
        if v:
            info.update(codec=v[1], width=int(v[2]), height=int(v[3]))
        info["hdr"] = "smpte2084" in p.stderr or "arib-std-b67" in p.stderr
        return info

    # ------------------------------------------------------------ filter chains
    def _sw_tonemap(self):
        if "tonemapx" in self.filters:  # jellyfin-ffmpeg
            return "tonemapx=tonemap=bt2390:t=bt709:m=bt709:p=bt709:format=yuv420p"
        if "zscale" in self.filters and "tonemap" in self.filters:
            return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        return "format=yuv420p"

    def _chain(self, mode, hdr, w, h):
        """(input args, filter prefix) giving w x h frames in system memory."""
        if mode == "cuda":
            if hdr and "tonemap_cuda" in self.filters:
                f = (f"scale_cuda=w={w}:h={h},tonemap_cuda=format=nv12:tonemap=bt2390:t=bt709:m=bt709:p=bt709,"
                     "hwdownload,format=nv12")
            elif hdr:
                f = f"scale_cuda=w={w}:h={h}:format=p010le,hwdownload,format=p010le,{self._sw_tonemap()}"
            else:
                f = f"scale_cuda=w={w}:h={h}:format=nv12,hwdownload,format=nv12"
            return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], f
        f = f"scale={w}:{h}:flags=bilinear" + (f",{self._sw_tonemap()}" if hdr else "")
        return (["-hwaccel", "d3d11va"] if mode == "d3d11va" else []), f

    def _order(self, info):
        good = self._good.get((info["codec"], info["hdr"]))
        return [good] + [m for m in self.modes if m != good] if good else list(self.modes)

    # ------------------------------------------------------------ sampling
    def chunk(self, src, info, start, length, stop=None, on_proc=None):
        """Decode [start, start+length): returns (frames uint8 [n,H,W,3], cut times). None if stopped."""
        last = None
        for mode in self._order(info):
            try:
                res = self._chunk(mode, src, info, start, length, stop, on_proc)
            except DecodeError as e:
                last = e
                continue
            if res is not None and len(res[0]) == 0 and start + 1 < info["duration"] - 1:
                last = DecodeError(f"{mode}: no frames")
                continue
            self._good[(info["codec"], info["hdr"])] = mode
            self.last_mode = mode
            return res
        raise last or DecodeError("could not decode")

    def _chunk(self, mode, src, info, start, length, stop, on_proc):
        inp, prefix = self._chain(mode, info["hdr"], W, H)
        graph = (f"[0:v]{prefix},split=2[a][b];[a]fps=1/{SAMPLE_EVERY:g}:round=near,format=rgb24[o1];"
                 f"[b]select='gt(scene\\,{SCENE_THRESHOLD})',showinfo[o2]")
        cmd = [self.ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info", *inp,
               "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src, "-an", "-sn", "-dn",
               "-filter_complex", graph, "-map", "[o1]", "-f", "rawvideo", "pipe:1", "-map", "[o2]", "-f", "null", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             creationflags=FLAGS)
        if on_proc:
            on_proc(p)
        cuts, err = [], []

        def read_err():
            for line in p.stderr:
                if b"showinfo" in line:
                    m = _PTS.search(line)
                    if m:
                        cuts.append(start + float(m[1]))
                else:
                    err.append(line)
                    del err[:-30]
        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        size = W * H * 3
        frames = []
        while True:
            if stop is not None and stop():
                p.kill()
                p.wait()
                return None
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            frames.append(np.frombuffer(buf, np.uint8).reshape(H, W, 3))
        p.wait()
        t.join(5)
        if p.returncode != 0 and not frames:
            raise DecodeError(f"{mode}: " + b"".join(err[-5:]).decode(errors="ignore").strip()[-400:])
        arr = np.stack(frames) if frames else np.zeros((0, H, W, 3), np.uint8)
        return arr, cuts

    def frame_jpeg(self, src, info, t, width=320):
        """One picture at time t as JPEG bytes (thumbnails)."""
        width = int(width) // 2 * 2
        height = max(2, int(round(width * (info.get("height") or 9) / max(info.get("width") or 16, 1) / 2)) * 2)
        for mode in self._order(info):
            inp, prefix = self._chain(mode, info["hdr"], width, height)
            cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *inp, "-ss", f"{max(t, 0):.3f}",
                   "-i", src, "-an", "-sn", "-dn", "-frames:v", "1", "-vf", prefix + ",format=yuvj420p",
                   "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
            p = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=FLAGS)
            if p.returncode == 0 and p.stdout:
                return p.stdout
        raise DecodeError("thumbnail failed")


def prepare(frames):
    """Crop letterbox/pillarbox bars (found over the whole chunk) and resize to the model's 224x224.
    Returns (uint8 [n,224,224,3], luma uint8 [n])."""
    from PIL import Image
    n = len(frames)
    if not n:
        return np.zeros((0, SIZE, SIZE, 3), np.uint8), np.zeros(0, np.uint8)
    lum = frames[::max(1, n // 40)].mean(axis=3)
    rows = np.percentile(lum.mean(axis=2), 90, axis=0) > 18
    cols = np.percentile(lum.mean(axis=1), 90, axis=0) > 18
    y0, y1 = (np.argmax(rows), H - np.argmax(rows[::-1])) if rows.any() else (0, H)
    x0, x1 = (np.argmax(cols), W - np.argmax(cols[::-1])) if cols.any() else (0, W)
    if (y1 - y0) < H * 0.4 or (x1 - x0) < W * 0.4:
        y0, y1, x0, x1 = 0, H, 0, W
    out = np.empty((n, SIZE, SIZE, 3), np.uint8)
    for i in range(n):
        out[i] = np.asarray(Image.fromarray(frames[i, y0:y1, x0:x1]).resize((SIZE, SIZE), Image.BICUBIC))
    luma = frames[:, y0:y1, x0:x1].mean(axis=(1, 2, 3)).astype(np.uint8)
    return out, luma
