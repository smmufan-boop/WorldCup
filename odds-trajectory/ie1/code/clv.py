"""Closing-line value (CLV) for IE1 picks and candidates.

The closing reference is Royal's last valid prematch Asian quote before kickoff
(plus Royal's last prematch totals quote, else the T-10 total). Its de-vigged
fair price defines a closing score model; CLV is the locked bet's expected
profit per unit under that model:

    CLV = W_close * locked_odds + P_close - 1

so it works when the closing line differs from the locked line (quarter lines
included). CLV > 0 means the locked price beat the close. It is a faster but
indirect yardstick than realised ROI; it inherits any error in the close.
"""
from __future__ import annotations

from pricing import ah_outcomes, fit_market, no_push_probability, score_matrix, total_outcomes
from quotes import asian_decimal, market_book, totals_decimal


def closing_model(ref_close, rho=-0.05, fallback_total=None):
    """Fit the closing score model from Royal's last prematch quotes."""
    asian = ref_close.get("asian") or []
    if not asian:
        return None
    totals = ref_close.get("totals") or []
    fit = fit_market(asian=asian_decimal(asian[-1]),
                     totals=totals_decimal(totals[-1]) if totals else None,
                     rho=rho, prior_total=fallback_total or 2.6)
    return {"fit": fit, "grid": score_matrix(fit["lambdaHome"], fit["lambdaAway"], rho),
            "asian": asian[-1], "totals": totals[-1] if totals else None}


def clv(bet, model):
    """bet: {'market','side','line','odds'} -> CLV per unit and closing fair win prob."""
    if model is None:
        return None
    if bet["market"] == "asian":
        win, push = ah_outcomes(model["grid"], bet["line"], bet["side"])
    elif bet["market"] == "totals":
        win, push = total_outcomes(model["grid"], bet["line"], bet["side"])
    else:
        return None
    a = model["asian"]
    return {"clv": win * bet["odds"] + push - 1, "closeFairP": no_push_probability(win, push),
            "closeTs": a["ts"], "closeLine": a["home_handicap"], "closeHome": a["home"], "closeAway": a["away"],
            "closeTotalSource": model["fit"]["totalSource"]}


def closing_book(rows_by_company, kickoff, order_evidence=None):
    """All prematch quotes up to kickoff (source minute ends at or before kickoff)."""
    return market_book(rows_by_company, kickoff, kickoff, order_evidence)
