"""Summarize local results: cycles and reads vs baseline for key cases."""
import csv, glob, sys
from pathlib import Path
rows=[]
for f in glob.glob(sys.argv[1] if len(sys.argv)>1 else 'out/*/local_results_mo0.csv'):
    rows+=list(csv.DictReader(open(f)))
idx={(r['variant'],r['case'],r['run']):r for r in rows}
variants=['baseline','s1_areuse','s1_areuse_zs','s1_dbuf','s1_all','s2_p1_2a','s2_2b_only','s2_p1_2','s2_all']
cases=sys.argv[2].split(',') if len(sys.argv)>2 else ['rectangular','sparse_90','long_latency','bench64_lat1','bench64_lat23','bench64_sp90_lat23']
for c in cases:
    b=idx.get(('baseline',c,'0'))
    if not b: continue
    print(f'\n{c}  (m={b["m"]} n={b["n"]} k={b["k"]} latency={b["lat"]})')
    print(f'{"variant":14s} {"cycles":>9s} {"speedup":>8s} {"reads":>9s} {"reads vs base":>13s}')
    for v in variants:
        r=idx.get((v,c,'0'))
        if not r: continue
        rd=int(r['a_reads'])+int(r['b_reads']); brd=int(b['a_reads'])+int(b['b_reads'])
        print(f'{v:14s} {int(r["cycles"]):9d} {int(b["cycles"])/int(r["cycles"]):7.2f}x {rd:9d} {100*(rd-brd)/brd:+12.1f}%')
