package steps

import (
	"runtime"
	"sort"
	"strings"

	"mediastack/internal/core"
)

// Component is one part of the stack on the "Choose your stack" page. Most are one wizard step; a few are
// options inside a step (Intro Skipper, backups, phone alerts).
type Component struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	Group string `json:"group"`
	What  string `json:"what"`         // one plain sentence: what it's for
	OS    string `json:"os,omitempty"` // "" = both, "windows", "linux"
	// Rough footprint, for the page's estimate.
	DiskMB int `json:"disk_mb"`
	RAMMB  int `json:"ram_mb"`
	// Needs are components this one can't work without; Why says what each need is used for (shown when the
	// user unticks the need).
	Needs  []string          `json:"needs,omitempty"`
	Why    map[string]string `json:"why,omitempty"`
	Locked string            `json:"locked,omitempty"` // always installed, and why
	Steps  []string          `json:"-"`
	// Presets this component is part of.
	Minimal     bool `json:"minimal,omitempty"`
	Recommended bool `json:"recommended,omitempty"`
	// Legacy is the answer for installs made before this page existed (only matters for options).
	Legacy bool `json:"-"`
}

// ComponentGroups is the page's order.
var ComponentGroups = []string{"Watch", "Find & organise", "Download", "Request & share", "Remote access", "Extras"}

var both = func(a, b string) map[string]string { return map[string]string{"sonarr": a, "radarr": b} }

