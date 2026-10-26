"""Prepared lines for Classic mode (no AI): the welcome line, title blurbs, ending comments, search answers and the
reasons next to picks. Edit or translate freely; marquee.scripted picks among them.

Placeholders (a line is only used when every placeholder in it is known):
    {name}      viewer's first name            {part}      morning | afternoon | evening | night
    {weekday}   Monday...                      {season}    spring | summer | autumn | winter
    {holiday}   e.g. Halloween                 {days}      "in 5 days" | "tomorrow" | "today"
    {daysn}     "5 days" | "1 day"
    {show}      a show in progress             {se}        e.g. S2E5          {ep}  episode title
    {movie}     a film you're partway through  {pct}       how far in, e.g. 40%
    {count}     how many (new episodes...)     {newshow}   a show with new episodes
    {newmovie}  a newly added film             {pick}      an unwatched, well-rated title
    {genre}     a genre word, e.g. sci-fi      {recent}    the last thing you watched
    {streak}    days in a row you watched      {binge}     episodes watched today
    {title}     the title itself (blurbs, endings)   {because} something you watched
    {actor} {year} {mins} {hours} {rating} {seasons} {series} {next} {n}

Keep lines short (one sentence, max ~110 characters), warm, a little playful, never a spoiler, at most one emoji."""

