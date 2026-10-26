# Updates

Marquee updates itself from the GitHub releases of `phonehseng/marquee` (setting `update_repo`).

## Publishing a release

```powershell
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Version 1.4.0 -Notes notes\1.4.0.md
```

What it does:

1. Sets `__version__` in `marquee\__init__.py`.
2. Runs `build.ps1`, which writes `dist\Marquee\VERSION`.
3. Writes `dist\release\v1.4.0\marquee-update.json` with the zip's SHA-256 and size.
4. If this folder is a git repository, it commits the bump, tags `v1.4.0` (author Phone) and pushes.
5. Runs `gh release create v1.4.0` with the zip and the manifest.

Options:

| Option | What it does |
|---|---|
| `-DryRun` | Does everything except git and gh, and prints those commands. The source keeps its old version; `dist\` keeps the new build. |
| `-Draft` | Creates a draft release. Marquee only sees a release once it's published, because `releases/latest` skips drafts. |
| `-Repo owner/name` | Publishes to another repo. The default is `phonehseng/marquee`. |
| `-Critical` | Marks the release as important: the pill turns orange and the update can't be skipped. |
| `-MinOs 10.0.17763` | Sets the oldest Windows build the release runs on. |
| `-InstallerDir <dir> [-InstallerVersion x.y.z] [-InstallerNotes f.md]` | Attaches the Media Stack Setup builds (`MediaStackSetup.exe`, `MediaStackSetup-linux-x64`, `MediaStackSetup-linux-arm64`). Adds the `installer` entry and the `stack-assets` entry, at the same version unless `-StackAssetsVersion` says otherwise. Without `-InstallerVersion`, the version comes from `MediaStackSetup.exe --version`. |
| `-Merge extra.json` | Merges more top-level entries into the manifest. |
| `-ExtraAssets a,b` | Uploads more files with the release. |

A version with a pre-release part (`1.5.0-beta.1`) is published as a GitHub pre-release. Only the beta channel offers it.

## Manifest schema (`marquee-update.json`, schema 1)

One file, read by both Marquee (`marquee/updater.py`) and Media Stack Setup (`internal/core/manifest.go`):

```json
{
  "schema": 1,
  "published": "2026-10-26T12:00:00Z",
  "marquee": {
    "version": "1.4.0",
    "notes": "Short **markdown**: headings, - bullets, `code`, [links](https://...)",
    "published": "2026-10-26T12:00:00Z",
    "notes_url": "https://github.com/phonehseng/marquee/releases/tag/v1.4.0",
    "min_os": "10.0.17763",
    "critical": false,
    "files": {
      "windows-x64": {
        "name": "Marquee-windows-x64.zip",
        "url": "https://github.com/phonehseng/marquee/releases/download/v1.4.0/Marquee-windows-x64.zip",
        "sha256": "64 hex characters",
        "size": 75182846
      }
    }
  },
  "installer": {
    "version": "1.4.0",
    "notes": "...",
    "files": {
      "windows-x64": {"name": "MediaStackSetup.exe", "url": "...", "sha256": "...", "size": 0},
      "linux-x64":   {"name": "MediaStackSetup-linux-x64", "url": "...", "sha256": "...", "size": 0},
      "linux-arm64": {"name": "MediaStackSetup-linux-arm64", "url": "...", "sha256": "...", "size": 0}
    }
  },
  "stack-assets": {"version": "1.4.0", "min_installer": "1.4.0", "notes": "..."}
}
```

Each product entry (`marquee`, `installer`, `stack-assets`) has these fields:

| Field | Meaning |
|---|---|
| `version` | Required. |
| `notes` | Optional, short markdown. |
| `files` | Downloads by platform: `windows-x64`, `linux-x64` or `linux-arm64`, each `{url, sha256, size, name?}`. An entry with a single download may give `url`, `sha256` and `size` directly instead. |
| `published`, `notes_url`, `min_os`, `critical` | Marquee-only extras. Media Stack Setup ignores them. |
| `min_installer` | `stack-assets` only: the oldest installer that carries these assets. |

Rules:

- Marquee needs `marquee.version` plus a `windows-x64` download with a 64-hex `sha256` and an integer `size`. Without them it ignores the manifest. Media Stack Setup also requires `sha256` and `size`, and accepts only `https` URLs, except from a local test manifest.
- Versions follow semver. A pre-release such as `1.5.0-beta.1` sorts before `1.5.0`. A leading `v` is allowed.
- **Your stack:** Marquee compares the higher of `installer.version` and `stack-assets.version` with what the stack runs: `deployed.hub` or `deployed.mediasorter` in `<root>\stack.json`, where `<root>` comes from `mediastack.json`. If the manifest's version is newer, the "Your stack" page shows a quiet line. Its Update button:
  - downloads `installer.files["windows-x64"]` to Downloads;
  - checks its size and SHA-256;
  - starts `MediaStackSetup.exe --root <root> --update`, which opens "Update my stack".
- `marquee.notes` should stay short. The full text goes in the GitHub release body. `tools\release.ps1` cuts `notes` at 4000 characters.

## How Marquee checks

- It fetches `https://github.com/<update_repo>/releases/latest/download/marquee-update.json`:
  - 30 s after start, then every 12 h;
  - with a 10 s timeout and the ETag of the last answer (If-None-Match);
  - never while a video plays.
