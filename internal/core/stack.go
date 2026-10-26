// Package core holds what every part of the installer shares: the saved install (Stack), where things
// live on disk, how apps reach each other, and the job log the wizard streams to the browser.
package core

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

// BasePorts are each app's usual port. Test installs next to an existing stack add PortOffset.
var BasePorts = map[string]int{
	"jellyfin": 8096, "qbittorrent": 8080, "prowlarr": 9696, "sonarr": 8989, "radarr": 7878,
	"bazarr": 6767, "seerr": 5055, "cleanuparr": 11011, "autobrr": 7474, "jfago": 8056,
	"requestrr": 4545, "byparr": 8191, "trawl": 8190, "hub": 8585, "lidarr": 8686, "qui": 7476,
}

// Libraries are the four media folders. Sonarr owns TV Shows + Anime, Radarr owns Movies + Anime Movies.
var Libraries = []struct{ Key, Folder string }{
	{"movies", "Movies"}, {"tv", "TV Shows"}, {"anime", "Anime"}, {"anime_movies", "Anime Movies"},
}

// DownloadCategories are qBittorrent categories: the first four are for manual downloads (sorted by
// MediaSorter), sonarr/radarr/lidarr are the ones Sonarr, Radarr and Lidarr use.
var DownloadCategories = []string{"movies", "tv", "anime", "other", "sonarr", "radarr", "lidarr"}

// StepState records how a wizard step ended.
type StepState struct {
	Status  string    `json:"status"` // done | skipped | failed
	At      time.Time `json:"at"`
	Message string    `json:"message,omitempty"`
}

// VPN describes how torrents are kept on the VPN.
type VPN struct {
	// Windows: "app" (a VPN app's network adapter, e.g. NordVPN's NordLynx) or "none".
	// Linux: "gluetun" (VPN container that qBittorrent runs inside) or "none".
	Mode     string `json:"mode"`
	Adapter  string `json:"adapter,omitempty"`  // Windows: adapter alias qBittorrent is bound to
	Provider string `json:"provider,omitempty"` // Linux: gluetun VPN_SERVICE_PROVIDER
	Country  string `json:"country,omitempty"`
}

// Stack is everything the installer has set up, saved as <root>/stack.json. It holds API keys (needed to
// wire apps together and by the Media Hub) but never passwords.
type Stack struct {
	Version    int                   `json:"version"`
	OS         string                `json:"os"`
	Root       string                `json:"root"`
	PortOffset int                   `json:"port_offset,omitempty"`
	Name       string                `json:"name"` // Task Scheduler folder (Windows) / Docker Compose project (Linux)
	AdminUser  string                `json:"admin_user,omitempty"`
	TZ         string                `json:"tz,omitempty"`
	UID        int                   `json:"uid,omitempty"`
	GID        int                   `json:"gid,omitempty"`
	VPN        VPN                   `json:"vpn"`
	Keys       map[string]string     `json:"keys"`
	Steps      map[string]*StepState `json:"steps"`
	Opts       map[string]string     `json:"opts"`     // small facts discovered on the way (GPU, versions...)
	Deployed   map[string]string     `json:"deployed"` // app id -> installed version / image
	// Parts are the components picked on "Choose your stack" (nil: an install from before that page, where
	// every step is shown).
	Parts []string `json:"parts,omitempty"`

	// AdminPass is the admin password while the wizard runs. It is never saved.
	AdminPass string `json:"-"`

	mu sync.Mutex
}

// New makes a fresh, unsaved stack.
func New(root string) *Stack {
	return &Stack{Version: 1, OS: runtime.GOOS, Root: filepath.Clean(root), Name: "Media Stack",
		Keys: map[string]string{}, Steps: map[string]*StepState{}, Opts: map[string]string{}, Deployed: map[string]string{}}
}

// File is where a stack rooted at root is saved.
func File(root string) string { return filepath.Join(root, "stack.json") }

