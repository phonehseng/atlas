package steps

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "prowlarr", Title: "Prowlarr", Group: "Find & organise", Button: "Install Prowlarr",
		Needs: []string{"account"}, Password: true, Run: runProwlarr, Links: links("prowlarr")})
	step(&Step{ID: "sonarr", Title: "Sonarr (TV & anime)", Group: "Find & organise", Button: "Install Sonarr",
		Needs: []string{"qbittorrent", "prowlarr"}, Password: true, Run: func(c *Ctx) error { return runArr(c, "sonarr") },
		Links: links("sonarr")})
	step(&Step{ID: "radarr", Title: "Radarr (movies)", Group: "Find & organise", Button: "Install Radarr",
		Needs: []string{"qbittorrent", "prowlarr"}, Password: true, Run: func(c *Ctx) error { return runArr(c, "radarr") },
		Links: links("radarr")})
	step(&Step{ID: "lidarr", Title: "Lidarr (music)", Group: "Find & organise", Optional: true, Button: "Install Lidarr",
		Needs: []string{"qbittorrent", "prowlarr"}, Password: true, Run: runLidarr, Links: links("lidarr")})
	step(&Step{ID: "recyclarr", Title: "Quality profiles (grab, then upgrade)", Group: "Find & organise", Button: "Install Recyclarr",
		Needs: []string{"sonarr", "radarr"}, Run: runRecyclarr})
	step(&Step{ID: "trawl", Title: "Cloudflare helper: trawl", Group: "Find & organise", Optional: true, Button: "Install trawl",
		Needs: []string{"prowlarr"}, Run: func(c *Ctx) error { return runSolver(c, "trawl", "/health") }})
	step(&Step{ID: "byparr", Title: "Cloudflare helper: Byparr", Group: "Find & organise", Optional: true, Button: "Install Byparr",
		Needs: []string{"prowlarr"}, Run: func(c *Ctx) error { return runSolver(c, "byparr", "/health") }})
	step(&Step{ID: "bazarr", Title: "Bazarr (subtitles)", Group: "Find & organise", Button: "Install Bazarr",
		Needs: []string{"sonarr", "radarr"}, Password: true, Run: runBazarr, Links: links("bazarr")})
	step(&Step{ID: "unpackerr", Title: "Unpackerr (archives)", Group: "Find & organise", Button: "Install Unpackerr",
		Needs: []string{"sonarr", "radarr"}, Run: runUnpackerr})
	step(&Step{ID: "cleanuparr", Title: "Cleanuparr (stuck downloads)", Group: "Find & organise", Button: "Install Cleanuparr",
		Needs: []string{"sonarr", "radarr"}, Password: true, Run: runCleanuparr, Links: links("cleanuparr")})
}

// arrLogin turns on Sonarr/Radarr/Prowlarr's login with the admin account. On Windows, pages opened on
// this PC or the home network skip it (as before); on a Linux server it's always asked.
func arrLogin(c *Ctx, id string) error {
	api := c.Arr(id)
	var host core.M
	if err := api.Get("/config/host", &host); err != nil {
		return err
	}
	pass := c.Pass()
	host["authenticationMethod"] = "forms"
	if c.St.Docker() {
		host["authenticationRequired"] = "enabled"
	} else {
		host["authenticationRequired"] = "disabledForLocalAddresses"
		host["allowedHosts"] = strings.Join(hostNames(), ",") // required for the local-network exception
	}
	host["username"], host["password"], host["passwordConfirmation"] = c.St.AdminUser, pass, pass
	host["analyticsEnabled"], host["launchBrowser"] = false, false
	if err := api.Put(fmt.Sprintf("/config/host/%v", host["id"]), host, nil); err != nil {
		return err
	}
	c.J.OK("%s login set (user %s)", platform.Get(id).Name, c.St.AdminUser)
	return nil
}

// ---------------------------------------------------------------------------------------------- Prowlarr

func runProwlarr(c *Ctx) error {
	if err := c.Deploy("prowlarr", "/ping"); err != nil {
		return err
	}
	return arrLogin(c, "prowlarr")
}

// ---------------------------------------------------------------------------------------------- Sonarr / Radarr

const subtitleExts = "srt,ass,ssa,sub,idx,sup,vtt"

