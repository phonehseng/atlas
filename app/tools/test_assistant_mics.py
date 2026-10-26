"""Microphone check for the assistant: lists every input device, opens the ones it would use all at once, listens
for at most 2 seconds and prints level / voice activity per device. Audio stays in memory and is discarded.
Run: .venv\\Scripts\\python tools\\test_assistant_mics.py [--stt]   (--stt: also check speech recognition with a
synthetic Windows voice sentence, generated in memory)"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MARQUEE_HOME", str(ROOT / ".devhome"))

import sounddevice as sd  # noqa: E402

from marquee.assistant import config, intents  # noqa: E402
from marquee.assistant.voice import Mic, list_inputs  # noqa: E402


def mics():
    cfg = config.load()
    print("all input endpoints Windows reports:")
    apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i:2}] {apis[d['hostapi']]['name']:20} {d['name']}")
    chosen = list_inputs(cfg)
    print(f"\nthe assistant opens {len(chosen)} (one per microphone, exclusions: {', '.join(cfg['exclude_devices'][:4])}...):")
    opened = []
    t0 = time.perf_counter()
    for info in chosen:
        try:
            opened.append(Mic(info, cfg["vad"]))
        except Exception as e:
            print(f"  could not open {info['name']}: {e}")
    print(f"  opened {len(opened)} streams in {int((time.perf_counter() - t0) * 1000)} ms; listening 2 s...")
    time.sleep(2.0)
    for m in opened:
        fr = m.recent(1000)
        voiced = sum(1 for x in fr if x[3])
        peak = max((x[2] for x in fr), default=0)
        print(f"  {m.name[:52]:52} frames {len(fr):3}  voiced {voiced:3}  rms avg {sum(x[2] for x in fr) / max(1, len(fr)):7.1f}"
              f"  peak {peak:7.1f}  floor {m.floor:6.1f}")
        m.close()  # drops the buffered audio
    return opened


def stt_check():
    """Synthesize a command with Windows speech into memory, transcribe it, parse it."""
    import clr  # pythonnet
    clr.AddReference("System.Speech, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35")
    from System.IO import MemoryStream
    from System.Speech.AudioFormat import AudioBitsPerSample, AudioChannel, SpeechAudioFormatInfo
    from System.Speech.Synthesis import SpeechSynthesizer
    import numpy as np

    from marquee.assistant import stt
    eng = stt.engine()
    t0 = time.perf_counter()
    eng.load(wait=True)
    print(f"\nspeech model: {eng.status()} (load {int((time.perf_counter() - t0) * 1000)} ms)")
    synth = SpeechSynthesizer()
    for sentence in ["Hey Marquee, skip forward thirty seconds.", "Turn on the subtitles.",
                     "What did that character just say?", "Play the next episode."]:
        ms = MemoryStream()
        synth.SetOutputToAudioStream(ms, SpeechAudioFormatInfo(16000, AudioBitsPerSample.Sixteen, AudioChannel.Mono))
        synth.Speak(sentence)
        audio = np.frombuffer(bytes(ms.ToArray()), dtype=np.int16).astype(np.float32) / 32768.0
        text, ms_ = eng.transcribe(audio)
        print(f"  said {sentence!r:46} heard {text!r:46} {ms_:4} ms -> {(intents.parse(text) or {}).get('intent')}")
        del audio


if __name__ == "__main__":
    mics()
    if "--stt" in sys.argv:
        stt_check()
