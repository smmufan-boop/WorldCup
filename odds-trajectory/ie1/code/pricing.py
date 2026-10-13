"""Dixon-Coles score model and Asian/total/1X2 pricing for the IE1 scheme.

Python 3 standard library only. All market prices are decimal odds; Hong Kong
waters must be converted with ``1 + water`` by the caller. ``home_handicap`` is
the number of goals added to the home score at settlement (home gives 0.5 ->
-0.5). This is the opposite sign of the local V1 ``line`` field.
"""
from __future__ import annotations

import math

MAX_GOALS = 10


def _poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_matrix(lam_home, lam_away, rho=0.0, max_goals=MAX_GOALS):
    """Return a normalised {(home, away): probability} Dixon-Coles matrix."""
    if not (lam_home > 0 and lam_away > 0):
        raise ValueError("Expected goals must be positive")
    # Keep tau non-negative for every low-score cell.
    lo = max(-1 / lam_home, -1 / lam_away)
    hi = min(1 / (lam_home * lam_away), 1.0)
    rho = min(max(rho, lo), hi)
    ph = [_poisson(k, lam_home) for k in range(max_goals + 1)]
    pa = [_poisson(k, lam_away) for k in range(max_goals + 1)]
    grid = {}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tau = 1.0
            if i == 0 and j == 0:
                tau = 1 - lam_home * lam_away * rho
            elif i == 0 and j == 1:
                tau = 1 + lam_home * rho
            elif i == 1 and j == 0:
                tau = 1 + lam_away * rho
            elif i == 1 and j == 1:
                tau = 1 - rho
            grid[(i, j)] = ph[i] * pa[j] * tau
    total = math.fsum(grid.values())
    return {k: v / total for k, v in grid.items()}


def _halves(line):
    """Split a quarter line into its two settlement halves."""
    if round(line * 4) != line * 4:
        raise ValueError("Line must be a multiple of 0.25")
    if round(line * 2) == line * 2:
        return (line, line)
    return (line - 0.25, line + 0.25)


def _margin_outcomes(grid, margin, line):
    """(win, push) half-weighted probabilities for margin(score) + line > 0."""
    win = push = 0.0
    for half in _halves(line):
        for score, p in grid.items():
            m = margin(score) + half
            if m > 1e-9:
                win += 0.5 * p
            elif abs(m) <= 1e-9:
                push += 0.5 * p
    return win, push


def ah_outcomes(grid, home_handicap, side="home"):
    if side == "home":
        return _margin_outcomes(grid, lambda s: s[0] - s[1], home_handicap)
    return _margin_outcomes(grid, lambda s: s[1] - s[0], -home_handicap)


def total_outcomes(grid, line, side="over"):
    if side == "over":
        return _margin_outcomes(grid, lambda s: s[0] + s[1], -line)
    return _margin_outcomes(grid, lambda s: -(s[0] + s[1]), line)


def expected_return(win, push, decimal_odds):
    """Expected net profit per staked unit, handling half win/loss lines."""
    return win * decimal_odds + push - 1.0


def no_push_probability(win, push):
    """Win probability equivalent with pushes removed: 1 / fair decimal odds."""
    return win / (1.0 - push) if push < 1 else None


def one_x_two(grid):
    home = math.fsum(p for (i, j), p in grid.items() if i > j)
    draw = math.fsum(p for (i, j), p in grid.items() if i == j)
    return home, draw, 1.0 - home - draw


def devig_two_way(odds_a, odds_b):
    """Proportional removal of the bookmaker margin. Returns (pA, pB, margin)."""
    qa, qb = 1 / odds_a, 1 / odds_b
    s = qa + qb
    return qa / s, qb / s, s - 1


def devig_power(odds):
    """Power-method de-vig for 1X2 (reduces favourite-longshot bias)."""
    q = [1 / o for o in odds]
    lo, hi = 0.5, 3.0
    for _ in range(80):
        k = (lo + hi) / 2
        if math.fsum(x ** k for x in q) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return [x ** k for x in q], math.fsum(q) - 1


