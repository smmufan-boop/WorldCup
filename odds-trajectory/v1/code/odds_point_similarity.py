#!/usr/bin/env python3
"""Compare two prematch Asian-handicap quote paths by update-point progress.

Python 3 standard library only. No timestamp alignment, DTW or per-path scaling.
``records`` must already be in chronological update order; timestamps are ignored.

Python API::

    result = compare_odds(match_a, match_b)
    print(result["direction_score"], result["relative_rmse"])

Command line::

    python odds_point_similarity.py input.json --output result.json

The JSON input has keys ``match_a``, ``match_b`` and optional ``config``.
Each match requires: id, bookmaker, market="asian_handicap", phase="prematch",
odds_format="hong_kong", perspective="home", window_id, and records.
Each record requires: home_handicap, home_water, away_water.
Example record: {"home_handicap": -1, "home_water": .85, "away_water": .97}.

Compare only ``rankable`` results. Sort by direction_score descending, then
relative_rmse ascending, then net_change_gap ascending, then match ID. Scores
measure quote-path agreement, not outcome probabilities. More/fewer intermediate
updates change point-progress correspondence, even when physical times coincide.
"""

import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys


ALGORITHM_VERSION = "odds-point-progress-v1.0.0"
META_FIELDS = (
    "bookmaker", "market", "phase", "odds_format", "perspective", "window_id"
)
REQUIRED_VALUES = {
    "market": "asian_handicap", "phase": "prematch",
    "odds_format": "hong_kong", "perspective": "home",
}
SIDES = ("home", "away")
NOTES = [
    "Point progress follows the order of quote changes; elapsed time is ignored.",
    "Agreement at corresponding point progress is not synchronization in real time.",
    "Different recording/update densities can change the score.",
    "Only consecutive identical complete quote states are removed.",
    "A similarity score is not a match-result probability.",
    "Rank only rankable results: direction_score descending, relative_rmse "
    "ascending, net_change_gap ascending, then match ID.",
]


class InputProblem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputProblem("invalid_input", name + " must be a finite number.")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise InputProblem("invalid_input", name + " must be a finite number.")
    if not math.isfinite(number):
        raise InputProblem("invalid_input", name + " must be a finite number.")
    return number


def _configuration(config):
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise InputProblem("invalid_input", "config must be an object.")
    unknown = set(config) - {"slope_epsilon", "channel_weights"}
    if unknown:
        raise InputProblem("invalid_input", "Unknown config keys: " + str(sorted(map(str, unknown))))
    epsilon = _number(config.get("slope_epsilon", 1e-9), "slope_epsilon")
    if epsilon < 0:
        raise InputProblem("invalid_input", "slope_epsilon must be nonnegative.")
    weights = config.get("channel_weights", {"home": 0.5, "away": 0.5})
    if not isinstance(weights, dict) or set(weights) != set(SIDES):
        raise InputProblem("invalid_input", "channel_weights requires exactly home and away.")
    weights = {side: _number(weights[side], "channel_weights." + side) for side in SIDES}
    if min(weights.values()) < 0 or max(weights.values()) <= 0:
        raise InputProblem("invalid_input", "Weights must be nonnegative with a positive sum.")
    # Scaling first prevents overflow for valid very large finite weights.
    largest = max(weights.values())
    scaled = {side: weights[side] / largest for side in SIDES}
    total = sum(scaled.values())
    return {"slope_epsilon": epsilon,
            "channel_weights": {side: scaled[side] / total for side in SIDES}}


def _prepare(match, label):
    if not isinstance(match, dict):
        raise InputProblem("invalid_input", label + " must be an object.")
    for key in ("id",) + META_FIELDS:
        if not isinstance(match.get(key), str) or not match[key].strip():
            raise InputProblem("invalid_input", label + "." + key + " must be a nonempty string.")
    records = match.get("records")
    if not isinstance(records, list):
        raise InputProblem("invalid_input", label + ".records must be an array.")
    if len(records) < 2:
        raise InputProblem("insufficient_data", label + " requires at least two original records.")
    clean = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise InputProblem("invalid_input", "%s.records[%d] must be an object." % (label, i))
        state = []
        for key in ("home_handicap", "home_water", "away_water"):
            value = _number(record.get(key), "%s.records[%d].%s" % (label, i, key))
            if key.endswith("water") and value <= 0:
                raise InputProblem("invalid_input", "Hong Kong water values must be positive.")
            value = round(value, 6)
            if key.endswith("water") and value <= 0:
                raise InputProblem("invalid_input", "Water must remain positive at six-decimal precision.")
            if key == "home_handicap" and value % 0.25 != 0:
                raise InputProblem("invalid_input", "home_handicap must be a multiple of 0.25.")
            state.append(value)
        state = tuple(state)
        if not clean or state != clean[-1]:
            clean.append(state)
    return {
        "match": match, "states": clean,
        "counts": {"original": len(records), "cleaned": len(clean),
                   "removed_consecutive_duplicates": len(records) - len(clean)},
        "handicaps": set(state[0] for state in clean),
    }


