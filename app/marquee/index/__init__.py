"""Atlas's optional media index: a small local frame index per title for scene search, sensitive-content
ranges (blur/skip), chapter suggestions and intro/outro detection.

Everything heavy (numpy, ONNX Runtime, the CLIP model) is loaded only when used, and fetched on request when
missing, so importing this package is cheap and safe without them.

    from marquee.index import blueprint      # Flask blueprint with /api/index/...
    from marquee import index; index.touch(item_id)   # a title was opened or played: index it soon
"""
from . import deps

deps.add_lib_path()

from .api import bp as blueprint, worker  # noqa: E402
from .worker import load_config  # noqa: E402


_waiting = set()   # titles asked for while the index was still being installed


def touch(item_id, urgent=False):
    """Queue a title for indexing if the index is installed. When it isn't and someone explicitly asked for a
    scene (urgent), install it now (a one-time download) and index the title as soon as that's done. Returns
    its state, or {"state": "installing", ...} while the index downloads."""
    try:
        if deps.availability()["available"]:
            worker.queue(item_id, urgent=urgent)
            return worker.state(item_id)
        if urgent:
            import threading
            _waiting.add(item_id)
            if not deps.INSTALL.get("running"):
                def go():
                    deps.install()
                    if deps.availability()["available"]:
                        deps.add_lib_path()
                        for i in list(_waiting):
                            worker.queue(i, urgent=True)
                        _waiting.clear()
                threading.Thread(target=go, daemon=True, name="index-install").start()
            a = deps.availability()
            return {"state": "installing", "download_mb": a.get("download_mb"), "installing": a.get("installing")}
    except Exception as e:
        print("index:", e)
    return None


def sensitive_ranges(item_id):
    """For the player: [{start, end, category, action ('blur'|'skip')}] for categories not set to 'off', or
    None if the title isn't indexed (yet)."""
    from . import analysis, api, store
    cfg = load_config()["sensitive"]
    if not any(v != "off" for v in cfg.values()) or not deps.availability()["available"]:
        return []
    d = store.load(item_id.lower().replace("-", ""))
    if d is None or not d["meta"].get("complete"):
        return None
    d = api._with_sens(d)
    return [{**r, "action": cfg[r["category"]]} for r in analysis.sensitive_ranges(d) if cfg.get(r["category"]) != "off"]


def sensitive_settings():
    """{'violence'|'gore'|'nudity': 'off'|'blur'|'skip'}"""
    return load_config()["sensitive"]
