"""IE1 tests: pricing invariants, settlement, payload parsing, T-10 run + ledger.

Run from the repository root: python -m unittest discover -s odds-trajectory/ie1/tests
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

import ie1  # noqa: E402
from engine import load_parameters  # noqa: E402
from ledger import Ledger  # noqa: E402
from parse import goal_line, handicap_goals, rows_from_payload, source_clock  # noqa: E402
from pricing import (ah_outcomes, devig_two_way, fit_market, no_push_probability,  # noqa: E402
                     one_x_two, score_matrix)
from quotes import BJT, clock  # noqa: E402
from settle import ah_profit, total_profit  # noqa: E402

KICKOFF = clock("2030-03-01T20:00:00+08:00")


def element(minute, a, mid, b, score=""):
    t = datetime.fromtimestamp(KICKOFF, BJT).replace(hour=int(minute[:2]), minute=int(minute[3:]))
    return {"time": t.strftime("%m-%d %H:%M"), "homeWin": a, "draw": mid, "awayWin": b, "score": score}


PAYLOADS = {
    ("3", "asia"): [("10:00", "0.92", "半球", "0.96"), ("15:00", "0.84", "半球", "1.04"),
                    ("18:00", "0.98", "半/一", "0.90"), ("19:40", "0.86", "半/一", "1.02"),
                    ("20:05", "0.70", "一球", "1.20", "1-0")],
    ("3", "size"): [("10:00", "0.90", "2.5", "0.98"), ("19:40", "0.94", "2.5/3", "0.94")],
    ("3", "euro"): [("10:00", "2.02", "3.40", "3.55"), ("19:40", "1.88", "3.55", "3.95")],
    ("1", "asia"): [("10:00", "0.90", "半球", "0.95"), ("16:00", "0.88", "半球", "0.97"),
                    ("19:30", "0.94", "半球", "0.91")],
    ("1", "size"): [], ("1", "euro"): [],
}


class FakeFetcher:
    """Serves synthetic newest-first payloads in the aggregation-API shape."""

    def market(self, match_id, company, market):
        items = PAYLOADS[(company, market)]
        return {"data": {"detail": [element(*i) for i in reversed(items)]}}, {"path": "fake", "sha256": "0"}


class Pricing(unittest.TestCase):
    def test_fit_reproduces_market(self):
        fit = fit_market(asian=(-0.75, 1.92, 1.98), totals=(2.75, 1.95, 1.93))
        grid = score_matrix(fit["lambdaHome"], fit["lambdaAway"], fit["rho"])
        p = no_push_probability(*ah_outcomes(grid, -0.75))
        self.assertAlmostEqual(p, devig_two_way(1.92, 1.98)[0], places=4)
        self.assertAlmostEqual(sum(one_x_two(grid)), 1.0, places=9)

    def test_whole_line_fair_prices_sum_to_one(self):
        grid = score_matrix(1.6, 1.1, -0.05)
        h = no_push_probability(*ah_outcomes(grid, -1.0, "home"))
        a = no_push_probability(*ah_outcomes(grid, -1.0, "away"))
        self.assertAlmostEqual(h + a, 1.0, places=9)


class Settlement(unittest.TestCase):
    def test_quarter_lines(self):
        self.assertAlmostEqual(ah_profit((1, 0), -0.75, "home", 1.9), 0.45)
        self.assertAlmostEqual(ah_profit((1, 0), -0.75, "away", 1.9), -0.5)
        self.assertAlmostEqual(ah_profit((0, 0), 0.25, "home", 2.0), 0.5)
        self.assertAlmostEqual(total_profit((1, 1), 2.25, "under", 1.9), 0.45)
        self.assertEqual(ah_profit((1, 1), 0.0, "home", 1.9), 0.0)


class Parsing(unittest.TestCase):
    def test_handicap_and_line_text(self):
        self.assertEqual(handicap_goals("受一/球半"), -1.25)
        self.assertEqual(handicap_goals("平/半"), 0.25)
        self.assertEqual(handicap_goals("球半/两"), 1.75)
        self.assertIsNone(handicap_goals("一球半半"))
        self.assertEqual(goal_line("2/2.5"), 2.25)
        self.assertIsNone(goal_line("2.6"))

    def test_year_inference_across_new_year(self):
        k = clock("2026-12-31T23:00:00+08:00")
        self.assertEqual(source_clock("01-01 00:30", k).year, 2027)
        self.assertEqual(source_clock("12-30 10:00", k).year, 2026)

    def test_payload_rows_and_fail_closed(self):
        payload = {"detail": [element("19:40", "0.86", "半/一", "1.02"), element("18:00", "0.98", "怪盘", "0.90")]}
        rows, problems = rows_from_payload(payload, "asia", "1", "3", KICKOFF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["handicap_value"], "0.75")
        self.assertEqual(rows[0]["source_order"], 1)
        self.assertEqual(len(problems), 1)


class RunAndLedger(unittest.TestCase):
    def test_t10_run_registers_first_pick_and_settles(self):
        params = load_parameters()
        meta = {"matchId": "54580227", "competition": "英超", "home": "示例主队", "away": "示例客队",
                "kickoff": KICKOFF}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            fixed = KICKOFF - 300
            ie1.now = lambda: fixed
            self.assertEqual(ie1.due([meta], ledger, params, KICKOFF - 900), [])
            self.assertEqual(len(ie1.due([meta], ledger, params, fixed)), 1)
            status, registered, text = ie1.run_one(FakeFetcher(), ledger, meta, params, {}, Path(tmp))
            self.assertEqual(status, "ok")
            self.assertTrue(registered, text)
            # The in-play row after kickoff must not enter the book.
            run = json.loads(ledger.db.execute("SELECT input_json FROM runs").fetchone()[0])
            self.assertEqual(run["book"]["3"]["asian"][-1]["home"], 0.86)
            # A second run cannot add or overwrite the locked pick.
            ledger.db.execute("UPDATE runs SET status='retry'")
            ie1.run_one(FakeFetcher(), ledger, meta, params, {}, Path(tmp))
            picks = ledger.picks()
            self.assertEqual(len(picks), 1)
            self.assertEqual((picks[0]["company"], picks[0]["side"], picks[0]["line"]), ("1", "home", -0.5))
            ledger.settle(picks[0]["fixture_key"], (2, 0), ah_profit((2, 0), -0.5, "home", picks[0]["odds"]), fixed)
            self.assertAlmostEqual(ledger.picks()[0]["profit"], picks[0]["odds"] - 1)
            ledger.close()

    def test_late_run_is_not_registered(self):
        params = load_parameters()
        meta = {"matchId": "x", "competition": "英超", "home": "A", "away": "B", "kickoff": KICKOFF}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ie1.now = lambda: KICKOFF + 60
            status, registered, _ = ie1.run_one(FakeFetcher(), ledger, meta, params, {}, Path(tmp))
            self.assertEqual((status, registered), ("late", False))
            ledger.close()

    def test_manual_note_after_cutoff_is_rejected(self):
        notes = {"x": {"recordedAt": (datetime.fromtimestamp(KICKOFF, BJT) - timedelta(minutes=5)).isoformat(),
                       "supremacy": 0.1}}
        manual = ie1.manual_for(notes, "x")
        from engine import predict
        from quotes import market_book
        params = load_parameters()
        rows = {"3": rows_from_payload(FakeFetcher().market("x", "3", "asia")[0], "asia", "x", "3", KICKOFF)[0]}
        book = market_book(rows, KICKOFF, KICKOFF - 600)
        res = predict({"matchId": "x", "competition": "英超", "home": "A", "away": "B", "kickoff": KICKOFF},
                      book, params, manual=manual)
        self.assertIn("信息面录入时间缺失或晚于截点，未采用", res["cautions"])
        self.assertNotIn("M6", res["signals"])


class QuoteAgeGates(unittest.TestCase):
    def _book(self, macau_minute, royal=None):
        from quotes import market_book
        payloads = dict(PAYLOADS)
        payloads[("1", "asia")] = [("08:00", "0.90", "半球", "0.95"), (macau_minute, "0.94", "半球", "0.91")]
        if royal:
            payloads[("3", "asia")] = royal
        rows = {}
        for (company, market), items in payloads.items():
            payload = {"detail": [element(*i) for i in reversed(items)]}
            rows.setdefault(company, []).extend(rows_from_payload(payload, market, "x", company, KICKOFF)[0])
        return market_book(rows, KICKOFF, KICKOFF - 600)

    def _predict(self, book):
        from engine import predict
        meta = {"matchId": "x", "competition": "英超", "home": "A", "away": "B", "kickoff": KICKOFF}
        return predict(meta, book, load_parameters())

    def test_stale_stake_quote_is_void(self):
        fresh = self._predict(self._book("19:30"))
        self.assertIn(fresh["decision"]["tier"], ("A", "B"))
        stale = self._predict(self._book("17:00"))  # 170 minutes before the 19:50 cutoff
        self.assertIsNone(stale["decision"]["tier"])
        self.assertIn("作废", stale["staleRejected"])

    def test_stale_reference_blocks_match(self):
        royal = [("08:00", "0.92", "半球", "0.96"), ("10:00", "0.84", "半球", "1.04"),
                 ("13:00", "0.86", "半/一", "1.02")]  # Royal unchanged for 410 minutes before the cutoff
        res = self._predict(self._book("19:30", royal))
        self.assertTrue(any("超过硬门槛" in b for b in res["blocks"]))
        self.assertIsNone(res["decision"]["tier"])


if __name__ == "__main__":
    unittest.main()
