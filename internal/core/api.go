package core

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"time"
)

// API is a small JSON client for one app's web API.
type API struct {
	Base    string
	Headers map[string]string
	Client  *http.Client
}

// NewAPI makes a client that keeps cookies (some apps log in with a session cookie).
func NewAPI(base string, headers map[string]string) *API {
	jar, _ := cookiejar.New(nil)
	if headers == nil {
		headers = map[string]string{}
	}
	return &API{Base: strings.TrimRight(base, "/"), Headers: headers, Client: &http.Client{Timeout: 120 * time.Second, Jar: jar}}
}

// HTTPError is a non-2xx response.
type HTTPError struct {
	Method, URL string
	Status      int
	Body        string
}

func (e *HTTPError) Error() string {
	body := strings.TrimSpace(strings.TrimPrefix(e.Body, string(rune(0xFEFF))))
	if strings.HasPrefix(body, "<") {
		body = "(an error page)" // don't dump HTML into the log
	}
	return fmt.Sprintf("%s %s: HTTP %d %s", e.Method, e.URL, e.Status, Truncate(body, 300))
}

// Retry repeats fn while the app answers "503 starting up" (some apps open their port before they're
// ready), for up to the given time.
func Retry(ctx context.Context, d time.Duration, fn func() error) error {
	deadline := time.Now().Add(d)
	for {
		err := fn()
		var he *HTTPError
		if err == nil || !errors.As(err, &he) || he.Status != 503 || time.Now().After(deadline) {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
}

// Do sends body (JSON-encoded unless it's url.Values or []byte) and decodes a JSON reply into out.
func (a *API) Do(method, path string, body, out any) error {
	_, err := a.DoStatus(method, path, body, out)
	return err
}

// DoStatus is Do that also returns the HTTP status.
func (a *API) DoStatus(method, path string, body, out any) (int, error) {
	var rd io.Reader
	ctype := ""
	switch b := body.(type) {
	case nil:
	case url.Values:
		rd, ctype = strings.NewReader(b.Encode()), "application/x-www-form-urlencoded"
	case []byte:
		rd, ctype = bytes.NewReader(b), "application/octet-stream"
	default:
		buf, err := json.Marshal(b)
		if err != nil {
			return 0, err
		}
		rd, ctype = bytes.NewReader(buf), "application/json"
	}
	u := path
	if !strings.HasPrefix(path, "http://") && !strings.HasPrefix(path, "https://") {
		u = a.Base + path
	}
	req, err := http.NewRequest(method, u, rd)
	if err != nil {
		return 0, err
	}
	if ctype != "" {
		req.Header.Set("Content-Type", ctype)
	}
	req.Header.Set("Accept", "application/json")
	for k, v := range a.Headers {
		req.Header.Set(k, v)
	}
	resp, err := a.Client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return resp.StatusCode, &HTTPError{Method: method, URL: u, Status: resp.StatusCode, Body: string(data)}
	}
	if out != nil && len(bytes.TrimSpace(data)) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return resp.StatusCode, fmt.Errorf("%s %s: unexpected reply: %w", method, u, err)
		}
	}
	return resp.StatusCode, nil
}

func (a *API) Get(path string, out any) error         { return a.Do("GET", path, nil, out) }
func (a *API) Post(path string, body, out any) error  { return a.Do("POST", path, body, out) }
func (a *API) Put(path string, body, out any) error   { return a.Do("PUT", path, body, out) }
func (a *API) Delete(path string) error               { return a.Do("DELETE", path, nil, nil) }
func (a *API) Patch(path string, body, out any) error { return a.Do("PATCH", path, body, out) }

// WaitHTTP polls url until it answers with one of the ok statuses (any status < 500 if none given).
func WaitHTTP(ctx context.Context, j *Job, what, u string, timeout time.Duration, ok ...int) error {
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	start := time.Now()
	lastNote := time.Now()
	for {
		req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
		resp, err := client.Do(req)
		if err == nil {
			resp.Body.Close()
			good := resp.StatusCode < 500
			if len(ok) > 0 {
				good = false
				for _, s := range ok {
					good = good || resp.StatusCode == s
				}
			}
			if good {
				return nil
			}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("%s didn't start (no answer at %s after %s)", what, u, timeout)
		}
		if j != nil && time.Since(lastNote) > 20*time.Second {
			lastNote = time.Now()
			j.Info("Still waiting for %s to start (%ds)...", what, int(time.Since(start).Seconds()))
		}
		time.Sleep(2 * time.Second)
	}
}

// Truncate shortens s for log messages.
func Truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// M is shorthand for a JSON object.
type M = map[string]any