def _nelder_mead(f, x0, step=0.3, iters=300, tol=1e-12):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if vals[-1] - vals[0] < tol:
            break
        centroid = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        reflect = [centroid[j] + (centroid[j] - pts[-1][j]) for j in range(n)]
        fr = f(reflect)
        if fr < vals[0]:
            expand = [centroid[j] + 2 * (centroid[j] - pts[-1][j]) for j in range(n)]
            fe = f(expand)
            pts[-1], vals[-1] = (expand, fe) if fe < fr else (reflect, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = reflect, fr
        else:
            contract = [centroid[j] + 0.5 * (pts[-1][j] - centroid[j]) for j in range(n)]
            fc = f(contract)
            if fc < vals[-1]:
                pts[-1], vals[-1] = contract, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    best = min(range(n + 1), key=lambda i: vals[i])
    return pts[best], vals[best]


def lambdas(total, supremacy):
    """Split expected total goals and supremacy (home minus away) into lambdas."""
    total = max(total, 0.2)
    sup = max(min(supremacy, total - 0.05), -(total - 0.05))
    return (total + sup) / 2, (total - sup) / 2


def fit_market(asian=None, totals=None, euro=None, rho=-0.05, prior_total=None,
               weights=(1.0, 1.0, 1.0)):
    """Fit (total, supremacy) so model fair prices reproduce de-vigged markets.

    asian  = (home_handicap, home_decimal, away_decimal)
    totals = (line, over_decimal, under_decimal)
    euro   = (home_decimal, draw_decimal, away_decimal)
    If totals and euro are both missing, ``prior_total`` fixes the total and the
    result is flagged ``totalSource='prior'`` (one equation, one unknown).
    """
    targets = []
    if asian:
        h, oh, oa = asian
        targets.append(("asian", weights[0], h, devig_two_way(oh, oa)[0]))
    if totals:
        t, oo, ou = totals
        targets.append(("totals", weights[1], t, devig_two_way(oo, ou)[0]))
    if euro:
        probs, _ = devig_power(euro)
        targets.append(("euro", weights[2], None, probs))
    if not targets:
        raise ValueError("At least one market is required")
    fixed_total = None
    if not totals and not euro:
        if prior_total is None:
            raise ValueError("A total-goals source or prior_total is required")
        fixed_total = prior_total

    def loss(x):
        total = fixed_total if fixed_total is not None else x[0]
        sup = x[-1]
        if total <= 0.3 or total > 8 or abs(sup) >= total:
            return 1e6
        grid = score_matrix(*lambdas(total, sup), rho)
        err = 0.0
        for kind, w, line, target in targets:
            if kind == "asian":
                p = no_push_probability(*ah_outcomes(grid, line, "home"))
                err += w * (p - target) ** 2
            elif kind == "totals":
                p = no_push_probability(*total_outcomes(grid, line, "over"))
                err += w * (p - target) ** 2
            else:
                err += w * sum((a - b) ** 2 for a, b in zip(one_x_two(grid), target))
        return err

    if fixed_total is not None:
        x, err = _nelder_mead(loss, [0.0], step=0.4)
        total, sup = fixed_total, x[0]
    else:
        start_sup = -asian[0] if asian else 0.0
        start_total = totals[0] if totals else 2.6
        x, err = _nelder_mead(loss, [start_total, start_sup], step=0.4)
        total, sup = x
    lh, la = lambdas(total, sup)
    return {"total": total, "supremacy": sup, "lambdaHome": lh, "lambdaAway": la,
            "rho": rho, "fitError": err, "totalSource": "prior" if fixed_total is not None else "market",
            "markets": [t[0] for t in targets]}


def implied_supremacy(home_handicap, home_decimal, away_decimal, total, rho=-0.05):
    """Supremacy implied by one Asian quote at a fixed total (for path signals)."""
    return fit_market(asian=(home_handicap, home_decimal, away_decimal),
                      rho=rho, prior_total=total)["supremacy"]


def implied_total(line, over_decimal, under_decimal, supremacy=0.0, rho=-0.05):
    """Total goals implied by one over/under quote at a fixed supremacy."""
    target = devig_two_way(over_decimal, under_decimal)[0]
    lo, hi = max(abs(supremacy) + 0.06, 0.3), 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = no_push_probability(*total_outcomes(score_matrix(*lambdas(mid, supremacy), rho), line, "over"))
        lo, hi = (mid, hi) if p < target else (lo, mid)
    return (lo + hi) / 2
