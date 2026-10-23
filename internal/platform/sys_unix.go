//go:build !windows

package platform

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"

	"mediastack/internal/core"
)

// Chown hands a folder (recursively) to the user the apps run as, when setup runs as root.
func Chown(st *core.Stack, path string) error {
	if os.Geteuid() != 0 || st.UID == 0 {
		return nil
	}
	return filepath.WalkDir(path, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		return os.Lchown(p, st.UID, st.GID)
	})
}

func groupOf(path string) string {
	fi, err := os.Stat(path)
	if err != nil {
		return ""
	}
	if s, ok := fi.Sys().(*syscall.Stat_t); ok {
		return fmt.Sprint(s.Gid)
	}
	return ""
}

// FreeSpace is the free space on the drive holding path.
func FreeSpace(path string) (uint64, error) {
	var s syscall.Statfs_t
	if err := syscall.Statfs(path, &s); err != nil {
		return 0, err
	}
	return s.Bavail * uint64(s.Bsize), nil
}

// DefaultRoot suggests where to put the stack.
func DefaultRoot() string { return "/srv/mediastack" }

func renderGroup() string { return groupOf("/dev/dri/renderD128") }
func dockerGroup() string { return groupOf("/var/run/docker.sock") }
