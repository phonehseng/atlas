package core

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// ManifestURL is the update manifest published with every release (marquee-update.json).
const ManifestURL = "https://github.com/phonehseng/marquee/releases/latest/download/marquee-update.json"

// EnvManifest overrides ManifestURL: an http(s) address or a local file (for testing a release before it
// is published). A local manifest may also point at local files.
const EnvManifest = "MEDIASTACK_UPDATE_MANIFEST"

// ManifestFile is one download.
type ManifestFile struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// ManifestEntry is one product in the manifest. Files are keyed by platform ("windows-x64", "linux-x64",
// "linux-arm64"); an entry with a single download may give url/sha256/size directly instead.
type ManifestEntry struct {
	Version string                  `json:"version"`
	Notes   string                  `json:"notes,omitempty"`
	Files   map[string]ManifestFile `json:"files,omitempty"`
	ManifestFile
	// stack-assets: the oldest installer that carries these assets (informational).
	MinInstaller string `json:"min_installer,omitempty"`
}

// File returns the download for a platform key, if any.
func (e *ManifestEntry) File(key string) (ManifestFile, bool) {
	if e == nil {
		return ManifestFile{}, false
	}
	if f, ok := e.Files[key]; ok && f.URL != "" {
		return f, true
	}
	if e.URL != "" {
		return e.ManifestFile, true
	}
	return ManifestFile{}, false
}

// Manifest is marquee-update.json.
type Manifest struct {
	Schema      int            `json:"schema"`
	Marquee     *ManifestEntry `json:"marquee,omitempty"`
	Installer   *ManifestEntry `json:"installer,omitempty"`
	StackAssets *ManifestEntry `json:"stack-assets,omitempty"`
	// Local is true when the manifest came from a local file (EnvManifest); only then may it name local files.
	Local bool `json:"-"`
}

// PlatformKey is this program's platform in manifest terms.
func PlatformKey() string {
	arch := map[string]string{"amd64": "x64", "arm64": "arm64"}[runtime.GOARCH]
	if arch == "" {
		arch = runtime.GOARCH
	}
	return runtime.GOOS + "-" + arch
}

// FetchManifest reads the update manifest (EnvManifest, else ManifestURL).
func FetchManifest(ctx context.Context) (*Manifest, error) {
	src := os.Getenv(EnvManifest)
	if src == "" {
		src = ManifestURL
	}
	m := &Manifest{}
	if strings.HasPrefix(src, "http://") || strings.HasPrefix(src, "https://") {
		if err := GetJSON(ctx, src, m); err != nil {
			return nil, err
		}
	} else {
		b, err := os.ReadFile(src)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(bytes.TrimPrefix(b, []byte("\xef\xbb\xbf")), m); err != nil {
			return nil, fmt.Errorf("%s: %w", src, err)
		}
		m.Local = true
	}
	return m, nil
}

// FetchFile saves a manifest download to dest and checks its size and SHA-256 (both required). Local
// paths are allowed only for a local manifest.
func FetchFile(ctx context.Context, j *Job, m *Manifest, f ManifestFile, dest string) error {
	if f.SHA256 == "" {
		return fmt.Errorf("the update manifest gives no SHA-256 for %s", f.URL)
	}
	var sum string
	var err error
	switch {
	case strings.HasPrefix(f.URL, "https://") || strings.HasPrefix(f.URL, "http://") && m.Local:
		sum, err = Download(ctx, j, f.URL, dest)
	case m.Local:
		sum, err = CopyFile(f.URL, dest)
	default:
		return fmt.Errorf("refusing to download %s (not https)", f.URL)
	}
	if err != nil {
		return err
	}
	if fi, err := os.Stat(dest); err != nil || (f.Size > 0 && fi.Size() != f.Size) {
		os.Remove(dest)
		return fmt.Errorf("%s has the wrong size", f.URL)
	}
	if !strings.EqualFold(sum, f.SHA256) {
		os.Remove(dest)
		return fmt.Errorf("%s failed its checksum check", f.URL)
	}
	return nil
}

// CopyFile copies a local file and returns the copy's SHA-256.
func CopyFile(src, dest string) (string, error) {
	b, err := os.ReadFile(src)
	if err != nil {
		return "", err
	}
	if err := WriteFileAtomic(dest, b, 0o644); err != nil {
		return "", err
	}
	return SHA256File(dest)
}

// parseVersion reads "v1.2.3" / "1.2.3-test" as numbers (ok=false if it isn't a version).
func parseVersion(v string) ([]int, bool) {
	v = strings.TrimPrefix(strings.TrimSpace(v), "v")
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	if v == "" {
		return nil, false
	}
	var out []int
	for _, p := range strings.Split(v, ".") {
		n, err := strconv.Atoi(p)
		if err != nil {
			return nil, false
		}
		out = append(out, n)
	}
	return out, true
}

// VersionNewer reports whether version a is newer than b. Anything is newer than a b that isn't a
// version number (e.g. a local test build); nothing that isn't a version is newer.
func VersionNewer(a, b string) bool {
	va, ok := parseVersion(a)
	if !ok {
		return false
	}
	vb, ok := parseVersion(b)
	if !ok {
		return true
	}
	for i := 0; i < len(va) || i < len(vb); i++ {
		x, y := 0, 0
		if i < len(va) {
			x = va[i]
		}
		if i < len(vb) {
			y = vb[i]
		}
		if x != y {
			return x > y
		}
	}
	return false
}