// Components is every part of the stack.
var Components = []*Component{
	// Watch
	{ID: "folders", Name: "Media folders", Group: "Watch", Locked: "Every app keeps its files here (you chose it in the previous step).",
		What: "One folder for the library, downloads and every app's settings, so it's easy to back up or move.", Legacy: true},
	{ID: "jellyfin", Name: "Jellyfin", Group: "Watch", DiskMB: 450, RAMMB: 500, Steps: []string{"jellyfin"},
		Locked: "Everything else delivers to Jellyfin: it's the library and the player on phones and TVs.",
		What:   "Your own streaming service: watch your films and shows on phones, TVs and browsers, at home or away.", Legacy: true},
	{ID: "introskipper", Name: "Intro Skipper", Group: "Watch", DiskMB: 5, Needs: []string{"jellyfin"}, Minimal: false, Recommended: true, Legacy: true,
		What: "Adds a Netflix-style \"Skip Intro\" button to episodes in Jellyfin."},
	{ID: "marquee", Name: "Atlas", Group: "Watch", OS: "windows", DiskMB: 250, RAMMB: 400, Steps: []string{"marquee"},
		Needs: []string{"jellyfin", "seerr"}, Recommended: true,
		Why:  map[string]string{"jellyfin": "Atlas plays your Jellyfin library", "seerr": "Atlas's requests go through Seerr"},
		What: "A Netflix-style player for this PC with requests and your stack's dashboard in one app; plays your files directly."},

	// Find & organise
	{ID: "prowlarr", Name: "Prowlarr", Group: "Find & organise", DiskMB: 200, RAMMB: 150, Steps: []string{"prowlarr"}, Minimal: true, Recommended: true, Legacy: true,
		What: "Keeps the list of search sites (indexers) in one place and shares it with Sonarr, Radarr and Lidarr."},
	{ID: "sonarr", Name: "Sonarr", Group: "Find & organise", DiskMB: 250, RAMMB: 250, Steps: []string{"sonarr"}, Minimal: true, Recommended: true, Legacy: true,
		Needs: []string{"download", "prowlarr"},
		Why:   map[string]string{"download": "Sonarr sends what it finds to qBittorrent", "prowlarr": "Sonarr searches the sites Prowlarr manages"},
		What:  "Finds and downloads TV episodes and anime automatically, and upgrades them when better copies appear."},
	{ID: "radarr", Name: "Radarr", Group: "Find & organise", DiskMB: 250, RAMMB: 250, Steps: []string{"radarr"}, Minimal: true, Recommended: true, Legacy: true,
		Needs: []string{"download", "prowlarr"},
		Why:   map[string]string{"download": "Radarr sends what it finds to qBittorrent", "prowlarr": "Radarr searches the sites Prowlarr manages"},
		What:  "Does the same for films: finds, downloads, renames and upgrades them."},
	{ID: "recyclarr", Name: "Recyclarr", Group: "Find & organise", DiskMB: 80, Steps: []string{"recyclarr"}, Recommended: true, Legacy: true,
		Needs: []string{"sonarr", "radarr"}, Why: both("its TV and anime quality rules are Sonarr's", "its film quality rules are Radarr's"),
		What: "Sets expert quality rules (TRaSH Guides): any watchable copy first, then upgrades (films to 4K, shows to 1080p). Runs weekly."},
	{ID: "bazarr", Name: "Bazarr", Group: "Find & organise", DiskMB: 400, RAMMB: 200, Steps: []string{"bazarr"}, Recommended: true, Legacy: true,
		Needs: []string{"sonarr", "radarr"}, Why: both("it finds subtitles for Sonarr's episodes", "it finds subtitles for Radarr's films"),
		What: "Adds English subtitles when the audio isn't English (anime, foreign films)."},
	{ID: "mediasorter", Name: "MediaSorter", Group: "Find & organise", DiskMB: 250, RAMMB: 60, Steps: []string{"mediasorter"}, Recommended: true, Legacy: true,
		Needs: []string{"sonarr", "radarr"}, Why: both("it files your own TV downloads through Sonarr", "it files your own film downloads through Radarr"),
		What: "Sorts what you download yourself (or drop in a folder) into the library, and strips unwanted audio and subtitle languages."},
	{ID: "lidarr", Name: "Lidarr", Group: "Find & organise", DiskMB: 250, RAMMB: 250, Steps: []string{"lidarr"},
		Needs: []string{"download", "prowlarr"},
		Why:   map[string]string{"download": "Lidarr sends albums to qBittorrent", "prowlarr": "Lidarr searches the sites Prowlarr manages"},
		What:  "Music: add an artist and it finds, downloads, tags and files their albums for Jellyfin."},
	{ID: "trawl", Name: "trawl", Group: "Find & organise", DiskMB: 1200, RAMMB: 400, Steps: []string{"trawl"},
		Needs: []string{"prowlarr"}, Why: map[string]string{"prowlarr": "it opens Cloudflare-protected sites for Prowlarr"},
		What: "Gets Prowlarr past Cloudflare's \"checking your browser\" page on the sites that use it."},
	{ID: "byparr", Name: "Byparr", Group: "Find & organise", DiskMB: 700, RAMMB: 300, Steps: []string{"byparr"},
		Needs: []string{"prowlarr"}, Why: map[string]string{"prowlarr": "it opens Cloudflare-protected sites for Prowlarr"},
		What: "A second Cloudflare helper, for sites trawl can't open."},

	// Download
	{ID: "download", Name: "VPN + qBittorrent", Group: "Download", DiskMB: 120, RAMMB: 250, Steps: []string{"vpn", "qbittorrent"},
		Minimal: true, Recommended: true, Legacy: true,
		What: "Downloads torrents, only ever through your VPN: if the VPN drops, downloads stop instead of using your home connection."},
	{ID: "unpackerr", Name: "Unpackerr", Group: "Download", DiskMB: 20, RAMMB: 30, Steps: []string{"unpackerr"}, Recommended: true, Legacy: true,
		Needs: []string{"sonarr", "radarr"}, Why: both("it unpacks Sonarr's downloads", "it unpacks Radarr's downloads"),
		What: "Unpacks releases that arrive as .rar or .zip archives so they can be imported."},
	{ID: "cleanuparr", Name: "Cleanuparr", Group: "Download", DiskMB: 150, RAMMB: 150, Steps: []string{"cleanuparr"}, Recommended: true, Legacy: true,
		Needs: []string{"sonarr", "radarr"}, Why: both("it tells Sonarr to try another release", "it tells Radarr to try another release"),
		What: "Removes stalled and fake downloads, so requests don't sit stuck forever."},
	{ID: "qui", Name: "Qui", Group: "Download", DiskMB: 40, RAMMB: 50, Steps: []string{"qui"},
		Needs: []string{"download"}, Why: map[string]string{"download": "Qui is a page for qBittorrent"},
		What: "A faster qBittorrent page with seeding rules per tracker (useful with private trackers)."},
	{ID: "autobrr", Name: "autobrr", Group: "Download", DiskMB: 50, RAMMB: 50, Steps: []string{"autobrr"},
		Needs: []string{"download", "sonarr", "radarr"},
		Why:   map[string]string{"download": "it sends grabs to qBittorrent", "sonarr": "it hands TV grabs to Sonarr", "radarr": "it hands film grabs to Radarr"},
		What:  "Grabs new releases the moment private trackers announce them on IRC."},

	// Request & share
	{ID: "seerr", Name: "Seerr", Group: "Request & share", DiskMB: 1500, RAMMB: 300, Steps: []string{"seerr"}, Minimal: true, Recommended: true, Legacy: true,
		Needs: []string{"jellyfin", "sonarr", "radarr"},
		Why:   map[string]string{"jellyfin": "everyone signs in to Seerr with their Jellyfin account", "sonarr": "TV and anime requests go to Sonarr", "radarr": "film requests go to Radarr"},
		What:  "The request page: search for a film or show and click Request. Friends can use it too."},
	{ID: "jfago", Name: "jfa-go", Group: "Request & share", DiskMB: 30, RAMMB: 40, Steps: []string{"jfago"},
		Needs: []string{"jellyfin"}, Why: map[string]string{"jellyfin": "it creates Jellyfin accounts"},
		What: "Invite links: a friend picks a username and password and gets their own Jellyfin account."},
	{ID: "requestrr", Name: "Requestrr", Group: "Request & share", DiskMB: 150, RAMMB: 100, Steps: []string{"requestrr"},
		Needs: []string{"seerr"}, Why: map[string]string{"seerr": "it sends Discord requests to Seerr"},
		What: "A Discord bot: friends request films and shows from your Discord server."},

	// Remote access
	{ID: "tailscale", Name: "Tailscale", Group: "Remote access", DiskMB: 100, RAMMB: 50, Steps: []string{"remote"}, Recommended: true, Legacy: true,
		What: "Private access from your phone, TV and friends away from home, without opening anything to the internet."},

	// Extras
	{ID: "hub", Name: "Media Hub", Group: "Extras", DiskMB: 30, RAMMB: 80, Steps: []string{"hub"}, Recommended: true, Legacy: true,
		Needs: []string{"mediasorter"}, Why: map[string]string{"mediasorter": "the hub shares MediaSorter's Python and its activity"},
		What: "One dashboard for status, problems, requests and logs, with an hourly health check, a watchdog that restarts apps, a VPN leak check and a request doctor."},
	{ID: "backups", Name: "Nightly backups", Group: "Extras", DiskMB: 200, Needs: []string{"hub"}, Recommended: true, Legacy: true,
		Why:  map[string]string{"hub": "the Media Hub makes and restores the backups"},
		What: "Backs up every app's settings and databases (not the media) every night at 3:30, with a Restore button."},
	{ID: "alerts", Name: "Phone alerts", Group: "Extras", Needs: []string{"hub"},
		Why:  map[string]string{"hub": "the Media Hub sends the alerts"},
		What: "\"Ready to watch\" and problems on your phone through the free ntfy app, even away from home."},
}

