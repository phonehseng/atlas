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

// Atlas's release download (the app was called Marquee before; the Go names keep that), set when an Atlas
// version is published: one zip, about 75 MB, with Atlas.exe and its files. Fill in all three of version,
// SHA-256 and size; while the version or the SHA-256 is empty this build has no Atlas download.
//
//	MarqueeVersion = the release version, e.g. "1.6.0" (the URL is made from it)
//	MarqueeSHA256  = lowercase hex of Atlas-windows-x64.zip
//	MarqueeSize    = the zip's size in bytes
const (
	MarqueeVersion = "1.6.0"
	MarqueeURL     = "https://github.com/phonehseng/atlas/releases/download/v" + MarqueeVersion + "/Atlas-windows-x64.zip"
	MarqueeSHA256  = "e88e06607b5ca8c8ce07dd4cf4190c4e2575b3f5b1c4ce08621c375077e35fec"
	MarqueeSize    = 99281419
)

// Overrides for testing a local build:
//
//	MEDIASTACK_MARQUEE_ZIP   a local Atlas-windows-x64.zip (or an http(s) address) instead of MarqueeURL
//	MEDIASTACK_MARQUEE_HOME  where Atlas's settings are written instead of %APPDATA%\Marquee
const (
	envMarqueeZip  = "MEDIASTACK_MARQUEE_ZIP"
	envMarqueeHome = "MEDIASTACK_MARQUEE_HOME"
)

func init() {
	step(&Step{ID: "marquee", Title: "Atlas (your stack app)", Group: "Your app", OS: "windows", Optional: true,
		Button: "Install Atlas", Needs: []string{"jellyfin", "seerr"}, Run: runMarquee})
}

// MarqueeAvailable reports whether this build can download Atlas.
func MarqueeAvailable() bool {
	return MarqueeVersion != "" && MarqueeSHA256 != "" || os.Getenv(envMarqueeZip) != ""
}

// MarqueeDir is where Atlas is installed.
func MarqueeDir(st *core.Stack) string { return filepath.Join(st.Root, "apps", "Atlas") }

// legacyMarqueeDir is where installers from before the rename put the app (moved to MarqueeDir on update).
func legacyMarqueeDir(st *core.Stack) string { return filepath.Join(st.Root, "apps", "Marquee") }

// marqueeExes are the app's exe names, newest first: Marquee.exe is a build from before the rename (the first
// Atlas release carries both).
var marqueeExes = []string{"Atlas.exe", "Marquee.exe"}

// MarqueeExe is the program the shortcuts and the firewall rule point at: Atlas.exe, or Marquee.exe when the
// installed build is older than the rename.
func MarqueeExe(st *core.Stack) string { return appExe(MarqueeDir(st)) }

func appExe(dir string) string {
	for _, n := range marqueeExes {
		if p := filepath.Join(dir, n); fileExists(p) {
			return p
		}
	}
	return filepath.Join(dir, marqueeExes[0])
}

// defaultMarqueeHome is where Atlas keeps its settings unless told otherwise (the folder kept its old name).
func defaultMarqueeHome() string {
	return filepath.Join(os.Getenv("APPDATA"), "Marquee")
}

// MarqueeHome is the settings folder this install's Atlas uses. A test install (ports moved) never
// shares the real one: its Atlas gets <root>\config\marquee.
func MarqueeHome(st *core.Stack) string {
	if h := os.Getenv(envMarqueeHome); h != "" {
		return h
	}
	if h := os.Getenv("MARQUEE_HOME"); h != "" {
		return h // Atlas itself reads this too
	}
	if st.PortOffset != 0 {
		return filepath.Join(st.ConfigDir("marquee"))
	}
	return defaultMarqueeHome()
}

func runMarquee(c *Ctx) error {
	st := c.St
	if st.Docker() {
		return core.UserAction("Atlas is a Windows app for now; on Linux the Media Hub is your dashboard")
	}
	if !MarqueeAvailable() {
		return core.UserAction("this version of Media Stack Setup has no Atlas download yet. Choose \"Media Hub only\" " +
			"on the Choose your stack page, or get a newer Media Stack Setup")
	}
	zip, version, err := fetchMarquee(c)
	if err != nil {
		return err
	}
	defer os.Remove(zip)
	migrated, err := migrateMarquee(c)
	if err != nil {
		return err
	}
	if err := installMarquee(c, zip); err != nil {
		return err
	}
	if err := st.SetDeployed("marquee", version); err != nil {
		return err
	}
	c.J.OK("Atlas %s is installed in %s", version, MarqueeDir(st))
	marqueeShortcuts(c)
	if migrated {
		migrateMarqueeFirewall(c)
	}

	home := MarqueeHome(st)
	if home != defaultMarqueeHome() {
		c.J.Info("Atlas's settings for this install live in %s", home)
	}
	if err := WriteMarqueeSettings(c, home); err != nil {
		return err
	}
	if st.IsDeployed("hub") {
		if err := setHubSettings(st, map[string]any{"ui": "marquee"}); err != nil {
			c.J.Warn("Couldn't tell the Media Hub that Atlas is the stack app: %v", err)
		} else {
			c.J.OK("The Media Hub keeps running in the background; Atlas's \"Your stack\" page is its dashboard now")
		}
	}
	c.J.Action("Open Atlas from the Start menu or the desktop and sign in with your Jellyfin login (%s). Atlas never got your password from this setup.", st.AdminUser)
	return nil
}

