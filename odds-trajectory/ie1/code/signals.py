"""IE1 market-intent signals M1-M7.

Every signal is a number in [-1, 1] with a fixed sign convention:
positive supremacy signals favour the home team, positive total signals favour
over. All path measures are expressed through the pricing model as changes in
implied supremacy/total goals, so a line move and a water move are on one scale
("升盘不挺水" simply produces a smaller supremacy change than "升盘降水").
"""
from __future__ import annotations

import math

from pricing import fit_market, implied_supremacy, implied_total
from quotes import asian_decimal, at_or_before, euro_decimal, totals_decimal


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _sup(q, total, rho):
    return implied_supremacy(*asian_decimal(q), total, rho)


def _describe_asian_move(first, last):
    d_line = last["home_handicap"] - first["home_handicap"]
    if abs(d_line) < 1e-9:
        dw = last["home"] - first["home"]
        return f"盘口未变（{_line(first)}），主水 {first['home']:.2f}→{last['home']:.2f}" + (
            "，主队降水" if dw <= -0.03 else "，主队升水" if dw >= 0.03 else "，水位平稳")
    toward_home = d_line < 0  # home gives more
    side, w0, w1 = ("home", first["home"], last["home"]) if toward_home else ("away", first["away"], last["away"])
    who = "主" if toward_home else "客"
    hold = "挺水（降水）" if w1 - w0 <= -0.03 else "不挺水（升水）" if w1 - w0 >= 0.03 else "水位持平"
    return f"{_line(first)}→{_line(last)}，盘口向{who}队移动，{who}水 {w0:.2f}→{w1:.2f} {hold}"


def _line(q):
    h = -q["home_handicap"]
    if abs(h) < 1e-9:
        return "平手"
    return ("主让" if h > 0 else "主受让") + f"{abs(h):g}"


