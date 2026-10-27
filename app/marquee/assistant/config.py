"""The assistant's own settings, kept under one "assistant" key in Atlas's settings.json."""
from .. import settings

DEFAULTS = {
    "voice": "off",            # off | push_to_talk | wake_word   (off by default: no microphone is opened)
    "hotkey": "f8",            # push-to-talk key, held while speaking (or tapped: listens for one sentence)
    "wake_engine": "whisper",  # whisper (VAD-gated speech recognition, matches "Hey Atlas") | openwakeword
    "wake_model": "",          # openWakeWord model name or .onnx/.tflite path (empty: hey_jarvis, its stock model)
    "wake_phrases": ["hey atlas", "hi atlas", "okay atlas", "hey atlus", "hey atlis", "hey at last", "hey outlast"],
    "stt_model": "base",       # faster-whisper size: tiny | base | small
    "stt_device": "auto",      # auto (CUDA when it works, else CPU) | cuda | cpu
    "language": "en",
    "vad": 2,                  # webrtcvad aggressiveness 0-3
    "exclude_devices": ["steam streaming", "stereo mix", "what u hear", "loopback", "cable output", "voicemeeter",
                        "game capture", "line in", "line (", "analog connector"],
    "devices": [],             # if not empty: only input devices whose name contains one of these
    "vision_model": "",        # Ollama / API model for looking at the screen (empty: best installed vision model)
}


# The wake phrases before the app was renamed; settings that still hold exactly these get the new ones.
OLD_WAKE_PHRASES = ["hey marquee", "hey marky", "hey markee", "a marquee", "hey marquis", "hey mark e", "okay marquee"]


def load():
    raw = settings.load().get("assistant") or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if out["wake_phrases"] == OLD_WAKE_PHRASES:
        out["wake_phrases"] = list(DEFAULTS["wake_phrases"])
    return out


def save(changes):
    cur = load()
    cur.update({k: v for k, v in (changes or {}).items() if k in DEFAULTS})
    settings.save({"assistant": cur})
    return cur
