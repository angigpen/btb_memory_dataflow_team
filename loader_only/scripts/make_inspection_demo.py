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
GROUP_CASE = 'inspect_set3_groups'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--html', type=Path, default=ROOT/'build/waves.html')
    ap.add_argument('--iverilog', default='iverilog')
    ap.add_argument('--vvp', default='vvp')
    ap.add_argument('--ivl-dir', type=Path, help='Support directory for a standalone Icarus package')
    ap.add_argument('--set3-groups', action='store_true', help='Add only the Set 3 group-switch example')
    args = ap.parse_args()
    source = args.html.read_text()
    payload, _ = json.JSONDecoder().raw_decode(source[source.index('const P=')+8:])
    build = ROOT/'build'
    case = GROUP_CASE if args.set3_groups else CASE
    fixture = build/('set3/vectors' if args.set3_groups else 'set2/vectors')/case
    overrides = {'BBUF_DEPTH': 4} if args.set3_groups else {}
    make_case(fixture, m=8, n=16 if args.set3_groups else 8, k=2 if args.set3_groups else 6,
              pattern='inspection', latency=3, seed=42)

    def read_hex(kind, bits):
        values = [int(s,16) for s in (fixture/(kind+'.hex')).read_text().split()]
        return [v-(1<<bits) if v >= (1<<(bits-1)) else v for v in values]

    matrices = {x: read_hex(x,32 if x=='c' else 8) for x in ('a','b','c')}
    fresh = dict(sets={}, metrics={})
    variants = [('s3_all','set3')] if args.set3_groups else [('baseline','baseline'),('s1_all','set1'),('s2_all','set2'),('s3_all','set3')]
    for variant, folder in variants:
        out = build/('inspection_groups' if args.set3_groups else 'inspection')/folder
        out.mkdir(parents=True, exist_ok=True)
        for name in ('a.hex','b.hex','c.hex','config.hex'):
            shutil.copy2(fixture/name, out/name)
        tb = out/'tb_configured.sv'
        tb_src = (ROOT/'tb/tb_gemm.sv').read_text()
        if overrides:
            depth = overrides['BBUF_DEPTH']
            anchor = '    rv_monitor #(.WIDTH(82+R+C))'
            assert anchor in tb_src
            tb_src = tb_src.replace(anchor, f'    defparam tb_gemm.dut.loader.BBUF_DEPTH = {depth};\n'
                                          f'    defparam tb_gemm.dut.control.BBUF_DEPTH = {depth};\n'+anchor, 1)
        tb.write_text('`define PR 4\n`define PC 4\n'+tb_src)
        exe = out/'sim.vvp'
        sources = [ROOT/folder/name if (ROOT/folder/name).exists() else ROOT/'baseline'/name for name in SOURCES]
        compiler = [args.iverilog] + (['-B',str(args.ivl_dir)] if args.ivl_dir else [])
        cmd = compiler+['-g2012','-o',str(exe),str(ROOT/'tb/rv_monitor.sv'),str(tb)]+[str(p) for p in sources]+[str(ROOT/'tb/wave_dump.v')]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        vcd = out/('wave_'+case+'.vcd')
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
        if overrides:
            wave['params'] = overrides
        row = next(csv.DictReader((out/'metrics.csv').open()))
        metrics = {k:row[k] for k in ('m','n','k','cycles','a_reads','b_reads','operand_wait')}
        metrics['lat']='3'
        fresh['sets'][variant] = dict(params=spec['params'], waves={case:wave})
        fresh['metrics'][variant+'|'+case] = metrics
        print(f'PASS {variant}: {row["cycles"]} cycles, A reads {row["a_reads"]}, B reads {row["b_reads"]}', flush=True)
    add_buffer_traces(fresh, build)
    add_packet_traces(fresh, build)
    for spec in fresh['sets'].values():
        wave = spec['waves'][case]
        if args.set3_groups:
            continue  # Group controls are derived from the actual group/BRAM trace in the viewer.
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
    for variant, _ in variants:
        payload['sets'][variant]['waves'][case] = fresh['sets'][variant]['waves'][case]
    payload['metrics'].update(fresh['metrics'])
    payload['cases'] = [c for c in payload['cases'] if c!=case]
    if args.set3_groups:
        payload['cases'].append(case)
    else:
        payload['cases'].insert(0, case)
        payload['defaultCase'] = case
    args.html.write_text(TEMPLATE.replace('/*DATA*/null',json.dumps(payload,separators=(',',':'))))
    print(f'Saved {args.html}')


if __name__ == '__main__':
    main()
