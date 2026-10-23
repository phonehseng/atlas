package platform

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"mediastack/internal/core"
)

func linuxStack(t *testing.T) *core.Stack {
	st := core.New(t.TempDir())
	st.OS = "linux"
	st.UID, st.GID, st.TZ = 1000, 1000, "Europe/London"
	return st
}

func TestComposeVPN(t *testing.T) {
	st := linuxStack(t)
	st.VPN = core.VPN{Mode: "gluetun", Provider: "nordvpn", Country: "Netherlands"}
	for _, id := range []string{"gluetun", "qbittorrent", "sonarr", "trawl", "hub", "jellyfin"} {
		st.Deployed[id] = "x"
	}
	doc, err := BuildCompose(st)
	if err != nil {
		t.Fatal(err)
	}
	svcs := doc["services"].(map[string]any)
	qbt := svcs["qbittorrent"].(map[string]any)
	if qbt["network_mode"] != "service:gluetun" || qbt["ports"] != nil {
		t.Fatalf("qBittorrent must live inside the VPN container: %v", qbt)
	}
	g := svcs["gluetun"].(map[string]any)
	if ports := g["ports"].([]string); len(ports) != 1 || ports[0] != "8080:8080" {
		t.Fatalf("gluetun should publish qBittorrent's web UI: %v", g["ports"])
	}
	env := g["environment"].(map[string]any)
	if env["OPENVPN_PASSWORD"] != "${VPN_PASSWORD}" || env["SERVER_COUNTRIES"] != "Netherlands" {
		t.Fatalf("gluetun env: %v", env)
	}
	if p := svcs["trawl"].(map[string]any)["ports"].([]string)[0]; !strings.HasPrefix(p, "127.0.0.1:") {
		t.Fatalf("trawl must only listen locally: %s", p)
	}
	if u := svcs["sonarr"].(map[string]any)["environment"].(map[string]any)["PUID"]; u != "1000" {
		t.Fatalf("PUID: %v", u)
	}
	b, err := yaml.Marshal(doc)
	if err != nil || !strings.Contains(string(b), "linuxserver/sonarr") {
		t.Fatalf("yaml: %v\n%s", err, b)
	}
}

func TestComposeNoVPN(t *testing.T) {
	st := linuxStack(t)
	st.VPN = core.VPN{Mode: "none"}
	st.Deployed["qbittorrent"] = "x"
	doc, _ := BuildCompose(st)
	qbt := doc["services"].(map[string]any)["qbittorrent"].(map[string]any)
	if qbt["network_mode"] != nil || qbt["ports"] == nil {
		t.Fatalf("without a VPN qBittorrent publishes its own port: %v", qbt)
	}
}

func TestPortOffset(t *testing.T) {
	st := linuxStack(t)
	st.PortOffset = 20000
	st.Deployed["sonarr"] = "x"
	doc, _ := BuildCompose(st)
	if p := doc["services"].(map[string]any)["sonarr"].(map[string]any)["ports"].([]string)[0]; p != "28989:8989" {
		t.Fatalf("offset port mapping: %s", p)
	}
}

func TestFlatXML(t *testing.T) {
	file := filepath.Join(t.TempDir(), "config.xml")
	os.WriteFile(file, []byte("<Config>\n  <Port>8989</Port>\n  <ApiKey>abc</ApiKey>\n  <UrlBase></UrlBase>\n</Config>\n"), 0o644)
	x, err := ReadFlatXML(file)
	if err != nil {
		t.Fatal(err)
	}
	if x.Get("ApiKey") != "abc" || x.Get("Port") != "8989" {
		t.Fatalf("read: %+v", x)
	}
	x.Set("Port", "28989")
	x.Set("InstanceName", "A & B")
	if err := x.Write(file); err != nil {
		t.Fatal(err)
	}
	y, _ := ReadFlatXML(file)
	if y.Get("Port") != "28989" || y.Get("ApiKey") != "abc" || y.Get("InstanceName") != "A & B" || y.Keys[0] != "Port" {
		t.Fatalf("round trip: %+v", y)
	}
}

func TestIni(t *testing.T) {
	file := filepath.Join(t.TempDir(), "q.ini")
	os.WriteFile(file, []byte("\xef\xbb\xbf[BitTorrent]\nSession\\Port=1234\n\n[Preferences]\nWebUI\\Port=8080\n"), 0o644)
	ini, err := ReadIni(file)
	if err != nil {
		t.Fatal(err)
	}
	ini.Set("WebUI", "Port", "28080")
	ini.Set("Preferences", `WebUI\Port`, "28080")
	if err := ini.Write(); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(file)
	s := string(b)
	if strings.HasPrefix(s, "\xef\xbb\xbf") || !strings.Contains(s, "Session\\Port=1234") || !strings.Contains(s, "[WebUI]\nPort=28080") ||
		!strings.Contains(s, "WebUI\\Port=28080") {
		t.Fatalf("ini:\n%s", s)
	}
}

func TestQbtHash(t *testing.T) {
	h := QbtPasswordHash("correct horse")
	if !strings.HasPrefix(h, `"@ByteArray(`) || !strings.HasSuffix(h, `)"`) || strings.Count(h, ":") != 1 {
		t.Fatalf("hash format: %s", h)
	}
}

func TestEscapeArg(t *testing.T) {
	cases := map[string]string{
		`plain`:            `plain`,
		`C:\Program Files`: `"C:\Program Files"`,
		`a"b`:              `"a\"b"`,
		`ends\`:            `ends\`,
		`sp ace\`:          `"sp ace\\"`,
		``:                 `""`,
	}
	for in, want := range cases {
		if got := EscapeArg(in); got != want {
			t.Errorf("EscapeArg(%q) = %s, want %s", in, got, want)
		}
	}
}

func TestTaskXML(t *testing.T) {
	st := core.New(`D:\Stack`)
	st.OS = "windows"
	b, err := taskXML(st, Get("sonarr"), `D:\Stack\apps\MediaStack.exe`, []string{"run", "--log", `D:\Stack\logs\s.log`, "--", `D:\My Apps\Sonarr.exe`}, `D:\Stack\apps\sonarr`)
	if err != nil {
		t.Fatal(err)
	}
	if b[0] != 0xFF || b[1] != 0xFE {
		t.Fatal("task XML must be UTF-16 with a byte-order mark")
	}
	s := decodeUTF16(b[2:])
	for _, want := range []string{"<LogonTrigger>", `&quot;D:\My Apps\Sonarr.exe&quot;`, "<RestartOnFailure>", "LeastPrivilege"} {
		if !strings.Contains(s, want) {
			t.Errorf("task XML lacks %s:\n%s", want, s)
		}
	}
	b, _ = taskXML(st, Get("recyclarr"), "x.exe", nil, `D:\`)
	if s := decodeUTF16(b[2:]); !strings.Contains(s, "<CalendarTrigger>") || strings.Contains(s, "RestartOnFailure") {
		t.Errorf("weekly task:\n%s", s)
	}
}

func decodeUTF16(b []byte) string {
	r := make([]rune, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		r = append(r, rune(uint16(b[i])|uint16(b[i+1])<<8))
	}
	return string(r)
}

func TestExpand(t *testing.T) {
	st := core.New(`D:\Stack`)
	st.PortOffset = 10
	if got := Expand(st, "bazarr", "{config}|{port}"); got != filepath.Join(`D:\Stack`, "config", "bazarr")+"|6777" {
		t.Fatalf("Expand: %s", got)
	}
}
