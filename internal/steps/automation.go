package steps

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "mediasorter", Title: "MediaSorter (manual downloads)", Group: "Automation", Button: "Install MediaSorter",
		Needs: []string{"sonarr", "radarr", "jellyfin"}, Fields: sorterFields, Run: runSorter})
	step(&Step{ID: "hub", Title: "Media Hub (one dashboard)", Group: "Automation", Button: "Install the Media Hub",
		Needs: []string{"mediasorter"}, Run: runHub,
		Links: func(st *core.Stack) []Link { return []Link{linkTo(st, "hub", "Open the Media Hub")} }})
}

// appInfo describes each app to the Media Hub and the health check.
type appInfo struct {
	Name   string `json:"name"`
	Role   string `json:"role"`
	URL    string `json:"url"`            // how the hub reaches it
	Port   int    `json:"port,omitempty"` // where people open it
	Key    string `json:"key,omitempty"`
	Remote bool   `json:"remote,omitempty"`     // reachable from phones over Tailscale
	Local  bool   `json:"local_only,omitempty"` // only answers on the server itself
	Embed  bool   `json:"embed"`
	Link   string `json:"link,omitempty"`
	Log    string `json:"log,omitempty"`  // file glob, or "docker:<service>"
	Task   string `json:"task,omitempty"` // Windows scheduled task name
	Svc    string `json:"service,omitempty"`
}

var roles = map[string]string{
	"jellyfin": "Watch", "seerr": "Requests", "sonarr": "TV & anime", "radarr": "Movies", "prowlarr": "Indexers",
	"qbittorrent": "Downloads", "bazarr": "Subtitles", "cleanuparr": "Stuck-download cleanup", "jfago": "Friend & family invites",
	"requestrr": "Discord requests", "autobrr": "IRC autodownloads", "trawl": "Cloudflare helper", "byparr": "Cloudflare helper",
	"unpackerr": "Archive extraction", "mediasorter": "Sorts manual downloads", "recyclarr": "Quality profiles", "gluetun": "VPN",
}

// logGlob says where each app writes its log (as the hub sees it).
func logGlob(st *core.Stack, id string) string {
	if st.Docker() {
		c := "/appconfig/" + id
		switch id {
		case "jellyfin":
			return c + "/log/log_*.log"
		case "qbittorrent":
			return c + "/qBittorrent/logs/qbittorrent.log"
		case "sonarr", "radarr", "prowlarr":
			return c + "/logs/" + id + ".txt"
		case "bazarr":
			return c + "/log/bazarr.log"
		case "seerr":
			return c + "/logs/seerr-*.log"
		case "cleanuparr":
			return c + "/logs/cleanuparr-*.txt"
		case "unpackerr":
			return c + "/unpackerr.log"
		case "mediasorter":
			return c + "/logs/mediasorter.log"
		}
		return "docker:" + st.Service(id)
	}
	c := st.ConfigDir(id)
	switch id {
	case "jellyfin":
		return filepath.Join(c, "log", "log_*.log")
	case "qbittorrent":
		return filepath.Join(c, "qBittorrent", "data", "logs", "qbittorrent.log")
	case "sonarr", "radarr", "prowlarr":
		return filepath.Join(c, "logs", id+".txt")
	case "bazarr":
		return filepath.Join(c, "log", "bazarr.log")
	case "seerr":
		return filepath.Join(c, "logs", "seerr-*.log")
	case "cleanuparr":
		return filepath.Join(st.AppDir(id), "config", "logs", "cleanuparr-*.txt")
	case "unpackerr":
		return filepath.Join(c, "unpackerr.log")
	case "mediasorter":
		return filepath.Join(c, "logs", "mediasorter.log")
	}
	return filepath.Join(st.LogDir(), id+".log")
}

