package steps

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "remote", Title: "Phones, TVs & away from home", Group: "Finish", Button: "Set up access",
		Needs: []string{"jellyfin"}, Fields: remoteFields, Run: runRemote})
	step(&Step{ID: "finish", Title: "All done", Group: "Finish", Button: "Finish",
		Needs: []string{"jellyfin"}, Run: runFinish,
		Links: func(st *core.Stack) []Link {
			out := []Link{}
			for _, id := range []string{"hub", "jellyfin", "seerr"} {
				if st.IsDeployed(id) {
					out = append(out, linkTo(st, id, ""))
				}
			}
			return out
		}})
}

func remoteFields(c *Ctx) []Field {
	f := []Field{{Name: "tailscale", Type: "checkbox", Default: fmt.Sprint(Chosen(c.St, "tailscale") || !c.St.HasParts()),
		Label: "Set up Tailscale so your phone, TV and friends can reach the server from anywhere (free)"}}
	if !c.St.Docker() {
		label := "Let phones and TVs connect through Windows Firewall (Jellyfin, Seerr, jfa-go, Media Hub)"
		if marqueeWanted(c.St) {
			label = "Let phones and TVs connect through Windows Firewall (Jellyfin, Seerr, jfa-go, Media Hub, and Marquee Mobile over Tailscale)"
		}
		f = append(f, Field{Name: "firewall", Type: "checkbox", Default: "true", Label: label})
	}
	return f
}

func marqueeWanted(st *core.Stack) bool {
	return st != nil && !st.Docker() && (st.IsDeployed("marquee") || Chosen(st, "marquee"))
}

func tailscaleExe() string {
	if p, err := exec.LookPath("tailscale"); err == nil {
		return p
	}
	if p := filepath.Join(os.Getenv("ProgramFiles"), "Tailscale", "tailscale.exe"); fileExists(p) {
		return p
	}
	return ""
}

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

// Tailscale for Windows, pinned: the official MSI from pkgs.tailscale.com/stable. Tailscale updates itself
// afterwards; a new pin only matters for new installs.
const (
	TailscaleVersion   = "1.104.2"
	TailscaleMSISHA256 = "7eff4abdcbcf592ac9bce28f2b9f0fd51f8fc09194c955fbe8253b67b531e7f5"
	TailscaleMSISize   = 38756352
)

// TailscaleMSIURL is the pinned installer's address.
func TailscaleMSIURL() string {
	return "https://pkgs.tailscale.com/stable/tailscale-setup-" + TailscaleVersion + "-amd64.msi"
}

// DownloadTailscaleMSI fetches the pinned MSI into dir and checks its size and SHA-256.
func DownloadTailscaleMSI(ctx context.Context, j *core.Job, dir string) (string, error) {
	file := filepath.Join(dir, filepath.Base(TailscaleMSIURL()))
	sum, err := core.Download(ctx, j, TailscaleMSIURL(), file)
	if err != nil {
		return "", err
	}
	if fi, err := os.Stat(file); err != nil || fi.Size() != TailscaleMSISize {
		os.Remove(file)
		return "", fmt.Errorf("the Tailscale installer has the wrong size")
	}
	if sum != TailscaleMSISHA256 {
		os.Remove(file)
		return "", fmt.Errorf("the Tailscale installer failed its checksum check (got %s)", sum)
	}
	return file, nil
}

// psQuote makes a PowerShell single-quoted string.
func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// TailscaleMSIScript installs the MSI silently (run elevated). 3010 = installed, restart pending.
func TailscaleMSIScript(msi, logFile string) string {
	args := fmt.Sprintf(`/i "%s" /qn /norestart /l*v "%s"`, msi, logFile)
	return fmt.Sprintf(`$p = Start-Process -FilePath msiexec.exe -ArgumentList %s -Wait -PassThru
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw ("Tailscale's installer failed with code " + $p.ExitCode + " (log: " + %s + ")") }
`, psQuote(args), psQuote(logFile))
}

// MarqueeRuleName is the Windows Firewall rule that lets phones reach Marquee Mobile over Tailscale.
func MarqueeRuleName(st *core.Stack) string {
	if st.Name != "Media Stack" {
		return "Marquee Mobile (Tailscale) (" + st.Name + ")"
	}
	return "Marquee Mobile (Tailscale)"
}

// MarqueeMobilePort is Marquee Mobile's port (Marquee's remote_port default).
const MarqueeMobilePort = 8737