def compute(book, kickoff, params, manual=None, v1=None):
    """Return {'signals': {...}, 'context': {...}} from a market_book at cutoff."""
    ref = params["referenceCompany"]
    cutoff = kickoff - params["cutoffSeconds"]
    rho = params["rho"]
    cfg = params["signals"]
    refbook = book.get(ref) or {}
    asian = refbook.get("asian") or []
    totals = refbook.get("totals") or []
    euro = refbook.get("euro") or []
    out, ctx = {}, {}
    if not asian:
        return {"signals": out, "context": {"error": refbook.get("asianError") or "参考公司无亚盘"}}

    # Total goals at cutoff anchors every path conversion.
    if totals:
        total_now = fit_market(asian=asian_decimal(asian[-1]), totals=totals_decimal(totals[-1]),
                               rho=rho)["total"]
        ctx["totalSource"] = "market"
    else:
        total_now = params["priorTotal"]
        ctx["totalSource"] = "prior"
    ctx["totalAtCutoff"] = total_now

    sup_open, sup_cut = _sup(asian[0], total_now, rho), _sup(asian[-1], total_now, rho)
    ctx.update(supOpen=sup_open, supCutoff=sup_cut, asianOpen=asian[0], asianCutoff=asian[-1])

    # M2: whole-window path move (open -> cutoff), one scale for line+water.
    d = sup_cut - sup_open
    out["M2"] = {"value": clamp(d / cfg["M2"]["scale"]), "raw": d, "axis": "supremacy",
                 "text": _describe_asian_move(asian[0], asian[-1]) + f"；隐含净胜球 {sup_open:+.2f}→{sup_cut:+.2f}"}

    # M3: total line path.
    if len(totals) >= 2:
        t_open = implied_total(*totals_decimal(totals[0]), sup_cut, rho)
        dt = total_now - t_open
        out["M3"] = {"value": clamp(dt / cfg["M3"]["scale"]), "raw": dt, "axis": "total",
                     "text": f"大小线 {totals[0]['line']:g}({totals[0]['over']:.2f}/{totals[0]['under']:.2f})→"
                             f"{totals[-1]['line']:g}({totals[-1]['over']:.2f}/{totals[-1]['under']:.2f})；"
                             f"隐含总进球 {t_open:.2f}→{total_now:.2f}"}

    # M5: late window move with cross-company agreement.
    late = cutoff - params["lateWindowSeconds"]
    moves = {}
    for company, b in book.items():
        path = b.get("asian") or []
        anchor = at_or_before(path, late)
        if anchor and path:
            moves[company] = _sup(path[-1], total_now, rho) - _sup(anchor, total_now, rho)
    if ref in moves:
        lead = moves[ref]
        sign = (lead > 0) - (lead < 0)
        agree = sum(1 for v in moves.values() if (v > 0) - (v < 0) == sign and sign) / len(moves)
        out["M5"] = {"value": clamp(lead / cfg["M5"]["scale"]) * agree, "raw": lead, "axis": "supremacy",
                     "agreement": agree,
                     "text": f"临场{params['lateWindowSeconds']//60}分钟参考公司隐含净胜球变化 {lead:+.3f}；"
                             f"同向公司 {round(agree*len(moves))}/{len(moves)}"}

    # M4: reference (sharper) vs other companies at cutoff.
    others = {c: _sup(b["asian"][-1], total_now, rho) for c, b in book.items()
              if c != ref and b.get("asian")}
    if others:
        gap = sup_cut - sum(others.values()) / len(others)
        out["M4"] = {"value": clamp(gap / cfg["M4"]["scale"]), "raw": gap, "axis": "supremacy",
                     "text": "参考公司比其余公司" + ("更看好主队" if gap > 0 else "更看好客队") +
                             f" {abs(gap):.3f} 球；" + "，".join(
                                 f"{params['companies'].get(c, c)} {v:+.2f}" for c, v in sorted(others.items()))}

    # M1: 1X2-implied vs Asian-implied supremacy (needs euro).
    if euro:
        fit_e = fit_market(euro=euro_decimal(euro[-1]),
                           totals=totals_decimal(totals[-1]) if totals else None, rho=rho)
        gap = fit_e["supremacy"] - sup_cut
        out["M1"] = {"value": clamp(gap / cfg["M1"]["scale"]), "raw": gap, "axis": "supremacy",
                     "text": f"欧赔 {euro[-1]['home']:.2f}/{euro[-1]['draw']:.2f}/{euro[-1]['away']:.2f} 折算净胜球 "
                             f"{fit_e['supremacy']:+.2f}，亚盘折算 {sup_cut:+.2f}；" +
                             ("亚盘对主队开浅" if gap > 0.05 else "亚盘对主队开深" if gap < -0.05 else "欧亚一致")}

    # M6: manual information, bounded and time-stamped before cutoff.
    if manual:
        recorded = manual.get("recordedAt")
        if recorded is None or recorded > cutoff:
            ctx["manualRejected"] = "信息面录入时间缺失或晚于截点，未采用"
        else:
            out["M6"] = {"sup": clamp(manual.get("supremacy", 0.0), -cfg["M6"]["capSupremacy"], cfg["M6"]["capSupremacy"]),
                         "tot": clamp(manual.get("total", 0.0), -cfg["M6"]["capTotal"], cfg["M6"]["capTotal"]),
                         "axis": "manual", "text": manual.get("notes", "")}

    # M7: existing V1 historical same-trajectory direction (upper side).
    if v1 and v1.get("score") is not None:
        s = (v1["score"] - 50) / 50 * min(1.0, v1.get("effectiveN", 0) / cfg["M7"]["effectiveNFull"])
        upper_is_home = -asian[-1]["home_handicap"] >= 0
        out["M7"] = {"value": clamp(s if upper_is_home else -s), "raw": v1["score"], "axis": "supremacy",
                     "text": f"V1 方向原分 D={v1['score']:.1f}，有效样本 {v1.get('effectiveN', 0):.1f}"}
    return {"signals": out, "context": ctx}


def adjustments(signals, params):
    cfg, caps = params["signals"], params["caps"]
    dsup = dtot = 0.0
    for key, s in signals.items():
        if key == "M6":
            dsup += s["sup"]
            dtot += s["tot"]
            continue
        dsup += cfg[key].get("betaSupremacy", 0.0) * s["value"]
        dtot += cfg[key].get("betaTotal", 0.0) * s["value"]
    return clamp(dsup, -caps["supremacy"], caps["supremacy"]), clamp(dtot, -caps["total"], caps["total"])


def votes(signals, axis, threshold, params):
    """Directional support among enabled signals (non-zero coefficient) and M6.

    Diagnostic signals (coefficient 0) are reported but never vote, so an
    unvalidated reading cannot block or promote a pick.
    """
    beta = "betaSupremacy" if axis == "supremacy" else "betaTotal"
    pos = neg = strongest_neg = strongest_pos = 0.0
    for key, s in signals.items():
        if key == "M6":
            v = s["sup"] if axis == "supremacy" else s["tot"]
            v = 1.0 if v > 0 else -1.0 if v < 0 else 0.0
        elif s.get("axis") != axis or not params["signals"][key].get(beta):
            continue
        else:
            v = s["value"]
        if v >= threshold:
            pos += 1
            strongest_pos = max(strongest_pos, v)
        elif v <= -threshold:
            neg += 1
            strongest_neg = max(strongest_neg, -v)
    return {"pos": pos, "neg": neg, "maxPos": strongest_pos, "maxNeg": strongest_neg}


def is_finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)