// Load reads a saved stack.
func Load(root string) (*Stack, error) {
	b, err := os.ReadFile(File(root))
	if err != nil {
		return nil, err
	}
	st := New(root)
	if err := json.Unmarshal(b, st); err != nil {
		return nil, fmt.Errorf("%s is damaged: %w", File(root), err)
	}
	if st.Keys == nil {
		st.Keys = map[string]string{}
	}
	if st.Steps == nil {
		st.Steps = map[string]*StepState{}
	}
	if st.Opts == nil {
		st.Opts = map[string]string{}
	}
	if st.Deployed == nil {
		st.Deployed = map[string]string{}
	}
	return st, nil
}

// SetDeployed records that an app is installed (version or image) and saves.
func (st *Stack) SetDeployed(app, version string) error {
	st.mu.Lock()
	st.Deployed[app] = version
	st.mu.Unlock()
	return st.Save()
}

// IsDeployed reports whether an app has been installed.
func (st *Stack) IsDeployed(app string) bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	_, ok := st.Deployed[app]
	return ok
}

// DeployedVersion is the installed version/image of an app ("" if not installed).
func (st *Stack) DeployedVersion(app string) string {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.Deployed[app]
}

// Remove forgets an installed app and saves.
func (st *Stack) Remove(app string) error {
	st.mu.Lock()
	delete(st.Deployed, app)
	st.mu.Unlock()
	return st.Save()
}

// DeployedApps lists installed apps in order.
func (st *Stack) DeployedApps() []string {
	st.mu.Lock()
	defer st.mu.Unlock()
	return SortedKeys(st.Deployed)
}

// Save writes stack.json atomically, readable only by its owner where the OS supports that.
func (st *Stack) Save() error {
	st.mu.Lock()
	defer st.mu.Unlock()
	if err := os.MkdirAll(st.Root, 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	return WriteFileAtomic(File(st.Root), b, 0o600)
}

// SetStep records a step result and saves.
func (st *Stack) SetStep(id, status, msg string) error {
	st.mu.Lock()
	st.Steps[id] = &StepState{Status: status, At: time.Now(), Message: msg}
	st.mu.Unlock()
	return st.Save()
}

// StepResult returns a copy of a step's saved result (nil if it never ran).
func (st *Stack) StepResult(id string) *StepState {
	st.mu.Lock()
	defer st.mu.Unlock()
	if s := st.Steps[id]; s != nil {
		c := *s
		return &c
	}
	return nil
}

// Done reports whether a step finished successfully.
func (st *Stack) Done(id string) bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	s := st.Steps[id]
	return s != nil && s.Status == "done"
}

// Key returns an app's API key ("" if unknown).
func (st *Stack) Key(app string) string {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.Keys[app]
}

// SetKey stores an app's API key and saves.
func (st *Stack) SetKey(app, key string) error {
	st.mu.Lock()
	st.Keys[app] = key
	st.mu.Unlock()
	return st.Save()
}

// Opt / SetOpt keep small discovered facts.
func (st *Stack) Opt(k string) string {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.Opts[k]
}

func (st *Stack) SetOpt(k, v string) error {
	st.mu.Lock()
	st.Opts[k] = v
	st.mu.Unlock()
	return st.Save()
}

// HasParts reports whether the components were chosen on "Choose your stack".
func (st *Stack) HasParts() bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.Parts != nil
}

// Picked reports whether a component was chosen (false when nothing was chosen yet).
func (st *Stack) Picked(id string) bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, p := range st.Parts {
		if p == id {
			return true
		}
	}
	return false
}

// SetParts saves the chosen components.
func (st *Stack) SetParts(parts []string) error {
	st.mu.Lock()
	st.Parts = append([]string{}, parts...)
	sort.Strings(st.Parts)
	st.mu.Unlock()
	return st.Save()
}

// ---------------------------------------------------------------------------------------------- ports & URLs

// Port is where an app listens as seen from this computer.
func (st *Stack) Port(app string) int { return BasePorts[app] + st.PortOffset }

// Docker reports whether apps run as containers (Linux).
func (st *Stack) Docker() bool { return st.OS == "linux" }

// URL is how the installer (on this computer) reaches an app.
func (st *Stack) URL(app string) string { return fmt.Sprintf("http://127.0.0.1:%d", st.Port(app)) }

