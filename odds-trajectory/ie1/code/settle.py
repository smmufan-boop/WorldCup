"""Settlement of locked IE1 terms on a verified regular-90-minute score."""


def _halves(line):
    return (line, line) if round(line * 2) == line * 2 else (line - 0.25, line + 0.25)


def _settle(margins, odds):
    """Profit per unit; each half of a quarter line carries half the stake."""
    return sum(0.5 * ((odds - 1) if m > 1e-9 else 0.0 if abs(m) <= 1e-9 else -1.0) for m in margins)


def ah_profit(score, home_handicap, side, odds):
    d = score[0] - score[1]
    if side == "home":
        return _settle([d + h for h in _halves(home_handicap)], odds)
    return _settle([-d - h for h in _halves(home_handicap)], odds)


def total_profit(score, line, side, odds):
    t = score[0] + score[1]
    sign = 1 if side == "over" else -1
    return _settle([sign * (t - h) for h in _halves(line)], odds)


def pick_profit(market, side, line, odds, score):
    if market == "asian":
        return ah_profit(score, line, side, odds)
    if market == "totals":
        return total_profit(score, line, side, odds)
    win = {"home": score[0] > score[1], "draw": score[0] == score[1], "away": score[0] < score[1]}[side]
    return odds - 1 if win else -1.0
