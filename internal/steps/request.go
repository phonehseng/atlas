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
	step(&Step{ID: "seerr", Title: "Seerr (requests)", Group: "Request & share", Button: "Install Seerr",
		Needs: []string{"jellyfin", "sonarr", "radarr"}, Password: true, Run: runSeerr, Links: links("seerr")})
	step(&Step{ID: "jfago", Title: "jfa-go (invite friends)", Group: "Request & share", Optional: true, Button: "Install jfa-go",
		Needs: []string{"jellyfin"}, Password: true, Run: runJfago, Links: links("jfago")})
	step(&Step{ID: "requestrr", Title: "Requestrr (Discord requests)", Group: "Request & share", Optional: true,
		Button: "Install Requestrr", Needs: []string{"seerr"}, Run: func(c *Ctx) error { return c.Deploy("requestrr", "/") },
		Links: links("requestrr")})
	step(&Step{ID: "autobrr", Title: "autobrr (IRC auto-grabs)", Group: "Request & share", Optional: true, Button: "Install autobrr",
		Needs: []string{"qbittorrent", "sonarr", "radarr"}, Password: true, Run: runAutobrr, Links: links("autobrr")})
}

// ---------------------------------------------------------------------------------------------- Seerr

func runSeerr(c *Ctx) error {
	st := c.St
	if !st.Docker() {
		c.J.Info("Seerr is built from its source code on Windows - this step takes 10-15 minutes.")
	}
	if err := c.Deploy("seerr", "/api/v1/status"); err != nil {
		return err
	}
	s := core.NewAPI(st.URL("seerr")+"/api/v1", nil)
	var public struct{ Initialized bool }
	if err := s.Get("/settings/public", &public); err != nil {
		return err
	}
	body := core.M{"username": st.AdminUser, "password": c.Pass(), "email": ""}
	if !public.Initialized {
		body["hostname"], body["port"], body["urlBase"], body["useSsl"], body["serverType"] =
			st.AppHost("jellyfin"), st.AppPort("jellyfin"), "", false, 2
	}
	if err := s.Post("/auth/jellyfin", body, nil); err != nil {
		return fmt.Errorf("Seerr couldn't sign in to Jellyfin with the admin login: %w", err)
	}
	c.J.OK("Seerr signed in to Jellyfin as %s", st.AdminUser)

	s.Post("/settings/jellyfin/library/sync", nil, nil)
	var libs []struct {
		ID string `json:"id"`
	}
	s.Get("/settings/jellyfin/library", &libs)
	for _, l := range libs {
		s.Put("/settings/jellyfin/library/"+l.ID, core.M{"enabled": true}, nil)
	}

	type profile struct {
		ID   int    `json:"id"`
		Name string `json:"name"`
	}
	pick := func(id string, wants ...string) (profile, error) {
		var ps []profile
		if err := c.Arr(id).Get("/qualityprofile", &ps); err != nil {
			return profile{}, err
		}
		for _, want := range wants {
			for _, p := range ps {
				if p.Name == want {
					return p, nil
				}
			}
		}
		if len(ps) == 0 {
			return profile{}, fmt.Errorf("%s has no quality profiles", id)
		}
		why := "was the quality-profile step skipped?"
		if st.HasParts() && !st.Picked("recyclarr") {
			why = "Recyclarr isn't part of this stack"
		}
		c.J.Warn("%s has no %q profile (%s) - using %q", platform.Get(id).Name, wants[0], why, ps[0].Name)
		return ps[0], nil
	}
	// Films go up to 4K; shows stop at 1080p (the "1080p" profile; older installs named it "WEB-1080p").
	tv, err := pick("sonarr", "1080p", "WEB-1080p", "Remux 2160p (Combined)")
	if err != nil {
		return err
	}
	animeTV, _ := pick("sonarr", "[Anime] Remux-1080p")
	mv, err := pick("radarr", "Remux 2160p (Combined)")
	if err != nil {
		return err
	}
	animeMV, _ := pick("radarr", "[Anime] Remux-1080p")

	common := core.M{"useSsl": false, "baseUrl": "", "tags": []int{}, "is4k": false, "isDefault": true, "externalUrl": "",
		"syncEnabled": true, "preventSearch": false, "tagRequests": false}
	sonarr := core.M{"name": "Sonarr", "hostname": st.AppHost("sonarr"), "port": st.AppPort("sonarr"), "apiKey": st.Key("sonarr"),
		"activeProfileId": tv.ID, "activeProfileName": tv.Name, "activeDirectory": c.InApp("sonarr", st.MediaDir("tv")),
		"seriesType": "standard", "animeSeriesType": "anime", "activeAnimeProfileId": animeTV.ID,
		"activeAnimeProfileName": animeTV.Name, "activeAnimeDirectory": c.InApp("sonarr", st.MediaDir("anime")),
		"animeTags": []int{}, "enableSeasonFolders": true, "monitorNewItems": "all"}
	radarr := core.M{"name": "Radarr", "hostname": st.AppHost("radarr"), "port": st.AppPort("radarr"), "apiKey": st.Key("radarr"),
		"activeProfileId": mv.ID, "activeProfileName": mv.Name, "activeDirectory": c.InApp("radarr", st.MediaDir("movies")),
		"minimumAvailability": "released"}
	for kind, body := range map[string]core.M{"sonarr": sonarr, "radarr": radarr} {
		for k, v := range common {
			body[k] = v
		}
		var existing []struct {
			ID int `json:"id"`
		}
		s.Get("/settings/"+kind, &existing)
		if len(existing) > 0 {
			err = s.Put(fmt.Sprintf("/settings/%s/%d", kind, existing[0].ID), body, nil)
		} else {
			err = s.Post("/settings/"+kind, body, nil)
		}
		if err != nil {
			return fmt.Errorf("connecting Seerr to %s: %w", kind, err)
		}
	}
	c.J.OK("Seerr sends shows to Sonarr (%q, anime %q) and films to Radarr (%q)", tv.Name, animeTV.Name, mv.Name)

	// Japanese animated films -> Anime Movies folder with the anime profile (TMDB genre 16 = Animation).
	var rules []struct {
		RootFolder string `json:"rootFolder"`
	}
	s.Get("/overrideRule", &rules)
	animeFolder := c.InApp("radarr", st.MediaDir("anime_movies"))
	found := false
	for _, r := range rules {
		found = found || r.RootFolder == animeFolder
	}
	if !found {
		var rs []struct {
			ID int `json:"id"`
		}
		s.Get("/settings/radarr", &rs)
		if len(rs) > 0 {
			if err := s.Post("/overrideRule", core.M{"radarrServiceId": rs[0].ID, "genre": "16", "language": "ja",
				"profileId": animeMV.ID, "rootFolder": animeFolder}, nil); err != nil {
				c.J.Warn("Anime-film rule: %v", err)
			}
		}
	}
	// Everyone who signs in with their Jellyfin account can request, and requests are approved at once
	// (Seerr permissions: REQUEST | AUTO_APPROVE | issue reporting).
	if err := s.Post("/settings/main", core.M{"partialRequestsEnabled": true, "localLogin": true,
		"defaultPermissions": 32 | 128 | 2097152 | 4194304}, nil); err != nil {
		return err
	}
	s.Post("/settings/initialize", nil, nil)
	var main struct {
		APIKey string `json:"apiKey"`
	}
	if err := s.Get("/settings/main", &main); err == nil && main.APIKey != "" {
		st.SetKey("seerr", main.APIKey)
	}
	if st.IsDeployed("hub") {
		hubHooks(c)
	}
	c.J.OK("Requests are approved automatically for everyone with a Jellyfin account")
	return nil
}

