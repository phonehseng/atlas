"""Marquee's local assistant: voice and text commands for the player, and quick answers about what's playing.

    from .assistant import bp as assistant_bp
    server.app.register_blueprint(assistant_bp)

Commands are understood by rules (intents.py); a language model is used only for questions, summaries and
requests the rules don't understand. Voice (faster-whisper, every microphone, push-to-talk or "Hey Marquee") is
optional and off by default."""
from .blueprint import bp, hook_player, init  # noqa: F401