func runArr(c *Ctx, id string) error {
	st := c.St
	name := platform.Get(id).Name
	if err := c.Deploy(id, "/ping"); err != nil {
		return err
	}
	api := c.Arr(id)
	in := func(p string) string { return c.InApp(id, p) }

	// Library folders.
	roots := []string{"tv", "anime"}
	if id == "radarr" {
		roots = []string{"movies", "anime_movies"}
	}
	for _, k := range roots {
		if err := ensureRoot(api, in(st.MediaDir(k))); err != nil {
			return fmt.Errorf("adding library folder %s: %w", st.MediaDir(k), err)
		}
	}
	c.J.OK("%s library folders: %s", name, strings.Join(roots, ", "))

	// File naming: clean titles with the TVDB/TMDB id in the folder so Jellyfin matches exactly.
	common := core.M{"copyUsingHardlinks": true, "importExtraFiles": true, "extraFileExtensions": subtitleExts,
		"deleteEmptyFolders": true, "skipFreeSpaceCheckWhenImporting": true,
		"recycleBin": in(st.RecycleDir()), "recycleBinCleanupDays": 14, "downloadPropersAndRepacks": "doNotPrefer"}
	if id == "sonarr" {
		if err := update(api, "/config/naming", core.M{
			"renameEpisodes": true, "colonReplacementFormat": 4,
			"standardEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title} [{Quality Full}]",
			"dailyEpisodeFormat":    "{Series Title} - {Air-Date} - {Episode Title} [{Quality Full}]",
			"animeEpisodeFormat":    "{Series Title} - S{season:00}E{episode:00} - {absolute:000} - {Episode Title} [{Quality Full}]",
			"seriesFolderFormat":    "{Series TitleYear} [tvdbid-{TvdbId}]",
			"seasonFolderFormat":    "Season {season:00}", "specialsFolderFormat": "Season 00",
		}); err != nil {
			return err
		}
		common["createEmptySeriesFolders"] = false
		common["autoUnmonitorPreviouslyDownloadedEpisodes"] = true
	} else {
		if err := update(api, "/config/naming", core.M{
			"renameMovies": true, "colonReplacementFormat": "smart",
			"standardMovieFormat": "{Movie Title} ({Release Year}) [{Quality Full}]",
			"movieFolderFormat":   "{Movie Title} ({Release Year}) [tmdbid-{TmdbId}]",
		}); err != nil {
			return err
		}
		common["createEmptyMovieFolders"] = false
		common["autoUnmonitorPreviouslyDownloadedMovies"] = true
	}
	if err := update(api, "/config/mediamanagement", common); err != nil {
		return err
	}
	if err := update(api, "/config/downloadclient", core.M{"enableCompletedDownloadHandling": true,
		"autoRedownloadFailed": true, "autoRedownloadFailedFromInteractiveSearch": true}); err != nil {
		return err
	}
	c.J.OK("Naming, hardlinks, 14-day recycle bin and automatic retry of failed downloads are set")

	if err := ensureQbtClient(c, id); err != nil {
		return fmt.Errorf("connecting %s to qBittorrent: %w", name, err)
	}
	c.J.OK("%s sends downloads to qBittorrent (category %q)", name, id)

	if err := ensureProwlarrApp(c, id); err != nil {
		return fmt.Errorf("connecting Prowlarr to %s: %w", name, err)
	}
	c.J.OK("Prowlarr shares its indexers with %s", name)

	if st.IsDeployed("jellyfin") && st.Key("jellyfin") != "" {
		if err := ensureJellyfinConnect(c, id); err != nil {
			c.J.Warn("Couldn't link %s to Jellyfin (%v) - Jellyfin's folder watching still picks up new files", name, err)
		} else {
			c.J.OK("%s tells Jellyfin about every import", name)
		}
	}
	return arrLogin(c, id)
}

func ensureRoot(api *core.API, path string) error {
	var roots []struct{ Path string }
	if err := api.Get("/rootfolder", &roots); err != nil {
		return err
	}
	for _, r := range roots {
		if strings.EqualFold(strings.TrimRight(r.Path, `\/`), strings.TrimRight(path, `\/`)) {
			return nil
		}
	}
	return api.Post("/rootfolder", core.M{"path": path}, nil)
}

func ensureQbtClient(c *Ctx, id string) error {
	api := c.Arr(id)
	var clients []core.M
	if err := api.Get("/downloadclient", &clients); err != nil {
		return err
	}
	var obj core.M
	for _, cl := range clients {
		if cl["implementation"] == "QBittorrent" {
			obj = cl
		}
	}
	if obj == nil {
		var schemas []core.M
		if err := api.Get("/downloadclient/schema", &schemas); err != nil {
			return err
		}
		s, err := schema(schemas, "QBittorrent")
		if err != nil {
			return err
		}
		obj = s
	}
	field(obj, "host", c.St.AppHost("qbittorrent"))
	field(obj, "port", c.St.AppPort("qbittorrent"))
	if c.St.Docker() {
		field(obj, "username", c.St.AdminUser)
		field(obj, "password", c.Pass())
	}
	switch id {
	case "sonarr":
		field(obj, "tvCategory", "sonarr")
	case "lidarr":
		field(obj, "musicCategory", "lidarr")
	default:
		field(obj, "movieCategory", "radarr")
	}
	obj["name"], obj["enable"], obj["priority"] = "qBittorrent", true, 1
	obj["removeCompletedDownloads"], obj["removeFailedDownloads"] = true, true
	if obj["id"] != nil {
		return api.Put(fmt.Sprintf("/downloadclient/%v", obj["id"]), obj, nil)
	}
	return api.Post("/downloadclient", obj, nil)
}

func ensureProwlarrApp(c *Ctx, id string) error {
	p := c.Arr("prowlarr")
	impl := platform.Get(id).Name // "Sonarr" / "Radarr"
	var apps []core.M
	if err := p.Get("/applications", &apps); err != nil {
		return err
	}
	var obj core.M
	for _, a := range apps {
		if a["implementation"] == impl {
			obj = a
		}
	}
	if obj == nil {
		var schemas []core.M
		if err := p.Get("/applications/schema", &schemas); err != nil {
			return err
		}
		s, err := schema(schemas, impl)
		if err != nil {
			return err
		}
		obj = s
	}
	field(obj, "prowlarrUrl", c.St.AppURL("prowlarr"))
	field(obj, "baseUrl", c.St.AppURL(id))
	field(obj, "apiKey", c.St.Key(id))
	obj["name"], obj["syncLevel"] = impl, "fullSync"
	if obj["id"] != nil {
		return p.Put(fmt.Sprintf("/applications/%v", obj["id"]), obj, nil)
	}
	return p.Post("/applications", obj, nil)
}

