package core

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const userAgent = "MediaStackSetup/1.0 (+https://github.com)"

var httpClient = &http.Client{Timeout: 0} // downloads can be large; each call sets its own deadline

// Download saves url to dest, reporting progress to the job. Returns the file's SHA-256.
func Download(ctx context.Context, j *Job, url, dest string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("download %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("download %s: HTTP %d", url, resp.StatusCode)
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}
	tmp := dest + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	name := filepath.Base(dest)
	total := resp.ContentLength
	var got int64
	last := time.Time{}
	buf := make([]byte, 256*1024)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := f.Write(buf[:n]); werr != nil {
				f.Close()
				return "", werr
			}
			h.Write(buf[:n])
			got += int64(n)
			if j != nil && time.Since(last) > 700*time.Millisecond {
				last = time.Now()
				if total > 0 {
					j.Progress(int(got*100/total), "Downloading %s (%d of %d MB)", name, got>>20, total>>20)
				} else {
					j.Progress(0, "Downloading %s (%d MB)", name, got>>20)
				}
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			f.Close()
			return "", fmt.Errorf("download %s: %w", url, rerr)
		}
	}
	if err := f.Close(); err != nil {
		return "", err
	}
	if total > 0 && got != total {
		return "", fmt.Errorf("download %s stopped early (%d of %d bytes)", url, got, total)
	}
	if err := os.Rename(tmp, dest); err != nil {
		return "", err
	}
	if j != nil {
		j.Progress(100, "Downloaded %s (%d MB)", name, got>>20)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// GetJSON fetches a JSON document.
func GetJSON(ctx context.Context, url string, out any) error {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/json")
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return fmt.Errorf("%s: HTTP %d %s", url, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// GetText fetches a small text document.
func GetText(ctx context.Context, url string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	req.Header.Set("User-Agent", userAgent)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("%s: HTTP %d", url, resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	return string(b), err
}

// Release is a GitHub release reduced to what the installer needs.
type Release struct {
	Tag    string
	Assets map[string]string // file name -> download URL
}

// LatestRelease asks GitHub for a repository's newest release. When the API's hourly limit is used up it
// falls back to the public release page, which has no limit.
func LatestRelease(ctx context.Context, repo string) (*Release, error) {
	var api struct {
		TagName string `json:"tag_name"`
		Assets  []struct {
			Name string `json:"name"`
			URL  string `json:"browser_download_url"`
		} `json:"assets"`
	}
	if err := GetJSON(ctx, "https://api.github.com/repos/"+repo+"/releases/latest", &api); err == nil && api.TagName != "" {
		r := &Release{Tag: api.TagName, Assets: map[string]string{}}
		for _, a := range api.Assets {
			r.Assets[a.Name] = a.URL
		}
		return r, nil
	}
	// Fallback: /releases/latest redirects to /releases/tag/<tag>; the asset list is a separate fragment.
	req, _ := http.NewRequestWithContext(ctx, "HEAD", "https://github.com/"+repo+"/releases/latest", nil)
	req.Header.Set("User-Agent", userAgent)
	noRedirect := &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	resp, err := noRedirect.Do(req)
	if err != nil {
		return nil, fmt.Errorf("couldn't look up the latest %s release: %w", repo, err)
	}
	resp.Body.Close()
	loc := resp.Header.Get("Location")
	i := strings.LastIndex(loc, "/tag/")
	if i < 0 {
		return nil, fmt.Errorf("couldn't look up the latest %s release (HTTP %d)", repo, resp.StatusCode)
	}
	tag := loc[i+5:]
	page, err := GetText(ctx, "https://github.com/"+repo+"/releases/expanded_assets/"+tag)
	if err != nil {
		return nil, err
	}
	r := &Release{Tag: tag, Assets: map[string]string{}}
	for _, m := range regexp.MustCompile(`href="(/[^"]+/releases/download/[^"]+)"`).FindAllStringSubmatch(page, -1) {
		r.Assets[filepath.Base(m[1])] = "https://github.com" + m[1]
	}
	return r, nil
}

// Asset finds the release file whose name matches pattern.
func (r *Release) Asset(pattern string) (name, url string, err error) {
	re := regexp.MustCompile(pattern)
	for n, u := range r.Assets {
		if re.MatchString(n) {
			return n, u, nil
		}
	}
	return "", "", fmt.Errorf("release %s has no file matching %s", r.Tag, pattern)
}

// Unzip extracts a zip into dest. strip removes that many leading folders from every path (for archives
// that wrap everything in one top folder).
func Unzip(src, dest string, strip int) error {
	zr, err := zip.OpenReader(src)
	if err != nil {
		return err
	}
	defer zr.Close()
	destAbs, _ := filepath.Abs(dest)
	for _, f := range zr.File {
		parts := strings.Split(strings.ReplaceAll(f.Name, "\\", "/"), "/")
		if len(parts) <= strip {
			continue
		}
		rel := filepath.Join(parts[strip:]...)
		if rel == "" || rel == "." {
			continue
		}
		target := filepath.Join(destAbs, rel)
		if !strings.HasPrefix(target, destAbs+string(os.PathSeparator)) && target != destAbs {
			return fmt.Errorf("unsafe path in archive: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		mode := f.Mode().Perm()
		if mode == 0 {
			mode = 0o644
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode|0o200)
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(out, rc)
		rc.Close()
		if cerr := out.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// ZipTopFolder returns the single top-level folder of a zip ("" if files sit at the root).
func ZipTopFolder(src string) (string, error) {
	zr, err := zip.OpenReader(src)
	if err != nil {
		return "", err
	}
	defer zr.Close()
	top := ""
	for _, f := range zr.File {
		first := strings.SplitN(strings.ReplaceAll(f.Name, "\\", "/"), "/", 2)[0]
		if !strings.Contains(strings.ReplaceAll(f.Name, "\\", "/"), "/") && !f.FileInfo().IsDir() {
			return "", nil // a file at the root
		}
		if top == "" {
			top = first
		} else if top != first {
			return "", nil
		}
	}
	return top, nil
}

// SHA256File hashes a file.
func SHA256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