// fetchMarquee gets the zip into downloads-cache and checks it. Returns the file and the version.
func fetchMarquee(c *Ctx) (string, string, error) {
	st := c.St
	src, override := MarqueeURL, false
	if o := os.Getenv(envMarqueeZip); o != "" {
		src, override = o, true
		c.J.Info("Using the Atlas build from %s (%s)", o, envMarqueeZip)
	}
	dest := filepath.Join(st.Root, "downloads-cache", "Atlas-windows-x64.zip")
	var sum string
	var err error
	if strings.HasPrefix(src, "http://") || strings.HasPrefix(src, "https://") {
		c.J.Info("Downloading Atlas...")
		sum, err = core.Download(c, c.J, src, dest)
	} else {
		sum, err = core.CopyFile(src, dest)
	}
	if err != nil {
		return "", "", fmt.Errorf("getting Atlas: %w", err)
	}
	fi, err := os.Stat(dest)
	if err != nil {
		return "", "", err
	}
	version := MarqueeVersion
	switch {
	case override && MarqueeSHA256 == "":
		c.J.Info("Atlas zip: %d bytes, SHA-256 %s (not checked: no release hash in this build)", fi.Size(), sum)
		version = "local-" + sum[:12]
	default:
		if MarqueeSize > 0 && fi.Size() != MarqueeSize {
			os.Remove(dest)
			return "", "", fmt.Errorf("the Atlas download is %d bytes, expected %d", fi.Size(), MarqueeSize)
		}
		if !strings.EqualFold(sum, MarqueeSHA256) {
			os.Remove(dest)
			return "", "", fmt.Errorf("the Atlas download failed its checksum check (got %s)", sum)
		}
		c.J.OK("Atlas download verified (SHA-256 %s)", sum)
		if version == "" {
			version = "release-" + sum[:12]
		}
	}
	return dest, version, nil
}

// installMarquee unpacks the zip into <root>\apps\Atlas, replacing an older copy.
func installMarquee(c *Ctx, zip string) error {
	dir := MarqueeDir(c.St)
	top, err := core.ZipTopFolder(zip)
	if err != nil {
		return fmt.Errorf("the Atlas download isn't a valid zip: %w", err)
	}
	strip := 0
	if top != "" {
		strip = 1
	}
	fresh := dir + ".new"
	os.RemoveAll(fresh)
	c.J.Info("Unpacking Atlas...")
	if err := core.Unzip(zip, fresh, strip); err != nil {
		return err
	}
	if !fileExists(appExe(fresh)) {
		os.RemoveAll(fresh)
		return fmt.Errorf("the Atlas download doesn't contain Atlas.exe")
	}
	old := dir + ".old"
	os.RemoveAll(old)
	if fileExists(dir) {
		if err := os.Rename(dir, old); err != nil {
			os.RemoveAll(fresh)
			return core.UserAction("Atlas is open - close it (and its tray icon), then run this step again")
		}
	}
	if err := os.Rename(fresh, dir); err != nil {
		os.Rename(old, dir)
		return err
	}
	os.RemoveAll(old)
	return nil
}

// marqueeShortcutName is "Atlas", or "Atlas (<install name>)" for a second install.
func marqueeShortcutName(st *core.Stack) string { return shortcutName(st, "Atlas") }

// legacyShortcutName is the shortcuts' name from before the rename.
func legacyShortcutName(st *core.Stack) string { return shortcutName(st, "Marquee") }

func shortcutName(st *core.Stack, app string) string {
	if st.Name != "Media Stack" {
		return app + " (" + st.Name + ")"
	}
	return app
}

// marqueeShortcut is how the shortcuts start Atlas: directly, or (when this install keeps Atlas's
// settings somewhere else) through cmd.exe with MARQUEE_HOME set.
func marqueeShortcut(st *core.Stack) platform.Shortcut {
	exe := MarqueeExe(st)
	s := platform.Shortcut{Target: exe, WorkDir: MarqueeDir(st), Icon: exe + ",0",
		Description: "Atlas: your films and shows, requests and media stack"}
	if home := MarqueeHome(st); home != defaultMarqueeHome() {
		s.Target = filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
		s.Args = fmt.Sprintf(`/c set "MARQUEE_HOME=%s" && start "" "%s"`, home, exe)
		s.Minimized = true
	}
	return s
}