func ensureJellyfinConnect(c *Ctx, id string) error {
	api := c.Arr(id)
	var notes []core.M
	if err := api.Get("/notification", &notes); err != nil {
		return err
	}
	for _, n := range notes {
		if n["name"] == "Jellyfin" {
			api.Delete(fmt.Sprintf("/notification/%v", n["id"]))
		}
	}
	var schemas []core.M
	if err := api.Get("/notification/schema", &schemas); err != nil {
		return err
	}
	s, err := schema(schemas, "MediaBrowser")
	if err != nil {
		return err
	}
	field(s, "host", c.St.AppHost("jellyfin"))
	field(s, "port", c.St.AppPort("jellyfin"))
	field(s, "useSsl", false)
	field(s, "apiKey", c.St.Key("jellyfin"))
	field(s, "updateLibrary", true)
	field(s, "notify", false)
	s["name"] = "Jellyfin"
	for _, k := range []string{"onDownload", "onUpgrade", "onRename", "onSeriesDelete", "onEpisodeFileDelete", "onMovieDelete",
		"onMovieFileDelete", "onReleaseImport", "onTrackRetag", "onArtistDelete", "onAlbumDelete"} {
		if _, ok := s[k]; ok {
			s[k] = true
		}
	}
	return api.Post("/notification", s, nil)
}

// ensureWebhook adds/replaces a webhook connection in Sonarr/Radarr/Prowlarr.
func ensureWebhook(api *core.API, name, hook string, events []string) error {
	var notes []core.M
	if err := api.Get("/notification", &notes); err != nil {
		return err
	}
	for _, n := range notes {
		if n["name"] == name {
			api.Delete(fmt.Sprintf("/notification/%v", n["id"]))
		}
	}
	var schemas []core.M
	if err := api.Get("/notification/schema", &schemas); err != nil {
		return err
	}
	s, err := schema(schemas, "Webhook")
	if err != nil {
		return err
	}
	field(s, "url", hook)
	field(s, "method", 1) // POST
	s["name"] = name
	for _, ev := range events {
		if _, ok := s[ev]; ok {
			s[ev] = true
		}
	}
	// forceSave: the target may not answer Sonarr's test request yet.
	return api.Post("/notification?forceSave=true", s, nil)
}

// ---------------------------------------------------------------------------------------------- Recyclarr

// TRaSH Guides templates: 4K remux first (1080p only when there's no 4K), anime as 1080p remux.
var recyclarrTemplates = map[string][]string{
	"sonarr": {"sonarr-remux-2160p-combined", "web-1080p", "sonarr-anime-remux-1080p"},
	"radarr": {"radarr-remux-2160p-combined", "hd-bluray-web", "uhd-bluray-web", "radarr-anime-remux-1080p"},
}

const templatesBase = "https://raw.githubusercontent.com/recyclarr/config-templates/master/"

func runRecyclarr(c *Ctx) error {
	st := c.St
	conf, err := RecyclarrConfig(c, st)
	if err != nil {
		return fmt.Errorf("building the quality settings: %w", err)
	}
	if err := core.WriteFileAtomic(filepath.Join(st.ConfigDir("recyclarr"), "recyclarr.yml"), conf, 0o600); err != nil {
		return err
	}
	if err := c.Deploy("recyclarr", ""); err != nil {
		return err
	}
	c.J.Info("Syncing TRaSH Guides quality profiles into Sonarr and Radarr (about a minute)...")
	if st.Docker() {
		time.Sleep(3 * time.Second) // let the container settle before exec
	}
	// SD sizes aren't in TRaSH's TV sizes (Recyclarr refuses them), so lower those floors directly - before
	// the sync: Sonarr applies size updates a few seconds late, so reading them back after it would undo it.
	var sizes []core.M
	if c.Arr("sonarr").Get("/qualitydefinition", &sizes) == nil {
		for _, d := range sizes {
			q, _ := d["quality"].(map[string]any)
			if sizeFloors[fmt.Sprint(q["name"])] == 1 {
				if m, _ := d["minSize"].(float64); m > 1 {
					d["minSize"] = 1
				}
			}
		}
		c.Arr("sonarr").Put("/qualitydefinition/update", sizes, nil)
		time.Sleep(5 * time.Second)
	}
	if err := c.P.RunNow(c, st, c.J, "recyclarr"); err != nil {
		return fmt.Errorf("recyclarr sync failed: %w", err)
	}
	c.J.OK("Quality profiles: anything watchable is grabbed first, then upgraded. Films up to 4K (\"Remux 2160p (Combined)\"), " +
		"shows up to 1080p (\"1080p\"), anime up to 1080p remux (\"[Anime] Remux-1080p\"). Re-synced weekly.")
	return nil
}

// Grab whatever is available first, then upgrade: films up to 4K remux, shows up to 1080p, anime up to its
// 1080p remux. Each profile accepts every real resolution below its target, grouped so that within a
// resolution TRaSH's scores pick the best release. The worst-but-watchable formats (YTS-style encodes, x265,
// upscales) become last resorts instead of blocked; discs, extras, 3D, dubbed-only versions and raws stay
// blocked.
var (
	hdGroups = []any{
		map[string]any{"name": "1080p", "qualities": []any{"WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p", "HDTV-1080p"}},
		map[string]any{"name": "720p", "qualities": []any{"WEBDL-720p", "WEBRip-720p", "Bluray-720p", "HDTV-720p"}},
	}
	uhdGroup = map[string]any{"name": "2160p", "qualities": []any{"WEBDL-2160p", "WEBRip-2160p", "Bluray-2160p", "HDTV-2160p"}}
	sdTV     = map[string]any{"name": "SD", "qualities": []any{"Bluray-576p", "Bluray-480p", "WEBDL-480p", "WEBRip-480p", "DVD", "SDTV"}}
	sdFilm   = map[string]any{"name": "SD", "qualities": []any{"Bluray-576p", "Bluray-480p", "WEBDL-480p", "WEBRip-480p", "DVD", "DVD-R", "SDTV"}}
)

