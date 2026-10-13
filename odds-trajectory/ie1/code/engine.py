"""IE1 decision engine: fair price -> bounded signal adjustment -> EV -> gates.

Flow (all at the fixed decision cutoff, default T-10):
  1. Reference company (Royal) Asian + totals (+1X2) -> de-vigged fair market.
  2. Fit Dixon-Coles (total, supremacy); M1-M7 signals shift them within caps.
  3. Price every company's cutoff quote with the adjusted model -> EV.
  4. Gates (data quality, competition, signal agreement) -> tier A/B/C or 观望.
The output is a research record, not a calibrated betting promise.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from pricing import (ah_outcomes, expected_return, fit_market, lambdas, one_x_two,
                     score_matrix, total_outcomes)
from quotes import BJT, asian_decimal, euro_decimal, totals_decimal
from signals import adjustments, compute, votes

HERE = Path(__file__).resolve().parent
FIRST_TIER = HERE.parents[1] / "v1" / "rules" / "first-tier-competitions.json"


def load_parameters(path=HERE / "parameters.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def competition_label(name):
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = re.split(r"[·｜|]", text, maxsplit=1)[0]
    return "".join(text.split()).lower()


def first_tier(name):
    names = set(json.loads(FIRST_TIER.read_text(encoding="utf-8")))
    return competition_label(name) in names


def _candidates(book, model_grid, params):
    """Every company's cutoff quote priced by the adjusted model."""
    rows = []
    for company, b in book.items():
        cname = params["companies"].get(company, company)
        if b.get("asian"):
            h, oh, oa = asian_decimal(b["asian"][-1])
            for side, odds in (("home", oh), ("away", oa)):
                win, push = ah_outcomes(model_grid, h, side)
                rows.append({"company": company, "companyName": cname, "market": "asian", "side": side,
                             "line": h, "odds": odds, "ev": expected_return(win, push, odds),
                             "pWin": win, "pPush": push, "quoteTs": b["asian"][-1]["ts"]})
        if b.get("totals"):
            t, oo, ou = totals_decimal(b["totals"][-1])
            for side, odds in (("over", oo), ("under", ou)):
                win, push = total_outcomes(model_grid, t, side)
                rows.append({"company": company, "companyName": cname, "market": "totals", "side": side,
                             "line": t, "odds": odds, "ev": expected_return(win, push, odds),
                             "pWin": win, "pPush": push, "quoteTs": b["totals"][-1]["ts"]})
        if b.get("euro"):
            probs = one_x_two(model_grid)
            for side, odds, p in zip(("home", "draw", "away"), euro_decimal(b["euro"][-1]), probs):
                rows.append({"company": company, "companyName": cname, "market": "1x2", "side": side,
                             "line": None, "odds": odds, "ev": p * odds - 1, "pWin": p, "pPush": 0.0,
                             "quoteTs": b["euro"][-1]["ts"]})
    return sorted(rows, key=lambda r: -r["ev"])


def _direction(candidate):
    if candidate["market"] == "totals":
        return "total", 1 if candidate["side"] == "over" else -1
    if candidate["side"] == "draw":
        return None, 0
    return "supremacy", 1 if candidate["side"] == "home" else -1


def predict(meta, book, params=None, manual=None, v1=None):
    """meta: {'matchId','competition','home','away','kickoff'(epoch)}."""
    params = params or load_parameters()
    gates = params["gates"]
    kickoff = meta["kickoff"]
    cutoff = kickoff - params["cutoffSeconds"]
    result = {"version": params["version"], "status": params["status"], "match": meta,
              "cutoff": datetime.fromtimestamp(cutoff, BJT).isoformat(timespec="seconds"),
              "blocks": [], "cautions": []}
    ref = book.get(params["referenceCompany"]) or {}
    asian = ref.get("asian") or []
    if len(asian) < gates["minAsianQuotes"]:
        result["blocks"].append(ref.get("asianError") or f"参考公司亚盘有效赛前报价不足{gates['minAsianQuotes']}条")
        result["decision"] = {"tier": None, "units": 0, "text": "不出结论"}
        return result

    label = competition_label(meta.get("competition"))
    if re.search(gates["blockedLabelRegex"], label):
        result["blocks"].append("目标赛事属青年/女足/预备/友谊等阻断类型")
    if gates["requireFirstTier"] and not first_tier(meta.get("competition")):
        result["blocks"].append("赛事不在V1一级赛事精确名单")
    if re.search(gates["cautionLabelRegex"], label):
        result["cautions"].append("杯赛/国家队/洲际赛事：轮换与战意不确定，降一档")

    sig = compute(book, kickoff, params, manual, v1)
    signals, ctx = sig["signals"], sig["context"]
    if ctx.get("manualRejected"):
        result["cautions"].append(ctx["manualRejected"])
    if ctx.get("totalSource") == "prior":
        result["cautions"].append(f"缺大小球报价，总进球用先验 {params['priorTotal']}")
    age = (cutoff - asian[-1]["ts"]) / 60
    if age > gates["maxDataAgeMinutes"]:
        result["cautions"].append(f"参考亚盘末报价距截点 {age:.0f} 分钟")

    rho = params["rho"]
    totals = ref.get("totals") or []
    euro = ref.get("euro") or []
    fair = fit_market(asian=asian_decimal(asian[-1]),
                      totals=totals_decimal(totals[-1]) if totals else None,
                      euro=euro_decimal(euro[-1]) if euro and not totals else None,
                      rho=rho, prior_total=params["priorTotal"])
    if fair["fitError"] > gates["maxFitError"]:
        result["cautions"].append(f"多市场拟合残差 {fair['fitError']:.2e}：盘赔内部不一致")
    m1 = signals.get("M1")
    if m1 and abs(m1["raw"]) > gates["maxEuroAsianGap"]:
        result["cautions"].append(f"欧亚折算净胜球相差 {m1['raw']:+.2f} 球")
    dsup, dtot = adjustments(signals, params)
    total, sup = fair["total"] + dtot, fair["supremacy"] + dsup
    lh, la = lambdas(total, sup)
    grid = score_matrix(lh, la, rho)
    p1, px, p2 = one_x_two(grid)
    result.update(
        signals=signals, context=ctx, fairMarket=fair,
        model={"total": total, "supremacy": sup, "lambdaHome": lh, "lambdaAway": la, "rho": rho,
               "adjustSupremacy": dsup, "adjustTotal": dtot, "p1x2": [p1, px, p2]},
        topScores=sorted(grid.items(), key=lambda kv: -kv[1])[:6])

    candidates = _candidates(book, grid, params)
    result["candidates"] = candidates[:8]
    result["stakeMarkets"] = list(gates["stakeMarkets"])
    result["stakeCompanies"] = list(gates["stakeCompanies"])
    best = None
    for c in candidates:
        axis, sign = _direction(c)
        if (axis is None or c["market"] not in gates["stakeMarkets"]
                or c["company"] not in gates["stakeCompanies"]):
            continue
        v = votes(signals, axis, gates["signalThreshold"], params)
        support, oppose = (v["pos"], v["neg"]) if sign > 0 else (v["neg"], v["pos"])
        strongest_against = v["maxNeg"] if sign > 0 else v["maxPos"]
        c["support"], c["oppose"] = support, oppose
        c["conflict"] = strongest_against >= gates["conflictThreshold"]
        if support >= oppose and not c["conflict"]:
            best = c
            break
    result["pick"] = best
    tier = None
    if best and not result["blocks"]:
        if best["ev"] >= gates["evTierA"]:
            tier = "A"
        elif best["ev"] >= gates["evTierB"]:
            tier = "B"
        if tier and result["cautions"]:
            tier = {"A": "B", "B": "C"}[tier]
    units = gates["units"].get(tier, 0.0) if tier else 0.0
    result["decision"] = {"tier": tier, "units": units,
                          "text": describe_pick(best, meta) if tier else "观望"}
    return result


