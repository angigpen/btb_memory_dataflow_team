"""Turn the baseline / Set 1 / Set 2 / Set 3 VCD waveforms into one self-contained HTML waveform viewer.

    python scripts/run_local.py --variants baseline,s1_all,s2_all,s3_all --cases rectangular,sparse_90,long_latency --vcd
    python scripts/run_local.py --extended --variants baseline,s1_all,s2_all --out out_full   # table rows
    # realistic case: Set 2/3 dumped in full, baseline/Set 1 only for their first 160k cycles (else several GB)
    python scripts/run_local.py --variants s2_all,s3_all --cases bench_64x128x128_l23 --vcd --out out_big
    python scripts/run_local.py --variants baseline,s1_all --cases bench_64x128x128_l23 --vcd --vcd-cycles 160000 --out out_big
    python scripts/vcd_to_html.py                    # -> out/waves.html
    python scripts/vcd_to_html.py --cases rectangular,long_latency --out out/waves.html

Signals are the ones picked in tb/set1.gtkw and tb/set2.gtkw (plus control row/col/kk). Values are
sampled once per clock, just before each rising edge (what the flops see).
"""
import argparse, csv, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTRL_ST = {0: 'IDLE', 1: 'ISSUE', 2: 'RESULT', 3: 'WRITE'}
S3_CTRL_ST = {**CTRL_ST, 4: 'SETUP'}
BASE_LD_ST = {0: 'IDLE', 1: 'A_REQ', 2: 'A_RSP', 3: 'B_REQ', 4: 'B_RSP', 5: 'OFFER'}
S1_LD_ST = {0: 'IDLE', 1: 'A_CRD', 2: 'A_REQ', 3: 'A_RSP', 4: 'B_REQ', 5: 'B_RSP'}

# (group, label, vcd path, kind[, enum map[, style]])   kind: bit | dec | hex | bin | enum
COMMON_TOP = [
    ('top', 'busy', 'tb_local.busy', 'bit'),
    ('top', 'done', 'tb_local.done', 'bit'),
]
MEM = [
    ('memory port', 'mem_req_valid', 'tb_local.mem_req_valid', 'bit'),
    ('memory port', 'mem_req_ready', 'tb_local.mem_req_ready', 'bit'),
    ('memory port', 'mem_req_is_b', 'tb_local.mem_req_is_b', 'bit'),
    ('memory port', 'mem_req_addr', 'tb_local.mem_req_addr', 'dec'),
    ('memory port', 'mem_rsp_valid', 'tb_local.mem_rsp_valid', 'bit'),
]
OUT = [('result', 'out_valid', 'tb_local.out_valid', 'bit')]


def ctrl(states=CTRL_ST, reuse_a=False, reuse_b=False, extra=()):
    g = 'gemm_control'
    r = [(g, 'control.state', 'tb_local.dut.control.state', 'enum', states),
         (g, 'row', 'tb_local.dut.control.row', 'dec'),
         (g, 'col', 'tb_local.dut.control.col', 'dec'),
         (g, 'kk', 'tb_local.dut.control.kk', 'dec')]
    r += list(extra)
    if reuse_a: r.append((g, 'req_reuse_a', 'tb_local.dut.control.req_reuse_a', 'bit'))
    if reuse_b: r.append((g, 'req_reuse_b', 'tb_local.dut.control.req_reuse_b', 'bit'))
    return r


SLOTS = 'operand_loader (slots, in flight)'
SLOT_LOADER = [
    (SLOTS, 'alloc', 'tb_local.dut.loader.alloc', 'bin'),
    (SLOTS, 'prep', 'tb_local.dut.loader.prep', 'bin'),
    (SLOTS, 'ip', 'tb_local.dut.loader.ip', 'dec'),
    (SLOTS, 'out_cnt', 'tb_local.dut.loader.out_cnt', 'dec', None, 'analog'),
    (SLOTS, 'op_valid', 'tb_local.dut.loader.op_valid', 'bit'),
    (SLOTS, 'op_ready', 'tb_local.dut.loader.op_ready', 'bit'),
]
# signals only used by the animated block diagrams (not drawn as waveform rows)
ANIM_COMMON = ['tb_local.mem_rsp_ready', 'tb_local.out_ready', 'tb_local.dut.loader.req_valid',
               'tb_local.dut.loader.req_ready', 'tb_local.dut.loader.op_valid', 'tb_local.dut.loader.op_ready',
               'tb_local.dut.control.state', 'tb_local.dut.control.row', 'tb_local.dut.control.col',
               'tb_local.dut.control.kk', 'tb_local.dut.loader.state']
ANIM = {
    'baseline': ANIM_COMMON,
    's1_all': ANIM_COMMON + ['tb_local.dut.loader.full', 'tb_local.dut.loader.wr', 'tb_local.dut.loader.rd',
                             'tb_local.dut.loader.accept', 'tb_local.dut.loader.hit_a', 'tb_local.dut.loader.aw_pend'],
    's2_all': ANIM_COMMON + ['tb_local.dut.loader.alloc', 'tb_local.dut.loader.prep', 'tb_local.dut.loader.rd',
                             'tb_local.dut.loader.out_cnt', 'tb_local.dut.loader.cap_v', 'tb_local.dut.loader.cap_hita',
                             'tb_local.dut.loader.cap_hitb', 'tb_local.dut.loader.skip_b', 'tb_local.dut.loader.aw_pend',
                             'tb_local.dut.loader.bw_pend'],
}
ANIM['s3_all'] = ANIM['s2_all'] + ['tb_local.dut.control.grp_start', 'tb_local.dut.control.gw']

SETS = {
    'baseline': dict(name='Baseline', title='Baseline — original RTL (one request at a time, no reuse)',
                     signals=COMMON_TOP + ctrl() + MEM + [
        ('operand_loader', 'loader.state', 'tb_local.dut.loader.state', 'enum', BASE_LD_ST),
        ('operand_loader', 'op_valid', 'tb_local.dut.loader.op_valid', 'bit'),
        ('operand_loader', 'op_ready', 'tb_local.dut.loader.op_ready', 'bit'),
    ] + OUT),
    's1_all': dict(name='Set 1', title='Set 1 — Phase 1 + 2a (A reuse, zero skip, Ping/Pong)',
                   signals=COMMON_TOP + ctrl(reuse_a=True) + MEM + [
        ('operand_loader (Ping/Pong)', 'loader.state', 'tb_local.dut.loader.state', 'enum', S1_LD_ST),
        ('operand_loader (Ping/Pong)', 'wr', 'tb_local.dut.loader.wr', 'bit'),
        ('operand_loader (Ping/Pong)', 'rd', 'tb_local.dut.loader.rd', 'bit'),
        ('operand_loader (Ping/Pong)', 'full', 'tb_local.dut.loader.full', 'bin'),
        ('operand_loader (Ping/Pong)', 'op_valid', 'tb_local.dut.loader.op_valid', 'bit'),
        ('operand_loader (Ping/Pong)', 'op_ready', 'tb_local.dut.loader.op_ready', 'bit'),
    ] + OUT),
    's2_all': dict(name='Set 2', title='Set 2 — all phases (slots, requests in flight, A+B reuse)',
                   signals=COMMON_TOP + ctrl(reuse_a=True, reuse_b=True) + MEM + SLOT_LOADER + OUT),
    's3_all': dict(name='Set 3', title='Set 3 — Set 2 + grouped B reuse (G column tiles per group, 4 KB B buffer)',
                   signals=COMMON_TOP + ctrl(S3_CTRL_ST, reuse_a=True, reuse_b=True, extra=[
        ('gemm_control', 'grp_start', 'tb_local.dut.control.grp_start', 'dec'),
        ('gemm_control', 'gw (group width)', 'tb_local.dut.control.gw', 'dec'),
    ]) + MEM + SLOT_LOADER + OUT),
}

# ---------------- on-chip storage, computed from the RTL declarations ----------------
import math, re as _re
SRC_DIR = {'baseline': 'baseline', 's1_all': 'set1_phase1_2a', 's2_all': 'set2_all_phases', 's3_all': 'set3_grouped_b'}


def top_params(v):
    txt = (ROOT / SRC_DIR[v] / 'gemm_top.v').read_text()
    hdr = _re.sub(r'//[^\n]*', '', txt[txt.index('module gemm_top'):txt.index(')(')])
    return {k: int(x) for k, x in _re.findall(r'(\w+)\s*=\s*(\d+)', hdr)}


def bram36(depth, width):
    """Xilinx block RAM count (BRAM36 units, BRAM18 = 0.5) for a depth x width simple dual-port RAM."""
    b36 = min(math.ceil(depth / d) * math.ceil(width / w) for d, w in
              [(32768, 1), (16384, 2), (8192, 4), (4096, 9), (2048, 18), (1024, 36), (512, 72)])
    b18 = min(math.ceil(depth / d) * math.ceil(width / w) for d, w in
              [(16384, 1), (8192, 2), (4096, 4), (2048, 9), (1024, 18), (512, 36)])
    return min(b36, b18 / 2)


