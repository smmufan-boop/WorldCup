#!/usr/bin/env bash
# Rebuild every research result and the forward-rule backtests from the snapshot, in dependency order.
# Run from the repository root:  bash odds-trajectory/research/run_all.sh
# Takes about 15-20 minutes on 4 cores; intermediate data goes to research/cache/ (not committed).
set -euo pipefail
R=odds-trajectory/research
F=odds-trajectory/forward
step() { echo; echo "=== $* ==="; }

step "1. Royal T-10 inputs and V1 replay";            python $R/build_inputs.py --stage 10
                                                     python $R/replay_v1.py --stage 10
step "2. Reliability marks and diagnostics";          python $R/reliability.py > /dev/null
                                                     python $R/diagnostics.py > /dev/null
step "3. 澳彩 inputs and V1 replay (alpha=50)";        python $R/build_inputs.py --company 1
                                                     python $R/replay_v1.py --company 1
step "4. Earlier stages T-30/T-60/T-120";
for s in 30 60 120; do
  python $R/build_inputs.py --stage $s
  python $R/replay_v1.py --stage $s
done
step "5. Cross-company 澳彩 vs Royal";
for s in 10 60 120; do python $R/cross_company.py --stage $s > /dev/null; done
step "6. Stage comparison";                           python $R/stages.py > /dev/null
step "7. Win-rate re-scoring";                        python $R/winrate.py
step "8. Rule + V1 confirmation";                     python $R/rule_calibration.py > /dev/null
step "9. Gates from manual analyses";                 python $R/gates.py > /dev/null
step "10. Further improvements";                      python $R/improve.py > /dev/null
step "11. Forward rule and shadow backtests";         python $F/forward_rule.py backtest
step "12. T-10 signal correction power";              python $F/t10_correction.py
echo; echo "Done. Results: $R/results/*.json, $F/backtest-*.json"
