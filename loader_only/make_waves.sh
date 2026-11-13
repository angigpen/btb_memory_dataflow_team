#!/bin/bash
# Full official suite + waveforms + 64x128x128 buffer sweep for the HTML viewer (Icarus, ~30 min).
#   ./make_waves.sh            then   python3 scripts/vcd_to_html.py   -> build/waves.html
cd "$(dirname "$0")/scripts"
W=demo_8x8x4,demo_20x12x6,rectangular,long_latency      # short: whole run recorded
L=bench64,bench_64x128x128_l23                           # long: baseline / Set 1 only the first 160k cycles
B=bench_64x128x128,bench_64x128x128_l23
mkdir -p ../build ../results; rm -f ../build/fails.txt ../build/done.txt
run(){ python3 run_official.py --extended "$@" > /dev/null 2>&1 || echo "FAIL $*" >> ../build/fails.txt; }
suite(){ run --rtl-dir ../$1 --suite $2 --build ../build/suite_$1 && cp ../build/suite_$1/summary.csv ../results/$3.csv; }
{ suite baseline --baseline-checks baseline; echo suite_baseline >> ../build/done.txt; } &
{ suite set1 "" s1; echo suite_set1 >> ../build/done.txt; } &
{ suite set2 "" s2; echo suite_set2 >> ../build/done.txt; } &
{ suite set3 "" s3; echo suite_set3 >> ../build/done.txt; } &
{ suite set4 "" s4; echo suite_set4 >> ../build/done.txt; } &
{
 run --rtl-dir ../baseline --baseline-checks --cases $W --vcd --build ../build/baseline
 run --rtl-dir ../baseline --baseline-checks --cases $L --vcd --vcd-cycles 160000 --build ../build/baseline_big
 run --rtl-dir ../set1 --cases $W --vcd --build ../build/set1
 run --rtl-dir ../set1 --cases $L --vcd --vcd-cycles 160000 --build ../build/set1_big
 echo waves_A >> ../build/done.txt
} &
{
 run --rtl-dir ../set2 --cases $W,$L --vcd --build ../build/set2
 run --rtl-dir ../set3 --cases $W,$L --vcd --build ../build/set3
 echo waves_B >> ../build/done.txt
} &
{
 run --rtl-dir ../set4 --cases $W,$L --vcd --build ../build/set4
 run --rtl-dir ../set2 --cases $B --param BBUF_DEPTH=1024 --build ../build/sweep/s2_buf4k
 for d in 256:s3_buf1k 512:s3_buf2k 2048:s3_buf8k 4096:s3_buf16k; do
   run --rtl-dir ../set3 --cases $B --param BBUF_DEPTH=${d%%:*} --param control.BBUF_DEPTH=${d%%:*} --build ../build/sweep/${d#*:}
 done
 echo waves_C >> ../build/done.txt
} &
wait
echo ALL >> ../build/done.txt
