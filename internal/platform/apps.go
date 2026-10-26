// Package platform installs and runs the apps: natively with Task Scheduler on Windows, as Docker Compose
// services on Linux. The catalogue below says, for every app, where it comes from and how it runs on each.
package platform

import (
	"context"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"mediastack/internal/core"
)

// App is one program in the stack.
type App struct {
	ID, Name string
	Win      *WinRecipe
	Docker   *DockerSpec
	// Prepare writes settings before the first start. port is where the app itself listens (inside its
	// container on Linux). It must keep existing settings when an app is re-installed or updated.
	Prepare func(ctx context.Context, st *core.Stack, j *core.Job, port int) error
}

// WinRecipe is how an app is installed and run on Windows.
type WinRecipe struct {
	Repo, Asset string // GitHub repository and release-file pattern (a zip), or...
	// Source finds a download that isn't a GitHub release asset (returns file name, URL, version).
	Source func(ctx context.Context) (name, url, version string, err error)
	// Install replaces the zip download for apps built from source or needing a runtime.
	Install   func(ctx context.Context, st *core.Stack, j *core.Job, dir string) (version string, err error)
	Winget    string // installed system-wide with winget instead (asks for admin approval)
	WingetExe string // where the winget package puts the program

	Exe  string   // relative to the app folder (or absolute for winget apps)
	Args []string // placeholders: {app} program folder, {config} settings folder, {data}, {port}, {root}
	Env  []string // NAME=value, same placeholders
	// Visible apps (qBittorrent) run with their own window/tray icon instead of the hidden launcher.
	Visible bool
	// Keep lists folders inside the program folder that hold settings (apps that store them next to the
	// program); they're carried over when the app is updated.
	Keep    []string
	Trigger string // "logon" (default), "weekly", "daily" (3:30), "hourly", or "none" (installed, not scheduled)
}

// DockerSpec is an app's Docker Compose service (Linux).
type DockerSpec struct {
	Image   string
	Port    int      // container port published as the app's port ("" for none)
	Local   bool     // publish on 127.0.0.1 only (helpers nothing else should reach)
	Volumes []string // "{config}:/config", "{data}:/data", ...
	Env     map[string]string
	LSIO    bool // linuxserver.io image: takes PUID/PGID/TZ
	User    bool // run as the stack's uid:gid
	Extra   map[string]any
	Build   string // build context folder under the root (instead of Image)
	Command []string
}

// Apps is the catalogue, keyed by app id.
var Apps = map[string]*App{}

func add(a *App) *App { Apps[a.ID] = a; return a }

// Get returns an app from the catalogue.
func Get(id string) *App {
	a := Apps[id]
	if a == nil {
		panic("unknown app " + id)
	}
	return a
}

var servarrVolumes = []string{"{config}:/config", "{data}:/data"}