def describe_pick(c, meta):
    if c is None:
        return "观望"
    if c["market"] == "asian":
        team = meta["home"] if c["side"] == "home" else meta["away"]
        h = c["line"] if c["side"] == "home" else -c["line"]
        return f"{team} {h:+g} @{c['odds']:.2f}（{c['companyName']}）"
    if c["market"] == "totals":
        return f"全场{'大' if c['side'] == 'over' else '小'} {c['line']:g} @{c['odds']:.2f}（{c['companyName']}）"
    return f"{dict(home='主胜', draw='平', away='客胜')[c['side']]} @{c['odds']:.2f}（{c['companyName']}）"


def render(result):
    """Chinese text report in the 庄家意图/模型/结论/推荐 layout."""
    m = result["match"]
    lines = [f"{m.get('competition', '')}｜{m['home']} vs {m['away']}",
             f"决策截点 {result['cutoff']}"]
    if "model" not in result:
        lines.append("阻断：" + "；".join(result["blocks"]))
        return "\n".join(lines)
    f, mo = result["fairMarket"], result["model"]
    p1, px, p2 = mo["p1x2"]
    lines += ["", "一、市场公允价（参考公司去水）",
              f"λ主={f['lambdaHome']:.2f} λ客={f['lambdaAway']:.2f}，总进球 {f['total']:.2f}，"
              f"净胜球 {f['supremacy']:+.2f}，ρ={f['rho']}，来源 {'+'.join(f['markets'])}"
              + ("（总进球为先验）" if f["totalSource"] == "prior" else ""),
              "", "二、庄家意图（M 分类）"]
    for key in ("M1", "M2", "M3", "M4", "M5", "M6", "M7"):
        s = result["signals"].get(key)
        if not s:
            continue
        val = f"{s['value']:+.2f}" if "value" in s else f"Δ净胜{s['sup']:+.2f}/Δ总{s['tot']:+.2f}"
        lines.append(f"• {key}［{s.get('axis')}］{val}：{s['text']}")
    lines += ["", "三、模型（公允价 + 信号修正）",
              f"修正 Δ净胜球 {mo['adjustSupremacy']:+.3f}，Δ总进球 {mo['adjustTotal']:+.3f}",
              f"P(主/平/客) = {p1:.1%} / {px:.1%} / {p2:.1%}",
              "比分参考：" + "，".join(f"{i}-{j}（{p:.1%}）" for (i, j), p in result["topScores"]),
              "", "四、价值（各公司截点报价 EV 前四）"]
    for c in result["candidates"][:4]:
        lines.append(f"  {describe_pick(c, m)}  EV {c['ev']:+.1%}"
                     + ("" if c["market"] in result["stakeMarkets"] and c["company"] in result["stakeCompanies"]
                        else "（仅观察）"))
    d = result["decision"]
    lines += ["", "五、结论"]
    if result["blocks"]:
        lines.append("阻断：" + "；".join(result["blocks"]))
    if result["cautions"]:
        lines.append("提示：" + "；".join(result["cautions"]))
    pick = result.get("pick")
    if pick:
        lines.append(f"支持信号 {pick['support']:.0f} / 反向 {pick['oppose']:.0f}")
    lines.append(f"推荐：{d['text']}" + (f"｜等级 {d['tier']}｜{d['units']:g} 单位" if d["tier"] else ""))
    lines.append(f"（{result['version']}，{result['status']}：EV 为参考公司公允价对他家报价的差，"
                 "M 信号仅作诊断；均非已校准胜率）")
    return "\n".join(lines)
