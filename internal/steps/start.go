package steps

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

// Defaults are command-line choices (a test install next to an existing stack uses an offset and name).
type Defaults struct {
	Root       string
	PortOffset int
	Name       string
}

func init() {
	step(&Step{ID: "welcome", Title: "Check this computer", Group: "Get started", Button: "Check",
		Fields: welcomeFields, Run: runWelcome})
	step(&Step{ID: "folders", Title: "Choose a folder", Group: "Get started", Button: "Use this folder",
		Needs: []string{"welcome"}, Fields: folderFields, Run: runFolders})
	step(&Step{ID: "account", Title: "Your admin login", Group: "Get started", Button: "Save",
		Needs: []string{"folders"}, Fields: accountFields, Run: runAccount})
}

// ---------------------------------------------------------------------------------------------- welcome

func welcomeFields(c *Ctx) []Field {
	if runtime.GOOS != "linux" {
		return nil
	}
	if di := platform.CheckDocker(); !di.Installed || !di.Compose {
		return []Field{{Name: "install_docker", Type: "checkbox", Default: "true",
			Label: "Install Docker for me (Docker's official script from get.docker.com)"}}
	}
	return nil
}

func runWelcome(c *Ctx) error {
	var problems []string
	c.J.Info("System: %s %s", runtime.GOOS, runtime.GOARCH)
	if runtime.GOARCH != "amd64" && runtime.GOARCH != "arm64" {
		problems = append(problems, "this computer's processor ("+runtime.GOARCH+") isn't supported - you need a 64-bit PC or server")
	}

	ctx, cancel := context.WithTimeout(c, 20*time.Second)
	if _, err := core.GetText(ctx, "https://api.github.com/zen"); err != nil {
		problems = append(problems, "no internet connection (couldn't reach github.com)")
	} else {
		c.J.OK("Internet connection works")
	}
	cancel()

	switch runtime.GOOS {
	case "windows":
		if _, err := platform.LookPath("winget.exe"); err != nil {
			problems = append(problems, `Windows' package installer is missing: install "App Installer" from the Microsoft Store`)
		} else {
			c.J.OK("Windows package installer (winget) found")
		}
		if platform.HasNvidia() {
			c.J.OK("NVIDIA graphics found - Jellyfin will use it to convert video for phones and TVs")
		}
	case "linux":
		if os.Geteuid() != 0 {
			problems = append(problems, "run this program as root: sudo ./"+filepath.Base(os.Args[0]))
			break
		}
		di := platform.CheckDocker()
		if (!di.Installed || !di.Compose) && c.In["install_docker"] == "true" {
			if err := platform.InstallDocker(c, c.J); err != nil {
				return err
			}
			di = platform.CheckDocker()
		} else if di.Installed && !di.Running {
			c.J.Info("Starting Docker...")
			platform.StartDocker(c, c.J)
			di = platform.CheckDocker()
		}
		switch {
		case !di.Installed:
			problems = append(problems, "Docker isn't installed - tick \"Install Docker for me\" or install it yourself (https://docs.docker.com/engine/install/)")
		case !di.Compose:
			problems = append(problems, "Docker Compose is missing - install the docker-compose-plugin package")
		case !di.Running:
			problems = append(problems, "Docker is installed but not running (try: sudo systemctl start docker)")
		default:
			c.J.OK("Docker %s with Compose", di.Version)
		}
		if _, err := os.Stat("/dev/net/tun"); err != nil {
			c.J.Warn("/dev/net/tun is missing - the VPN container needs it (on a VM/LXC, enable TUN for this machine)")
		}
	}

	// Ports: something else already using one would make that app fail to start.
	busy := []string{}
	for _, id := range core.SortedKeys(core.BasePorts) {
		port := core.BasePorts[id] + c.Def.PortOffset
		if c.St != nil && c.St.IsDeployed(id) {
			continue // ours
		}
		if !portFree(port) {
			busy = append(busy, fmt.Sprintf("%d (%s)", port, platform.Get(id).Name))
		}
	}
	if len(busy) > 0 {
		problems = append(problems, "these ports are already used by other programs: "+strings.Join(busy, ", ")+
			". Close those programs (or uninstall an older media server) and check again")
	} else {
		c.J.OK("All the ports the apps need are free")
	}
	if len(problems) > 0 {
		return core.UserAction(strings.Join(problems, "; "))
	}
	return nil
}

