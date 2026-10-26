"""What the index is used for: scene search, sensitive-content ranges, chapter suggestions, intro/outro."""
import math


from .deps import np
from .frames import SAMPLE_EVERY

DARK = 14  # frames darker than this (0-255) are ignored for search, thumbnails and intro matching


def _emb(idx):
    return idx["emb"].astype(np.float32)


def _shot_start(cuts, t, within=6.0):
    """Where to jump for a hit at t: the start of its shot when that's close, else a second before."""
    i = np.searchsorted(cuts, t, side="right") - 1
    if i >= 0 and t - cuts[i] <= within:
        return float(cuts[i])
    return max(0.0, t - 1.0)


# ---------------------------------------------------------------- search
def search(idx, q, k=10, gap=12.0):
    """Frames most like the text embedding q -> [{t, start, score, z}], best first, at least `gap` s apart.
    z is how far above this title's typical frame the hit is (in standard deviations): about 3+ is a real
    match, below about 2 is a guess."""
    if not len(idx["t"]):
        return []
    s = _emb(idx) @ q
    s = np.maximum(s, 0.5 * (s + np.r_[s[1:], s[-1]]))  # a hit straddling two samples keeps its score
    mu, sd = float(s.mean()), float(s.std()) or 1e-6
    s_rank = np.where(idx["luma"] < DARK, -1.0, s)
    out = []
    for i in np.argsort(-s_rank):
        if len(out) >= k or s_rank[i] < 0:
            break
        t = float(idx["t"][i])
        if any(abs(t - o["t"]) < gap for o in out):
            continue
        out.append({"t": round(t, 2), "start": round(_shot_start(idx["cuts"], t), 2),
                    "score": round(float(s[i]), 4), "z": round((float(s[i]) - mu) / sd, 2)})
    return out


# ---------------------------------------------------------------- sensitive content
# Zero-shot: each frame's similarity to these descriptions, softmaxed against a broad set of ordinary scenes.
# A category's score is its share of the probability mass. Thresholds were set on real films and anime
# episodes (see tools/index_try.py): they favour missing a mild moment over flagging ordinary action.
SENSITIVE = {
    "violence": ["a violent fight with punches", "a person being shot with a gun", "a person being stabbed with a knife",
                 "a brutal beating", "a person being strangled", "an execution", "a person attacked with a sword",
                 "people killing each other"],
    "gore": ["a bloody wound", "a gory scene with lots of blood", "a corpse covered in blood", "a severed limb",
             "a mutilated body", "blood splattering", "an exploding head", "intestines and gore"],
    "nudity": ["a naked person", "a nude woman", "a naked body", "bare breasts", "a topless woman",
               "a sex scene", "naked people in bed", "explicit nudity"],
}
NEUTRAL = ["people talking", "a person's face", "a close-up of a face", "a person walking", "a city street",
           "a landscape", "the sky", "a room interior", "a car driving", "people eating", "an anime character",
           "a cartoon", "text on a screen", "the credits of a film", "a dark scene", "soldiers in a war",
           "a tank", "an explosion", "a gun", "a fight scene in an action movie", "people running", "a crowd",
           "a person in a bed sleeping", "a person in a swimsuit at the beach", "a shirtless man",
           "a woman in a dress", "a kiss", "red lights", "a red background", "a futuristic city at night",
           "a robot", "a computer screen", "an office", "a forest", "a house", "an animal", "a vehicle interior",
           "a person in underwear", "a hospital", "fire", "a red sunset", "a neon sign", "a person lying on a couch",
           "an elevator", "a colorful abstract pattern", "a person with cybernetic implants", "a sci-fi scene",
           "red light on a face", "a dark alley at night", "a person in pain", "an injured person with a bandage",
           "a soldier with a rifle", "a muddy battlefield", "a man without a shirt", "a man washing himself",
           "people holding hands", "a muscular man"]
THRESHOLDS = {"violence": 0.6, "gore": 0.7, "nudity": 0.5}
SENS_VERSION = 2


def sensitive_prompts():
    labels, texts = [], []
    for cat, ps in SENSITIVE.items():
        labels += [cat] * len(ps)
        texts += ps
    labels += [""] * len(NEUTRAL)
    return labels, texts + NEUTRAL


