#!/bin/bash
# MACR simplified model - runs on macOS with Python 3 (about 5-15 minutes)
set -e
python3 -m pip install --user numpy scipy matplotlib
echo "== 1/4 main experiments (680 runs) =="; python3 experiments.py 20
echo "== 2/4 buffer sweep (180 runs) ==";    python3 sweep.py
echo "== 3/4 figures =="                     ; python3 analyse.py > analyse_log.txt
echo "== 4/4 summary tables =="             ; python3 summary_tables.py
echo "Done. Figures: fig_t90.png fig_ts.png fig_harm.png"