- A 401, 403 or 404 (private repo, or no release yet) is noted quietly as "Can't reach updates" in Settings > About and never shown as an error.
- On the beta channel, it looks through the newest releases (through the GitHub API), including pre-releases, for the newest one with a manifest.

Settings:

- `update_check`:
  - `auto` (default): downloads in the background, then shows "Restart to update";
  - `notify`: only shows the pill;
  - `off`: never checks on its own.
- `update_channel`: `stable` or `beta`.
- `update_skip`: the version the user chose to skip.

Users can also check by hand in Settings > About.

Development overrides:

- `MARQUEE_UPDATE_URL`: the manifest address, for example a local test server.
- `MARQUEE_UPDATE_FIRST_DELAY`: seconds before the first check.
- `MARQUEE_UPDATE_INSTALL_DIR`: the folder to treat as the install.

## How an update is applied

1. **Before downloading:** Marquee checks it can write to its install folder and to the folder that holds it.
   - If it can't (for example Program Files), the sheet says to run Media Stack Setup and links the release page instead.
   - Running from source never self-updates.
2. **Download:**
   - The zip goes to `%APPDATA%\Marquee\updates\` and resumes with HTTP Range.
   - In auto mode it pauses while a video plays.
   - Marquee checks the size and SHA-256. If either doesn't match, it deletes the file and reports the error.
3. **Unpack:**
   - The zip unpacks into `<install>.new`, next to the install folder and on the same drive.
   - It must contain `Marquee.exe` and a `VERSION` file that matches the manifest.
4. **Restart to update** (refused while a video plays):
   - Marquee copies `apply_update.ps1` into the updates folder and writes `updates\job.json`.
   - It starts the script in a hidden console (`powershell -ExecutionPolicy Bypass -WindowStyle Hidden`, CREATE_NO_WINDOW).
   - It closes through its normal closing path only once the script has written `updates\job.ack`. If the script never runs (for example, scripts are blocked by policy), Marquee stays open and says so.
5. **The script:**
   1. Waits up to 60 s for the Marquee process to exit.
   2. Renames `<install>` to `<install>.old`, then `<install>.new` to `<install>`.
   3. Starts `Marquee.exe --updated <old version>` and waits up to 60 s for `updates\heartbeat.json` with the new version. Marquee writes it once its window shows and the player is attached.
   4. **On success:** writes `updates\result.json`, deletes `.old`, and Marquee shows "Updated to 1.4.0 ✓" with "What's new".
   5. **On failure** (no heartbeat, or the new exe exits): stops it, puts `.old` back, starts the old version with `--update-failed <version>` and deletes the failed copy. Auto mode won't download that version again by itself.
   6. Writes a log to `updates\update.log`.

Settings, the catalog and WebView2's data (`storage_path`) all live in `%APPDATA%\Marquee` (or `MARQUEE_HOME`), never in the install folder, so swapping the folder keeps everything.

### Why a PowerShell script

- Windows PowerShell 5.1 is part of every Windows 10 and 11. The script adds about 5 KB to the build, has no second PyInstaller build to keep in step, and can be read in full.
- It runs as a copy in the updates folder, so it never holds the install folder open while it renames it.
- `-ExecutionPolicy Bypass` applies to that one process only.
- Machines that block PowerShell scripts by policy fall back to the installer, as do non-writable folders.
