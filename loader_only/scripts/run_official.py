"""Run the OFFICIAL testbench (tb/tb_gemm.sv) with Icarus Verilog instead of Vivado xsim.

Same fixtures (generate.py), same plusargs and the same pass/fail rules as run.py:
  +HOST_TEST always, +RESET_TEST for 'restart', +OPTIMIZED unless --baseline-checks.
The memory model inside tb_gemm.sv allows ONE outstanding request (INTERFACES.md).

    python run_official.py --rtl-dir ../set1 --suite --extended --build ../build/set1
    python run_official.py --rtl-dir ../baseline --suite --extended --baseline-checks --build ../build/baseline
    python run_official.py --rtl-dir ../set1 --cases rectangular --param USE_DBUF=0 --build ../build/s1_nodbuf
Writes <build>/summary.csv (same columns as run.py's metrics.csv + case name).
"""
import argparse, csv, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from generate import suite, make_case

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ['gemm_control.v','operand_loader.v','sparsity_frontend.v','mac_array.v','result_writer.v','gemm_top.v']
EXTRA = {   # extra benchmark fixtures (not part of the official suite)
    'bench64':          dict(m=64, n=64, k=64, seed=11),
    'bench64_lat23':    dict(m=64, n=64, k=64, seed=11, latency=23),
    'bench64_sp90':     dict(m=64, n=64, k=64, seed=12, sparsity=.9),
    'bench_64x128x128': dict(m=64, n=128, k=128, seed=14),
    'bench_64x128x128_l23': dict(m=64, n=128, k=128, seed=14, latency=23),
    'bench_64x64x256':  dict(m=64, n=64, k=256, seed=15),        # B = 16 KB: just fits Set 2's buffer
    'bench_64x128x512': dict(m=64, n=128, k=512, seed=16),       # B = 64 KB: fits no B buffer
}
# small cases with values -3..3 so the viewer can print every number and partial sum
DEMO = {
    'demo_8x8x4':   dict(m=8,  n=8,  k=4, seed=21),   # 2 row tiles x 2 col tiles
    'demo_20x12x6': dict(m=20, n=12, k=6, seed=22),   # 5 row tiles (Set 4: block of 4 + block of 1) x 3 col tiles
}

