package core

import (
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"
)

// Line is one entry in a step's live log.
type Line struct {
	N    int    `json:"n"`
	T    int64  `json:"t"`
	Kind string `json:"kind"` // info | ok | warn | error | progress | done | fail | action
	Text string `json:"text"`
	Pct  int    `json:"pct,omitempty"`
}

// Job is one run of a wizard step; the browser follows it live.
type Job struct {
	ID   string
	Step string

	mu     sync.Mutex
	lines  []Line
	done   bool
	wake   chan struct{}
	mirror io.Writer
}

func NewJob(step string, mirror io.Writer) *Job {
	if mirror == nil {
		mirror = os.Stdout
	}
	return &Job{ID: RandomHex(8), Step: step, wake: make(chan struct{}), mirror: mirror}
}

func (j *Job) add(kind, text string, pct int) {
	j.mu.Lock()
	j.lines = append(j.lines, Line{N: len(j.lines), T: time.Now().Unix(), Kind: kind, Text: text, Pct: pct})
	close(j.wake)
	j.wake = make(chan struct{})
	j.mu.Unlock()
	if kind != "progress" {
		fmt.Fprintf(j.mirror, "[%s] %s\n", j.Step, text)
	}
}

func (j *Job) Info(format string, a ...any) { j.add("info", fmt.Sprintf(format, a...), 0) }
func (j *Job) OK(format string, a ...any)   { j.add("ok", fmt.Sprintf(format, a...), 0) }
func (j *Job) Warn(format string, a ...any) { j.add("warn", fmt.Sprintf(format, a...), 0) }

// Action tells the user to do something (e.g. approve a Windows prompt).
func (j *Job) Action(format string, a ...any) { j.add("action", fmt.Sprintf(format, a...), 0) }

// Progress shows a percentage for long downloads/builds.
func (j *Job) Progress(pct int, format string, a ...any) {
	j.add("progress", fmt.Sprintf(format, a...), pct)
}

// Finish ends the job with success or the error.
func (j *Job) Finish(err error) {
	if err != nil {
		j.add("fail", err.Error(), 0)
	} else {
		j.add("done", "Finished", 0)
	}
	j.mu.Lock()
	j.done = true
	j.mu.Unlock()
}

// Since returns lines from n on, whether the job is over, and a channel closed when more arrive.
func (j *Job) Since(n int) ([]Line, bool, <-chan struct{}) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if n < 0 {
		n = 0
	}
	var out []Line
	if n < len(j.lines) {
		out = append(out, j.lines[n:]...)
	}
	return out, j.done, j.wake
}

// Writer adapts the job to an io.Writer (for command output), one log line per text line.
func (j *Job) Writer(prefix string) io.Writer { return &jobWriter{j: j, prefix: prefix} }

type jobWriter struct {
	j      *Job
	prefix string
	buf    strings.Builder
}

func (w *jobWriter) Write(p []byte) (int, error) {
	for _, c := range string(p) {
		if c == '\n' || c == '\r' {
			if s := strings.TrimSpace(w.buf.String()); s != "" {
				w.j.Info("%s%s", w.prefix, s)
			}
			w.buf.Reset()
			continue
		}
		w.buf.WriteRune(c)
	}
	return len(p), nil
}