// MarqueeShortcutFiles are the Start menu and desktop shortcuts this install makes.
func MarqueeShortcutFiles(st *core.Stack) []string { return shortcutFiles(marqueeShortcutName(st)) }

// LegacyShortcutFiles are the shortcuts installers from before the rename made ("Marquee").
func LegacyShortcutFiles(st *core.Stack) []string { return shortcutFiles(legacyShortcutName(st)) }

func shortcutFiles(base string) []string {
	var out []string
	name := base + ".lnk"
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

// ---------------------------------------------------------------------------------------------- the rename

// migrateMarquee moves an install from before the rename (Marquee) to Atlas's names: <root>\apps\Marquee
// becomes <root>\apps\Atlas and the "Marquee" Start menu and desktop shortcuts are removed (the caller makes
// the Atlas ones once the new build is in place). Settings stay in %APPDATA%\Marquee. Returns whether there
// was anything to move.
func migrateMarquee(c *Ctx) (bool, error) {
	st := c.St
	old, dir := legacyMarqueeDir(st), MarqueeDir(st)
	moved := false
	if fileExists(old) {
		if fileExists(dir) {
			// both: an earlier run already installed Atlas; the old folder is only a leftover
			if err := os.RemoveAll(old); err != nil {
				return false, core.UserAction("Marquee is open from " + old + " - close it (and its tray icon), then run this step again")
			}
			c.J.OK("Removed the old copy in %s (Atlas is in %s)", old, dir)
		} else {
			if err := os.Rename(old, dir); err != nil {
				return false, core.UserAction("Marquee is open - close it (and its tray icon), then run this step again")
			}
			c.J.OK("Moved %s to %s (Marquee is called Atlas now; its settings stay where they are)", old, dir)
		}
		moved = true
	}
	for _, f := range LegacyShortcutFiles(st) {
		if !fileExists(f) {
			continue
		}
		if err := os.Remove(f); err != nil {
			c.J.Warn("Couldn't remove the old shortcut %s: %v", f, err)
			continue
		}
		c.J.OK("Removed the old shortcut %s", f)
		moved = true
	}
	return moved, nil
}

// migrateMarqueeFirewall replaces the firewall rule installers before the rename made ("Marquee Mobile
// (Tailscale)", for apps\Marquee\Marquee.exe) with Atlas's, if it's there. One Windows permission prompt.
func migrateMarqueeFirewall(c *Ctx) {
	st := c.St
	if st.Docker() || !platform.FirewallRuleExists(LegacyRuleName(st)) {
		return
	}
	c.J.Info("Moving the Windows Firewall rule for phones to Atlas...")
	script := "$ErrorActionPreference = 'Stop'\n" +
		fmt.Sprintf("Get-NetFirewallRule -DisplayName %s -ErrorAction SilentlyContinue | Remove-NetFirewallRule\n", psQuote(LegacyRuleName(st))) +
		marqueeRuleScript(st)
	if err := platform.RunElevated(c, c.J, script); err != nil {
		c.J.Warn("The firewall rule wasn't moved (%v). Run \"Remote access\" again, or use Allow Atlas in Atlas > Settings > Atlas Mobile.", err)
		return
	}
	c.J.OK("Windows Firewall rule %q: Atlas Mobile (port %d) on private networks", MarqueeRuleName(st), MarqueeMobilePort)
}

// ---------------------------------------------------------------------------------------------- settings

// marqueeSecret are the settings Atlas keeps DPAPI-encrypted (settings.py SECRET).
var marqueeSecret = map[string]bool{"token": true, "seerr_cookie": true, "ai_key": true, "gdrive_client_secret": true,
	"gdrive_token": true, "radarr_key": true, "sonarr_key": true, "hub_token": true, "bazarr_key": true}

// MarqueeSettings is what the installer fills in so Atlas works on first launch. Secrets are plain here
// and encrypted when written. No password is ever part of it: the user signs in to Jellyfin in Atlas.
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
	for _, id := range []string{"radarr", "sonarr", "bazarr"} {
		if st.IsDeployed(id) && st.Key(id) != "" {
			s[id+"_url"] = local(id)
			s[id+"_key"] = st.Key(id)
		}
	}
	return s
}

// WriteMarqueeSettings merges MarqueeSettings into <home>\settings.json in Atlas's own format (JSON,
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
			c.J.Warn("Atlas is already signed in to another Jellyfin (%s), so its settings were left as they are. "+
				"To use this server: Atlas > Settings > Sign out, then sign in with %s.", srv, want["server"])
			return nil
		}
		delete(want, "user_name")
	}
	if b, _ := cur["mobile_bind"].(string); b != "" {
		delete(want, "mobile_bind") // the user's own choice in Atlas's Settings
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
	c.J.OK("Atlas is set up for this stack (%s)", strings.Join(filled, ", "))
	return nil
}

// ReadMarqueeSecret decrypts one secret from an Atlas settings file (tests).
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
