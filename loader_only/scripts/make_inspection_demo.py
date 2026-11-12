"""Add a small, actually simulated matrix to an existing waveform viewer.

python3 scripts/make_inspection_demo.py
Uses Icarus Verilog and the unchanged official testbench for all four variants.
"""
import argparse
import csv
import json
import shutil
import subprocess
from pathlib import Path

from generate import make_case
from run_official import SOURCES
from vcd_to_html import ROOT, ANIM, TEMPLATE, parse_vcd, trim, encode
from buffer_trace import add_buffer_traces
from packet_trace import add_packet_traces

CASE = 'inspect_8x8x6'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--html', type=Path, default=ROOT/'build/waves.html')
    ap.add_argument('--iverilog', default='iverilog')
    ap.add_argument('--vvp', default='vvp')
    ap.add_argument('--ivl-dir', type=Path, help='Support directory for a standalone Icarus package')
    args = ap.parse_args()
    source = args.html.read_text()
    payload, _ = json.JSONDecoder().raw_decode(source[source.index('const P=')+8:])
    build = ROOT/'build'
    fixture = build/'set2/vectors'/CASE
    make_case(fixture, m=8, n=8, k=6, pattern='inspection', latency=3, seed=42)

    def read_hex(kind, bits):
        values = [int(s,16) for s in (fixture/(kind+'.hex')).read_text().split()]
        return [v-(1<<bits) if v >= (1<<(bits-1)) else v for v in values]

    matrices = {x: read_hex(x,32 if x=='c' else 8) for x in ('a','b','c')}
    fresh = dict(sets={}, metrics={})
    for variant, folder in [('baseline','baseline'),('s1_all','set1'),('s2_all','set2'),('s3_all','set3')]:
        out = build/'inspection'/folder
        out.mkdir(parents=True, exist_ok=True)
        for name in ('a.hex','b.hex','c.hex','config.hex'):
            shutil.copy2(fixture/name, out/name)
        tb = out/'tb_configured.sv'
        tb.write_text('`define PR 4\n`define PC 4\n'+(ROOT/'tb/tb_gemm.sv').read_text())
        exe = out/'sim.vvp'
        sources = [ROOT/folder/name if (ROOT/folder/name).exists() else ROOT/'baseline'/name for name in SOURCES]
        compiler = [args.iverilog] + (['-B',str(args.ivl_dir)] if args.ivl_dir else [])
        cmd = compiler+['-g2012','-o',str(exe),str(ROOT/'tb/rv_monitor.sv'),str(tb)]+[str(p) for p in sources]+[str(ROOT/'tb/wave_dump.v')]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        vcd = out/('wave_'+CASE+'.vcd')
        runtime = [args.vvp] + (['-M',str(args.ivl_dir)] if args.ivl_dir else [])
        result = subprocess.run(runtime+['-n',str(exe),'+HOST_TEST',f'+VCD={vcd.name}']+
                                ([] if variant=='baseline' else ['+OPTIMIZED']),
                                cwd=out, capture_output=True, text=True, timeout=120)
        if result.returncode or 'GEMM REGRESSION PASS' not in result.stdout:
            raise RuntimeError(f'{variant}: simulation failed\n{result.stdout}\n{result.stderr}')
        spec = payload['sets'][variant]
        data, n = parse_vcd(vcd, {s['path'] for s in spec['signals']} | set(ANIM[variant]))
        data, n, offset, truncated = trim(data, n)
        assert not truncated
        wave = dict(n=n, offset=offset, trunc=False, data={p:encode(ch) for p,ch in data.items()}, matrices=matrices)
        row = next(csv.DictReader((out/'metrics.csv').open()))
        metrics = {k:row[k] for k in ('m','n','k','cycles','a_reads','b_reads','operand_wait')}
        metrics['lat']='3'
        fresh['sets'][variant] = dict(params=spec['params'], waves={CASE:wave})
        fresh['metrics'][variant+'|'+CASE] = metrics
        print(f'PASS {variant}: {row["cycles"]} cycles, A reads {row["a_reads"]}, B reads {row["b_reads"]}', flush=True)
    add_buffer_traces(fresh, build)
    add_packet_traces(fresh, build)
    for spec in fresh['sets'].values():
        wave = spec['waves'][CASE]
        markers, rows = {}, {}
        for e in wave.get('packets',{}).get('events',[]):
            at, slot, op, *data = e
            if op==1:
                rows[slot]=data[0]
            elif op==2:
                a,b,skip=data
                if a is not None: markers.setdefault('A 재사용',at)
                if b is not None: markers.setdefault('B 재사용',at)
                if skip: markers.setdefault('B zero skip',at)
            elif op==3 and data[0]==1 and rows.get(slot,0)>=4:
                markers.setdefault('다음 행의 B 외부 읽기' if spec is fresh['sets'].get('s1_all') else 'B 미저장분 재읽기',at-1)
        wave['markers']=markers
    for variant in payload['order']:
        payload['sets'][variant]['waves'][CASE] = fresh['sets'][variant]['waves'][CASE]
    payload['metrics'].update(fresh['metrics'])
    payload['cases'] = [CASE]+[c for c in payload['cases'] if c!=CASE]
    payload['defaultCase'] = CASE
    args.html.write_text(TEMPLATE.replace('/*DATA*/null',json.dumps(payload,separators=(',',':'))))
    print(f'Saved {args.html}')


if __name__ == '__main__':
    main()