func qualityList(parts ...any) []any {
	var out []any
	for _, p := range parts {
		switch v := p.(type) {
		case string:
			out = append(out, map[string]any{"name": v})
		case []any:
			out = append(out, v...)
		default:
			out = append(out, v)
		}
	}
	return out
}

// profileRules: per service, guide profile trash_id -> settings Recyclarr applies on top of the guide.
var profileRules = map[string]map[string]map[string]any{
	"sonarr": {
		"911df0e05a395c19d8b3efc76a7467c1": { // Remux 2160p (Combined): for shows someone wants in 4K
			"upgrade":   map[string]any{"allowed": true, "until_quality": "Bluray-2160p Remux", "until_score": 10000},
			"qualities": qualityList("Bluray-2160p Remux", uhdGroup, "Bluray-1080p Remux", hdGroups, sdTV)},
		"72dae194fc92bf828f32cde7744e51a1": { // WEB-1080p, renamed: the default for shows
			"name":      "1080p",
			"upgrade":   map[string]any{"allowed": true, "until_quality": "1080p", "until_score": 10000},
			"qualities": qualityList(hdGroups, sdTV)},
		"20e0fc959f1f1704bed501f23bdae76f": {}, // [Anime] Remux-1080p
	},
	"radarr": {
		"d1d310673359205736b4b84acd5ea8c8": { // Remux 2160p (Combined): the default for films
			"upgrade":   map[string]any{"allowed": true, "until_quality": "Remux-2160p", "until_score": 10000},
			"qualities": qualityList("Remux-2160p", uhdGroup, "Remux-1080p", hdGroups, sdFilm)},
		"722b624f9af1e492284c4bc842153a38": {}, // [Anime] Remux-1080p
	},
}

// softened: profile trash_id -> custom formats (trash_id: score) the guide blocks with -10000 there.
var softened = map[string]map[string]int{
	"911df0e05a395c19d8b3efc76a7467c1": {"9c11cd3f07101cdba90a2d81cf0e56b4": -1000, "e2315f990da2e2cbfc9fa5b7a6fcfe48": -1000, // LQ, LQ (Release Title)
		"32b367365729d530ca1c124a0b180c64": -1000, "9b64dff695c2115facf1b6ea59c9bd07": -500, // Bad Dual Groups, x265 (no HDR/DV)
		"23297a736ca77c0fc8e70f8edd7ee56c": -2000, "15a05bc7c1a36e2b57fd628f8977e2fc": -2000}, // Upscaled, AV1
	"72dae194fc92bf828f32cde7744e51a1": {"9c11cd3f07101cdba90a2d81cf0e56b4": -1000, "e2315f990da2e2cbfc9fa5b7a6fcfe48": -1000,
		"32b367365729d530ca1c124a0b180c64": -1000, "47435ece6b99a0b477caf360e79ba0bb": -500, // x265 (HD)
		"23297a736ca77c0fc8e70f8edd7ee56c": -2000, "15a05bc7c1a36e2b57fd628f8977e2fc": -2000},
	"20e0fc959f1f1704bed501f23bdae76f": {"15a05bc7c1a36e2b57fd628f8977e2fc": -2000, "e3515e519f3b1360cbfc17651944354c": -1000}, // AV1, Anime LQ Groups
	"d1d310673359205736b4b84acd5ea8c8": {"90a6f9a284dff5103f6346090e6280c8": -1000, "e204b80c87be9497a8a6eaff48f72905": -1000,
		"b6832f586342ef70d9c128d40c07b872": -1000, "839bea857ed2c0a8e084f3cbdbd65ecb": -500,
		"bfd8eb01832d646a0a89c4deb46f8564": -2000, "cae4ca30163749b891686f95532519bd": -2000,
		"e6886871085226c3da1830830146846c": -2000}, // Generated Dynamic HDR
	"722b624f9af1e492284c4bc842153a38": {"cae4ca30163749b891686f95532519bd": -2000, "b0fdc5897f68c9a68c70c25169f77447": -1000},
}

// sizeFloors: the smallest believable file per quality, in MB per minute (TRaSH's are much higher).
var sizeFloors = map[string]float64{"SDTV": 1, "WEBRip-480p": 1, "WEBDL-480p": 1, "DVD": 1, "Bluray-480p": 1, "Bluray-576p": 1,
	"HDTV-720p": 3, "WEBRip-720p": 3, "WEBDL-720p": 3, "Bluray-720p": 3,
	"HDTV-1080p": 4, "WEBRip-1080p": 4, "WEBDL-1080p": 4, "Bluray-1080p": 4,
	"HDTV-2160p": 8, "WEBRip-2160p": 8, "WEBDL-2160p": 8, "Bluray-2160p": 8}

