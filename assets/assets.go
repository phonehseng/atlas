// Package assets carries the files the installer writes out: the MediaSorter / Media Hub Python apps and
// the setup wizard's web page.
package assets

import "embed"

// Version of Media Stack Setup (also tags the MediaSorter/Media Hub image on Linux).
const Version = "1.3.0"

// Python holds MediaSorter (mediasorter/) and the Media Hub (mediahub/).
//
//go:embed python
var Python embed.FS

// Web is the setup wizard's page.
//
//go:embed web
var Web embed.FS