// FirewallScript adds the inbound rules for phones and TVs (run elevated). Tailscale's network counts as
// Private, so Marquee Mobile's rule is Private only, and only for Marquee.exe.
func FirewallScript(st *core.Stack) string {
	var b strings.Builder
	rule := func(name string, port int, profile, program string) {
		fmt.Fprintf(&b, "Get-NetFirewallRule -DisplayName %s -ErrorAction SilentlyContinue | Remove-NetFirewallRule\n", psQuote(name))
		prog := ""
		if program != "" {
			prog = " -Program " + psQuote(program)
		}
		fmt.Fprintf(&b, "New-NetFirewallRule -DisplayName %s -Direction Inbound -Protocol TCP -LocalPort %d%s -Action Allow -Profile %s | Out-Null\n",
			psQuote(name), port, prog, profile)
	}
	for _, r := range []struct{ id, profile string }{{"jellyfin", "Any"}, {"seerr", "Any"}, {"jfago", "Any"}, {"hub", "Private"}} {
		if st.IsDeployed(r.id) {
			rule(fmt.Sprintf("%s - %s", st.Name, platform.Get(r.id).Name), st.Port(r.id), r.profile, "")
		}
	}
	if marqueeWanted(st) {
		rule(MarqueeRuleName(st), MarqueeMobilePort, "Private", MarqueeExe(st))
	}
	return b.String()
}

func runRemote(c *Ctx) error {
	st := c.St
	wantTS := c.In["tailscale"] == "true"
	ts := tailscaleExe()
	if wantTS && ts != "" {
		c.J.OK("Tailscale is already installed (%s) - not installing it again", ts)
	}
	// Windows: one permission prompt for everything that needs an administrator.
	var admin strings.Builder
	if wantTS && ts == "" && !st.Docker() {
		c.J.Info("Downloading Tailscale %s (the official installer from pkgs.tailscale.com)...", TailscaleVersion)
		msi, err := DownloadTailscaleMSI(c, c.J, filepath.Join(st.Root, "downloads-cache"))
		if err != nil {
			return err
		}
		defer os.Remove(msi)
		c.J.OK("Tailscale installer verified (SHA-256 %s)", TailscaleMSISHA256)
		admin.WriteString(TailscaleMSIScript(msi, filepath.Join(st.LogDir(), "tailscale-install.log")))
	}
	if c.In["firewall"] == "true" && !st.Docker() {
		admin.WriteString(FirewallScript(st))
	}
	if admin.Len() > 0 {
		os.MkdirAll(st.LogDir(), 0o755)
		if err := platform.RunElevated(c, c.J, "$ErrorActionPreference = 'Stop'\n"+admin.String()); err != nil {
			return err
		}
		if c.In["firewall"] == "true" {
			c.J.OK("Windows Firewall lets phones and TVs reach Jellyfin, Seerr and jfa-go (and the Media Hub over Tailscale)")
			if marqueeWanted(st) {
				c.J.OK("Windows Firewall rule %q: Marquee Mobile (port %d) on private networks, which includes Tailscale",
					MarqueeRuleName(st), MarqueeMobilePort)
			}
		}
	}
	if !wantTS {
		return nil
	}
	if ts == "" {
		if st.Docker() {
			if err := installTailscaleLinux(c); err != nil {
				return err
			}
		}
		for i := 0; i < 60 && ts == ""; i++ { // the MSI's files and service take a moment
			time.Sleep(time.Second)
			ts = tailscaleExe()
		}
		if ts == "" {
			return fmt.Errorf("Tailscale was installed but its program wasn't found")
		}
		c.J.OK("Tailscale is installed")
	}
	if !tailscaleOnline(ts) {
		if err := tailscaleSignIn(c, ts); err != nil {
			return err
		}
	}
	name, ip := tailscaleSelf(ts)
	if name == "" {
		return fmt.Errorf("Tailscale is installed but not connected")
	}
	st.SetOpt("tailscale_name", name)
	st.SetOpt("tailscale_ip", ip)
	c.J.OK("Tailscale connected: this server is \"%s\" (%s)", name, ip)
	if st.IsDeployed("hub") || st.IsDeployed("mediasorter") {
		WriteAppConfig(st)
	}
	if !st.Docker() && st.VPN.Mode == "app" && strings.Contains(strings.ToLower(st.VPN.Adapter), "nord") {
		c.J.Action("NordVPN: add tailscaled.exe to Split tunneling as well (see the guide below), or Tailscale can't sign in while NordVPN is connected.")
	}
	return nil
}

