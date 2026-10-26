// Media Stack Setup: a guided installer for a complete home media server (Jellyfin, Seerr, Sonarr, Radarr,
// Prowlarr, qBittorrent behind a VPN, Bazarr, Recyclarr, Unpackerr, Cleanuparr, MediaSorter, Media Hub,
// Tailscale and optional extras). Natively on Windows, with Docker on Linux.
//
//	MediaStackSetup                 open the setup wizard in your browser
//	MediaStackSetup --root D:\Media use (or continue) an install in that folder
//	MediaStackSetup run ...         internal: the hidden launcher Windows' scheduled tasks use
package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"mediastack/assets"
	"mediastack/internal/platform"
	"mediastack/internal/steps"
	"mediastack/internal/wizard"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "run" {
		os.Exit(launcher(os.Args[2:]))
	}
	platform.ShowConsole()

	def := &steps.Defaults{}
	flag.StringVar(&def.Root, "root", "", "folder to install into (or continue)")
	flag.IntVar(&def.PortOffset, "port-offset", 0, "add this to every app's port (to test next to an existing install)")
	flag.StringVar(&def.Name, "name", "", `name of the install (Task Scheduler folder / Docker project), default "Media Stack"`)
	listen := flag.String("listen", "", "address for the setup page (default 127.0.0.1; 0.0.0.0 on a Linux server without a screen)")
	port := flag.Int("port", 8590, "port for the setup page")
	noBrowser := flag.Bool("no-browser", false, "don't open a browser")
	token := flag.String("token", "", "use this access key for the setup page instead of a random one (for automation)")
	version := flag.Bool("version", false, "print the version")
	flag.BoolVar(&def.Update, "update", false, "open \"Update my stack\" (update the installed apps and files)")
	flag.Parse()
	if *version {
		fmt.Println("Media Stack Setup", assets.Version)
		return
	}

	headless := runtime.GOOS == "linux" && os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == ""
	if *listen == "" && headless {
		*listen = "0.0.0.0"
	}
	// Started again while the wizard is still open: just bring its page back.
	if *token == "" && !headless {
		if url := runningSession(); url != "" {
			fmt.Printf("\n  Media Stack Setup is already running: %s\n", url)
			if !*noBrowser {
				openBrowser(url)
			}
			return
		}
	}
	if self, err := os.Executable(); err == nil {
		os.Remove(self + ".old") // left by a self-update
	}
	srv := wizard.New(def)
	if len(*token) >= 16 {
		srv.Token = *token
	} else if *token != "" {
		fail("--token must be at least 16 characters")
	}
	ln, err := wizard.Listen(*listen, *port)
	if err != nil {
		fail("Couldn't start the setup page: %v", err)
	}
	p := ln.Addr().(*net.TCPAddr).Port
	host := "127.0.0.1"
	if *listen == "0.0.0.0" {
		if ip := steps.LANAddress(); ip != "" {
			host = ip
		}
	}
	url := fmt.Sprintf("http://%s:%d/?t=%s", host, p, srv.Token)

	fmt.Printf("\n  Media Stack Setup %s\n\n", assets.Version)
	fmt.Printf("  The setup wizard is open in your browser. If it isn't, open this address:\n\n    %s\n\n", url)
	if *listen == "0.0.0.0" {
		fmt.Printf("  (Open it from any computer on your network. The address contains a one-time key - don't share it.)\n\n")
	}
	fmt.Printf("  Keep this window open while you use the wizard. Close it (or press Ctrl+C) when you're done.\n\n")

	if !*noBrowser && !headless {
		openBrowser(url)
	}
	server := &http.Server{Handler: srv.Handler()}
	go server.Serve(ln)
	if *token == "" {
		saveSession(url)
	}
	go func() {
		if v := srv.CheckUpdates(); v != "" {
			fmt.Printf("  Media Stack Setup %s is available: the wizard offers it at the top of the page.\n\n", v)
		}
	}()
	<-srv.Quit
	if *token == "" {
		os.Remove(sessionFile())
	}
	if srv.Restart() {
		server.Close()
		self, _ := os.Executable()
		fmt.Println("  Starting the new version...")
		cmd := exec.Command(self, os.Args[1:]...)
		if err := cmd.Start(); err != nil {
			fail("Couldn't start the new version (%v). Start it again yourself.", err)
		}
		return
	}
	fmt.Println("  Setup closed. Run this program again any time to continue or update.")
}

// sessionFile remembers the open wizard's address (with its key) for this user only.
func sessionFile() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(dir, "MediaStackSetup", "session.txt")
}

func saveSession(url string) {
	if f := sessionFile(); f != "" {
		os.MkdirAll(filepath.Dir(f), 0o700)
		os.WriteFile(f, []byte(url), 0o600)
	}
}

// runningSession returns the address of a wizard that is still open, or "".
func runningSession() string {
	b, err := os.ReadFile(sessionFile())
	if err != nil {
		return ""
	}
	url := strings.TrimSpace(string(b))
	i := strings.Index(url, "/?t=")
	if i < 0 {
		return ""
	}
	req, _ := http.NewRequest("GET", url[:i]+"/api/state", nil)
	req.Header.Set("X-Setup-Token", url[i+4:])
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		return ""
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return ""
	}
	return url
}

// launcher implements "run --log FILE [--env K=V]... -- EXE ARGS...".
func launcher(args []string) int {
	var logFile string
	var env []string
	for len(args) > 0 {
		switch args[0] {
		case "--log":
			logFile, args = args[1], args[2:]
		case "--env":
			env, args = append(env, args[1]), args[2:]
		case "--":
			args = args[1:]
			if len(args) == 0 || logFile == "" {
				return 2
			}
			return platform.RunLauncher(logFile, env, args[0], args[1:])
		default:
			return 2
		}
	}
	return 2
}

func openBrowser(url string) {
	if err := platform.OpenBrowser(url); err != nil {
		fmt.Println("  (Couldn't open a browser automatically.)")
	}
}

func fail(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "\n  "+format+"\n", a...)
	if runtime.GOOS == "windows" {
		fmt.Fprint(os.Stderr, "\n  Press Enter to close.")
		fmt.Scanln()
	}
	os.Exit(1)
}
