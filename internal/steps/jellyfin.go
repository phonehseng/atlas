package steps

import (
	"fmt"
	"net/url"
	"runtime"
	"strings"
	"time"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "jellyfin", Title: "Jellyfin", Group: "Watch", Button: "Install Jellyfin",
		Needs: []string{"account"}, Password: true, Run: runJellyfin,
		Fields: func(c *Ctx) []Field {
			return []Field{{Name: "introskipper", Type: "checkbox", Default: fmt.Sprint(Chosen(c.St, "introskipper")),
				Label: "Add a \"Skip Intro\" button (the Intro Skipper plugin)",
				Help:  "Finds the intro and credits of every episode and offers to skip them, like Netflix."}}
		},
		Links: func(st *core.Stack) []Link { return []Link{linkTo(st, "jellyfin", "Open Jellyfin")} }})
}

const jfClient = `MediaBrowser Client="Media Stack Setup", Device="Setup", DeviceId="mediastack-setup", Version="1.0"`

// JellyfinLogin signs in to Jellyfin and returns an API client using that session.
func JellyfinLogin(st *core.Stack, user, pass string) (*core.API, string, error) {
	jf := core.NewAPI(st.URL("jellyfin"), map[string]string{"Authorization": jfClient})
	var auth struct {
		AccessToken string
		User        struct {
			ID     string `json:"Id"`
			Policy struct{ IsAdministrator bool }
		}
	}
	if err := jf.Post("/Users/AuthenticateByName", core.M{"Username": user, "Pw": pass}, &auth); err != nil {
		return nil, "", err
	}
	jf.Headers["Authorization"] = fmt.Sprintf(`%s, Token="%s"`, jfClient, auth.AccessToken)
	if !auth.User.Policy.IsAdministrator {
		return nil, "", fmt.Errorf("%s isn't a Jellyfin administrator", user)
	}
	return jf, auth.User.ID, nil
}

