package steps

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"mediastack/internal/core"
)

// Every step that installs something belongs to a component, and every component's needs exist.
func TestComponents(t *testing.T) {
	always := map[string]bool{"welcome": true, "folders": true, "choose": true, "account": true, "finish": true, "update": true}
	for _, s := range All {
		if !always[s.ID] && componentOf(s.ID) == "" {
			t.Errorf("step %s isn't part of any component", s.ID)
		}
	}
	for _, c := range Components {
		for _, n := range c.Needs {
			if ComponentByID(n) == nil {
				t.Errorf("%s needs unknown %s", c.ID, n)
			}
		}
		for _, s := range c.Steps {
			if Find(s) == nil {
				t.Errorf("%s names unknown step %s", c.ID, s)
			}
		}
	}
	// A component's steps' Needs must all be chosen whenever it is (so no chosen step is ever blocked).
	for _, goos := range []string{"windows", "linux"} {
		for _, c := range ComponentsFor(goos) {
			parts, _ := CloseParts([]string{c.ID}, goos)
			set := map[string]bool{}
			for _, p := range parts {
				set[p] = true
			}
			for _, sid := range c.Steps {
				for _, need := range Find(sid).Needs {
					if comp := componentOf(need); comp != "" && !set[comp] {
						t.Errorf("%s: step %s needs %s, but choosing %s doesn't add %s", goos, sid, need, c.ID, comp)
					}
				}
			}
		}
	}
}

func TestPresets(t *testing.T) {
	min, _ := CloseParts(Preset("minimal", "windows"), "windows")
	want := "download,folders,jellyfin,prowlarr,radarr,seerr,sonarr"
	if got := strings.Join(min, ","); got != want {
		t.Errorf("minimal = %s, want %s", got, want)
	}
	rec, added := CloseParts(Preset("recommended", "windows"), "windows")
	if len(added) != 0 {
		t.Errorf("recommended isn't closed under needs: %v", added)
	}
	if !strings.Contains(","+strings.Join(rec, ",")+",", ",marquee,") {
		t.Errorf("recommended on Windows includes Marquee: %v", rec)
	}
	lin, _ := CloseParts(Preset("everything", "linux"), "linux")
	for _, p := range lin {
		if p == "marquee" {
			t.Error("Marquee offered on Linux")
		}
	}
	// Ticking Marquee pulls in Jellyfin and Seerr (and what Seerr needs).
	got, added := CloseParts([]string{"marquee"}, "windows")
	if !strings.Contains(strings.Join(got, ","), "seerr") || len(added) == 0 {
		t.Errorf("marquee closure: %v %v", got, added)
	}
}

func TestVisible(t *testing.T) {
	st := core.New(t.TempDir())
	st.OS = "windows"
	if !Visible(st, Find("lidarr")) {
		t.Error("before choosing, every step shows")
	}
	if Visible(st, Find("update")) {
		t.Error("update shows only once something is installed")
	}
	parts, _ := CloseParts(Preset("minimal", "windows"), "windows")
	st.SetParts(parts)
	for id, want := range map[string]bool{"jellyfin": true, "seerr": true, "qbittorrent": true, "vpn": true, "hub": false,
		"marquee": false, "lidarr": false, "recyclarr": false, "remote": true, "finish": true, "choose": true} {
		if Visible(st, Find(id)) != want {
			t.Errorf("minimal: %s visible = %v", id, !want)
		}
	}
	st.OS = "linux"
	if Visible(st, Find("remote")) {
		t.Error("linux minimal: no Tailscale step")
	}
}

func TestStackApp(t *testing.T) {
	st := core.New(t.TempDir())
	st.OS = "windows"
	if StackApp(st) != "hub" {
		t.Error("older installs keep the Media Hub")
	}
	st.SetParts([]string{"marquee", "hub"})
	if StackApp(st) != "marquee" {
		t.Error("marquee chosen")
	}
	st.OS = "linux"
	if StackApp(st) != "hub" {
		t.Error("linux is Media Hub only")
	}
}

