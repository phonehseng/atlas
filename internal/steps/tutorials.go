package steps

import (
	"bytes"
	"fmt"
	"html/template"
	"path/filepath"
	"strings"

	"mediastack/internal/platform"

	"mediastack/internal/core"
)

// Guide is a step's help text: Intro is shown before it runs, After once it has finished.
type Guide struct{ Intro, After string }

var guides = map[string]Guide{
	"welcome": {Intro: `
<p>This program builds a complete home media server, one piece at a time, and connects the pieces for you.
Every step explains what it installs before it does anything.</p>
<ul class="apps">
<li><b>Jellyfin</b> - your own streaming service: watch on phones, TVs and browsers.</li>
<li><b>Seerr</b> - a request page: search for a film or show and click <i>Request</i>. Friends can use it too.</li>
<li><b>Sonarr</b> &amp; <b>Radarr</b> - find, download, rename and upgrade TV/anime and films.</li>
<li><b>Prowlarr</b> - one place for the search sites (indexers) Sonarr and Radarr use.</li>
<li><b>qBittorrent</b> - downloads, only through your VPN.</li>
<li><b>Recyclarr</b> - expert quality settings from the TRaSH Guides: whatever is available first, then upgraded (films up to 4K, shows up to 1080p).</li>
<li><b>Bazarr</b> - English subtitles for anime and foreign-language films.</li>
<li><b>Unpackerr</b> &amp; <b>Cleanuparr</b> - unpack archived releases; remove stuck or fake downloads.</li>
<li><b>MediaSorter</b> &amp; the <b>Media Hub</b> - sort files you download yourself; one dashboard for everything, with nightly
backups, a VPN leak check, phone alerts, and a request doctor that explains (and fixes) why a request isn't here yet.</li>
{{if win}}<li><b>Atlas</b> - a Netflix-style player, requests and your stack's dashboard in one app for this PC.</li>{{end}}
<li><b>Tailscale</b> - private access from your phone or TV away from home. Optional extras: <b>jfa-go</b> (invite friends),
<b>Requestrr</b> (requests from Discord), <b>autobrr</b> (IRC), <b>trawl</b>/<b>Byparr</b> (Cloudflare-protected sites),
<b>Lidarr</b> (music), <b>Qui</b> (a faster qBittorrent page with seeding rules).</li>
</ul>
{{if win}}<p>On Windows every app runs in the background whenever you're signed in (Task Scheduler folder <i>{{.St.Name}}</i>),
and restarts by itself if it crashes. Some steps install software system-wide: Windows will ask you to click <b>Yes</b>.
The first time Jellyfin, Seerr (Node.js), jfa-go and the Media Hub/Bazarr (Python) start, Windows Firewall may ask whether
to allow them on networks: click <b>Allow</b> so phones and TVs can connect.</p>
{{else}}<p>On Linux every app runs as a Docker container that starts with the computer. Run this program with <code>sudo</code>.</p>{{end}}
<p>You can close this program at any time and run it again later - finished steps are remembered.</p>`,
		After: `<p>Everything this computer needs is in place. Next, choose where the media server keeps its files, then which parts you want.</p>`},

	"folders": {Intro: `
<p>Everything lives in one folder, so it's easy to back up or move:</p>
<pre>{{.Root}}
├─ data
│  ├─ media        Movies, TV Shows, Anime, Anime Movies   ← Jellyfin shows these
│  ├─ downloads    incomplete, complete, watch
│  └─ drop         drop video files here to have them sorted
├─ config          each app's settings and database
{{if win}}├─ apps         the programs themselves
└─ logs          background-app output{{else}}└─ docker-compose.yml   the containers{{end}}</pre>
<p>Downloads and the library sit on the same drive on purpose: finished downloads are <i>hardlinked</i> into the library,
which is instant and uses no extra space while a torrent keeps seeding.</p>`,
		After: `<p>Folders are ready. Your library is in <code>{{media}}</code>.</p>`},

	"choose": {Intro: `
<p>Pick the parts of the stack you want. Each card says what that part is for and roughly what it takes; ticking one also adds what
it needs, and unticking one tells you what depends on it. Start from a preset:</p>
<ul><li><b>Recommended</b> - everything most homes want: watch, request, download through the VPN, quality rules, subtitles,
tidying, the dashboard with nightly backups, and Tailscale{{if win}}, with Atlas as your app{{end}}.</li>
<li><b>Minimal</b> - just watch, request and download: Jellyfin, Seerr, Sonarr, Radarr, Prowlarr and qBittorrent behind your VPN.</li>
<li><b>Everything</b> - every part, including the extras for private trackers, Discord and music.</li></ul>
<p>Only the parts you pick appear as steps. You can come back here any time ("Change what's installed" on the last page) to add
more; unticking something already installed hides its step but doesn't uninstall it.</p>`,
		After: `<p>Saved. The steps on the left are now exactly the parts you picked. Next: your admin login.</p>`},

	"marquee": {Intro: `
<p><b>Atlas</b> is your stack's app for this PC: a Netflix-style player for your Jellyfin library (it plays the files directly,
with your graphics card), requests through Seerr, and your stack's dashboard (the Media Hub's <i>Your stack</i> page) in one window.
The Media Hub keeps running in the background either way: health checks, the watchdog, alerts and backups.</p>
<p>This step downloads Atlas (about 75 MB), installs it in <code>{{.Root}}\apps\Atlas</code>, adds Start menu and desktop
shortcuts, and fills in Atlas's settings for this stack: the Jellyfin and Seerr addresses, qBittorrent, the Media Hub, and
Sonarr/Radarr (for its storage clean-up). Your password is never stored: you sign in inside Atlas.</p>
<p>Atlas's optional AI features aren't installed here; Atlas offers them in its own Settings.</p>`,
		After: `
<ol><li>Open <b>Atlas</b> from the Start menu or the desktop.</li>
<li>The first screen is <i>Sign in with your Jellyfin account</i>. The Jellyfin address and your username are already filled in:
type your admin password and press <b>Sign in</b>. To connect requests at the same time, open
<i>+ Requests with Seerr (optional)</i> first (its address is filled in too); otherwise connect Seerr later in Atlas's
Settings with the same password.</li>
<li>Atlas then asks a few quick questions about what you like to watch (you can skip them), and opens on your library.</li>
<li><b>Your stack</b> (in Atlas's menu) shows the same status, problems, requests, downloads and backups as the Media Hub.</li>
<li><b>Atlas Mobile</b> (watching on your phone over Tailscale) is off until you turn it on in Atlas's Settings; the
next step lets it through Windows Firewall.</li></ol>
<p>To uninstall Atlas: delete <code>{{.Root}}\apps\Atlas</code>, its Start menu and desktop shortcuts, and
<code>%APPDATA%\Marquee</code> (its settings).</p>`},

	"update": {Intro: `
<p>Brings an existing install up to date, without wiping anything: settings, logins, requests, the library and downloads stay
exactly as they are.</p>
<ul><li>MediaSorter, the Media Hub and the health check get the files this version of Media Stack Setup carries
(only when they changed), and restart.</li>
<li>Apps this installer pins to a version (trawl) are moved to that version.</li>
<li>Ticked below: every app that follows its newest release is updated, but only if a newer release exists.</li>
<li>Atlas is updated to the newest release (its settings are kept). An install from before Marquee was renamed
to Atlas moves from <code>apps\Marquee</code> to <code>apps\Atlas</code>, and its shortcuts and firewall rule are renamed too.</li>
<li>Anything that should be running but isn't is started again.</li></ul>
<p>The log ends with a summary of what changed. If a newer Media Stack Setup exists, the banner at the top offers it first:
update the installer, then run this step.</p>`,
		After: `<p>Done. Run this step again whenever a new version comes out, or start the program with <code>--update</code>.</p>`},

	"account": {Intro: `
<p>Choose <b>one</b> username and password for everything you manage:</p>
<ul><li>your <b>Jellyfin</b> administrator account (also your Seerr login),</li>
<li>the admin pages of qBittorrent, Sonarr, Radarr, Prowlarr, Bazarr, Cleanuparr and autobrr.</li></ul>
<p>Friends and family get their <b>own</b> accounts later (jfa-go step), so this one stays yours. A password manager is a good place to keep it.</p>`,
		After: `<p>Saved. If you close this program and come back, it will ask for the password again when a step needs it.</p>`},

	"jellyfin": {Intro: `
<p><b>Jellyfin</b> is the part you watch: a free, private streaming service for your own files. This step installs it,
creates your admin account, and adds the libraries <i>Movies</i>, <i>TV Shows</i> and <i>Anime</i> (series and films together).
With the box below ticked it also adds <b>Intro Skipper</b>, which gives episodes a Netflix-style <i>Skip Intro</i> button.
{{if nvidia}}Your NVIDIA graphics card will be used to convert video for devices that can't play a file as-is.{{end}}</p>`,
		After: `
<ol><li>Open Jellyfin: <a href="{{url "jellyfin"}}" target="_blank">{{url "jellyfin"}}</a> and sign in with your admin login.</li>
<li>Install the <b>Jellyfin</b> app on your phone or TV (Android, iPhone, Android TV / Google TV, Fire TV, Roku, Apple TV via Swiftfin...).
At home, use the server address <code>{{lan "jellyfin"}}</code>. For away from home, see the last steps (Tailscale).</li>
<li>The libraries stay empty until something is requested or sorted in - that's the next steps.</li>
<li><i>Skip Intro</i> appears once Intro Skipper has scanned an episode (its scheduled task runs daily; new episodes are scanned as they arrive).</li></ol>`},

	"vpn": {Intro: `
<p>Torrent downloads are visible to everyone sharing the same file, and to your internet provider. A VPN hides your home address.
{{if win}}This installer <b>binds qBittorrent to your VPN's network connection</b>: if the VPN disconnects, downloads simply stop -
they never fall back to your normal connection. Everything else on the PC keeps using your normal internet.</p>
<ol><li>Install your VPN app and connect it. <b>NordVPN</b>: Settings → Connection → VPN protocol <i>NordLynx</i>.</li>
<li>If your VPN app uses <i>split tunnelling</i> ("VPN only for selected apps", as with NordVPN), you'll add qBittorrent to it
after the next step.</li>
<li>Press <b>Refresh</b> and pick the VPN's connection below (NordVPN's is called <i>NordLynx</i>).</li></ol>
{{else}}On Linux, qBittorrent runs <b>inside a VPN container</b> (Gluetun): it has no network at all unless the VPN is up.
Nothing else on the server uses the VPN.</p>
<p>Gluetun needs your provider's <b>manual-setup credentials</b> - these are not your website login:</p>
<ul><li><b>NordVPN</b>: nordaccount.com → NordVPN → <i>Set up NordVPN manually</i> → <i>Service credentials</i> (username + password). OpenVPN.</li>
<li><b>Proton VPN</b>: account.proton.me → VPN → <i>OpenVPN / IKEv2 username</i> and password.</li>
<li><b>Mullvad</b>: account page → <i>WireGuard configuration</i> → generate a key; use the private key and the address it shows.</li>
<li><b>Surfshark</b>, <b>PIA</b>, <b>ExpressVPN</b>, <b>Windscribe</b>...: look for "manual setup" or "OpenVPN credentials".</li></ul>
<p>They are stored only in <code>{{.Root}}/.env</code>, readable by root only.</p>{{end}}`,
		After: `{{if win}}<p>Saved. The next step installs qBittorrent and locks it to <b>{{.St.VPN.Adapter}}</b>. The Media Hub's
<b>VPN leak check</b> later proves it: it compares the address torrents use with your home address.</p>
{{else}}<p>The VPN container is connected. The next step puts qBittorrent inside it.</p>{{end}}`},

	"qbittorrent": {Intro: `
<p><b>qBittorrent</b> does the downloading. Sonarr and Radarr send it what to fetch; you can also add torrents yourself.
This step sets: download folders, categories, seeding limits (stop at ratio 1.0, after 12 hours, or after 1 hour with nobody
downloading), a <i>watch</i> folder, and your admin login for its web page.</p>`,
		After: `
{{if win}}{{if nord}}<p class="todo"><b>NordVPN split tunnelling - do this now:</b> NordVPN → Settings → <i>Split tunneling</i> → turn it on →
<i>Enable VPN for selected apps only</i> → <i>Add apps</i> → choose <code>C:\Program Files\qBittorrent\qbittorrent.exe</code>.
Then reconnect NordVPN.</p>{{end}}
<p>qBittorrent runs with its own window/tray icon. Manual downloads: pick a category (<i>movies</i>, <i>tv</i>, <i>anime</i>)
and MediaSorter files it into the right library.</p>{{else}}
<p>qBittorrent's web page: <a href="{{url "qbittorrent"}}" target="_blank">{{url "qbittorrent"}}</a> (your admin login).
For manual downloads pick a category (<i>movies</i>, <i>tv</i>, <i>anime</i>) and MediaSorter files it into the right library.</p>{{end}}
<p>Or drop <code>.torrent</code> files into <code>{{watch}}</code>.</p>`},

	"qui": {Intro: `
<p><b>Optional.</b> <b>Qui</b> is a faster, modern page for qBittorrent that copes with thousands of torrents, and adds
<b>rules per tracker</b>: how long to seed, when to pause or remove, tags and categories. Most useful with private trackers,
which expect you to seed for a set time. This step installs it, creates its login (your admin login) and connects qBittorrent.</p>`,
		After: `<p>Open <a href="{{url "qui"}}" target="_blank">Qui</a> and sign in with your admin login. Seeding rules: <i>Automations</i>.
qBittorrent's own simple limits (ratio 1.0 / 12 hours) still apply to anything without a rule.</p>`},

	"prowlarr": {Intro: `
<p><b>Prowlarr</b> keeps the list of sites (indexers) that Sonarr and Radarr search. You add each site once here and it's
shared with both automatically.</p>`,
		After: `
<p class="todo"><b>Add your indexers</b> - the sites you want to search. This is up to you; nothing is added automatically.</p>
<ol><li>Open <a href="{{url "prowlarr"}}" target="_blank">Prowlarr</a> → <i>Indexers</i> → <i>Add Indexer</i>.</li>
<li>Search for a site you use, click it, fill in what it asks (private trackers need your account's key or cookie), <i>Test</i>, <i>Save</i>.</li>
<li>Sites behind Cloudflare ("checking your browser") need a helper: install <b>trawl</b> a few steps down and give the indexer the tag <code>trawl</code>.</li></ol>
<p>You can add indexers now or later; Sonarr and Radarr pick them up within a minute.</p>`},

	"sonarr": {Intro: `
<p><b>Sonarr</b> handles TV and anime: it watches for new episodes, picks the best release, sends it to qBittorrent, then renames
and files it into <i>TV Shows</i> or <i>Anime</i>. This step connects it to qBittorrent, Prowlarr and Jellyfin.</p>`,
		After: `<p>Add shows through <b>Seerr</b> (later step) - or in <a href="{{url "sonarr"}}" target="_blank">Sonarr</a> → <i>Add New</i>.
Choose series type <i>Anime</i> for anime so episodes are matched by absolute number.</p>`},

	"radarr": {Intro: `
<p><b>Radarr</b> does the same for films: <i>Movies</i>, and Japanese animated films go to <i>Anime Movies</i>.</p>`,
		After: `<p>Add films through <b>Seerr</b> - or in <a href="{{url "radarr"}}" target="_blank">Radarr</a> → <i>Add New</i>.</p>`},

	"recyclarr": {Intro: `
<p><b>Recyclarr</b> copies the community's best-known quality rules (TRaSH Guides) into Sonarr and Radarr and keeps them up to date
weekly. On top of them this installer applies one rule: <b>get whatever is available first, then upgrade</b>, so a request never
waits for a perfect copy:</p>
<ul><li><b>Films</b> (<i>Remux 2160p (Combined)</i>): any watchable release first, upgraded step by step up to a 4K remux.</li>
<li><b>Shows</b> (<i>1080p</i>): any watchable release first, upgraded up to 1080p. Want a show in 4K? Switch it to
<i>Remux 2160p (Combined)</i> in Sonarr.</li>
<li><b>Anime</b> (<i>[Anime] Remux-1080p</i>): the anime release groups' rules, with anything English-subtitled accepted while it waits.</li></ul>
<p>Low-quality encodes (like YTS), x265 and upscales are taken only when nothing better exists. Disc images, extras, 3D,
dubbed-only versions, raw (unsubtitled) anime and anime subtitled only in another language (custom format
<i>Foreign subs only</i>, e.g. Brazilian "Legendado" releases) are never taken. 4K remuxes are big (40-80 GB per film); for smaller films
pick <i>UHD Bluray + WEB</i> or <i>HD Bluray + WEB</i> instead.</p>`,
		After: `<p>Profiles synced. Seerr uses them automatically, and the Media Hub's request doctor keeps looking for upgrades.</p>`},

	"lidarr": {Intro: `
<p><b>Optional.</b> <b>Lidarr</b> does for music what Sonarr does for shows: add an artist, and it finds, downloads, tags and files
their albums into <i>Music</i>, which this step adds to Jellyfin. It takes what's available first and upgrades to lossless.</p>`,
		After: `
<ol><li>Open <a href="{{url "lidarr"}}" target="_blank">Lidarr</a> → <i>Add New</i> → search for an artist.</li>
<li>Lidarr only gets the Prowlarr indexers that have <b>music</b>. If Lidarr says it has no indexers, add a site with music in
Prowlarr; it's shared within a minute once it returns music results.</li>
<li>Listen in Jellyfin (the <i>Music</i> library), or a music app like Finamp (Android/iPhone).</li></ol>`},

	"trawl": {Intro: `
<p><b>Optional.</b> Some indexer sites sit behind Cloudflare's "checking your browser" page, which Prowlarr can't get past.
<b>trawl</b> opens those pages in a hidden real browser and hands the result to Prowlarr. Only indexers you tag <code>trawl</code> use it.
Skip this if your indexers work without it.</p>
<p>trawl runs locally only (never reachable from other computers) with its extras off (no MCP server, no proxying, no paid captcha services).</p>`,
		After: `<p>In Prowlarr, edit a Cloudflare-protected indexer, add the tag <code>trawl</code>, and <i>Test</i> it.</p>`},

	"byparr": {Intro: `
<p><b>Optional fallback</b> for Cloudflare-protected sites, if trawl can't open one. Indexers tagged <code>byparr</code> use it.</p>`,
		After: `<p>Tag an indexer <code>byparr</code> in Prowlarr to use it.</p>`},

	"bazarr": {Intro: `
<p><b>Bazarr</b> fetches subtitles. It's set to add <b>English subtitles only when the audio isn't English</b> - anime and foreign
films get them, English shows are left alone. English subtitles already inside a file count, and downloaded ones are synced to the audio.</p>`,
		After: `<p>Tip: a free <b>OpenSubtitles.com</b> account improves coverage: <a href="{{url "bazarr"}}" target="_blank">Bazarr</a> →
<i>Settings</i> → <i>Providers</i> → <i>OpenSubtitles.com</i>.</p>`},

	"unpackerr": {Intro: `<p><b>Unpackerr</b> extracts releases that arrive as <code>.rar</code>/<code>.zip</code> archives so they can be imported.
It runs in the background and needs nothing from you.</p>`},

	"cleanuparr": {Intro: `
<p><b>Cleanuparr</b> watches the download queue: downloads that stall, and fake releases (programs or scripts disguised as video),
are removed and Sonarr/Radarr are told to try another release - so requests don't sit stuck forever.</p>`,
		After: `<p>After an internet or VPN outage, check Sonarr/Radarr's <i>Activity → Blocklist</i>: downloads that stalled during the
outage may have been blocklisted and can be removed from it.</p>`},

	"seerr": {Intro: `
<p><b>Seerr</b> is the request page: search for anything, click <i>Request</i>, and it's downloaded in the background and appears
in Jellyfin. Everyone signs in with their Jellyfin account, and requests are approved automatically.
{{if win}}On Windows Seerr is built from its source code, which takes <b>10-15 minutes</b>.{{end}}</p>`,
		After: `
<ol><li>Open <a href="{{url "seerr"}}" target="_blank">Seerr</a> and sign in with your Jellyfin login.</li>
<li>Search for a film or show and click <b>Request</b>. Anime is detected and sent to the Anime folders automatically.</li>
<li>Watch it progress in the Media Hub (later step) - it appears in Jellyfin a minute or two after the download finishes.</li></ol>`},

	"jfago": {Intro: `
<p><b>Optional.</b> <b>jfa-go</b> makes invite links: send one to a friend, they pick a username and password, and get their
own Jellyfin account (which also works in Seerr). jfa-go keeps your admin login in its settings to create those accounts.</p>`,
		After: `
<ol><li>Open <a href="{{url "jfago"}}" target="_blank">jfa-go</a> and sign in with your Jellyfin admin login.</li>
<li><i>Invites</i> → <i>Create</i> → copy the link and send it.</li>
<li>Friends outside your home also need Tailscale access - see the next steps.</li></ol>`},

	"requestrr": {Intro: `<p><b>Optional.</b> <b>Requestrr</b> is a Discord bot: friends type a command in your Discord server to request a film or show.
You need a Discord server and a bot you create yourself (free).</p>`,
		After: `
<ol><li>Open <a href="{{url "requestrr"}}" target="_blank">Requestrr</a>. The first visit asks you to create its login.</li>
<li>Create a bot: discord.com/developers → <i>New Application</i> → <i>Bot</i> → <i>Reset Token</i> (copy it) → turn on
<i>Message Content Intent</i> → <i>OAuth2 → URL Generator</i> (scopes <i>bot</i>, <i>applications.commands</i>) → open the link to add it to your server.</li>
<li>In Requestrr: paste the bot token, choose <i>Overseerr</i> as the request service with address <code>{{appurl "seerr"}}</code> and Seerr's API key
(Seerr → Settings → General).</li></ol>`},

	"autobrr": {Intro: `<p><b>Optional, for private trackers.</b> <b>autobrr</b> watches trackers' IRC announce channels and grabs new releases the
moment they appear. Only useful if you're a member of trackers that announce on IRC.</p>`,
		After: `<p>In <a href="{{url "autobrr"}}" target="_blank">autobrr</a>: <i>Settings → Indexers</i> → add your tracker (it connects to its IRC channel),
then <i>Filters</i> → create a filter that sends matches to Sonarr or Radarr (already connected).</p>`},

	"mediasorter": {Intro: `
<p><b>MediaSorter</b> handles what you download yourself: it recognises the film or show, names it properly and files it into the
right library (through Sonarr/Radarr when it can). It also removes audio and subtitle languages you don't want from new files -
losslessly, nothing is re-encoded.</p>`,
		After: `
<ul><li>Manual downloads in qBittorrent: choose category <i>movies</i>, <i>tv</i> or <i>anime</i>.</li>
<li>Video files from anywhere: drop them into <code>{{drop}}</code>.</li>
<li>Each sorted file shows up in the Media Hub's activity feed.</li></ul>`},

	"hub": {Intro: `
<p>The <b>Media Hub</b> is one page for everything: what's downloading, requests (approve, search, request), problems from every app
with fix buttons, a combined error log, and every app's own page as a tab. It also shows the addresses to use on phones and TVs.</p>
<ul><li><b>Request doctor</b>: every request that isn't here yet says why (not released yet, no seeders, blocked by the quality
rules...), and what it could fix, it fixes: wrong profile, never searched, broken files, Seerr losing a request.</li>
<li><b>Nightly backups</b> of every app's settings and databases (not the media), with a <i>Restore</i> button.</li>
<li><b>VPN leak check</b>: proves torrents use the VPN's address, not yours, and alerts you if that ever changes.</li>
<li><b>Phone alerts</b> through the free ntfy app: "ready to watch" and problems, even away from home.</li></ul>`,
		After: `
<p>Open <a href="{{url "hub"}}" target="_blank">the Media Hub</a>{{if win}} (there's a desktop shortcut){{end}}.
{{if win}}On this PC it needs no login. From your phone over Tailscale, sign in with your Jellyfin admin login.
{{else}}Sign in with your Jellyfin admin login.{{end}} A health check runs every hour and fixes common problems by itself,
and a watchdog checks every 5 minutes: it restarts any app that goes down, notes what its log said (Overview → Incidents),
and tells you if a restart didn't fix it.</p>
<p>Phone alerts: Overview → <i>Phone alerts</i> → <b>Turn on</b>, then subscribe to the topic it shows in the ntfy app.</p>`},

	"remote": {Intro: `
<p><b>At home</b>, phones and TVs on the same Wi-Fi can reach Jellyfin and Seerr directly{{if win}} once Windows Firewall allows it (first option){{end}}.</p>
<p><b>Away from home</b>, <b>Tailscale</b> makes a private network between your devices: nothing is opened to the internet,
and it's free for personal use. This step installs it on this computer{{if win}} (the official installer, checked against its
published fingerprint; Windows asks once for permission, together with the firewall rules){{else}} (Tailscale's official install
script){{end}}, then opens Tailscale's sign-in page: sign in there with Google, Microsoft, GitHub or a new free account.
This program never sees your Tailscale account. If Tailscale is already installed and signed in, it's left exactly as it is.</p>`,
		After: `
<ol><li>Install <b>Tailscale</b> on your phone/tablet/TV (App Store, Google Play - Google TV and Fire TV too) and sign in with the same account.</li>
<li>In the Jellyfin app, the server address is <code>{{remote "jellyfin"}}</code>. If the name doesn't work, use <code>http://{{tsip}}:{{port "jellyfin"}}</code>.</li>
<li>Requests: <code>{{remote "seerr"}}</code>{{if deployed "hub"}}; Media Hub: <code>{{remote "hub"}}</code>{{end}}.</li>
<li><b>Friends</b>: Tailscale admin console → <i>Machines</i> → this computer → <i>Share</i> → send the link. They install Tailscale, accept, and use the same addresses.</li>
{{if marquee}}<li><b>Atlas Mobile</b>: turn it on in Atlas's Settings to watch on your phone over Tailscale. Windows Firewall lets it through on private networks only (Tailscale counts as private).</li>{{end}}
{{if nord}}<li><b>NordVPN</b>: add <code>C:\Program Files\Tailscale\tailscaled.exe</code> to split tunnelling (like qBittorrent), or Tailscale can't sign in while NordVPN is on.</li>{{end}}
</ol>`},

	"finish": {Intro: `<p>That's everything. Press <b>Finish</b> for a summary of where to open each part.</p>`,
		After: `
<p class="todo"><b>Your to-do list:</b></p>
<ol><li>Add indexers in <a href="{{url "prowlarr"}}" target="_blank">Prowlarr</a> (if you haven't).</li>
<li>Request something in <a href="{{url "seerr"}}" target="_blank">Seerr</a> and watch it arrive in the Media Hub.</li>
<li>Install the Jellyfin app on your TV and phone.</li>
<li>Invite friends with jfa-go and share Tailscale with them.</li></ol>
<p>Run this program again any time: <b>Update my stack</b> keeps everything current, and
<button class="b" type="button" data-step="choose">Change what's installed</button> adds more parts.</p>`},
}