// applyUpgradeRules adds the grab-then-upgrade settings to one merged Recyclarr instance.
func applyUpgradeRules(service string, inst map[string]any) {
	profiles, _ := inst["quality_profiles"].([]any)
	var cfs []any
	for _, p := range profiles {
		prof, ok := p.(map[string]any)
		if !ok {
			continue
		}
		tid := fmt.Sprint(prof["trash_id"])
		rule, ok := profileRules[service][tid]
		if !ok {
			continue
		}
		for k, v := range rule {
			prof[k] = v
		}
		prof["min_format_score"] = -5000
		ids := make([]string, 0, len(softened[tid]))
		for cf := range softened[tid] {
			ids = append(ids, cf)
		}
		sort.Strings(ids)
		for _, cf := range ids {
			cfs = append(cfs, map[string]any{"trash_ids": []any{cf},
				"assign_scores_to": []any{map[string]any{"trash_id": tid, "score": softened[tid][cf]}}})
		}
	}
	existing, _ := inst["custom_formats"].([]any)
	inst["custom_formats"] = append(existing, cfs...)
	qd, _ := inst["quality_definition"].(map[string]any)
	if qd != nil {
		names := make([]string, 0, len(sizeFloors))
		for q, mb := range sizeFloors {
			if mb > 1 { // SD isn't in the guide's sizes; set directly after the sync
				names = append(names, q)
			}
		}
		sort.Strings(names)
		var qs []any
		for _, q := range names {
			qs = append(qs, map[string]any{"name": q, "min": sizeFloors[q]})
		}
		qd["qualities"] = qs
	}
}

// RecyclarrConfig merges the TRaSH templates into one Sonarr and one Radarr instance.
func RecyclarrConfig(ctx context.Context, st *core.Stack) ([]byte, error) {
	var index map[string][]struct{ Template, ID string }
	if err := core.GetJSON(ctx, templatesBase+"templates.json", &index); err != nil {
		return nil, err
	}
	out := map[string]any{}
	for _, service := range []string{"sonarr", "radarr"} {
		var merged map[string]any
		for _, tid := range recyclarrTemplates[service] {
			path := ""
			for _, t := range index[service] {
				if t.ID == tid {
					path = t.Template
				}
			}
			if path == "" {
				return nil, fmt.Errorf("TRaSH template %s not found", tid)
			}
			text, err := core.GetText(ctx, templatesBase+path)
			if err != nil {
				return nil, err
			}
			var doc map[string]map[string]map[string]any
			if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
				return nil, fmt.Errorf("%s: %w", path, err)
			}
			var inst map[string]any
			for _, v := range doc[service] {
				inst = v
			}
			if merged == nil {
				merged = inst
				continue
			}
			for _, key := range []string{"quality_profiles", "custom_formats", "custom_format_groups"} {
				v, ok := inst[key]
				if !ok {
					continue
				}
				if m, isMap := v.(map[string]any); isMap { // custom_format_groups: {add: [...]}
					cur, _ := merged[key].(map[string]any)
					if cur == nil {
						cur = map[string]any{}
					}
					have := map[any]bool{}
					list, _ := cur["add"].([]any)
					for _, g := range list {
						if gm, ok := g.(map[string]any); ok {
							have[gm["trash_id"]] = true
						}
					}
					more, _ := m["add"].([]any)
					for _, g := range more {
						if gm, ok := g.(map[string]any); ok && !have[gm["trash_id"]] {
							list = append(list, g)
						}
					}
					cur["add"] = list
					merged[key] = cur
				} else {
					a, _ := merged[key].([]any)
					b, _ := v.([]any)
					merged[key] = append(a, b...)
				}
			}
		}
		applyUpgradeRules(service, merged)
		merged["base_url"] = st.AppURL(service)
		merged["api_key"] = st.Key(service)
		out[service] = map[string]any{service: merged}
	}
	return yaml.Marshal(out)
}

// ---------------------------------------------------------------------------------------------- Cloudflare helpers

func runSolver(c *Ctx, id, health string) error {
	if err := c.Deploy(id, health); err != nil {
		return err
	}
	name := platform.Get(id).Name
	if err := ensureProxy(c, name, id, c.St.AppURL(id)+"/"); err != nil {
		return fmt.Errorf("adding %s to Prowlarr: %w", name, err)
	}
	c.J.OK("Prowlarr can route Cloudflare-protected sites through %s: give such an indexer the tag %q", name, id)
	return nil
}

func ensureProxy(c *Ctx, name, tag, host string) error {
	p := c.Arr("prowlarr")
	tagID, err := ensureTag(p, tag)
	if err != nil {
		return err
	}
	var proxies []core.M
	if err := p.Get("/indexerProxy", &proxies); err != nil {
		return err
	}
	var obj core.M
	for _, x := range proxies {
		if strings.EqualFold(fmt.Sprint(x["name"]), name) {
			obj = x
		}
	}
	if obj == nil {
		var schemas []core.M
		if err := p.Get("/indexerProxy/schema", &schemas); err != nil {
			return err
		}
		if obj, err = schema(schemas, "FlareSolverr"); err != nil {
			return err
		}
	}
	field(obj, "host", host)
	field(obj, "requestTimeout", 60)
	obj["name"], obj["tags"] = name, []int{tagID}
	if obj["id"] != nil {
		return p.Put(fmt.Sprintf("/indexerProxy/%v", obj["id"]), obj, nil)
	}
	return p.Post("/indexerProxy", obj, nil)
}

func ensureTag(api *core.API, label string) (int, error) {
	var tags []struct {
		ID    int `json:"id"`
		Label string
	}
	if err := api.Get("/tag", &tags); err != nil {
		return 0, err
	}
	for _, t := range tags {
		if strings.EqualFold(t.Label, label) {
			return t.ID, nil
		}
	}
	var made struct {
		ID int `json:"id"`
	}
	err := api.Post("/tag", core.M{"label": label}, &made)
	return made.ID, err
}