func TestTailscaleCommands(t *testing.T) {
	s := TailscaleMSIScript(`D:\Stack Test\downloads-cache\tailscale-setup-1.104.2-amd64.msi`, `D:\Stack Test\logs\tailscale-install.log`)
	for _, want := range []string{"Start-Process -FilePath msiexec.exe",
		`'/i "D:\Stack Test\downloads-cache\tailscale-setup-1.104.2-amd64.msi" /qn /norestart /l*v "D:\Stack Test\logs\tailscale-install.log"'`,
		"-Wait -PassThru", "-ne 3010"} {
		if !strings.Contains(s, want) {
			t.Errorf("MSI script lacks %q:\n%s", want, s)
		}
	}
	if TailscaleMSIURL() != "https://pkgs.tailscale.com/stable/tailscale-setup-1.104.2-amd64.msi" {
		t.Error(TailscaleMSIURL())
	}
	st := core.New(`D:\StackTest`)
	st.OS, st.Name = "windows", "Media Stack Test"
	st.Deployed["marquee"], st.Deployed["jellyfin"] = "x", "x"
	fw := FirewallScript(st)
	want := `New-NetFirewallRule -DisplayName 'Marquee Mobile (Tailscale) (Media Stack Test)' -Direction Inbound -Protocol TCP -LocalPort 8737 -Program 'D:\StackTest\apps\Marquee\Marquee.exe' -Action Allow -Profile Private`
	if !strings.Contains(fw, want) {
		t.Errorf("firewall script:\n%s\nwant line:\n%s", fw, want)
	}
	st.Name = "Media Stack"
	if MarqueeRuleName(st) != "Marquee Mobile (Tailscale)" {
		t.Error(MarqueeRuleName(st))
	}
}

// The real download of the pinned MSI (size + SHA-256). Network: run with MEDIASTACK_NET_TESTS=1.
func TestTailscaleMSIDownload(t *testing.T) {
	if os.Getenv("MEDIASTACK_NET_TESTS") == "" {
		t.Skip("set MEDIASTACK_NET_TESTS=1")
	}
	j := core.NewJob("test", os.Stderr)
	file, err := DownloadTailscaleMSI(context.Background(), j, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("verified %s", file)
}

func TestMarqueeSettings(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("DPAPI")
	}
	root := t.TempDir()
	st := core.New(root)
	st.OS, st.PortOffset, st.Name, st.AdminUser = "windows", 20000, "Media Stack Test", "stacktest"
	for _, id := range []string{"jellyfin", "seerr", "qbittorrent", "hub", "radarr", "sonarr"} {
		st.Deployed[id] = "x"
	}
	st.Keys["radarr"], st.Keys["sonarr"] = "radarr-key-123", "sonarr-key-456"
	os.MkdirAll(filepath.Dir(appConfigFile(st)), 0o755)
	os.WriteFile(appConfigFile(st), []byte("{}"), 0o644)
	home := MarqueeHome(st)
	if home != filepath.Join(root, "config", "marquee") {
		t.Fatalf("a test install must not use %%APPDATA%%: %s", home)
	}
	// Existing user settings survive.
	os.MkdirAll(home, 0o755)
	os.WriteFile(filepath.Join(home, "settings.json"), []byte(`{"theme": "light", "quality": "1080"}`), 0o600)
	c := &Ctx{Context: context.Background(), St: st, J: core.NewJob("t", os.Stderr)}
	if err := WriteMarqueeSettings(c, home); err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	b, _ := os.ReadFile(filepath.Join(home, "settings.json"))
	json.Unmarshal(b, &m)
	for k, v := range map[string]string{"server": "http://localhost:28096", "seerr": "http://localhost:25055",
		"qbt_url": "http://127.0.0.1:28080", "hub_url": "http://127.0.0.1:28585", "radarr_url": "http://127.0.0.1:27878",
		"sonarr_url": "http://127.0.0.1:28989", "mobile_bind": "tailscale", "theme": "light", "user_name": "stacktest",
		"stack_config": appConfigFile(st)} {
		if m[k] != v {
			t.Errorf("%s = %v, want %s", k, m[k], v)
		}
	}
	if strings.Contains(string(b), "radarr-key-123") {
		t.Error("API key stored in the clear")
	}
	if got, err := ReadMarqueeSecret(filepath.Join(home, "settings.json"), "radarr_key"); err != nil || got != "radarr-key-123" {
		t.Errorf("radarr_key round trip: %q %v", got, err)
	}
	for _, k := range []string{"password", "token"} {
		if _, ok := m[k]; ok {
			t.Errorf("%s must never be written", k)
		}
	}
	// Signed in to another server: nothing changes.
	m["token"], m["server"] = "c2VjcmV0", "http://other:8096"
	b, _ = json.Marshal(m)
	os.WriteFile(filepath.Join(home, "settings.json"), b, 0o600)
	WriteMarqueeSettings(c, home)
	b2, _ := os.ReadFile(filepath.Join(home, "settings.json"))
	if string(b2) != string(b) {
		t.Error("settings signed in elsewhere were changed")
	}
}

func TestVersionNewer(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{{"1.4.1", "1.4.0", true}, {"v1.10.0", "1.9.9", true}, {"1.4.0", "1.4.0", false}, {"1.4.0-test", "1.4.0", false},
		{"1.3.9", "1.4.0", false}, {"1.0.0", "local-abc", true}, {"local-abc", "1.0.0", false}, {"1.4", "1.4.0", false}} {
		if got := core.VersionNewer(c.a, c.b); got != c.want {
			t.Errorf("VersionNewer(%s, %s) = %v", c.a, c.b, got)
		}
	}
}
