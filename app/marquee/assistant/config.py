"""The assistant's own settings, kept under one "assistant" key in Marquee's settings.json."""
from .. import settings

DEFAULTS = {
    "voice": "off",            # off | push_to_talk | wake_word   (off by default: no microphone is opened)
    "hotkey": "f8",            # push-to-talk key, held while speaking (or tapped: listens for one sentence)
    "wake_engine": "whisper",  # whisper (VAD-gated speech recognition, matches "Hey Marquee") | openwakeword
    "wake_model": "",          # openWakeWord model name or .onnx/.tflite path (empty: hey_jarvis, its stock model)
    "wake_phrases": ["hey marquee", "hey marky", "hey markee", "a marquee", "hey marquis", "hey mark e", "okay marquee"],
    "stt_model": "base",       # faster-whisper size: tiny | base | small
    "stt_device": "auto",      # auto (CUDA when it works, else CPU) | cuda | cpu
    "language": "en",
    "vad": 2,                  # webrtcvad aggressiveness 0-3
    "exclude_devices": ["steam streaming", "stereo mix", "what u hear", "loopback", "cable output", "voicemeeter",
                        "game capture", "line in", "line (", "analog connector"],
    "devices": [],             # if not empty: only input devices whose name contains one of these
    "vision_model": "",        # Ollama / API model for looking at the screen (empty: best installed vision model)
}


def load():
    raw = settings.load().get("assistant") or {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


def save(changes):
    cur = load()
    cur.update({k: v for k, v in (changes or {}).items() if k in DEFAULTS})
    settings.save({"assistant": cur})
    return cur
