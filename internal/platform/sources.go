package platform

import (
	"context"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha512"
	"encoding/base64"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"mediastack/assets"
	"mediastack/internal/core"
)

// ---------------------------------------------------------------------------------------------- Jellyfin

func jellyfinSource(ctx context.Context) (string, string, string, error) {
	const dir = "https://repo.jellyfin.org/files/server/windows/latest-stable/amd64/"
	page, err := core.GetText(ctx, dir)
	if err != nil {
		return "", "", "", fmt.Errorf("couldn't reach Jellyfin's download site: %w", err)
	}
	m := regexp.MustCompile(`jellyfin_([\d.]+)-amd64\.zip`).FindStringSubmatch(page)
	if m == nil {
		return "", "", "", fmt.Errorf("couldn't find the Jellyfin download on %s", dir)
	}
	return m[0], dir + m[0], m[1], nil
}

// ---------------------------------------------------------------------------------------------- qBittorrent

// qbtIni is where qBittorrent keeps its settings for our profile folder.
func qbtIni(st *core.Stack) string {
	if st.Docker() {
		return filepath.Join(st.ConfigDir("qbittorrent"), "qBittorrent", "qBittorrent.conf")
	}
	return filepath.Join(st.ConfigDir("qbittorrent"), "qBittorrent", "config", "qBittorrent.ini")
}

// QbtPasswordHash is qBittorrent's stored form of a Web UI password: PBKDF2-SHA512, 100,000 rounds.
func QbtPasswordHash(password string) string {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		panic(err)
	}
	key, err := pbkdf2.Key(sha512.New, password, salt, 100_000, 64)
	if err != nil {
		panic(err)
	}
	return fmt.Sprintf(`"@ByteArray(%s:%s)"`, base64.StdEncoding.EncodeToString(salt), base64.StdEncoding.EncodeToString(key))
}

