// Package wizard serves the setup page and runs the steps one at a time.
package wizard

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"time"

	"mediastack/assets"
	"mediastack/internal/core"
	"mediastack/internal/platform"
	"mediastack/internal/steps"
)

// Server is the wizard.
type Server struct {
	Token string
	Quit  chan struct{}

	def      *steps.Defaults
	mu       sync.Mutex
	st       *core.Stack
	jobs     map[string]*core.Job
	running  *core.Job
	welcome  string // welcome step result before a stack exists
	ctx      context.Context
	cancel   context.CancelFunc
	quitOnce sync.Once
}

// New loads an earlier install (from --root or the remembered folder) if there is one.
func New(def *steps.Defaults) *Server {
	ctx, cancel := context.WithCancel(context.Background())
	s := &Server{Token: core.RandomHex(16), Quit: make(chan struct{}), def: def, jobs: map[string]*core.Job{},
		ctx: ctx, cancel: cancel}
	root := def.Root
	if root == "" {
		root = rememberedRoot()
	}
	if root != "" {
		if st, err := core.Load(root); err == nil {
			s.st = st
			if def.Root == "" {
				def.Root = root
			}
		}
	}
	return s
}

// Stack returns the loaded install (nil before a folder is chosen).
func (s *Server) Stack() *core.Stack {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.st
}

func pointerFile() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(dir, "MediaStackSetup", "last-root.txt")
}

func rememberedRoot() string {
	b, err := os.ReadFile(pointerFile())
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (s *Server) setStack(st *core.Stack) {
	s.mu.Lock()
	s.st = st
	if s.welcome == "done" {
		st.Steps["welcome"] = &core.StepState{Status: "done", At: time.Now()}
	}
	s.mu.Unlock()
	st.Save()
	if p := pointerFile(); p != "" {
		os.MkdirAll(filepath.Dir(p), 0o755)
		os.WriteFile(p, []byte(st.Root), 0o644)
	}
}

// Handler is the HTTP interface: the page, and a JSON API that needs the session token.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	web, _ := fs.Sub(assets.Web, "web")
	files := http.FileServer(http.FS(web))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Frame-Options", "DENY")
		files.ServeHTTP(w, r)
	})
	mux.HandleFunc("GET /api/state", s.api(s.handleState))
	mux.HandleFunc("POST /api/run", s.api(s.handleRun))
	mux.HandleFunc("POST /api/skip", s.api(s.handleSkip))
	mux.HandleFunc("GET /api/job", s.api(s.handleJob))
	mux.HandleFunc("POST /api/quit", s.api(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, core.M{"ok": true})
		go func() { time.Sleep(300 * time.Millisecond); s.Stop() }()
	}))
	return mux
}

// Stop ends the wizard (cancelling a running step).
func (s *Server) Stop() {
	s.quitOnce.Do(func() {
		s.cancel()
		close(s.Quit)
	})
}

// api checks the session token and that the request came for this server's own address (so another web
// page can't drive the installer).
func (s *Server) api(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Setup-Token") != s.Token {
			writeJSON(w, 403, core.M{"error": "This page has expired - open the link the setup program printed."})
			return
		}
		if o := r.Header.Get("Origin"); o != "" && !strings.HasSuffix(o, "//"+r.Host) {
			writeJSON(w, 403, core.M{"error": "wrong origin"})
			return
		}
		h(w, r)
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

// ---------------------------------------------------------------------------------------------- state

type stepView struct {
	ID       string        `json:"id"`
	Title    string        `json:"title"`
	Group    string        `json:"group"`
	Optional bool          `json:"optional"`
	Status   string        `json:"status"` // todo | done | skipped | failed | running
	Message  string        `json:"message,omitempty"`
	Blocked  string        `json:"blocked,omitempty"`
	Intro    string        `json:"intro"`
	After    string        `json:"after"`
	Fields   []steps.Field `json:"fields"`
	Links    []steps.Link  `json:"links,omitempty"`
	Button   string        `json:"button"`
	Password bool          `json:"password"` // ask for the admin password with this step
}

