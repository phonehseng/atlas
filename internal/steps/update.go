package steps

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"mediastack/assets"
	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "update", Title: "Update my stack", Group: "Maintenance", Button: "Update now",
		Fields: updateFields, Run: runUpdate})
}

// HasInstall reports whether this stack has installed apps (the update step is shown only then).
func HasInstall(st *core.Stack) bool { return st != nil && len(st.DeployedApps()) > 0 }

func updateFields(c *Ctx) []Field {
	f := []Field{{Name: "all", Type: "checkbox", Default: "false",
		Label: "Also update every app that follows its newest release (Jellyfin, Sonarr, Radarr, Prowlarr...)",
		Help:  "Only apps with a newer release are touched; each restarts once. Seerr, Bazarr and others built on this computer update by running their own step again."}}
	if c.St != nil && c.St.IsDeployed("marquee") {
		f = append(f, Field{Name: "marquee", Type: "checkbox", Default: "true",
			Label: "Update Atlas to the newest release (close Atlas first)"})
	}
	return f
}

func runUpdate(c *Ctx) error {
	st := c.St
	var changed, current []string
	note := func(list *[]string, format string, a ...any) {
		msg := fmt.Sprintf(format, a...)
		*list = append(*list, msg)
		c.J.OK("%s", msg)
	}

	m, merr := core.FetchManifest(c)
	if merr != nil {
		c.J.Warn("Couldn't read the update list (%v) - updating with what this program carries", merr)
	} else {
		if m.Installer != nil && core.VersionNewer(m.Installer.Version, assets.Version) {
			c.J.Warn("Media Stack Setup %s is available (this is %s). Use \"Update the installer\" at the top first: it carries the newest stack files.",
				m.Installer.Version, assets.Version)
		} else if m.StackAssets != nil && core.VersionNewer(m.StackAssets.Version, assets.Version) {
			c.J.Warn("Newer stack files (%s) exist than this program carries (%s): get the newest Media Stack Setup first.",
				m.StackAssets.Version, assets.Version)
		}
	}

	// 1. MediaSorter, the Media Hub, the health check and the backup: the files this program carries.
	var py []string
	for _, id := range []string{"mediasorter", "hub", "healthcheck", "backup"} {
		if st.IsDeployed(id) {
			py = append(py, id)
		}
	}
	if len(py) > 0 {
		diff := platform.PythonAppsChanged(filepath.Join(st.Root, "apps", "mediastack-py"))
		if len(diff) > 0 || st.DeployedVersion("mediasorter") != assets.Version && st.DeployedVersion("mediasorter") != "mediastack-py:"+assets.Version {
			c.J.Info("Updating MediaSorter / Media Hub files (%d changed): %s", len(diff), strings.Join(diff, ", "))
			if err := WriteAppConfig(st); err != nil {
				return err
			}
			for _, id := range py {
				var err error
				switch id {
				case "hub":
					err = c.Deploy("hub", "/api/me")
				case "mediasorter":
					err = c.Deploy("mediasorter", "")
				default:
					err = c.P.Deploy(c, st, c.J, id)
				}
				if err != nil {
					return fmt.Errorf("updating %s: %w", platform.Get(id).Name, err)
				}
			}
			note(&changed, "MediaSorter, the Media Hub and the health check now run the %s files (%d files changed)", assets.Version, len(diff))
		} else {
			note(&current, "MediaSorter and the Media Hub already run this version's files")
		}
	}

	// 2. Apps this program pins to a version.
	for _, id := range st.DeployedApps() {
		want, ok := platform.Pinned(st, id)
		if !ok {
			continue
		}
		have := st.DeployedVersion(id)
		if have == want {
			note(&current, "%s is at the pinned version %s", platform.Get(id).Name, want)
			continue
		}
		c.J.Info("Updating %s from %s to %s...", platform.Get(id).Name, have, want)
		if err := c.P.Deploy(c, st, c.J, id); err != nil {
			return fmt.Errorf("updating %s: %w", platform.Get(id).Name, err)
		}
		note(&changed, "%s: %s -> %s", platform.Get(id).Name, have, want)
	}

	// 3. Optionally, every app that follows its newest release.
	if c.In["all"] == "true" {
		for _, id := range st.DeployedApps() {
			if _, pinned := platform.Pinned(st, id); pinned || !platform.FollowsReleases(st, id) {
				continue
			}
			name := platform.Get(id).Name
			have := st.DeployedVersion(id)
			if !st.Docker() {
				latest, err := platform.LatestVersion(c, id)
				if err != nil {
					c.J.Warn("%s: couldn't check for a newer release (%v)", name, err)
					continue
				}
				if latest == have {
					note(&current, "%s %s is the newest release", name, have)
					continue
				}
			}
			if err := c.P.Deploy(c, st, c.J, id); err != nil {
				c.J.Warn("%s didn't update: %v", name, err)
				continue
			}
			if st.Docker() {
				note(&changed, "%s: pulled the newest %s", name, st.DeployedVersion(id))
			} else {
				note(&changed, "%s: %s -> %s", name, have, st.DeployedVersion(id))
			}
		}
	}

	// 4. Atlas (Marquee before the rename), from the update list. An install from before the rename moves
	// from apps\Marquee to apps\Atlas first and gets Atlas shortcuts (and firewall rule) at the end.
	if st.IsDeployed("marquee") && c.In["marquee"] != "false" {
		migrated, err := migrateMarquee(c)
		if err != nil {
			return err
		}
		if migrated {
			note(&changed, "Marquee is called Atlas now: moved to %s (settings kept)", MarqueeDir(st))
		}
		have := st.DeployedVersion("marquee")
		app := m.App()
		switch {
		case app == nil:
			c.J.Warn("Atlas: no release in the update list, left at %s", have)
		case !core.VersionNewer(app.Version, have):
			note(&current, "Atlas %s is the newest release", have)
		default:
			f, ok := app.File("windows-x64")
			if !ok {
				c.J.Warn("Atlas %s has no Windows download in the update list", app.Version)
				break
			}
			zip := filepath.Join(st.Root, "downloads-cache", "Atlas-windows-x64.zip")
			c.J.Info("Downloading Atlas %s...", app.Version)
			if err := core.FetchFile(c, c.J, m, f, zip); err != nil {
				return fmt.Errorf("Atlas update: %w", err)
			}
			err := installMarquee(c, zip)
			os.Remove(zip)
			if err != nil {
				return err
			}
			// Its settings stay exactly as they are: only the program files change.
			st.SetDeployed("marquee", app.Version)
			note(&changed, "Atlas: %s -> %s (settings kept)", have, app.Version)
		}
		if migrated {
			marqueeShortcuts(c)
			migrateMarqueeFirewall(c)
		}
	}

	// 5. Everything that should be running, running.
	if st.Docker() {
		if err := platform.Compose(c, st, c.J, "up", "--detach"); err != nil {
			c.J.Warn("docker compose up: %v", err)
		}
	} else {
		for _, id := range st.DeployedApps() {
			a := platform.Apps[id]
			if a == nil || a.Win == nil || (a.Win.Trigger != "" && a.Win.Trigger != "logon") {
				continue
			}
			if !platform.TaskRunning(st, id) {
				if err := c.P.Start(st, id); err != nil {
					c.J.Warn("Couldn't start %s: %v", a.Name, err)
				} else {
					note(&changed, "Started %s (it wasn't running)", a.Name)
				}
			}
		}
	}
	if st.IsDeployed("mediasorter") || st.IsDeployed("hub") {
		if err := WriteAppConfig(st); err != nil {
			return err
		}
	}
	st.SetOpt("updated_with", assets.Version)

	c.J.Info("Summary")
	if len(changed) == 0 {
		c.J.OK("Everything was already up to date. Nothing was changed.")
	}
	for _, s := range changed {
		c.J.OK("Updated: %s", s)
	}
	c.J.Info("Settings, logins, requests and media were not touched.")
	return nil
}