// ---------------------------------------------------------------------------------------------- Bazarr

func bazarrKey(st *core.Stack) (string, error) {
	file := filepath.Join(st.ConfigDir("bazarr"), "config", "config.yaml")
	b, err := os.ReadFile(file)
	if err != nil {
		return "", err
	}
	var conf struct {
		Auth struct{ Apikey string } `yaml:"auth"`
	}
	if err := yaml.Unmarshal(b, &conf); err != nil {
		return "", err
	}
	if conf.Auth.Apikey == "" {
		return "", fmt.Errorf("no API key in %s yet", file)
	}
	return conf.Auth.Apikey, nil
}

func runBazarr(c *Ctx) error {
	st := c.St
	if err := c.Deploy("bazarr", "/"); err != nil {
		return err
	}
	var key string
	var err error
	for i := 0; i < 30; i++ {
		if key, err = bazarrKey(st); err == nil {
			break
		}
		time.Sleep(2 * time.Second)
	}
	if err != nil {
		return err
	}
	st.SetKey("bazarr", key)

	// One profile: English subtitles, skipped when the audio is already English (so anime and foreign
	// films get subtitles, English shows are left alone).
	profile := `[{"profileId":1,"name":"English","cutoff":null,"items":[{"id":1,"language":"en","audio_exclude":"True",` +
		`"audio_only_include":"False","hi":"False","forced":"False"}],"mustContain":[],"mustNotContain":[],"originalFormat":false,"tag":null}]`
	form := url.Values{}
	set := func(k, v string) { form.Set(k, v) }
	set("languages-enabled", "en")
	set("languages-profiles", profile)
	set("settings-general-use_sonarr", "true")
	set("settings-general-use_radarr", "true")
	set("settings-sonarr-ip", st.AppHost("sonarr"))
	set("settings-sonarr-port", fmt.Sprint(st.AppPort("sonarr")))
	set("settings-sonarr-apikey", st.Key("sonarr"))
	set("settings-radarr-ip", st.AppHost("radarr"))
	set("settings-radarr-port", fmt.Sprint(st.AppPort("radarr")))
	set("settings-radarr-apikey", st.Key("radarr"))
	set("settings-general-serie_default_enabled", "true")
	set("settings-general-serie_default_profile", "1")
	set("settings-general-movie_default_enabled", "true")
	set("settings-general-movie_default_profile", "1")
	set("settings-general-use_embedded_subs", "true")
	set("settings-general-parse_embedded_audio_track", "true")
	set("settings-subtitlecat-include_machine_translated", "false")
	set("settings-general-minimum_score", "80")
	set("settings-subsync-use_subsync", "true")
	set("settings-subsync-use_subsync_threshold", "true")
	set("settings-subsync-subsync_threshold", "96")
	set("settings-subsync-use_subsync_movie_threshold", "true")
	set("settings-subsync-subsync_movie_threshold", "86")
	set("settings-auth-type", "form")
	set("settings-auth-username", st.AdminUser)
	set("settings-auth-password", c.Pass())
	for _, p := range []string{"tsukihime", "subtitlecat", "gestdown", "podnapisi", "yifysubtitles"} {
		form.Add("settings-general-enabled_providers", p)
	}
	bz := core.NewAPI(st.URL("bazarr"), map[string]string{"X-API-KEY": key})
	if err := bz.Post("/api/system/settings", form, nil); err != nil {
		return fmt.Errorf("saving Bazarr settings: %w", err)
	}
	c.J.OK("Bazarr: English subtitles for anime and foreign-language audio, free providers on, auto-sync on, login on")
	for _, id := range []string{"sonarr", "radarr"} {
		hook := fmt.Sprintf("%s/api/webhooks/%s?apikey=%s", st.AppURL("bazarr"), id, key)
		if err := ensureWebhook(c.Arr(id), "Bazarr", hook, []string{"onDownload", "onUpgrade"}); err != nil {
			c.J.Warn("%s -> Bazarr webhook: %v", platform.Get(id).Name, err)
		}
	}
	c.J.OK("Sonarr and Radarr tell Bazarr about every new file")
	return nil
}

// ---------------------------------------------------------------------------------------------- Unpackerr

func runUnpackerr(c *Ctx) error {
	if err := writeUnpackerrConf(c); err != nil {
		return err
	}
	return c.Deploy("unpackerr", "")
}

func writeUnpackerrConf(c *Ctx) error {
	st := c.St
	in := func(p string) string { return c.InApp("unpackerr", p) }
	complete := filepath.Join(st.DownloadsDir(), "complete")
	logFile := in(filepath.Join(st.ConfigDir("unpackerr"), "unpackerr.log"))
	var folders strings.Builder
	for _, cat := range []string{"movies", "tv", "anime", "other"} {
		fmt.Fprintf(&folders, "\n[[folder]]\n  path = '%s'\n  move_back = true\n  delete_after = \"0\"\n", in(filepath.Join(complete, cat)))
	}
	conf := fmt.Sprintf(`# Unpackerr: extracts .rar/.zip releases so they can be imported. Written by Media Stack Setup.
log_file = '%s'
log_files = 5
log_file_mb = 10
interval = "2m"
start_delay = "1m"
retry_delay = "5m"
parallel = 1

[[sonarr]]
  url = "%s"
  api_key = "%s"
  paths = ['%s']

[[radarr]]
  url = "%s"
  api_key = "%s"
  paths = ['%s']
%s`, logFile, st.AppURL("sonarr"), st.Key("sonarr"), in(filepath.Join(complete, "sonarr")),
		st.AppURL("radarr"), st.Key("radarr"), in(filepath.Join(complete, "radarr")), folders.String())
	if st.IsDeployed("lidarr") {
		conf += fmt.Sprintf("\n[[lidarr]]\n  url = \"%s\"\n  api_key = \"%s\"\n  paths = ['%s']\n",
			st.AppURL("lidarr"), st.Key("lidarr"), in(filepath.Join(complete, "lidarr")))
	}
	return core.WriteFileAtomic(filepath.Join(st.ConfigDir("unpackerr"), "unpackerr.conf"), []byte(conf), 0o600)
}

