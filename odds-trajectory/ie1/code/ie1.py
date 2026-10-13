#!/usr/bin/env python3
"""IE1 command line: probe, T-10 run loop, manual notes, settlement, stats, offline.

Run from the repository root, e.g.:
  python odds-trajectory/ie1/code/ie1.py probe --match-id 54580227 --kickoff 2026-10-14T19:30:00+08:00
  python odds-trajectory/ie1/code/ie1.py run --fixtures fixtures.csv --loop
  python odds-trajectory/ie1/code/ie1.py note --match-id 54580227 --supremacy -0.05 --notes "主力前锋伤缺"
  python odds-trajectory/ie1/code/ie1.py settle --results results.csv
  python odds-trajectory/ie1/code/ie1.py close
  python odds-trajectory/ie1/code/ie1.py stats
  python odds-trajectory/ie1/code/ie1.py offline --input match.json
  python odds-trajectory/ie1/code/ie1.py replay --match-id 54569678 --cache fixtures.pkl
See ../RUNBOOK.md for the full operating procedure.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from engine import load_parameters, predict, render
from ledger import Ledger
from clv import clv
from parse import minute_order_evidence, rows_from_payload
from quotes import BJT, clock, market_book
from settle import pick_profit
from source import Fetcher, SourceBlocked, load_fixtures_csv, load_fixtures_db

COMPANIES = ("3", "1")
MARKETS = ("asia", "size", "euro")


def now():
    return time.time()


def stamp(ts):
    return datetime.fromtimestamp(ts, BJT).isoformat(timespec="seconds")


def workdir(args):
    path = Path(args.workdir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_notes(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def manual_for(notes, match_id):
    note = notes.get(str(match_id))
    if not note:
        return None
    return {"recordedAt": clock(note["recordedAt"]), "supremacy": note.get("supremacy", 0.0),
            "total": note.get("total", 0.0), "notes": note.get("notes", "")}


def fetch_book(fetcher, meta, params):
    """Fetch 2 companies x 3 markets, parse, and build the T-10 market book."""
    fields = params.get("payload")
    rows_by_company, order, evidence, problems = {}, {}, [], {}
    for company in COMPANIES:
        rows = []
        for market in MARKETS:
            payload, info = fetcher.market(meta["matchId"], company, market)
            if payload is None:
                continue
            parsed, issues = rows_from_payload(payload, market, meta["matchId"], company, meta["kickoff"], fields)
            rows += parsed
            evidence.append({"company": company, "market": market, **info, "rows": len(parsed)})
            if issues:
                problems[f"{company}/{market}"] = issues
        rows_by_company[company] = rows
        order[company] = minute_order_evidence(rows)
    cutoff = meta["kickoff"] - params["cutoffSeconds"]
    book = market_book(rows_by_company, meta["kickoff"], cutoff, order)
    return book, rows_by_company, evidence, problems


def cmd_probe(args):
    params = load_parameters()
    wd = workdir(args)
    meta = {"matchId": args.match_id, "competition": args.competition, "home": args.home, "away": args.away,
            "kickoff": clock(args.kickoff)}
    if meta["kickoff"] is None:
        sys.exit("--kickoff must be ISO time with +08:00")
    fetcher = Fetcher(wd / "evidence", params["fetch"]["minIntervalSeconds"], params["fetch"]["timeoutSeconds"])
    try:
        book, rows, evidence, problems = fetch_book(fetcher, meta, params)
    except (SourceBlocked, RuntimeError) as exc:
        sys.exit(f"抓取失败：{exc}")
    for e in evidence:
        print(f"[{e['company']}/{e['market']}] rows={e['rows']} saved={e['path']}")
    for key, issues in problems.items():
        print(f"[{key}] {len(issues)} rows dropped, e.g.: " + "; ".join(issues[:3]))
    for company, rs in rows.items():
        for r in [r for r in rs if r["market_type"] == "asia"][-2:] + [r for r in rs if r["market_type"] != "asia"][-2:]:
            print(f"  {company} {r['market_type']} {r['change_time_local']} raw={r['raw']} -> "
                  f"line={r.get('handicap_value')} {r.get('home_water')}/{r.get('away_water')} score='{r['score']}'")
    for company, b in book.items():
        print(f"book {company}: asian={len(b['asian'])} ({b['asianError'] or 'ok'}) totals={len(b['totals'])} "
              f"euro={len(b['euro'])}")
    if now() >= meta["kickoff"] - params["cutoffSeconds"]:
        print(render(predict(meta, book, params)))
    else:
        print("截点未到：上面只核对解析；正式结论须在 T−10 之后由 run 生成。")


def due(fixtures, ledger, params, t):
    cutoff = params["cutoffSeconds"]
    grace = params["fetch"]["runGraceSeconds"]
    return [f for f in fixtures if f["kickoff"] - cutoff <= t < f["kickoff"] + grace and not ledger.has_run(f)]


def run_one(fetcher, ledger, meta, params, notes, out_dir):
    book, _, evidence, problems = fetch_book(fetcher, meta, params)
    manual = manual_for(notes, meta["matchId"])
    result = predict(meta, book, params, manual=manual)
    generated = now()
    result["generatedAt"] = stamp(generated)
    result["parseProblems"] = {k: v[:20] for k, v in problems.items()}
    report = render(result)
    inputs = {"meta": meta, "book": book, "manual": manual, "evidence": evidence}
    run_id, status, registered = ledger.record(meta, inputs, params, result, report, generated)
    day = datetime.fromtimestamp(meta["kickoff"], BJT).strftime("%Y-%m-%d")
    path = out_dir / day / f"{meta['matchId']}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = f"\n\n生成时间 {result['generatedAt']}｜run #{run_id}｜{status}" + ("｜已登记" if registered else "")
    path.write_text(report + tail + "\n", encoding="utf-8")
    return status, registered, result["decision"]["text"]


def close_due(fetcher, ledger, params, t):
    """Fill closing-line value for tracked bets whose match has kicked off."""
    from clv import closing_book, closing_model
    done = 0
    for row in ledger.open_clv(t - params["clv"]["delaySeconds"]):
        rows, order = {}, {}
        ref = params["referenceCompany"]
        meta = {"matchId": row["match_id"], "kickoff": row["kickoff"]}
        for market in ("asia", "size"):
            payload, _ = fetcher.market(row["match_id"], ref, market)
            if payload is not None:
                parsed, _ = rows_from_payload(payload, market, row["match_id"], ref, row["kickoff"],
                                              params.get("payload"))
                rows.setdefault(ref, []).extend(parsed)
        order[ref] = minute_order_evidence(rows.get(ref, []))
        book = closing_book(rows, meta["kickoff"], order)
        model = closing_model(book.get(ref) or {}, params["rho"], row["fallback_total"])
        if model is None:
            ledger.set_clv(row["run_id"], None, now(), (book.get(ref) or {}).get("asianError") or "无收盘亚盘")
        else:
            data = clv({"market": row["market"], "side": row["side"], "line": row["line"], "odds": row["odds"]},
                       model)
            ledger.set_clv(row["run_id"], data, now())
        done += 1
    return done


def cmd_close(args):
    params = load_parameters()
    wd = workdir(args)
    ledger = Ledger(wd / "ledger.sqlite3")
    fetcher = Fetcher(wd / "evidence", params["fetch"]["minIntervalSeconds"], params["fetch"]["timeoutSeconds"])
    try:
        print(f"已补 {close_due(fetcher, ledger, params, now())} 场收盘价。")
    except SourceBlocked as exc:
        print(f"数据源拒绝访问，已停止：{exc}")
        return 2
    finally:
        ledger.close()


def cmd_run(args):
    params = load_parameters()
    wd = workdir(args)
    ledger = Ledger(wd / "ledger.sqlite3")
    fetcher = Fetcher(wd / "evidence", params["fetch"]["minIntervalSeconds"], params["fetch"]["timeoutSeconds"])
    try:
        while True:
            t = now()
            if args.fixtures:
                fixtures = load_fixtures_csv(args.fixtures)
            else:
                fixtures = load_fixtures_db(args.db, t - 86400, t + 86400)
            for meta in due(fixtures, ledger, params, t):
                try:
                    status, registered, text = run_one(fetcher, ledger, meta, params, load_notes(wd / "manual.json"),
                                                       wd / "reports")
                except SourceBlocked as exc:
                    print(f"{stamp(now())} 数据源拒绝访问，已停止：{exc}", flush=True)
                    return 2
                except (RuntimeError, ValueError) as exc:
                    print(f"{stamp(now())} {meta['matchId']} 失败：{exc}", flush=True)
                    continue
                print(f"{stamp(now())} {meta['home']} vs {meta['away']}：{text}"
                      + ("｜已登记" if registered else "") + (f"｜{status}" if status != "ok" else ""), flush=True)
            try:
                close_due(fetcher, ledger, params, now())
            except SourceBlocked as exc:
                print(f"{stamp(now())} 数据源拒绝访问，已停止：{exc}", flush=True)
                return 2
            except (RuntimeError, ValueError) as exc:
                print(f"{stamp(now())} 收盘价补取失败：{exc}", flush=True)
            if not args.loop:
                return 0
            time.sleep(params["fetch"]["scanSeconds"])
    finally:
        ledger.close()


def cmd_note(args):
    wd = workdir(args)
    path = wd / "manual.json"
    notes = load_notes(path)
    notes[str(args.match_id)] = {"recordedAt": stamp(now()), "supremacy": args.supremacy, "total": args.total,
                                 "notes": args.notes}
    path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已记录 {args.match_id}（{notes[str(args.match_id)]['recordedAt']}）；截点后录入的不会被采用。")


def results_from_csv(path):
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            out[row["match_id"].strip()] = (int(row["score_home"]), int(row["score_away"]))
    return out


def results_from_db(path):
    import sqlite3

    from dataset import regular_time_score
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return {r["match_id"]: s for r in db.execute("SELECT * FROM matches WHERE status='Played'")
                if (s := regular_time_score(dict(r))) is not None}
    finally:
        db.close()


def cmd_settle(args):
    wd = workdir(args)
    ledger = Ledger(wd / "ledger.sqlite3")
    scores = results_from_csv(args.results) if args.results else results_from_db(args.db)
    done = 0
    for p in ledger.pending():
        score = scores.get(p["match_id"])
        if score is None:
            continue
        profit = pick_profit(p["market"], p["side"], p["line"], p["odds"], score)
        ledger.settle(p["fixture_key"], score, profit, now())
        done += 1
    print(f"结算 {done} 注；待结算 {len(ledger.pending())} 注。")
    ledger.close()


def cmd_stats(args):
    params = load_parameters()
    wd = workdir(args)
    ledger = Ledger(wd / "ledger.sqlite3")
    picks = ledger.picks()
    clv_rows = ledger.clv_rows()
    ledger.close()
    settled = [p for p in picks if p["settled_at"] is not None]
    print(f"登记 {len(picks)} 注，已结算 {len(settled)} 注。")
    for tier in ("A", "B", None):
        sel = [p for p in settled if tier is None or p["tier"] == tier]
        if not sel:
            continue
        flat = [p["profit"] for p in sel]
        n = len(flat)
        mean = sum(flat) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in flat) / max(n - 1, 1))
        staked = sum(p["units"] for p in sel)
        pnl = sum(p["units"] * p["profit"] for p in sel)
        print(f"{tier or '合计'}：{n} 注，平注 ROI {mean:+.1%}（95% 下限 {mean - 1.96 * sd / math.sqrt(n):+.1%}），"
              f"按单位 {staked:g} 单位盈亏 {pnl:+.2f}")
    gate = params["goLive"]
    flat = [p["profit"] for p in settled]
    if flat:
        n, mean = len(flat), sum(flat) / len(flat)
        sd = math.sqrt(sum((x - mean) ** 2 for x in flat) / max(n - 1, 1))
        ok = n >= gate["minSettled"] and mean - 1.96 * sd / math.sqrt(n) > gate["minLowerBoundRoi"]
        print(("已" if ok else "未") + f"达到实盘门槛（≥{gate['minSettled']} 注且平注 ROI 95% 下限 > "
              f"{gate['minLowerBoundRoi']:.0%}）。")
    print_clv(clv_rows, params)


def _clv_line(label, rows):
    if not rows:
        return
    c = [r["clv"] for r in rows]
    n, mean = len(c), sum(c) / len(c)
    sd = math.sqrt(sum((x - mean) ** 2 for x in c) / max(n - 1, 1))
    print(f"  {label}：{n} 场，平均 CLV {mean:+.2%}（95% 下限 {mean - 1.96 * sd / math.sqrt(n):+.2%}），"
          f"胜过收盘 {sum(x > 0 for x in c) / n:.0%}")


def print_clv(rows, params):
    print("收盘价对比（CLV，参考 Royal 开球前最后报价）：")
    if not rows:
        print("  暂无已收盘的记录。")
        return
    gate = params["gates"]
    _clv_line("已登记推荐", [r for r in rows if r["kind"] == "pick"])
    cand = [r for r in rows if r["kind"] == "candidate"]
    _clv_line(f"未登记·EV≥{gate['evTierB']:.0%}·报价过旧作废",
              [r for r in cand if r["ev"] >= gate["evTierB"] and (r["age_minutes"] or 0) > gate["maxStakeQuoteAgeMinutes"]])
    _clv_line(f"未登记·EV≥{gate['evTierB']:.0%}·其他原因（阻断/降为C/否决）",
              [r for r in cand if r["ev"] >= gate["evTierB"] and (r["age_minutes"] or 0) <= gate["maxStakeQuoteAgeMinutes"]])
    _clv_line(f"未登记·0≤EV<{gate['evTierB']:.0%}", [r for r in cand if 0 <= r["ev"] < gate["evTierB"]])
    _clv_line("未登记·EV<0", [r for r in cand if r["ev"] < 0])


def _rows_from_manual(match_id, company, kickoff, quotes):
    from parse import goal_line, handicap_goals
    rows = []
    for market, items in quotes.items():
        mt = {"asian": "asia", "totals": "size", "euro": "euro"}[market]
        for q in items:
            t = datetime.fromisoformat(q["time"]).replace(tzinfo=BJT) if "+" not in q["time"] else \
                datetime.fromisoformat(q["time"])
            row = {"match_id": match_id, "company_id": company, "market_type": mt, "score": "",
                   "change_time_local": t.isoformat(timespec="minutes")}
            if mt == "asia":
                g = handicap_goals(q["handicap"])
                row.update(handicap_text=q["handicap"], handicap_value=f"{abs(g):.2f}",
                           home_water=str(q["home"]), away_water=str(q["away"]))
            elif mt == "size":
                row.update(handicap_value=f"{goal_line(q['line']):.2f}", home_water=str(q["over"]),
                           away_water=str(q["under"]))
            else:
                row.update(home_water=str(q["home"]), handicap_value=str(q["draw"]), away_water=str(q["away"]))
            rows.append(row)
    return rows


def cmd_offline(args):
    """Hand-entered odds (e.g. copied from a client) -> the same report, no ledger."""
    params = load_parameters()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    m = data["match"]
    kickoff = clock(m["kickoff"])
    meta = {"matchId": m.get("matchId", "manual"), "competition": m.get("competition", ""), "home": m["home"],
            "away": m["away"], "kickoff": kickoff}
    rows = {c: _rows_from_manual(meta["matchId"], c, kickoff, q) for c, q in data["companies"].items()}
    book = market_book(rows, kickoff, kickoff - params["cutoffSeconds"])
    manual = data.get("manual")
    if manual:
        manual = {**manual, "recordedAt": clock(manual["recordedAt"])}
    print(render(predict(meta, book, params, manual=manual)))


def cmd_replay(args):
    """Rebuild one snapshot match at T-10 and print the report plus its settlement."""
    import dataset
    params = load_parameters()
    fixtures = dataset.build(args.cache)
    f = next((x for x in fixtures if x["matchId"] == args.match_id), None)
    if f is None:
        sys.exit("快照中没有这场比赛（需同时有 Royal 亚盘与欧指/大小球任务）")
    meta = {k: f[k] for k in ("matchId", "competition", "home", "away", "kickoff")}
    result = predict(meta, f["book"], params)
    print(render(result))
    pick, score = result.get("pick"), f["score"]
    print(f"\n回看：常规 90 分钟比分 {score[0]}-{score[1]}" if score else "\n回看：比分未核验")
    if score and pick and result["decision"]["tier"]:
        profit = pick_profit(pick["market"], pick["side"], pick["line"], pick["odds"], score)
        print(f"按锁定条款结算：平注 {profit:+.3f}，按 {result['decision']['units']:g} 单位 "
              f"{profit * result['decision']['units']:+.3f}（赛后回看，非当时前瞻记录）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="IE1 盘赔意图–价值方案执行工具")
    ap.add_argument("--workdir", default="ie1-runtime", help="台账、证据、报告与人工信息的目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="抓取一场比赛并核对解析结果")
    p.add_argument("--match-id", required=True)
    p.add_argument("--kickoff", required=True)
    p.add_argument("--home", default="主队")
    p.add_argument("--away", default="客队")
    p.add_argument("--competition", default="")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("run", help="到 T−10 的比赛逐场计算并登记")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixtures", help="CSV: match_id,competition,home,away,kickoff_local")
    src.add_argument("--db", help="V1 结构的 SQLite（读取 matches 表）")
    p.add_argument("--loop", action="store_true", help="持续扫描，直到被中断或数据源拒绝")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("note", help="截点前录入信息面（M6）")
    p.add_argument("--match-id", required=True)
    p.add_argument("--supremacy", type=float, default=0.0, help="净胜球修正，正数利主队，上限 ±0.15")
    p.add_argument("--total", type=float, default=0.0, help="总进球修正，上限 ±0.15")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_note)
    p = sub.add_parser("settle", help="按常规 90 分钟比分结算")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--results", help="CSV: match_id,score_home,score_away（常规 90 分钟）")
    src.add_argument("--db", help="V1 结构的 SQLite（按核验规则取比分）")
    p.set_defaults(func=cmd_settle)
    p = sub.add_parser("close", help="开球后补取 Royal 收盘价并计算 CLV（run --loop 会自动执行）")
    p.set_defaults(func=cmd_close)
    p = sub.add_parser("stats", help="前瞻台账统计、实盘门槛与 CLV")
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser("offline", help="手工录入赔率生成报告（不登记）")
    p.add_argument("--input", required=True)
    p.set_defaults(func=cmd_offline)
    p = sub.add_parser("replay", help="用仓库快照回看一场比赛的 T−10 报告与结算")
    p.add_argument("--match-id", required=True)
    p.add_argument("--cache", help="dataset 缓存文件（首次约 1–2 分钟）")
    p.set_defaults(func=cmd_replay)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
