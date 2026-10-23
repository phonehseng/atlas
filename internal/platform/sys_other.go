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