func init() {
	add(&App{ID: "jellyfin", Name: "Jellyfin",
		Win: &WinRecipe{Source: jellyfinSource, Exe: "jellyfin.exe",
			Args: []string{"--datadir", "{config}", "--cachedir", `{config}\cache`, "--logdir", `{config}\log`}},
		Docker: &DockerSpec{Image: "jellyfin/jellyfin:latest", Port: 8096, User: true,
			Volumes: []string{"{config}:/config", "{config}/cache:/cache", "{data}:/data"}},
		Prepare: prepareJellyfin})

	add(&App{ID: "qbittorrent", Name: "qBittorrent",
		Win: &WinRecipe{Winget: "qBittorrent.qBittorrent", WingetExe: `%ProgramFiles%\qBittorrent\qbittorrent.exe`,
			Args: []string{"--profile={config}"}, Visible: true},
		Docker: &DockerSpec{Image: "lscr.io/linuxserver/qbittorrent:latest", Port: 8080, LSIO: true,
			Volumes: servarrVolumes, Env: map[string]string{"WEBUI_PORT": "8080", "TORRENTING_PORT": "6881"}},
		Prepare: prepareQbittorrent})

	// -nosingleinstancecheck: Sonarr/Radarr/Prowlarr/Lidarr refuse to start while any other copy is running (another
	// install's too, matched by program name), so one install would stop the other starting after a reboot.
	add(&App{ID: "prowlarr", Name: "Prowlarr",
		Win: &WinRecipe{Repo: "Prowlarr/Prowlarr", Asset: `^Prowlarr\.\w+\.[\d.]+\.windows-core-x64\.zip$`,
			Exe: "Prowlarr.Console.exe", Args: []string{"-nobrowser", "-nosingleinstancecheck", "-data={config}"}},
		Docker:  &DockerSpec{Image: "lscr.io/linuxserver/prowlarr:latest", Port: 9696, LSIO: true, Volumes: []string{"{config}:/config"}},
		Prepare: servarrPrepare("Prowlarr", "master")})

	add(&App{ID: "sonarr", Name: "Sonarr",
		Win: &WinRecipe{Repo: "Sonarr/Sonarr", Asset: `^Sonarr\.main\.[\d.]+\.win-x64\.zip$`,
			Exe: "Sonarr.Console.exe", Args: []string{"-nobrowser", "-nosingleinstancecheck", "-data={config}"}},
		Docker:  &DockerSpec{Image: "lscr.io/linuxserver/sonarr:latest", Port: 8989, LSIO: true, Volumes: servarrVolumes},
		Prepare: servarrPrepare("Sonarr", "main")})

	add(&App{ID: "radarr", Name: "Radarr",
		Win: &WinRecipe{Repo: "Radarr/Radarr", Asset: `^Radarr\.master\.[\d.]+\.windows-core-x64\.zip$`,
			Exe: "Radarr.Console.exe", Args: []string{"-nobrowser", "-nosingleinstancecheck", "-data={config}"}},
		Docker:  &DockerSpec{Image: "lscr.io/linuxserver/radarr:latest", Port: 7878, LSIO: true, Volumes: servarrVolumes},
		Prepare: servarrPrepare("Radarr", "master")})

	add(&App{ID: "lidarr", Name: "Lidarr",
		Win: &WinRecipe{Repo: "Lidarr/Lidarr", Asset: `^Lidarr\.master\.[\d.]+\.windows-core-x64\.zip$`,
			Exe: "Lidarr.Console.exe", Args: []string{"-nobrowser", "-nosingleinstancecheck", "-data={config}"}},
		Docker:  &DockerSpec{Image: "lscr.io/linuxserver/lidarr:latest", Port: 8686, LSIO: true, Volumes: servarrVolumes},
		Prepare: servarrPrepare("Lidarr", "master")})

	add(&App{ID: "qui", Name: "Qui",
		Win: &WinRecipe{Repo: "autobrr/qui", Asset: `^qui_[\d.]+_windows_x86_64\.zip$`, Exe: "qui.exe",
			Args: []string{"serve", "--config-dir", "{config}"}},
		Docker:  &DockerSpec{Image: "ghcr.io/autobrr/qui:latest", Port: 7476, User: true, Volumes: []string{"{config}:/config"}},
		Prepare: prepareQui})

	add(&App{ID: "bazarr", Name: "Bazarr",
		Win: &WinRecipe{Install: installBazarr, Exe: `.venv\Scripts\python.exe`,
			Args: []string{`{app}\bazarr.py`, "--config", "{config}", "--port", "{port}", "--no-update"},
			Env:  []string{"PYTHONIOENCODING=utf-8"}},
		Docker: &DockerSpec{Image: "lscr.io/linuxserver/bazarr:latest", Port: 6767, LSIO: true, Volumes: servarrVolumes}})

	add(&App{ID: "seerr", Name: "Seerr",
		Win: &WinRecipe{Install: installSeerr, Exe: `{root}\apps\node\node.exe`, Args: []string{"dist/index.js"},
			Env: []string{"NODE_ENV=production", "PORT={port}", "CONFIG_DIRECTORY={config}"}},
		Docker: &DockerSpec{Image: "ghcr.io/seerr-team/seerr:latest", Port: 5055, User: true,
			Volumes: []string{"{config}:/app/config"}, Env: map[string]string{"LOG_LEVEL": "info"}, Extra: map[string]any{"init": true}}})

	add(&App{ID: "recyclarr", Name: "Recyclarr",
		Win: &WinRecipe{Repo: "recyclarr/recyclarr", Asset: `^recyclarr-win-x64\.zip$`, Exe: "recyclarr.exe",
			Args: []string{"sync"}, Env: []string{"RECYCLARR_CONFIG_DIR={config}"}, Trigger: "weekly"},
		Docker: &DockerSpec{Image: "ghcr.io/recyclarr/recyclarr:latest", User: true, Volumes: []string{"{config}:/config"},
			Env: map[string]string{"CRON_SCHEDULE": "@weekly"}}})

	add(&App{ID: "unpackerr", Name: "Unpackerr",
		Win: &WinRecipe{Repo: "Unpackerr/unpackerr", Asset: `^unpackerr\.amd64\.exe\.zip$`, Exe: "unpackerr.exe",
			Args: []string{"--config", `{config}\unpackerr.conf`}},
		Docker: &DockerSpec{Image: "golift/unpackerr:latest", User: true, Volumes: []string{"{config}:/config", "{data}:/data"},
			Command: []string{"--config", "/config/unpackerr.conf"}}})

	add(&App{ID: "cleanuparr", Name: "Cleanuparr",
		Win: &WinRecipe{Repo: "Cleanuparr/Cleanuparr", Asset: `^Cleanuparr-[\d.]+-win-amd64\.zip$`, Exe: "Cleanuparr.exe",
			Env: []string{"PORT={port}"}, Keep: []string{"config"}},
		Docker: &DockerSpec{Image: "ghcr.io/cleanuparr/cleanuparr:latest", Port: 11011, LSIO: true,
			Volumes: []string{"{config}:/config", "{data}:/data"}, Env: map[string]string{"PORT": "11011"}}})

	add(&App{ID: "autobrr", Name: "autobrr",
		Win: &WinRecipe{Repo: "autobrr/autobrr", Asset: `^autobrr_[\d.]+_windows_x86_64\.zip$`, Exe: "autobrr.exe",
			Args: []string{"--config", "{config}"}},
		Docker:  &DockerSpec{Image: "ghcr.io/autobrr/autobrr:latest", Port: 7474, User: true, Volumes: []string{"{config}:/config"}},
		Prepare: prepareAutobrr})

	add(&App{ID: "byparr", Name: "Byparr",
		Win: &WinRecipe{Install: installByparr, Exe: `.venv\Scripts\python.exe`, Args: []string{"main.py"},
			Env: []string{"HOST=127.0.0.1", "PORT={port}", "PYTHONIOENCODING=utf-8"}},
		Docker: &DockerSpec{Image: "ghcr.io/thephaseless/byparr:latest", Port: 8191, Local: true, Extra: map[string]any{"shm_size": "2gb"}}})

	add(&App{ID: "trawl", Name: "trawl",
		Win: &WinRecipe{Install: installTrawl, Exe: `{root}\apps\bun\bun.exe`, Args: []string{"run", "apps/api/src/index.ts"},
			Env: append([]string{"HOST=127.0.0.1", "PORT={port}", `CAMOUFOX_INSTALL_DIR={app}\camoufox`,
				`METRICS_DB_PATH={config}\trawl.sqlite`}, trawlEnv...)},
		Docker: &DockerSpec{Image: "ghcr.io/germondai/trawl:" + strings.TrimPrefix(trawlVersion, "v"), Port: 8190, Local: true,
			Volumes: []string{"{config}:/data"}, Env: trawlDockerEnv(), Extra: map[string]any{"shm_size": "2gb"}}})

	add(&App{ID: "jfago", Name: "jfa-go",
		Win: &WinRecipe{Repo: "hrfee/jfa-go", Asset: `^jfa-go_[\d.]+_Windows_x86_64\.zip$`, Exe: "jfa-go.exe",
			Args: []string{"-config", `{config}\config.ini`, "-data", "{config}", "-port", "{port}"}},
		Docker: &DockerSpec{Image: "hrfee/jfa-go:latest", Port: 8056, Volumes: []string{"{config}:/data"}}})

	add(&App{ID: "requestrr", Name: "Requestrr",
		Win: &WinRecipe{Repo: "thomst08/requestrr", Asset: `^requestrr-win-x64\.zip$`, Exe: "Requestrr.WebApi.exe",
			Keep: []string{"config"}},
		Docker:  &DockerSpec{Image: "thomst08/requestrr:latest", Port: 4545, Volumes: []string{"{config}:/root/config"}},
		Prepare: prepareRequestrr})

	add(&App{ID: "mediasorter", Name: "MediaSorter",
		Win: &WinRecipe{Install: installPythonApps, Exe: `{root}\apps\python-env\Scripts\python.exe`,
			Args: []string{`{root}\apps\mediastack-py\mediasorter\mediasorter.py`, "watch"},
			Env:  []string{"PYTHONIOENCODING=utf-8", `MEDIASTACK_CONFIG={root}\config\shared\mediastack.json`}},
		Docker: &DockerSpec{Build: "apps/mediastack-py", User: true,
			Volumes: []string{"{root}/config/shared:/shared:ro", "{config}:/state", "{data}:/data"},
			Env:     map[string]string{"MEDIASTACK_CONFIG": "/shared/mediastack.json", "MEDIASTACK_STATE": "/state"},
			Command: []string{"python", "/app/mediasorter/mediasorter.py", "watch"}}})

	add(&App{ID: "hub", Name: "Media Hub",
		Win: &WinRecipe{Install: installPythonApps, Exe: `{root}\apps\python-env\Scripts\python.exe`,
			Args: []string{`{root}\apps\mediastack-py\mediahub\hub.py`},
			Env:  []string{"PYTHONIOENCODING=utf-8", `MEDIASTACK_CONFIG={root}\config\shared\mediastack.json`}},
		Docker: &DockerSpec{Build: "apps/mediastack-py", Port: 8585,
			// /appconfig is writable for restoring backups (the hub makes them nightly into /backups).
			Volumes: []string{"{root}/config/shared:/shared:ro", "{config}:/state",
				"{data}:/data", "{root}/config:/appconfig", "{root}/backups:/backups", "{root}/config/mediasorter:/sorter",
				"/var/run/docker.sock:/var/run/docker.sock"},
			Env:     map[string]string{"MEDIASTACK_CONFIG": "/shared/mediastack.json", "MEDIASTACK_STATE": "/state"},
			Command: []string{"python", "/app/mediahub/hub.py"}}})

	add(&App{ID: "healthcheck", Name: "Health check",
		Win: &WinRecipe{Install: installPythonApps, Exe: `{root}\apps\python-env\Scripts\python.exe`,
			Args: []string{`{root}\apps\mediastack-py\mediasorter\healthcheck.py`}, Trigger: "hourly",
			Env: []string{"PYTHONIOENCODING=utf-8", `MEDIASTACK_CONFIG={root}\config\shared\mediastack.json`}}})

	// Nightly backup of every app's settings (Linux: the Media Hub container does it).
	add(&App{ID: "backup", Name: "Backup",
		Win: &WinRecipe{Install: installPythonApps, Exe: `{root}\apps\python-env\Scripts\python.exe`,
			Args: []string{`{root}\apps\mediastack-py\mediasorter\backup.py`}, Trigger: "daily",
			Env: []string{"PYTHONIOENCODING=utf-8", `MEDIASTACK_CONFIG={root}\config\shared\mediastack.json`}}})
}