// Service is an app's Docker Compose service name. qBittorrent shares the VPN container's network, so
// other containers reach it through that container's name.
func (st *Stack) Service(app string) string {
	if app == "qbittorrent" && st.VPN.Mode == "gluetun" {
		return "gluetun"
	}
	return app
}

// AppHost / AppPort are how one app reaches another (container DNS name on Linux, loopback on Windows).
func (st *Stack) AppHost(app string) string {
	if st.Docker() {
		return st.Service(app)
	}
	return "127.0.0.1"
}

func (st *Stack) AppPort(app string) int {
	if st.Docker() {
		return BasePorts[app]
	}
	return st.Port(app)
}

// AppURL is the base URL other apps use for app.
func (st *Stack) AppURL(app string) string {
	return fmt.Sprintf("http://%s:%d", st.AppHost(app), st.AppPort(app))
}

// ---------------------------------------------------------------------------------------------- folders

// DataDir holds media + downloads together so imports can be hardlinks (instant, no extra space).
func (st *Stack) DataDir() string { return filepath.Join(st.Root, "data") }

// MediaDir is a library folder ("movies", "tv", "anime", "anime_movies") on this computer.
func (st *Stack) MediaDir(key string) string {
	for _, l := range Libraries {
		if l.Key == key {
			return filepath.Join(st.DataDir(), "media", l.Folder)
		}
	}
	panic("unknown library " + key)
}

func (st *Stack) DownloadsDir() string { return filepath.Join(st.DataDir(), "downloads") }
func (st *Stack) DropDir() string      { return filepath.Join(st.DataDir(), "drop") }
func (st *Stack) RecycleDir() string   { return filepath.Join(st.DataDir(), "media", ".recycle-bin") }

// ConfigDir is an app's settings/database folder.
func (st *Stack) ConfigDir(app string) string { return filepath.Join(st.Root, "config", app) }

// AppDir is where an app's program files go (Windows).
func (st *Stack) AppDir(app string) string { return filepath.Join(st.Root, "apps", app) }

// LogDir is where the background launcher writes each app's console output (Windows).
func (st *Stack) LogDir() string { return filepath.Join(st.Root, "logs") }

// InApp converts a path on this computer to the same path as an app sees it: unchanged on Windows,
// /data/... or /config inside containers on Linux.
func (st *Stack) InApp(app, hostPath string) string {
	if !st.Docker() {
		return hostPath
	}
	rel, err := filepath.Rel(st.DataDir(), hostPath)
	if err == nil && !strings.HasPrefix(rel, "..") {
		return filepath.ToSlash(filepath.Join("/data", rel))
	}
	rel, err = filepath.Rel(st.ConfigDir(app), hostPath)
	if err == nil && !strings.HasPrefix(rel, "..") {
		return filepath.ToSlash(filepath.Join("/config", rel))
	}
	return filepath.ToSlash(hostPath)
}

// MakeFolders creates the data layout.
func (st *Stack) MakeFolders() error {
	dirs := []string{st.DropDir(), st.RecycleDir(), filepath.Join(st.DownloadsDir(), "watch")}
	for _, l := range Libraries {
		dirs = append(dirs, st.MediaDir(l.Key))
	}
	for _, c := range DownloadCategories {
		dirs = append(dirs, filepath.Join(st.DownloadsDir(), "complete", c), filepath.Join(st.DownloadsDir(), "incomplete", c))
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0o775); err != nil {
			return err
		}
	}
	return nil
}

// ---------------------------------------------------------------------------------------------- helpers

// WriteFileAtomic replaces a file in one step so a crash never leaves it half-written.
func WriteFileAtomic(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, perm); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// RandomHex returns n random bytes as hex (API keys, secrets).
func RandomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// SortedKeys returns a map's keys in order.
func SortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ErrNeedsUser marks a step that can't continue until the user does something (shown as guidance, not a crash).
var ErrNeedsUser = errors.New("waiting for you")

// UserAction wraps guidance for the user as an error a step can return.
func UserAction(msg string) error { return fmt.Errorf("%w: %s", ErrNeedsUser, msg) }
