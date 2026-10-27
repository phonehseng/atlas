"""Table test for the assistant's rule-based intent parser. Run: .venv\\Scripts\\python tools\\test_assistant_intents.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marquee.assistant.intents import duration, parse  # noqa: E402

# (phrase, expected intent or None, {argument: expected value})
CASES = [
    ("pause", "pause", {"mode": "pause"}),
    ("Hey Atlas, pause the movie", "pause", {"mode": "pause"}),
    ("hold on", "pause", {"mode": "pause"}),
    ("stop", "pause", {"mode": "pause"}),
    ("resume", "pause", {"mode": "resume"}),
    ("keep playing", "pause", {"mode": "resume"}),
    ("play", "pause", {"mode": "resume"}),
    ("skip forward 30 seconds", "seek", {"value": 30, "relative": True}),
    ("fast forward two minutes", "seek", {"value": 120, "relative": True}),
    ("jump ahead a minute and a half", "seek", {"value": 90, "relative": True}),
    ("go back 10 seconds", "seek", {"value": -10, "relative": True}),
    ("rewind", "seek", {"value": -10, "relative": True}),
    ("rewind thirty seconds", "seek", {"value": -30, "relative": True}),
    ("back 15", "seek", {"value": -15, "relative": True}),
    ("skip ahead", "seek", {"value": 10, "relative": True}),
    ("30 seconds back", "seek", {"value": -30, "relative": True}),
    ("go to 1:23:45", "seek", {"value": 5025, "relative": False}),
    ("jump to the 45 minute mark", "seek", {"value": 2700, "relative": False}),
    ("go to 1 hour 20 minutes", "seek", {"value": 4800, "relative": False}),
    ("start over", "seek", {"value": 0, "relative": False}),
    ("skip the intro", "skip_intro", {}),
    ("louder", "volume", {"value": 10, "relative": True}),
    ("turn it up a little", "volume", {"value": 5, "relative": True}),
    ("a lot louder", "volume", {"value": 20, "relative": True}),
    ("turn it down", "volume", {"value": -10, "relative": True}),
    ("too loud", "volume", {"value": -10, "relative": True}),
    ("set the volume to 40", "volume", {"value": 40, "relative": False}),
    ("volume to fifty percent", "volume", {"value": 50, "relative": False}),
    ("mute", "mute", {"on": True}),
    ("unmute", "mute", {"on": False}),
    ("turn on subtitles", "subtitles", {"on": True}),
    ("Could you turn the captions on please", "subtitles", {"on": True}),
    ("subtitles off", "subtitles", {"on": False}),
    ("turn off the subs", "subtitles", {"on": False}),
    ("spanish subtitles", "subtitles", {"on": True, "lang": "spa"}),
    ("switch to Japanese audio", "audio", {"lang": "jpn"}),
    ("play the next episode", "next_episode", {}),
    ("next episode", "next_episode", {}),
    ("skip this episode", "next_episode", {}),
    ("previous episode", "previous_episode", {}),
    ("play Dune", "play", {"title": "dune"}),
    ("put on The Office season 3 episode 5", "play", {"title": "the office", "season": 3, "episode": 5}),
    ("watch breaking bad s2e4", "play", {"title": "breaking bad", "season": 2, "episode": 4}),
    ("play the movie Interstellar", "play", {"title": "interstellar"}),
    ("find something funny", "find", {}),
    ("recommend a scary movie", "find", {}),
    ("play something relaxing", "find", {}),
    ("I'm in the mood for a romantic comedy", "find", {}),
    ("go back to where they entered the ship", "scene", {"query": "they entered the ship"}),
    ("take me to the part where the dragon appears", "scene", {"query": "the dragon appears"}),
    ("find the scene where they kiss", "scene", {"query": "they kiss"}),
    ("jump to the bit with the flying car", "scene", {"query": "the flying car"}),
    ("What did that character just say?", "what_said", {}),
    ("what did he say", "what_said", {}),
    ("say that again", "what_said", {}),
    ("Summarize the last 5 minutes", "summarize", {"seconds": 300}),
    ("recap the last ten minutes", "summarize", {"seconds": 600}),
    ("what did I miss", "summarize", {"seconds": 120}),
    ("catch me up on what happened so far", "summarize", {"so_far": True}),
    ("Who is that actor?", "cast", {}),
    ("who's in this", "cast", {}),
    ("who plays Paul", "cast", {"role": "paul"}),
    ("how much time is left", "time_left", {}),
    ("when does this end", "time_left", {}),
    ("what am I watching", "now_playing", {}),
    ("play at 1.5x", "speed", {"value": 1.5}),
    ("normal speed", "speed", {"value": 1.0}),
    ("go fullscreen", "fullscreen", {"on": True}),
    ("exit full screen", "fullscreen", {"on": False}),
    ("close the player", "stop", {}),
    # looking at the picture / listening ahead
    ("is there a jumpscare soon", "loud_soon", {}),
    ("does it get loud", "loud_soon", {}),
    ("any loud parts coming up", "loud_soon", {}),
    ("is something going to jump out", "loud_soon", {}),
    ("who's that actor on my screen right now", "who_screen", {}),
    ("who is on the screen", "who_screen", {}),
    ("is that the same guy from walking dead", "same_as", {"other": "walking dead"}),
    ("wasn't she in breaking bad", "same_as", {"other": "breaking bad"}),
    ("is that the actor from the office", "same_as", {"other": "office"}),
    ("where have I seen him before", "where_seen", {}),
    ("what is that thing", "look", {}),
    ("what's he holding", "look", {}),
    ("what's happening", "look", {}),
    ("when does the dragon show up", "scene_when", {"query": "the dragon"}),
    ("when does the movie end", "time_left", {}),
    ("who is that actor", "cast", {}),
    # scene jumps (a seek), the picker and undo
    ("hey atlas go to the part where spiderman gets shot", "scene", {"query": "spiderman gets shot"}),
    ("skip to the scene where the tank gets hit", "scene", {"query": "the tank gets hit"}),
    ("jump to when they eat breakfast", "scene", {"query": "they eat breakfast"}),
    ("rewind to when he says best job I ever had", "scene", {"query": "he says best job i ever had"}),
    ("find the moment when the tiger shows up", "scene", {"query": "the tiger shows up"}),
    ("where the dragon attacks happens", "scene", {"query": "the dragon attacks"}),
    ("play from when they reach the crossroads", "scene", {"query": "they reach the crossroads"}),
    ("take me to the bit where she cries", "scene", {"query": "she cries"}),
    ("where does the dragon appear", None, {}),
    ("the second one", "pick", {"n": 2}),
    ("number 3", "pick", {"n": 3}),
    ("first", "pick", {"n": 1}),
    ("undo", "undo", {}),
    ("take me back to where I was", "undo", {}),
    # left for the language model
    ("why did he leave the ship", None, {}),
    ("is this based on a book", None, {}),
    ("what's the name of the song playing", None, {}),
]

DURATIONS = [("30 seconds", 30), ("a minute", 60), ("1:30", 90), ("two and a half minutes", 150),
             ("1 hour 20 minutes", 4800), ("half a minute", 30), ("twenty five seconds", 25), ("90", 90)]


def main():
    bad = 0
    t0 = time.perf_counter()
    for phrase, want, args in CASES:
        got = parse(phrase)
        name = got["intent"] if got else None
        ok = name == want and all(got.get(k) == v for k, v in args.items())
        bad += not ok
        shown = {k: v for k, v in (got or {}).items() if k not in ("intent", "text")}
        print(f"{'ok  ' if ok else 'FAIL'} {phrase!r:58} -> {name} {shown if shown else ''}"
              + ("" if ok else f"   (wanted {want} {args})"))
    per = (time.perf_counter() - t0) / len(CASES) * 1000
    for text, secs in DURATIONS:
        got = duration(text)
        ok = got == secs
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} duration({text!r}) = {got}" + ("" if ok else f" (wanted {secs})"))
    print(f"\n{len(CASES)} phrasings + {len(DURATIONS)} durations, {bad} failed, {per:.3f} ms per parse")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
