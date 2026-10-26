package steps

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

// Marquee's release download, set when a Marquee version is published (one zip, about 72 MB, holding a
// Marquee folder with Marquee.exe). Empty URL: this build has no Marquee download.
//
//	MarqueeURL    = "https://github.com/<owner>/<repo>/releases/download/v<version>/Marquee-windows-x64.zip"
//	MarqueeSHA256 = lowercase hex of the zip
//	MarqueeSize   = the zip's size in bytes
const (
	MarqueeVersion = ""
	MarqueeURL     = ""
	MarqueeSHA256  = ""
	MarqueeSize    = 0
)

// Overrides for testing a local build:
//
//	MEDIASTACK_MARQUEE_ZIP   a local Marquee-windows-x64.zip (or an http(s) address) instead of MarqueeURL
//	MEDIASTACK_MARQUEE_HOME  where Marquee's settings are written instead of %APPDATA%\Marquee
const (
	envMarqueeZip  = "MEDIASTACK_MARQUEE_ZIP"
	envMarqueeHome = "MEDIASTACK_MARQUEE_HOME"
)

func init() {
	step(&Step{ID: "marquee", Title: "Marquee (your stack app)", Group: "Your app", OS: "windows", Optional: true,
		Button: "Install Marquee", Needs: []string{"jellyfin", "seerr"}, Run: runMarquee})
}

// MarqueeAvailable reports whether this build can download Marquee.
func MarqueeAvailable() bool { return MarqueeURL != "" || os.Getenv(envMarqueeZip) != "" }

// MarqueeDir is where Marquee is installed.
func MarqueeDir(st *core.Stack) string { return filepath.Join(st.Root, "apps", "Marquee") }

// MarqueeExe is the program the shortcuts and the firewall rule point at.
func MarqueeExe(st *core.Stack) string { return filepath.Join(MarqueeDir(st), "Marquee.exe") }

// defaultMarqueeHome is where Marquee keeps its settings unless told otherwise.
func defaultMarqueeHome() string {
	return filepath.Join(os.Getenv("APPDATA"), "Marquee")
}

// MarqueeHome is the settings folder this install's Marquee uses. A test install (ports moved) never
// shares the real one: its Marquee gets <root>\config\marquee.
func MarqueeHome(st *core.Stack) string {
	if h := os.Getenv(envMarqueeHome); h != "" {
		return h
	}
	if h := os.Getenv("MARQUEE_HOME"); h != "" {
		return h // Marquee itself reads this too
	}
	if st.PortOffset != 0 {
		return filepath.Join(st.ConfigDir("marquee"))
	}
	return defaultMarqueeHome()
}

func runMarquee(c *Ctx) error {
	st := c.St
	if st.Docker() {
		return core.UserAction("Marquee is a Windows app for now; on Linux the Media Hub is your dashboard")
	}
	if !MarqueeAvailable() {
		return core.UserAction("this version of Media Stack Setup has no Marquee download yet. Choose \"Media Hub only\" " +
			"on the Choose your stack page, or get a newer Media Stack Setup")
	}
	zip, version, err := fetchMarquee(c)
	if err != nil {
		return err
	}
	defer os.Remove(zip)
	if err := installMarquee(c, zip); err != nil {
		return err
	}
	if err := st.SetDeployed("marquee", version); err != nil {
		return err
	}
	c.J.OK("Marquee %s is installed in %s", version, MarqueeDir(st))
	marqueeShortcuts(c)

	home := MarqueeHome(st)
	if home != defaultMarqueeHome() {
		c.J.Info("Marquee's settings for this install live in %s", home)
	}
	if err := WriteMarqueeSettings(c, home); err != nil {
		return err
	}
	if st.IsDeployed("hub") {
		if err := setHubSettings(st, map[string]any{"ui": "marquee"}); err != nil {
			c.J.Warn("Couldn't tell the Media Hub that Marquee is the stack app: %v", err)
		} else {
			c.J.OK("The Media Hub keeps running in the background; Marquee's \"Your stack\" page is its dashboard now")
		}
	}
	c.J.Action("Open Marquee from the Start menu or the desktop and sign in with your Jellyfin login (%s). Marquee never got your password from this setup.", st.AdminUser)
	return nil
}