// ---------------------------------------------------------------------------------------------- jfa-go

func runJfago(c *Ctx) error {
	st := c.St
	file := filepath.Join(st.ConfigDir("jfago"), "config.ini")
	if _, err := os.Stat(file); os.IsNotExist(err) {
		// Pre-fill jfa-go's settings so its own setup wizard isn't needed. It signs in to Jellyfin as the admin
		// to create accounts for the people you invite (jfa-go stores that login in this file).
		public := remoteURL(st, "jellyfin")
		conf := fmt.Sprintf(`first_run = false

[jellyfin]
username = %s
password = %s
server = %s
public_server = %s
client = jfa-go
type = jellyfin
cache_timeout = 30

[ui]
host = 0.0.0.0
port = %d
jellyfin_login = true
admin_only = true
allow_all = false
theme = Jellyfin (Dark)
language-form = en-us
language-admin = en-us
jfa_url = %s
url_base =

[updates]
enabled = false

[password_validation]
enabled = true
min_length = 8

[email]
method =

[invite_emails]
enabled = false

[password_resets]
enabled = false
`, st.AdminUser, c.Pass(), st.AppURL("jellyfin"), public, st.AppPort("jfago"), remoteURL(st, "jfago"))
		if err := core.WriteFileAtomic(file, []byte(conf), 0o600); err != nil {
			return err
		}
	}
	return c.Deploy("jfago", "/")
}

