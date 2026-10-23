package steps

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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
	f := []Field{{Name: "tailscale", Type: "checkbox", Default: "true",
		Label: "Set up Tailscale so your phone, TV and friends can reach the server from anywhere (free)"}}
	if !c.St.Docker() {
		f = append(f, Field{Name: "firewall", Type: "checkbox", Default: "true",
			Label: "Let phones and TVs connect through Windows Firewall (Jellyfin, Seerr, jfa-go, Media Hub)"})
	}
	return f
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

func runRemote(c *Ctx) error {
	st := c.St
	if c.In["firewall"] == "true" && !st.Docker() {
		var rules strings.Builder
		rules.WriteString("$ErrorActionPreference = 'Stop'\n")
		for _, r := range []struct {
			id, profile string
		}{{"jellyfin", "Any"}, {"seerr", "Any"}, {"jfago", "Any"}, {"hub", "Private"}} {
			if !st.IsDeployed(r.id) {
				continue
			}
			name := fmt.Sprintf("%s - %s", st.Name, platform.Get(r.id).Name)
			fmt.Fprintf(&rules, "Get-NetFirewallRule -DisplayName '%s' -ErrorAction SilentlyContinue | Remove-NetFirewallRule\n", name)
			fmt.Fprintf(&rules, "New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Protocol TCP -LocalPort %d -Action Allow -Profile %s | Out-Null\n",
				name, st.Port(r.id), r.profile)
		}
		if err := platform.RunElevated(c, c.J, rules.String()); err != nil {
			return err
		}
		c.J.OK("Windows Firewall lets phones and TVs reach Jellyfin, Seerr and jfa-go (and the Media Hub over Tailscale)")
	}
	if c.In["tailscale"] != "true" {
		return nil
	}
	ts := tailscaleExe()
	if ts == "" {
		if st.Docker() {
			c.J.Info("Installing Tailscale with its official script (tailscale.com/install.sh)...")
			if err := platform.Run(c, c.J, "/", nil, "sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"); err != nil {
				return err
			}
		} else if err := platform.Winget(c, c.J, "tailscale.tailscale"); err != nil {
			return err
		}
		time.Sleep(5 * time.Second)
		if ts = tailscaleExe(); ts == "" {
			return fmt.Errorf("Tailscale was installed but its program wasn't found")
		}
	}
	if !tailscaleOnline(ts) {
		c.J.Action("Sign in to Tailscale: open the link below (it appears in a few seconds), sign in or create a free account, then come back here.")
		args := []string{"up"}
		if !st.Docker() {
			args = append(args, "--unattended") // stay connected when you sign out of Windows
		}
		ctx, cancel := context.WithTimeout(c, 10*time.Minute)
		defer cancel()
		if err := platform.Run(ctx, c.J, "", nil, ts, args...); err != nil && !tailscaleOnline(ts) {
			return fmt.Errorf("Tailscale sign-in didn't finish: %w", err)
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
