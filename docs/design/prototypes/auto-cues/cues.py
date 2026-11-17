"""Prototype: phrase-aware mix-in/mix-out cues for Segue.

Usage: python3 cues.py OUTDIR FILE [FILE ...]
Writes OUTDIR/<n>.png and OUTDIR/results.json.
"""
import json, os, subprocess, sys
import numpy as np
import librosa
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SR = 22050
HOP = 512


def load(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def fit_grid(y):
    """Constant-tempo beat grid: (period_s, first_beat_s, bpm)."""
    oenv = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    bpm = float(librosa.feature.tempo(onset_envelope=oenv, sr=SR, hop_length=HOP,
                                      start_bpm=124, max_tempo=180)[0])
    if bpm < 90:
        bpm *= 2
    times = librosa.frames_to_time(np.arange(len(oenv)), sr=SR, hop_length=HOP)
    fps = SR / HOP
    # fine search: tempo (+-3 %) and phase that put the most onset energy on one fixed grid
    best = (-1, None, None)
    for cand in np.arange(bpm * 0.97, bpm * 1.03, 0.01):
        p = 60.0 / cand
        n = int(times[-1] / p)
        for ph in np.arange(0, p, 0.01):
            g = ph + p * np.arange(n)
            sc = oenv[np.minimum((g * fps).astype(int), len(oenv) - 1)].sum()
            if sc > best[0]:
                best = (sc, p, ph)
    _, period, t0 = best
    first = t0 - period * np.floor(t0 / period)
    return period, first, 60.0 / period


def beat_features(y, grid):
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=2048)
    times = librosa.frames_to_time(np.arange(S.shape[1]), sr=SR, hop_length=HOP)
    mel = librosa.power_to_db(librosa.feature.melspectrogram(S=S ** 2, sr=SR, n_mels=48))
    bands = {"bass": (30, 120), "mid": (120, 2000), "high": (2000, 11000)}
    edges = np.searchsorted(times, grid)
    feats, en = [], {k: [] for k in bands}
    for a, b in zip(edges[:-1], edges[1:]):
        b = max(b, a + 1)
        feats.append(mel[:, a:b].mean(axis=1))
        for k, (lo, hi) in bands.items():
            m = (freqs >= lo) & (freqs < hi)
            en[k].append(10 * np.log10((S[m, a:b] ** 2).sum(axis=0).mean() + 1e-10))
    return np.array(feats), {k: np.array(v) for k, v in en.items()}


def novelty(F, w=16):
    F = (F - F.mean(0)) / (F.std(0) + 1e-6)
    F = F / (np.linalg.norm(F, axis=1, keepdims=True) + 1e-9)
    Sm = F @ F.T
    k = np.outer(np.r_[-np.ones(w), np.ones(w)], np.r_[-np.ones(w), np.ones(w)])
    k *= np.outer(np.hanning(2 * w), np.hanning(2 * w))
    n = len(F)
    pad = np.pad(Sm, w, mode="constant")
    nov = np.array([(pad[i:i + 2 * w, i:i + 2 * w] * k).sum() for i in range(n)])
    return np.maximum(nov, 0)


