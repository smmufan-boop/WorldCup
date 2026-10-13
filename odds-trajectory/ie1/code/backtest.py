"""IE1 T-10 backtest on the published Asian + euro/totals snapshots.

Usage (repository root):
  python odds-trajectory/ie1/code/backtest.py --cache fixtures.pkl --out report.json

Split predeclared before any result was read:
  development = kickoff (BJT) before 2026-09-01; holdout = 2026-09-01 onwards.
Steps:
  1. Fair market at T-10 from Royal Asian + totals; calibration check.
  2. Per-signal study on development: OLS of goal residuals on signal values.
  3. Coefficients with |t| >= 2 on development are kept, others set to 0.
  4. The full engine with those coefficients is run once on the holdout and
     settled at the locked Royal/Macau T-10 quote.
All results are retrospective: the data were collected after the matches, so
they cannot prove the quotes were available at the historical cutoff.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime
from multiprocessing import Pool

import dataset
from engine import first_tier, load_parameters, predict
from pricing import ah_outcomes, fit_market, no_push_probability, score_matrix, total_outcomes
from quotes import BJT, asian_decimal, totals_decimal
from settle import ah_profit, pick_profit, total_profit
from signals import compute

SPLIT = datetime(2026, 9, 1, tzinfo=BJT).timestamp()
SIGNALS = ("M1", "M2", "M3", "M4", "M5")


def candidate_profit(c, score):
    return pick_profit(c["market"], c["side"], c["line"], c["odds"], score)


def study_row(fixture):
    params = load_parameters()
    book, ref = fixture["book"], fixture["book"]["3"]
    if fixture["score"] is None or len(ref["asian"]) < 3 or not ref["totals"]:
        return None
    try:
        fair = fit_market(asian=asian_decimal(ref["asian"][-1]), totals=totals_decimal(ref["totals"][-1]),
                          rho=params["rho"])
        sig = compute(book, fixture["kickoff"], params)["signals"]
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    grid = score_matrix(fair["lambdaHome"], fair["lambdaAway"], fair["rho"])
    h, oh, oa = asian_decimal(ref["asian"][-1])
    t, oo, ou = totals_decimal(ref["totals"][-1])
    s = fixture["score"]
    cross = []
    other = book.get("1") or {}
    if other.get("asian"):
        mh, moh, moa = asian_decimal(other["asian"][-1])
        for side, odds in (("home", moh), ("away", moa)):
            win, push = ah_outcomes(grid, mh, side)
            cross.append({"market": "asian", "ev": win * odds + push - 1,
                          "profit": ah_profit(s, mh, side, odds), "sameLine": abs(mh - h) < 1e-9})
    if other.get("totals"):
        mt, moo, mou = totals_decimal(other["totals"][-1])
        for side, odds in (("over", moo), ("under", mou)):
            win, push = total_outcomes(grid, mt, side)
            cross.append({"market": "totals", "ev": win * odds + push - 1,
                          "profit": total_profit(s, mt, side, odds), "sameLine": abs(mt - t) < 1e-9})
    return {"cross": cross, "matchId": fixture["matchId"], "kickoff": fixture["kickoff"], "dev": fixture["kickoff"] < SPLIT,
            "firstTier": first_tier(fixture["competition"]),
            "fairSup": fair["supremacy"], "fairTotal": fair["total"], "fitError": fair["fitError"],
            "pHome": no_push_probability(*ah_outcomes(grid, h, "home")),
            "pOver": no_push_probability(*total_outcomes(grid, t, "over")),
            "supResid": (s[0] - s[1]) - fair["supremacy"], "totResid": (s[0] + s[1]) - fair["total"],
            "ahHome": ah_profit(s, h, "home", oh), "ahAway": ah_profit(s, h, "away", oa),
            "over": total_profit(s, t, "over", oo), "under": total_profit(s, t, "under", ou),
            "signals": {k: v["value"] for k, v in sig.items() if "value" in v},
            "axes": {k: v["axis"] for k, v in sig.items() if "axis" in v}}


def ols(xs, ys):
    """Multivariate OLS with intercept via normal equations; returns (beta, t)."""
    k = len(xs[0]) + 1
    X = [[1.0] + list(x) for x in xs]
    xtx = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * y for r, y in zip(X, ys)) for i in range(k)]
    inv = _invert(xtx)
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    resid = [y - sum(b * v for b, v in zip(beta, r)) for r, y in zip(X, ys)]
    sigma2 = sum(e * e for e in resid) / max(len(ys) - k, 1)
    t = [beta[i] / math.sqrt(sigma2 * inv[i][i]) if inv[i][i] > 0 else 0.0 for i in range(k)]
    return beta, t


def _invert(m):
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        piv = a[c][c]
        if abs(piv) < 1e-12:
            raise ValueError("Singular design matrix")
        a[c] = [v / piv for v in a[c]]
        for r in range(n):
            if r != c:
                f = a[r][c]
                a[r] = [v - f * w for v, w in zip(a[r], a[c])]
    return [row[n:] for row in a]


def summary(profits):
    n = len(profits)
    if not n:
        return {"n": 0}
    mean = sum(profits) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in profits) / max(n - 1, 1))
    return {"n": n, "profit": round(sum(profits), 2), "roi": round(mean, 4),
            "se": round(sd / math.sqrt(n), 4), "winRate": round(sum(p > 0 for p in profits) /
                                                                 max(sum(p != 0 for p in profits), 1), 4)}


def calibration(rows):
    out = {}
    for name, sel in (("development", [r for r in rows if r["dev"]]), ("holdout", [r for r in rows if not r["dev"]])):
        ah = [(r["pHome"], 1.0 if r["ahHome"] > 0 else 0.0) for r in sel if r["ahHome"] != 0]
        ov = [(r["pOver"], 1.0 if r["over"] > 0 else 0.0) for r in sel if r["over"] != 0]
        out[name] = {
            "n": len(sel),
            "meanSupResidual": round(sum(r["supResid"] for r in sel) / len(sel), 4),
            "meanTotalResidual": round(sum(r["totResid"] for r in sel) / len(sel), 4),
            "asianHomeBrier": round(sum((p - y) ** 2 for p, y in ah) / len(ah), 4),
            "asianHomeMeanP": round(sum(p for p, _ in ah) / len(ah), 4),
            "asianHomeHitRate": round(sum(y for _, y in ah) / len(ah), 4),
            "overBrier": round(sum((p - y) ** 2 for p, y in ov) / len(ov), 4),
            "overMeanP": round(sum(p for p, _ in ov) / len(ov), 4),
            "overHitRate": round(sum(y for _, y in ov) / len(ov), 4),
            "baseline": {"ahHome": summary([r["ahHome"] for r in sel]), "ahAway": summary([r["ahAway"] for r in sel]),
                         "over": summary([r["over"] for r in sel]), "under": summary([r["under"] for r in sel])},
        }
    return out


def signal_study(rows):
    dev = [r for r in rows if r["dev"]]
    hold = [r for r in rows if not r["dev"]]
    single, buckets = {}, {}
    for k in SIGNALS:
        sel = [r for r in dev if k in r["signals"]]
        if len(sel) < 200:
            continue
        axis = next(r["axes"][k] for r in sel)
        y = "supResid" if axis == "supremacy" else "totResid"
        beta, t = ols([[r["signals"][k]] for r in sel], [r[y] for r in sel])
        single[k] = {"axis": axis, "n": len(sel), "beta": round(beta[1], 4), "t": round(t[1], 2)}
        pos, neg = ("ahHome", "ahAway") if axis == "supremacy" else ("over", "under")
        for split, data in (("development", dev), ("holdout", hold)):
            strong = [r for r in data if k in r["signals"] and abs(r["signals"][k]) >= 0.5]
            follow = [r[pos] if r["signals"][k] > 0 else r[neg] for r in strong]
            fade = [r[neg] if r["signals"][k] > 0 else r[pos] for r in strong]
            buckets.setdefault(k, {})[split] = {"follow": summary(follow), "fade": summary(fade)}
    fitted = {}
    for axis, y, keys in (("supremacy", "supResid", ("M2", "M4", "M5")), ("total", "totResid", ("M3",))):
        sel = [r for r in dev if all(k in r["signals"] for k in keys)]
        if len(sel) < 200:
            continue
        beta, t = ols([[r["signals"][k] for k in keys] for r in sel], [r[y] for r in sel])
        fitted[axis] = {"n": len(sel), "keys": list(keys),
                        "beta": [round(b, 4) for b in beta[1:]], "t": [round(v, 2) for v in t[1:]],
                        "intercept": round(beta[0], 4)}
    return {"single": single, "strongSignalBets": buckets, "joint": fitted}


def cross_book(rows):
    """Royal fair price applied to Macau's T-10 quote: EV buckets and realised ROI."""
    out = {}
    for split, sel in (("development", [r for r in rows if r["dev"]]), ("holdout", [r for r in rows if not r["dev"]])):
        for market in ("asian", "totals"):
            cands = [c for r in sel for c in r["cross"] if c["market"] == market]
            out.setdefault(market, {})[split] = {
                f"ev>={lo:.0%}": summary([c["profit"] for c in cands if c["ev"] >= lo])
                for lo in (-1.0, 0.0, 0.02, 0.04, 0.06)}
            out[market][split]["ev>=-100%"]["note"] = "all Macau quotes"
    return out


