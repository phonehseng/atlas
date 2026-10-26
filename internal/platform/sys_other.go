//go:build !windows

package platform

import (
	"errors"
	"os/exec"
)

func hideWindow(*exec.Cmd) {}

// AdapterID is Windows-only.
func AdapterID(string) (string, error) { return "", errors.New("only on Windows") }

// ShowConsole is Windows-only (a terminal is already attached elsewhere).
func ShowConsole() {}

// RunLauncher is Windows-only.
func RunLauncher(string, []string, string, []string) int { return 1 }

// DesktopDir is Windows-only.
func DesktopDir() string { return "" }

// Protect is Windows-only (DPAPI).
func Protect([]byte) ([]byte, error) { return nil, errors.New("only on Windows") }

// Unprotect is Windows-only (DPAPI).
func Unprotect([]byte) ([]byte, error) { return nil, errors.New("only on Windows") }

// ProgramsDir is Windows-only.
func ProgramsDir() string { return "" }

// Shortcut describes a Windows .lnk file.
type Shortcut struct {
	Target, Args, WorkDir, Icon, Description string
	Minimized                                bool
}

// MakeShortcut is Windows-only.
func MakeShortcut(string, Shortcut) error { return errors.New("only on Windows") }