WELCOME = {
    # ---------------------------------------------------------------- time of day
    "morning": [
        "Morning, {name}. Coffee first, credits later.",
        "Good morning! The projector's warm and the popcorn can wait.",
        "Rise and stream, {name}.",
        "An early show? Bold. We respect it.",
        "Morning. Nobody's judging a breakfast episode.",
        "Good morning, {name}. The couch saved your spot.",
        "A {weekday} morning, and the whole library is wide awake.",
        "Morning! Something light with the toast?",
        "Up early, {name}? So is everything on these shelves.",
        "Good morning. Today's forecast: a strong chance of a good film.",
    ],
    "afternoon": [
        "Good afternoon, {name}. Matinee time.",
        "An afternoon screening, how very civilised.",
        "Afternoon, {name}. The best seats are all free.",
        "A {weekday} afternoon is exactly the right length for a movie.",
        "Hello, {name}. Fancy a matinee?",
        "Afternoon slump? We have a cure, and it has a runtime.",
        "Good afternoon. The lights are down whenever you are.",
        "Afternoon, {name}. One episode counts as a lunch break, right?",
        "The sun's out, the curtains are closed. Perfect.",
        "Hey {name}, the afternoon show is about to start.",
    ],
    "evening": [
        "Good evening, {name}. Showtime.",
        "Evening! Dim the lights, the good stuff's in here.",
        "The evening's yours, {name}. Let's make it a good one.",
        "Evening, {name}. Blanket, snacks, remote: check, check, check.",
        "Welcome back. Tonight's lineup is looking sharp.",
        "A {weekday} evening deserves a proper feature.",
        "Good evening. The marquee is lit and the seats are comfy.",
        "Hey {name}, curtain's up whenever you're ready.",
        "Evening plans: none. Evening plans now: this.",
        "The day's done, {name}. Time for someone else's adventure.",
        "Pull up a seat, {name}. The night is young.",
        "Evening, {name}. Your library missed you.",
    ],
    "night": [
        "Still up, {name}? So are we.",
        "Late show for one? Coming right up.",
        "It's late, {name}. One more episode never hurt anyone. Probably.",
        "Night owl hours. Something cosy, or something wild?",
        "The midnight screening is now seating, {name}.",
        "Can't sleep? Let a good story do the counting.",
        "It's quiet out there. Perfect for a film.",
        "Late night, low volume, big story.",
        "Shh, everyone's asleep. Perfect time for a movie.",
        "The witching hour has excellent picture quality.",
    ],
    # ---------------------------------------------------------------- the day of the week
    "weekend": [
        "It's the weekend, {name}. Movie-marathon rules apply.",
        "Weekend mode: on. Double feature?",
        "No alarms tomorrow? This calls for a long one.",
        "A {weekday} {part} is made for big movies.",
        "Weekend vibes, {name}. The good seats are reserved for you.",
        "Weekend! Tonight's feature is your call.",
        "It's {weekday}, which legally makes this movie night.",
        "Weekend plans: blanket fort, big screen, zero apologies.",
    ],
    "friday": [
        "Friday night! The week is officially over, {name}.",
        "It's Friday. You've earned a big one.",
        "Friday {part}: the best time slot of the week.",
        "TGIF, {name}. Popcorn's on the house.",
    ],
    "monday": [
        "Mondays are better with a good episode.",
        "Made it through Monday, {name}? Reward unlocked.",
        "A Monday {part}. Let's ease into the week gently.",
    ],
    "sunday": [
        "Sunday {part}: the coziest slot in the schedule.",
        "Lazy Sunday? There's a show for that.",
        "Sunday, {name}. Squeeze one more in before the week starts.",
    ],
    "midweek": [
        "Halfway through the week, {name}. Treat yourself.",
        "Hump day, solved: one good episode.",
        "It's {weekday}. Unremarkable day, remarkable library.",
    ],
    # ---------------------------------------------------------------- seasons
    "spring": [
        "Spring {part}, {name}. Fresh season, fresh picks.",
        "Spring is here. The shelves have had a clean too.",
        "Longer days, same comfy couch.",
    ],
    "summer": [
        "Summer {part}, {name}. Blockbuster season is open.",
        "It's summer. Air conditioning and a big movie?",
        "Hot out there. Cool, dark and comfy in here.",
        "Summer nights were made for big-screen adventures.",
    ],
    "autumn": [
        "Autumn {part}. Peak blanket-and-movie season.",
        "Leaves are falling, {name}, and so are your excuses not to watch something.",
        "Cosy season is in full swing.",
        "Autumn evenings pair nicely with a mystery.",
    ],
    "winter": [
        "Winter {part}, {name}. Blanket required.",
        "Cold out there. Warm glow in here.",
        "It's winter, {name}. The couch has never looked better.",
        "Hot chocolate, a heavy blanket and a long film.",
    ],
    # ---------------------------------------------------------------- holidays (any)
    "holiday": [
        "{holiday} is {days}. Planning the watch list yet?",
        "{holiday} {days}, {name}. Something festive, perhaps?",
        "Counting down to {holiday}. The library is ready when you are.",
        "With {holiday} {days}, it's officially themed-movie season.",
    ],
    "holiday_today": [
        "Happy {holiday}, {name}!",
        "It's {holiday}! The marquee is lit for the occasion.",
        "Happy {holiday}. Pick something special tonight.",
    ],
    "halloween": [
        "Halloween is {days}, {name}. Spooky season is officially open.",
        "{daysn} till Halloween. Something with a few jump scares?",
        "Halloween's {days}. Lights low, volume up, if you dare.",
        "It's spooky season, {name}. Leave a light on.",
        "Halloween approaches. The library is getting a little creaky.",
        "Pumpkins out, lights down, Halloween {days}.",
    ],
    "halloween_today": [
        "Happy Halloween, {name}! Pick something that goes bump.",
        "It's Halloween. Tonight, the scary ones get the good seats.",
    ],
    "christmas": [
        "Christmas is {days}, {name}. Blanket, cocoa, a festive classic?",
        "{daysn} till Christmas. The cosy picks are glowing.",
        "It's beginning to look a lot like movie night.",
        "Christmas is {days}. Time for the comfort rewatch tradition.",
    ],
    "christmas_today": [
        "Merry Christmas, {name}! The couch is reserved all day.",
        "Merry Christmas. Something warm and wonderful tonight?",
    ],
    "newyear": [
        "The year's almost done, {name}. Squeeze in one more great film?",
        "New Year's is {days}. Finish that series before the countdown.",
        "Last films of the year, {name}. Make them count.",
    ],
    "valentine": [
        "Valentine's Day is {days}. Date night needs a good film.",
        "Love is in the air {days}. So are rom-coms.",
    ],
    "thanksgiving": [
        "Thanksgiving is {days}. Thankful for a full library.",
        "Thanksgiving {days}, {name}. Something for the whole family?",
    ],
    # ---------------------------------------------------------------- what you're watching
    "next_up": [
        "{se} of {show} is waiting for you, {name}.",
        "{show} is on {se}. Shall we?",
        "Picking up {show}? {se} is cued and ready.",
        "{se} of {show}, \"{ep}\", is next in line.",
        "Your seat for {show} is still warm. {se} is up.",
        "{show}, {se}. The story stopped right where you left it.",
        "Ready for {se} of {show}? It's ready for you.",
        "{show} is calling. {se} picks up right where you left off.",
        "Next stop: {show}, {se}.",
        "{ep}, {se} of {show}, is cued up.",
        "Back to {show}? {se} is waiting.",
        "Welcome back, {name}. {show} {se} is next.",
    ],
    "next_up_late": [
        "One more episode of {show}? {se} says yes.",
        "{show}, {se}. Just one more. We promise not to tell.",
        "It's late, but {se} of {show} is right there.",
        "\"Just one more {show}\", famous last words. {se} is up.",
        "{se} of {show}, then bed. Probably.",
    ],
    "resume": [
        "{movie} is paused at {pct}. Finish it tonight?",
        "You left {movie} at {pct}. The ending's still waiting.",
        "{movie} is mid-story at {pct}. Let's see how it ends.",
        "Still got {movie} at {pct}. Back in?",
        "{movie} has been on pause at {pct}. Hit play?",
    ],
    "new_episodes": [
        "{count} new episodes of {newshow} just landed.",
        "Fresh in: {count} new episodes of {newshow}.",
        "{newshow} has {count} new episodes waiting.",
        "Good news, {name}: {count} new episodes of {newshow}.",
    ],
    "new_episode": [
        "A new episode of {newshow} just arrived.",
        "{newshow} has a new episode. Fresh off the press.",
        "New {newshow} episode spotted in the library.",
    ],
    "new_show": [
        "New show alert: {newshow} just arrived with {count} episodes.",
        "{newshow} moved in: {count} episodes ready to binge.",
        "Fresh arrival: all {count} episodes of {newshow}.",
    ],
    "new_movie": [
        "Just added: {newmovie}. Premiere tonight?",
        "{newmovie} has arrived. Front row's free.",
        "New on the shelf: {newmovie}.",
        "Fresh delivery: {newmovie}. Popcorn ready?",
        "{newmovie} just landed in your library.",
    ],
    "new_many": [
        "{count} new arrivals in the library this week.",
        "The shelves grew: {count} new titles this week.",
        "{count} new things to watch since last week, {name}.",
    ],
    "streak": [
        "{streak} days in a row, {name}. A true regular.",
        "That's a {streak}-day streak. The usher knows your name.",
        "Day {streak} of your streak. Keep the lights low.",
        "{streak} nights running, {name}. Impressive commitment.",
    ],
    "binge": [
        "{binge} episodes today. A fine binge, {name}.",
        "{binge} episodes of {show} today. Who's counting? We are.",
        "{binge} down today. {show} has you hooked.",
    ],
    "back": [
        "Welcome back, {name}. It's been a while.",
        "Long time no see, {name}. The shelves kept your spot.",
        "Hey, you're back! We kept the seat warm.",
        "Good to see you again, {name}.",
    ],
    "first_time": [
        "Welcome to Marquee, {name}. Pick anything, it's all yours.",
        "Welcome! Your library is lit and ready.",
        "First show's on us. Pick anything.",
    ],
    "recent": [
        "After {recent}, how about something new?",
        "Loved {recent}? There's more where that came from.",
        "Fresh off {recent}, {name}? Let's find the next one.",
    ],
    "pick": [
        "Ever seen {pick}? Tonight could be the night.",
        "{pick} has been waiting patiently on the shelf.",
        "Unwatched and highly rated: {pick}.",
        "Psst, {name}. {pick}. Trust us.",
        "{pick} is sitting right there, unwatched. Just saying.",
        "Tonight's suggestion from the projection booth: {pick}.",
        "{pick} is overdue a viewing.",
    ],
    "genre": [
        "Feeling more {genre} tonight, {name}?",
        "You've been on a {genre} streak lately. More?",
        "Your recent picks say {genre}. The library agrees.",
        "{genre} fan spotted. There's plenty left on the shelf.",
    ],
    "anime": [
        "Anime night, {name}? Subtitles or dub, your call.",
        "The anime shelf is looking lively today.",
    ],
    "generic": [
        "Hey {name}. What are we watching?",
        "Welcome back. Your library is all lit up.",
        "Hello again, {name}. Something new or an old favourite?",
        "The show starts whenever you say.",
        "Hi {name}. Your seats are ready.",
        "Grab a snack. We'll wait.",
        "What'll it be tonight: laughs, thrills or tears?",
        "Good to see you, {name}. Shall we find something good?",
        "The house lights are down. Over to you.",
        "Welcome in. No ads, no trailers, just your stuff.",
        "Settle in, {name}. It's showtime.",
        "Hello! Everything's ready, the rest is up to you.",
        "Tonight's headliner: whatever you pick.",
        "Choose wisely. Or choose chaotically. Both work.",
    ],
}