def sensitive_scores(emb, clip):
    """[N, C] scores in 0..1 for the categories in SENSITIVE (order of the dict)."""
    labels, texts = sensitive_prompts()
    P = clip.texts(texts)
    logits = 100.0 * (emb.astype(np.float32) @ P.T)
    logits -= logits.max(1, keepdims=True)
    p = np.exp(logits)
    p /= p.sum(1, keepdims=True)
    labels = np.array(labels)
    return np.stack([p[:, labels == c].sum(1) for c in SENSITIVE], 1).astype(np.float16)


def sensitive_ranges(idx, thresholds=None, pad=1.5, merge=6.0):
    """[{start, end, category, peak}] where a category's (lightly smoothed) score passes its threshold."""
    th = {**THRESHOLDS, **(thresholds or {})}
    t, out = idx["t"], []
    if not len(t) or not idx["sens_cats"]:
        return out
    cuts = idx["cuts"]
    for ci, cat in enumerate(idx["sens_cats"]):
        s = idx["sens"][:, ci].astype(np.float32)
        sm = np.convolve(s, [0.25, 0.5, 0.25], mode="same")
        hot = (sm >= th[cat]) | (s >= min(0.95, th[cat] + 0.25))  # a single very clear frame counts too
        hot &= idx["luma"] >= DARK // 2
        i, n = 0, len(t)
        ranges = []
        while i < n:
            if not hot[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and hot[j + 1]:
                j += 1
            ranges.append([float(t[i]), float(t[j]), float(s[max(0, i - 1):j + 2].max())])
            i = j + 1
        merged = []
        for r in ranges:
            if merged and r[0] - merged[-1][1] <= merge:
                merged[-1][1], merged[-1][2] = r[1], max(merged[-1][2], r[2])
            else:
                merged.append(r)
        for a, b, peak in merged:
            a, b = a - pad, b + pad
            # Start at the cut that begins the shot, if there's one just before.
            before = cuts[(cuts <= a + pad) & (cuts >= a - SAMPLE_EVERY)]
            if len(before):
                a = float(before[0])
            after = cuts[(cuts >= b - pad) & (cuts <= b + SAMPLE_EVERY)]
            if len(after):
                b = float(after[-1])
            out.append({"start": round(max(0.0, a), 2), "end": round(b, 2), "category": cat, "peak": round(peak, 3)})
    return sorted(out, key=lambda r: r["start"])


# ---------------------------------------------------------------- chapters
def chapter_count(duration):
    return int(min(15, max(8, round(math.sqrt(duration / 60) * 1.4))))


def chapters(idx, k=None, min_len=None):
    """Split the title into k contiguous chapters along scene cuts: shots are merged bottom-up (Ward's
    criterion on mean embeddings, adjacent shots only) until k remain. Each gets a representative time."""
    t, E = idx["t"], _emb(idx)
    n = len(t)
    if n < 4:
        return []
    duration = float(idx["meta"].get("duration") or t[-1] + SAMPLE_EVERY)
    k = k or chapter_count(duration)
    min_len = min_len if min_len is not None else min(60.0, duration / (k * 4))
    # Shots: frames between consecutive cuts.
    cut_idx = np.unique(np.searchsorted(t, idx["cuts"]))
    bounds = [0] + [int(c) for c in cut_idx if 0 < c < n] + [n]
    segs = [[a, b] for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    sums = [E[a:b].sum(0) for a, b in segs]
    cnt = [b - a for a, b in segs]

    target = len(t) / k

    def cost(i):  # Ward distance between segment i and i+1, growing as the merged chapter gets long
        mi, mj = sums[i] / cnt[i], sums[i + 1] / cnt[i + 1]
        ward = cnt[i] * cnt[i + 1] / (cnt[i] + cnt[i + 1]) * float(((mi - mj) ** 2).sum())
        return ward * (1 + ((cnt[i] + cnt[i + 1]) / target) ** 2)

    def merge(i):
        segs[i][1] = segs[i + 1][1]
        sums[i] = sums[i] + sums[i + 1]
        cnt[i] += cnt[i + 1]
        del segs[i + 1], sums[i + 1], cnt[i + 1]
        del costs[i]
        if i > 0:
            costs[i - 1] = cost(i - 1)
        if i < len(segs) - 1:
            costs[i] = cost(i)

    costs = [cost(i) for i in range(len(segs) - 1)]
    while len(segs) > k:
        merge(int(np.argmin(costs)))
    # No chapter shorter than min_len: fold it into its more similar neighbour.
    while len(segs) > 1:
        lens = [(t[b - 1] - t[a] + SAMPLE_EVERY) for a, b in segs]
        i = int(np.argmin(lens))
        if lens[i] >= min_len:
            break
        if i == 0:
            merge(0)
        elif i == len(segs) - 1:
            merge(i - 1)
        else:
            merge(i - 1 if costs[i - 1] < costs[i] else i)
    out = []
    cuts = idx["cuts"]
    for (a, b), sm in zip(segs, sums):
        m = sm / (b - a)
        sim = E[a:b] @ (m / (np.linalg.norm(m) or 1))
        sim[idx["luma"][a:b] < DARK * 2] -= 1  # prefer a picture you can see
        rep = a + int(np.argmax(sim))
        if a == 0:
            start = 0.0
        else:  # the cut that opens the chapter (between the previous sample and this one)
            c = cuts[(cuts > t[a - 1]) & (cuts <= t[a])]
            start = float(c[0]) if len(c) else float(t[a])
        out.append({"start": round(start, 2), "thumb_t": round(float(t[rep]), 2)})
    for i, c in enumerate(out):
        c["end"] = out[i + 1]["start"] if i + 1 < len(out) else round(duration, 2)
        c["name"] = f"Chapter {i + 1}"
    return out


# ---------------------------------------------------------------- intro / outro
def _runs(mask, max_gap):
    """[(i, j)] runs of True in mask, bridging gaps of up to max_gap False samples."""
    runs, i, n = [], 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j, last = i, i
        while j + 1 < n and (mask[j + 1] or (j + 1 - last) <= max_gap):
            j += 1
            if mask[j]:
                last = j
        runs.append((i, last))
        i = last + 1
    return runs


def recurring(idx, others, where, window=None, sim=0.88, anchor=0.93, min_len=30.0):
    """The longest stretch near the start ('intro') or end ('outro') of idx that also plays in at least one of
    `others` (the same season's other episodes), at one consistent time offset. A style alone (anime) makes
    many unrelated frames look alike; the same footage lines up frame after frame. -> {start, end, matches}."""
    t, E = idx["t"], _emb(idx)
    duration = float(idx["meta"].get("duration") or (t[-1] if len(t) else 0))
    if not len(t) or not others:
        return None

    def region(d, dur):
        w = window or (min(dur * 0.4, 600.0) if where == "intro" else min(dur * 0.3, 420.0))
        m = (d["t"] <= w) if where == "intro" else (d["t"] >= dur - w)
        return m & (d["luma"] >= DARK)

    mine = region(idx, duration)
    if mine.sum() < 5:
        return None
    hits = np.zeros(len(t), np.int32)
    Em, tm = E[mine], t[mine]
    for o in others:
        od = float(o["meta"].get("duration") or o["t"][-1])
        om = region(o, od)
        if om.sum() < 5:
            continue
        S = Em @ o["emb"][om].astype(np.float32).T
        to = o["t"][om]
        # The offset most of the clearly matching frames agree on (2 s bins, neighbours pooled).
        best = S.max(1)
        offs = np.round((to[S.argmax(1)] - tm)[best >= anchor] / SAMPLE_EVERY).astype(int)
        if len(offs) < 4:
            continue
        vals, cnt = np.unique(offs, return_counts=True)
        pooled = [cnt[np.abs(vals - v) <= 1].sum() for v in vals]
        k = int(np.argmax(pooled))
        if pooled[k] < 4:
            continue
        off = vals[k] * SAMPLE_EVERY
        near = np.abs(to[None, :] - (tm[:, None] + off)) <= SAMPLE_EVERY * 1.5
        hits[np.where(mine)[0]] += (np.where(near, S, 0).max(1) >= sim)
    matched = hits > 0
    runs = [(i, j) for i, j in _runs(matched, max_gap=5) if t[j] - t[i] + SAMPLE_EVERY >= min_len
            and matched[i:j + 1].mean() >= 0.5]
    if not runs:
        return None
    i, j = max(runs, key=lambda r: t[r[1]] - t[r[0]])
    a, b = float(t[i]) - SAMPLE_EVERY / 2, float(t[j]) + SAMPLE_EVERY / 2
    cuts = idx["cuts"]
    near = cuts[np.abs(cuts - a) <= SAMPLE_EVERY]
    if len(near):
        a = float(near[np.argmin(np.abs(near - a))])
    near = cuts[np.abs(cuts - b) <= SAMPLE_EVERY]
    if len(near):
        b = float(near[np.argmin(np.abs(near - b))])
    if where == "outro" and duration - b < 3 * SAMPLE_EVERY:
        b = duration
    return {"start": round(max(0.0, a), 2), "end": round(b, 2), "matches": int(hits[i:j + 1].max())}
