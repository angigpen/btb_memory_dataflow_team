"""Compare result CSVs against the baseline (same idea as compare.py).

    python summarize_official.py ../results  [case1,case2,...]  > ../results/summary.txt
Reads <dir>/<variant>.csv (run_official.py summary.csv format) and prints cycles, speed-up,
A/B reads and read change for every variant.
"""
import csv, sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else '../results')
cases = sys.argv[2].split(',') if len(sys.argv) > 2 else \
    ['demo_8x8x4', 'demo_20x12x6', 'rectangular', 'sparse_90', 'dense_benchmark', 'sparse_benchmark', 'long_latency',
     'bench64', 'bench64_lat23', 'bench64_sp90', 'bench_64x128x128', 'bench_64x128x128_l23',
     'bench_64x64x256', 'bench_64x128x512', 'max_dimensions']
order = ['baseline', 's1_areuse', 's1_dbuf', 's1', 's2_b4k', 's2', 's3', 's4_nb1', 's4_noshare', 's4_nob', 's4']
data = {f.stem: {(r['case'], r['run']): r for r in csv.DictReader(open(f))} for f in root.glob('*.csv')}
base = data['baseline']
for c in cases:
    k = (c, '0')
    if k not in base: continue
    b = base[k]; br = int(b['a_reads']) + int(b['b_reads'])
    print(f"\n{c}  (M={b['m']} N={b['n']} K={b['k']}, latency {b.get('latency', '?')})")
    print(f"  {'variant':13s} {'cycles':>10s} {'speed-up':>9s} {'A reads':>9s} {'B reads':>9s} {'reads':>9s} {'vs base':>8s}")
    for v in order:
        if v in data and k in data[v]:
            r = data[v][k]; rd = int(r['a_reads']) + int(r['b_reads'])
            print(f"  {v:13s} {int(r['cycles']):10d} {int(b['cycles'])/int(r['cycles']):8.2f}x "
                  f"{int(r['a_reads']):9d} {int(r['b_reads']):9d} {rd:9d} {100*(rd-br)/br:+7.1f}%")
