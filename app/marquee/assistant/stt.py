"""Speech to text on this PC with faster-whisper (base by default). CUDA (float16, using the
nvidia-cublas-cu12 / nvidia-cudnn-cu12 runtimes) when it works, else the CPU with int8. Audio is handed over in memory and never written anywhere."""
import os
import threading
import time

from .. import settings
from . import config, installer

MODELS_DIR = settings.DIR / "models"
_engine = None


class STT:
    def __init__(self):
        self.model = None
        self.device = None
        self.size = None
        self.pref = None     # the stt_device setting it was loaded with (auto, cuda, cpu)
        self.error = None
        self.loading = False
        self._lock = threading.Lock()
        self._ready = threading.Event()

    @property
    def ready(self):
        return self.model is not None

    def load(self, wait=False):
        cfg = config.load()
        if self.model is not None and (self.size, self.pref) == (cfg["stt_model"], cfg["stt_device"]):
            return True
        with self._lock:
            if self.loading:
                pass
            else:
                self.loading, self.error = True, None
                self._ready.clear()
                threading.Thread(target=self._load, args=(cfg,), daemon=True).start()
        if wait:
            self._ready.wait(900)
        return self.model is not None

    def _load(self, cfg):
        try:
            installer.add_dll_dirs()
            # Everything Hugging Face keeps (model cache, Xet chunk cache) under Atlas's folder; no progress bars
            # (a windowed app has no console) and no warning when Windows can't make symlinks (files get copied).
            os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            import numpy as np
            from faster_whisper import WhisperModel
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            tries = []
            if cfg["stt_device"] in ("auto", "cuda") and (cfg["stt_device"] == "cuda" or _cuda_libs()):
                tries += [("cuda", "float16"), ("cuda", "int8_float16")]
            if cfg["stt_device"] in ("auto", "cpu"):
                tries.append(("cpu", "int8"))
            last, loaded = None, False
            for device, compute in tries:
                if device == "cuda" and last is not None and "cannot be loaded" in str(last):
                    continue   # a CUDA library is missing: a second CUDA try would hang in ctranslate2
                try:
                    m = WhisperModel(cfg["stt_model"], device=device, compute_type=compute,
                                     download_root=str(MODELS_DIR), cpu_threads=4)
                    # CUDA libraries are only loaded on first use: prove it works with a moment of silence
                    list(m.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)[0])
                    self.model, self.device, self.size = m, f"{device}/{compute}", cfg["stt_model"]
                    self.pref, loaded = cfg["stt_device"], True
                    break
                except Exception as e:
                    last = e
                    print(f"speech model on {device} failed:", str(e)[:200])
            if not loaded:
                raise last or RuntimeError("no device")
        except Exception as e:
            self.error = str(e)[:300]
        finally:
            self.loading = False
            self._ready.set()

    def transcribe(self, audio, prompt=None):
        """audio: float32 mono 16 kHz numpy array -> (text, milliseconds)."""
        if self.model is None and not self.load(wait=True):
            raise RuntimeError(self.error or "speech model not ready")
        cfg = config.load()
        t0 = time.perf_counter()
        segs, _info = self.model.transcribe(audio, language=cfg["language"] or None, beam_size=1, best_of=1,
                                            temperature=0.0, condition_on_previous_text=False, vad_filter=False,
                                            without_timestamps=True,
                                            initial_prompt=prompt or "Hey Atlas, pause. Skip forward 30 seconds.")
        text = " ".join(s.text.strip() for s in segs).strip()
        return text, int((time.perf_counter() - t0) * 1000)

    def status(self):
        return {"ready": self.ready, "loading": self.loading, "device": self.device, "model": self.size,
                "error": self.error}


def _cuda_libs():
    """Are the CUDA runtime libraries faster-whisper needs loadable (the gpu part, or a CUDA install)? Without
    them, trying CUDA first fails, and a CUDA retry after that failure can hang inside ctranslate2."""
    import ctypes
    try:
        for name in ("cublas64_12.dll", "cublasLt64_12.dll"):
            ctypes.WinDLL(name)
        return True
    except OSError:
        return False


def selftest(sentence="Hey Atlas, skip forward thirty seconds."):
    """Check speech recognition end to end without a microphone: Windows speech synthesis renders the sentence
    into memory (nothing is played), then it's transcribed. -> {said, heard, ms, device, load_ms}"""
    import clr  # pythonnet
    clr.AddReference("System.Speech, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35")
    from System.IO import MemoryStream
    from System.Speech.AudioFormat import AudioBitsPerSample, AudioChannel, SpeechAudioFormatInfo
    from System.Speech.Synthesis import SpeechSynthesizer
    import numpy as np
    eng = engine()
    t0 = time.perf_counter()
    if not eng.load(wait=True):
        raise RuntimeError(eng.error or "speech model not ready")
    load_ms = int((time.perf_counter() - t0) * 1000)
    synth, ms = SpeechSynthesizer(), MemoryStream()
    try:
        synth.SetOutputToAudioStream(ms, SpeechAudioFormatInfo(16000, AudioBitsPerSample.Sixteen, AudioChannel.Mono))
        synth.Speak(sentence)
        audio = np.frombuffer(bytes(ms.ToArray()), dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        synth.Dispose()
        ms.Dispose()
    text, took = eng.transcribe(audio)
    return {"said": sentence, "heard": text, "ms": took, "device": eng.device, "load_ms": load_ms,
            "audio_ms": int(len(audio) / 16)}


def engine():
    global _engine
    if _engine is None:
        _engine = STT()
    return _engine