type guideData struct {
	St   *core.Stack
	Root string
}

// RenderGuide fills a step's help text for this install.
func RenderGuide(id string, st *core.Stack, root string) (intro, after string) {
	g, ok := guides[id]
	if !ok {
		return "", ""
	}
	if st == nil {
		st = core.New(root)
	}
	funcs := template.FuncMap{
		"win":      func() bool { return !st.Docker() },
		"url":      func(id string) string { return OpenURL(st, id) },
		"appurl":   func(id string) string { return st.AppURL(id) },
		"port":     func(id string) int { return st.Port(id) },
		"deployed": st.IsDeployed,
		"nvidia":   hasNvidia,
		"marquee":  func() bool { return marqueeWanted(st) },
		"nord": func() bool {
			return st.VPN.Mode == "app" && containsFold(st.VPN.Adapter, "nord")
		},
		"lan": func(id string) string {
			if ip := LANAddress(); ip != "" {
				return fmt.Sprintf("http://%s:%d", ip, st.Port(id))
			}
			return OpenURL(st, id)
		},
		"remote": func(id string) string {
			if n := st.Opt("tailscale_name"); n != "" {
				return fmt.Sprintf("http://%s:%d", n, st.Port(id))
			}
			return fmt.Sprintf("http://<this computer's Tailscale name>:%d", st.Port(id))
		},
		"tsip":  func() string { return orDefault(st.Opt("tailscale_ip"), "100.x.y.z") },
		"media": func() string { return filepath.Join(st.DataDir(), "media") },
		"drop":  st.DropDir,
		"watch": func() string { return filepath.Join(st.DownloadsDir(), "watch") },
	}
	render := func(s string) string {
		if s == "" {
			return ""
		}
		t, err := template.New(id).Funcs(funcs).Parse(s)
		if err != nil {
			return "<p>(help text error: " + template.HTMLEscapeString(err.Error()) + ")</p>"
		}
		var b bytes.Buffer
		if err := t.Execute(&b, guideData{St: st, Root: st.Root}); err != nil {
			return "<p>(help text error: " + template.HTMLEscapeString(err.Error()) + ")</p>"
		}
		return b.String()
	}
	return render(g.Intro), render(g.After)
}

func orDefault(s, d string) string {
	if s == "" {
		return d
	}
	return s
}

func hasNvidia() bool { return platform.HasNvidia() }

func containsFold(s, sub string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(sub))
}