// fetchMarquee gets the zip into downloads-cache and checks it. Returns the file and the version.
func fetchMarquee(c *Ctx) (string, string, error) {
	st := c.St
	src, override := MarqueeURL, false
	if o := os.Getenv(envMarqueeZip); o != "" {
		src, override = o, true
		c.J.Info("Using the Marquee build from %s (%s)", o, envMarqueeZip)
	}
	dest := filepath.Join(st.Root, "downloads-cache", "Marquee-windows-x64.zip")
	var sum string
	var err error
	if strings.HasPrefix(src, "http://") || strings.HasPrefix(src, "https://") {
		c.J.Info("Downloading Marquee...")
		sum, err = core.Download(c, c.J, src, dest)
	} else {
		sum, err = core.CopyFile(src, dest)
	}
	if err != nil {
		return "", "", fmt.Errorf("getting Marquee: %w", err)
	}
	fi, err := os.Stat(dest)
	if err != nil {
		return "", "", err
	}
	version := MarqueeVersion
	switch {
	case override && MarqueeSHA256 == "":
		c.J.Info("Marquee zip: %d bytes, SHA-256 %s (not checked: no release hash in this build)", fi.Size(), sum)
		version = "local-" + sum[:12]
	default:
		if MarqueeSize > 0 && fi.Size() != MarqueeSize {
			os.Remove(dest)
			return "", "", fmt.Errorf("the Marquee download is %d bytes, expected %d", fi.Size(), MarqueeSize)
		}
		if !strings.EqualFold(sum, MarqueeSHA256) {
			os.Remove(dest)
			return "", "", fmt.Errorf("the Marquee download failed its checksum check (got %s)", sum)
		}
		c.J.OK("Marquee download verified (SHA-256 %s)", sum)
		if version == "" {
			version = "release-" + sum[:12]
		}
	}
	return dest, version, nil
}

// installMarquee unpacks the zip into <root>\apps\Marquee, replacing an older copy.
func installMarquee(c *Ctx, zip string) error {
	dir := MarqueeDir(c.St)
	top, err := core.ZipTopFolder(zip)
	if err != nil {
		return fmt.Errorf("the Marquee download isn't a valid zip: %w", err)
	}
	strip := 0
	if top != "" {
		strip = 1
	}
	fresh := dir + ".new"
	os.RemoveAll(fresh)
	c.J.Info("Unpacking Marquee...")
	if err := core.Unzip(zip, fresh, strip); err != nil {
		return err
	}
	if !fileExists(filepath.Join(fresh, "Marquee.exe")) {
		os.RemoveAll(fresh)
		return fmt.Errorf("the Marquee download doesn't contain Marquee.exe")
	}
	old := dir + ".old"
	os.RemoveAll(old)
	if fileExists(dir) {
		if err := os.Rename(dir, old); err != nil {
			os.RemoveAll(fresh)
			return core.UserAction("Marquee is open - close it (and its tray icon), then run this step again")
		}
	}
	if err := os.Rename(fresh, dir); err != nil {
		os.Rename(old, dir)
		return err
	}
	os.RemoveAll(old)
	return nil
}

// marqueeShortcutName is "Marquee", or "Marquee (<install name>)" for a second install.
func marqueeShortcutName(st *core.Stack) string {
	if st.Name != "Media Stack" {
		return "Marquee (" + st.Name + ")"
	}
	return "Marquee"
}

// marqueeShortcut is how the shortcuts start Marquee: directly, or (when this install keeps Marquee's
// settings somewhere else) through cmd.exe with MARQUEE_HOME set.
func marqueeShortcut(st *core.Stack) platform.Shortcut {
	exe := MarqueeExe(st)
	s := platform.Shortcut{Target: exe, WorkDir: MarqueeDir(st), Icon: exe + ",0",
		Description: "Marquee: your films and shows, requests and media stack"}
	if home := MarqueeHome(st); home != defaultMarqueeHome() {
		s.Target = filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
		s.Args = fmt.Sprintf(`/c set "MARQUEE_HOME=%s" && start "" "%s"`, home, exe)
		s.Minimized = true
	}
	return s
}

// MarqueeShortcutFiles are the Start menu and desktop shortcuts this install makes.
func MarqueeShortcutFiles(st *core.Stack) []string {
	var out []string
	name := marqueeShortcutName(st) + ".lnk"
	if d := platform.ProgramsDir(); d != "" {
		out = append(out, filepath.Join(d, name))
	}
	if d := platform.DesktopDir(); d != "" {
		out = append(out, filepath.Join(d, name))
	}
	return out
}

func marqueeShortcuts(c *Ctx) {
	s := marqueeShortcut(c.St)
	for _, f := range MarqueeShortcutFiles(c.St) {
		if err := platform.MakeShortcut(f, s); err != nil {
			c.J.Warn("Couldn't create the shortcut %s: %v", f, err)
		} else {
			c.J.OK("Shortcut: %s", f)
		}
	}
}

// ---------------------------------------------------------------------------------------------- settings

// marqueeSecret are the settings Marquee keeps DPAPI-encrypted (settings.py SECRET).
var marqueeSecret = map[string]bool{"token": true, "seerr_cookie": true, "ai_key": true, "gdrive_client_secret": true,
	"gdrive_token": true, "radarr_key": true, "sonarr_key": true, "hub_token": true}