// remoteURL is the address friends use over Tailscale (falls back to this computer's address).
func remoteURL(st *core.Stack, id string) string {
	if name := st.Opt("tailscale_name"); name != "" {
		return fmt.Sprintf("http://%s:%d", name, st.Port(id))
	}
	return OpenURL(st, id)
}

// ---------------------------------------------------------------------------------------------- autobrr

func runAutobrr(c *Ctx) error {
	st := c.St
	if err := c.Deploy("autobrr", "/api/healthz/liveness"); err != nil {
		return err
	}
	a := core.NewAPI(st.URL("autobrr"), nil)
	creds := core.M{"username": st.AdminUser, "password": c.Pass()}
	if code, _ := a.DoStatus("GET", "/api/auth/onboard", nil, nil); code == 200 || code == 204 {
		if err := a.Post("/api/auth/onboard", creds, nil); err != nil {
			return fmt.Errorf("creating the autobrr login: %w", err)
		}
	}
	if err := a.Post("/api/auth/login", core.M{"username": st.AdminUser, "password": c.Pass(), "remember_me": true}, nil); err != nil {
		return core.UserAction("autobrr didn't accept the admin login (" + err.Error() + ")")
	}
	var keys []struct{ Name, Key string }
	a.Get("/api/keys", &keys)
	key := ""
	for _, k := range keys {
		if k.Name == "Media Hub" {
			key = k.Key
		}
	}
	if key == "" {
		var made struct{ Key string }
		if err := a.Post("/api/keys", core.M{"name": "Media Hub", "scopes": []string{}}, &made); err != nil {
			return err
		}
		key = made.Key
		if key == "" {
			a.Get("/api/keys", &keys)
			for _, k := range keys {
				if k.Name == "Media Hub" {
					key = k.Key
				}
			}
		}
	}
	st.SetKey("autobrr", key)
	var have []struct{ Name string }
	a.Get("/api/downloaders", &have)
	names := map[string]bool{}
	for _, d := range have {
		names[d.Name] = true
	}
	user, pass := "", ""
	if st.Docker() {
		user, pass = st.AdminUser, c.Pass()
	}
	base := core.M{"enabled": true, "port": 0, "tls": false, "tls_skip_verify": false}
	for _, d := range []core.M{
		{"name": "qBittorrent", "type": "QBITTORRENT", "host": st.AppURL("qbittorrent"), "username": user, "password": pass, "settings": core.M{}},
		{"name": "Sonarr", "type": "SONARR", "host": st.AppURL("sonarr"), "username": "", "password": "", "settings": core.M{"apikey": st.Key("sonarr")}},
		{"name": "Radarr", "type": "RADARR", "host": st.AppURL("radarr"), "username": "", "password": "", "settings": core.M{"apikey": st.Key("radarr")}},
	} {
		if names[d["name"].(string)] {
			continue
		}
		for k, v := range base {
			d[k] = v
		}
		if err := a.Post("/api/downloaders", d, nil); err != nil {
			c.J.Warn("autobrr -> %s: %v", d["name"], err)
		}
	}
	c.J.OK("autobrr can send grabs to qBittorrent, Sonarr and Radarr")
	return nil
}

// jsonString is used by tests and the hub config.
func jsonString(v any) string {
	b, _ := json.Marshal(v)
	return strings.TrimSpace(string(b))
}