def analyse(path):
    y = load(path)
    dur = len(y) / SR
    period, first, bpm = fit_grid(y)
    grid = np.arange(first, dur, period)
    F, en = beat_features(y, np.r_[grid, dur])
    nb = len(grid)
    nov = novelty(F)

    loud = en["mid"] > np.percentile(en["mid"], 90) - 30
    first_sound = int(np.argmax(loud))
    last_sound = nb - 1 - int(np.argmax(loud[::-1]))


    # bars: Start is the first beat with sound; DJ tracks start on a phrase
    start_beat = first_sound
    bar_starts = np.arange(start_beat % 4, nb, 4)
    nbars = len(bar_starts)
    def bmean(x):
        return np.array([x[s:s + 4].mean() for s in bar_starts])
    bass, mid, high = bmean(en["bass"]), bmean(en["mid"]), bmean(en["high"])
    ref = np.percentile(bass, 80)
    full = bass > ref - 6
    kickless = bass < ref - 12
    z = lambda x: (x - np.median(x)) / (x.std() + 1e-6)
    fullness = z(bass) + 0.5 * z(mid) + 0.5 * z(high)
    novbar = np.array([nov[max(s - 2, 0):s + 3].max() for s in bar_starts])
    novbar = novbar / (novbar.max() + 1e-9)

    start_bar = int(np.searchsorted(bar_starts, start_beat))
    end_bar = int(np.searchsorted(bar_starts, last_sound, side="right") - 1)

    # phrase grid: which 4-bar offset from Start carries the structural changes
    offs = [novbar[start_bar + o:end_bar:4].mean() for o in range(4)]
    poff = int(np.argmax(offs)) if max(offs) > 1.3 * offs[0] else 0
    phrase_bar0 = start_bar + poff

    def snap4(bar):
        return int(round((bar - phrase_bar0) / 4) * 4 + phrase_bar0)

    cands = range(phrase_bar0, end_bar + 1, 4)
    def win(x, a, b):
        a, b = max(a, 0), min(b, nbars)
        return x[a:b].mean() if b > a else 0.0

    # mix in: first phrase where the track clearly "arrives" (bass and fullness step up)
    def arrive(b):
        return (win(full, b, b + 8) - win(full, b - 8, b)
                + 0.5 * (win(fullness, b, b + 8) - win(fullness, b - 8, b)) + 0.5 * novbar[b])
    ws = [b for b in cands if 8 <= b - start_bar <= 96]
    if ws:
        top = max(arrive(b) for b in ws)
        mix_in = next(b for b in ws if arrive(b) >= 0.9 * top)
    else:
        mix_in = start_bar

    # mix out: phrase where the track thins out for good (8-64 bars left)
    ws = [b for b in cands if 8 <= end_bar - b <= 48 and b > mix_in]
    def drop(b):
        # the track must not come back after the mix-out point
        after = max(win(fullness, a, a + 8) for a in range(b, max(end_bar - 7, b + 1)))
        return win(fullness, b - 8, b) - after + 0.5 * novbar[b]
    mix_out = max(ws, key=drop) if ws else end_bar

    # breakdowns: >= 8 bars without bass between mix in and mix out
    bds = []
    b = mix_in
    while b < mix_out:
        if kickless[b]:
            e = b
            while e < mix_out and not full[e]:
                e += 1
            if e - b >= 8:
                bds.append((snap4(b), snap4(e)))
            b = e
        b += 1

    def t(bar):
        bar = int(min(max(bar, 0), nbars - 1))
        return float(grid[bar_starts[bar]])

    cues = {
        "mixpoints": [("Start", start_bar), ("-16", mix_in - 16), ("-8", mix_in - 8), ("Mix in", mix_in),
                      ("Mix out", mix_out), ("+8", mix_out + 8), ("+16", mix_out + 16), ("End", end_bar)],
        "structure": [("Start", start_bar)] + sum(
            [[(f"Breakdown{'' if i == 0 else ' ' + str(i + 1)}", s),
              (f"Drop{'' if i == 0 else ' ' + str(i + 1)}", e)] for i, (s, e) in enumerate(bds[:2])], [])
        + [("Fade-out", mix_out)],
    }
    out = {}
    for name, lst in cues.items():
        keep = [(n, b) for n, b in lst if (n in ("Start", "End") or start_bar < b < end_bar - 2)]
        out[name] = [{"name": n, "bar": int(b - start_bar), "time": round(t(b), 3)} for n, b in keep]
    return {
        "file": os.path.basename(path), "duration": round(dur, 1), "bpm": round(bpm, 2),
        "first_beat": round(first, 3), "phrase_offset_bars": int(poff),
        "intro_bars": int(mix_in - start_bar), "outro_bars": int(end_bar - mix_out),
        "cues": out, "_plot": (y, grid, bar_starts, bass, ref, nov, start_bar, end_bar, t),
    }


def plot(r, png):
    y, grid, bar_starts, bass, ref, nov, sb, eb, t = r.pop("_plot")
    fig, ax = plt.subplots(2, 1, figsize=(16, 5.5), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1]})
    step = SR // 50
    env = np.abs(y[: len(y) // step * step]).reshape(-1, step).max(1)
    tt = np.arange(len(env)) * step / SR
    ax[0].fill_between(tt, -env, env, color="#8a8f98", lw=0)
    bt = grid[bar_starts]
    ax[0].fill_between(bt, -1.05, -0.95, where=bass > ref - 6, step="post", color="#d9a441")
    for i, b in enumerate(range(sb, eb + 1)):
        if (b - sb) % 8 == 0:
            ax[0].axvline(bt[b], color="#444", lw=0.5, alpha=0.6)
    colors = {"Start": "#e8d44d", "Mix in": "#3fb950", "Mix out": "#f85149", "End": "#a371f7"}
    for c in r["cues"]["mixpoints"]:
        col = colors.get(c["name"], "#58a6ff")
        ax[0].axvline(c["time"], color=col, lw=2)
        ax[0].text(c["time"], 1.02, f'{c["name"]}\n{c["bar"]}', color=col, fontsize=8,
                   ha="center", va="bottom")
    for c in r["cues"]["structure"]:
        if c["name"].startswith(("Breakdown", "Drop")):
            ax[0].axvline(c["time"], color="#ff7b72" if "Drop" in c["name"] else "#79c0ff",
                          lw=1, ls="--")
    ax[0].set_ylim(-1.1, 1.25)
    ax[0].set_yticks([])
    ax[0].set_title(f'{r["file"]}  ·  {r["bpm"]} BPM  ·  intro {r["intro_bars"]} bars  ·  outro {r["outro_bars"]} bars',
                    fontsize=10, loc="left", pad=28)
    ax[1].plot(grid[: len(nov)], nov / (nov.max() + 1e-9), color="#58a6ff", lw=0.8)
    ax[1].set_yticks([])
    ax[1].set_xlabel("seconds   (grey lines = every 8 bars from Start; gold strip = bass in)")
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)


if __name__ == "__main__":
    outdir = sys.argv[1]
    os.makedirs(outdir, exist_ok=True)
    res = []
    for i, f in enumerate(sys.argv[2:], 1):
        r = analyse(f)
        plot(r, os.path.join(outdir, f"{i}.png"))
        res.append(r)
        print(json.dumps(r, indent=1))
    json.dump(res, open(os.path.join(outdir, "results.json"), "w"), indent=1)
