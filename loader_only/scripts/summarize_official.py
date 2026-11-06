"""Compare official-testbench summary.csv files against the baseline (same idea as compare.py).

    python summarize_official.py ../meas  [case1,case2,...]
Prints cycles, speed-up, A/B reads and read change for every variant folder under ../meas.
"""
import csv, sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else '../meas')
cases = sys.argv[2].split(',') if len(sys.argv) > 2 else \
    ['rectangular', 'sparse_90', 'dense_benchmark', 'sparse_benchmark', 'long_latency',
     'bench64', 'bench64_lat23', 'bench64_sp90', 'bench_64x128x128', 'max_dimensions']
order = ['baseline', 's1_areuse', 's1_areuse_zs', 's1_dbuf', 's1', 's2_b4k', 's2', 's3']
data = {}
for d in root.iterdir():
    f = d/'summary.csv'
    if d.is_dir() and f.exists():
        data[d.name] = {(r['case'], r['run']): r for r in csv.DictReader(open(f))}
base = data['baseline']
for c in cases:
    k = (c, '0')
    if k not in base: continue
    b = base[k]; br = int(b['a_reads']) + int(b['b_reads'])
    print(f"\n{c}  (M={b['m']} N={b['n']} K={b['k']})")
    print(f"  {'variant':13s} {'cycles':>10s} {'speed-up':>9s} {'A reads':>9s} {'B reads':>9s} {'reads':>9s} {'vs base':>8s}")
    for v in order:
        if v in data and k in data[v]:
            r = data[v][k]; rd = int(r['a_reads']) + int(r['b_reads'])
            print(f"  {v:13s} {int(r['cycles']):10d} {int(b['cycles'])/int(r['cycles']):8.2f}x "
                  f"{int(r['a_reads']):9d} {int(r['b_reads']):9d} {rd:9d} {100*(rd-br)/br:+7.1f}%")