def _breakpoints(n):
    return [Fraction(0), Fraction(1)] if n == 1 else [Fraction(i, n - 1) for i in range(n)]


def _interpolate(values, progress):
    if len(values) == 1:
        return values[0]
    position = progress * (len(values) - 1)
    index = min(position.numerator // position.denominator, len(values) - 2)
    fraction = float(position - index)
    return values[index] + (values[index + 1] - values[index]) * fraction


def _square_integral(h, start, end):
    return h * (start * start + start * end + end * end) / 3.0


def _side_metrics(values_a, values_b, breaks, epsilon):
    ya = [_interpolate(values_a, u) for u in breaks]
    yb = [_interpolate(values_b, u) for u in breaks]
    direction_lengths = {"same_direction": Fraction(0), "opposite_direction": Fraction(0),
                         "one_flat": Fraction(0), "both_flat": Fraction(0)}
    rel_sq, level_sq = [], []
    # Work in baseline-removed coordinates to reduce cancellation in Pearson.
    xa = [value - values_a[0] for value in ya]
    xb = [value - values_b[0] for value in yb]
    integrals_a, integrals_b = [], []
    for k in range(len(breaks) - 1):
        h_exact = breaks[k + 1] - breaks[k]
        h = float(h_exact)
        slope_a = (ya[k + 1] - ya[k]) / h
        slope_b = (yb[k + 1] - yb[k]) / h
        sign_a = 1 if slope_a > epsilon else (-1 if slope_a < -epsilon else 0)
        sign_b = 1 if slope_b > epsilon else (-1 if slope_b < -epsilon else 0)
        if sign_a == 0 and sign_b == 0:
            bucket = "both_flat"
        elif sign_a == 0 or sign_b == 0:
            bucket = "one_flat"
        elif sign_a == sign_b:
            bucket = "same_direction"
        else:
            bucket = "opposite_direction"
        direction_lengths[bucket] += h_exact
        rel_sq.append(_square_integral(h, xa[k] - xb[k], xa[k + 1] - xb[k + 1]))
        level_sq.append(_square_integral(h, ya[k] - yb[k], ya[k + 1] - yb[k + 1]))
        integrals_a.append(h * (xa[k] + xa[k + 1]) / 2)
        integrals_b.append(h * (xb[k] + xb[k + 1]) / 2)
    mean_a, mean_b = math.fsum(integrals_a), math.fsum(integrals_b)
    variance_a, variance_b, covariance = [], [], []
    for k in range(len(breaks) - 1):
        h = float(breaks[k + 1] - breaks[k])
        a0, a1 = xa[k] - mean_a, xa[k + 1] - mean_a
        b0, b1 = xb[k] - mean_b, xb[k + 1] - mean_b
        variance_a.append(_square_integral(h, a0, a1))
        variance_b.append(_square_integral(h, b0, b1))
        covariance.append(h * (2 * a0 * b0 + a0 * b1 + a1 * b0 + 2 * a1 * b1) / 6)
    var_a, var_b = math.fsum(variance_a), math.fsum(variance_b)
    pearson = None
    if max(values_a) != min(values_a) and max(values_b) != min(values_b) and var_a > 0 and var_b > 0:
        pearson = max(-1.0, min(1.0, (math.fsum(covariance) / math.sqrt(var_a)) / math.sqrt(var_b)))
    active = 1 - direction_lengths["both_flat"]
    agreement = float(direction_lengths["same_direction"] / active) if active else None
    net_a, net_b = values_a[-1] - values_a[0], values_b[-1] - values_b[0]
    range_a, range_b = max(values_a) - min(values_a), max(values_b) - min(values_b)
    return {
        "direction_score": None if agreement is None else 100 * agreement,
        "direction_agreement": agreement,
        "active_coverage": float(active),
        "progress_fractions": {key: float(value) for key, value in direction_lengths.items()},
        "relative_rmse": math.sqrt(max(0.0, math.fsum(rel_sq))),
        "level_rmse": math.sqrt(max(0.0, math.fsum(level_sq))),
        "pearson": pearson,
        "net_change_a": net_a, "net_change_b": net_b,
        "net_change_gap": abs(net_a - net_b),
        "range_a": range_a, "range_b": range_b, "range_gap": abs(range_a - range_b),
        "start_gap": abs(values_a[0] - values_b[0]),
        "end_gap": abs(values_a[-1] - values_b[-1]),
    }


def compare_odds(a, b, config=None):
    """Return a JSON-serializable result, including structured input failures.

    Config: slope_epsilon defaults to 1e-9 water units per unit progress;
    channel_weights defaults to {"home": 0.5, "away": 0.5}. Positive weights
    are normalized. Channels with no directional evidence are excluded from the
    direction-score average, but remain in magnitude-error aggregation.

    status: ok / low_information / insufficient_points / no_directional_evidence
    / not_comparable / insufficient_data / invalid_input / numerical_error.
    Only status=ok is rankable. Two distinct states only define a single segment;
    three or four states provide low information. Five is an operational minimum,
    not a statistical significance or reliability guarantee.
    """
    result = {"algorithm_version": ALGORITHM_VERSION, "status": "invalid_input",
              "rankable": False, "direction_score": None, "relative_rmse": None,
              "net_change_gap": None, "notes": list(NOTES)}
    try:
        cfg = _configuration(config)
        result["config"] = cfg
        pa, pb = _prepare(a, "match_a"), _prepare(b, "match_b")
        result["match_a_id"], result["match_b_id"] = a["id"], b["id"]
        result["counts"] = {"match_a": pa["counts"], "match_b": pb["counts"]}
        mismatches = [key for key in META_FIELDS if a[key] != b[key]]
        if mismatches:
            raise InputProblem("not_comparable", "Metadata mismatch: " + ", ".join(mismatches))
        unsupported = [key for key, value in REQUIRED_VALUES.items() if a[key] != value]
        if unsupported:
            raise InputProblem("not_comparable", "Unsupported metadata: " + ", ".join(unsupported))
        if len(pa["handicaps"]) != 1 or len(pb["handicaps"]) != 1:
            raise InputProblem("not_comparable", "A handicap changes within an observation window.")
        if pa["handicaps"] != pb["handicaps"]:
            raise InputProblem("not_comparable", "The two constant handicaps differ.")
        result["home_handicap"] = next(iter(pa["handicaps"]))
        na, nb = len(pa["states"]), len(pb["states"])
        result["cleaned_point_count_ratio"] = max(na, nb) / min(na, nb)
        breaks = sorted(set(_breakpoints(na)) | set(_breakpoints(nb)))
        result["alignment"] = {"method": "union_of_exact_point_progress_breakpoints",
                               "breakpoint_count": len(breaks), "interval_count": len(breaks) - 1}
        metrics = {
            side: _side_metrics([s[index] for s in pa["states"]],
                                [s[index] for s in pb["states"]], breaks, cfg["slope_epsilon"])
            for side, index in (("home", 1), ("away", 2))
        }
        result["channels"] = metrics
        weights = cfg["channel_weights"]
        usable = [side for side in SIDES if weights[side] > 0 and metrics[side]["active_coverage"] > 0]
        if usable:
            denominator = sum(weights[side] for side in usable)
            result["direction_score"] = sum(weights[side] * metrics[side]["direction_score"]
                                            for side in usable) / denominator
        result["direction_channel_weights"] = {
            side: (weights[side] / sum(weights[s] for s in usable) if side in usable else 0.0)
            for side in SIDES
        }
        # Weighted L2 for both RMSEs; weighted means for the other diagnostics.
        for key in ("relative_rmse", "level_rmse"):
            result[key] = math.hypot(*[
                math.sqrt(weights[side]) * metrics[side][key] for side in SIDES])
        for key in ("net_change_gap", "range_gap", "start_gap", "end_gap", "active_coverage"):
            result[key] = sum(weights[side] * metrics[side][key] for side in SIDES)
        dynamic_counts = [n for n in (na, nb) if n > 1]
        if not usable:
            result["status"] = "no_directional_evidence"
            result["reason"] = "No positively weighted channel has nonflat directional evidence."
        elif 2 in dynamic_counts:
            result["status"] = "insufficient_points"
            result["reason"] = "A dynamic path has only two distinct consecutive states."
        elif any(n in (3, 4) for n in dynamic_counts):
            result["status"] = "low_information"
            result["reason"] = "A dynamic path has only three or four distinct consecutive states."
        else:
            result["status"], result["rankable"] = "ok", True
        # Reject any overflow rather than returning invalid JSON NaN/Infinity.
        json.dumps(result, allow_nan=False)
        return result
    except InputProblem as error:
        result.update(status=error.status, rankable=False, reason=str(error),
                      direction_score=None, relative_rmse=None, net_change_gap=None)
        return result
    except (OverflowError, ArithmeticError, ValueError) as error:
        return {"algorithm_version": ALGORITHM_VERSION, "status": "numerical_error",
                "rankable": False, "direction_score": None, "relative_rmse": None,
                "net_change_gap": None, "reason": str(error), "notes": list(NOTES)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="JSON file containing match_a, match_b and optional config")
    parser.add_argument("--output", help="Write result JSON here; otherwise print to stdout")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Input JSON must be an object.")
        result = compare_odds(payload.get("match_a"), payload.get("match_b"), payload.get("config"))
    except (OSError, UnicodeError, ValueError) as error:
        result = {"algorithm_version": ALGORITHM_VERSION, "status": "invalid_input",
                  "rankable": False, "direction_score": None, "reason": str(error)}
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        try:
            Path(args.output).write_text(rendered, encoding="utf-8")
        except OSError as error:
            print(json.dumps({"status": "output_error", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
            return 2
    else:
        sys.stdout.write(rendered)
    return 2 if result["status"] in {"invalid_input", "insufficient_data", "numerical_error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
