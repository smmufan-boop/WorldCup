"""Closing-line value on the snapshots: does it track realised ROI, and do IE1 rules beat the close?

Usage (repository root):
  python odds-trajectory/ie1/code/clv_backtest.py --cache fixtures.pkl --out odds-trajectory/ie1/CLV_REPORT.json

Same development/holdout split as backtest.py. The close is Royal's last
prematch Asian quote (and totals quote) before kickoff; see clv.py.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from multiprocessing import Pool
from pathlib import Path

import dataset
from backtest import SPLIT
from clv import clv, closing_model
from engine import first_tier, load_parameters, predict
from pricing import ah_outcomes, fit_market, score_matrix
from quotes import asian_decimal, totals_decimal
from settle import ah_profit


def one(fixture):
    params = load_parameters()
    book, ref = fixture["book"], fixture["book"]["3"]
    s = fixture["score"]
    if s is None or len(ref["asian"]) < 3 or not ref["totals"] or "3" not in fixture.get("close", {}):
        return None
    try:
        fair = fit_market(asian=asian_decimal(ref["asian"][-1]), totals=totals_decimal(ref["totals"][-1]),
                          rho=params["rho"])
        model = closing_model(fixture["close"]["3"], params["rho"], fair["total"])
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    grid = score_matrix(fair["lambdaHome"], fair["lambdaAway"], params["rho"])
    cut = fixture["kickoff"] - params["cutoffSeconds"]
    out = {"dev": fixture["kickoff"] < SPLIT, "firstTier": first_tier(fixture["competition"]), "bets": []}

    def add(kind, company, side, quote, extra=None):
        h, oh, oa = asian_decimal(quote)
        odds = oh if side == "home" else oa
        win, push = ah_outcomes(grid, h, side)
        c = clv({"market": "asian", "side": side, "line": h, "odds": odds}, model)
        out["bets"].append({"kind": kind, "company": company, "side": side, "ev": win * odds + push - 1,
                            "clv": c["clv"], "profit": ah_profit(s, h, side, odds),
                            "age": (cut - quote["ts"]) / 60, **(extra or {})})

    for side in ("home", "away"):
        add("royalT10", "3", side, ref["asian"][-1])
        macau = (book.get("1") or {}).get("asian")
        if macau:
            add("macauT10", "1", side, macau[-1])
    # Signal rule from SIGNAL_EXPLORATION: line never changed, follow the side whose water fell >= 0.05.
    a = ref["asian"]
    if all(q["home_handicap"] == a[0]["home_handicap"] for q in a):
        dh = a[-1]["home"] - a[0]["home"]
        if abs(dh) >= 0.05 - 1e-9:
            side = "home" if dh < 0 else "away"
            add("waterFollowRoyal", "3", side, a[-1])
            if (book.get("1") or {}).get("asian"):
                add("waterFollowMacau", "1", side, book["1"]["asian"][-1])
    meta = {k: fixture[k] for k in ("matchId", "competition", "home", "away", "kickoff")}
    try:
        res = predict(meta, book, params)
    except (ValueError, ZeroDivisionError, OverflowError):
        res = {}
    pick = res.get("pick")
    if pick and (res.get("decision") or {}).get("tier") in ("A", "B") and pick["market"] == "asian":
        c = clv(pick, model)
        out["bets"].append({"kind": "enginePick", "company": pick["company"], "side": pick["side"],
                            "ev": pick["ev"], "clv": c["clv"], "profit": ah_profit(s, pick["line"], pick["side"],
                                                                                  pick["odds"]),
                            "age": pick.get("ageMinutes"), "tier": res["decision"]["tier"]})
    return out


def stat(bets):
    n = len(bets)
    if not n:
        return {"n": 0}
    p = [b["profit"] for b in bets]
    c = [b["clv"] for b in bets]
    mp, mc = sum(p) / n, sum(c) / n
    sp = math.sqrt(sum((x - mp) ** 2 for x in p) / max(n - 1, 1))
    sc = math.sqrt(sum((x - mc) ** 2 for x in c) / max(n - 1, 1))
    w = sum(x > 0 for x in p)
    l = sum(x < 0 for x in p)
    return {"n": n, "meanEV": round(sum(b["ev"] for b in bets) / n, 4),
            "meanCLV": round(mc, 4), "clvSE": round(sc / math.sqrt(n), 4),
            "beatCloseRate": round(sum(x > 0 for x in c) / n, 4), "roi": round(mp, 4), "roiSE": round(sp / math.sqrt(n), 4),
            "winRate": round(w / max(w + l, 1), 4)}


def report(rows):
    splits = (("development", True), ("holdout", False))
    bets = lambda flag: [b for r in rows if r["dev"] == flag for b in r["bets"]]
    out = {"rows": len(rows)}
    # 1. Is CLV a valid yardstick? All T-10 quotes of both companies, by CLV bucket.
    edges = (-1, -0.06, -0.03, 0.0, 0.03, 0.06, 1)
    out["clvBuckets"] = {}
    for split, flag in splits:
        pool = [b for b in bets(flag) if b["kind"] in ("royalT10", "macauT10")]
        out["clvBuckets"][split] = {f"[{lo:+.2f},{hi:+.2f})": stat([b for b in pool if lo <= b["clv"] < hi])
                                    for lo, hi in zip(edges, edges[1:])}
    # 2. Rules.
    out["rules"] = {}
    for split, flag in splits:
        b = bets(flag)
        ft = [x for r in rows if r["dev"] == flag and r["firstTier"] for x in r["bets"]]
        out["rules"][split] = {
            "royalT10 (all sides)": stat([x for x in b if x["kind"] == "royalT10"]),
            "macauT10 (all sides)": stat([x for x in b if x["kind"] == "macauT10"]),
            "macau EV>=2%": stat([x for x in b if x["kind"] == "macauT10" and x["ev"] >= 0.02]),
            "macau EV>=2%, age<=120": stat([x for x in b if x["kind"] == "macauT10" and x["ev"] >= 0.02
                                            and x["age"] <= 120]),
            "macau EV>=2%, age>120": stat([x for x in b if x["kind"] == "macauT10" and x["ev"] >= 0.02
                                           and x["age"] > 120]),
            "macau EV>=4%, age<=120": stat([x for x in b if x["kind"] == "macauT10" and x["ev"] >= 0.04
                                            and x["age"] <= 120]),
            "engine picks (all competitions gate on)": stat([x for x in ft if x["kind"] == "enginePick"]),
            "waterFollow @Royal": stat([x for x in b if x["kind"] == "waterFollowRoyal"]),
            "waterFollow @Macau": stat([x for x in b if x["kind"] == "waterFollowMacau"]),
            "waterFollow @Macau age<=120": stat([x for x in b if x["kind"] == "waterFollowMacau" and x["age"] <= 120]),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    fixtures = dataset.build(a.cache)
    with Pool(a.workers) as pool:
        rows = [r for r in pool.map(one, fixtures, chunksize=50) if r]
    text = json.dumps(report(rows), ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
