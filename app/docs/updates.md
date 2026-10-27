# Updates

Atlas updates itself from the GitHub releases of `phonehseng/atlas` (setting `update_repo`; the old name
`phonehseng/marquee` is read as the new one, and GitHub redirects it too).

## Publishing a release

```powershell
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Version 1.4.0 -Notes notes\1.4.0.md
```

What it does:

1. Sets `__version__` in `marquee\__init__.py`.
2. Runs `build.ps1`, which writes `dist\Atlas\VERSION` and `dist\Atlas-windows-x64.zip`.
3. Writes `dist\release\v1.4.0\atlas-update.json` with the zip's SHA-256 and size, and the same file as
   `marquee-update.json` (the name installs from before the rename look for).
4. If this folder is a git repository, it commits the bump, tags `v1.4.0` (author Phone) and pushes.
5. Runs `gh release create v1.4.0` with the zip and both manifests.

Options:

| Option | What it does |
|---|---|
| `-DryRun` | Does everything except git and gh, and prints those commands. The source keeps its old version; `dist\` keeps the new build. |
| `-Draft` | Creates a draft release. Atlas only sees a release once it's published, because `releases/latest` skips drafts. |
| `-Repo owner/name` | Publishes to another repo. The default is `phonehseng/atlas`. |
| `-DistDir <dir>` | Uses (or, without `-NoBuild`, builds into) another folder than `dist\` (`build.ps1 -OutDir`). |
| `-Critical` | Marks the release as important: the pill turns orange and the update can't be skipped. |
| `-MinOs 10.0.17763` | Sets the oldest Windows build the release runs on. |
| `-InstallerDir <dir> [-InstallerVersion x.y.z] [-InstallerNotes f.md]` | Attaches the Media Stack Setup builds (`MediaStackSetup.exe`, `MediaStackSetup-linux-x64`, `MediaStackSetup-linux-arm64`). Adds the `installer` entry and the `stack-assets` entry, at the same version unless `-StackAssetsVersion` says otherwise. Without `-InstallerVersion`, the version comes from `MediaStackSetup.exe --version`. |
| `-Merge extra.json` | Merges more top-level entries into the manifest. |
| `-ExtraAssets a,b` | Uploads more files with the release. |

A version with a pre-release part (`1.5.0-beta.1`) is published as a GitHub pre-release. Only the beta channel offers it.

## Manifest schema (`atlas-update.json`, schema 1)

One file, read by both Atlas (`marquee/updater.py`) and Media Stack Setup (`internal/core/manifest.go`). The app's
entry is written twice with the same content: `atlas`, and `marquee` (its name before the rename, which Marquee
1.5.1 and older and Media Stack Setup 1.4 read). Readers take `atlas` and fall back to `marquee`.

```json
{
  "schema": 1,
  "published": "2026-10-26T12:00:00Z",
  "atlas": {
    "version": "1.4.0",
    "notes": "Short **markdown**: headings, - bullets, `code`, [links](https://...)",
    "published": "2026-10-26T12:00:00Z",
    "notes_url": "https://github.com/phonehseng/atlas/releases/tag/v1.4.0",
    "min_os": "10.0.17763",
    "critical": false,
    "files": {
      "windows-x64": {
        "name": "Atlas-windows-x64.zip",
        "url": "https://github.com/phonehseng/atlas/releases/download/v1.4.0/Atlas-windows-x64.zip",
        "sha256": "64 hex characters",
        "size": 75182846
      }
    }
  },
  "marquee": { "...": "the same as atlas" },
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

Each product entry (`atlas`/`marquee`, `installer`, `stack-assets`) has these fields:

| Field | Meaning |
|---|---|
| `version` | Required. |
| `notes` | Optional, short markdown. |
| `files` | Downloads by platform: `windows-x64`, `linux-x64` or `linux-arm64`, each `{url, sha256, size, name?}`. An entry with a single download may give `url`, `sha256` and `size` directly instead. |
| `published`, `notes_url`, `min_os`, `critical` | Atlas-only extras. Media Stack Setup ignores them. |
| `min_installer` | `stack-assets` only: the oldest installer that carries these assets. |

Rules:

- Atlas needs `atlas.version` (or `marquee.version`) plus a `windows-x64` download with a 64-hex `sha256` and an integer `size`. Without them it ignores the manifest. Media Stack Setup also requires `sha256` and `size`, and accepts only `https` URLs, except from a local test manifest.
- Versions follow semver. A pre-release such as `1.5.0-beta.1` sorts before `1.5.0`. A leading `v` is allowed.
- **Your stack:** Atlas compares the higher of `installer.version` and `stack-assets.version` with what the stack runs: `deployed.hub` or `deployed.mediasorter` in `<root>\stack.json`, where `<root>` comes from `mediastack.json`. If the manifest's version is newer, the "Your stack" page shows a quiet line. Its Update button:
  - downloads `installer.files["windows-x64"]` to Downloads;
  - checks its size and SHA-256;
  - starts `MediaStackSetup.exe --root <root> --update`, which opens "Update my stack".
- `atlas.notes` should stay short. The full text goes in the GitHub release body. `tools\release.ps1` cuts `notes` at 4000 characters.

## How Atlas checks

- It fetches `https://github.com/<update_repo>/releases/latest/download/atlas-update.json` (and
  `marquee-update.json` when that's missing, as on releases from before the rename):
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

1. **Before downloading:** Atlas checks it can write to its install folder and to the folder that holds it.
   - If it can't (for example Program Files), the sheet says to run Media Stack Setup and links the release page instead.
   - Running from source never self-updates.
2. **Download:**
   - The zip goes to `%APPDATA%\Marquee\updates\` and resumes with HTTP Range.
   - In auto mode it pauses while a video plays.
   - Atlas checks the size and SHA-256. If either doesn't match, it deletes the file and reports the error.
3. **Unpack:**
   - The zip unpacks into `<install>.new`, next to the install folder and on the same drive.
   - It must contain `Atlas.exe` (or `Marquee.exe`, a build from before the rename) and a `VERSION` file that
     matches the manifest, at the zip's top level or in one folder.
4. **Restart to update** (refused while a video plays):
   - Atlas copies `apply_update.ps1` into the updates folder and writes `updates\job.json`.
   - It starts the script in a hidden console (`powershell -ExecutionPolicy Bypass -WindowStyle Hidden`, CREATE_NO_WINDOW).
   - It closes through its normal closing path only once the script has written `updates\job.ack`. If the script never runs (for example, scripts are blocked by policy), Atlas stays open and says so.
5. **The script:**
   1. Waits up to 60 s for the Atlas process to exit.
   2. Renames `<install>` to `<install>.old`, then `<install>.new` to `<install>`.
   3. Starts `Atlas.exe` (or `Marquee.exe` when the folder only has that) with `--updated <old version>` and waits up to 60 s for `updates\heartbeat.json` with the new version. Atlas writes it once its window shows and the player is attached.
   4. **On success:** writes `updates\result.json`, deletes `.old`, and Atlas shows "Updated to 1.4.0 ✓" with "What's new".
   5. **On failure** (no heartbeat, or the new exe exits): stops it, puts `.old` back, starts the old version with `--update-failed <version>` and deletes the failed copy. Auto mode won't download that version again by itself.
   6. Writes a log to `updates\update.log`.

Settings, the catalog and WebView2's data (`storage_path`) all live in `%APPDATA%\Marquee` (or `MARQUEE_HOME`), never in the install folder, so swapping the folder keeps everything.

### Why a PowerShell script

- Windows PowerShell 5.1 is part of every Windows 10 and 11. The script adds about 5 KB to the build, has no second PyInstaller build to keep in step, and can be read in full.
- It runs as a copy in the updates folder, so it never holds the install folder open while it renames it.
- `-ExecutionPolicy Bypass` applies to that one process only.
- Machines that block PowerShell scripts by policy fall back to the installer, as do non-writable folders.

## The rename from Marquee to Atlas

Marquee became Atlas after version 1.5.1: the exe is `Atlas.exe`, the zip `Atlas-windows-x64.zip`, the manifest
`atlas-update.json` and the repo `phonehseng/atlas`. What stayed the same so nothing of the user's moves: the data
folder `%APPDATA%\Marquee` (and `MARQUEE_HOME`, all `MARQUEE_*` variables), the Python package `marquee`, the local
API's `local-api.json` and `X-Marquee-Token` (`X-Atlas-Token` and `atlas-local-api.json` work too).

Installs of Marquee 1.5.1 and older update to Atlas on their own, because the transition release keeps what they
look for:

- they fetch `marquee-update.json` from the old repo name (GitHub redirects it to `phonehseng/atlas`), so every
  release keeps publishing it (`tools\release.ps1` does);
- they read the manifest's `marquee` entry, so it stays (same content as `atlas`);
- their updater checks the zip for `Marquee.exe` (at the top level or in a `Marquee\` folder) and restarts
  `Marquee.exe`, so the build also carries `Marquee.exe`, a copy of `Atlas.exe` that runs the same app.

**For the release after the transition:** build with `build.ps1 -NoLegacyExe` to leave the extra `Marquee.exe`
out. Installs that already run the transition release update fine without it (their updater looks for `Atlas.exe`
first). Installs still on 1.5.1 or older can't take a build without `Marquee.exe`: they need the transition
release first, or Media Stack Setup. Shortcuts that still point at `Marquee.exe` stop working once it's gone:
Media Stack Setup's "Update my stack" moves `apps\Marquee` to `apps\Atlas` and replaces the Marquee shortcuts
with Atlas ones; shortcuts made by hand need pointing at `Atlas.exe`.
