package platform

import (
	"bytes"
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"mediastack/assets"
	"mediastack/internal/core"
)

// PythonAppsChanged lists the embedded MediaSorter / Media Hub / health check files that differ from (or
// are missing in) an installed copy at dest.
func PythonAppsChanged(dest string) []string {
	var out []string
	fs.WalkDir(assets.Python, "python", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(path, "python"), "/")
		want, _ := assets.Python.ReadFile(path)
		have, err := os.ReadFile(filepath.Join(dest, filepath.FromSlash(rel)))
		if err != nil || !bytes.Equal(want, have) {
			out = append(out, rel)
		}
		return nil
	})
	return out
}

// Pinned is the version (Windows) or image (Linux) this installer pins an app to; ok=false when the app
// follows its newest release instead.
func Pinned(st *core.Stack, id string) (string, bool) {
	a := Apps[id]
	if a == nil {
		return "", false
	}
	if st.Docker() {
		if a.Docker == nil || a.Docker.Image == "" || strings.HasSuffix(a.Docker.Image, ":latest") || !strings.Contains(a.Docker.Image, ":") {
			return "", false
		}
		return a.Docker.Image, true
	}
	if id == "trawl" {
		return trawlVersion, true
	}
	return "", false
}

// FollowsReleases reports whether re-deploying an app fetches its newest release (and does nothing when
// it's already current), so an update can safely try it.
func FollowsReleases(st *core.Stack, id string) bool {
	a := Apps[id]
	if a == nil {
		return false
	}
	if st.Docker() {
		return a.Docker != nil && a.Docker.Build == "" && strings.HasSuffix(a.Docker.Image, ":latest")
	}
	return a.Win != nil && a.Win.Install == nil && a.Win.Winget == "" && (a.Win.Repo != "" || a.Win.Source != nil)
}

// LatestVersion is the newest release of an app that follows its releases (Windows), as download() would
// install it.
func LatestVersion(ctx context.Context, id string) (string, error) {
	r := Get(id).Win
	if r.Source != nil {
		_, _, v, err := r.Source(ctx)
		return v, err
	}
	rel, err := core.LatestRelease(ctx, r.Repo)
	if err != nil {
		return "", err
	}
	return rel.Tag, nil
}