// ---------------------------------------------------------------------------------------------- Lidarr

func runLidarr(c *Ctx) error {
	st := c.St
	if err := c.Deploy("lidarr", "/ping"); err != nil {
		return err
	}
	api := c.Arr("lidarr")
	music := filepath.Join(st.DataDir(), "media", "Music")
	if err := os.MkdirAll(music, 0o755); err != nil {
		return err
	}
	platform.Chown(st, music)

	// Quality: take whatever is there, then upgrade up to lossless (the stack's rule).
	var profiles, metas []core.M
	if err := api.Get("/qualityprofile", &profiles); err != nil {
		return err
	}
	api.Get("/metadataprofile", &metas)
	var anyProfile core.M
	for _, p := range profiles {
		if p["name"] == "Any" {
			anyProfile = p
		}
	}
	if anyProfile == nil && len(profiles) > 0 {
		anyProfile = profiles[0]
	}
	if anyProfile != nil {
		items, _ := anyProfile["items"].([]any)
		for _, it := range items {
			if m, ok := it.(map[string]any); ok {
				m["allowed"] = true
				if strings.EqualFold(fmt.Sprint(m["name"]), "lossless") && m["id"] != nil {
					anyProfile["cutoff"] = m["id"]
				}
			}
		}
		anyProfile["upgradeAllowed"] = true
		api.Put(fmt.Sprintf("/qualityprofile/%v", anyProfile["id"]), anyProfile, nil)
	}
	var roots []struct{ Path string }
	api.Get("/rootfolder", &roots)
	have := false
	for _, r := range roots {
		have = have || strings.EqualFold(strings.TrimRight(r.Path, `\/`), strings.TrimRight(c.InApp("lidarr", music), `\/`))
	}
	if !have && anyProfile != nil && len(metas) > 0 {
		if err := api.Post("/rootfolder", core.M{"name": "Music", "path": c.InApp("lidarr", music),
			"defaultQualityProfileId": anyProfile["id"], "defaultMetadataProfileId": metas[0]["id"],
			"defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all", "defaultTags": []int{}}, nil); err != nil {
			return fmt.Errorf("adding the music folder: %w", err)
		}
	}
	update(api, "/config/mediamanagement", core.M{"copyUsingHardlinks": true, "recycleBin": c.InApp("lidarr", st.RecycleDir()),
		"recycleBinCleanupDays": 14})
	c.J.OK("Lidarr's music folder: %s (takes what's available first, then upgrades to lossless)", music)

	if err := ensureQbtClient(c, "lidarr"); err != nil {
		return fmt.Errorf("connecting Lidarr to qBittorrent: %w", err)
	}
	c.J.OK("Lidarr sends downloads to qBittorrent (category \"lidarr\")")
	if err := ensureProwlarrApp(c, "lidarr"); err != nil {
		return fmt.Errorf("connecting Prowlarr to Lidarr: %w", err)
	}
	c.J.OK("Prowlarr shares its indexers that have music with Lidarr")
	if st.IsDeployed("jellyfin") && st.Key("jellyfin") != "" {
		jf := core.NewAPI(st.URL("jellyfin"), map[string]string{"Authorization": fmt.Sprintf(`MediaBrowser Token="%s"`, st.Key("jellyfin"))})
		var folders []struct{ Name string }
		if jf.Get("/Library/VirtualFolders", &folders) == nil {
			found := false
			for _, f := range folders {
				found = found || f.Name == "Music"
			}
			if !found {
				q := url.Values{"name": {"Music"}, "collectionType": {"music"}, "refreshLibrary": {"true"}}
				jf.Post("/Library/VirtualFolders?"+q.Encode(), core.M{"LibraryOptions": core.M{
					"EnableRealtimeMonitor": true, "PathInfos": []core.M{{"Path": c.InApp("jellyfin", music)}}}}, nil)
				c.J.OK("Jellyfin has a Music library")
			}
		}
		if err := ensureJellyfinConnect(c, "lidarr"); err == nil {
			c.J.OK("Lidarr tells Jellyfin about every new album")
		}
	}
	if st.IsDeployed("hub") {
		ensureWebhook(api, "Media Hub", st.AppURL("hub")+"/api/hook/lidarr", []string{"onGrab", "onReleaseImport",
			"onUpgrade", "onHealthIssue", "includeHealthWarnings", "onHealthRestored", "onDownloadFailure", "onImportFailure"})
	}
	if st.IsDeployed("unpackerr") && writeUnpackerrConf(c) == nil {
		c.P.Restart(st, "unpackerr")
	}
	return arrLogin(c, "lidarr")
}

// ---------------------------------------------------------------------------------------------- Cleanuparr