# Short per-title blurbs ("why watch"), built from what's known about a title.
INTRO = {
    "because": [
        "Because you watched {because}.",
        "If you liked {because}, this one's for you.",
        "A natural next step after {because}.",
        "Fans of {because} tend to love this one.",
    ],
    "sequel": [
        "Next in line after {because}.",
        "The story continues from {because}.",
    ],
    "genre": {
        "horror": ["Lights off for this one. Or maybe on.", "Proper scary. Bring a pillow to hide behind."],
        "comedy": ["Guaranteed laughs, or your evening back.", "A good laugh is just a play button away."],
        "scifi": ["Big ideas, bigger worlds.", "Strap in: the future is calling."],
        "fantasy": ["A world full of magic, ready when you are.", "Pack for a long journey into somewhere else."],
        "action": ["Wall-to-wall action. Volume up.", "Buckle up: it moves fast."],
        "adventure": ["A proper adventure, maps and all.", "Grab your gear, this one goes places."],
        "drama": ["The kind of story that stays with you.", "Big feelings, beautifully told."],
        "thriller": ["Edge-of-your-seat stuff.", "Tense in the best way."],
        "mystery": ["A puzzle worth solving.", "Keep your eyes open, the clues are everywhere."],
        "crime": ["Slick, sharp and a little dangerous.", "Crime, done stylishly."],
        "romance": ["A love story to curl up with.", "Swoon-worthy. Tissues optional."],
        "animation": ["Gorgeous animation, for kids and grown-ups alike.", "Every frame is a painting."],
        "family": ["Fun for the whole couch.", "One the whole family can agree on."],
        "documentary": ["True stories hit different.", "Real life, stranger than fiction."],
        "war": ["Gripping and intense, history up close.", "Hard-hitting and unforgettable."],
        "western": ["Saddle up for the frontier.", "Dust, sunsets and showdowns."],
        "music": ["Turn it up, this one sings.", "Made for good speakers."],
        "history": ["History, brought to life.", "A trip back in time."],
        "anime": ["Peak anime energy.", "Subbed or dubbed, it slaps."],
    },
    "runtime_short": ["Only {mins} minutes: an easy yes tonight.", "A quick {mins} minutes. Perfect before bed."],
    "runtime_long": ["A {hours} epic. Snacks are mandatory.", "{hours} of story. Settle in properly."],
    "rating_high": ["Rated {rating}: the crowd has spoken.", "Rated {rating} by viewers. Big fans out there."],
    "rating_good": ["Solidly liked at {rating}.", "Viewers give it a {rating}."],
    "year_new": ["Fresh from {year}.", "Brand new, from {year}."],
    "year_classic": ["A classic from {year}.", "Vintage {year}, and it still holds up."],
    "year_90s": ["Straight out of the 90s.", "Pure {year} vibes."],
    "year_00s": ["A 2000s favourite.", "Peak 2000s, from {year}."],
    "cast": ["Starring {actor}.", "{actor} leads the way.", "With {actor}."],
    "seasons": ["{seasons} seasons to sink into.", "{seasons} seasons, plenty of evenings sorted."],
    "season_one": ["One tidy season, perfect for a weekend.", "Just one season: a perfect binge."],
    "unwatched": ["You haven't seen this one yet.", "Still unwatched. Not for long?"],
    "rewatch": ["A rewatch? Always a good idea.", "Seen it before? Even better the second time."],
    "progress": ["You're {pct} through. The rest is waiting.", "Already {pct} in. Finish strong."],
}

