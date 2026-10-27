"""Fast, rule-based reading of what was said: the common player commands need no language model.

parse(text) returns {"intent": name, ...arguments} or None (then the language model gets a go). Everything here is
plain regular expressions, so it answers in well under a millisecond."""
import re

WAKE = re.compile(r"^\s*(?:(?:hey|hi|ok|okay|a|hay)[\s,]+)?(?:marquee|marky|markee|marquis|marquie|mark e)\b[\s,.!?]*", re.I)
FILLER = re.compile(r"^(?:(?:please|can you|could you|would you|will you|would you mind|i want to|i wanna|i'd like to|"
                    r"let's|lets|just|now|ok|okay|go ahead and|um+|uh+|so)\s+)+", re.I)

NUM_WORDS = {
    "zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "couple": 2,
    "few": 3, "half": 0.5, "quarter": 0.25,
}
UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")"
UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|h|m|s)"
# "30 seconds", "a minute and a half", "one and a half minutes", "1 hour 20 minutes", "1:30", "90"
DUR = (rf"(?:\d{{1,2}}:\d{{2}}(?::\d{{2}})?|(?:(?:{NUM}(?:[\s-]+{NUM})?)(?:\s+and\s+a\s+half)?\s*{UNIT}"
       rf"(?:\s+and\s+a\s+half)?(?:\s*(?:and\s+)?{NUM}(?:[\s-]+{NUM})?\s*{UNIT})*)|half\s+(?:a|an)\s+{UNIT}|\d+)")

LANGS = {
    "english": "eng", "spanish": "spa", "french": "fre", "german": "ger", "japanese": "jpn", "italian": "ita",
    "portuguese": "por", "korean": "kor", "chinese": "chi", "mandarin": "chi", "cantonese": "chi", "russian": "rus",
    "hindi": "hin", "arabic": "ara", "dutch": "dut", "swedish": "swe", "polish": "pol", "turkish": "tur",
    "thai": "tha", "vietnamese": "vie", "khmer": "khm", "danish": "dan", "norwegian": "nor", "finnish": "fin",
    "greek": "gre", "hebrew": "heb", "indonesian": "ind", "tagalog": "tgl", "czech": "cze", "hungarian": "hun",
}
LANG = "(" + "|".join(LANGS) + ")"
SUBS = r"(?:subtitles?|subs|captions?|closed captions?|cc)"
GENREISH = re.compile(r"\b(something|anything|funny|comed|hilarious|laugh|scary|horror|spooky|creepy|romantic|romance|"
                      r"action|thriller|sci-?fi|science fiction|documentar|animated|cartoon|kids|family|drama|sad|"
                      r"mystery|crime|war|western|fantasy|adventure|musical|feel-?good|light|chill|relaxing|"
                      r"exciting|recommend|suggest|good (movie|show|film)|(a|some) (movie|show|film|series))\b", re.I)


def _num(tok):
    tok = tok.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    parts = re.split(r"[\s-]+", tok)
    if all(p in NUM_WORDS for p in parts):
        return sum(NUM_WORDS[p] for p in parts)  # "twenty five" -> 25
    return None


def duration(text):
    """Seconds in a spoken duration, or None. '30 seconds', 'a minute and a half', '1:30', '2 and a half minutes'."""
    t = (text or "").lower().strip()
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        return a * 3600 + b * 60 + int(c) if c else a * 60 + b
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return float(t)
    m = re.fullmatch(rf"half\s+(?:a|an)\s+({UNIT})", t)
    if m:
        return 0.5 * UNITS[m.group(1)]
    total, found = 0.0, False
    for m in re.finditer(rf"({NUM}(?:[\s-]+{NUM})?)(\s+and\s+a\s+half)?\s*({UNIT})\b(\s+and\s+a\s+half)?", t):
        n = _num(m.group(1))
        if n is None:
            continue
        if m.group(2) or m.group(4):
            n += 0.5
        total += n * UNITS[m.group(3)]
        found = True
    return total if found else None


def clean(text):
    t = (text or "").strip()
    t = WAKE.sub("", t)
    t = re.sub(r"[’`]", "'", t)
    t = re.sub(r"[!?,;\"]+", " ", t)
    t = re.sub(r"\.(?!\d)", " ", t)  # keep 1.5x
    t = re.sub(r"\s+", " ", t).strip().lower()
    for _ in range(3):
        t = FILLER.sub("", t)
    t = re.sub(r"\s+(please|for me|now|thanks|thank you)$", "", t)
    return t.strip()


