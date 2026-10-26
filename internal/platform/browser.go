package platform

import (
	"context"
	"io"
	"os"
	"os/exec"
	"runtime"
)

// OpenBrowser opens an address in the default browser.
func OpenBrowser(url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	return cmd.Start()
}

// RunTo runs a program with no window, sending its output to w.
func RunTo(ctx context.Context, w io.Writer, exe string, args ...string) error {
	cmd := exec.CommandContext(ctx, exe, args...)
	cmd.Env = os.Environ()
	cmd.Stdout, cmd.Stderr = w, w
	hideWindow(cmd)
	return cmd.Run()
}
