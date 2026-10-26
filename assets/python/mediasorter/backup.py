"""Nightly backup of every app's settings and databases (not the media itself), and restore.
Part of Media Stack Setup.

  backup.py                       make a backup now (Windows: the "Backup" task runs this nightly;
                                  Linux: the Media Hub container does)
  backup.py list                  list the backups
  backup.py restore <zip> [apps]  put a backup back: stops those apps, copies their files back, starts them
                                  again (apps = comma-separated ids, default all; a safety backup of the
                                  current state is made first)

Everything under <folder>/config is covered (each app's settings folder), plus the settings Cleanuparr and
Requestrr keep next to their program on Windows. Backups go to <folder>/backups and the newest 7 are kept.
Databases are copied with SQLite's online backup, so the apps keep running while it works. Each zip has a
manifest.json that records where every file came from.
"""
import fnmatch
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stackconf  # noqa: E402

KEEP = 7
SETTINGS = ("*.xml", "*.json", "*.yml", "*.yaml", "*.ini", "*.conf", "*.toml", "*.db", "*.sqlite", "*.sqlite3",
            "*.key", "*.cfg", "*.jsonl")
SKIP_DIRS = {"logs", "log", "cache", "backups", "mediacover", "metadata", "transcodes", "bin", "asp", "sentry",
             "updatelogs", "sqlitebackups", "attachments", "resources", "definitions", ".venv", "node_modules",
             "geodb", "crashes", "camoufox"}
SKIP_FILES = ("logs.db", "*-wal", "*-shm", "*.log", "lockfile", "settings.old.json")
MAX_FILE = 500 * 2**20
EVERYTHING = {"BT_backup"}  # qBittorrent's torrents (.torrent + resume data): small and worth keeping whole


def places(cfg):
    """(app id, folder) pairs to back up, and where backups go, as this process sees them."""
    if stackconf.docker(cfg):
        root, dest = Path("/appconfig"), Path("/backups")
        extra = []
    else:
        root, dest = Path(cfg["root"]) / "config", Path(cfg["root"]) / "backups"
        # Apps that keep their settings next to the program.
        extra = [(a, Path(cfg["root"]) / "apps" / a / "config") for a in ("cleanuparr", "requestrr")]
    out = [(d.name, d) for d in sorted(root.iterdir()) if d.is_dir()] if root.is_dir() else []
    out += [(a, d) for a, d in extra if d.is_dir()]
    return out, dest


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def wanted(rel):
    parts = [p.lower() for p in Path(rel).parts]
    if any(p in {e.lower() for e in EVERYTHING} for p in parts[:-1]):
        return True
    if any(p in SKIP_DIRS for p in parts[:-1]) or any(fnmatch.fnmatch(parts[-1], p) for p in SKIP_FILES):
        return False
    return any(fnmatch.fnmatch(parts[-1], p) for p in SETTINGS)


def files_of(folder):
    for dirpath, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS or d in EVERYTHING]
        for n in names:
            p = Path(dirpath) / n
            rel = str(p.relative_to(folder))
            try:
                if wanted(rel) and p.stat().st_size <= MAX_FILE:
                    yield p, rel
            except OSError:
                pass


def make_backup(cfg, label="nightly"):
    sources, dest = places(cfg)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    target = dest / f"media-stack-{stamp}{'' if label == 'nightly' else '-' + label}.zip"
    tmp = target.with_suffix(".part")
    manifest = {"made": time.time(), "label": label, "name": cfg.get("name"), "files": []}
    problems = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z, tempfile.TemporaryDirectory() as td:
        for n, (app, folder) in enumerate(sources):
            for i, (path, rel) in enumerate(files_of(folder)):
                arc = f"{app}/{n}/{rel}".replace("\\", "/")
                try:
                    if is_sqlite(path):
                        copy = Path(td) / f"{n}-{i}.db"
                        src = sqlite3.connect(path, timeout=30)  # only read; opens read-only if it must
                        dst = sqlite3.connect(copy)
                        src.backup(dst)
                        dst.close()
                        src.close()
                        z.write(copy, arc)
                        copy.unlink()
                    else:
                        z.write(path, arc)
                    manifest["files"].append({"app": app, "archive": arc, "path": str(path)})
                except Exception as e:
                    problems.append(f"{path}: {e}")
        manifest["problems"] = problems
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    tmp.replace(target)
    nightly = sorted(dest.glob("media-stack-*.zip"))
    for old in nightly[:-KEEP]:
        if not old.stem.endswith("-before-restore") or old.stat().st_mtime < time.time() - 30 * 86400:
            old.unlink()
    return target, manifest


