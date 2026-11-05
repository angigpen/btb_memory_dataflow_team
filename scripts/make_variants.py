"""Make one RTL folder per variant for the official flow (run.py --rtl-dir ...).

run.py compiles gemm_top with its default parameters, so each variant is a copy of
set1/ or set2/ whose gemm_top.v defaults are rewritten.

    python make_variants.py --src ../set1_phase1_2a --out ../variants   (also set2_all_phases, set3_grouped_b)
    python run.py --suite --extended --optimized --rtl-dir ../variants/s1_all --build ../build/s1_all
    python compare.py ../build/baseline/results.json ../build/s1_all/results.json --out s1_all.csv
"""
import argparse, re, shutil
from pathlib import Path

SET1 = {
    's1_areuse':    dict(USE_A_REUSE=1, USE_ZERO_SKIP=0, USE_DBUF=0),
    's1_areuse_zs': dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=0),
    's1_dbuf':      dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_DBUF=1),
    's1_all':       dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=1),
}
SET2 = {
    's2_p1_2a':     dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=0, NSLOTS=2, MAX_OUT=1),
    's2_2b_only':   dict(USE_A_REUSE=0, USE_ZERO_SKIP=0, USE_B_REUSE=0, NSLOTS=4, MAX_OUT=32),
    's2_p1_2':      dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=0, NSLOTS=4, MAX_OUT=32),
    's2_all':       dict(USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=1, NSLOTS=4, MAX_OUT=32),
}
SET3 = {   # grouped B reuse; BBUF_DEPTH entries x 4 byte
    's3_buf1k':     dict(BBUF_DEPTH=256),
    's3_buf2k':     dict(BBUF_DEPTH=512),
    's3_all':       dict(BBUF_DEPTH=1024),
    's3_buf8k':     dict(BBUF_DEPTH=2048),
    's3_buf16k':    dict(BBUF_DEPTH=4096),
}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--src', type=Path, required=True, help='set1 or set2 folder')
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    top = (a.src/'gemm_top.v').read_text()
    table = SET3 if 'SET 3' in top else SET2 if 'USE_B_REUSE' in top else SET1
    for name, params in table.items():
        d = a.out/name; d.mkdir(parents=True, exist_ok=True)
        for f in a.src.glob('*.v'): shutil.copy2(f, d/f.name)
        t = top
        for k, v in params.items():
            t, n = re.subn(rf'(parameter {k}=)\d+', rf'\g<1>{v}', t)
            if n != 1: raise SystemExit(f'{k} not found in gemm_top.v')
        (d/'gemm_top.v').write_text(t)
        print(d, params)

if __name__ == '__main__':
    main()