def _amount(word, base):
    if word and re.search(r"\b(lot|much|way)\b", word):
        return base * 2
    if word and re.search(r"\b(bit|little|tad|touch|slightly)\b", word):
        return base // 2
    return base


def parse(text):
    t = clean(text)
    if not t:
        return None
    r = _parse(t)
    if r:
        r["text"] = t
    return r


def _parse(t):  # noqa: C901 - one flat table of rules reads best
    # -------- questions answered from subtitles / metadata (checked first: they contain command words)
    m = re.search(rf"\b(?:summari[sz]e|recap|sum up|catch me up on|what happened in|what's happened in)\b"
                  rf"(?:\s+(?:what happened|the))?(?:\s+(?:in|over|during|for))?(?:\s+the)?(?:\s+(?:last|past|previous))"
                  rf"\s+({DUR}|few minutes|couple (?:of )?minutes)", t)
    if m:
        d = duration(m.group(1)) or (180 if "few" in m.group(1) else 120 if "couple" in m.group(1) else None)
        return {"intent": "summarize", "seconds": min(max(d or 300, 30), 3600)}
    if re.search(r"\b(summari[sz]e|recap|sum up|catch me up|what happened so far|what have i missed|what did i miss)\b", t):
        so_far = bool(re.search(r"so far|from the (start|beginning)|whole|everything", t))
        if re.search(r"what did i miss|what have i missed", t) and not so_far:
            return {"intent": "summarize", "seconds": 120}
        return {"intent": "summarize", "seconds": 1800 if so_far else 300, "so_far": so_far}
    if re.search(r"\bwhat (?:did|was|were|is) (?:he|she|they|that|it|the \w+|that \w+|this \w+|\w+) (?:just )?"
                 r"(?:say|said|saying|talking about)\b|\bwhat was that( line)?$|\bwhat did (?:he|she|they) just say|"
                 r"\b(?:repeat|say) (?:that|the last line|what (?:he|she|they) said)(?: again)?\b|\bi didn't (?:hear|catch) "
                 r"(?:that|what)|\bwhat'd (?:he|she|they) say", t):
        return {"intent": "what_said", "seconds": 20}
    # -------- looking at the picture / listening ahead (answered in the background, see atlas.py)
    if re.search(r"\bjump ?scares?\b|\b(?:is|does|will) (?:it|this|something|anything) (?:get|going to get|gonna get|"
                 r"about to get) (?:loud|louder|scary|scarier)\b|\bloud (?:part|bit|moment|scene|noise|bang)s?\b|"
                 r"\bvolume spikes?\b|\bsomething (?:going to|gonna|about to) jump out\b|\bscary (?:part|bit|moment)s? "
                 r"(?:coming|soon|ahead|next)\b|\bany (?:loud|scary|sudden) (?:parts|bits|moments|noises)\b|"
                 r"\bis (?:it|something) going to scare me\b|\bshould i turn (?:it|the volume) down\b", t):
        return {"intent": "loud_soon"}
    m = re.search(r"\b(?:is|was|isn't|wasn't) (?:that|this|he|she) (?:the same (?:guy|actor|actress|woman|man|girl|person|"
                  r"one|dude|lady|kid) |the (?:guy|actor|actress|woman|man|girl|dude|lady|one|kid) )(?:from|in|who was in|"
                  r"who played in|that was in) (?:the )?(.+)$", t) or \
        re.search(r"\b(?:is|was|isn't|wasn't) (?:he|she) (?:in|from) (?:the )?(.+)$", t) or \
        re.search(r"\b(?:didn't|did|wasn't|was) (?:he|she|that (?:guy|actor|actress)) (?:play|star|act|appear) in (?:the )?(.+)$", t) or \
        re.search(r"\bsame (?:guy|actor|actress|woman|man|person|dude|lady) (?:from|as in|in|who was in) (?:the )?(.+)$", t)
    if m:
        return {"intent": "same_as", "other": re.sub(r"\s+(?:right|too|as well|before)$", "", m.group(1)).strip()}
    if re.search(r"\bwhere (?:have|do|did) i (?:seen|see|know|recognize) (?:him|her|them|that|this)\b|\bwhat else (?:has|was|is) "
                 r"(?:he|she|that (?:guy|actor|actress)|this (?:guy|actor|actress)) (?:been )?in\b|\bwhy do i recognize\b|"
                 r"\bwhat (?:else )?do i know (?:him|her) from\b", t):
        return {"intent": "where_seen"}
    if re.search(r"\bwho(?:'s| is| are) (?:that|this|those|these)(?: (?:actor|actress|guy|woman|man|girl|person|dude|lady|"
                 r"people|kid))? (?:on (?:my |the )?screen|right now|here|in this (?:shot|scene|frame))\b|"
                 r"\bwho(?:'s| is| are) on (?:my |the )?screen\b|\bwho am i looking at\b", t):
        return {"intent": "who_screen"}
    if re.search(r"\bwhat(?:'s| is| are) (?:that|this|those|these) (?:thing|object|building|place|car|animal|creature|logo|"
                 r"monster|weapon|ship|song)s?\b|\bwhat am i looking at\b|\bdescribe (?:the |this )?(?:scene|screen|picture|"
                 r"frame|shot)\b|\bwhat(?:'s| is| are) (?:he|she|they) (?:holding|wearing|doing|eating|reading|looking at|"
                 r"drinking|driving)\b|\bwhere (?:is|are) (?:this|they|we|he|she) (?:right now)?$|\bwhat(?:'s| is) on "
                 r"(?:the |my )?screen\b|\blook at (?:this|the screen)\b|\bwhat(?:'s| is) (?:happening|going on)"
                 r"(?: here| right now| on screen)?$|\bwhat's that$|\bwhat is that$", t):
        return {"intent": "look"}
    m = re.search(r"^when (?:does|do|will|is|are) (?!this\b|it\b)(.+?)(?: (?:happen|happens|come|come up|show up|appear|"
                  r"start|scene))?$", t)
    if m and not re.search(r"\b(?:end|ends|finish|finishes|over)$", t):
        return {"intent": "scene_when", "query": m.group(1)}
    m = re.search(r"\bwho (?:plays|played|is playing|portrays|is) (?!that\b|this\b|in\b|the actor|the actress|"
                  r"the guy|the woman|the man|the girl|the person|speaking|talking)(.+)$", t)
    if m and not re.match(r"(he|she|they|it)\b", m.group(1)):
        return {"intent": "cast", "role": m.group(1).strip()}
    if re.search(r"\bwho(?:'s| is| are| was)? (?:that|this|the|those|these)? ?(?:actor|actress|guy|woman|man|girl|"
                 r"boy|character|person|people|cast|speaking|talking)\b|\bwho(?:'s| is| are) in (?:this|it|the (?:movie|"
                 r"show|film|episode))\b|\b(?:show|list|tell me)(?: me)? the cast\b|^cast$|\bwho stars in\b", t):
        return {"intent": "cast"}
    if re.search(r"\bhow (?:much|long)(?: time)?(?: is| has it got| does it have)? (?:left|remaining)\b|\bwhen does (?:this|it|"
                 r"the (?:movie|episode|show|film)) end\b|\btime (?:left|remaining)\b|\bhow long is (?:this|it)\b", t):
        return {"intent": "time_left"}
    if re.search(r"\bwhat(?:'s| is| am i)(?: this| watching| playing| on)\b|\bwhat (?:movie|show|episode|film) is this\b|"
                 r"\bwhat am i watching\b|\bwhich episode\b", t):
        return {"intent": "now_playing"}

    # -------- scene picker / undo (after a scene jump)
    m = re.search(r"^(?:the |pick |choose |play |go with |take )?(?:the )?(first|second|third|1st|2nd|3rd|number (?:one|two|three|"
                  r"1|2|3)|option (?:one|two|three|1|2|3)|#?[123])(?: one| option)?$", t)
    if m:
        w = m.group(1).split()[-1].lstrip("#")
        n = {"first": 1, "1st": 1, "one": 1, "1": 1, "second": 2, "2nd": 2, "two": 2, "2": 2,
             "third": 3, "3rd": 3, "three": 3, "3": 3}.get(w)
        if n:
            return {"intent": "pick", "n": n}
    if re.search(r"^(?:undo(?: that)?|take me back(?: to where i was)?|go back to where i was|back to where i was|"
                 r"wrong (?:one|scene|part)|that's (?:wrong|not it)|not that one)$", t):
        return {"intent": "undo"}

    # -------- scene jumps ("go to the part where spiderman gets shot", "where they kiss happens")
    scene = None
    m = re.search(r"^(?:go|jump|skip|take me|bring me|rewind|seek|fast forward|head|play|start|resume|pick up)?\s*"
                  r"(?:back|forward|ahead|it|playing)?\s*(?:to |from )?(?:the (?:part|scene|bit|moment|point|spot|place) )?"
                  r"(?:where|when)\s+(.+)$", t)
    if m and not re.match(rf"^{DUR}$", m.group(1)) and not (
            t.startswith(("where ", "when ")) and re.match(r"(?:does|do|did|is|are|was|were|will|can|have|has)\b", m.group(1))):
        scene = m.group(1)
    m = m if scene else re.search(r"^(?:find|show me|search for|look for|play|go to|jump to|skip to|take me to|go back to|"
                                  r"rewind to|bring me to|play from|start from|skip ahead to|fast forward to) (?:the|that) "
                                  r"(?:part|scene|bit|moment|point) (?:where|when|with|in which)\s+(.+)$", t)
    if m and not scene:
        scene = m.group(1)
    if not scene:  # without "where": "go to the part percy gives the bolt back", "skip to the bit with the dragon"
        m = re.search(r"^(?:go|jump|skip|take me|bring me|rewind|seek|fast forward|head|play|start|get me)\s*"
                      r"(?:back|forward|ahead)?\s*(?:to|from)\s+(?:the|that)\s+(?:part|scene|bit|moment|point|spot|place)"
                      r"(?:\s+(?:that|with|in which|of|after|before))?\s+(.+)$", t)
        if m and not re.match(rf"^{DUR}$", m.group(1)):
            scene = m.group(1)
    if scene:
        scene = re.sub(r"\s+(?:happens|happened|happen|takes place|is)$", "", scene).strip()
        if scene:
            return {"intent": "scene", "query": scene}

    # -------- seeking
    if re.search(r"\b(start (?:it )?over|restart|from the (?:beginning|start)|go to the (?:beginning|start)|back to the "
                 r"(?:beginning|start))\b", t):
        return {"intent": "seek", "value": 0, "relative": False}
    m = re.search(rf"^(?:go|jump|skip|seek|move|take me|fast forward|rewind)(?: ahead| forward| back)? (?:straight )?to "
                  rf"(?:the )?(?:minute )?({DUR})(?: mark| minute mark| in)?$", t)
    if m:
        v = duration(m.group(1))
        if v is not None:
            return {"intent": "seek", "value": v, "relative": False}
    m = re.search(rf"^(?:go |skip |jump )?({DUR}) (back|backwards?|earlier)$", t)
    if m:
        return {"intent": "seek", "value": -(duration(m.group(1)) or 10), "relative": True}
    m = re.search(rf"^(?:skip |jump |go )?({DUR}) (forward|ahead|later)$", t)
    if m:
        return {"intent": "seek", "value": duration(m.group(1)) or 10, "relative": True}
    m = re.search(rf"\b(?:rewind|go back|back up|jump back|skip back|skip backwards?|go backwards?|back|reverse|seek back)"
                  rf"(?: by)?(?: about)?(?: ({DUR}))?$", t)
    if m:
        return {"intent": "seek", "value": -(duration(m.group(1)) or 10), "relative": True}
    m = re.search(rf"\b(?:skip|skip ahead|skip forward|go forward|go ahead|jump ahead|jump forward|fast forward|forward|"
                  rf"ahead|seek forward|move forward)(?: by)?(?: about)?(?: ({DUR}))?$", t)
    if m and not re.search(r"\bskip (the )?(intro|opening|recap|credits|episode|this episode|song)\b", t):
        return {"intent": "seek", "value": duration(m.group(1)) or 10, "relative": True}
    if re.search(r"\bskip (?:the )?(intro|opening|theme|recap|previously|opening credits|song)\b", t):
        return {"intent": "skip_intro"}
    if re.search(r"\bskip (?:the )?(?:end )?credits\b", t):
        return {"intent": "next_episode"}

    # -------- episodes
    if re.search(r"\b(?:next episode|next one|skip (?:this )?episode|following episode|play the next|go to the next|^next$)", t):
        return {"intent": "next_episode"}
    if re.search(r"\b(?:previous|last|prior) episode\b|\bback (?:an|one) episode\b|^previous$", t):
        return {"intent": "previous_episode"}

    # -------- pause / resume / stop
    if re.search(r"^(?:pause|pause (?:it|this|the (?:movie|show|video|film|episode))|hold (?:on|it|up)|wait|freeze|"
                 r"hang on|pause for a (?:sec|second|moment|minute)|stop for a (?:sec|second|moment|minute)|"
                 r"one (?:sec|second|moment)|stop|stop it)$", t):
        return {"intent": "pause", "mode": "pause"}
    if re.search(r"^(?:resume|unpause|un-pause|continue|keep (?:playing|going)|go on|carry on|play|play it|"
                 r"start (?:playing|it)|play again|resume (?:playing|the (?:movie|show|video|episode)))$", t):
        return {"intent": "pause", "mode": "resume"}
    if re.search(r"^(?:play[\s/]pause|toggle (?:play|pause))$", t):
        return {"intent": "pause", "mode": "toggle"}
    if re.search(r"^(?:stop (?:playing|playback|the (?:movie|show|video|episode|film))|close(?: the)?(?: player| video)?|"
                 r"exit(?: the player)?|quit|i'm done|go (?:back )?(?:to )?(?:the )?(?:home|menu|browse|library))$", t):
        return {"intent": "stop"}

    # -------- volume
    m = re.search(r"(?:set |turn |change )?(?:the )?volume (?:to|at) (\d+|" + NUM + r")(?: percent| %)?$|"
                  r"^volume (\d+)(?: percent)?$", t)
    if m:
        v = _num(m.group(1) or m.group(2))
        if v is not None:
            return {"intent": "volume", "value": int(v), "relative": False}
    if re.search(r"^(?:mute|mute (?:it|the (?:sound|audio|volume))|silence|sound off|quiet|shh+|shush)$", t):
        return {"intent": "mute", "on": True}
    if re.search(r"^(?:unmute|un-mute|sound on|unmute (?:it|the sound))$", t):
        return {"intent": "mute", "on": False}
    m = re.search(r"\b(?:(a (?:lot|bit|little|tad|touch)|way|much|slightly) )?(?:louder|turn (?:it |the volume |the sound )?up|"
                  r"volume up|increase (?:the )?volume|raise (?:the )?volume|crank it|more volume|i can't hear)\b", t)
    if m:
        return {"intent": "volume", "value": _amount(m.group(1) or t, 10), "relative": True}
    m = re.search(r"\b(?:(a (?:lot|bit|little|tad|touch)|way|much|slightly) )?(?:quieter|softer|turn (?:it |the volume |the sound )?"
                  r"down|volume down|decrease (?:the )?volume|lower (?:the )?volume|less volume|too loud)\b", t)
    if m:
        return {"intent": "volume", "value": -_amount(m.group(1) or t, 10), "relative": True}

    # -------- subtitles and audio
    m = re.search(rf"\b{LANG} {SUBS}\b|\b{SUBS} (?:in|to) {LANG}\b", t)
    if m:
        return {"intent": "subtitles", "on": True, "lang": LANGS[m.group(1) or m.group(2)]}
    if re.search(rf"\b(?:turn|switch|put|flip) (?:the )?{SUBS} off\b|\b(?:turn|switch|put) off (?:the )?{SUBS}\b|"
                 rf"\b(?:hide|disable|remove|no|kill|lose) (?:the )?{SUBS}\b|\b{SUBS} off\b", t):
        return {"intent": "subtitles", "on": False}
    if re.search(rf"\b(?:next|other|different|change|switch|cycle)(?: the)? {SUBS}(?: track)?\b", t):
        return {"intent": "subtitles", "on": True, "cycle": True}
    if re.search(rf"\b(?:turn|switch|put|flip) (?:the )?{SUBS} on\b|\b(?:turn|switch|put) on (?:the )?{SUBS}\b|"
                 rf"\b(?:show|enable|display|add|give me|i need|i want) (?:the |some )?{SUBS}\b|\b{SUBS}(?: on)?$", t):
        return {"intent": "subtitles", "on": True}
    m = re.search(rf"\b(?:switch|change|put|set)?(?: it| the audio)?(?: to| in)? ?{LANG}(?: audio| language| dub| track)\b|"
                  rf"\baudio (?:in|to) {LANG}\b|\b(?:in|to) {LANG}$", t)
    if m:
        return {"intent": "audio", "lang": LANGS[m.group(1) or m.group(2) or m.group(3)]}
    if re.search(r"\b(?:dubbed|the dub|english dub)\b", t):
        return {"intent": "audio", "lang": "eng"}
    if re.search(r"\b(?:original (?:audio|language)|the original)\b", t):
        return {"intent": "audio", "lang": "original"}
    if re.search(r"\b(?:next|other|different|change|switch|cycle)(?: the)? audio(?: track)?\b", t):
        return {"intent": "audio", "cycle": True}

    # -------- speed, window
    m = re.search(r"\b(?:play (?:it )?at|speed(?: it)?(?: up)?(?: to)?|set (?:the )?speed to|playback speed(?: to)?)\s*"
                  r"(\d+(?:\.\d+)?|one and a half|double|half)\s*(?:x|times|speed)?\b", t)
    if m:
        v = {"one and a half": 1.5, "double": 2.0, "half": 0.5}.get(m.group(1)) or float(m.group(1))
        return {"intent": "speed", "value": max(0.25, min(4.0, v))}
    if re.search(r"\b(?:normal|regular|real) speed\b|\bspeed (?:back )?to normal\b", t):
        return {"intent": "speed", "value": 1.0}
    if re.search(r"\bdouble speed\b|\b2x\b", t):
        return {"intent": "speed", "value": 2.0}
    if re.search(r"^(?:speed (?:it )?up|faster)$", t):
        return {"intent": "speed", "delta": 0.25}
    if re.search(r"^(?:slow (?:it )?down|slower)$", t):
        return {"intent": "speed", "delta": -0.25}
    if re.search(r"\b(?:exit|leave|close|get out of|turn off|no more) full ?screen\b|\bfull ?screen off\b", t):
        return {"intent": "fullscreen", "on": False}
    if re.search(r"\b(?:go |make it |turn on |enter )?full ?screen\b|\bbigger\b$", t):
        return {"intent": "fullscreen", "on": True}
    if re.search(r"\b(?:picture in picture|pip|mini ?player|small window)\b", t):
        return {"intent": "pip"}

    # -------- find something / play a title
    m = re.search(r"^(?:find|recommend|suggest|show me|give me|search for|look for|i want|i'm in the mood for|"
                  r"in the mood for|what should i watch|anything|something)\b\s*(.*)$", t)
    if m and (GENREISH.search(t) or t.startswith(("recommend", "suggest", "what should"))):
        return {"intent": "find", "query": m.group(1) or t}
    m = re.search(r"^(?:play|watch|put on|start|open|stream|queue up|i want to watch|let's watch|show)\s+(.+)$", t)
    if m:
        title = m.group(1)
        if GENREISH.search(title) and re.search(r"\b(something|anything|some|a (?:\w+ )?(?:movie|show|film))\b", title):
            return {"intent": "find", "query": title}
        season = episode = None
        se = re.search(r"\s*(?:,\s*)?(?:season|series) (\d+|" + NUM + r")(?:,? episode (\d+|" + NUM + r"))?$", title) or \
            re.search(r"\s*s(\d+)\s*e(\d+)$", title)
        if se:
            season = int(_num(se.group(1)) or 1)
            episode = int(_num(se.group(2))) if se.group(2) else None
            title = title[:se.start()].strip()
        else:
            ep = re.search(r"\s*episode (\d+|" + NUM + r")$", title)
            if ep:
                episode = int(_num(ep.group(1)) or 1)
                title = title[:ep.start()].strip()
        title = re.sub(r"^(?:the (?:movie|film|show|series)|movie|film|show)\s+", "", title)
        title = re.sub(r"\s+(?:movie|film|the movie|the show|on marquee)$", "", title).strip()
        if title:
            return {"intent": "play", "title": title, "season": season, "episode": episode}
    return None