// MarqueeSettings is what the installer fills in so Marquee works on first launch. Secrets are plain here
// and encrypted when written. No password is ever part of it: the user signs in to Jellyfin in Marquee.
func MarqueeSettings(st *core.Stack) map[string]string {
	local := func(id string) string { return fmt.Sprintf("http://127.0.0.1:%d", st.Port(id)) }
	s := map[string]string{
		"server":      fmt.Sprintf("http://localhost:%d", st.Port("jellyfin")),
		"mobile_bind": "tailscale",
	}
	if st.AdminUser != "" {
		s["user_name"] = st.AdminUser // fills the sign-in form's username
	}
	if st.IsDeployed("seerr") {
		s["seerr"] = fmt.Sprintf("http://localhost:%d", st.Port("seerr"))
	}
	if st.IsDeployed("qbittorrent") {
		s["qbt_url"] = local("qbittorrent")
	}
	if fileExists(appConfigFile(st)) {
		s["stack_config"] = appConfigFile(st)
	}
	if st.IsDeployed("hub") && st.Port("hub") != core.BasePorts["hub"] {
		s["hub_url"] = local("hub")
	}
	for _, id := range []string{"radarr", "sonarr"} {
		if st.IsDeployed(id) && st.Key(id) != "" {
			s[id+"_url"] = local(id)
			s[id+"_key"] = st.Key(id)
		}
	}
	return s
}

// WriteMarqueeSettings merges MarqueeSettings into <home>\settings.json in Marquee's own format (JSON,
// secrets as base64 DPAPI blobs). Settings the user already has stay; a sign-in to another server is kept
// and nothing is changed.
func WriteMarqueeSettings(c *Ctx, home string) error {
	file := filepath.Join(home, "settings.json")
	cur := map[string]any{}
	if b, err := os.ReadFile(file); err == nil {
		if err := json.Unmarshal(bytes.TrimPrefix(b, []byte("\xef\xbb\xbf")), &cur); err != nil {
			return fmt.Errorf("%s is damaged (%v) - fix or delete it, then run this step again", file, err)
		}
	}
	want := MarqueeSettings(c.St)
	if tok, _ := cur["token"].(string); tok != "" {
		if srv, _ := cur["server"].(string); srv != "" && !strings.EqualFold(strings.TrimRight(srv, "/"), want["server"]) {
			c.J.Warn("Marquee is already signed in to another Jellyfin (%s), so its settings were left as they are. "+
				"To use this server: Marquee > Settings > Sign out, then sign in with %s.", srv, want["server"])
			return nil
		}
		delete(want, "user_name")
	}
	if b, _ := cur["mobile_bind"].(string); b != "" {
		delete(want, "mobile_bind") // the user's own choice in Marquee's Settings
	}
	for k, v := range want {
		if marqueeSecret[k] {
			blob, err := platform.Protect([]byte(v))
			if err != nil {
				return err
			}
			cur[k] = base64.StdEncoding.EncodeToString(blob)
		} else {
			cur[k] = v
		}
	}
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", " ")
	if err := enc.Encode(cur); err != nil {
		return err
	}
	if err := core.WriteFileAtomic(file, b.Bytes(), 0o600); err != nil {
		return err
	}
	var filled []string
	for _, k := range core.SortedKeys(want) {
		filled = append(filled, k)
	}
	c.J.OK("Marquee is set up for this stack (%s)", strings.Join(filled, ", "))
	return nil
}

// ReadMarqueeSecret decrypts one secret from a Marquee settings file (tests).
func ReadMarqueeSecret(file, key string) (string, error) {
	b, err := os.ReadFile(file)
	if err != nil {
		return "", err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return "", err
	}
	s, _ := m[key].(string)
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return "", err
	}
	out, err := platform.Unprotect(raw)
	return string(out), err
}

// ---------------------------------------------------------------------------------------------- Media Hub

func hubSettingsFile(st *core.Stack) string { return filepath.Join(st.ConfigDir("hub"), "hub.json") }

// setHubSettings merges keys into the Media Hub's hub.json (it reads the file on every request).
func setHubSettings(st *core.Stack, changes map[string]any) error {
	file := hubSettingsFile(st)
	cur := map[string]any{}
	if b, err := os.ReadFile(file); err == nil {
		json.Unmarshal(b, &cur)
	}
	for k, v := range changes {
		cur[k] = v
	}
	b, err := json.Marshal(cur)
	if err != nil {
		return err
	}
	if err := core.WriteFileAtomic(file, b, 0o644); err != nil {
		return err
	}
	return platform.Chown(st, file)
}

func hubSetting(st *core.Stack, key string) any {
	cur := map[string]any{}
	if b, err := os.ReadFile(hubSettingsFile(st)); err == nil {
		json.Unmarshal(b, &cur)
	}
	return cur[key]
}