// Cleanuparr's permissive malware list minus archives (Unpackerr extracts those; scene releases ship as .rar).
const cleanuparrList = "https://raw.githubusercontent.com/Cleanuparr/Cleanuparr/refs/heads/main/blacklist_permissive"

var archivePattern = regexp.MustCompile(`(?i)^\*\.(rar|r\d\d|\d{3}|7z(\.\d{3})?|zip|z\d\d)$`)

func runCleanuparr(c *Ctx) error {
	st := c.St
	if err := c.Deploy("cleanuparr", "/"); err != nil {
		return err
	}
	cl := core.NewAPI(st.URL("cleanuparr"), nil)
	var status struct{ SetupCompleted bool }
	if err := cl.Get("/api/auth/status", &status); err != nil {
		return err
	}
	creds := core.M{"username": st.AdminUser, "password": c.Pass()}
	if !status.SetupCompleted {
		var made struct{ Tokens struct{ AccessToken string } }
		if code, err := cl.DoStatus("POST", "/api/auth/setup/account", creds, &made); err != nil && !strings.Contains(strings.ToLower(err.Error()), "exist") {
			return fmt.Errorf("creating the Cleanuparr login (HTTP %d): %w", code, err)
		}
		if made.Tokens.AccessToken != "" {
			cl.Headers["Authorization"] = "Bearer " + made.Tokens.AccessToken
		}
		cl.Post("/api/auth/setup/complete", core.M{}, nil)
	}
	var login struct{ Tokens struct{ AccessToken string } }
	if err := cl.Post("/api/auth/login", creds, &login); err != nil {
		return core.UserAction("Cleanuparr didn't accept the admin login (" + err.Error() + ")")
	}
	cl.Headers["Authorization"] = "Bearer " + login.Tokens.AccessToken
	var key struct {
		APIKey string `json:"apiKey"`
	}
	if err := cl.Get("/api/account/api-key", &key); err == nil && key.APIKey != "" {
		st.SetKey("cleanuparr", key.APIKey)
	}

	for _, a := range []struct {
		kind, name string
		version    int
	}{{"sonarr", "Sonarr", 4}, {"radarr", "Radarr", 6}} {
		var conf struct{ Instances []any }
		cl.Get("/api/configuration/"+a.kind, &conf)
		if len(conf.Instances) == 0 {
			if err := cl.Post("/api/configuration/"+a.kind+"/instances", core.M{"name": a.name, "url": st.AppURL(a.kind),
				"apiKey": st.Key(a.kind), "version": a.version, "enabled": true}, nil); err != nil {
				return fmt.Errorf("connecting Cleanuparr to %s: %w", a.name, err)
			}
		}
	}
	var dc struct{ Clients []any }
	cl.Get("/api/configuration/download_client", &dc)
	if len(dc.Clients) == 0 {
		user, pass := "", ""
		if st.Docker() {
			user, pass = st.AdminUser, c.Pass()
		}
		if err := cl.Post("/api/configuration/download_client", core.M{"enabled": true, "name": "qBittorrent",
			"typeName": "qBittorrent", "host": st.AppURL("qbittorrent"), "username": user, "password": pass,
			"apiKey": "", "urlBase": "", "externalUrl": ""}, nil); err != nil {
			return fmt.Errorf("connecting Cleanuparr to qBittorrent: %w", err)
		}
	}
	var qc core.M
	if err := cl.Get("/api/configuration/queue_cleaner", &qc); err == nil {
		qc["enabled"] = true
		// Torrents that never get past "downloading metadata" (dead/unreachable swarms) aren't "stalled" to
		// Cleanuparr; give up on them after ~30 minutes (6 checks, 5 minutes apart) so another release is tried.
		qc["downloadingMetadataMaxStrikes"] = 6
		cl.Put("/api/configuration/queue_cleaner", qc, nil)
	}

	text, err := core.GetText(c, cleanuparrList)
	if err != nil {
		return fmt.Errorf("downloading Cleanuparr's malware list: %w", err)
	}
	var kept []string
	for _, p := range strings.Fields(text) {
		if !archivePattern.MatchString(p) {
			kept = append(kept, p)
		}
	}
	if len(kept) < 100 {
		return fmt.Errorf("Cleanuparr's malware list looks truncated")
	}
	list := filepath.Join(st.ConfigDir("cleanuparr"), "blocklist.txt")
	if err := core.WriteFileAtomic(list, []byte(strings.Join(kept, "\n")+"\n"), 0o644); err != nil {
		return err
	}
	var mb core.M
	if err := cl.Get("/api/configuration/malware_blocker", &mb); err == nil {
		mb["enabled"] = true
		for _, k := range []string{"sonarr", "radarr"} {
			mb[k] = core.M{"enabled": true, "blocklistType": "Blacklist", "blocklistPath": c.InApp("cleanuparr", list)}
		}
		if err := cl.Put("/api/configuration/malware_blocker", mb, nil); err != nil {
			c.J.Warn("Malware blocker: %v", err)
		}
	}
	var rules []any
	cl.Get("/api/queue-rules/stall", &rules)
	if len(rules) == 0 {
		cl.Post("/api/queue-rules/stall", core.M{"name": "Stalled downloads", "enabled": true, "maxStrikes": 3,
			"privacyType": "Both", "minCompletionPercentage": 0, "maxCompletionPercentage": 100, "resetOnProgress": false,
			"deletePrivate": false, "changeCategory": false}, nil)
	}
	c.J.OK("Cleanuparr removes stalled and fake downloads and asks Sonarr/Radarr for another release")
	return nil
}
