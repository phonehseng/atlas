package platform

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strings"

	"time"
	"unicode/utf16"

	"mediastack/internal/core"
)

// Windows runs every app as a Task Scheduler task in its own folder: it starts when you sign in,
// restarts if it crashes, and needs no admin rights. Console apps run through this program's hidden
// launcher ("MediaStackSetup.exe run ..."), which keeps their output in <root>\logs.
type Windows struct{}

// LauncherPath is the copy of this program the scheduled tasks use.
func LauncherPath(st *core.Stack) string { return filepath.Join(st.Root, "apps", "MediaStack.exe") }

func taskName(st *core.Stack, a *App) string { return `\` + st.Name + `\` + a.Name }

func (w *Windows) Deploy(ctx context.Context, st *core.Stack, j *core.Job, id string) error {
	a := Get(id)
	r := a.Win
	if r == nil {
		return fmt.Errorf("%s isn't available on Windows", a.Name)
	}
	if err := ensureLauncher(st); err != nil {
		return err
	}
	if err := os.MkdirAll(st.ConfigDir(id), 0o755); err != nil {
		return err
	}
	if st.DeployedVersion(id) != "" {
		j.Info("Stopping %s for the update...", a.Name)
		w.Stop(st, id)
		time.Sleep(2 * time.Second)
	}
	var version string
	var err error
	switch {
	case r.Winget != "":
		exe := ExpandWin(r.WingetExe)
		if _, serr := os.Stat(exe); serr != nil {
			if err = Winget(ctx, j, r.Winget); err != nil {
				return err
			}
			if _, serr := os.Stat(exe); serr != nil {
				return fmt.Errorf("%s was installed but %s is missing", a.Name, exe)
			}
		} else {
			j.Info("%s is already installed", a.Name)
		}
		version = "installed"
	case r.Install != nil:
		version, err = r.Install(ctx, st, j, st.AppDir(id))
	default:
		version, err = download(ctx, st, j, a)
	}
	if err != nil {
		return err
	}
	if a.Prepare != nil {
		if err := a.Prepare(ctx, st, j, st.Port(id)); err != nil {
			return fmt.Errorf("writing %s settings: %w", a.Name, err)
		}
	}
	// The task starts in this folder; apps that share code (MediaSorter, Media Hub) have no folder of their own.
	if err := os.MkdirAll(w.workDir(st, a), 0o755); err != nil {
		return err
	}
	if err := w.register(st, a); err != nil {
		return err
	}
	if err := st.SetDeployed(id, version); err != nil {
		return err
	}
	if r.Trigger == "" || r.Trigger == "logon" {
		return w.Start(st, id)
	}
	return nil
}

func (w *Windows) Start(st *core.Stack, id string) error {
	return schtasks("/Run", "/TN", taskName(st, Get(id)))
}

func (w *Windows) Stop(st *core.Stack, id string) error {
	if id == "qbittorrent" {
		// Let qBittorrent save its torrent state instead of being killed.
		if core.NewAPI(st.URL(id), nil).Post("/api/v2/app/shutdown", nil, nil) == nil {
			time.Sleep(4 * time.Second)
		}
	}
	return schtasks("/End", "/TN", taskName(st, Get(id)))
}

func (w *Windows) Restart(st *core.Stack, id string) error {
	w.Stop(st, id)
	time.Sleep(2 * time.Second)
	return w.Start(st, id)
}

// RunNow runs a scheduled job (Recyclarr sync, health check) once through the launcher and waits.
func (w *Windows) RunNow(ctx context.Context, st *core.Stack, j *core.Job, id string) error {
	a := Get(id)
	exe, args, env := w.command(st, a)
	cmd := exec.CommandContext(ctx, exe, args...)
	cmd.Dir = w.workDir(st, a)
	cmd.Env = append(os.Environ(), env...)
	out := j.Writer("  ")
	cmd.Stdout, cmd.Stderr = out, out
	hideWindow(cmd)
	return cmd.Run()
}

func (w *Windows) Remove(st *core.Stack, id string) error {
	w.Stop(st, id)
	return schtasks("/Delete", "/TN", taskName(st, Get(id)), "/F")
}

// command is the program, arguments and environment an app runs with (before the launcher wraps it).
func (w *Windows) command(st *core.Stack, a *App) (string, []string, []string) {
	r := a.Win
	exe := ExpandWin(Expand(st, a.ID, r.Exe))
	if r.Winget != "" {
		exe = ExpandWin(r.WingetExe)
	} else if !filepath.IsAbs(exe) {
		exe = filepath.Join(st.AppDir(a.ID), exe)
	}
	var args, env []string
	for _, s := range r.Args {
		args = append(args, Expand(st, a.ID, s))
	}
	for _, s := range r.Env {
		env = append(env, Expand(st, a.ID, s))
	}
	return exe, args, env
}

func (w *Windows) workDir(st *core.Stack, a *App) string {
	if a.Win.Winget != "" {
		return filepath.Dir(ExpandWin(a.Win.WingetExe))
	}
	return st.AppDir(a.ID)
}

// register creates/updates the app's scheduled task.
func (w *Windows) register(st *core.Stack, a *App) error {
	exe, args, env := w.command(st, a)
	if !a.Win.Visible {
		// Wrap console programs in the hidden launcher.
		wrapped := []string{"run", "--log", filepath.Join(st.LogDir(), a.ID+".log")}
		for _, e := range env {
			wrapped = append(wrapped, "--env", e)
		}
		wrapped = append(wrapped, "--", exe)
		args = append(wrapped, args...)
		exe = LauncherPath(st)
	}
	xml, err := taskXML(st, a, exe, args, w.workDir(st, a))
	if err != nil {
		return err
	}
	file := filepath.Join(st.Root, "downloads-cache", "task-"+a.ID+".xml")
	if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(file, xml, 0o644); err != nil {
		return err
	}
	defer os.Remove(file)
	return schtasks("/Create", "/TN", taskName(st, a), "/XML", file, "/F")
}

// taskXML builds a Task Scheduler definition (UTF-16, as schtasks expects).
func taskXML(st *core.Stack, a *App, exe string, args []string, dir string) ([]byte, error) {
	u, err := user.Current()
	if err != nil {
		return nil, err
	}
	var trig string
	start := time.Now().Add(5 * time.Minute).Format("2006-01-02T15:04:05")
	switch a.Win.Trigger {
	case "weekly":
		trig = fmt.Sprintf(`<CalendarTrigger><StartBoundary>%s</StartBoundary><Enabled>true</Enabled>`+
			`<ScheduleByWeek><DaysOfWeek><Sunday /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>`,
			time.Now().Format("2006-01-02")+"T04:00:00")
	case "daily":
		trig = fmt.Sprintf(`<CalendarTrigger><StartBoundary>%s</StartBoundary><Enabled>true</Enabled>`+
			`<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>`, time.Now().Format("2006-01-02")+"T03:30:00")
	case "hourly":
		trig = fmt.Sprintf(`<TimeTrigger><Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>`+
			`<StartBoundary>%s</StartBoundary><Enabled>true</Enabled></TimeTrigger>`, start)
	case "none":
		trig = ""
	default:
		trig = fmt.Sprintf(`<LogonTrigger><Enabled>true</Enabled><UserId>%s</UserId></LogonTrigger>`, xmlEsc(u.Username))
	}
	restart := `<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>`
	limit := "PT0S"
	if a.Win.Trigger == "weekly" || a.Win.Trigger == "hourly" || a.Win.Trigger == "daily" {
		restart, limit = "", "PT2H"
	}
	quoted := make([]string, len(args))
	for i, s := range args {
		quoted[i] = EscapeArg(s)
	}
	doc := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>%s - part of %s (Media Stack Setup)</Description></RegistrationInfo>
  <Triggers>%s</Triggers>
  <Principals><Principal id="Author"><UserId>%s</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>%s</ExecutionTimeLimit>
    <Priority>5</Priority>
    %s
  </Settings>
  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments><WorkingDirectory>%s</WorkingDirectory></Exec></Actions>
</Task>
`, xmlEsc(a.Name), xmlEsc(st.Name), trig, xmlEsc(u.Username), limit, restart, xmlEsc(exe), xmlEsc(strings.Join(quoted, " ")), xmlEsc(dir))
	return utf16LE(doc), nil
}

