// Package steps is the wizard's script: every step installs one piece of the stack, wires it to the
// pieces already installed, and explains what to do next.
package steps

import (
	"context"
	"fmt"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

// Field is one input on a step's form.
type Field struct {
	Name     string   `json:"name"`
	Label    string   `json:"label"`
	Type     string   `json:"type"` // text | password | select | checkbox | info
	Help     string   `json:"help,omitempty"`
	Options  []Option `json:"options,omitempty"`
	Default  string   `json:"default,omitempty"`
	Required bool     `json:"required,omitempty"`
}

// Option is a select choice.
type Option struct {
	Value string `json:"value"`
	Label string `json:"label"`
}

// Link is a button shown with a finished step.
type Link struct {
	Label string `json:"label"`
	URL   string `json:"url"`
}

// Step is one page of the wizard.
type Step struct {
	ID, Title, Group string
	Optional         bool
	OS               string   // "" = both, "windows", "linux"
	Needs            []string // steps that must be finished first
	Password         bool     // needs the admin password (asked again if setup was restarted)
	Button           string   // label of the main button
	Fields           func(c *Ctx) []Field
	Run              func(c *Ctx) error
	Links            func(st *core.Stack) []Link
}

// Ctx is what a running step works with.
type Ctx struct {
	context.Context
	St  *core.Stack // nil until a folder is chosen
	P   platform.Platform
	J   *core.Job
	In  map[string]string
	Def *Defaults
	// SetStack hands a newly created/loaded stack back to the wizard.
	SetStack func(*core.Stack)
}

// Order is the wizard's sequence. Steps register themselves from several files, and Go doesn't promise
// which file's init runs first, so the order lives here.
var Order = []string{
	"welcome", "folders", "choose", "account",
	"jellyfin",
	"vpn", "qbittorrent", "qui",
	"prowlarr", "sonarr", "radarr", "lidarr", "recyclarr", "trawl", "byparr", "bazarr", "unpackerr", "cleanuparr",
	"seerr", "jfago", "requestrr", "autobrr",
	"mediasorter", "hub", "marquee",
	"remote", "finish", "update",
}

// All is the wizard, in order (sorted on first use).
var All []*Step

var sortOnce sync.Once

func step(s *Step) { All = append(All, s) }

func sorted() []*Step {
	sortOnce.Do(func() {
		pos := map[string]int{}
		for i, id := range Order {
			pos[id] = i
		}
		for _, s := range All {
			if _, ok := pos[s.ID]; !ok {
				panic("step " + s.ID + " is missing from steps.Order")
			}
		}
		sort.SliceStable(All, func(a, b int) bool { return pos[All[a].ID] < pos[All[b].ID] })
	})
	return All
}

// Find returns a step by id.
func Find(id string) *Step {
	for _, s := range sorted() {
		if s.ID == id {
			return s
		}
	}
	return nil
}

// For lists the steps that apply to an OS.
func For(goos string) []*Step {
	var out []*Step
	for _, s := range sorted() {
		if s.OS == "" || s.OS == goos {
			out = append(out, s)
		}
	}
	return out
}

// ---------------------------------------------------------------------------------------------- helpers

// Deploy installs an app and waits until its web page answers.
func (c *Ctx) Deploy(id, readyPath string) error {
	a := platform.Get(id)
	c.J.Info("Installing %s...", a.Name)
	if err := c.P.Deploy(c, c.St, c.J, id); err != nil {
		return err
	}
	if readyPath == "" {
		c.J.OK("%s is installed", a.Name)
		return nil
	}
	c.J.Info("Waiting for %s to start...", a.Name)
	if err := core.WaitHTTP(c, c.J, a.Name, c.St.URL(id)+readyPath, 4*time.Minute); err != nil {
		return err
	}
	c.J.OK("%s is running at %s", a.Name, c.St.URL(id))
	return nil
}

// Pass is the admin password (the wizard asks again if it was restarted).
func (c *Ctx) Pass() string { return c.St.AdminPass }

// Arr is a client for Sonarr/Radarr/Lidarr/Prowlarr's API.
func (c *Ctx) Arr(id string) *core.API {
	ver := "v3"
	if id == "prowlarr" || id == "lidarr" {
		ver = "v1"
	}
	return core.NewAPI(c.St.URL(id)+"/api/"+ver, map[string]string{"X-Api-Key": c.St.Key(id)})
}

// InApp is a path as app id sees it.
func (c *Ctx) InApp(id, p string) string { return c.St.InApp(id, p) }

// field sets a Servarr-style "fields" entry ([{name, value}]).
func field(obj core.M, name string, value any) {
	fields, _ := obj["fields"].([]any)
	for _, f := range fields {
		if m, ok := f.(map[string]any); ok && m["name"] == name {
			m["value"] = value
			return
		}
	}
	obj["fields"] = append(fields, map[string]any{"name": name, "value": value})
}

// schema picks an implementation from a Servarr /schema list.
func schema(list []core.M, impl string) (core.M, error) {
	for _, s := range list {
		if s["implementation"] == impl {
			return s, nil
		}
	}
	return nil, fmt.Errorf("%s isn't offered by this version", impl)
}

// update merges changes into a settings object and saves it.
func update(api *core.API, path string, changes core.M) error {
	var cur core.M
	if err := api.Get(path, &cur); err != nil {
		return err
	}
	for k, v := range changes {
		cur[k] = v
	}
	if id, ok := cur["id"]; ok {
		return api.Put(fmt.Sprintf("%s/%v", path, id), cur, nil)
	}
	return api.Put(path, cur, nil)
}

// hostNames are the names and addresses this computer answers to (for "no login on my network").
func hostNames() []string {
	names := []string{"localhost", "127.0.0.1"}
	if h, err := os.Hostname(); err == nil {
		names = append(names, strings.ToLower(h))
	}
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if ip, _, err := net.ParseCIDR(a.String()); err == nil && ip.To4() != nil && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() {
				names = append(names, ip.String())
			}
		}
	}
	return names
}

// LANAddress is this computer's address on the home network ("" if unknown).
func LANAddress() string {
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		n := strings.ToLower(ifc.Name)
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 ||
			strings.Contains(n, "nord") || strings.Contains(n, "tailscale") || strings.Contains(n, "vethernet") ||
			strings.Contains(n, "docker") || strings.HasPrefix(n, "br-") || strings.HasPrefix(n, "veth") ||
			strings.HasPrefix(n, "tun") || strings.HasPrefix(n, "wg") || strings.Contains(n, "vpn") {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			ip, _, err := net.ParseCIDR(a.String())
			if err != nil || ip.To4() == nil || !ip.IsPrivate() {
				continue
			}
			if ip4 := ip.To4(); ip4[0] == 100 && ip4[1]&0xC0 == 64 {
				continue // Tailscale's range
			}
			return ip.String()
		}
	}
	return ""
}

// OpenURL is the address a person on this computer (Windows) or the home network (Linux server) uses.
func OpenURL(st *core.Stack, id string) string {
	host := "localhost"
	if st.Docker() {
		if ip := LANAddress(); ip != "" {
			host = ip
		}
	}
	return fmt.Sprintf("http://%s:%d", host, st.Port(id))
}

func linkTo(st *core.Stack, id, label string) Link {
	if label == "" {
		label = "Open " + platform.Get(id).Name
	}
	return Link{Label: label, URL: OpenURL(st, id)}
}

func links(ids ...string) func(*core.Stack) []Link {
	return func(st *core.Stack) []Link {
		var out []Link
		for _, id := range ids {
			out = append(out, linkTo(st, id, ""))
		}
		return out
	}
}
