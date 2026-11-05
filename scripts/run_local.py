"""Local regression with Icarus Verilog: compile each RTL variant, run every fixture, collect metrics.

Needs: iverilog, and the official generate.py copied into this scripts/ folder.
    python run_local.py --extended --variants baseline,s1_all,s2_all
    python run_local.py --memmaxout 1      # memory that accepts only ONE outstanding request
    python summarize.py 'out/local_results_mo0.csv'
"""
import argparse, csv, subprocess, sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from generate import suite, make_case

ROOT = Path(__file__).resolve().parents[1]
SRC = ['gemm_control.v','operand_loader.v','sparsity_frontend.v','mac_array.v','result_writer.v','gemm_top.v']

VARIANTS = {
    'baseline':       ('baseline', {}),
    # ---- Set 1 ----
    's1_off':         ('set1_phase1_2a', dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_DBUF=0)),
    's1_areuse':      ('set1_phase1_2a', dict(USE_A_REUSE=1, USE_ZERO_SKIP=0, USE_DBUF=0)),
    's1_areuse_zs':   ('set1_phase1_2a', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=0)),
    's1_dbuf':        ('set1_phase1_2a', dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_DBUF=1)),
    's1_all':         ('set1_phase1_2a', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=1)),
    's1_all_acache16':('set1_phase1_2a', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=1, ACACHE_DEPTH=16)),
    # ---- Set 2 ----
    's2_off':         ('set2_all_phases', dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_B_REUSE=0, NSLOTS=2, MAX_OUT=1)),
    's2_p1_2a':       ('set2_all_phases', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=0, NSLOTS=2, MAX_OUT=1)),
    's2_p1_2':        ('set2_all_phases', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=0, NSLOTS=4, MAX_OUT=32)),
    's2_2b_only':     ('set2_all_phases', dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_B_REUSE=0, NSLOTS=4, MAX_OUT=32)),
    's2_all':         ('set2_all_phases', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=1, NSLOTS=4, MAX_OUT=32)),
    's2_all_small':   ('set2_all_phases', dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=1, NSLOTS=2, MAX_OUT=4,
                                    ACACHE_DEPTH=16, BBUF_DEPTH=64)),
}

BENCH = [
    ('bench64_lat1',       dict(m=64, n=64, k=64, seed=11)),
    ('bench64_lat23',      dict(m=64, n=64, k=64, seed=11, latency=23)),
    ('bench64_sp90_lat1',  dict(m=64, n=64, k=64, seed=12, sparsity=.9)),
    ('bench64_sp90_lat23', dict(m=64, n=64, k=64, seed=12, sparsity=.9, latency=23)),
    ('bench_lat23_stall',  dict(m=9, n=13, k=21, seed=13, latency=23, stalls=2, output_stalls=2)),
]

def build(name, out):
    d, params = VARIANTS[name]
    b = out/'build'/name; b.mkdir(parents=True, exist_ok=True)
    (b/'tb_params.vh').write_text(''.join(f'    defparam dut.{k} = {v};\n' for k,v in params.items()))
    exe = b/'sim.vvp'
    cmd = ['iverilog','-g2012','-I',str(b),'-o',str(exe),str(ROOT/'tb/tb_local.sv')]+[str(ROOT/d/s) for s in SRC]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode: raise SystemExit(f'compile {name} failed:\n{r.stderr}')
    return exe

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--variants', default=','.join(VARIANTS))
    p.add_argument('--cases', default='')
    p.add_argument('--extended', action='store_true')
    p.add_argument('--memmaxout', type=int, default=0, help='0 = pipelined memory, 1 = one outstanding request')
    p.add_argument('--out', type=Path, default=ROOT/'out')
    a = p.parse_args()
    vec = a.out/'vectors'; names = suite(vec, a.extended)
    for n_, kw in BENCH:                      # extra benchmark fixtures (not in the official suite)
        make_case(vec/n_, **kw); names.append(n_)
    cases = a.cases.split(',') if a.cases else names
    rows = []; fails = 0
    for v in a.variants.split(','):
        exe = build(v, a.out)
        for c in cases:
            r = subprocess.run(['vvp','-n',str(exe),f'+CASEDIR={vec/c}',f'+MEMMAXOUT={a.memmaxout}'],
                               capture_output=True, text=True, timeout=3600)
            ok = 'LOCAL PASS' in r.stdout
            for line in r.stdout.splitlines():
                if line.startswith('RESULT'):
                    d = dict(kv.split('=') for kv in line.split()[1:])
                    rows.append(dict(variant=v, case=c, **{k:int(x) for k,x in d.items()}, pass_=ok))
            if not ok:
                fails += 1
                print(f'FAIL {v}/{c}\n' + '\n'.join(r.stdout.splitlines()[-15:]))
            else:
                print(f'pass {v:16s} {c}', flush=True)
    f = a.out/f'local_results_mo{a.memmaxout}.csv'
    with f.open('w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(f'{len(rows)} runs, {fails} failures -> {f}')
    sys.exit(1 if fails else 0)

if __name__ == '__main__':
    main()
