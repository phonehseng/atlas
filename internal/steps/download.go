package steps

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/url"
	"path/filepath"
	"strings"
	"time"

	"mediastack/internal/core"
	"mediastack/internal/platform"
)

func init() {
	step(&Step{ID: "vpn", Title: "VPN for downloads", Group: "Download", Button: "Save VPN choice",
		Needs: []string{"account"}, Fields: vpnFields, Run: runVPN})
	step(&Step{ID: "qbittorrent", Title: "qBittorrent", Group: "Download", Button: "Install qBittorrent",
		Needs: []string{"vpn"}, Password: true, Run: runQbittorrent,
		Links: func(st *core.Stack) []Link { return []Link{linkTo(st, "qbittorrent", "Open qBittorrent's web page")} }})
	step(&Step{ID: "qui", Title: "Qui (torrents & seeding rules)", Group: "Download", Optional: true, Button: "Install Qui",
		Needs: []string{"qbittorrent"}, Password: true, Run: runQui, Links: links("qui")})
}

// runQui installs Qui, a faster qBittorrent interface with per-tracker seeding rules, and connects it.
func runQui(c *Ctx) error {
	st := c.St
	if err := c.Deploy("qui", "/api/auth/check-setup"); err != nil {
		return err
	}
	q := core.NewAPI(st.URL("qui")+"/api", map[string]string{"X-Requested-With": "XMLHttpRequest"}) // Qui's CSRF check
	var setup struct {
		SetupRequired bool `json:"setupRequired"`
	}
	if err := q.Get("/auth/check-setup", &setup); err != nil {
		return err
	}
	creds := core.M{"username": st.AdminUser, "password": c.Pass()}
	if setup.SetupRequired {
		if err := q.Post("/auth/setup", creds, nil); err != nil {
			return fmt.Errorf("creating Qui's login: %w", err)
		}
	}
	if err := q.Post("/auth/login", creds, nil); err != nil {
		return core.UserAction("Qui didn't accept the admin login (" + err.Error() + ")")
	}
	var instances []struct{ Name string }
	if err := q.Get("/instances", &instances); err != nil {
		return err
	}
	for _, i := range instances {
		if i.Name == "qBittorrent" {
			c.J.OK("Qui already manages qBittorrent")
			return nil
		}
	}
	user, pass := "", ""
	if st.Docker() {
		user, pass = st.AdminUser, c.Pass() // containers can't use qBittorrent's "no login from this computer"
	}
	if err := q.Post("/instances", core.M{"name": "qBittorrent", "host": st.AppURL("qbittorrent"),
		"username": user, "password": pass}, nil); err != nil {
		return fmt.Errorf("connecting Qui to qBittorrent: %w", err)
	}
	c.J.OK("Qui manages qBittorrent (sign in with your admin login). Seeding rules per tracker: Qui > Automations")
	return nil
}

// ---------------------------------------------------------------------------------------------- VPN

// gluetun's provider names (VPN_SERVICE_PROVIDER).
var vpnProviders = []Option{
	{"nordvpn", "NordVPN"}, {"protonvpn", "Proton VPN"}, {"mullvad", "Mullvad"}, {"surfshark", "Surfshark"},
	{"private internet access", "Private Internet Access"}, {"expressvpn", "ExpressVPN"}, {"windscribe", "Windscribe"},
	{"ivpn", "IVPN"}, {"airvpn", "AirVPN"}, {"cyberghost", "CyberGhost"}, {"ipvanish", "IPVanish"},
	{"purevpn", "PureVPN"}, {"torguard", "TorGuard"}, {"vyprvpn", "VyprVPN"},
}

func vpnFields(c *Ctx) []Field {
	if !c.St.Docker() {
		var opts []Option
		def := ""
		for _, a := range adapters() {
			opts = append(opts, Option{a.Name, fmt.Sprintf("%s (%s)", a.Name, a.IP)})
			if def == "" && looksLikeVPN(a.Name) {
				def = a.Name
			}
		}
		if c.St.VPN.Adapter != "" {
			def = c.St.VPN.Adapter
		}
		return []Field{
			{Name: "mode", Label: "Protect downloads with", Type: "select", Default: "app", Options: []Option{
				{"app", "My VPN app on this PC (NordVPN, Proton VPN, Mullvad...)"}, {"none", "No VPN (not recommended)"}}},
			{Name: "adapter", Label: "Your VPN's network connection", Type: "select", Default: def, Options: opts,
				Help: "Connect your VPN app first, then press Refresh if it isn't listed. NordVPN's is called NordLynx."},
		}
	}
	return []Field{
		{Name: "mode", Label: "Protect downloads with", Type: "select", Default: "gluetun", Options: []Option{
			{"gluetun", "A VPN subscription (recommended)"}, {"none", "No VPN (not recommended)"}}},
		{Name: "provider", Label: "VPN provider", Type: "select", Default: "nordvpn", Options: vpnProviders},
		{Name: "type", Label: "Connection type", Type: "select", Default: "openvpn", Options: []Option{
			{"openvpn", "OpenVPN (username + password)"}, {"wireguard", "WireGuard (private key)"}}},
		{Name: "user", Label: "VPN service username", Type: "text",
			Help: "Not your website login - see the guide below for where your provider shows these."},
		{Name: "pass", Label: "VPN service password", Type: "password"},
		{Name: "wgkey", Label: "WireGuard private key", Type: "password"},
		{Name: "wgaddr", Label: "WireGuard address (only if your provider gives one, e.g. 10.64.222.21/32)", Type: "text"},
		{Name: "country", Label: "Server country (optional, e.g. Netherlands)", Type: "text"},
	}
}