def make_small(path, *, m, n, k, seed, lo=-3, hi=3, latency=1):
    """Same files as generate.make_case, but small values (readable in the viewer)."""
    import random
    r = random.Random(seed)
    a = [r.randint(lo, hi) for _ in range(m*k)]; bm = [r.randint(lo, hi) for _ in range(k*n)]
    c = [sum(a[i*k+t]*bm[t*n+j] for t in range(k)) for i in range(m) for j in range(n)]
    path.mkdir(parents=True, exist_ok=True)
    hx = lambda name, vals, bits: (path/name).write_text(''.join(f'{x & ((1<<bits)-1):0{bits//4}x}\n' for x in vals))
    zeros = sum(a[i*k+t] == 0 or bm[t*n+j] == 0 for i in range(m) for j in range(n) for t in range(k))
    hx('a.hex', a, 8); hx('b.hex', bm, 8); hx('c.hex', c, 32)
    hx('config.hex', [m, n, k, latency, 0, 0, 1, seed, zeros], 32)

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--rtl-dir', type=Path, required=True)
    p.add_argument('--build', type=Path, required=True)
    p.add_argument('--suite', action='store_true')
    p.add_argument('--extended', action='store_true')
    p.add_argument('--cases', default='')
    p.add_argument('--baseline-checks', action='store_true', help='do NOT pass +OPTIMIZED (baseline formulas enforced)')
    p.add_argument('--param', action='append', default=[],
                   help='override a parameter: USE_DBUF=0 (operand_loader) or control.BBUF_DEPTH=512 (gemm_control)')
    p.add_argument('--vcd', action='store_true', help='write <build>/wave_<case>.vcd (tb/wave_dump.v)')
    p.add_argument('--vcd-cycles', type=int, default=0, help='only the first N cycles of each waveform')
    a = p.parse_args()
    b = a.build.resolve(); b.mkdir(parents=True, exist_ok=True)
    vec = b/'vectors'; names = suite(vec, a.extended)
    for n, kw in EXTRA.items(): make_case(vec/n, **kw); names.append(n)
    for n, kw in DEMO.items(): make_small(vec/n, **kw); names.append(n)
    cases = a.cases.split(',') if a.cases else (names if a.suite else ['rectangular'])
    # testbench with defines (run.py prepends PR/PC the same way)
    tb = b/'tb_configured.sv'
    def dp(kv):
        k, v = kv.split('=')
        # USE_DBUF=0 -> loader, control.X=1 -> gemm_control, top.X=1 -> gemm_top (Set 4 settings)
        inst, k = k.split('.', 1) if '.' in k else ('loader', k)
        path = 'tb_gemm.dut' if inst == 'top' else f'tb_gemm.dut.{inst}'
        return f'    defparam {path}.{k} = {v};\n'
    defp = ''.join(dp(kv) for kv in a.param)
    src = (ROOT/'tb/tb_gemm.sv').read_text(encoding='utf-8')
    if defp: src = src.replace('    rv_monitor #(.WIDTH(82+R+C))', defp + '    rv_monitor #(.WIDTH(82+R+C))', 1)
    tb.write_text('`define PR 4\n`define PC 4\n' + src, encoding='utf-8')
    exe = b/'sim.vvp'
    # files not in --rtl-dir (e.g. set1/ holds only operand_loader.v) come from baseline/
    rtl = [a.rtl_dir.resolve()/s if (a.rtl_dir/s).exists() else ROOT/'baseline'/s for s in SOURCES]
    print('RTL:', ', '.join(str(p.relative_to(ROOT)) if ROOT in p.parents else str(p) for p in rtl), flush=True)
    cmd = ['iverilog', '-g2012', '-o', str(exe), str(ROOT/'tb/rv_monitor.sv'), str(tb)] + [str(p) for p in rtl] \
          + ([str(ROOT/'tb/wave_dump.v'), str(ROOT/'tb/pkt_trace.v')] if a.vcd else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode: sys.exit('compile failed:\n' + r.stderr)
    rows = []; fails = 0
    for c in cases:
        w = b/'run'/c; w.mkdir(parents=True, exist_ok=True)
        for f in ('a.hex','b.hex','c.hex','config.hex'): shutil.copy2(vec/c/f, w/f)
        args = ['vvp', '-n', str(exe), '+HOST_TEST'] + (['+RESET_TEST'] if c == 'restart' else []) \
               + ([] if a.baseline_checks else ['+OPTIMIZED']) \
               + ([f'+VCD={b}/wave_{c}.vcd', f'+PKT={b}/pkt_{c}.txt'] + ([f'+VCDCYCLES={a.vcd_cycles}'] if a.vcd_cycles else []) if a.vcd else [])
        r = subprocess.run(args, cwd=w, capture_output=True, text=True, timeout=7200)
        ok = 'GEMM REGRESSION PASS' in r.stdout
        if ok:
            lat = int((vec/c/'config.hex').read_text().split()[3], 16)
            for rec in csv.DictReader(open(w/'metrics.csv')):
                rows.append({'case': c, **{k: int(v) for k, v in rec.items()}, 'latency': lat})
            print(f'PASS {c}', flush=True)
        else:
            fails += 1
            print(f'FAIL {c}\n' + '\n'.join((r.stdout + r.stderr).splitlines()[-8:]), flush=True)
    if rows:
        with (b/'summary.csv').open('w', newline='') as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0])); wr.writeheader(); wr.writerows(rows)
    print(f'{len(cases) - fails}/{len(cases)} cases pass -> {b/"summary.csv"}')
    sys.exit(1 if fails else 0)

if __name__ == '__main__':
    main()