def memory(v, over=None):
    """Itemised data storage of the operand path: [dict(name, shape, bits, bram, kind, src)]."""
    P = top_params(v); P.update(over or {})
    R, C = P['PE_ROWS'], P['PE_COLS']
    pkt = 8 * R + 8 * C + R + C + 2                    # A, B, row/col masks, first, last
    it = []
    if v == 'baseline':
        it.append(dict(key='pkt', name='Packet data (1 register)', shape=f'1 × {pkt} b', bits=pkt, bram=0, kind='FF',
                       src='op_a, op_b, op_rows, op_cols, op_first, op_last'))
        return it
    if v == 's1_all':
        ns = 2 if P.get('USE_DBUF', 1) else 1
        it.append(dict(key='pkt', name='Packet data (Ping/Pong slots)', shape=f'{ns} × {pkt} b', bits=ns * pkt, bram=0, kind='FF',
                       src='s_a, s_b, s_rows, s_cols, s_first, s_last'))
        if P.get('USE_A_REUSE', 1):
            d = P['ACACHE_DEPTH']
            it.append(dict(key='acache', name='A cache (A word + zero flag)', shape=f'{d} × {8 * R + 1} b', bits=d * (8 * R + 1),
                           bram=bram36(d, 8 * R + 1), kind='BRAM', src='sdp_ram a_cache'))
        return it
    ni = R + C; iw = math.ceil(math.log2(ni)); ns = P['NSLOTS']; mo = P['MAX_OUT']
    sb = max(1, math.ceil(math.log2(ns)))
    trk = 32 + 32 + 16 + 32 + 2 + ni + 3 * (iw + 1)    # s_aaddr, s_baddr, s_kk, s_bba, s_fill*, s_rem, s_*left
    it.append(dict(key='pkt', name='Packet data (slots)', shape=f'{ns} × {pkt} b', bits=ns * pkt, bram=0, kind='FF',
                   src='s_a, s_b, s_rows, s_cols, s_first, s_last'))
    it.append(dict(key='trk', name='Slot request tracking', shape=f'{ns} × {trk} b', bits=ns * trk, bram=0, kind='FF',
                   src='s_aaddr, s_baddr, s_kk, s_bba, s_filla/b, s_rem, s_left, s_aleft/bleft'))
    it.append(dict(key='tag', name='Tag FIFO (requests in flight)', shape=f'{mo} × {sb + iw} b', bits=mo * (sb + iw), bram=0,
                   kind='FF/LUTRAM', src='tag_s, tag_i'))
    if P.get('USE_A_REUSE', 1):
        d = P['ACACHE_DEPTH']
        it.append(dict(key='acache', name='A cache (A word + zero flag)', shape=f'{d} × {8 * R + 1} b', bits=d * (8 * R + 1),
                       bram=bram36(d, 8 * R + 1), kind='BRAM', src='sdp_ram a_cache'))
    if P.get('USE_B_REUSE', 1):
        d = P['BBUF_DEPTH']
        it.append(dict(key='bbuf', name='B buffer', shape=f'{d} × {8 * C} b', bits=d * 8 * C, bram=bram36(d, 8 * C), kind='BRAM',
                       src='sdp_ram b_buf'))
    if v == 's3_all':
        it.append(dict(key='grp', name='Group registers (gemm_control)', shape='4 × 16 b + 32 b + 1 state bit', bits=4 * 16 + 32 + 1,
                       bram=0, kind='FF', src='grp_start, grp_end, gw, g_cnt, g_acc, state[2]'))
    return it


def parse_vcd(path, wanted):
    """Return {path: [[cycle, value], ...]} (change list, value None = x/z) and the cycle count."""
    ids, stack = {}, []        # vcd id -> list of wanted paths
    clk_id = None
    with open(path) as f:
        for line in f:
            t = line.split()
            if not t: continue
            if t[0] == '$scope': stack.append(t[2])
            elif t[0] == '$upscope': stack.pop()
            elif t[0] == '$var':
                p = '.'.join(stack + [t[4]])
                if p == 'tb_local.clk': clk_id = t[3]
                if p in wanted: ids.setdefault(t[3], []).append(p)
            elif t[0] == '$enddefinitions': break
        cur = {i: None for i in ids}; clk = 0
        out = {p: [] for p in wanted}; last = {p: object() for p in wanted}
        cyc = 0; pending = []

        def flush():
            nonlocal clk, cyc
            rising = False
            for i, v in pending:
                if i == clk_id:
                    nv = 1 if v == 1 else 0
                    rising = rising or (nv == 1 and clk == 0); clk = nv
            if rising:                      # sample values as they were *before* this timestep
                for i, ps in ids.items():
                    for p in ps:
                        if cur[i] != last[p]:
                            out[p].append([cyc, cur[i]]); last[p] = cur[i]
                cyc += 1
            for i, v in pending:
                if i in cur: cur[i] = v
            pending.clear()

        for line in f:
            if not line or line[0] == '$': continue
            c = line[0]
            if c == '#': flush(); continue
            if c in '01xzXZ':
                pending.append((line[1:].strip(), None if c in 'xzXZ' else int(c)))
            elif c == 'b':
                bits, i = line[1:].split()
                pending.append((i, None if any(ch in bits for ch in 'xzXZ') else int(bits, 2)))
        flush()
    return out, cyc


def trim(data, ncyc):
    """Cut the reset/config prologue: start a few cycles before busy first rises."""
    busy = data.get('tb_local.busy', [])
    start = next((c for c, v in busy if v == 1), 0)
    start = max(0, start - 3)
    end = ncyc
    done = data.get('tb_local.done', [])
    d1 = [c for c, v in done if v == 1]
    if d1: end = min(ncyc, d1[-1] + 6)
    res = {}
    for p, ch in data.items():
        before = [v for c, v in ch if c <= start]
        seq = [[0, before[-1] if before else None]] + [[c - start, v] for c, v in ch if start < c < end]
        res[p] = seq
    return res, end - start, start, not d1