func portFree(port int) bool {
	l, err := net.Listen("tcp", ":"+strconv.Itoa(port))
	if err != nil {
		return false
	}
	l.Close()
	return true
}

// ---------------------------------------------------------------------------------------------- folders

func folderFields(c *Ctx) []Field {
	def := c.Def.Root
	if c.St != nil {
		def = c.St.Root
	}
	if def == "" {
		def = platform.DefaultRoot()
	}
	return []Field{{Name: "root", Label: "Folder for the media server", Type: "text", Default: def, Required: true,
		Help: "Movies, shows, downloads and every app's settings go in here. Pick a drive with lots of free space - 4K films are 40-80 GB each."}}
}

func runFolders(c *Ctx) error {
	root := strings.TrimSpace(c.In["root"])
	if root == "" || !filepath.IsAbs(root) {
		return core.UserAction("enter a full folder path, like " + platform.DefaultRoot())
	}
	root = filepath.Clean(root)
	if err := os.MkdirAll(root, 0o755); err != nil {
		return fmt.Errorf("can't create %s: %w", root, err)
	}
	st := c.St
	if st == nil || st.Root != root {
		if loaded, err := core.Load(root); err == nil {
			st = loaded
			c.J.Info("Found an earlier setup in %s - continuing it", root)
		} else {
			st = core.New(root)
		}
	}
	st.PortOffset = c.Def.PortOffset
	if c.Def.Name != "" {
		st.Name = c.Def.Name
	}
	if st.Docker() {
		st.UID, st.GID = platform.InvokingUser()
		st.TZ = platform.TimeZone()
		c.J.Info("Apps will run as user %d:%d, time zone %s", st.UID, st.GID, st.TZ)
	}
	if err := st.MakeFolders(); err != nil {
		return err
	}
	if err := platform.Chown(st, st.DataDir()); err != nil {
		return err
	}
	if free, err := platform.FreeSpace(root); err == nil {
		c.J.Info("Free space on that drive: %d GB", free>>30)
		if free < 100<<30 {
			c.J.Warn("Less than 100 GB free - fine for testing, but 4K movies need far more")
		}
	}
	if err := st.Save(); err != nil {
		return err
	}
	c.SetStack(st)
	c.J.OK("Library folders are in %s", filepath.Join(st.DataDir(), "media"))
	return nil
}

// ---------------------------------------------------------------------------------------------- account

func accountFields(c *Ctx) []Field {
	user := "admin"
	if c.St != nil && c.St.AdminUser != "" {
		user = c.St.AdminUser
	}
	return []Field{
		{Name: "user", Label: "Username", Type: "text", Default: user, Required: true},
		{Name: "pass", Label: "Password", Type: "password", Required: true, Help: "At least 8 characters."},
		{Name: "pass2", Label: "Same password again", Type: "password", Required: true},
	}
}

func runAccount(c *Ctx) error {
	user, pass := strings.TrimSpace(c.In["user"]), c.In["pass"]
	switch {
	case user == "" || strings.ContainsAny(user, " :"):
		return core.UserAction("choose a username without spaces or colons")
	case len(pass) < 8:
		return core.UserAction("use a password of at least 8 characters")
	case pass != c.In["pass2"]:
		return core.UserAction("the two passwords don't match")
	}
	if c.St.AdminUser != "" && c.St.AdminUser != user && c.St.IsDeployed("jellyfin") {
		return core.UserAction("Jellyfin was already set up with the username \"" + c.St.AdminUser + "\" - keep that name")
	}
	c.St.AdminUser, c.St.AdminPass = user, pass
	c.J.OK("Saved. Setup itself keeps the password only in memory; each app stores its own copy (most as a one-way hash).")
	return c.St.Save()
}