func (s *Server) ctxFor(j *core.Job, in map[string]string) *steps.Ctx {
	st := s.Stack()
	var p platform.Platform
	if st != nil {
		p = platform.For(st)
	}
	return &steps.Ctx{Context: s.ctx, St: st, P: p, J: j, In: in, Def: s.def, SetStack: s.setStack}
}

func (s *Server) status(id string) (string, string) {
	st := s.Stack()
	s.mu.Lock()
	running := s.running
	welcome := s.welcome
	s.mu.Unlock()
	if running != nil && running.Step == id {
		if lines, done, _ := running.Since(0); !done {
			_ = lines
			return "running", ""
		}
	}
	if st != nil {
		if ss := st.StepResult(id); ss != nil {
			return ss.Status, ss.Message
		}
	}
	if id == "welcome" && welcome != "" {
		return welcome, ""
	}
	return "todo", ""
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	st := s.Stack()
	goos := runtime.GOOS
	ctx := s.ctxFor(nil, nil)
	var views []stepView
	for _, step := range steps.For(goos) {
		status, msg := s.status(step.ID)
		v := stepView{ID: step.ID, Title: step.Title, Group: step.Group, Optional: step.Optional, Status: status,
			Message: msg, Button: step.Button}
		v.Intro, v.After = steps.RenderGuide(step.ID, st, s.def.Root)
		if blocked := s.blockedBy(step); blocked != "" {
			v.Blocked = blocked
		}
		if step.Fields != nil && (st != nil || step.ID == "welcome" || step.ID == "folders") {
			v.Fields = safeFields(step, ctx)
		}
		if step.Links != nil && st != nil && status == "done" {
			v.Links = step.Links(st)
		}
		v.Password = step.Password && st != nil && st.AdminPass == "" && st.AdminUser != ""
		views = append(views, v)
	}
	out := core.M{"os": goos, "version": assets.Version, "steps": views}
	if st != nil {
		out["root"] = st.Root
		out["name"] = st.Name
		out["addresses"] = steps.Addresses(st)
		if st.PortOffset != 0 {
			out["test"] = fmt.Sprintf("Test install: ports +%d", st.PortOffset)
		}
	}
	s.mu.Lock()
	if s.running != nil {
		if _, done, _ := s.running.Since(0); !done {
			out["job"] = core.M{"id": s.running.ID, "step": s.running.Step}
		}
	}
	s.mu.Unlock()
	writeJSON(w, 200, out)
}

func safeFields(step *steps.Step, c *steps.Ctx) (f []steps.Field) {
	defer func() {
		if r := recover(); r != nil {
			f = nil
		}
	}()
	return step.Fields(c)
}

// blockedBy names unfinished prerequisite steps.
func (s *Server) blockedBy(step *steps.Step) string {
	var missing []string
	for _, n := range step.Needs {
		if st, _ := s.status(n); st != "done" {
			if ns := steps.Find(n); ns != nil {
				missing = append(missing, ns.Title)
			}
		}
	}
	return strings.Join(missing, ", ")
}

// ---------------------------------------------------------------------------------------------- run / skip

type runReq struct {
	Step     string            `json:"step"`
	Values   map[string]string `json:"values"`
	Password string            `json:"password"`
}

func (s *Server) handleRun(w http.ResponseWriter, r *http.Request) {
	var req runReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, core.M{"error": "bad request"})
		return
	}
	step := steps.Find(req.Step)
	if step == nil {
		writeJSON(w, 404, core.M{"error": "no such step"})
		return
	}
	if b := s.blockedBy(step); b != "" {
		writeJSON(w, 409, core.M{"error": "Finish these steps first: " + b})
		return
	}
	st := s.Stack()
	if st == nil && step.ID != "welcome" && step.ID != "folders" {
		writeJSON(w, 409, core.M{"error": "Choose a folder first."})
		return
	}
	if step.Password && st != nil && st.AdminPass == "" {
		if req.Password == "" {
			writeJSON(w, 409, core.M{"error": "Enter your admin password."})
			return
		}
		if err := s.checkPassword(st, req.Password); err != nil {
			writeJSON(w, 409, core.M{"error": err.Error()})
			return
		}
		st.AdminPass = req.Password
	}
	s.mu.Lock()
	if s.running != nil {
		if _, done, _ := s.running.Since(0); !done {
			s.mu.Unlock()
			writeJSON(w, 409, core.M{"error": "Another step is still running."})
			return
		}
	}
	job := core.NewJob(step.ID, os.Stdout)
	s.jobs[job.ID] = job
	s.running = job
	s.mu.Unlock()

	if req.Values == nil {
		req.Values = map[string]string{}
	}
	go s.execute(step, job, req.Values)
	writeJSON(w, 200, core.M{"job": job.ID})
}

