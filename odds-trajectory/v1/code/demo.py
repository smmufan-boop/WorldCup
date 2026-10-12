"""Run the published V1 algorithms on synthetic quotes only.

From odds-trajectory/v1: python code/demo.py
Python 3 standard library only; no network or local business data is used.
"""
import json

from direction_v1 import configuration, score_company
from matching_core import classify, clock, feature, points
from odds_point_similarity import compare_odds
from odds_segment_similarity import compare_segmented_odds


def match(identifier, home, away, handicap=-0.5):
    return {
        "id": identifier,
        "bookmaker": "1",
        "market": "asian_handicap",
        "phase": "prematch",
        "odds_format": "hong_kong",
        "perspective": "home",
        "window_id": "synthetic-T-10",
        "records": [
            {"home_handicap": handicap, "home_water": h, "away_water": a}
            for h, a in zip(home, away)
        ],
    }


def sample(identifier, point_score, result, profit):
    return {
        "matchId": identifier,
        "pointScore": point_score,
        "settlements": {"upper": {"result": result, "profit": profit}},
    }


def synthetic_comparisons():
    target = match("synthetic-target", [0.90, 0.87, 0.88, 0.84, 0.82],
                   [0.94, 0.97, 0.96, 1.00, 1.02])
    analogue = match("synthetic-analogue", [0.92, 0.89, 0.90, 0.86, 0.84],
                     [0.96, 0.99, 0.98, 1.02, 1.04])
    contrary = match("synthetic-contrary", [0.90, 0.93, 0.92, 0.96, 0.98],
                     [0.94, 0.91, 0.92, 0.88, 0.86])
    mixed = match("synthetic-mixed", [0.90, 0.91, 0.88, 0.92, 0.82],
                 [0.94, 0.95, 0.93, 0.90, 1.02])
    short = match("synthetic-three-points", [0.90, 0.87, 0.88],
                 [0.94, 0.97, 0.96])

    second_a = match("stage-two-a", [0.88, 0.86, 0.85, 0.87, 0.84, 0.83],
                    [0.98, 1.00, 1.01, 0.99, 1.02, 1.03], handicap=-0.75)
    second_b = match("stage-two-b", [0.88, 0.90, 0.91, 0.89, 0.92, 0.93],
                    [0.98, 0.96, 0.95, 0.97, 0.94, 0.93], handicap=-0.75)
    segmented_a = {**target, "records": target["records"] + second_a["records"]}
    segmented_b = {**analogue, "records": analogue["records"] + second_b["records"]}
    return {
        "sameDirection": compare_odds(target, analogue),
        "oppositeDirection": compare_odds(target, contrary),
        "mixedDirection": compare_odds(target, mixed),
        "insufficientPoints": compare_odds(target, short),
        "segmented": compare_segmented_odds(segmented_a, segmented_b),
    }


def synthetic_matching():
    kickoff = clock("2030-01-01T20:00:00+08:00")
    cutoff = kickoff - 600
    quotes = [
        ("19:00", 0.90, 0.94), ("19:10", 0.88, 0.96),
        ("19:20", 0.86, 0.98), ("19:35", 0.84, 1.00),
        ("19:50", 0.82, 1.02),
    ]

    def make_feature(offset):
        rows = [
            {"change_time_local": "2030-01-01T" + time + ":00+08:00",
             "market_type": "asia", "score": "", "handicap_text": "受半球",
             "handicap_value": 0.5, "home_water": h + offset, "away_water": a + offset}
            for time, h, a in quotes
        ]
        ticks, error = points(rows, kickoff, cutoff)
        assert not error, error
        return feature(ticks, cutoff)

    target = make_feature(0.0)
    return {
        "gap005": classify(target, make_feature(0.05)),
        "gap0051": classify(target, make_feature(0.051)),
        "gap0101": classify(target, make_feature(0.101)),
    }


def build_demo():
    comparisons = synthetic_comparisons()
    company = {"groups": {
        "strict": {"samples": [
            sample("synthetic-win", comparisons["sameDirection"], "win", 0.90),
            sample("synthetic-half-win", comparisons["sameDirection"], "half_win", 0.45),
            sample("synthetic-push", comparisons["segmented"], "push", 0.0),
            sample("synthetic-insufficient", comparisons["insufficientPoints"], "win", 0.90),
        ]},
        "approx": {"samples": [
            sample("synthetic-mixed-loss", comparisons["mixedDirection"], "loss", -1.0),
            sample("synthetic-opposite-half-loss", comparisons["oppositeDirection"], "half_loss", -0.5),
            # Strict has priority: this duplicate is ignored entirely.
            sample("synthetic-win", comparisons["oppositeDirection"], "loss", -1.0),
        ]},
    }}
    config = configuration()
    scores = {}
    for company_id, parameters in config["companies"].items():
        # Fresh samples avoid sharing annotation state between company outputs.
        current = json.loads(json.dumps(company))
        scores[company_id] = score_company(current, parameters=parameters, annotate=True)
        scores[company_id]["annotatedSamples"] = current["groups"]
    push_only = {"groups": {"strict": {"samples": [
        sample("synthetic-only-push", comparisons["sameDirection"], "push", 0.0)
    ]}}}
    diagnostic_only = {"groups": {"strict": {"samples": [
        sample("synthetic-only-short", comparisons["insufficientPoints"], "win", 0.90)
    ]}}}
    return {
        "dataOrigin": "all values are synthetic; no real match records",
        "configurationVersion": config["version"],
        "pointAndSegmentComparisons": comparisons,
        "matchingThresholdExamples": synthetic_matching(),
        "companyDirectionScores": scores,
        "pushOnly": score_company(push_only, parameters=config["companies"]["1"]),
        "diagnosticOnly": score_company(diagnostic_only, parameters=config["companies"]["1"]),
        "emptyCompany": score_company({}, parameters=config["companies"]["1"]),
    }


if __name__ == "__main__":
    print(json.dumps(build_demo(), ensure_ascii=False, indent=2, allow_nan=False))