type adapter struct{ Name, IP string }

func adapters() []adapter {
	var out []adapter
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if ip, _, err := net.ParseCIDR(a.String()); err == nil && ip.To4() != nil && !ip.IsLinkLocalUnicast() {
				out = append(out, adapter{ifc.Name, ip.String()})
				break
			}
		}
	}
	return out
}

func looksLikeVPN(name string) bool {
	n := strings.ToLower(name)
	for _, k := range []string{"nordlynx", "nordvpn", "proton", "mullvad", "wireguard", "wg", "tap-", "tun", "vpn", "surfshark", "express", "windscribe", "pia"} {
		if strings.Contains(n, k) && !strings.Contains(n, "tailscale") {
			return true
		}
	}
	return false
}

func adapterIP(name string) string {
	for _, a := range adapters() {
		if a.Name == name {
			return a.IP
		}
	}
	return ""
}

func runVPN(c *Ctx) error {
	st := c.St
	mode := c.In["mode"]
	if mode == "none" {
		st.VPN = core.VPN{Mode: "none"}
		c.J.Warn("No VPN: your internet provider (and anyone watching the torrent swarm) will see what you download.")
		return st.Save()
	}
	if !st.Docker() {
		name := c.In["adapter"]
		if name == "" || adapterIP(name) == "" {
			return core.UserAction("connect your VPN app, press Refresh, and pick its connection from the list")
		}
		if strings.Contains(strings.ToLower(name), "tailscale") {
			return core.UserAction("that's Tailscale (remote access), not a VPN for downloads - pick your VPN's connection")
		}
		id, err := platform.AdapterID(name)
		if err != nil {
			return err
		}
		st.VPN = core.VPN{Mode: "app", Adapter: name}
		c.J.OK("Downloads will only use %s (%s, %s). If the VPN disconnects, downloads stop instead of leaking.", name, adapterIP(name), id)
		return st.Save()
	}

	// Linux: Gluetun container.
	vt := c.In["type"]
	sec := platform.VPNSecrets{User: strings.TrimSpace(c.In["user"]), Password: c.In["pass"],
		PrivateKey: strings.TrimSpace(c.In["wgkey"]), Addresses: strings.TrimSpace(c.In["wgaddr"])}
	if vt == "wireguard" && sec.PrivateKey == "" {
		return core.UserAction("paste your WireGuard private key")
	}
	if vt != "wireguard" && (sec.User == "" || sec.Password == "") {
		return core.UserAction("enter your VPN service username and password")
	}
	st.VPN = core.VPN{Mode: "gluetun", Provider: c.In["provider"], Country: strings.TrimSpace(c.In["country"])}
	st.SetOpt("vpn_type", vt)
	if sec.Addresses != "" {
		st.SetOpt("vpn_wg_addresses", "1")
	} else {
		st.SetOpt("vpn_wg_addresses", "")
	}
	if err := platform.WriteVPNSecrets(st, sec); err != nil {
		return err
	}
	if err := c.Deploy("gluetun", ""); err != nil {
		return err
	}
	c.J.Info("Connecting to %s...", st.VPN.Provider)
	deadline := time.Now().Add(3 * time.Minute)
	for {
		h := platform.ServiceHealth(st, "gluetun")
		if h == "healthy" {
			break
		}
		if time.Now().After(deadline) {
			c.J.Info("Recent VPN log:")
			platform.Compose(c, st, c.J, "logs", "--tail", "25", "gluetun")
			return fmt.Errorf("the VPN didn't connect within 3 minutes (status %q) - check the username/password and the log above", h)
		}
		time.Sleep(5 * time.Second)
	}
	if ip := platform.GluetunPublicIP(st); ip != "" {
		c.J.OK("VPN connected - downloads will appear to come from %s", ip)
	} else {
		c.J.OK("VPN connected")
	}
	// If qBittorrent is already installed, recreate it so it joins the (new) VPN container.
	if st.IsDeployed("qbittorrent") {
		platform.WriteCompose(st)
		platform.Compose(c, st, c.J, "up", "--detach", "--force-recreate", "qbittorrent")
	}
	return st.Save()
}