func runJellyfin(c *Ctx) error {
	st := c.St
	if err := c.Deploy("jellyfin", "/System/Info/Public"); err != nil {
		return err
	}
	jf := core.NewAPI(st.URL("jellyfin"), map[string]string{"Authorization": jfClient})
	var info struct {
		StartupWizardCompleted bool
		Version                string
	}
	// Jellyfin answers "503 starting up" for a while after its port opens (longer on first start).
	if err := core.Retry(c, 4*time.Minute, func() error { return jf.Get("/System/Info/Public", &info) }); err != nil {
		return err
	}
	c.J.Info("Jellyfin %s", info.Version)
	if !info.StartupWizardCompleted {
		c.J.Info("Running Jellyfin's first-time setup with your admin login...")
		cfg := core.M{"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}
		if err := core.Retry(c, 4*time.Minute, func() error { return jf.Post("/Startup/Configuration", cfg, nil) }); err != nil {
			return err
		}
		jf.Get("/Startup/User", nil) // creates the first user record
		if err := jf.Post("/Startup/User", core.M{"Name": st.AdminUser, "Password": c.Pass()}, nil); err != nil {
			return err
		}
		jf.Post("/Startup/RemoteAccess", core.M{"EnableRemoteAccess": true, "EnableAutomaticPortMapping": false}, nil)
		if err := jf.Post("/Startup/Complete", nil, nil); err != nil {
			return err
		}
	}
	s, uid, err := JellyfinLogin(st, st.AdminUser, c.Pass())
	if err != nil {
		return core.UserAction("Jellyfin didn't accept your admin login (" + err.Error() + "). If you set Jellyfin up before, use that username and password in the \"Your admin login\" step")
	}
	defer s.Post("/Sessions/Logout", nil, nil)
	c.J.OK("Signed in to Jellyfin as %s", st.AdminUser)

	// Libraries. Anime series and films share one "Anime" library (two folders, one shelf).
	var folders []struct{ Name string }
	if err := s.Get("/Library/VirtualFolders", &folders); err != nil {
		return err
	}
	have := map[string]bool{}
	for _, f := range folders {
		have[f.Name] = true
	}
	libs := []struct {
		name, kind string
		keys       []string
	}{
		{"Movies", "movies", []string{"movies"}},
		{"TV Shows", "tvshows", []string{"tv"}},
		{"Anime", "mixed", []string{"anime", "anime_movies"}},
	}
	for _, l := range libs {
		if have[l.name] {
			c.J.Info("Library %q already exists", l.name)
			continue
		}
		if err := addLibrary(c, s, l.name, l.kind, l.keys); err != nil {
			if l.kind != "mixed" {
				return err
			}
			// Older/newer Jellyfin without mixed libraries: two separate ones instead.
			c.J.Warn("Combined anime library not supported (%v) - making separate Anime and Anime Movies libraries", err)
			if err := addLibrary(c, s, "Anime", "tvshows", []string{"anime"}); err != nil {
				return err
			}
			if err := addLibrary(c, s, "Anime Movies", "movies", []string{"anime_movies"}); err != nil {
				return err
			}
		}
	}

	// Playback: Japanese audio when there is some (anime), English subtitles whenever the audio isn't English.
	var user struct{ Configuration core.M }
	if err := s.Get("/Users/"+uid, &user); err == nil && user.Configuration != nil {
		for k, v := range map[string]any{"AudioLanguagePreference": "jpn", "SubtitleLanguagePreference": "eng",
			"SubtitleMode": "Smart", "PlayDefaultAudioTrack": false} {
			user.Configuration[k] = v
		}
		if err := s.Post("/Users/Configuration?userId="+uid, user.Configuration, nil); err == nil {
			c.J.OK("Playback: Japanese audio when available, English subtitles when the audio isn't English")
		}
	}

	// Hardware transcoding (phones/TVs that can't play a file directly).
	if runtime.GOOS == "windows" && platform.HasNvidia() {
		var enc core.M
		if err := s.Get("/System/Configuration/encoding", &enc); err == nil {
			for k, v := range map[string]any{"HardwareAccelerationType": "nvenc", "EnableHardwareEncoding": true,
				"HardwareDecodingCodecs":         []string{"h264", "hevc", "mpeg2video", "mpeg4", "vc1", "vp8", "vp9", "av1"},
				"EnableDecodingColorDepth10Hevc": true, "EnableDecodingColorDepth10Vp9": true, "EnableTonemapping": true} {
				enc[k] = v
			}
			if err := s.Post("/System/Configuration/encoding", enc, nil); err == nil {
				c.J.OK("NVIDIA hardware transcoding turned on")
			}
		}
	}

	// An API key for Sonarr, Radarr, MediaSorter, Seerr and the Media Hub.
	key, err := jellyfinKey(s)
	if err != nil {
		return err
	}
	if err := st.SetKey("jellyfin", key); err != nil {
		return err
	}
	if c.In["introskipper"] != "false" {
		if err := introSkipper(c, s); err != nil {
			c.J.Warn("Intro Skipper wasn't added (%v) - Jellyfin works without it; run this step again to retry", err)
		}
	}
	s.Post("/Library/Refresh", nil, nil)
	c.J.OK("Jellyfin is ready")
	return nil
}

const introSkipperRepo = "https://intro-skipper.org/manifest.json"

// introSkipper adds the Intro Skipper plugin (a "Skip Intro" button) from its own plugin repository, then
// restarts Jellyfin to load it. The repository serves the build that matches the Jellyfin asking.
func introSkipper(c *Ctx, s *core.API) error {
	var plugins []struct{ Name string }
	if err := s.Get("/Plugins", &plugins); err != nil {
		return err
	}
	for _, p := range plugins {
		if p.Name == "Intro Skipper" {
			c.J.Info("Intro Skipper is already installed")
			return nil
		}
	}
	var repos []struct {
		Name    string
		URL     string `json:"Url"`
		Enabled bool
	}
	if err := s.Get("/Repositories", &repos); err != nil {
		return err
	}
	list := []core.M{}
	have := false
	for _, r := range repos {
		have = have || strings.EqualFold(r.URL, introSkipperRepo)
		list = append(list, core.M{"Name": r.Name, "Url": r.URL, "Enabled": r.Enabled})
	}
	if !have {
		list = append(list, core.M{"Name": "Intro Skipper", "Url": introSkipperRepo, "Enabled": true})
		if err := s.Post("/Repositories", list, nil); err != nil {
			return err
		}
	}
	var pkgs []struct {
		Name     string
		GUID     string `json:"guid"`
		Versions []struct {
			Version       string
			RepositoryURL string `json:"repositoryUrl"`
		}
	}
	if err := s.Get("/Packages", &pkgs); err != nil {
		return err
	}
	for _, p := range pkgs {
		if p.Name != "Intro Skipper" || len(p.Versions) == 0 {
			continue
		}
		v := p.Versions[0]
		q := url.Values{"assemblyGuid": {p.GUID}, "version": {v.Version}, "repositoryUrl": {v.RepositoryURL}}
		if err := s.Post("/Packages/Installed/"+url.PathEscape(p.Name)+"?"+q.Encode(), nil, nil); err != nil {
			return err
		}
		c.J.Info("Restarting Jellyfin to load Intro Skipper %s...", v.Version)
		time.Sleep(10 * time.Second) // the download finishes in the background
		s.Post("/System/Restart", nil, nil)
		time.Sleep(10 * time.Second)
		pub := core.NewAPI(c.St.URL("jellyfin"), nil)
		if err := core.Retry(c, 4*time.Minute, func() error { return pub.Get("/System/Info/Public", nil) }); err != nil {
			return err
		}
		c.J.OK("Intro Skipper added: episodes get a \"Skip Intro\" button once it has scanned them (a scheduled task)")
		return nil
	}
	return fmt.Errorf("the Intro Skipper repository has no build for this Jellyfin version yet")
}

func addLibrary(c *Ctx, s *core.API, name, kind string, keys []string) error {
	var paths []core.M
	for _, k := range keys {
		paths = append(paths, core.M{"Path": c.InApp("jellyfin", c.St.MediaDir(k))})
	}
	opts := core.M{
		"EnableRealtimeMonitor": true, "PreferredMetadataLanguage": "en", "MetadataCountryCode": "US",
		"SaveSubtitlesWithMedia": true, "AllowEmbeddedSubtitles": "AllowAll", "SeasonZeroDisplayName": "Specials",
		"PathInfos": paths,
	}
	q := url.Values{"name": {name}, "collectionType": {kind}, "refreshLibrary": {"false"}}
	if err := s.Post("/Library/VirtualFolders?"+q.Encode(), core.M{"LibraryOptions": opts}, nil); err != nil {
		return err
	}
	c.J.OK("Created library %q", name)
	return nil
}

func jellyfinKey(s *core.API) (string, error) {
	find := func() string {
		var keys struct {
			Items []struct{ AppName, AccessToken string }
		}
		s.Get("/Auth/Keys", &keys)
		for _, k := range keys.Items {
			if k.AppName == "Media Stack" {
				return k.AccessToken
			}
		}
		return ""
	}
	if k := find(); k != "" {
		return k, nil
	}
	if err := s.Post("/Auth/Keys?app="+url.QueryEscape("Media Stack"), nil, nil); err != nil {
		return "", err
	}
	if k := find(); k != "" {
		return k, nil
	}
	return "", fmt.Errorf("Jellyfin didn't create an API key")
}