# Comments on the "Up next" screen when a title ends. No spoilers: these never know how it ended.
ENDING = {
    "generic": [
        "That's a wrap on {title}.",
        "And that's {title}. Roll credits.",
        "{title}: done and dusted.",
        "Lights up. That was {title}.",
        "The end of {title}. Stretch those legs.",
        "Another one in the books: {title}.",
        "That was {title}. Take a breath.",
        "Credits rolling on {title}. Good pick.",
    ],
    "episode": [
        "That's {se} of {series} done.",
        "{se} of {series}: wrapped.",
        "Another {series} episode down.",
        "{series}, {se}: in the books.",
        "And that's {se}. {series} keeps rolling.",
        "{se} done. {series} isn't finished with you yet.",
    ],
    "episode_late": [
        "That's {se}. One more, or is it bedtime?",
        "{se} done. Your pillow is also an option.",
        "It's late. One more {series}, or sweet dreams?",
    ],
    "season_finale": [
        "That's the season {season} finale of {series}.",
        "Season {season} of {series}: complete. Well played.",
        "And that closes season {season} of {series}.",
        "Season {season}, done. Time for a breather?",
        "That's a whole season of {series}. Respect.",
    ],
    "series_finale": [
        "That's the very last episode of {series}. What a ride.",
        "And that's all of {series}. Take a moment.",
        "{series}: complete. Every single episode.",
        "The final curtain on {series}. Well watched.",
    ],
    "sequel": [
        "That's {title}. {next} is next in line.",
        "{title}, done. The story carries on in {next}.",
        "Part one of the journey complete. {next} awaits.",
        "{next} picks up from here, whenever you're ready.",
    ],
    "collection_end": [
        "That's the last of the {series} films. All of them!",
        "The whole {series} collection: complete.",
    ],
    "genre": {
        "horror": ["Lights back on? Totally fine. Not scared at all.", "You made it to the credits. Brave.",
                   "Maybe check under the bed before sleep.", "Survived {title}. Now, about that hallway light..."],
        "comedy": ["Hope that one got some laughs.", "Laughter: the best runtime.", "Smiles all round, we hope."],
        "scifi": ["Back to planet Earth now.", "Re-entering normal reality in three, two, one...",
                  "Your brain may need a minute after that one."],
        "fantasy": ["Back from another world.", "The magic lingers a bit, doesn't it?"],
        "action": ["Phew. Catch your breath.", "Pulse back to normal yet?", "That was a lot of action."],
        "adventure": ["What a journey.", "Adventure complete. Boots off."],
        "drama": ["Take a moment with that one.", "Some stories need a minute to settle."],
        "thriller": ["You can unclench now.", "Heart rate returning to normal."],
        "mystery": ["Did you call it before the end?", "Puzzle solved, or still puzzling?"],
        "crime": ["Case closed.", "Crime doesn't pay, but it sure makes good films."],
        "romance": ["Swoon. That was a good one.", "Love, actually, on your screen."],
        "animation": ["Beautifully drawn, start to finish.", "Every frame a painting."],
        "family": ["Fun for the whole couch.", "A good one for everyone."],
        "documentary": ["Learned something new tonight.", "Real life, well told."],
        "war": ["A heavy one. Take a moment.", "Powerful stuff."],
        "western": ["Ride off into the sunset.", "The dust settles."],
        "anime": ["Peak anime moment, done.", "That opening song will be in your head all night."],
    },
    "late": [
        "It's late. Bed is also a great streaming service.",
        "Sleep well when you get there.",
    ],
}

