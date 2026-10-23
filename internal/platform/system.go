package platform

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// LookPath finds a program on PATH.
func LookPath(name string) (string, error) { return exec.LookPath(name) }

// HasNvidia reports an NVIDIA graphics driver (Windows).
func HasNvidia() bool {
	if runtime.GOOS != "windows" {
		_, err := os.Stat("/dev/nvidia0")
		return err == nil
	}
	_, err := os.Stat(filepath.Join(os.Getenv("SystemRoot"), "System32", "nvidia-smi.exe"))
	return err == nil
}

// InvokingUser is the user who ran "sudo" (the apps run as them), else the current user; never root.
func InvokingUser() (int, int) {
	uid, _ := strconv.Atoi(os.Getenv("SUDO_UID"))
	gid, _ := strconv.Atoi(os.Getenv("SUDO_GID"))
	if uid > 0 {
		return uid, gid
	}
	if u := os.Getuid(); u > 0 {
		return u, os.Getgid()
	}
	return 1000, 1000
}

// TimeZone is the computer's IANA time zone name (Linux).
func TimeZone() string {
	if tz := os.Getenv("TZ"); tz != "" {
		return tz
	}
	if b, err := os.ReadFile("/etc/timezone"); err == nil {
		if tz := strings.TrimSpace(string(b)); tz != "" {
			return tz
		}
	}
	if target, err := os.Readlink("/etc/localtime"); err == nil {
		if i := strings.Index(target, "zoneinfo/"); i >= 0 {
			return target[i+len("zoneinfo/"):]
		}
	}
	return "Etc/UTC"
}