def fitted_parameters(study):
    params = load_parameters()
    for name, joint in study["joint"].items():
        key = "betaSupremacy" if name == "supremacy" else "betaTotal"
        for k, b, t in zip(joint["keys"], joint["beta"], joint["t"]):
            params["signals"][k][key] = round(b, 4) if abs(t) >= 2 else 0.0
    params["status"] = "development_fitted_holdout_pending"
    return params


def _engine_one(args):
    fixture, params = args
    if fixture["score"] is None:
        return None
    meta = {k: fixture[k] for k in ("matchId", "competition", "home", "away", "kickoff")}
    try:
        res = predict(meta, fixture["book"], params)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    d = res.get("decision") or {}
    pick = res.get("pick")
    return {"tier": d.get("tier"), "blocked": bool(res.get("blocks")), "firstTier": first_tier(fixture["competition"]),
            "market": pick and pick["market"], "ev": pick and pick["ev"],
            "profit": candidate_profit(pick, fixture["score"]) if pick else None,
            "units": d.get("units", 0.0)}


def engine_holdout(fixtures, params, workers, gate_first_tier):
    params = copy.deepcopy(params)
    params["gates"]["requireFirstTier"] = gate_first_tier
    hold = [(f, params) for f in fixtures if f["kickoff"] >= SPLIT]
    with Pool(workers) as pool:
        res = [r for r in pool.map(_engine_one, hold, chunksize=50) if r]
    out = {"evaluated": len(res)}
    for tier in ("A", "B", "C"):
        sel = [r for r in res if r["tier"] == tier]
        out[tier] = summary([r["profit"] for r in sel])
        out[tier]["byMarket"] = {m: summary([r["profit"] for r in sel if r["market"] == m])
                                 for m in ("asian", "totals", "1x2")}
    ab = [r for r in res if r["tier"] in ("A", "B")]
    out["staked"] = {"units": sum(r["units"] for r in ab),
                     "profit": round(sum(r["units"] * r["profit"] for r in ab), 2)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    fixtures = dataset.build(a.cache)
    with Pool(a.workers) as pool:
        rows = [r for r in pool.map(study_row, fixtures, chunksize=50) if r]
    report = {"split": "development < 2026-09-01 BJT <= holdout", "fixtures": len(fixtures),
              "studyRows": len(rows), "calibration": calibration(rows), "signalStudy": signal_study(rows)}
    report["crossBook"] = cross_book(rows)
    params = fitted_parameters(report["signalStudy"])
    report["fittedSignals"] = params["signals"]
    report["engineHoldoutAll"] = engine_holdout(fixtures, params, a.workers, False)
    report["engineHoldoutFirstTier"] = engine_holdout(fixtures, params, a.workers, True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