// ---------------------------------------------------------------------------------------------- qBittorrent

// Qbt is a client for qBittorrent's Web API (signed in on Linux; on Windows this PC needs no login).
func Qbt(st *core.Stack) (*core.API, error) {
	api := core.NewAPI(st.URL("qbittorrent")+"/api/v2", map[string]string{"Referer": st.URL("qbittorrent")})
	if !st.Docker() {
		return api, nil // Windows: programs on this PC are let in without a login
	}
	resp, err := api.Client.PostForm(api.Base+"/auth/login", url.Values{"username": {st.AdminUser}, "password": {st.AdminPass}})
	if err != nil {
		return nil, err
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
	resp.Body.Close()
	if resp.StatusCode != 200 || !strings.HasPrefix(strings.TrimSpace(string(body)), "Ok") {
		return nil, fmt.Errorf("qBittorrent didn't accept the admin login (HTTP %d %s)", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return api, nil
}

func qbtPrefs(api *core.API, prefs core.M) error {
	b, _ := json.Marshal(prefs)
	return api.Post("/app/setPreferences", url.Values{"json": {string(b)}}, nil)
}

func runQbittorrent(c *Ctx) error {
	st := c.St
	if st.Docker() && st.VPN.Mode == "gluetun" && !st.IsDeployed("gluetun") {
		return core.UserAction("finish the VPN step first")
	}
	if err := c.Deploy("qbittorrent", "/api/v2/app/version"); err != nil {
		return err
	}
	api, err := Qbt(st)
	if err != nil {
		return err
	}
	in := func(p string) string { return c.InApp("qbittorrent", p) }
	dl := st.DownloadsDir()
	prefs := core.M{
		"web_ui_username": st.AdminUser, "web_ui_password": c.Pass(),
		"save_path": in(filepath.Join(dl, "complete", "other")), "temp_path_enabled": true, "temp_path": in(filepath.Join(dl, "incomplete")),
		"auto_tmm_enabled": true, "torrent_changed_tmm_enabled": true, "save_path_changed_tmm_enabled": true, "category_changed_tmm_enabled": true,
		// Seeding: stop at ratio 1.0, after 12 hours, or after 1 hour with nobody downloading.
		"max_ratio_enabled": true, "max_ratio": 1, "max_seeding_time_enabled": true, "max_seeding_time": 720,
		"max_inactive_seeding_time_enabled": true, "max_inactive_seeding_time": 60, "max_ratio_act": 0,
		"queueing_enabled": true, "max_active_downloads": 10, "max_active_torrents": 15, "max_active_uploads": 5,
		"dont_count_slow_torrents": true,
		"scan_dirs":                core.M{in(filepath.Join(dl, "watch")): 1},
		"upnp":                     false, "file_log_enabled": true,
	}
	if c.Pass() == "" {
		delete(prefs, "web_ui_username")
		delete(prefs, "web_ui_password")
	}
	if !st.Docker() && st.VPN.Mode == "app" {
		id, err := platform.AdapterID(st.VPN.Adapter)
		if err != nil {
			return err
		}
		prefs["current_network_interface"] = id
		prefs["current_interface_address"] = adapterIP(st.VPN.Adapter)
		c.J.Info("Binding qBittorrent to %s", st.VPN.Adapter)
	}
	if err := qbtPrefs(api, prefs); err != nil {
		return fmt.Errorf("couldn't save qBittorrent settings: %w", err)
	}
	for _, cat := range core.DownloadCategories {
		form := url.Values{"category": {cat}, "savePath": {in(filepath.Join(dl, "complete", cat))}}
		if code, err := api.DoStatus("POST", "/torrents/createCategory", form, nil); err != nil {
			if code != 409 {
				return err
			}
			api.Post("/torrents/editCategory", form, nil)
		}
	}
	c.J.OK("Download folders, categories (%s), seeding limits and the watch folder are set", strings.Join(core.DownloadCategories, ", "))

	time.Sleep(5 * time.Second)
	var t struct {
		ConnectionStatus string `json:"connection_status"`
		DHTNodes         int    `json:"dht_nodes"`
	}
	if err := api.Get("/transfer/info", &t); err == nil {
		c.J.Info("Connection: %s, %d DHT nodes (this grows over the first minutes)", t.ConnectionStatus, t.DHTNodes)
	}
	return nil
}
