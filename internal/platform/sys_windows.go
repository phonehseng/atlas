//go:build windows

package platform

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"

	"mediastack/internal/core"
)

func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
}

var (
	iphlpapi                        = windows.NewLazySystemDLL("iphlpapi.dll")
	procConvertInterfaceAliasToLuid = iphlpapi.NewProc("ConvertInterfaceAliasToLuid")
	procConvertInterfaceLuidToNameW = iphlpapi.NewProc("ConvertInterfaceLuidToNameW")
	kernel32                        = windows.NewLazySystemDLL("kernel32.dll")
	procAllocConsole                = kernel32.NewProc("AllocConsole")
)

// AdapterID turns an adapter's friendly name ("NordLynx") into the internal name qBittorrent binds to
// ("iftype53_32771"). Windows can renumber it when VPN adapters are recreated, so it's looked up live.
func AdapterID(alias string) (string, error) {
	a, err := windows.UTF16PtrFromString(alias)
	if err != nil {
		return "", err
	}
	var luid uint64
	if r, _, _ := procConvertInterfaceAliasToLuid.Call(uintptr(unsafe.Pointer(a)), uintptr(unsafe.Pointer(&luid))); r != 0 {
		return "", fmt.Errorf("no network adapter named %q", alias)
	}
	buf := make([]uint16, 256)
	if r, _, _ := procConvertInterfaceLuidToNameW.Call(uintptr(unsafe.Pointer(&luid)), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf))); r != 0 {
		return "", fmt.Errorf("couldn't read the adapter id of %q", alias)
	}
	return windows.UTF16ToString(buf), nil
}

// ShowConsole gives this (windowless) program a console window for the setup wizard's messages.
func ShowConsole() {
	if r, _, _ := procAllocConsole.Call(); r == 0 {
		return
	}
	if f, err := os.OpenFile("CONOUT$", os.O_RDWR, 0); err == nil {
		os.Stdout, os.Stderr = f, f
	}
	if f, err := os.OpenFile("CONIN$", os.O_RDWR, 0); err == nil {
		os.Stdin = f
	}
}

// RunLauncher is the hidden launcher the scheduled tasks start: it runs an app with no window, appends
// its output to a log file, and ties the app's life to its own (ending the task ends the app).
func RunLauncher(logFile string, env []string, exe string, args []string) int {
	if err := os.MkdirAll(filepath.Dir(logFile), 0o755); err != nil {
		return 1
	}
	rotate(logFile, 20<<20)
	logf, err := os.OpenFile(logFile, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return 1
	}
	defer logf.Close()

	job, err := windows.CreateJobObject(nil, nil)
	if err == nil {
		info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
		info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation,
			uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info)))
	}

	cmd := exec.Command(exe, args...)
	cmd.Dir, _ = os.Getwd()
	cmd.Env = append(os.Environ(), env...)
	cmd.Stdout, cmd.Stderr = logf, logf
	cmd.Stdin = nil
	hideWindow(cmd)
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(logf, "launcher: couldn't start %s: %v\n", exe, err)
		return 1
	}
	if job != 0 {
		if h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid)); err == nil {
			windows.AssignProcessToJobObject(job, h)
			windows.CloseHandle(h)
		}
	}
	err = cmd.Wait()
	if err != nil {
		fmt.Fprintf(logf, "launcher: %s exited: %v\n", filepath.Base(exe), err)
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		return 1
	}
	return 0
}

func rotate(file string, max int64) {
	if fi, err := os.Stat(file); err == nil && fi.Size() > max {
		os.Remove(file + ".1")
		os.Rename(file, file+".1")
	}
}

var _ io.Writer = (*os.File)(nil)

// FreeSpace is the free space on the drive holding path.
func FreeSpace(path string) (uint64, error) {
	p, err := windows.UTF16PtrFromString(filepath.VolumeName(path) + `\`)
	if err != nil {
		return 0, err
	}
	var free, total, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(p, &free, &total, &totalFree); err != nil {
		return 0, err
	}
	return free, nil
}

// DefaultRoot suggests <fixed drive with the most free space>\MediaServer.
func DefaultRoot() string {
	best, bestFree := "C:", uint64(0)
	for c := 'C'; c <= 'Z'; c++ {
		d := string(c) + `:\`
		p, _ := windows.UTF16PtrFromString(d)
		if windows.GetDriveType(p) != windows.DRIVE_FIXED {
			continue
		}
		if free, err := FreeSpace(d); err == nil && free > bestFree {
			best, bestFree = string(c)+":", free
		}
	}
	return best + `\MediaServer`
}

// Chown is a no-op on Windows (files belong to the signed-in user already).
func Chown(*core.Stack, string) error { return nil }

func renderGroup() string { return "" }
func dockerGroup() string { return "" }

// DesktopDir is the user's real Desktop folder (OneDrive may have moved it).
func DesktopDir() string {
	dir, err := windows.KnownFolderPath(windows.FOLDERID_Desktop, 0)
	if err != nil {
		return ""
	}
	return dir
}
