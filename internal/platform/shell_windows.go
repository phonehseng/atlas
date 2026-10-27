//go:build windows

package platform

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Protect encrypts data for the signed-in Windows account (DPAPI, CryptProtectData with no description,
// no extra entropy and no flags: the same call Atlas's settings.py makes).
func Protect(data []byte) ([]byte, error) {
	if len(data) == 0 {
		return nil, nil
	}
	in := windows.DataBlob{Size: uint32(len(data)), Data: &data[0]}
	var out windows.DataBlob
	if err := windows.CryptProtectData(&in, nil, nil, 0, nil, 0, &out); err != nil {
		return nil, fmt.Errorf("DPAPI: %w", err)
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return append([]byte(nil), unsafe.Slice(out.Data, out.Size)...), nil
}

// Unprotect reverses Protect (same Windows account only).
func Unprotect(data []byte) ([]byte, error) {
	if len(data) == 0 {
		return nil, nil
	}
	in := windows.DataBlob{Size: uint32(len(data)), Data: &data[0]}
	var out windows.DataBlob
	if err := windows.CryptUnprotectData(&in, nil, nil, 0, nil, 0, &out); err != nil {
		return nil, fmt.Errorf("DPAPI: %w", err)
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return append([]byte(nil), unsafe.Slice(out.Data, out.Size)...), nil
}

// ProgramsDir is the user's Start menu Programs folder.
func ProgramsDir() string {
	dir, err := windows.KnownFolderPath(windows.FOLDERID_Programs, 0)
	if err != nil {
		return ""
	}
	return dir
}

// FirewallRuleExists reports whether a Windows Firewall rule with this display name exists (reading the
// rules needs no administrator).
func FirewallRuleExists(name string) bool {
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
		"if (Get-NetFirewallRule -DisplayName $env:MS_RULE -ErrorAction SilentlyContinue) { 'yes' }")
	cmd.Env = append(os.Environ(), "MS_RULE="+name)
	hideWindow(cmd)
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) == "yes"
}

// Shortcut describes a .lnk file.
type Shortcut struct {
	Target, Args, WorkDir, Icon, Description string
	Minimized                                bool
}

// MakeShortcut writes a .lnk (through Windows' own WScript.Shell, so no COM code here). Values travel in
// environment variables, never inside the script text.
func MakeShortcut(file string, s Shortcut) error {
	style := "1"
	if s.Minimized {
		style = "7"
	}
	script := `$ErrorActionPreference = 'Stop'
$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MS_LNK)
$l.TargetPath = $env:MS_TARGET
$l.Arguments = $env:MS_ARGS
$l.WorkingDirectory = $env:MS_DIR
if ($env:MS_ICON) { $l.IconLocation = $env:MS_ICON }
$l.Description = $env:MS_DESC
$l.WindowStyle = [int]$env:MS_STYLE
$l.Save()`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
	cmd.Env = append(os.Environ(), "MS_LNK="+file, "MS_TARGET="+s.Target, "MS_ARGS="+s.Args, "MS_DIR="+s.WorkDir,
		"MS_ICON="+s.Icon, "MS_DESC="+s.Description, "MS_STYLE="+style)
	hideWindow(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("shortcut %s: %v %s", file, err, strings.TrimSpace(string(out)))
	}
	return nil
}