func (s *Server) execute(step *steps.Step, job *core.Job, values map[string]string) {
	var err error
	func() {
		defer func() {
			if r := recover(); r != nil {
				err = fmt.Errorf("internal error: %v", r)
				fmt.Fprintf(os.Stderr, "%v\n%s\n", r, debug.Stack())
			}
		}()
		c := s.ctxFor(job, values)
		err = step.Run(c)
	}()
	st := s.Stack()
	switch {
	case err == nil:
		if st != nil {
			st.SetStep(step.ID, "done", "")
			if st.IsDeployed("mediasorter") {
				// The hub and the health check learn about apps added after them.
				if werr := steps.WriteAppConfig(st); werr != nil {
					job.Warn("Couldn't update the Media Hub's settings: %v", werr)
				}
			}
		} else if step.ID == "welcome" {
			s.mu.Lock()
			s.welcome = "done"
			s.mu.Unlock()
		}
	default:
		msg := err.Error()
		if errors.Is(err, core.ErrNeedsUser) {
			msg = strings.TrimPrefix(msg, core.ErrNeedsUser.Error()+": ")
			err = errors.New(msg)
		}
		if st != nil {
			st.SetStep(step.ID, "failed", msg)
		} else if step.ID == "welcome" {
			s.mu.Lock()
			s.welcome = "failed"
			s.mu.Unlock()
		}
	}
	job.Finish(err)
}

func (s *Server) checkPassword(st *core.Stack, pass string) error {
	if !st.Done("jellyfin") {
		return nil // nothing to check it against yet
	}
	if _, _, err := steps.JellyfinLogin(st, st.AdminUser, pass); err != nil {
		return fmt.Errorf("that isn't the admin password for %s (Jellyfin rejected it)", st.AdminUser)
	}
	return nil
}

func (s *Server) handleSkip(w http.ResponseWriter, r *http.Request) {
	var req runReq
	json.NewDecoder(r.Body).Decode(&req)
	step := steps.Find(req.Step)
	st := s.Stack()
	if step == nil || st == nil {
		writeJSON(w, 400, core.M{"error": "can't skip that"})
		return
	}
	if !step.Optional {
		writeJSON(w, 400, core.M{"error": "This step is needed by later ones."})
		return
	}
	st.SetStep(step.ID, "skipped", "")
	writeJSON(w, 200, core.M{"ok": true})
}

// handleJob long-polls a job's log: returns new lines as soon as there are any (or after 20 s).
func (s *Server) handleJob(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	job := s.jobs[r.URL.Query().Get("id")]
	s.mu.Unlock()
	if job == nil {
		writeJSON(w, 404, core.M{"error": "no such job"})
		return
	}
	from := 0
	fmt.Sscan(r.URL.Query().Get("from"), &from)
	deadline := time.After(20 * time.Second)
	for {
		lines, done, wake := job.Since(from)
		if len(lines) > 0 || done {
			writeJSON(w, 200, core.M{"lines": lines, "done": done})
			return
		}
		select {
		case <-wake:
		case <-deadline:
			writeJSON(w, 200, core.M{"lines": []core.Line{}, "done": false})
			return
		case <-r.Context().Done():
			return
		}
	}
}

// ---------------------------------------------------------------------------------------------- serving

// Listen picks the wizard's address: 127.0.0.1 normally; every interface on a Linux server without a screen.
func Listen(addr string, port int) (net.Listener, error) {
	if addr == "" {
		addr = "127.0.0.1"
	}
	for p := port; p < port+20; p++ {
		ln, err := net.Listen("tcp", fmt.Sprintf("%s:%d", addr, p))
		if err == nil {
			return ln, nil
		}
	}
	return net.Listen("tcp", addr+":0")
}
