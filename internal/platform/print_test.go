package platform

import (
	"os"
	"testing"

	"mediastack/internal/core"
)

func TestPrintCompose(t *testing.T) {
	out := os.Getenv("PRINT_COMPOSE")
	if out == "" {
		t.Skip()
	}
	st := core.New("/srv/mediastack")
	st.OS, st.UID, st.GID, st.TZ = "linux", 1000, 1000, "Europe/London"
	st.VPN = core.VPN{Mode: "gluetun", Provider: "nordvpn"}
	for id := range Apps {
		if Apps[id].Docker != nil {
			st.Deployed[id] = "x"
		}
	}
	doc, _ := BuildCompose(st)
	b, _ := yamlMarshal(doc)
	os.WriteFile(out, b, 0o644)
}