// Component returns a component by id (nil if unknown).
func ComponentByID(id string) *Component {
	for _, c := range Components {
		if c.ID == id {
			return c
		}
	}
	return nil
}

// ComponentsFor lists the components that exist on an OS.
func ComponentsFor(goos string) []*Component {
	var out []*Component
	for _, c := range Components {
		if c.OS == "" || c.OS == goos {
			out = append(out, c)
		}
	}
	return out
}

// Preset returns the components of a preset ("minimal", "recommended", "everything") on an OS.
func Preset(name, goos string) []string {
	var out []string
	for _, c := range ComponentsFor(goos) {
		if c.Locked != "" || name == "everything" || (name == "minimal" && c.Minimal) || (name == "recommended" && c.Recommended) {
			out = append(out, c.ID)
		}
	}
	return out
}

// CloseParts cleans a choice: unknown and other-OS parts are dropped, locked parts and everything a chosen
// part needs are added. It returns the result and what was added for a need.
func CloseParts(in []string, goos string) (parts []string, added []string) {
	set := map[string]bool{}
	for _, id := range in {
		if c := ComponentByID(strings.TrimSpace(id)); c != nil && (c.OS == "" || c.OS == goos) {
			set[c.ID] = true
		}
	}
	for _, c := range ComponentsFor(goos) {
		if c.Locked != "" {
			set[c.ID] = true
		}
	}
	for changed := true; changed; {
		changed = false
		for id := range set {
			for _, n := range ComponentByID(id).Needs {
				if !set[n] {
					set[n] = true
					added = append(added, n)
					changed = true
				}
			}
		}
	}
	parts = core.SortedKeys(set)
	sort.Strings(added)
	return parts, added
}