# The answer line for a search in words (Classic mode: no AI, from these and the rules).
SEARCH = {
    "found": [
        "Here's the best of your library for that, top picks first.",
        "Found {n} that fit. The best ones are up front.",
        "{n} matches on your shelves. Front row first.",
        "Good call. Here's what fits, best first.",
    ],
    "genre": {
        "horror": ["{n} scary ones in your library. Lights optional.", "Spooky picks, coming right up."],
        "comedy": ["{n} funny ones ready to go.", "Laughs, served. Best first."],
        "scifi": ["{n} trips to the future, ready for launch.", "Sci-fi, coming right up."],
        "fantasy": ["{n} worlds full of magic.", "Fantasy picks, best first."],
        "action": ["{n} picks with plenty of action.", "Action, sorted. Strap in."],
        "adventure": ["{n} adventures waiting.", "Adventure picks, best first."],
        "drama": ["{n} dramas worth your evening.", "Drama, coming right up."],
        "thriller": ["{n} nail-biters ready.", "Tense picks, best first."],
        "mystery": ["{n} mysteries to solve.", "Mysteries, best first."],
        "crime": ["{n} crime picks, sharp ones first.", "Crime picks, best first."],
        "romance": ["{n} love stories ready.", "Romance, served warm."],
        "animation": ["{n} animated picks.", "Animation, best first."],
        "family": ["{n} for the whole couch.", "Family picks, best first."],
        "documentary": ["{n} true stories.", "Documentaries, best first."],
        "anime": ["{n} anime picks, ready to go.", "Anime night, sorted."],
    },
    "mood": {
        "feel good": ["Feel-good picks, warm and cosy.", "Good vibes only, best first."],
        "mind-bending": ["Mind-benders ahead. Pay attention.", "Brain-twisters, best first."],
        "chill": ["Easy watching, sorted.", "Low effort, high comfort."],
        "intense": ["Intense picks. Brace yourself.", "Gripping stuff, best first."],
        "date night": ["Date-night picks, best first.", "Something for two."],
        "epic": ["Big, epic picks for a big evening.", "Go big tonight."],
        "festive": ["Festive picks, cosy ones first.", "Holiday spirit, on demand."],
        "spooky": ["Spooky-season picks. Leave a light on.", "Things that go bump, best first."],
    },
    "short": ["Short ones, all under {mins} minutes.", "Quick picks, done in under {mins} minutes."],
    "era": ["Straight from the {era}.", "A trip back to the {era}."],
    "none": [
        "Nothing on the shelves fits that yet. Try another word?",
        "Couldn't find a match. Maybe a different mood?",
    ],
}