def encode(seq):
    """[[c, v], ...] -> flat delta list [c0, v0, c1-c0, v1, ...] (about half the JSON size)."""
    out, prev = [], 0
    for c, v in seq:
        out += [c - prev, v]; prev = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='rectangular,sparse_90,long_latency,bench_64x128x128_l23')
    ap.add_argument('--wave-dir', type=Path, action='append', help='where the VCDs are (default: out/ and out_big/)')
    ap.add_argument('--csv', type=Path, action='append',
                    help='result CSVs (later files win); default: results/set3_results.csv + out_full/local_results_mo0.csv')
    ap.add_argument('--out', type=Path, default=ROOT / 'out/waves.html')
    a = ap.parse_args()
    csvs = a.csv or [ROOT / 'results/set3_results.csv', ROOT / 'out_full/local_results_mo0.csv']
    metrics = {}
    for f in csvs:
        if not f.exists():
            print(f'skip (missing) {f}'); continue
        for r in csv.DictReader(f.open()):
            if r['run'] == '0': metrics[(r['variant'], r['case'])] = r
    KEYS = ('m', 'n', 'k', 'lat', 'cycles', 'a_reads', 'b_reads', 'operand_wait', 'max_outstanding')
    payload = dict(sets={}, order=list(SETS), cases=a.cases.split(','), metrics={}, buffers=[])
    for v, spec in SETS.items():
        sigs = [dict(group=s[0], label=s[1], path=s[2], kind=s[3],
                     enum={str(k): x for k, x in (s[4] or {}).items()} if len(s) > 4 and s[4] else None,
                     style=s[5] if len(s) > 5 else None) for s in spec['signals']]
        payload['sets'][v] = dict(name=spec['name'], title=spec['title'], signals=sigs, waves={},
                                  params=top_params(v))
        for c in payload['cases']:
            f = next((d / f'wave_{v}_{c}.vcd' for d in (a.wave_dir or [ROOT / 'out', ROOT / 'out_big'])
                      if (d / f'wave_{v}_{c}.vcd').exists()), None)
            if f is None:
                print(f'skip (missing) wave_{v}_{c}.vcd'); continue
            data, n = parse_vcd(f, {s['path'] for s in sigs} | set(ANIM[v]))
            data, n, off, trunc = trim(data, n)
            payload['sets'][v]['waves'][c] = dict(n=n, offset=off, trunc=trunc,
                                                  data={p: encode(q) for p, q in data.items()})
            print(f'{v:8s} {c:22s} {n:7d} cycles' + ('  (window only)' if trunc else ''))
    for (v, c), r in metrics.items():
        if v in SETS:
            payload['metrics'][f'{v}|{c}'] = {k: r[k] for k in KEYS}
    # B-buffer size sweep at 64x128x128 (results/set3_results.csv)
    BUF = [('baseline', 'Baseline', '—'), ('s1_all', 'Set 1', '—'), ('s2_buf4k', 'Set 2', '4 KB'),
           ('s3_buf1k', 'Set 3', '1 KB (G=2)'), ('s3_buf2k', 'Set 3', '2 KB (G=4)'), ('s3_all', 'Set 3 (default)', '4 KB (G=8)'),
           ('s3_buf8k', 'Set 3', '8 KB (G=16)'), ('s2_all', 'Set 2 (default)', '16 KB (all of B)'),
           ('s3_buf16k', 'Set 3', '16 KB (all of B)')]
    payload['memory'] = {v: memory(v) for v in SETS}
    def total_bits(v):
        base, over = {'s2_buf4k': ('s2_all', dict(BBUF_DEPTH=1024)), 's3_buf1k': ('s3_all', dict(BBUF_DEPTH=256)),
                      's3_buf2k': ('s3_all', dict(BBUF_DEPTH=512)), 's3_buf8k': ('s3_all', dict(BBUF_DEPTH=2048)),
                      's3_buf16k': ('s3_all', dict(BBUF_DEPTH=4096))}.get(v, (v, None))
        it = memory(base, over)
        return sum(x['bits'] for x in it), sum(x['bram'] for x in it)
    for v, name, buf in BUF:
        r1, r23 = metrics.get((v, 'bench_64x128x128')), metrics.get((v, 'bench_64x128x128_l23'))
        if r1 or r23:
            r = r1 or r23
            payload['buffers'].append(dict(v=v, name=name, buf=buf, c1=r1 and r1['cycles'], c23=r23 and r23['cycles'],
                                           reads=int(r['a_reads']) + int(r['b_reads']),
                                           mem_bits=total_bits(v)[0], bram=total_bits(v)[1]))
    html = TEMPLATE.replace('/*DATA*/null', json.dumps(payload, separators=(',', ':')))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(html)
    print(f'-> {a.out}  ({a.out.stat().st_size // 1024} KB)')


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>GEMM loader — baseline / Set 1 / Set 2 / Set 3 simulation</title>
<style>
:root{--bg:#fbfbfa;--panel:#fff;--ink:#1f2328;--mute:#6b7280;--line:#e5e7eb;--grid:#f0f1f3;
 --hi:#2563eb;--hiF:rgba(37,99,235,.14);--bus:#0f766e;--busF:rgba(15,118,110,.08);--x:#dc2626;--cur:#f59e0b;--grp:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#111315;--panel:#181b1f;--ink:#e6e8eb;--mute:#9aa1aa;--line:#2b3036;
 --grid:#1f2328;--hi:#60a5fa;--hiF:rgba(96,165,250,.18);--bus:#2dd4bf;--busF:rgba(45,212,191,.10);--x:#f87171;--cur:#fbbf24;--grp:#22262b}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
header{padding:18px 24px 6px}h1{font-size:19px;margin:0 0 4px}header p{margin:0;color:var(--mute)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 24px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)}
.seg button{border:0;background:none;color:var(--ink);padding:6px 12px;font:inherit;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}.seg button.on{background:var(--hi);color:#fff}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:6px 10px;font:inherit;cursor:pointer}
.hint{color:var(--mute);font-size:12px;margin-left:auto}
.stats{display:flex;flex-wrap:wrap;gap:10px;padding:4px 24px 12px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:120px}
.tile .k{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}.tile .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .d{font-size:12px;color:var(--mute)}
.view{margin:0 24px 16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.vtitle{padding:8px 14px;font-weight:600;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline}
.vtitle span{font-weight:400;color:var(--mute);font-size:12px}
canvas{display:block;width:100%;cursor:crosshair}
table{border-collapse:collapse;margin:0 24px 30px;background:var(--panel);border:1px solid var(--line);border-radius:10px;font-variant-numeric:tabular-nums}
th,td{padding:6px 14px;border-bottom:1px solid var(--line);text-align:right}th{font-size:12px;color:var(--mute);font-weight:600}
td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){text-align:left}
h2{font-size:15px;margin:18px 24px 8px}.note{margin:0 24px 14px;color:var(--mute);font-size:13px;max-width:980px}.best{font-weight:700}
.dg{margin:0 24px 14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.dg .vtitle{padding:8px 14px}.dg svg{display:block;width:100%;height:auto}
.dg p{margin:0;padding:0 14px 10px;color:var(--mute);font-size:12.5px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 24px 10px;font-size:12px;color:var(--mute)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:-1px;margin-right:5px}
svg text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
svg .mono{font-family:ui-monospace,Menlo,monospace}.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;margin-left:6px;border:1px solid var(--line)}.tag.new{background:var(--hiF);color:var(--hi);border-color:var(--hi)}.tag.up{background:var(--busF);color:var(--bus);border-color:var(--bus)}.tag.down{color:var(--mute)}tr.sub td{color:var(--mute);font-size:12px}tr.tot td{font-weight:600}tr.grpTop td{border-top:2px solid var(--line)}
</style></head><body>
<header><h1>GEMM operand loader — simulation waveforms</h1>
<p>Baseline, Set 1 (Phase 1 + 2a), Set 2 (all phases) and Set 3 (Set 2 + grouped B reuse), from the local Icarus runs (<code>tb_local.sv</code>). One column = one clock cycle, sampled just before the rising edge.</p></header>
<div class="bar">
 <div class="seg" id="setSeg"></div>
 <div class="seg" id="caseSeg"></div>
 <button class="btn" id="fit">Fit</button>
 <label style="color:var(--mute);font-size:13px"><input type="checkbox" id="sync" checked> same time scale for all views</label>
 <span class="hint">wheel / pinch = zoom · drag = pan · hover = values · click = pin cursor</span>
</div>
<table id="cmp" style="margin-bottom:8px"></table><p class="note" id="note"></p>
<h2>Additional on-chip memory (default parameters, from the RTL declarations)</h2>
<table id="memSum"></table>
<table id="memItems"></table>
<p class="note">Counted: data storage on the operand path — packet registers/slots, request tracking, tag FIFO, A cache, B buffer (bit widths
taken from the <code>reg</code>/<code>sdp_ram</code> declarations and each set's <code>gemm_top.v</code> defaults; PE 4×4).
Not counted: FSM/address/counter registers that baseline also has. BRAM36 = Xilinx block RAM count by aspect ratio
(1K×36, 4K×9, …; BRAM18 = 0.5); Vivado may map small RAMs to LUTRAM instead. Yosys estimate in the README: Set 1 ≈ 1 BRAM36 / +100 FF, Set 2 ≈ 5 BRAM36 / +300 FF.</p>
<h2>Block diagrams — animated from the simulation</h2>
<div class="bar" style="padding-top:0">
 <button class="btn" id="play">▶ Play</button>
 <button class="btn" id="stepB">◀ 1</button><button class="btn" id="stepF">1 ▶</button>
 <label style="color:var(--mute);font-size:13px">speed
  <select id="speed" class="btn" style="padding:4px 6px"><option value="2">2</option><option value="5">5</option><option value="10" selected>10</option><option value="25">25</option><option value="60">60</option><option value="200">200</option><option value="1000">1,000</option><option value="5000">5,000</option><option value="20000">20,000</option></select> cycles/s</label>
 <input type="range" id="scrub" min="0" max="100" value="0" step="1" style="flex:1;min-width:200px">
 <span id="cycLbl" style="font-variant-numeric:tabular-nums;min-width:90px">cycle 0</span>
 <label style="color:var(--mute);font-size:13px"><input type="checkbox" id="follow" checked> move waveform cursor</label>
</div>
<div class="legend" id="legend"></div>
<div id="diagrams"></div>
<h2>Waveforms</h2>
<div id="views"></div>
<h2>Set 3 B-buffer sweep — 64×128×128 (from <code>results/set3_results.csv</code>)</h2>
<p class="note">Where Set 3 matters: B for 64×128×128 is 16 KB. With the same 4 KB buffer, Set 2 can only reuse the column tiles that fit;
Set 3 reorders the loop (group → row tile → col tile in group → kk) so the buffer is refilled per group and every byte of B is read once.</p>
<table id="buf"></table>
<h2>All cases (local Icarus runs, pipelined memory)</h2>
<table id="tbl"></table>
<script>
const P=/*DATA*/null;
for(const k in P.sets)for(const c in P.sets[k].waves){const w=P.sets[k].waves[c];
 for(const p in w.data){const f=w.data[p],o=[];let t=0;for(let i=0;i<f.length;i+=2){t+=f[i];o.push([t,f[i+1]])}w.data[p]=o}}
const caseLabel=c=>{const m=P.metrics['s3_all|'+c]||P.metrics['baseline|'+c];return c.startsWith('bench_')&&m?`${m.m}×${m.n}×${m.k} · lat ${m.lat} (realistic)`:c};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SETNAMES=Object.fromEntries(P.order.map(k=>[k,P.sets[k].name]));
let mode='all', kase=P.cases.find(c=>P.order.some(k=>P.sets[k].waves[c]));
const views={};      // set -> view state
const ROWH=24, LBLW=170, TOP=26;

function seg(el,items,get,set){el.innerHTML='';for(const [k,l] of items){const b=document.createElement('button');b.textContent=l;
 b.className=get()===k?'on':'';b.onclick=()=>{set(k);seg(el,items,get,set);render()};el.appendChild(b)}}
seg(document.getElementById('setSeg'),[['all','All'],['s1s2s3','Sets 1–3'],...P.order.map(k=>[k,SETNAMES[k]])],()=>mode,v=>{mode=v;for(const k in views)views[k].fit=true});
seg(document.getElementById('caseSeg'),P.cases.map(c=>[c,caseLabel(c)]),()=>kase,v=>{kase=v;for(const k in views)views[k].fit=true});

function fmt(s,v){if(v===null||v===undefined)return'x';if(s.kind==='enum')return(s.enum&&s.enum[v])||String(v);
 if(s.kind==='hex')return'0x'+v.toString(16);if(s.kind==='bin'){const w=s.label==='full'?2:s.label==='ip'?2:4;return v.toString(2).padStart(w,'0')}return String(v)}
function valAt(ch,c){let lo=0,hi=ch.length-1,r=0;while(lo<=hi){const m=(lo+hi)>>1;if(ch[m][0]<=c){r=m;lo=m+1}else hi=m-1}return ch[r][1]}

function rows(set){const out=[];let g=null;for(const s of P.sets[set].signals){if(s.group!==g){out.push({group:s.group});g=s.group}out.push({sig:s})}return out}

function render(){
 const box=document.getElementById('views');box.innerHTML='';
 const sets=mode==='all'?P.order:mode==='s1s2s3'?P.order.filter(k=>k!=='baseline'):[mode];
 for(const set of sets){
  const w=P.sets[set].waves[kase];const d=document.createElement('div');d.className='view';
  const m=P.metrics[set+'|'+kase];
  d.innerHTML=`<div class="vtitle">${P.sets[set].title}<span>${w?(w.trunc?`first ${w.n.toLocaleString()} cycles only — run finishes at cycle ${m?(+m.cycles).toLocaleString():'?'}`:w.n.toLocaleString()+' cycles shown'):'no waveform for this case'}${m?` · M×N×K = ${m.m}×${m.n}×${m.k}, mem latency ${m.lat}`:''}</span></div>`;
  box.appendChild(d);if(!w)continue;
  const cv=document.createElement('canvas');d.appendChild(cv);
  const v=views[set]||(views[set]={x0:0,ppc:8,fit:true,hover:null,pin:null});
  v.set=set;v.cv=cv;v.rows=rows(set);v.w=w;
  attach(v);
 }
 stats();requestAnimationFrame(()=>{for(const s of sets)if(views[s].cv&&views[s].w){if(views[s].fit)fit(views[s]);draw(views[s])}});
}
function fit(v){const W=v.cv.clientWidth-LBLW-10;const n=maxN();v.ppc=Math.max(0.05,W/n);v.x0=0;v.fit=false}
function maxN(){const sync=document.getElementById('sync').checked;const ns=Object.values(views).filter(x=>x.w&&x.cv&&x.cv.isConnected).map(x=>x.w.n);
 return sync?Math.max(...ns):Math.max(...ns)}
function others(v){return document.getElementById('sync').checked?Object.values(views).filter(x=>x!==v&&x.cv&&x.cv.isConnected&&x.w):[]}

function attach(v){
 const cv=v.cv;let drag=null;
 cv.onwheel=e=>{e.preventDefault();const r=cv.getBoundingClientRect();const mx=e.clientX-r.left-LBLW;
  const c=v.x0+mx/v.ppc;const f=Math.exp(-(e.deltaY)*0.0015);v.ppc=Math.min(80,Math.max(0.02,v.ppc*f));v.x0=Math.max(0,c-mx/v.ppc);
  for(const o of others(v)){o.ppc=v.ppc;o.x0=v.x0;draw(o)}draw(v)};
 cv.onmousedown=e=>{drag={x:e.clientX,x0:v.x0,moved:false}};
 window.addEventListener('mouseup',e=>{if(drag&&!drag.moved&&e.target===cv){const c=cellAt(v,e);v.pin=v.pin===c?null:c;for(const o of others(v)){o.pin=v.pin;draw(o)}draw(v)}drag=null});
 cv.onmousemove=e=>{if(drag&&e.buttons){const dx=e.clientX-drag.x;if(Math.abs(dx)>3)drag.moved=true;v.x0=Math.max(0,drag.x0-dx/v.ppc);for(const o of others(v)){o.x0=v.x0;draw(o)}}
  v.hover=cellAt(v,e);for(const o of others(v)){o.hover=v.hover;draw(o)}draw(v)};
 cv.onmouseleave=()=>{v.hover=null;for(const o of others(v)){o.hover=null;draw(o)}draw(v)};
}
function cellAt(v,e){const r=v.cv.getBoundingClientRect();const x=e.clientX-r.left-LBLW;if(x<0)return null;return Math.floor(v.x0+x/v.ppc)}

function draw(v){
 const cv=v.cv,dpr=devicePixelRatio||1,W=cv.clientWidth,H=TOP+v.rows.length*ROWH+6;
 cv.style.height=H+'px';cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 const C={ink:css('--ink'),mute:css('--mute'),line:css('--line'),grid:css('--grid'),hi:css('--hi'),hiF:css('--hiF'),bus:css('--bus'),busF:css('--busF'),x:css('--x'),cur:css('--cur'),grp:css('--grp'),bg:css('--panel')};
 g.fillStyle=C.bg;g.fillRect(0,0,W,H);
 const n=v.w.n,pw=W-LBLW,c0=Math.max(0,Math.floor(v.x0)),c1=Math.min(n,Math.ceil(v.x0+pw/v.ppc));
 const X=c=>LBLW+(c-v.x0)*v.ppc;
 // time axis
 const step=niceStep(70/v.ppc);g.font='11px ui-monospace,Menlo,monospace';g.textBaseline='middle';
 for(let c=Math.ceil(c0/step)*step;c<=c1;c+=step){const x=X(c);g.fillStyle=C.grid;g.fillRect(x,TOP,1,H-TOP);g.fillStyle=C.mute;g.fillText(c,x+3,TOP/2)}
 g.fillStyle=C.line;g.fillRect(0,TOP-1,W,1);
 g.save();g.beginPath();g.rect(LBLW,0,pw,H);g.clip();
 if(v.ppc>=6){g.fillStyle=C.grid;for(let c=c0;c<=c1;c++)g.fillRect(X(c),TOP,1,H-TOP)}
 g.restore();
 v.rows.forEach((r,i)=>{
  const y=TOP+i*ROWH;
  if(r.group){g.fillStyle=C.grp;g.fillRect(0,y,W,ROWH);g.fillStyle=C.mute;g.font='600 11px -apple-system,sans-serif';g.fillText(r.group.toUpperCase(),10,y+ROWH/2);return}
  const s=r.sig,ch=v.w.data[s.path]||[[0,null]];
  g.fillStyle=C.ink;g.font='12px ui-monospace,Menlo,monospace';g.fillText(s.label,16,y+ROWH/2);
  g.save();g.beginPath();g.rect(LBLW,y,pw,ROWH);g.clip();
  const yt=y+5,yb=y+ROWH-5;
  // iterate segments
  let k=0;{let lo=0,hi=ch.length-1;while(lo<=hi){const m=(lo+hi)>>1;if(ch[m][0]<=c0){k=m;lo=m+1}else hi=m-1}}
  const maxv=s.style==='analog'?Math.max(1,...ch.map(e=>e[1]||0)):1;
  for(;k<ch.length&&ch[k][0]<=c1;k++){
   const a=ch[k][0],b=k+1<ch.length?ch[k+1][0]:n,val=ch[k][1];const xa=Math.max(LBLW-2,X(a)),xb=Math.min(W+2,X(b));
   if(xb-xa<1&&k+1<ch.length){                 // many changes inside one pixel: draw one dense block
    const px=Math.floor(xa)+1,cEnd=v.x0+(px+1-LBLW)/v.ppc;let lo=k,hi=ch.length-1;
    while(lo<hi){const mm=(lo+hi+1)>>1;if(ch[mm][0]<=cEnd)lo=mm;else hi=mm-1}
    if(lo-1>k){g.fillStyle=s.kind==='bit'?C.hi:C.bus;g.globalAlpha=.55;g.fillRect(xa,yt,px+1-xa,yb-yt);g.globalAlpha=1;k=lo-1;continue}
   }
   if(val===null){g.fillStyle=C.x;g.globalAlpha=.25;g.fillRect(xa,yt,xb-xa,yb-yt);g.globalAlpha=1;continue}
   if(s.kind==='bit'){
    const yy=val?yt:yb;if(val){g.fillStyle=C.hiF;g.fillRect(xa,yt,xb-xa,yb-yt)}
    g.strokeStyle=C.hi;g.lineWidth=1.5;g.beginPath();g.moveTo(xa,yy);g.lineTo(xb,yy);
    if(k+1<ch.length&&b<=c1){const nv=ch[k+1][1];g.lineTo(xb,nv?yt:yb)}g.stroke();
   }else if(s.style==='analog'){
    const yy=yb-(yb-yt)*val/maxv;g.fillStyle=C.busF;g.fillRect(xa,yy,xb-xa,yb-yy);g.strokeStyle=C.bus;g.lineWidth=1.5;
    g.beginPath();g.moveTo(xa,yy);g.lineTo(xb,yy);if(k+1<ch.length&&b<=c1){const nv=ch[k+1][1]||0;g.lineTo(xb,yb-(yb-yt)*nv/maxv)}g.stroke();
    if(xb-xa>18){g.fillStyle=C.ink;g.font='10px ui-monospace,Menlo,monospace';g.fillText(val,xa+3,Math.max(yt+5,yy-6))}
   }else{
    const e=Math.min(3,(xb-xa)/2);g.fillStyle=C.busF;g.strokeStyle=C.bus;g.lineWidth=1.2;g.beginPath();
    g.moveTo(xa,(yt+yb)/2);g.lineTo(xa+e,yt);g.lineTo(xb-e,yt);g.lineTo(xb,(yt+yb)/2);g.lineTo(xb-e,yb);g.lineTo(xa+e,yb);g.closePath();g.fill();g.stroke();
    const t=fmt(s,val);g.font='11px ui-monospace,Menlo,monospace';const tw=g.measureText(t).width;
    if(xb-xa>tw+8){g.fillStyle=C.ink;g.fillText(t,Math.max(xa,LBLW)+5,(yt+yb)/2)}
   }
  }
  g.restore();
  g.fillStyle=C.line;g.fillRect(0,y+ROWH-.5,W,.5);
 });
 g.fillStyle=C.line;g.fillRect(LBLW-1,0,1,H);
 // cursor(s)
 for(const [c,col,dash] of [[v.pin,C.cur,[]],[v.hover,C.mute,[3,3]]]){
  if(c===null||c===undefined||c>=n)continue;const x=X(c+.5);if(x<LBLW)continue;
  g.strokeStyle=col;g.setLineDash(dash);g.lineWidth=1;g.beginPath();g.moveTo(x,TOP);g.lineTo(x,H);g.stroke();g.setLineDash([]);
 }
 const c=v.hover??v.pin;
 if(c!==null&&c!==undefined&&c<n){
  // value readout in label column
  g.fillStyle=C.bg;g.fillRect(0,0,LBLW-1,TOP-1);g.fillStyle=C.cur;g.font='600 11px ui-monospace,Menlo,monospace';g.fillText('cycle '+c,10,TOP/2);
  v.rows.forEach((r,i)=>{if(!r.sig)return;const y=TOP+i*ROWH;const t=fmt(r.sig,valAt(v.w.data[r.sig.path],c));
   g.font='11px ui-monospace,Menlo,monospace';const tw=g.measureText(t).width;g.fillStyle=C.bg;g.fillRect(LBLW-tw-12,y+4,tw+8,ROWH-8);g.fillStyle=C.cur;g.fillText(t,LBLW-tw-8,y+ROWH/2)});
 }
}
function niceStep(x){const p=Math.pow(10,Math.floor(Math.log10(Math.max(x,1))));for(const m of[1,2,5,10])if(m*p>=x)return m*p;return 10*p}

function stats(){
 const b=P.metrics['baseline|'+kase];const num=x=>(+x).toLocaleString();
 let h='<tr><th>variant</th><th>case</th><th>cycles</th><th>speedup</th><th>A reads</th><th>B reads</th><th>total reads</th><th>vs baseline</th><th>operand wait</th><th>max in flight</th></tr>';
 const ms=P.order.map(v=>[v,P.metrics[v+'|'+kase]]).filter(x=>x[1]);const best=Math.min(...ms.map(x=>+x[1].cycles));
 for(const [v,m] of ms){const rd=(+m.a_reads)+(+m.b_reads),brd=b?(+b.a_reads)+(+b.b_reads):0;
  h+=`<tr><td>${SETNAMES[v]}</td><td>${v==='baseline'?`${kase} (${m.m}×${m.n}×${m.k}, lat ${m.lat})`:''}</td><td class="${+m.cycles===best?'best':''}">${num(m.cycles)}</td>
   <td>${b?(b.cycles/m.cycles).toFixed(2)+'×':''}</td><td>${num(m.a_reads)}</td><td>${num(m.b_reads)}</td><td>${num(rd)}</td>
   <td>${b&&v!=='baseline'?((rd/brd-1)*100).toFixed(1)+'%':''}</td><td>${num(m.operand_wait)}</td><td>${m.max_outstanding}</td></tr>`}
 document.getElementById('cmp').innerHTML=h;
 const m=P.metrics['s3_all|'+kase],m2=P.metrics['s2_all|'+kase];
 const w3=P.sets.s3_all.waves[kase];let setup=null;
 if(w3){const st=w3.data['tb_local.dut.control.state'];const i=st.findIndex(e=>e[1]===4);if(i>=0&&i+1<st.length)setup=st[i+1][0]-st[i][0]}
 const diff=m&&m2?(+m.cycles)-(+m2.cycles):null;
 document.getElementById('note').textContent=m&&+m.m>4?
  `What to watch: ${+m.m/4} row tiles × ${+m.n/4} col tiles × K=${m.k}. Set 2 fills its 16 KB B buffer during row tile 0 and reads it for every later row tile. Set 3 fills its 4 KB buffer during row tile 0 of each group of G=${Math.max(1,Math.min(+m.n/4,Math.floor((P.sets.s3_all.params.BBUF_DEPTH||1024)/+m.k)))} col tiles (the group range in gemm_control), then reuses it for the other row tiles. Baseline and Set 1 are far slower, so only their first cycles are recorded.`:
  m&&+m.m<=4?
  `Note: M=${m.m} fits in one row tile, so Set 3 has nothing to group (B is never reused across row tiles) and runs the same loop as Set 2. `+
  (diff>0?`The ${diff} extra cycle${diff>1?'s':''} (${m.cycles} vs ${m2.cycles}) come from Set 3's start-up SETUP state that picks G${setup?` (${setup} cycles in the waveform)`:''}. `:
   diff===0?`Cycle counts are equal: Set 3's SETUP state${setup?` (${setup} cycles)`:''} is hidden behind the memory latency. `:'')+
  'See the 64×128×128 sweep below for Set 3\'s gain.':'';
}
function kb(b){b=+b;return b>=8192?(b/8192).toFixed(2)+' KB':b>=8?(b/8).toFixed(b%8?1:0)+' B':b+' b'}
function memTables(){
 const tot=v=>P.memory[v].reduce((a,x)=>a+x.bits,0),br=v=>P.memory[v].reduce((a,x)=>a+x.bram,0);
 const ram=v=>P.memory[v].filter(x=>x.kind==='BRAM').reduce((a,x)=>a+x.bits,0);
 const sgn=d=>(d>0?'+':d<0?'−':'±')+kb(Math.abs(d));
 let h='<tr><th>variant</th><th>total storage</th><th>in block RAM</th><th>BRAM36</th><th>in flip-flops</th><th>added vs baseline</th><th>added vs previous set</th></tr>';
 P.order.forEach((v,i)=>{const t=tot(v),pv=i?P.order[i-1]:null;
  h+=`<tr class="tot"><td>${SETNAMES[v]}</td><td>${kb(t)} <span style="color:var(--mute);font-weight:400">(${t.toLocaleString()} b)</span></td><td>${kb(ram(v))}</td><td>${br(v)}</td><td>${kb(t-ram(v))}</td>
   <td>${i?sgn(t-tot('baseline')):'—'}</td><td>${pv?sgn(t-tot(pv))+` <span style="color:var(--mute);font-weight:400">vs ${SETNAMES[pv]}</span>`:'—'}</td></tr>`});
 document.getElementById('memSum').innerHTML=h;
 let g='<tr><th>variant</th><th>structure</th><th>size</th><th>bits</th><th>bytes</th><th>BRAM36</th><th>type</th><th>RTL</th></tr>';
 P.order.forEach((v,i)=>{const prev=i?Object.fromEntries(P.memory[P.order[i-1]].map(x=>[x.key,x])):{};
  P.memory[v].forEach((x,j)=>{const p=prev[x.key];let tag='';
   if(i){if(!p)tag='<span class="tag new">new</span>';else if(x.bits>p.bits)tag=`<span class="tag up">grown ${sgn(x.bits-p.bits)}</span>`;
    else if(x.bits<p.bits)tag=`<span class="tag down">smaller ${sgn(x.bits-p.bits)}</span>`}
   g+=`<tr class="${j?'':'grpTop'}"><td>${j?'':SETNAMES[v]}</td><td style="text-align:left">${x.name}${tag}</td><td>${x.shape}</td><td>${x.bits.toLocaleString()}</td><td>${kb(x.bits)}</td><td>${x.bram||'—'}</td><td>${x.kind}</td><td style="text-align:left"><code>${x.src}</code></td></tr>`});
  const t=P.memory[v].reduce((a,x)=>a+x.bits,0);
  g+=`<tr class="sub"><td></td><td style="text-align:left">total</td><td></td><td>${t.toLocaleString()}</td><td>${kb(t)}</td><td>${br(v)}</td><td></td><td></td></tr>`});
 document.getElementById('memItems').innerHTML=g;
}
function table(){
 const cs=[...new Set(Object.keys(P.metrics).map(k=>k.split('|')[1]))];
 let h='<tr><th>case</th><th>variant</th><th>M×N×K</th><th>lat</th><th>cycles</th><th>speedup</th><th>A reads</th><th>B reads</th><th>max in flight</th></tr>';
 for(const c of cs)for(const v of P.order){const m=P.metrics[v+'|'+c];if(!m)continue;const b=P.metrics['baseline|'+c];
  h+=`<tr class="${v===P.order.find(x=>P.metrics[x+'|'+c])?'grpTop':''}"><td>${v===P.order.find(x=>P.metrics[x+'|'+c])?c:''}</td><td>${SETNAMES[v]}</td><td>${m.m}×${m.n}×${m.k}</td><td>${m.lat}</td><td>${(+m.cycles).toLocaleString()}</td>
   <td>${b?(b.cycles/m.cycles).toFixed(2)+'×':''}</td><td>${(+m.a_reads).toLocaleString()}</td><td>${(+m.b_reads).toLocaleString()}</td><td>${m.max_outstanding}</td></tr>`}
 document.getElementById('tbl').innerHTML=h;
 const B=P.buffers,b0=B.find(x=>x.v==='baseline');memTables();const num=x=>x==null?'—':(+x).toLocaleString();
 let g='<tr><th>variant</th><th>B buffer</th><th>on-chip storage</th><th>BRAM36</th><th>cycles (lat 1)</th><th>speedup</th><th>cycles (lat 23)</th><th>speedup</th><th>total reads</th><th>read change</th></tr>';
 for(const r of B){g+=`<tr class="${r.v==='s3_all'?'best':''}"><td>${r.name}</td><td>${r.buf}</td><td>${kb(r.mem_bits)}</td><td>${r.bram}</td><td>${num(r.c1)}</td><td>${b0&&r.c1?(b0.c1/r.c1).toFixed(1)+'×':''}</td>
  <td>${num(r.c23)}</td><td>${b0&&r.c23?(b0.c23/r.c23).toFixed(1)+'×':''}</td><td>${num(r.reads)}</td><td>${b0&&r.v!=='baseline'?((r.reads/b0.reads-1)*100).toFixed(1)+'%':'—'}</td></tr>`}
 document.getElementById('buf').innerHTML=g;
}
document.getElementById('fit').onclick=()=>{for(const k in views)views[k].fit=true;render()};
document.getElementById('sync').onchange=()=>{for(const k in views)views[k].fit=true;render()};
window.addEventListener('resize',()=>{for(const k in views)if(views[k].cv&&views[k].cv.isConnected)draw(views[k])});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',render);
// ======================= animated block diagrams =======================
const NS='http://www.w3.org/2000/svg';
const DESC={
 baseline:'One request at a time: fetch 4 A bytes, then 4 B bytes, one memory round-trip each, then hand the packet to the MAC. No reuse, no overlap.',
 s1_all:'A cache keeps each kk\'s A word from col tile 0 and replays it for later col tiles (blue flash = cache read, orange = fill). All-zero A skips the B reads. Ping/Pong slots let the next packet load while the MAC holds the other — but still one memory request at a time.',
 s2_all:'4 slots and a 32-entry tag FIFO keep up to 32 requests in flight (dots in the memory pipe). The A cache and the 16 KB B buffer (filled during row tile 0) remove most reads.',
 s3_all:'Same loader as Set 2, but gemm_control walks group → row tile → col tile in group → kk, so a 4 KB B buffer holds G column tiles and is reused by every row tile of the group. SETUP picks G at start.'};
const COL={A:'#2563eb',B:'#ea580c',req:'#7c3aed',op:'#16a34a',out:'#0891b2',skip:'#9ca3af'};
document.getElementById('legend').innerHTML=[['A request / A data',COL.A],['B request / B data',COL.B],['request from gemm_control',COL.req],['packet to MAC',COL.op],['result written',COL.out],['zero-skipped B',COL.skip]]
 .map(([l,c])=>`<span><i style="background:${c}"></i>${l}</span>`).join('');
let T=0,playing=false,last=null;const dgs={};
function sa(e,k,v){if(typeof v==='string'&&v.startsWith('var(')&&(k==='fill'||k==='stroke')){e.removeAttribute(k);e.style[k]=v}else{if(k==='fill'||k==='stroke')e.style[k]='';e.setAttribute(k,v)}}
function mk(tag,attrs,parent,text){const e=document.createElementNS(NS,tag);for(const k in attrs)sa(e,k,attrs[k]);if(text!=null)e.textContent=text;if(parent)parent.appendChild(e);return e}
function dense(w,path){const ch=w.data[path];const a=new Int32Array(w.n);if(!ch)return a;let k=0;
 for(let c=0;c<w.n;c++){while(k+1<ch.length&&ch[k+1][0]<=c)k++;a[c]=ch[k][1]||0}return a}
function prepAnim(set){
 const w=P.sets[set].waves[kase];if(!w)return null;const L='tb_local.dut.loader.',Cp='tb_local.dut.control.';const d=p=>dense(w,p);
 const n=w.n,ev={};const and=(a,b)=>a.map((x,i)=>x&&b[i]?1:0);
 ev.reqIn=and(d(L+'req_valid'),d(L+'req_ready'));ev.memReq=and(d('tb_local.mem_req_valid'),d('tb_local.mem_req_ready'));
 ev.isB=d('tb_local.mem_req_is_b');ev.memRsp=and(d('tb_local.mem_rsp_valid'),d('tb_local.mem_rsp_ready'));
 ev.op=and(d(L+'op_valid'),d(L+'op_ready'));ev.out=and(d('tb_local.out_valid'),d('tb_local.out_ready'));
 ev.cst=d(Cp+'state');ev.row=d(Cp+'row');ev.col=d(Cp+'col');ev.kk=d(Cp+'kk');ev.lst=d(L+'state');ev.done=d('tb_local.done');
 ev.aRd=new Int32Array(n);ev.bRd=new Int32Array(n);ev.aWr=new Int32Array(n);ev.bWr=new Int32Array(n);ev.skip=new Int32Array(n);
 if(set==='s1_all'){ev.full=d(L+'full');ev.wr=d(L+'wr');ev.rd=d(L+'rd');ev.aRd=and(d(L+'accept'),d(L+'hit_a'));ev.aWr=d(L+'aw_pend');
  for(let c=0;c+1<n;c++)if(ev.lst[c]===1&&ev.lst[c+1]!==4)ev.skip[c]=1}         // A_CRD not followed by B_REQ = zero skip
 if(set==='s2_all'||set==='s3_all'){ev.alloc=d(L+'alloc');ev.prep=d(L+'prep');ev.rd=d(L+'rd');ev.oc=d(L+'out_cnt');
  const cv=d(L+'cap_v');ev.aRd=and(cv,d(L+'cap_hita'));ev.bRd=and(cv,d(L+'cap_hitb'));ev.skip=and(cv,d(L+'skip_b'));
  ev.aWr=d(L+'aw_pend');ev.bWr=d(L+'bw_pend')}
 if(set==='s3_all'){ev.gs=d(Cp+'grp_start');ev.gw=d(Cp+'gw')}
 // pair requests with (in-order) responses
 const q=[],reqs=[];for(let c=0;c<n;c++){if(ev.memRsp[c]&&q.length){q.shift().r=c}if(ev.memReq[c]){const o={q:c,b:ev.isB[c],r:null};q.push(o);reqs.push(o)}}
 ev.reqs=reqs;ev.byRsp=new Map(reqs.filter(o=>o.r!=null).map(o=>[o.r,o]));ev.trunc=!!w.trunc;const cum=a=>{const o=new Int32Array(n);let s=0;for(let c=0;c<n;c++){s+=a[c];o[c]=s}return o};
 ev.cA=cum(ev.memReq.map((x,i)=>x&&!ev.isB[i]?1:0));ev.cB=cum(ev.memReq.map((x,i)=>x&&ev.isB[i]?1:0));
 ev.cOp=cum(ev.op);ev.cOut=cum(ev.out);ev.cARd=cum(ev.aRd);ev.cBRd=cum(ev.bRd);ev.cSkip=cum(ev.skip);
 ev.end=(()=>{for(let c=0;c<n;c++)if(ev.done[c])return c;return n-1})();ev.m=P.metrics[set+'|'+kase];ev.n=n;return ev}
function box(g,x,y,w,h,title,sub){const r=mk('rect',{x,y,width:w,height:h,rx:8,fill:'var(--panel)',stroke:'var(--line)','stroke-width':1.5},g);
 mk('text',{x:x+10,y:y+18,'font-size':13,'font-weight':600,fill:'var(--ink)'},g,title);if(sub)mk('text',{x:x+w-10,y:y+18,'font-size':11,fill:'var(--mute)','text-anchor':'end'},g,sub);return r}
function arrow(g,d,lbl,lx,ly){const p=mk('path',{d,fill:'none',stroke:'var(--line)','stroke-width':3,'marker-end':'url(#ah)'},g);
 if(lbl)mk('text',{x:lx,y:ly,'font-size':10.5,fill:'var(--mute)','text-anchor':'middle'},g,lbl);const t=mk('circle',{r:6,opacity:0},g);return{p,t,len:null}}
function buildDiagram(set){
 const ev=prepAnim(set);if(!ev)return null;const pr=P.sets[set].params,m=P.metrics[set+'|'+kase]||{},lat=+m.lat||1;
 const wrap=document.createElement('div');wrap.className='dg';
 wrap.innerHTML=`<div class="vtitle">${P.sets[set].title}<span id="st_${set}"></span></div>`;
 const svg=mk('svg',{viewBox:'0 0 1000 330'});wrap.appendChild(svg);
 const defs=mk('defs',{},svg);const mkr=mk('marker',{id:'ah',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:5,markerHeight:5,orient:'auto'},defs);mk('path',{d:'M0,0L10,5L0,10z',fill:'var(--mute)'},mkr);
 const D={ev,set,el:{}};const E=D.el;
 // gemm_control
 box(svg,15,95,165,150,'gemm_control');
 E.cst=mk('text',{x:25,y:140,'font-size':15,'font-weight':600,fill:'var(--ink)',class:'mono'},svg,'IDLE');
 E.pos=mk('text',{x:25,y:165,'font-size':12,fill:'var(--mute)',class:'mono'},svg,'');
 E.grp=mk('text',{x:25,y:188,'font-size':12,fill:'var(--mute)',class:'mono'},svg,'');
 E.reqs=mk('text',{x:25,y:232,'font-size':11,fill:'var(--mute)'},svg,'');
 // loader
 const lt=set==='baseline'?'one FSM':set==='s1_all'?'FSM + Ping/Pong':'intake → prep → issue → route → supply';
 box(svg,220,15,400,300,'operand_loader',lt);
 const ns=set==='baseline'?1:set==='s1_all'?2:(pr.NSLOTS||4);E.slots=[];
 mk('text',{x:235,y:50,'font-size':11,fill:'var(--mute)'},svg,ns===1?'packet register':ns===2?'packet slots (Ping / Pong)':`${ns} packet slots`);
 for(let i=0;i<ns;i++){const x=235+i*92;const r=mk('rect',{x,y:58,width:82,height:52,rx:6,fill:'var(--grp)',stroke:'var(--line)','stroke-width':1.5},svg);
  const t=mk('text',{x:x+41,y:80,'font-size':11,'text-anchor':'middle',fill:'var(--ink)'},svg,ns>1?'slot '+i:'packet');
  const t2=mk('text',{x:x+41,y:98,'font-size':10.5,'text-anchor':'middle',fill:'var(--mute)'},svg,'empty');
  const mark=mk('text',{x:x+76,y:70,'font-size':10,'text-anchor':'end',fill:'var(--mute)'},svg,'');E.slots.push({r,t2,mark})}
 if(set!=='baseline'){E.ac=mk('rect',{x:235,y:128,width:180,height:62,rx:6,fill:'var(--grp)',stroke:'var(--line)','stroke-width':1.5},svg);
  mk('text',{x:245,y:147,'font-size':12,'font-weight':600,fill:'var(--ink)'},svg,'A cache');
  mk('text',{x:405,y:147,'font-size':10.5,'text-anchor':'end',fill:'var(--mute)'},svg,`${pr.ACACHE_DEPTH}×33 b`);
  E.acT=mk('text',{x:245,y:176,'font-size':11,fill:'var(--mute)',class:'mono'},svg,'')}
 if(set==='s2_all'||set==='s3_all'){E.bb=mk('rect',{x:425,y:128,width:180,height:62,rx:6,fill:'var(--grp)',stroke:'var(--line)','stroke-width':1.5},svg);
  mk('text',{x:435,y:147,'font-size':12,'font-weight':600,fill:'var(--ink)'},svg,'B buffer');
  mk('text',{x:595,y:147,'font-size':10.5,'text-anchor':'end',fill:'var(--mute)'},svg,`${pr.BBUF_DEPTH*4/1024} KB`);
  E.bbT=mk('text',{x:435,y:176,'font-size':11,fill:'var(--mute)',class:'mono'},svg,'')}
 if(set==='s2_all'||set==='s3_all'){const mo=pr.MAX_OUT||32;mk('text',{x:235,y:215,'font-size':11,fill:'var(--mute)'},svg,`tag FIFO — requests in flight (max ${mo})`);
  E.tags=[];const cw=Math.min(11,360/mo);for(let i=0;i<mo;i++)E.tags.push(mk('rect',{x:235+i*cw,y:222,width:cw-2,height:18,rx:2,fill:'var(--grp)'},svg));
  E.ocT=mk('text',{x:605,y:236,'font-size':11,'text-anchor':'end',fill:'var(--ink)',class:'mono'},svg,'')}
 else{mk('text',{x:235,y:215,'font-size':11,fill:'var(--mute)'},svg,'loader FSM (one memory request at a time)');
  E.lst=mk('text',{x:235,y:240,'font-size':14,'font-weight':600,fill:'var(--ink)',class:'mono'},svg,'')}
 E.skipT=mk('text',{x:235,y:290,'font-size':11,fill:'var(--mute)'},svg,'');
 // memory
 box(svg,660,15,325,135,'external memory',`latency ${lat}`);
 mk('line',{x1:680,y1:85,x2:965,y2:85,stroke:'var(--grid)','stroke-width':40,'stroke-linecap':'round'},svg);
 E.memG=mk('g',{},svg);E.memT=mk('text',{x:675,y:138,'font-size':11,fill:'var(--mute)',class:'mono'},svg,'');
 // MAC + writer
 box(svg,660,185,150,130,'mac_array','4×4');E.mac=mk('rect',{x:675,y:212,width:120,height:60,rx:4,fill:'var(--grp)'},svg);
 for(let i=0;i<4;i++)for(let j=0;j<4;j++)mk('rect',{x:680+j*29,y:216+i*14,width:25,height:11,rx:2,fill:'var(--panel)',opacity:.8},svg);
 E.macT=mk('text',{x:675,y:300,'font-size':11,fill:'var(--mute)',class:'mono'},svg,'');
 box(svg,835,185,150,130,'result_writer');E.wr=mk('rect',{x:850,y:212,width:120,height:60,rx:4,fill:'var(--grp)'},svg);
 E.wrT=mk('text',{x:850,y:300,'font-size':11,fill:'var(--mute)',class:'mono'},svg,'');
 E.doneT=mk('text',{x:910,y:250,'font-size':13,'font-weight':600,'text-anchor':'middle',fill:COL.op},svg,'');
 // arrows (+ moving tokens)
 E.aReq=arrow(svg,'M180,170 L218,170','req',199,160);
 E.aMem=arrow(svg,'M620,55 L658,55','mem req',639,45);
 E.aRsp=arrow(svg,'M660,115 L622,115','rsp',641,130);
 E.aOp=arrow(svg,'M620,250 L658,250','packet',639,240);
 E.aOut=arrow(svg,'M810,250 L833,250','',0,0);
 D.wrap=wrap;D.lat=lat;return D}
function tok(a,on,p,col){if(!on){a.t.setAttribute('opacity',0);sa(a.p,'stroke','var(--line)');return}
 if(a.len==null)a.len=a.p.getTotalLength();const pt=a.p.getPointAtLength(a.len*Math.min(1,p));a.t.setAttribute('cx',pt.x);a.t.setAttribute('cy',pt.y);
 sa(a.t,'fill',col);a.t.setAttribute('opacity',1);sa(a.p,'stroke',col)}
function anyIn(arr,c0,c1){for(let c=Math.max(0,c0);c<=c1&&c<arr.length;c++)if(arr[c])return c;return -1}
function update(D,t,prevT){
 const ev=D.ev,E=D.el,n=ev.n;const c=Math.min(Math.floor(t),n-1),p=t-Math.floor(t);
 const c0=Math.min(Math.floor(prevT)+1,c);           // cycles crossed since the last frame (fast playback)
 const st=P.sets[D.set].signals.find(s=>s.label==='control.state');const nm=v=>(st&&st.enum&&st.enum[v])||v;
 E.cst.textContent=nm(ev.cst[c]);E.pos.textContent=`row ${ev.row[c]}  col ${ev.col[c]}  kk ${ev.kk[c]}`;
 if(ev.gs)E.grp.textContent=ev.gw[c]?`group cols ${ev.gs[c]}–${ev.gs[c]+ev.gw[c]-1}`:'';
 E.reqs.textContent=`packets sent ${ev.cOp[c]}`;
 // tokens
 const f=(arr)=>{const k=anyIn(arr,c0,c);return k};
 let k=f(ev.reqIn);tok(E.aReq,k>=0,k===c?p:1,COL.req);
 k=f(ev.memReq);tok(E.aMem,k>=0,k===c?p:1,k>=0&&ev.isB[k]?COL.B:COL.A);
 k=f(ev.memRsp);let rb=null;if(k>=0){const r=ev.byRsp.get(k);rb=r?r.b:0}tok(E.aRsp,k>=0,k===c?p:1,rb?COL.B:COL.A);
 k=f(ev.op);tok(E.aOp,k>=0,k===c?p:1,COL.op);k=f(ev.out);tok(E.aOut,k>=0,k===c?p:1,COL.out);
 // memory pipe
 const inf=ev.reqs.filter(o=>o.q<=t&&(o.r===null||o.r>t));while(E.memG.firstChild)E.memG.firstChild.remove();
 inf.forEach((o,i)=>{const span=Math.max(1,(o.r??(o.q+D.lat))-o.q);const fr=Math.min(1,(t-o.q)/span);
  mk('circle',{cx:690+fr*265,cy:72+(i%3)*13,r:5,fill:o.b?COL.B:COL.A},E.memG)});
 E.memT.textContent=`in flight ${inf.length}   reads A ${ev.cA[c]}  B ${ev.cB[c]}`;
 // slots
 const S=E.slots;S.forEach((s,i)=>{let state='empty',fill='var(--grp)',mark='';
  if(D.set==='baseline'){const l=ev.lst[c];if(l===5){state='ready';fill=COL.op}else if(l>=1){state=l<=2?'loading A':'loading B';fill=l<=2?COL.A:COL.B}}
  else if(D.set==='s1_all'){const full=(ev.full[c]>>i)&1,l=ev.lst[c];
   if(full){state='ready';fill=COL.op}else if(ev.wr[c]===i&&l!==0){state=l===1?'A from cache':l<=3?'loading A':'loading B';fill=l<=3?COL.A:COL.B}
   mark=(ev.wr[c]===i?'wr ':'')+(ev.rd[c]===i?'rd':'')}
  else{const al=(ev.alloc[c]>>i)&1,pp=(ev.prep[c]>>i)&1;
   if(al){if(ev.rd[c]===i&&ev.op[c]){state='to MAC';fill=COL.op}else if(!pp){state='prep';fill='var(--busF)'}else{state='loading';fill='var(--hiF)'}}
   mark=ev.rd[c]===i?'rd':''}
  sa(s.r,'fill',fill);s.r.setAttribute('fill-opacity',fill.startsWith('#')?.35:1);s.t2.textContent=state;s.mark.textContent=mark});
 // caches
 const fl=(el,rd,wr)=>{if(!el)return;sa(el,'stroke',rd?COL.A:wr?COL.B:'var(--line)');el.setAttribute('stroke-width',rd||wr?4:1.5)};
 const aR=anyIn(ev.aRd,c0,c)>=0,aW=anyIn(ev.aWr,c0,c)>=0,bR=anyIn(ev.bRd,c0,c)>=0,bW=anyIn(ev.bWr,c0,c)>=0;
 fl(E.ac,aR,aW);fl(E.bb,bR,bW);
 if(E.acT)E.acT.textContent=`reads ${ev.cARd[c]}  ${aR?'◀ read':aW?'▶ fill':''}`;
 if(E.bbT)E.bbT.textContent=`reads ${ev.cBRd[c]}  ${bR?'◀ read':bW?'▶ fill':''}`;
 if(E.tags){const oc=ev.oc[c];E.tags.forEach((r,i)=>sa(r,'fill',i<oc?COL.req:'var(--grp)'));E.ocT.textContent=oc}
 if(E.lst){const ls=P.sets[D.set].signals.find(s=>s.label==='loader.state');E.lst.textContent=(ls&&ls.enum[ev.lst[c]])||ev.lst[c]}
 E.skipT.textContent=ev.cSkip[c]?`zero-skipped packets (B reads saved) ${ev.cSkip[c]}${anyIn(ev.skip,c0,c)>=0?'  ●':''}`:'';
 sa(E.mac,'fill',anyIn(ev.op,c0,c)>=0?COL.op:'var(--grp)');E.mac.setAttribute('fill-opacity',.45);
 sa(E.wr,'fill',anyIn(ev.out,c0,c)>=0?COL.out:'var(--grp)');E.wr.setAttribute('fill-opacity',.45);
 E.macT.textContent=`packets ${ev.cOp[c]}`;E.wrT.textContent=`outputs ${ev.cOut[c]}`;
 const fin=c>=ev.end&&!ev.trunc,M=ev.m;E.doneT.textContent=fin?`done ✓ cycle ${ev.end}`:'';
 const pct=M?(100*ev.cOut[c]/(M.m*M.n)).toFixed(1)+'% of outputs':'';
 document.getElementById('st_'+D.set).textContent=fin?`finished at cycle ${ev.end.toLocaleString()}`:
  ev.trunc&&c>=ev.end?`still running — ${pct} done at cycle ${c.toLocaleString()} (finishes at ${M?(+M.cycles).toLocaleString():'?'}; waveform window ends here)`:
  `cycle ${c.toLocaleString()} · ${pct}`;
 if(ev.trunc&&c>=ev.end)E.doneT.textContent=`${pct.split(' ')[0]} done`;
}
function buildDiagrams(){
 const box=document.getElementById('diagrams');box.innerHTML='';for(const k in dgs)delete dgs[k];
 const sets=mode==='all'?P.order:mode==='s1s2s3'?P.order.filter(k=>k!=='baseline'):[mode];
 for(const set of sets){const D=buildDiagram(set);if(!D)continue;dgs[set]=D;box.appendChild(D.wrap);
  const p=document.createElement('p');p.textContent=DESC[set];D.wrap.appendChild(p)}
 const mx=Math.max(1,...Object.values(dgs).map(D=>D.ev.end+1));const sc=document.getElementById('scrub');sc.max=mx;
 T=Math.min(T,mx);sc.value=Math.floor(T);paint(T,T-1)}
function paint(t,prev){for(const k in dgs)update(dgs[k],t,prev);document.getElementById('cycLbl').textContent='cycle '+Math.floor(t);
 document.getElementById('scrub').value=Math.floor(t);
 if(document.getElementById('follow').checked&&Math.floor(t)!==Math.floor(prev)){for(const k in views){const v=views[k];if(v.cv&&v.cv.isConnected&&v.w){v.pin=Math.floor(t);draw(v)}}}}
function frame(ts){if(!playing)return;const dt=last==null?0:(ts-last)/1000;last=ts;const prev=T;
 T+=dt*(+document.getElementById('speed').value);const mx=+document.getElementById('scrub').max;
 if(T>=mx){T=mx;playing=false;document.getElementById('play').textContent='▶ Play'}paint(T,prev);if(playing)requestAnimationFrame(frame)}
document.getElementById('play').onclick=()=>{playing=!playing;const b=document.getElementById('play');
 if(playing){if(T>=+document.getElementById('scrub').max)T=0;last=null;b.textContent='❚❚ Pause';requestAnimationFrame(frame)}else b.textContent='▶ Play'};
document.getElementById('scrub').oninput=e=>{const prev=T;T=+e.target.value;paint(T,T-.001)};
document.getElementById('stepF').onclick=()=>{playing=false;document.getElementById('play').textContent='▶ Play';const prev=T;T=Math.floor(T)+1;paint(T+.5,T-.5);T+=.5};
document.getElementById('stepB').onclick=()=>{playing=false;document.getElementById('play').textContent='▶ Play';T=Math.max(0,Math.floor(T)-1);paint(T+.5,T-.5);T+=.5};
const _render=render;render=function(){_render();T=0;buildDiagrams()};
table();render();
</script></body></html>
'''

if __name__ == '__main__':
    main()