// Expand fills the placeholders in a recipe string.
func Expand(st *core.Stack, appID, s string) string {
	r := strings.NewReplacer("{app}", st.AppDir(appID), "{config}", st.ConfigDir(appID), "{data}", st.DataDir(),
		"{root}", st.Root, "{port}", fmt.Sprint(st.Port(appID)))
	return os.ExpandEnv(r.Replace(s))
}

// ---------------------------------------------------------------------------------------------- settings files

// servarrPrepare writes Sonarr/Radarr/Prowlarr's config.xml with a known API key and port, keeping
// anything already there.
func servarrPrepare(instance, branch string) func(context.Context, *core.Stack, *core.Job, int) error {
	return func(_ context.Context, st *core.Stack, j *core.Job, port int) error {
		id := strings.ToLower(instance)
		file := filepath.Join(st.ConfigDir(id), "config.xml")
		conf, err := ReadFlatXML(file)
		if err != nil {
			return err
		}
		key := conf.Get("ApiKey")
		if key == "" {
			key = st.Key(id)
		}
		if key == "" {
			key = core.RandomHex(16)
		}
		// Windows: only this PC needs these admin pages (the Media Hub opens them here), and a loopback-only
		// app doesn't make Windows Firewall ask about it. Containers must listen on every interface.
		if st.Docker() {
			conf.Set("BindAddress", "*")
		} else {
			conf.Set("BindAddress", "127.0.0.1")
		}
		conf.Set("Port", fmt.Sprint(port))
		conf.Set("ApiKey", key)
		conf.Set("UrlBase", "")
		conf.Set("InstanceName", instance)
		conf.Set("LaunchBrowser", "False")
		conf.Set("AnalyticsEnabled", "False")
		conf.Set("LogLevel", "info")
		if conf.Get("Branch") == "" {
			conf.Set("Branch", branch)
		}
		if !st.Docker() {
			// The installer updates apps on Windows (re-run the step); a self-update would restart the app
			// outside its background task.
			conf.Set("UpdateAutomatically", "False")
		}
		if conf.Get("AuthenticationMethod") == "" {
			// Login is switched on by the installer once the app is running (it needs your password).
			conf.Set("AuthenticationMethod", "External")
			conf.Set("AuthenticationRequired", "DisabledForLocalAddresses")
		}
		if err := conf.Write(file); err != nil {
			return err
		}
		return st.SetKey(id, key)
	}
}

