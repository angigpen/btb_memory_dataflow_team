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
}

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
    cases = a.cases.split(',') if a.cases else (names if a.suite else ['rectangular'])
    # testbench with defines (run.py prepends PR/PC the same way)
    tb = b/'tb_configured.sv'
    def dp(kv):
        k, v = kv.split('=')
        inst, k = ('control', k.split('.', 1)[1]) if k.startswith('control.') else ('loader', k)
        return f'    defparam tb_gemm.dut.{inst}.{k} = {v};\n'
    defp = ''.join(dp(kv) for kv in a.param)
    src = (ROOT/'tb/tb_gemm.sv').read_text(encoding='utf-8')
    if defp: src = src.replace('    rv_monitor #(.WIDTH(82+R+C))', defp + '    rv_monitor #(.WIDTH(82+R+C))', 1)
    tb.write_text('`define PR 4\n`define PC 4\n' + src, encoding='utf-8')
    exe = b/'sim.vvp'
    # files not in --rtl-dir (e.g. set1/ holds only operand_loader.v) come from baseline/
    rtl = [a.rtl_dir.resolve()/s if (a.rtl_dir/s).exists() else ROOT/'baseline'/s for s in SOURCES]
    print('RTL:', ', '.join(str(p.relative_to(ROOT)) if ROOT in p.parents else str(p) for p in rtl), flush=True)
    cmd = ['iverilog', '-g2012', '-o', str(exe), str(ROOT/'tb/rv_monitor.sv'), str(tb)] + [str(p) for p in rtl] \
          + ([str(ROOT/'tb/wave_dump.v')] if a.vcd else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode: sys.exit('compile failed:\n' + r.stderr)
    rows = []; fails = 0
    for c in cases:
        w = b/'run'/c; w.mkdir(parents=True, exist_ok=True)
        for f in ('a.hex','b.hex','c.hex','config.hex'): shutil.copy2(vec/c/f, w/f)
        args = ['vvp', '-n', str(exe), '+HOST_TEST'] + (['+RESET_TEST'] if c == 'restart' else []) \
               + ([] if a.baseline_checks else ['+OPTIMIZED']) \
               + ([f'+VCD={b}/wave_{c}.vcd'] + ([f'+VCDCYCLES={a.vcd_cycles}'] if a.vcd_cycles else []) if a.vcd else [])
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