// installTailscaleLinux runs Tailscale's official install script on the server itself (it adds Tailscale's
// signed package repository, so the system's updates keep Tailscale current).
func installTailscaleLinux(c *Ctx) error {
	c.J.Info("Installing Tailscale with its official script (tailscale.com/install.sh)...")
	script := filepath.Join(c.St.Root, "downloads-cache", "tailscale-install.sh")
	if _, err := core.Download(c, c.J, "https://tailscale.com/install.sh", script); err != nil {
		return err
	}
	defer os.Remove(script)
	return platform.Run(c, c.J, "/", nil, "sh", script)
}

// tailscaleSignIn runs "tailscale up" and opens the sign-in page it prints. The user signs in there
// themselves; this program never sees their account.
func tailscaleSignIn(c *Ctx, ts string) error {
	c.J.Action("Sign in to Tailscale in the browser window that opens (or the link below), with Google, Microsoft, GitHub or a new free account. This step waits for you.")
	args := []string{"up"}
	if !c.St.Docker() {
		args = append(args, "--unattended") // stay connected when you sign out of Windows
	}
	ctx, cancel := context.WithTimeout(c, 10*time.Minute)
	defer cancel()
	w := &loginWatcher{j: c.J, out: c.J.Writer("  "), open: !c.St.Docker() || os.Getenv("DISPLAY") != ""}
	err := platform.RunTo(ctx, w, ts, args...)
	if err != nil && !tailscaleOnline(ts) {
		return fmt.Errorf("Tailscale sign-in didn't finish (%v). Sign in from the Tailscale icon, then run this step again", err)
	}
	return nil
}

var loginURL = regexp.MustCompile(`https://login\.tailscale\.com/\S+`)

// loginWatcher passes "tailscale up" output to the log and opens the sign-in link once.
type loginWatcher struct {
	j      *core.Job
	out    io.Writer
	open   bool
	opened bool
}

func (w *loginWatcher) Write(p []byte) (int, error) {
	if u := loginURL.Find(p); u != nil && !w.opened {
		w.opened = true
		if w.open && platform.OpenBrowser(string(u)) == nil {
			w.j.Action("Sign in to Tailscale in the browser window: %s", u)
		} else {
			w.j.Action("Open this link to sign in to Tailscale: %s", u)
		}
	}
	return w.out.Write(p)
}

func tailscaleOnline(ts string) bool {
	name, _ := tailscaleSelf(ts)
	return name != ""
}

func tailscaleSelf(ts string) (string, string) {
	cmd := exec.Command(ts, "status", "--json")
	out, err := cmd.Output()
	if err != nil {
		return "", ""
	}
	var s struct {
		BackendState string
		Self         struct {
			DNSName      string
			TailscaleIPs []string
			Online       bool
		}
	}
	if json.Unmarshal(out, &s) != nil || s.BackendState != "Running" {
		return "", ""
	}
	name := strings.SplitN(s.Self.DNSName, ".", 2)[0]
	ip := ""
	if len(s.Self.TailscaleIPs) > 0 {
		ip = s.Self.TailscaleIPs[0]
	}
	return name, ip
}

// ---------------------------------------------------------------------------------------------- finish

func runFinish(c *Ctx) error {
	st := c.St
	if st.IsDeployed("hub") || st.IsDeployed("mediasorter") {
		if err := WriteAppConfig(st); err != nil {
			return err
		}
	}
	for _, a := range Addresses(st) {
		line := fmt.Sprintf("%s: %s", a.Name, a.Here)
		if a.Remote != "" {
			line += "   (away from home: " + a.Remote + ")"
		}
		c.J.OK("%s", line)
	}
	return nil
}

// Address is one row of the "where to open things" table.
type Address struct {
	Name   string `json:"name"`
	Use    string `json:"use"`
	Here   string `json:"here"`
	Remote string `json:"remote,omitempty"`
}

// Addresses lists what to type on a phone/TV/browser for each app people use.
func Addresses(st *core.Stack) []Address {
	var out []Address
	ts := st.Opt("tailscale_name")
	for _, r := range []struct{ id, use string }{
		{"jellyfin", "Watch - server address for the Jellyfin app on phones and TVs"},
		{"seerr", "Request movies and shows"},
		{"hub", "Dashboard: status, problems, requests, logs"},
		{"jfago", "Invite friends and family"},
	} {
		if !st.IsDeployed(r.id) {
			continue
		}
		a := Address{Name: platform.Get(r.id).Name, Use: r.use, Here: OpenURL(st, r.id)}
		if !st.Docker() && r.id != "hub" {
			if ip := LANAddress(); ip != "" {
				a.Here = fmt.Sprintf("http://%s:%d", ip, st.Port(r.id))
			}
		}
		if ts != "" {
			a.Remote = fmt.Sprintf("http://%s:%d", ts, st.Port(r.id))
		}
		out = append(out, a)
	}
	return out
}
