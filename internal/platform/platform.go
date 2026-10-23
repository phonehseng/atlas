package platform

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"mediastack/internal/core"
)

// Platform installs apps and runs them in the background.
type Platform interface {
	// Deploy installs (or updates) an app, writes its first settings and starts it.
	Deploy(ctx context.Context, st *core.Stack, j *core.Job, id string) error
	Start(st *core.Stack, id string) error
	Stop(st *core.Stack, id string) error
	Restart(st *core.Stack, id string) error
	// RunNow runs an app's job once and waits (Recyclarr sync, health check).
	RunNow(ctx context.Context, st *core.Stack, j *core.Job, id string) error
	// Remove stops an app and unregisters it (its files and settings stay).
	Remove(st *core.Stack, id string) error
}

// For returns the platform matching the stack.
func For(st *core.Stack) Platform {
	if st.Docker() {
		return &Docker{}
	}
	return &Windows{}
}

// download fetches an app's zip (GitHub release or custom source) and unpacks it into the app folder.
func download(ctx context.Context, st *core.Stack, j *core.Job, a *App) (string, error) {
	r := a.Win
	var name, url, version string
	var err error
	if r.Source != nil {
		name, url, version, err = r.Source(ctx)
	} else {
		var rel *core.Release
		rel, err = core.LatestRelease(ctx, r.Repo)
		if err == nil {
			version = rel.Tag
			name, url, err = rel.Asset(r.Asset)
		}
	}
	if err != nil {
		return "", err
	}
	if st.DeployedVersion(a.ID) == version && version != "" {
		if _, err := os.Stat(filepath.Join(st.AppDir(a.ID), r.Exe)); err == nil {
			j.Info("%s %s is already the newest version", a.Name, version)
			return version, nil
		}
	}
	j.Info("Getting %s %s", a.Name, version)
	zip := filepath.Join(st.Root, "downloads-cache", name)
	if _, err := core.Download(ctx, j, url, zip); err != nil {
		return "", err
	}
	defer os.Remove(zip)
	top, err := core.ZipTopFolder(zip)
	if err != nil {
		return "", fmt.Errorf("%s isn't a valid zip: %w", name, err)
	}
	strip := 0
	if top != "" {
		strip = 1
	}
	dir := st.AppDir(a.ID)
	fresh := dir + ".new"
	os.RemoveAll(fresh)
	if err := core.Unzip(zip, fresh, strip); err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(fresh, r.Exe)); err != nil {
		return "", fmt.Errorf("%s doesn't contain %s", name, r.Exe)
	}
	for _, k := range r.Keep {
		old := filepath.Join(dir, k)
		if _, err := os.Stat(old); err == nil {
			os.RemoveAll(filepath.Join(fresh, k))
			if err := os.Rename(old, filepath.Join(fresh, k)); err != nil {
				return "", fmt.Errorf("couldn't carry %s's settings over: %w", a.Name, err)
			}
		}
	}
	if err := swapIn(fresh, dir); err != nil {
		return "", err
	}
	return version, nil
}
