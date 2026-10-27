package core

import (
	"archive/zip"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// Zips made by Windows PowerShell (Compress-Archive) use backslashes, folders included ("top\sub\").
func TestUnzipBackslashFolders(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "a.zip")
	f, _ := os.Create(src)
	w := zip.NewWriter(f)
	for _, name := range []string{`Top\app.exe`, `Top\_internal\lib\`, `Top\_internal\lib\x.pyd`} {
		e, _ := w.Create(name)
		if name[len(name)-1] != '\\' {
			e.Write([]byte("data"))
		}
	}
	w.Close()
	f.Close()
	if top, _ := ZipTopFolder(src); top != "Top" {
		t.Errorf("top folder %q", top)
	}
	out := filepath.Join(dir, "out")
	if err := Unzip(src, out, 1); err != nil {
		t.Fatal(err)
	}
	if b, err := os.ReadFile(filepath.Join(out, "_internal", "lib", "x.pyd")); err != nil || string(b) != "data" {
		t.Errorf("x.pyd: %v", err)
	}
}

func TestManifestFile(t *testing.T) {
	e := &ManifestEntry{Version: "1.0", Files: map[string]ManifestFile{"windows-x64": {URL: "https://a/w.zip"}}}
	if f, ok := e.File("windows-x64"); !ok || f.URL != "https://a/w.zip" {
		t.Error("files entry")
	}
	if _, ok := e.File("linux-x64"); ok {
		t.Error("no linux file")
	}
	e = &ManifestEntry{Version: "1.0", ManifestFile: ManifestFile{URL: "https://a/m.zip", SHA256: "x"}}
	if f, ok := e.File("windows-x64"); !ok || f.URL != "https://a/m.zip" {
		t.Error("flat entry")
	}
}

// The app's entry is "atlas", or "marquee" in a manifest from before the rename.
func TestManifestApp(t *testing.T) {
	var m Manifest
	json.Unmarshal([]byte(`{"schema":1,"marquee":{"version":"1.5.1"}}`), &m)
	if m.App() == nil || m.App().Version != "1.5.1" {
		t.Error("old manifest", m.App())
	}
	m = Manifest{}
	json.Unmarshal([]byte(`{"schema":1,"atlas":{"version":"1.6.0"},"marquee":{"version":"1.6.0-old"}}`), &m)
	if m.App() == nil || m.App().Version != "1.6.0" {
		t.Error("new manifest", m.App())
	}
	if (*Manifest)(nil).App() != nil {
		t.Error("nil manifest")
	}
}
