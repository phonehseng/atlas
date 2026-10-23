package platform

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"

	"mediastack/assets"
	"mediastack/internal/core"
)

// Docker runs every app as a service of one Docker Compose project (<root>/docker-compose.yml).
type Docker struct{}

func init() {
	// The VPN container qBittorrent runs inside (Linux only).
	add(&App{ID: "gluetun", Name: "VPN (Gluetun)",
		Docker: &DockerSpec{Image: "qmcgaw/gluetun:latest", Volumes: []string{"{config}:/gluetun"}}})
}

// Project is the Compose project name.
func Project(st *core.Stack) string {
	return strings.ToLower(regexp.MustCompile(`[^A-Za-z0-9]+`).ReplaceAllString(st.Name, ""))
}

func composeArgs(st *core.Stack, args ...string) []string {
	return append([]string{"compose", "--project-name", Project(st), "--file", ComposeFile(st), "--env-file", EnvFile(st)}, args...)
}

// Compose runs "docker compose ..." for the stack, streaming output to the job.
func Compose(ctx context.Context, st *core.Stack, j *core.Job, args ...string) error {
	return Run(ctx, j, st.Root, nil, "docker", composeArgs(st, args...)...)
}

func composeQuiet(st *core.Stack, args ...string) error {
	cmd := exec.Command("docker", composeArgs(st, args...)...)
	cmd.Dir = st.Root
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker compose %s: %s", strings.Join(args, " "), core.Truncate(string(out), 400))
	}
	return nil
}

func (d *Docker) Deploy(ctx context.Context, st *core.Stack, j *core.Job, id string) error {
	a := Get(id)
	spec := a.Docker
	if spec == nil {
		return fmt.Errorf("%s isn't available on Linux", a.Name)
	}
	if err := os.MkdirAll(st.ConfigDir(id), 0o755); err != nil {
		return err
	}
	if a.Prepare != nil {
		if err := a.Prepare(ctx, st, j, core.BasePorts[id]); err != nil {
			return fmt.Errorf("writing %s settings: %w", a.Name, err)
		}
	}
	if spec.Build != "" {
		if err := writeBuildContext(st); err != nil {
			return err
		}
	}
	if err := Chown(st, st.ConfigDir(id)); err != nil {
		return err
	}
	if err := st.SetDeployed(id, imageOf(st, a)); err != nil {
		return err
	}
	if err := WriteCompose(st); err != nil {
		return err
	}
	svc := id
	if spec.Build != "" {
		j.Info("Building %s...", a.Name)
		if err := Compose(ctx, st, j, "build", svc); err != nil {
			return err
		}
	} else {
		j.Info("Downloading %s (%s)...", a.Name, spec.Image)
		if err := Compose(ctx, st, j, "pull", "--quiet", svc); err != nil {
			return err
		}
	}
	return Compose(ctx, st, j, "up", "--detach", "--remove-orphans", svc)
}

func imageOf(st *core.Stack, a *App) string {
	if a.Docker.Build != "" {
		return "mediastack-py:" + assets.Version
	}
	return a.Docker.Image
}

func (d *Docker) Start(st *core.Stack, id string) error   { return composeQuiet(st, "start", id) }
func (d *Docker) Stop(st *core.Stack, id string) error    { return composeQuiet(st, "stop", id) }
func (d *Docker) Restart(st *core.Stack, id string) error { return composeQuiet(st, "restart", id) }

func (d *Docker) RunNow(ctx context.Context, st *core.Stack, j *core.Job, id string) error {
	switch id {
	case "recyclarr":
		return Compose(ctx, st, j, "exec", "-T", "recyclarr", "recyclarr", "sync")
	case "healthcheck":
		return Compose(ctx, st, j, "exec", "-T", "hub", "python", "/app/mediasorter/healthcheck.py")
	}
	return fmt.Errorf("%s has nothing to run", id)
}

func (d *Docker) Remove(st *core.Stack, id string) error {
	composeQuiet(st, "rm", "--stop", "--force", id)
	st.Remove(id)
	return WriteCompose(st)
}