# Reasons next to rule-made picks (Home, the request row, the Up-next screen).
WHY = {
    "sequel": "Next in the {col} series",
    "sequel_request": "Completes {col}",
    "director": "Same director as {title}",
    "director_name": "From director {director}",
    "cast": "With {actor}, like {title}",
    "studio": "From {studio}, like {title}",
    "similar": "Fans of {title} love this one",
    "genre": "More {genre} like {title}",
    "genre_plain": "More {genre}",
    "rating": "Rated {rating} and still unwatched",
    "seasonal_halloween": "A spooky-season pick",
    "seasonal_christmas": "A cosy holiday pick",
    "seasonal_summer": "A summer blockbuster",
    "seasonal_valentine": "A date-night pick",
    "anime": "More anime for your queue",
    "era": "Same era as {title}",
    "liked_by_fans": "Liked by fans of {title}",
    "trending": "Trending this week",
    "new": "Just added to your library",
    "finish": "{left} episodes left to finish",
    "next_up": "Up next",
}

ROWS = {
    "because": "Because you watched {title}",
    "short": "Short tonight (under 100 min)",
    "top_unseen": "Highly rated you haven't seen",
    "finish": "Finish the series",
    "halloween": "Spooky season",
    "christmas": "Cosy holiday picks",
    "summer": "Summer blockbusters",
    "valentine": "Date night",
    "picks": "Picked for you",
}