def list_backups(cfg):
    _, dest = places(cfg)
    out = []
    for z in sorted(dest.glob("media-stack-*.zip"), reverse=True) if dest.is_dir() else []:
        try:
            with zipfile.ZipFile(z) as f:
                m = json.loads(f.read("manifest.json"))
            apps = sorted({x["app"] for x in m["files"]})
        except Exception:
            m, apps = {}, []
        out.append({"name": z.name, "size_mb": round(z.stat().st_size / 2**20, 1), "made": m.get("made", z.stat().st_mtime),
                    "label": m.get("label", ""), "apps": apps, "problems": len(m.get("problems", []))})
    return out


# ---------------------------------------------------------------------------------------------- restore

def stop(cfg, app):
    meta = stackconf.app(cfg, app)
    if stackconf.docker(cfg):
        c = stackconf.containers(cfg).get(meta.get("service") or app)
        if c:
            stackconf.docker_api("POST", f"/containers/{c['id']}/stop?t=30")
    elif meta.get("task"):
        # Ending the task stops the program too (the background launcher closes it).
        stackconf.schtasks("/End", "/TN", stackconf.task_name(cfg, meta["task"]))


def start(cfg, app):
    meta = stackconf.app(cfg, app)
    if stackconf.docker(cfg):
        c = stackconf.containers(cfg).get(meta.get("service") or app)
        if c:
            stackconf.docker_api("POST", f"/containers/{c['id']}/start")
    elif meta.get("task"):
        stackconf.schtasks("/Run", "/TN", stackconf.task_name(cfg, meta["task"]))


def restore(cfg, name, only=None):
    _, dest = places(cfg)
    zip_path = dest / Path(name).name
    with zipfile.ZipFile(zip_path) as z:
        m = json.loads(z.read("manifest.json"))
        apps = sorted({f["app"] for f in m["files"] if not only or f["app"] in only})
        if stackconf.docker(cfg):
            apps = [a for a in apps if a != "hub"]  # the hub runs this; its own files aren't swapped under it
        safety, _ = make_backup(cfg, "before-restore")
        for app in apps:
            stop(cfg, app)
        time.sleep(5)
        restored, failed = 0, set()
        for f in m["files"]:
            if f["app"] not in apps or f["app"] in failed:
                continue
            target = Path(f["path"])
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                for side in ("-wal", "-shm"):  # stale write-ahead logs would be replayed over the restored DB
                    Path(str(target) + side).unlink(missing_ok=True)
                with z.open(f["archive"]) as src, open(target, "wb") as out:
                    out.write(src.read())
                restored += 1
            except OSError as e:
                failed.add(f["app"])
                print(f"couldn't restore {f['app']}: {e}")
        for app in apps:
            start(cfg, app)
    msg = (f"Restored {restored} files for {', '.join(a for a in apps if a not in failed)} from {zip_path.name} "
           f"(the previous state is saved as {safety.name})")
    if failed:
        msg += f". Not restored (still in use): {', '.join(sorted(failed))}"
    print(msg)
    stackconf.hub_hook(cfg, "Backup restored", msg, source="backup")
    return msg


def main():
    cfg = stackconf.load()
    args = sys.argv[1:]
    if args[:1] == ["list"]:
        for b in list_backups(cfg):
            print(f"{b['name']}  {b['size_mb']} MB  {len(b['apps'])} apps")
        return
    if args[:1] == ["restore"] and len(args) >= 2:
        restore(cfg, args[1], set(args[2].split(",")) if len(args) > 2 else None)
        return
    target, m = make_backup(cfg)
    apps = sorted({f["app"] for f in m["files"]})
    msg = f"{len(m['files'])} files from {len(apps)} apps, {target.stat().st_size / 2**20:.0f} MB"
    print("backup:", target, msg)
    for p in m["problems"]:
        print("couldn't back up:", p)
    stackconf.hub_hook(cfg, "Backup made" if not m["problems"] else f"Backup made, {len(m['problems'])} file(s) skipped",
                       msg, source="backup", attention=bool(m["problems"]))


if __name__ == "__main__":
    main()