// ContainerStates maps service -> state ("running", "exited", ...).
func ContainerStates(st *core.Stack) map[string]string {
	cmd := exec.Command("docker", composeArgs(st, "ps", "--all", "--format", "json")...)
	cmd.Dir = st.Root
	out, err := cmd.Output()
	states := map[string]string{}
	if err != nil {
		return states
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		var c struct{ Service, State string }
		if json.Unmarshal([]byte(line), &c) == nil && c.Service != "" {
			states[c.Service] = c.State
		}
	}
	return states
}

// ServiceHealth is a service's Docker health status ("healthy", "starting", "unhealthy", or its state).
func ServiceHealth(st *core.Stack, svc string) string {
	cmd := exec.Command("docker", composeArgs(st, "ps", "--all", "--format", "json", svc)...)
	cmd.Dir = st.Root
	out, err := cmd.Output()
	if err != nil {
		return "unknown"
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		var c struct{ Service, State, Health string }
		if json.Unmarshal([]byte(line), &c) == nil && c.Service == svc {
			if c.Health != "" {
				return c.Health
			}
			return c.State
		}
	}
	return "missing"
}

// GluetunPublicIP reads the VPN's public address from Gluetun's log ("" if not found).
func GluetunPublicIP(st *core.Stack) string {
	cmd := exec.Command("docker", composeArgs(st, "logs", "--tail", "200", "gluetun")...)
	cmd.Dir = st.Root
	out, _ := cmd.CombinedOutput()
	m := regexp.MustCompile(`Public IP address is ([0-9a-fA-F.:]+)`).FindAllStringSubmatch(string(out), -1)
	if len(m) == 0 {
		return ""
	}
	return m[len(m)-1][1]
}

// writeBuildContext writes the MediaSorter / Media Hub image sources.
func writeBuildContext(st *core.Stack) error {
	dir := filepath.Join(st.Root, "apps", "mediastack-py")
	if err := os.RemoveAll(dir); err != nil {
		return err
	}
	if err := WritePythonApps(dir); err != nil {
		return err
	}
	reqs := strings.Join(pythonRequirements, "\n") + "\n"
	if err := os.WriteFile(filepath.Join(dir, "requirements.txt"), []byte(reqs), 0o644); err != nil {
		return err
	}
	dockerfile := `# MediaSorter + Media Hub (generated by Media Stack Setup)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends mkvtoolnix && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
`
	return os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte(dockerfile), 0o644)
}

// ---------------------------------------------------------------------------------------------- Linux system

// DockerInfo describes the Docker installation.
type DockerInfo struct {
	Installed, Running, Compose bool
	Version                     string
}

func CheckDocker() DockerInfo {
	var di DockerInfo
	if _, err := exec.LookPath("docker"); err != nil {
		return di
	}
	di.Installed = true
	if out, err := exec.Command("docker", "version", "--format", "{{.Server.Version}}").Output(); err == nil {
		di.Running = true
		di.Version = strings.TrimSpace(string(out))
	}
	if err := exec.Command("docker", "compose", "version").Run(); err == nil {
		di.Compose = true
	}
	return di
}

// InstallDocker uses Docker's official convenience script, then starts the service.
func InstallDocker(ctx context.Context, j *core.Job) error {
	j.Info("Installing Docker with Docker's official script (get.docker.com)...")
	if err := Run(ctx, j, "/", nil, "sh", "-c", "curl -fsSL https://get.docker.com | sh"); err != nil {
		return err
	}
	return StartDocker(ctx, j)
}

// StartDocker starts the Docker service (systemd, or SysV "service" where systemd isn't running).
func StartDocker(ctx context.Context, j *core.Job) error {
	if _, err := os.Stat("/run/systemd/system"); err == nil {
		return Run(ctx, j, "/", nil, "systemctl", "enable", "--now", "docker")
	}
	return Run(ctx, j, "/", nil, "service", "docker", "start")
}
