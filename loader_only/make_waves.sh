#!/bin/bash
# Waveforms + 64x128x128 buffer sweep for the HTML viewer (official testbench, Icarus).
cd "$(dirname "$0")/scripts"
W=rectangular,sparse_90,long_latency
B=bench_64x128x128,bench_64x128x128_l23
run(){ python3 run_official.py --extended "$@" > /dev/null 2>&1 || echo "FAIL $*" >> ../build/fails.txt; }
{
 run --rtl-dir ../baseline --baseline-checks --cases $W --vcd --build ../build/baseline
 run --rtl-dir ../baseline --baseline-checks --cases bench_64x128x128_l23 --vcd --vcd-cycles 160000 --build ../build/baseline_big
 run --rtl-dir ../set1 --cases $W --vcd --build ../build/set1
 run --rtl-dir ../set1 --cases bench_64x128x128_l23 --vcd --vcd-cycles 160000 --build ../build/set1_big
 echo A >> ../build/done.txt
} &
{
 run --rtl-dir ../set2 --cases $W,bench_64x128x128_l23 --vcd --build ../build/set2
 run --rtl-dir ../set3 --cases $W,bench_64x128x128_l23 --vcd --build ../build/set3
 run --rtl-dir ../set2 --cases $B --param BBUF_DEPTH=1024 --build ../build/sweep/s2_buf4k
 for d in 256:s3_buf1k 512:s3_buf2k 2048:s3_buf8k 4096:s3_buf16k; do
   run --rtl-dir ../set3 --cases $B --param BBUF_DEPTH=${d%%:*} --param control.BBUF_DEPTH=${d%%:*} --build ../build/sweep/${d#*:}
 done
 echo B >> ../build/done.txt
} &
wait
