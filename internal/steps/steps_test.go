package steps

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"testing"

	"mediastack/internal/core"
)

// Every step's help text must render for both platforms, before and after setup choices.
func TestGuidesRender(t *testing.T) {
	for _, goos := range []string{"windows", "linux"} {
		st := core.New(t.TempDir())
		st.OS = goos
		for _, s := range All {
			for _, stack := range []*core.Stack{nil, st} {
				intro, after := RenderGuide(s.ID, stack, `D:\MediaServer`)
				if strings.Contains(intro+after, "help text error") {
					t.Errorf("%s/%s: %s %s", goos, s.ID, intro, after)
				}
			}
			if _, ok := guides[s.ID]; !ok {
				t.Errorf("step %s has no guide", s.ID)
			}
		}
		st.VPN = core.VPN{Mode: "app", Adapter: "NordLynx"}
		st.Opts["tailscale_name"] = "server"
		_, after := RenderGuide("remote", st, "")
		if goos == "windows" && !strings.Contains(after, "tailscaled.exe") {
			t.Errorf("NordVPN users must be told about tailscaled.exe:\n%s", after)
		}
		if !strings.Contains(after, "http://server:8096") {
			t.Errorf("remote address missing:\n%s", after)
		}
	}
}

func TestStepGraph(t *testing.T) {
	seen := map[string]bool{}
	for _, s := range For("windows") {
		for _, n := range s.Needs {
			if !seen[n] {
				t.Errorf("%s needs %s, which comes later (or doesn't exist)", s.ID, n)
			}
		}
		seen[s.ID] = true
		if s.Run == nil {
			t.Errorf("%s has no Run", s.ID)
		}
	}
}

func TestInAppPaths(t *testing.T) {
	st := core.New("/srv/stack")
	st.OS = "linux"
	if got := st.InApp("sonarr", st.MediaDir("tv")); got != "/data/media/TV Shows" {
		t.Errorf("docker path: %s", got)
	}
	if got := st.InApp("cleanuparr", st.ConfigDir("cleanuparr")+"/blocklist.txt"); got != "/config/blocklist.txt" {
		t.Errorf("config path: %s", got)
	}
	w := core.New(`D:\Stack`)
	w.OS = "windows"
	if got := w.InApp("sonarr", w.MediaDir("tv")); got != w.MediaDir("tv") {
		t.Errorf("windows path changed: %s", got)
	}
}

func TestAppConfig(t *testing.T) {
	st := core.New(t.TempDir())
	st.OS = "linux"
	st.VPN = core.VPN{Mode: "gluetun"}
	st.AdminUser, st.AdminPass = "admin", "secret-pass"
	for _, id := range []string{"jellyfin", "qbittorrent", "sonarr", "radarr", "trawl", "hub", "mediasorter", "gluetun"} {
		st.Deployed[id] = "x"
		st.Keys[id] = "key-" + id
	}
	if err := WriteAppConfig(st); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(appConfigFile(st))
	var c struct {
		Platform string
		Library  map[string]string
		Apps     map[string]struct {
			URL, Key, Log, Service string
			Port                   int
			Remote                 bool
			Local                  bool `json:"local_only"`
		}
		Hub   map[string]any
		Login map[string]string `json:"qbittorrent_login"`
	}
	if err := json.Unmarshal(b, &c); err != nil {
		t.Fatal(err)
	}
	if c.Platform != "docker" || c.Library["tv"] != "/data/media/TV Shows" {
		t.Errorf("platform/library: %+v", c)
	}
	if c.Apps["qbittorrent"].URL != "http://gluetun:8080" || c.Apps["qbittorrent"].Service != "gluetun" {
		t.Errorf("qBittorrent is reached through the VPN container: %+v", c.Apps["qbittorrent"])
	}
	if !c.Apps["trawl"].Local || !c.Apps["jellyfin"].Remote || c.Apps["sonarr"].Key != "key-sonarr" {
		t.Errorf("flags/keys: %+v", c.Apps)
	}
	if c.Login["password"] != "secret-pass" || c.Hub["access"] != "lan-login" {
		t.Errorf("docker login/access: %+v %+v", c.Login, c.Hub)
	}
	if _, ok := c.Apps["gluetun"]; !ok {
		t.Errorf("gluetun should be listed for the hub")
	}
}

func TestForeignSubsRule(t *testing.T) {
	foreign, english := regexp.MustCompile("(?i)"+foreignRx), regexp.MustCompile("(?i)"+englishRx)
	blocked := func(title string) bool { return foreign.MatchString(title) && !english.MatchString(title) }
	for title, want := range map[string]bool{
		"[AT] Initial D Fourth Stage (Quarto Estágio) [1080p) 01 ~ 24 (Legendado)": true,
		"[Erai-raws] Initial D First Stage - 01 [1080p][Multiple Subtitle]":        false,
		"[SubsPlease] Frieren - 01 (1080p) [ENG]":                                  false,
		"Frieren 01 1080p Sub ITA":                                                 true,
	} {
		if blocked(title) != want {
			t.Errorf("%s: blocked=%v, want %v", title, !want, want)
		}
	}
	inst := map[string]any{"quality_profiles": []any{map[string]any{"trash_id": "20e0fc959f1f1704bed501f23bdae76f"},
		map[string]any{"trash_id": "not-one-of-ours"}}}
	applyUpgradeRules("sonarr", inst)
	for _, p := range inst["quality_profiles"].([]any) {
		reset, _ := p.(map[string]any)["reset_unmatched_scores"].(map[string]any)
		if except, _ := reset["except"].([]any); len(except) != 1 || except[0] != foreignSubs {
			t.Errorf("the weekly sync must keep the %q score: %+v", foreignSubs, p)
		}
	}
}