// WriteAppConfig writes <root>/config/mediastack.json for MediaSorter, the Media Hub and the health check.
func WriteAppConfig(st *core.Stack) error {
	view := func(p string) string { return st.InApp("mediasorter", p) }
	apps := map[string]appInfo{}
	for _, id := range st.DeployedApps() {
		a := platform.Apps[id]
		if a == nil || id == "healthcheck" {
			continue
		}
		info := appInfo{Name: a.Name, Role: roles[id], Key: st.Key(id), Log: logGlob(st, id)}
		if p, ok := core.BasePorts[id]; ok {
			info.URL = fmt.Sprintf("http://%s:%d", st.AppHost(id), st.AppPort(id))
			if st.Docker() && id == "qbittorrent" && st.VPN.Mode != "gluetun" {
				info.URL = fmt.Sprintf("http://qbittorrent:%d", p)
			}
			info.Port = st.Port(id)
			info.Embed = true
		}
		switch id {
		case "jellyfin":
			info.Embed, info.Link, info.Remote = false, "/web/", true
		case "seerr", "jfago":
			info.Remote = true
		case "qbittorrent":
			info.Embed, info.Link = false, "/" // its Web UI refuses to be framed
		case "trawl":
			info.Embed, info.Link = false, "/dashboard"
		case "byparr", "hub":
			info.Embed = false
		}
		if a.Docker != nil && a.Docker.Local {
			info.Local = true
		}
		if st.Docker() {
			info.Svc = st.Service(id)
		} else {
			info.Task = a.Name
		}
		apps[id] = info
	}
	mkvmerge := "mkvmerge"
	if !st.Docker() {
		mkvmerge = filepath.Join(os.Getenv("ProgramFiles"), "MKVToolNix", "mkvmerge.exe")
	}
	// state_dir: MediaSorter's database as the hub sees it; each process's own folder can be overridden
	// with MEDIASTACK_STATE (the containers mount theirs at /state).
	state := st.ConfigDir("mediasorter")
	hubState := st.ConfigDir("hub")
	listen := st.Port("hub")
	if st.Docker() {
		state, hubState, listen = "/sorter", "/state", core.BasePorts["hub"]
	}
	conf := core.M{
		"version":  1,
		"platform": map[bool]string{true: "docker", false: "windows"}[st.Docker()],
		"name":     st.Name,
		"root":     st.Root,
		"library": core.M{"movies": view(st.MediaDir("movies")), "tv": view(st.MediaDir("tv")),
			"anime": view(st.MediaDir("anime")), "anime_movies": view(st.MediaDir("anime_movies"))},
		"downloads_complete":   view(filepath.Join(st.DownloadsDir(), "complete")),
		"downloads_incomplete": view(filepath.Join(st.DownloadsDir(), "incomplete")),
		"drop_folder":          view(st.DropDir()),
		"recycle_bin":          view(st.RecycleDir()),
		"state_dir":            state,
		"hub_state_dir":        hubState,
		"apps":                 apps,
		"vpn":                  core.M{"mode": st.VPN.Mode, "adapter": st.VPN.Adapter},
		"lan_ip":               LANAddress(),
		"tailscale_ip":         st.Opt("tailscale_ip"),
		"hub": core.M{"listen_port": listen, "public_port": st.Port("hub"),
			// Windows: no login on this PC, Jellyfin-admin login over Tailscale. Linux server: login from
			// the home network and Tailscale (a container can't tell "this computer" apart).
			"access": map[bool]string{true: "lan-login", false: "local-or-tailscale"}[st.Docker()]},
		"tracks": core.M{
			"enabled": st.Opt("tracks_enabled") != "false", "mkvmerge": mkvmerge,
			"audio": splitList(st.Opt("tracks_audio"), "eng"), "anime_audio": splitList(st.Opt("tracks_anime_audio"), "eng,jpn"),
			"subtitles": splitList(st.Opt("tracks_subs"), "eng"),
		},
		// The Media Hub's watchdog: restarts apps that go down. Set ollama_host/ollama_model to a local Ollama
		// model to have it write the incident diagnosis; otherwise it's read from the app's log.
		"watchdog":               core.M{"minutes": 5, "auto_restart": true, "ollama_host": "", "ollama_model": ""},
		"managed_categories":     []string{"sonarr", "radarr"},
		"sweep_interval_seconds": map[bool]int{true: 60, false: 300}[st.Docker()],
		"drop_settle_seconds":    60,
		"min_video_mb":           40,
		"tailscale_name":         st.Opt("tailscale_name"),
		"task_folder":            `\` + st.Name + `\`,
		"compose_project":        platform.Project(st),
	}
	if st.Docker() && st.AdminPass != "" {
		// Containers can't use qBittorrent's "no login from this computer" exception.
		conf["qbittorrent_login"] = core.M{"username": st.AdminUser, "password": st.AdminPass}
	} else if st.Docker() {
		// Keep the login written earlier when setup restarts without the password.
		if old, err := os.ReadFile(appConfigFile(st)); err == nil {
			var prev core.M
			if json.Unmarshal(old, &prev) == nil && prev["qbittorrent_login"] != nil {
				conf["qbittorrent_login"] = prev["qbittorrent_login"]
			}
		}
	}
	b, _ := json.MarshalIndent(conf, "", "  ")
	if err := core.WriteFileAtomic(appConfigFile(st), b, 0o600); err != nil {
		return err
	}
	return platform.Chown(st, appConfigFile(st))
}

func appConfigFile(st *core.Stack) string {
	return filepath.Join(st.Root, "config", "shared", "mediastack.json")
}

func splitList(s, def string) []string {
	if strings.TrimSpace(s) == "" {
		s = def
	}
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.ToLower(strings.TrimSpace(p)); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// ---------------------------------------------------------------------------------------------- MediaSorter

func sorterFields(c *Ctx) []Field {
	def := func(k, d string) string {
		if c.St != nil && c.St.Opt(k) != "" {
			return c.St.Opt(k)
		}
		return d
	}
	return []Field{
		{Name: "tracks", Label: "Remove unwanted audio and subtitle languages from new files (lossless)", Type: "checkbox",
			Default: map[bool]string{true: "false", false: "true"}[def("tracks_enabled", "true") == "false"]},
		{Name: "audio", Label: "Audio languages to keep", Type: "text", Default: def("tracks_audio", "eng"),
			Help: "Three-letter codes, comma separated: eng, spa, fre, ger, ita, jpn, kor... Files with none of these keep all their audio."},
		{Name: "anime_audio", Label: "Audio languages to keep for anime", Type: "text", Default: def("tracks_anime_audio", "eng,jpn"),
			Help: "eng,jpn keeps both the English dub and the original Japanese."},
		{Name: "subs", Label: "Subtitle languages to keep", Type: "text", Default: def("tracks_subs", "eng")},
	}
}

func runSorter(c *Ctx) error {
	st := c.St
	st.SetOpt("tracks_enabled", fmt.Sprint(c.In["tracks"] == "true"))
	st.SetOpt("tracks_audio", strings.Join(splitList(c.In["audio"], "eng"), ","))
	st.SetOpt("tracks_anime_audio", strings.Join(splitList(c.In["anime_audio"], "eng,jpn"), ","))
	st.SetOpt("tracks_subs", strings.Join(splitList(c.In["subs"], "eng"), ","))
	if !st.Docker() && c.In["tracks"] == "true" {
		if _, err := os.Stat(filepath.Join(os.Getenv("ProgramFiles"), "MKVToolNix", "mkvmerge.exe")); err != nil {
			if err := platform.Winget(c, c.J, "MoritzBunkus.MKVToolNix"); err != nil {
				c.J.Warn("MKVToolNix didn't install (%v) - language cleanup stays off until it's installed", err)
			}
		}
	}
	// Deploy first (it installs Python on Windows), then write the settings it reads.
	st.SetDeployed("mediasorter", st.DeployedVersion("mediasorter"))
	if err := WriteAppConfig(st); err != nil {
		return err
	}
	if err := c.Deploy("mediasorter", ""); err != nil {
		return err
	}
	if err := WriteAppConfig(st); err != nil {
		return err
	}
	if !st.Docker() {
		// qBittorrent hands every finished manual download to MediaSorter.
		py := filepath.Join(st.Root, "apps", "python-env", "Scripts", "pythonw.exe")
		script := filepath.Join(st.Root, "apps", "mediastack-py", "mediasorter", "mediasorter.py")
		prog := fmt.Sprintf(`"%s" "%s" import "%%F" --category "%%L" --hash "%%I" --name "%%N"`, py, script)
		api, err := Qbt(st)
		if err == nil {
			err = qbtPrefs(api, core.M{"autorun_enabled": true, "autorun_program": prog})
		}
		if err != nil {
			c.J.Warn("Couldn't hook MediaSorter into qBittorrent (%v) - it still sorts finished downloads every 5 minutes", err)
		} else {
			c.J.OK("qBittorrent hands every finished download to MediaSorter")
		}
	}
	c.J.OK("Drop video files into %s, or download manually with category movies/tv/anime - they're sorted into the library", st.DropDir())
	return nil
}

// ---------------------------------------------------------------------------------------------- Media Hub

func runHub(c *Ctx) error {
	st := c.St
	st.SetDeployed("hub", st.DeployedVersion("hub"))
	if err := WriteAppConfig(st); err != nil {
		return err
	}
	if err := c.Deploy("hub", "/api/me"); err != nil {
		return err
	}
	if !st.Docker() {
		if err := c.P.Deploy(c, st, c.J, "healthcheck"); err != nil {
			c.J.Warn("Hourly health check: %v", err)
		} else {
			c.J.OK("Hourly health check scheduled")
		}
		desktopShortcut(c, "Media Hub", OpenURL(st, "hub"))
	}
	hubHooks(c)
	return WriteAppConfig(st)
}

// hubHooks makes Sonarr, Radarr, Prowlarr and Seerr report to the Media Hub's activity feed.
func hubHooks(c *Ctx) {
	st := c.St
	hook := st.AppURL("hub") + "/api/hook/"
	for _, id := range []string{"sonarr", "radarr"} {
		if !st.IsDeployed(id) {
			continue
		}
		add := "onSeriesAdd"
		if id == "radarr" {
			add = "onMovieAdded"
		}
		if err := ensureWebhook(c.Arr(id), "Media Hub", hook+id, []string{"onGrab", "onDownload", "onUpgrade",
			"onHealthIssue", "includeHealthWarnings", "onHealthRestored", "onManualInteractionRequired", add}); err != nil {
			c.J.Warn("%s -> Media Hub: %v", platform.Get(id).Name, err)
		}
	}
	if st.IsDeployed("prowlarr") {
		if err := ensureWebhook(c.Arr("prowlarr"), "Media Hub", hook+"prowlarr",
			[]string{"onHealthIssue", "includeHealthWarnings", "onHealthRestored"}); err != nil {
			c.J.Warn("Prowlarr -> Media Hub: %v", err)
		}
	}
	if st.IsDeployed("seerr") && st.Key("seerr") != "" {
		s := core.NewAPI(st.URL("seerr")+"/api/v1", map[string]string{"X-Api-Key": st.Key("seerr")})
		payload, _ := json.Marshal(core.M{"notification_type": "{{notification_type}}", "event": "{{event}}",
			"subject": "{{subject}}", "message": "{{message}}", "image": "{{image}}", "media_type": "{{media_type}}",
			"tmdbId": "{{media_tmdbid}}", "requested_by": "{{requestedBy_username}}"})
		err := s.Post("/settings/notifications/webhook", core.M{"enabled": true, "embedPoster": true,
			"types":   2 | 4 | 8 | 16 | 64 | 128 | 256,
			"options": core.M{"webhookUrl": hook + "seerr", "jsonPayload": string(payload)}}, nil)
		if err != nil {
			c.J.Warn("Seerr -> Media Hub: %v", err)
		}
	}
	c.J.OK("Sonarr, Radarr, Prowlarr and Seerr report to the Media Hub's activity feed")
}

func desktopShortcut(c *Ctx, name, target string) {
	if c.St.Name != "Media Stack" {
		name += " (" + c.St.Name + ")" // a second install never replaces the first one's shortcut
	}
	dir := platform.DesktopDir() // Windows' real Desktop (OneDrive can move it)
	if dir == "" {
		return
	}
	file := filepath.Join(dir, name+".url")
	body := "[InternetShortcut]\r\nURL=" + target + "\r\n"
	if old, err := os.ReadFile(file); err == nil && !strings.Contains(string(old), "URL="+target) {
		c.J.Warn("Left the existing desktop shortcut %q alone (it points somewhere else)", name)
		return
	}
	if err := os.WriteFile(file, []byte(body), 0o644); err == nil {
		c.J.OK("Desktop shortcut: %s", name)
	}
}