// EscapeArg quotes one command-line argument the way Windows programs parse them.
func EscapeArg(s string) string {
	if s == "" {
		return `""`
	}
	if !strings.ContainsAny(s, " \t\"") {
		return s
	}
	var b strings.Builder
	b.WriteByte('"')
	slashes := 0
	for _, c := range s {
		switch c {
		case '\\':
			slashes++
			continue
		case '"':
			b.WriteString(strings.Repeat(`\`, slashes*2+1))
		default:
			b.WriteString(strings.Repeat(`\`, slashes))
		}
		slashes = 0
		b.WriteRune(c)
	}
	b.WriteString(strings.Repeat(`\`, slashes*2))
	b.WriteByte('"')
	return b.String()
}

func xmlEsc(s string) string {
	var b bytes.Buffer
	if err := xmlEscape(&b, s); err != nil {
		return s
	}
	return b.String()
}

func xmlEscape(w io.Writer, s string) error {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&apos;")
	_, err := io.WriteString(w, r.Replace(s))
	return err
}

func utf16LE(s string) []byte {
	u := utf16.Encode([]rune(s))
	b := make([]byte, 2+2*len(u))
	b[0], b[1] = 0xFF, 0xFE
	for i, c := range u {
		binary.LittleEndian.PutUint16(b[2+2*i:], c)
	}
	return b
}

func schtasks(args ...string) error {
	cmd := exec.Command("schtasks.exe", args...)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("schtasks %s: %s", args[0], strings.TrimSpace(string(out)))
	}
	return nil
}

// TaskRunning reports whether an app's task is running.
func TaskRunning(st *core.Stack, id string) bool {
	cmd := exec.Command("schtasks.exe", "/Query", "/TN", taskName(st, Get(id)), "/FO", "CSV", "/NH")
	hideWindow(cmd)
	out, err := cmd.Output()
	return err == nil && strings.Contains(string(out), `"Running"`)
}

// ensureLauncher keeps a copy of this program where the tasks expect it. A running copy can't be
// overwritten but can be renamed, so an update moves the old one aside.
func ensureLauncher(st *core.Stack) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	dest := LauncherPath(st)
	if same, _ := sameFile(self, dest); same {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	if old, _ := filepath.Glob(dest + ".old*"); len(old) > 0 {
		for _, o := range old {
			os.Remove(o) // fails harmlessly while still in use
		}
	}
	if _, err := os.Stat(dest); err == nil {
		if err := os.Rename(dest, fmt.Sprintf("%s.old%d", dest, time.Now().Unix())); err != nil {
			return fmt.Errorf("couldn't update %s: %w", dest, err)
		}
	}
	in, err := os.ReadFile(self)
	if err != nil {
		return err
	}
	return os.WriteFile(dest, in, 0o755)
}

func sameFile(a, b string) (bool, error) {
	fa, err := os.Stat(a)
	if err != nil {
		return false, err
	}
	fb, err := os.Stat(b)
	if err != nil {
		return false, err
	}
	if os.SameFile(fa, fb) {
		return true, nil
	}
	if fa.Size() != fb.Size() {
		return false, nil
	}
	ha, _ := core.SHA256File(a)
	hb, _ := core.SHA256File(b)
	return ha == hb, nil
}

// ExpandWin expands %VAR% references.
func ExpandWin(s string) string {
	for {
		i := strings.Index(s, "%")
		if i < 0 {
			return s
		}
		k := strings.Index(s[i+1:], "%")
		if k < 0 {
			return s
		}
		name := s[i+1 : i+1+k]
		s = s[:i] + os.Getenv(name) + s[i+2+k:]
	}
}

// Winget installs a package system-wide; Windows asks the user to approve it.
func Winget(ctx context.Context, j *core.Job, id string) error {
	if _, err := exec.LookPath("winget.exe"); err != nil {
		return core.UserAction("Windows' package installer (winget) is missing. Install \"App Installer\" from the Microsoft Store, then try again.")
	}
	j.Action("Installing %s - if Windows asks for permission, click Yes.", id)
	err := Run(ctx, j, "", nil, "winget.exe", "install", "--id", id, "--exact", "--silent",
		"--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity")
	if err != nil {
		var ee *exec.ExitError
		if asExit(err, &ee) && uint32(ee.ExitCode()) == 0x8A15002B {
			return nil // already installed and up to date
		}
		return fmt.Errorf("installing %s failed (was the Windows permission prompt declined?): %w", id, err)
	}
	return nil
}

// RunElevated runs a PowerShell script as administrator (one Windows permission prompt) and waits.
func RunElevated(ctx context.Context, j *core.Job, script string) error {
	file := filepath.Join(os.TempDir(), fmt.Sprintf("mediastack-%d.ps1", time.Now().UnixNano()))
	if err := os.WriteFile(file, append([]byte{0xEF, 0xBB, 0xBF}, script...), 0o644); err != nil {
		return err
	}
	defer os.Remove(file)
	j.Action("Windows will ask for permission - click Yes to continue.")
	launcher := fmt.Sprintf(`$p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `+
		`-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','"%s"'; exit $p.ExitCode`, file)
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-Command", launcher)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if strings.Contains(msg, "canceled by the user") {
			return core.UserAction("the Windows permission prompt was declined - click the button again and choose Yes")
		}
		return fmt.Errorf("the administrator step failed: %v %s", err, core.Truncate(msg, 300))
	}
	return nil
}

func asExit(err error, target **exec.ExitError) bool {
	for err != nil {
		if e, ok := err.(*exec.ExitError); ok {
			*target = e
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}