// prepareQbittorrent turns the Web UI on with your admin login before the first start (qBittorrent
// otherwise invents a temporary password). Everything else is set through its API once it runs.
func prepareQbittorrent(_ context.Context, st *core.Stack, _ *core.Job, port int) error {
	ini, err := ReadIni(qbtIni(st))
	if err != nil {
		return err
	}
	addr := "127.0.0.1" // Windows: apps on this PC only; the hub opens it in a tab on this PC
	if st.Docker() {
		addr = "*"
	}
	set := map[string]string{
		"Enabled": "true", "Address": addr, "Port": strconv.Itoa(port),
		// Apps on this PC (Sonarr, Radarr, MediaSorter...) don't need the password on Windows.
		"LocalHostAuth":        strconv.FormatBool(st.Docker()),
		"HostHeaderValidation": "false", "CSRFProtection": "false",
	}
	if st.AdminUser != "" && st.AdminPass != "" && ini.Get("WebUI", "Password_PBKDF2") == "" {
		set["Username"] = st.AdminUser
		set["Password_PBKDF2"] = QbtPasswordHash(st.AdminPass)
	}
	for k, v := range set {
		ini.Set("WebUI", k, v)
		ini.Set("Preferences", `WebUI\`+k, v) // older qBittorrent versions read this section
	}
	ini.Set("LegalNotice", "Accepted", "true")
	ini.Set("Preferences", `General\Locale`, "en")
	ini.Set("Application", `FileLogger\Enabled`, "true")
	ini.Set("Application", `FileLogger\Path`, "logs")
	return ini.Write()
}

// ---------------------------------------------------------------------------------------------- Python (uv)

// EnsureUV installs uv (Astral's Python manager) and a private Python, so nothing touches a system Python.
func EnsureUV(ctx context.Context, st *core.Stack, j *core.Job) (string, error) {
	dir := filepath.Join(st.Root, "apps", "uv")
	uv := filepath.Join(dir, "uv.exe")
	if _, err := os.Stat(uv); err != nil {
		rel, err := core.LatestRelease(ctx, "astral-sh/uv")
		if err != nil {
			return "", err
		}
		name, url, err := rel.Asset(`^uv-x86_64-pc-windows-msvc\.zip$`)
		if err != nil {
			return "", err
		}
		zip := filepath.Join(st.Root, "downloads-cache", name)
		if _, err := core.Download(ctx, j, url, zip); err != nil {
			return "", err
		}
		if err := core.Unzip(zip, dir, 0); err != nil {
			return "", err
		}
		os.Remove(zip)
	}
	return uv, nil
}

// uvEnv keeps uv's Python builds and cache inside the stack folder.
func uvEnv(st *core.Stack) []string {
	return []string{
		"UV_PYTHON_INSTALL_DIR=" + filepath.Join(st.Root, "apps", "python"),
		"UV_CACHE_DIR=" + filepath.Join(st.Root, "apps", "uv", "cache"),
		"UV_LINK_MODE=copy", "UV_NO_PROGRESS=1", "PYTHONIOENCODING=utf-8",
		// Always uv's own Python: a venv built on the system Python breaks if that Python is removed.
		"UV_PYTHON_PREFERENCE=only-managed",
	}
}

// Run runs a command, streaming its output into the job log.
func Run(ctx context.Context, j *core.Job, dir string, env []string, exe string, args ...string) error {
	cmd := exec.CommandContext(ctx, exe, args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), env...)
	w := j.Writer("  ")
	cmd.Stdout, cmd.Stderr = w, w
	hideWindow(cmd)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s %s failed: %w", filepath.Base(exe), strings.Join(args, " "), err)
	}
	return nil
}

// githubSource downloads a repository's source for a tag and unpacks it into dest (replacing it).
func githubSource(ctx context.Context, st *core.Stack, j *core.Job, repo, tag, dest string) error {
	zip := filepath.Join(st.Root, "downloads-cache", strings.ReplaceAll(repo, "/", "_")+"-"+tag+".zip")
	if _, err := core.Download(ctx, j, fmt.Sprintf("https://github.com/%s/archive/refs/tags/%s.zip", repo, tag), zip); err != nil {
		return err
	}
	defer os.Remove(zip)
	if err := os.RemoveAll(dest); err != nil {
		return err
	}
	return core.Unzip(zip, dest, 1)
}

// swapIn replaces dir with fresh (built next to it), keeping the old copy until the swap succeeds.
func swapIn(fresh, dir string) error {
	old := dir + ".old"
	os.RemoveAll(old)
	if _, err := os.Stat(dir); err == nil {
		if err := os.Rename(dir, old); err != nil {
			return fmt.Errorf("couldn't replace %s (is it still running?): %w", dir, err)
		}
	}
	if err := os.Rename(fresh, dir); err != nil {
		os.Rename(old, dir)
		return err
	}
	os.RemoveAll(old)
	return nil
}

// ---------------------------------------------------------------------------------------------- Bazarr

func installBazarr(ctx context.Context, st *core.Stack, j *core.Job, dir string) (string, error) {
	rel, err := core.LatestRelease(ctx, "morpheus65535/bazarr")
	if err != nil {
		return "", err
	}
	_, url, err := rel.Asset(`^bazarr\.zip$`)
	if err != nil {
		return "", err
	}
	zip := filepath.Join(st.Root, "downloads-cache", "bazarr-"+rel.Tag+".zip")
	if _, err := core.Download(ctx, j, url, zip); err != nil {
		return "", err
	}
	defer os.Remove(zip)
	fresh := dir + ".new"
	os.RemoveAll(fresh)
	if err := core.Unzip(zip, fresh, 0); err != nil {
		return "", err
	}
	uv, err := EnsureUV(ctx, st, j)
	if err != nil {
		return "", err
	}
	j.Info("Setting up Python for Bazarr (first time takes a few minutes)...")
	env := uvEnv(st)
	if err := Run(ctx, j, fresh, env, uv, "venv", "--seed", "--python", "3.12", ".venv"); err != nil {
		return "", err
	}
	// pip itself: Bazarr's requirements.txt uses per-package options uv doesn't parse.
	py := filepath.Join(fresh, ".venv", "Scripts", "python.exe")
	if err := Run(ctx, j, fresh, env, py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off", "-r", "requirements.txt"); err != nil {
		return "", err
	}
	if err := swapIn(fresh, dir); err != nil {
		return "", err
	}
	return rel.Tag, nil
}

// ---------------------------------------------------------------------------------------------- Seerr

// installSeerr builds Seerr from source (it has no Windows download) with a private Node.js.
func installSeerr(ctx context.Context, st *core.Stack, j *core.Job, dir string) (string, error) {
	node, err := ensureNode(ctx, st, j, "22")
	if err != nil {
		return "", err
	}
	rel, err := core.LatestRelease(ctx, "seerr-team/seerr")
	if err != nil {
		return "", err
	}
	// Built where it runs: pnpm links packages by absolute path on Windows, so the folder can't be moved after.
	fresh := dir
	if err := githubSource(ctx, st, j, "seerr-team/seerr", rel.Tag, fresh); err != nil {
		return "", err
	}
	nodeDir := filepath.Dir(node)
	env := []string{
		"PATH=" + nodeDir + string(os.PathListSeparator) + os.Getenv("PATH"),
		"COREPACK_HOME=" + filepath.Join(nodeDir, "corepack-home"),
		"COREPACK_ENABLE_DOWNLOAD_PROMPT=0",
		"CYPRESS_INSTALL_BINARY=0", // test tool, not needed to run Seerr
		"NODE_OPTIONS=--max-old-space-size=4096",
		"NEXT_TELEMETRY_DISABLED=1",
	}
	corepack := filepath.Join(nodeDir, "corepack.cmd")
	if err := Run(ctx, j, nodeDir, env, corepack, "enable", "--install-directory", nodeDir); err != nil {
		return "", err
	}
	pnpm := filepath.Join(nodeDir, "pnpm.cmd")
	j.Info("Installing Seerr's packages (several minutes)...")
	if err := Run(ctx, j, fresh, env, pnpm, "install", "--frozen-lockfile", "--store-dir", filepath.Join(st.Root, "apps", "node", "pnpm-store")); err != nil {
		return "", err
	}
	j.Info("Building Seerr (5-10 minutes; the log may pause for a while)...")
	if err := Run(ctx, j, fresh, env, pnpm, "build"); err != nil {
		return "", err
	}
	return rel.Tag, nil
}

// ensureNode installs the newest Node.js of a major version into apps\node.
func ensureNode(ctx context.Context, st *core.Stack, j *core.Job, major string) (string, error) {
	dir := filepath.Join(st.Root, "apps", "node")
	exe := filepath.Join(dir, "node.exe")
	var index []struct {
		Version string `json:"version"`
		Files   []string
	}
	if err := core.GetJSON(ctx, "https://nodejs.org/dist/index.json", &index); err != nil {
		return "", fmt.Errorf("couldn't reach nodejs.org: %w", err)
	}
	want := ""
	for _, v := range index {
		if strings.HasPrefix(v.Version, "v"+major+".") {
			want = v.Version
			break
		}
	}
	if want == "" {
		return "", fmt.Errorf("no Node.js %s release found", major)
	}
	if b, err := os.ReadFile(filepath.Join(dir, "VERSION")); err == nil && strings.TrimSpace(string(b)) == want {
		return exe, nil
	}
	name := fmt.Sprintf("node-%s-win-x64.zip", want)
	base := "https://nodejs.org/dist/" + want + "/"
	sums, err := core.GetText(ctx, base+"SHASUMS256.txt")
	if err != nil {
		return "", err
	}
	zip := filepath.Join(st.Root, "downloads-cache", name)
	got, err := core.Download(ctx, j, base+name, zip)
	if err != nil {
		return "", err
	}
	defer os.Remove(zip)
	if !strings.Contains(sums, got+"  "+name) {
		return "", fmt.Errorf("%s failed its checksum check", name)
	}
	fresh := dir + ".new"
	os.RemoveAll(fresh)
	if err := core.Unzip(zip, fresh, 1); err != nil {
		return "", err
	}
	os.WriteFile(filepath.Join(fresh, "VERSION"), []byte(want), 0o644)
	if err := swapIn(fresh, dir); err != nil {
		return "", err
	}
	j.OK("Node.js %s ready", want)
	return exe, nil
}

// ---------------------------------------------------------------------------------------------- Byparr

func installByparr(ctx context.Context, st *core.Stack, j *core.Job, dir string) (string, error) {
	rel, err := core.LatestRelease(ctx, "ThePhaseless/Byparr")
	if err != nil {
		return "", err
	}
	fresh := dir + ".new"
	if err := githubSource(ctx, st, j, "ThePhaseless/Byparr", rel.Tag, fresh); err != nil {
		return "", err
	}
	uv, err := EnsureUV(ctx, st, j)
	if err != nil {
		return "", err
	}
	env := uvEnv(st)
	j.Info("Setting up Python for Byparr...")
	if err := Run(ctx, j, fresh, env, uv, "sync", "--frozen", "--no-dev"); err != nil {
		return "", err
	}
	j.Info("Downloading Byparr's browser...")
	py := filepath.Join(fresh, ".venv", "Scripts", "python.exe")
	if err := Run(ctx, j, fresh, env, py, "-m", "invisible_playwright", "fetch"); err != nil {
		return "", err
	}
	if err := swapIn(fresh, dir); err != nil {
		return "", err
	}
	return rel.Tag, nil
}

// ---------------------------------------------------------------------------------------------- trawl

// Pinned: trawl is patched at install time, so versions move only after the patches are re-checked.
const (
	trawlVersion  = "v1.9.2"
	bunVersion    = "1.4.3"
	camoufoxTag   = "v152.0.4-beta.30"
	camoufoxVer   = "152.0.4"
	camoufoxRel   = "beta.30"
	uboVersion    = "1.75.0"
	uboAMOFileID  = "5034826"
	uboSHA256     = "5b74415860456370644bd80f16125e865b0e6c356bb5dfcfb84069967eaa5287"
	geoLiteURL    = "https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-City.mmdb"
	geoLiteMinLen = 50_000_000
)

// trawlEnv: no MCP server, no man-in-the-middle proxy, no paid captcha services.
var trawlEnv = []string{"SESSION_CACHE_DRIVER=memory", "BROWSER_POOL_SIZE=1", "MCP_ENABLED=false", "MITM_ENABLED=false",
	"CAPTCHA_SOLVER=none", "METRICS_DASHBOARD_ENABLED=true", "LOG_LEVEL=info", "NODE_NO_WARNINGS=1"}

func trawlDockerEnv() map[string]string {
	env := map[string]string{"PORT": "8190", "METRICS_DB_PATH": "/data/trawl.sqlite"}
	for _, kv := range trawlEnv {
		k, v, _ := strings.Cut(kv, "=")
		env[k] = v
	}
	return env
}

func installTrawl(ctx context.Context, st *core.Stack, j *core.Job, dir string) (string, error) {
	bunDir := filepath.Join(st.Root, "apps", "bun")
	bun := filepath.Join(bunDir, "bun.exe")
	if _, err := os.Stat(bun); err != nil {
		zip := filepath.Join(st.Root, "downloads-cache", "bun.zip")
		if _, err := core.Download(ctx, j, "https://github.com/oven-sh/bun/releases/download/bun-v"+bunVersion+"/bun-windows-x64.zip", zip); err != nil {
			return "", err
		}
		if err := core.Unzip(zip, bunDir, 1); err != nil {
			return "", err
		}
		os.Remove(zip)
	}
	fresh := dir + ".new"
	if err := githubSource(ctx, st, j, "germondai/trawl", trawlVersion, fresh); err != nil {
		return "", err
	}
	// Upstream listens on every interface (fine inside Docker); natively that would expose an
	// unauthenticated scraper to the network. Bind to loopback unless HOST says otherwise.
	if err := patchFile(filepath.Join(fresh, "apps", "api", "src", "index.ts"), "createApiApp().listen(PORT)",
		`createApiApp().listen({ port: PORT, hostname: process.env.HOST || "127.0.0.1" })`); err != nil {
		return "", err
	}
	// Upstream picks fingerprints from the fonts a Linux image ships; a Windows browser must look like Windows.
	anchor := "  if (!installDir) return ALL_FINGERPRINTS\n"
	if err := patchFile(filepath.Join(fresh, "packages", "browser", "src", "fingerprint.ts"), anchor, anchor+
		"  if (process.platform === \"win32\") {\n"+
		"    const windows = ALL_FINGERPRINTS.filter((f) => f.platform === \"Win32\")\n"+
		"    if (windows.length) return windows\n"+
		"  }\n"); err != nil {
		return "", err
	}
	j.Info("Installing trawl's packages...")
	env := []string{"BUN_INSTALL_CACHE_DIR=" + filepath.Join(bunDir, "cache")}
	// --ignore-scripts: better-sqlite3 ships prebuilt Windows binaries but would still try to compile.
	if err := Run(ctx, j, fresh, env, bun, "install", "--frozen-lockfile", "--production", "--omit=dev", "--linker=hoisted", "--ignore-scripts"); err != nil {
		return "", err
	}
	// Keep the (large) browser across updates.
	camou := filepath.Join(dir, "camoufox")
	if _, err := os.Stat(camou); err == nil {
		if err := os.Rename(camou, filepath.Join(fresh, "camoufox")); err != nil {
			return "", err
		}
	}
	camou = filepath.Join(fresh, "camoufox")
	want := fmt.Sprintf(`{"version": "%s", "release": "%s"}`, camoufoxVer, camoufoxRel)
	if b, _ := os.ReadFile(filepath.Join(camou, "version.json")); strings.TrimSpace(string(b)) != want {
		os.RemoveAll(camou)
		zip := filepath.Join(st.Root, "downloads-cache", "camoufox.zip")
		url := fmt.Sprintf("https://github.com/daijro/camoufox/releases/download/%s/camoufox-%s-%s-win.x86_64.zip", camoufoxTag, camoufoxVer, camoufoxRel)
		if _, err := core.Download(ctx, j, url, zip); err != nil {
			return "", err
		}
		if err := core.Unzip(zip, camou, 0); err != nil {
			return "", err
		}
		os.Remove(zip)
		os.WriteFile(filepath.Join(camou, "version.json"), []byte(want), 0o644)
	}
	mmdb := filepath.Join(camou, "GeoLite2-City.mmdb")
	if fi, err := os.Stat(mmdb); err != nil || fi.Size() < geoLiteMinLen {
		if _, err := core.Download(ctx, j, geoLiteURL, mmdb); err != nil {
			return "", err
		}
		if fi, err := os.Stat(mmdb); err != nil || fi.Size() < geoLiteMinLen {
			return "", fmt.Errorf("GeoLite2-City.mmdb download looks truncated")
		}
	}
	ubo := filepath.Join(camou, "addons", "UBO")
	if _, err := os.Stat(filepath.Join(ubo, "manifest.json")); err != nil {
		xpi := filepath.Join(st.Root, "downloads-cache", "ubo.xpi")
		sum, err := core.Download(ctx, j, fmt.Sprintf("https://addons.mozilla.org/firefox/downloads/file/%s/ublock_origin-%s.xpi", uboAMOFileID, uboVersion), xpi)
		if err != nil {
			return "", err
		}
		if sum != uboSHA256 {
			return "", fmt.Errorf("uBlock Origin failed its checksum check")
		}
		if err := core.Unzip(xpi, ubo, 0); err != nil {
			return "", err
		}
		os.Remove(xpi)
	}
	if err := swapIn(fresh, dir); err != nil {
		return "", err
	}
	return trawlVersion, nil
}

func patchFile(file, old, repl string) error {
	b, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	if !strings.Contains(string(b), old) {
		return fmt.Errorf("%s changed upstream; the installer's patch needs updating", filepath.Base(file))
	}
	return os.WriteFile(file, []byte(strings.Replace(string(b), old, repl, 1)), 0o644)
}

// ---------------------------------------------------------------------------------------------- MediaSorter + Media Hub

// pythonRequirements for MediaSorter, the Media Hub and the health check.
var pythonRequirements = []string{"requests", "psutil", "flask", "waitress", "pyyaml", "guessit"}

// WritePythonApps copies the embedded MediaSorter / Media Hub sources to dest.
func WritePythonApps(dest string) error {
	return fs.WalkDir(assets.Python, "python", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(path, "python"), "/")
		target := filepath.Join(dest, filepath.FromSlash(rel))
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		b, err := assets.Python.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, b, 0o644)
	})
}

func installPythonApps(ctx context.Context, st *core.Stack, j *core.Job, _ string) (string, error) {
	if err := WritePythonApps(filepath.Join(st.Root, "apps", "mediastack-py")); err != nil {
		return "", err
	}
	venv := filepath.Join(st.Root, "apps", "python-env")
	py := filepath.Join(venv, "Scripts", "python.exe")
	uv, err := EnsureUV(ctx, st, j)
	if err != nil {
		return "", err
	}
	env := uvEnv(st)
	if _, err := os.Stat(py); err != nil {
		j.Info("Setting up Python...")
		if err := Run(ctx, j, st.Root, env, uv, "venv", "--python", "3.12", venv); err != nil {
			return "", err
		}
	}
	reqs := append([]string{"pip", "install", "--python", py}, pythonRequirements...)
	reqs = append(reqs, "win11toast")
	if err := Run(ctx, j, st.Root, env, uv, reqs...); err != nil {
		return "", err
	}
	return assets.Version, nil
}

// ---------------------------------------------------------------------------------------------- INI files

// Ini edits a Qt-style .ini (qBittorrent) keeping sections, keys and their order.
type Ini struct {
	file     string
	sections []string
	keys     map[string][]string
	vals     map[string]map[string]string
}

func ReadIni(file string) (*Ini, error) {
	ini := &Ini{file: file, keys: map[string][]string{}, vals: map[string]map[string]string{}}
	b, err := os.ReadFile(file)
	if os.IsNotExist(err) {
		return ini, nil
	}
	if err != nil {
		return nil, err
	}
	sec := ""
	for _, line := range strings.Split(strings.TrimPrefix(string(b), "\xef\xbb\xbf"), "\n") {
		line = strings.TrimRight(line, "\r")
		t := strings.TrimSpace(line)
		if strings.HasPrefix(t, "[") && strings.HasSuffix(t, "]") {
			sec = t[1 : len(t)-1]
			ini.section(sec)
			continue
		}
		if k, v, ok := strings.Cut(line, "="); ok && t != "" {
			ini.Set(sec, k, v)
		}
	}
	return ini, nil
}

func (i *Ini) section(s string) {
	if _, ok := i.vals[s]; !ok {
		i.sections = append(i.sections, s)
		i.vals[s] = map[string]string{}
	}
}

func (i *Ini) Get(sec, key string) string { return i.vals[sec][key] }

func (i *Ini) Set(sec, key, val string) {
	i.section(sec)
	if _, ok := i.vals[sec][key]; !ok {
		i.keys[sec] = append(i.keys[sec], key)
	}
	i.vals[sec][key] = val
}

// Write saves without a byte-order mark (Qt misreads the first section otherwise).
func (i *Ini) Write() error {
	var b strings.Builder
	secs := append([]string(nil), i.sections...)
	sort.SliceStable(secs, func(a, c int) bool { return secs[a] == "" && secs[c] != "" })
	for n, s := range secs {
		if n > 0 {
			b.WriteString("\n")
		}
		if s != "" {
			b.WriteString("[" + s + "]\n")
		}
		for _, k := range i.keys[s] {
			b.WriteString(k + "=" + i.vals[s][k] + "\n")
		}
	}
	return core.WriteFileAtomic(i.file, []byte(b.String()), 0o600)
}