func prepareJellyfin(_ context.Context, st *core.Stack, _ *core.Job, port int) error {
	if port == 8096 {
		return nil // Jellyfin's default
	}
	file := filepath.Join(st.ConfigDir("jellyfin"), "config", "network.xml")
	if _, err := os.Stat(file); err == nil {
		return nil
	}
	x := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <InternalHttpPort>%d</InternalHttpPort>
  <PublicHttpPort>%d</PublicHttpPort>
  <EnableRemoteAccess>true</EnableRemoteAccess>
</NetworkConfiguration>
`, port, port)
	return core.WriteFileAtomic(file, []byte(x), 0o644)
}

func prepareAutobrr(_ context.Context, st *core.Stack, _ *core.Job, port int) error {
	file := filepath.Join(st.ConfigDir("autobrr"), "config.toml")
	if b, err := os.ReadFile(file); err == nil {
		// Keep the session secret; only make sure the port is right.
		s := regexp.MustCompile(`(?m)^port\s*=.*$`).ReplaceAllString(string(b), fmt.Sprintf("port = %d", port))
		return core.WriteFileAtomic(file, []byte(s), 0o600)
	}
	host := "127.0.0.1"
	if st.Docker() {
		host = "0.0.0.0"
	}
	conf := fmt.Sprintf(`# autobrr settings (written by Media Stack Setup)
host = "%s"
port = %d
baseUrl = "/"
logLevel = "INFO"
logMaxSize = 50
logMaxBackups = 3
checkForUpdates = false
sessionSecret = "%s"
`, host, port, core.RandomHex(16))
	return core.WriteFileAtomic(file, []byte(conf), 0o600)
}

// prepareQui writes Qui's config.toml: its port, and on Windows only this PC (keeps the session secret).
func prepareQui(_ context.Context, st *core.Stack, _ *core.Job, port int) error {
	file := filepath.Join(st.ConfigDir("qui"), "config.toml")
	if b, err := os.ReadFile(file); err == nil {
		s := regexp.MustCompile(`(?m)^port\s*=.*$`).ReplaceAllString(string(b), fmt.Sprintf("port = %d", port))
		return core.WriteFileAtomic(file, []byte(s), 0o600)
	}
	host := "127.0.0.1"
	if st.Docker() {
		host = "0.0.0.0"
	}
	conf := fmt.Sprintf(`# Qui settings (written by Media Stack Setup)
host = "%s"
port = %d
baseUrl = "/"
sessionSecret = "%s"
logLevel = "INFO"
checkForUpdates = false
`, host, port, core.RandomHex(32))
	return core.WriteFileAtomic(file, []byte(conf), 0o600)
}

// prepareRequestrr sets Requestrr's port in its settings (created from the template it ships with).
func prepareRequestrr(_ context.Context, st *core.Stack, _ *core.Job, port int) error {
	if st.Docker() {
		return nil // the container uses the default port
	}
	file := filepath.Join(st.AppDir("requestrr"), "config", "settings.json")
	b, err := os.ReadFile(file)
	if os.IsNotExist(err) {
		b, err = os.ReadFile(filepath.Join(st.AppDir("requestrr"), "SettingsTemplate.json"))
	}
	if err != nil {
		return err
	}
	var settings map[string]any
	if err := json.Unmarshal(b, &settings); err != nil {
		return fmt.Errorf("Requestrr's settings: %w", err)
	}
	settings["Port"] = port
	out, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return err
	}
	return core.WriteFileAtomic(file, out, 0o600)
}

// ---------------------------------------------------------------------------------------------- flat XML

// FlatXML is a <Config><Key>value</Key>...</Config> file (the Servarr apps' config.xml), kept in order.
type FlatXML struct {
	Root  string
	Keys  []string
	Value map[string]string
}

// ReadFlatXML reads a flat XML settings file; a missing file gives an empty <Config>.
func ReadFlatXML(file string) (*FlatXML, error) {
	f := &FlatXML{Root: "Config", Value: map[string]string{}}
	b, err := os.ReadFile(file)
	if os.IsNotExist(err) {
		return f, nil
	}
	if err != nil {
		return nil, err
	}
	dec := xml.NewDecoder(strings.NewReader(string(b)))
	depth := 0
	var cur string
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		switch t := tok.(type) {
		case xml.StartElement:
			depth++
			if depth == 1 {
				f.Root = t.Name.Local
			} else if depth == 2 {
				cur = t.Name.Local
				if _, ok := f.Value[cur]; !ok {
					f.Keys = append(f.Keys, cur)
				}
				f.Value[cur] = ""
			}
		case xml.CharData:
			if depth == 2 && cur != "" {
				f.Value[cur] += string(t)
			}
		case xml.EndElement:
			depth--
		}
	}
	return f, nil
}

func (f *FlatXML) Get(k string) string { return strings.TrimSpace(f.Value[k]) }

func (f *FlatXML) Set(k, v string) {
	if _, ok := f.Value[k]; !ok {
		f.Keys = append(f.Keys, k)
	}
	f.Value[k] = v
}

func (f *FlatXML) Write(file string) error {
	var b strings.Builder
	b.WriteString("<" + f.Root + ">\n")
	for _, k := range f.Keys {
		var esc strings.Builder
		xml.EscapeText(&esc, []byte(f.Value[k]))
		fmt.Fprintf(&b, "  <%s>%s</%s>\n", k, esc.String(), k)
	}
	b.WriteString("</" + f.Root + ">\n")
	return core.WriteFileAtomic(file, []byte(b.String()), 0o644)
}
