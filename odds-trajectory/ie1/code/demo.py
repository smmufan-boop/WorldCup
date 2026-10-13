"""Run IE1 end to end on synthetic odds_history-style rows (no real data).

From the repository root: python odds-trajectory/ie1/code/demo.py
"""
import json
import sys

from engine import load_parameters, predict, render
from matching_core import clock
from quotes import market_book

KICKOFF = "2030-03-01T20:00:00+08:00"
TEXT = {0.25: "平/半", 0.5: "半球", 0.75: "半/一", 1.0: "一球"}


def asia(time, line, home, away):
    return {"change_time_local": f"2030-03-01T{time}:00+08:00", "market_type": "asia", "score": "",
            "handicap_text": TEXT[line], "handicap_value": f"{line:.2f}",
            "home_water": f"{home:.2f}", "away_water": f"{away:.2f}"}


def size(time, line, over, under):
    return {"change_time_local": f"2030-03-01T{time}:00+08:00", "market_type": "size", "score": "",
            "handicap_value": f"{line:.2f}", "home_water": f"{over:.2f}", "away_water": f"{under:.2f}"}


def euro(time, h, d, a):
    return {"change_time_local": f"2030-03-01T{time}:00+08:00", "market_type": "euro", "score": "",
            "home_water": f"{h:.2f}", "handicap_value": f"{d:.2f}", "away_water": f"{a:.2f}"}


ROWS = {
    # Royal: 主让半球升至半/一并降水，大小 2.5→2.75，欧赔主胜收窄。
    "3": [asia("10:00", 0.5, 0.92, 0.96), asia("15:00", 0.5, 0.84, 1.04), asia("18:00", 0.75, 0.98, 0.90),
          asia("19:05", 0.75, 0.90, 0.98), asia("19:40", 0.75, 0.86, 1.02),
          size("10:00", 2.5, 0.90, 0.98), size("17:00", 2.75, 1.00, 0.88), size("19:40", 2.75, 0.94, 0.94),
          euro("10:00", 2.02, 3.40, 3.55), euro("19:40", 1.88, 3.55, 3.95)],
    # 澳彩 lags: still 半球 with home 0.94 at the cutoff.
    "1": [asia("10:00", 0.5, 0.90, 0.95), asia("16:00", 0.5, 0.88, 0.97), asia("19:30", 0.5, 0.94, 0.91),
          size("10:00", 2.5, 0.88, 0.97), size("19:30", 2.5, 0.80, 1.05)],
    # Bet365 follows Royal.
    "8": [asia("10:00", 0.5, 0.95, 0.85), asia("18:30", 0.75, 0.97, 0.83), asia("19:45", 0.75, 0.90, 0.90)],
}

if __name__ == "__main__":
    kickoff = clock(KICKOFF)
    params = load_parameters()
    book = market_book(ROWS, kickoff, kickoff - params["cutoffSeconds"])
    meta = {"matchId": "synthetic-1", "competition": "英超", "home": "合成主队", "away": "合成客队",
            "kickoff": kickoff}
    manual = {"recordedAt": kickoff - 3 * 3600, "supremacy": 0.03, "total": 0.0,
              "notes": "合成：主队主力中卫复出（人工录入，+0.03 球）"}
    v1 = {"score": 56.0, "effectiveN": 6.2}
    result = predict(meta, book, params, manual=manual, v1=v1)
    print(render(result))
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