// Chosen reports whether a component is part of this install. Installs from before "Choose your stack"
// keep what they always had.
func Chosen(st *core.Stack, id string) bool {
	if st == nil {
		return false
	}
	if !st.HasParts() {
		c := ComponentByID(id)
		return c != nil && c.Legacy
	}
	return st.Picked(id)
}

// componentOf is the component a step belongs to ("" for steps every install has).
func componentOf(stepID string) string {
	for _, c := range Components {
		for _, s := range c.Steps {
			if s == stepID {
				return c.ID
			}
		}
	}
	return ""
}

// Visible reports whether a step belongs in this install's wizard. Before anything is chosen (and for
// installs from before "Choose your stack") every step is shown.
func Visible(st *core.Stack, s *Step) bool {
	if s.ID == "update" {
		return HasInstall(st)
	}
	if st == nil || !st.HasParts() {
		return true
	}
	if s.ID == "remote" && !st.Docker() {
		return true // also sets up Windows Firewall for phones and TVs at home
	}
	comp := componentOf(s.ID)
	return comp == "" || st.Picked(comp)
}

// StackApp is the "Your stack app" choice: "marquee" or "hub" ("" when neither is chosen).
func StackApp(st *core.Stack) string {
	switch {
	case st == nil:
		return ""
	case Chosen(st, "marquee") && !st.Docker():
		return "marquee"
	case Chosen(st, "hub"):
		return "hub"
	}
	return ""
}

// ---------------------------------------------------------------------------------------------- the step

func init() {
	step(&Step{ID: "choose", Title: "Choose your stack", Group: "Get started", Button: "Use this selection",
		Needs: []string{"folders"}, Fields: chooseFields, Run: runChoose})
}

// chooseFields carries the current choice; the page draws the cards from the component list.
func chooseFields(c *Ctx) []Field {
	return []Field{{Name: "parts", Type: "components", Default: strings.Join(CurrentParts(c.St), ",")}}
}

// CurrentParts is what the page starts with: the saved choice, else Recommended plus anything installed.
func CurrentParts(st *core.Stack) []string {
	goos := runtime.GOOS
	if st != nil {
		goos = st.OS
	}
	if st != nil && st.HasParts() {
		st2, _ := CloseParts(st.Parts, goos)
		return st2
	}
	parts := Preset("recommended", goos)
	if st != nil {
		for _, c := range ComponentsFor(goos) {
			for _, s := range c.Steps {
				if st.Done(s) {
					parts = append(parts, c.ID)
				}
			}
		}
	}
	out, _ := CloseParts(parts, goos)
	return out
}

func runChoose(c *Ctx) error {
	st := c.St
	parts, added := CloseParts(strings.Split(c.In["parts"], ","), st.OS)
	if err := st.SetParts(parts); err != nil {
		return err
	}
	var names []string
	for _, id := range parts {
		names = append(names, ComponentByID(id).Name)
	}
	c.J.OK("Your stack: %s", strings.Join(names, ", "))
	for _, id := range added {
		c.J.Info("Also added %s (needed by something you picked)", ComponentByID(id).Name)
	}
	switch StackApp(st) {
	case "marquee":
		c.J.OK("Stack app: Atlas (the Media Hub keeps working in the background)")
	case "hub":
		c.J.OK("Stack app: the Media Hub in your browser, with Jellyfin as the player")
	}
	var installed []string
	for _, id := range st.DeployedApps() {
		if comp := componentOf(id); comp != "" && !st.Picked(comp) {
			installed = append(installed, ComponentByID(comp).Name)
		}
	}
	if len(installed) > 0 {
		c.J.Warn("Already installed and left as they are (unticking hides their step, it doesn't uninstall): %s",
			strings.Join(installed, ", "))
	}
	return nil
}
