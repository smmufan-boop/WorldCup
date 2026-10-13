"""Explore whether the M signals carry directional information on their own.

Direction comes from the signals, never from EV. Every rule's cut points and
follow/fade side are chosen on the development period (kickoff < 2026-09-01 BJT);
the holdout is only read back. Settlement is at the locked Royal T-10 quote
(and at Macau's T-10 quote where it exists), on verified 90-minute scores.

Usage (repository root):
  python odds-trajectory/ie1/code/explore_signals.py --cache fixtures.pkl --out explore.json
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from multiprocessing import Pool
from pathlib import Path

import dataset
from backtest import SPLIT, ols
from engine import first_tier, load_parameters
from pricing import devig_power, fit_market
from quotes import asian_decimal, at_or_before, euro_decimal, totals_decimal
from settle import ah_profit, total_profit
from signals import compute

SUP_FEATURES = ("M1", "M2", "M4", "M5", "lineSteps", "lastSegWaterHome", "lateLineSteps",
                "euroHomeShift", "euroDrawShift")
TOT_FEATURES = ("M3", "totLineSteps", "lastSegOverWater", "euroDrawShift")
FLAT = 0.03


def features(fixture):
    params = load_parameters()
    book, ref = fixture["book"], fixture["book"]["3"]
    a = ref["asian"]
    if fixture["score"] is None or len(a) < 3 or not ref["totals"]:
        return None
    try:
        sig = compute(book, fixture["kickoff"], params)["signals"]
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    s, cut = fixture["score"], fixture["kickoff"] - params["cutoffSeconds"]
    f = {k: sig[k]["raw"] for k in ("M1", "M2", "M3", "M4", "M5") if k in sig}
    # Raw line/water mechanics in home-favouring units (quarter steps).
    f["lineSteps"] = (a[0]["home_handicap"] - a[-1]["home_handicap"]) * 4
    seg_start = next(q for q in a if q["home_handicap"] == a[-1]["home_handicap"]
                     and all(p["home_handicap"] == a[-1]["home_handicap"] for p in a[a.index(q):]))
    f["lastSegWaterHome"] = -(a[-1]["home"] - seg_start["home"])  # home water falling = pro-home
    late = at_or_before(a, cut - params["lateWindowSeconds"])
    f["lateLineSteps"] = (late["home_handicap"] - a[-1]["home_handicap"]) * 4 if late else 0.0
    f["lineChanges"] = sum(1 for p, q in zip(a, a[1:]) if p["home_handicap"] != q["home_handicap"])
    f["quotes"] = len(a)
    t = ref["totals"]
    f["totLineSteps"] = (t[-1]["line"] - t[0]["line"]) * 4
    tseg = next(q for q in t if q["line"] == t[-1]["line"]
                and all(p["line"] == t[-1]["line"] for p in t[t.index(q):]))
    f["lastSegOverWater"] = -(t[-1]["over"] - tseg["over"])  # over water falling = pro-over
    e = ref["euro"]
    if len(e) >= 2:
        p0, _ = devig_power(euro_decimal(e[0]))
        p1, _ = devig_power(euro_decimal(e[-1]))
        f["euroHomeShift"] = (p1[0] - p1[2]) - (p0[0] - p0[2])
        f["euroDrawShift"] = p1[1] - p0[1]
    else:
        f["euroHomeShift"] = f["euroDrawShift"] = 0.0
    macau = (book.get("1") or {}).get("asian")
    h, oh, oa = asian_decimal(a[-1])
    tl, oo, ou = totals_decimal(t[-1])
    fav_home = h < 0 or (h == 0 and oh <= oa)
    out = {"dev": fixture["kickoff"] < SPLIT, "firstTier": first_tier(fixture["competition"]),
           "f": f, "favHome": fav_home, "line": h,
           "ahHome": ah_profit(s, h, "home", oh), "ahAway": ah_profit(s, h, "away", oa),
           "over": total_profit(s, tl, "over", oo), "under": total_profit(s, tl, "under", ou)}
    if macau:
        mh, moh, moa = asian_decimal(macau[-1])
        out.update(mHome=ah_profit(s, mh, "home", moh), mAway=ah_profit(s, mh, "away", moa),
                   mAge=(cut - macau[-1]["ts"]) / 60)
    # Favourite-relative screenshot vocabulary: 升盘/降盘 x 挺水/不挺水.
    fav_line = (-a[-1]["home_handicap"] if fav_home else a[-1]["home_handicap"])
    fav_open = (-a[0]["home_handicap"] if fav_home else a[0]["home_handicap"])
    dl = fav_line - fav_open
    side = "home" if fav_home else "away"
    dw = a[-1][side] - seg_start[side]
    out["pattern"] = ("升盘" if dl > 1e-9 else "降盘" if dl < -1e-9 else "盘不变") + \
                     ("·降水" if dw <= -FLAT else "·升水" if dw >= FLAT else "·水平")
    return out


def stat(profits):
    n = len(profits)
    if not n:
        return {"n": 0}
    w = sum(p > 0 for p in profits)
    l = sum(p < 0 for p in profits)
    m = sum(profits) / n
    sd = math.sqrt(sum((p - m) ** 2 for p in profits) / max(n - 1, 1))
    return {"n": n, "winRate": round(w / max(w + l, 1), 4), "roi": round(m, 4), "se": round(sd / math.sqrt(n), 4),
            "profit": round(sum(profits), 2)}


def pick(r, axis, sign, venue="royal"):
    if axis == "sup":
        if venue == "macau":
            return r.get("mHome") if sign > 0 else r.get("mAway")
        return r["ahHome"] if sign > 0 else r["ahAway"]
    return r["over"] if sign > 0 else r["under"]


def quintiles(rows, key, axis):
    """Quintile cut points from dev; per bucket, ROI of the pro-signal side."""
    dev = sorted(r["f"][key] for r in rows if r["dev"] and key in r["f"])
    if len(dev) < 500:
        return None
    cuts = [dev[int(len(dev) * q / 5)] for q in (1, 2, 3, 4)]

    def bucket(v):
        return sum(v > c for c in cuts)
    out = {"cuts": [round(c, 4) for c in cuts], "buckets": []}
    for b in range(5):
        row = {"bucket": b + 1}
        for split, flag in (("development", True), ("holdout", False)):
            sel = [r for r in rows if r["dev"] == flag and key in r["f"] and bucket(r["f"][key]) == b]
            row[split] = {"home_or_over": stat([pick(r, axis, +1) for r in sel]),
                          "away_or_under": stat([pick(r, axis, -1) for r in sel])}
        out["buckets"].append(row)
    return out


def extreme_rule(rows, key, axis):
    """Bet the dev-better side in the outer quintiles (follow or fade, chosen on dev)."""
    dev = sorted(r["f"][key] for r in rows if r["dev"] and key in r["f"])
    if len(dev) < 500:
        return None
    lo, hi = dev[int(len(dev) * 0.2)], dev[int(len(dev) * 0.8)]
    if lo == hi:
        return None

    def bets(sel, mode, venue="royal"):
        out = []
        for r in sel:
            v = r["f"].get(key)
            if v is None or lo < v < hi or v == lo == hi:
                continue
            sign = (1 if v >= hi else -1) * mode
            p = pick(r, axis, sign, venue)
            if p is not None:
                out.append(p)
        return out
    dev_rows = [r for r in rows if r["dev"]]
    follow, fade = stat(bets(dev_rows, +1)), stat(bets(dev_rows, -1))
    mode = 1 if follow["roi"] >= fade["roi"] else -1
    hold = [r for r in rows if not r["dev"]]
    res = {"rule": "follow" if mode > 0 else "fade", "low": round(lo, 4), "high": round(hi, 4),
           "development": follow if mode > 0 else fade, "holdout": stat(bets(hold, mode))}
    if axis == "sup":
        res["holdoutMacau"] = stat(bets(hold, mode, "macau"))
        res["holdoutFirstTier"] = stat(bets([r for r in hold if r["firstTier"]], mode))
    return res


def patterns(rows):
    out = {}
    for name in sorted({r["pattern"] for r in rows}):
        out[name] = {}
        for split, flag in (("development", True), ("holdout", False)):
            sel = [r for r in rows if r["dev"] == flag and r["pattern"] == name]
            fav = [r["ahHome"] if r["favHome"] else r["ahAway"] for r in sel]
            dog = [r["ahAway"] if r["favHome"] else r["ahHome"] for r in sel]
            out[name][split] = {"favourite": stat(fav), "underdog": stat(dog)}
    return out


def consensus(rows):
    """Follow when k of M1/M2/M4/M5 (|raw| >= 0.05 goals) point the same way."""
    out = {}
    for k in (2, 3, 4):
        res = {}
        for split, flag in (("development", True), ("holdout", False)):
            bets, mbets = [], []
            for r in rows:
                if r["dev"] != flag:
                    continue
                votes = [math.copysign(1, r["f"][m]) for m in ("M1", "M2", "M4", "M5")
                         if m in r["f"] and abs(r["f"][m]) >= 0.05]
                pos, neg = votes.count(1.0), votes.count(-1.0)
                if max(pos, neg) >= k and min(pos, neg) == 0:
                    sign = 1 if pos else -1
                    bets.append(pick(r, "sup", sign))
                    if "mHome" in r:
                        mbets.append(pick(r, "sup", sign, "macau"))
            res[split] = stat(bets)
            res[split + "Macau"] = stat(mbets)
        out[f">={k} agree, none against"] = res
    return out


def combined(rows, keys, axis):
    """OLS on dev goal residual proxy (+1 home/over win, -1 loss); holdout deciles."""
    target = (lambda r: (r["ahHome"] > 0) - (r["ahHome"] < 0)) if axis == "sup" else \
        (lambda r: (r["over"] > 0) - (r["over"] < 0))
    dev = [r for r in rows if r["dev"] and all(k in r["f"] for k in keys)]
    mean = {k: sum(r["f"][k] for r in dev) / len(dev) for k in keys}
    sd = {k: math.sqrt(sum((r["f"][k] - mean[k]) ** 2 for r in dev) / len(dev)) or 1.0 for k in keys}
    z = lambda r: [(r["f"][k] - mean[k]) / sd[k] for k in keys]
    beta, t = ols([z(r) for r in dev], [target(r) for r in dev])
    score = lambda r: sum(b * v for b, v in zip(beta[1:], z(r)))
    dev_scores = sorted(score(r) for r in dev)
    lo, hi = dev_scores[int(len(dev_scores) * 0.1)], dev_scores[int(len(dev_scores) * 0.9)]
    res = {"coefficients": {k: {"beta": round(b, 4), "t": round(tt, 2)} for k, b, tt in zip(keys, beta[1:], t[1:])}}
    for split, flag in (("development", True), ("holdout", False)):
        sel = [r for r in rows if r["dev"] == flag and all(k in r["f"] for k in keys)]
        bets = [pick(r, axis, 1 if score(r) >= hi else -1) for r in sel if score(r) >= hi or score(r) <= lo]
        res[split + "Top/BottomDecile"] = stat(bets)
        if axis == "sup":
            mb = [pick(r, axis, 1 if score(r) >= hi else -1, "macau") for r in sel
                  if (score(r) >= hi or score(r) <= lo) and "mHome" in r]
            res[split + "Macau"] = stat(mb)
    return res


def water_follow(rows):
    """Back the side whose Royal water fell on the final line segment (screenshot '挺水')."""
    out = {}
    for scope in ("anyLinePath", "lineNeverChanged"):
        for thr in (0.03, 0.05, 0.08, 0.12):
            res = {}
            for split, flag in (("development", True), ("holdout", False)):
                sel = [r for r in rows if r["dev"] == flag and abs(r["f"]["lastSegWaterHome"]) >= thr - 1e-9
                       and (scope == "anyLinePath" or r["f"]["lineChanges"] == 0)]
                side = lambda r, h, a: r[h] if r["f"]["lastSegWaterHome"] > 0 else r[a]
                res[split] = {"royal": stat([side(r, "ahHome", "ahAway") for r in sel]),
                              "macau": stat([side(r, "mHome", "mAway") for r in sel if "mHome" in r]),
                              "macauFresh120": stat([side(r, "mHome", "mAway") for r in sel
                                                     if "mHome" in r and r["mAge"] <= 120])}
            out[f"{scope}|water>={thr}"] = res
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache")
    ap.add_argument("--features", help="optional cache for computed features")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.features and Path(a.features).exists():
        rows = pickle.loads(Path(a.features).read_bytes())
    else:
        fixtures = dataset.build(a.cache)
        with Pool(a.workers) as pool:
            rows = [r for r in pool.map(features, fixtures, chunksize=50) if r]
        if a.features:
            Path(a.features).write_bytes(pickle.dumps(rows))
    report = {"rows": len(rows), "split": "development < 2026-09-01 BJT <= holdout",
              "baseline": {split: {k: stat([r[k] for r in rows if r["dev"] == flag])
                                   for k in ("ahHome", "ahAway", "over", "under")}
                           for split, flag in (("development", True), ("holdout", False))},
              "extremes": {}, "quintiles": {}}
    for key in SUP_FEATURES:
        report["extremes"][key] = extreme_rule(rows, key, "sup")
        report["quintiles"][key] = quintiles(rows, key, "sup")
    for key in TOT_FEATURES:
        report["extremes"]["total:" + key] = extreme_rule(rows, key, "tot")
        report["quintiles"]["total:" + key] = quintiles(rows, key, "tot")
    report["patterns"] = patterns(rows)
    report["consensus"] = consensus(rows)
    report["waterFollow"] = water_follow(rows)
    report["combinedSupremacy"] = combined(rows, SUP_FEATURES, "sup")
    report["combinedTotal"] = combined(rows, TOT_FEATURES, "tot")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
