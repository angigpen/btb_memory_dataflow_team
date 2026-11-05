"""Turn the baseline / Set 1 / Set 2 / Set 3 VCD waveforms into one self-contained HTML waveform viewer.

    python scripts/run_local.py --variants baseline,s1_all,s2_all,s3_all --cases rectangular,sparse_90,long_latency --vcd
    python scripts/run_local.py --extended --variants baseline,s1_all,s2_all --out out_full   # table rows
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
    return res, end - start, start


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='rectangular,sparse_90,long_latency')
    ap.add_argument('--wave-dir', type=Path, default=ROOT / 'out')
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
        payload['sets'][v] = dict(name=spec['name'], title=spec['title'], signals=sigs, waves={})
        for c in payload['cases']:
            f = a.wave_dir / f'wave_{v}_{c}.vcd'
            if not f.exists():
                print(f'skip (missing) {f}'); continue
            data, n = parse_vcd(f, {s['path'] for s in sigs})
            data, n, off = trim(data, n)
            payload['sets'][v]['waves'][c] = dict(n=n, offset=off, data=data)
            print(f'{v:8s} {c:16s} {n:6d} cycles')
    for (v, c), r in metrics.items():
        if v in SETS:
            payload['metrics'][f'{v}|{c}'] = {k: r[k] for k in KEYS}
    # B-buffer size sweep at 64x128x128 (results/set3_results.csv)
    BUF = [('baseline', 'Baseline', '—'), ('s1_all', 'Set 1', '—'), ('s2_buf4k', 'Set 2', '4 KB'),
           ('s3_buf1k', 'Set 3', '1 KB (G=2)'), ('s3_buf2k', 'Set 3', '2 KB (G=4)'), ('s3_all', 'Set 3 (default)', '4 KB (G=8)'),
           ('s3_buf8k', 'Set 3', '8 KB (G=16)'), ('s2_all', 'Set 2 (default)', '16 KB (all of B)'),
           ('s3_buf16k', 'Set 3', '16 KB (all of B)')]
    for v, name, buf in BUF:
        r1, r23 = metrics.get((v, 'bench_64x128x128')), metrics.get((v, 'bench_64x128x128_l23'))
        if r1 or r23:
            r = r1 or r23
            payload['buffers'].append(dict(v=v, name=name, buf=buf, c1=r1 and r1['cycles'], c23=r23 and r23['cycles'],
                                           reads=int(r['a_reads']) + int(r['b_reads'])))
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
h2{font-size:15px;margin:18px 24px 8px}.note{margin:0 24px 14px;color:var(--mute);font-size:13px;max-width:980px}.best{font-weight:700}tr.grpTop td{border-top:2px solid var(--line)}
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
<div id="views"></div>
<h2>Set 3 B-buffer sweep — 64×128×128 (from <code>results/set3_results.csv</code>)</h2>
<p class="note">Where Set 3 matters: B for 64×128×128 is 16 KB. With the same 4 KB buffer, Set 2 can only reuse the column tiles that fit;
Set 3 reorders the loop (group → row tile → col tile in group → kk) so the buffer is refilled per group and every byte of B is read once.</p>
<table id="buf"></table>
<h2>All cases (local Icarus runs, pipelined memory)</h2>
<table id="tbl"></table>
<script>
const P=/*DATA*/null;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SETNAMES=Object.fromEntries(P.order.map(k=>[k,P.sets[k].name]));
let mode='all', kase=P.cases.find(c=>P.order.some(k=>P.sets[k].waves[c]));
const views={};      // set -> view state
const ROWH=24, LBLW=170, TOP=26;

function seg(el,items,get,set){el.innerHTML='';for(const [k,l] of items){const b=document.createElement('button');b.textContent=l;
 b.className=get()===k?'on':'';b.onclick=()=>{set(k);seg(el,items,get,set);render()};el.appendChild(b)}}
seg(document.getElementById('setSeg'),[['all','All'],['s1s2s3','Sets 1–3'],...P.order.map(k=>[k,SETNAMES[k]])],()=>mode,v=>{mode=v;for(const k in views)views[k].fit=true});
seg(document.getElementById('caseSeg'),P.cases.map(c=>[c,c]),()=>kase,v=>{kase=v;for(const k in views)views[k].fit=true});

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
  d.innerHTML=`<div class="vtitle">${P.sets[set].title}<span>${w?w.n+' cycles shown':'no waveform for this case'}${m?` · M×N×K = ${m.m}×${m.n}×${m.k}, mem latency ${m.lat}`:''}</span></div>`;
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
 document.getElementById('note').textContent=m&&+m.m<=4?
  `Note: M=${m.m} fits in one row tile, so Set 3 has nothing to group (B is never reused across row tiles) and runs the same loop as Set 2. `+
  (diff>0?`The ${diff} extra cycle${diff>1?'s':''} (${m.cycles} vs ${m2.cycles}) come from Set 3's start-up SETUP state that picks G${setup?` (${setup} cycles in the waveform)`:''}. `:
   diff===0?`Cycle counts are equal: Set 3's SETUP state${setup?` (${setup} cycles)`:''} is hidden behind the memory latency. `:'')+
  'See the 64×128×128 sweep below for Set 3\'s gain.':'';
}
function table(){
 const cs=[...new Set(Object.keys(P.metrics).map(k=>k.split('|')[1]))];
 let h='<tr><th>case</th><th>variant</th><th>M×N×K</th><th>lat</th><th>cycles</th><th>speedup</th><th>A reads</th><th>B reads</th><th>max in flight</th></tr>';
 for(const c of cs)for(const v of P.order){const m=P.metrics[v+'|'+c];if(!m)continue;const b=P.metrics['baseline|'+c];
  h+=`<tr class="${v===P.order.find(x=>P.metrics[x+'|'+c])?'grpTop':''}"><td>${v===P.order.find(x=>P.metrics[x+'|'+c])?c:''}</td><td>${SETNAMES[v]}</td><td>${m.m}×${m.n}×${m.k}</td><td>${m.lat}</td><td>${(+m.cycles).toLocaleString()}</td>
   <td>${b?(b.cycles/m.cycles).toFixed(2)+'×':''}</td><td>${(+m.a_reads).toLocaleString()}</td><td>${(+m.b_reads).toLocaleString()}</td><td>${m.max_outstanding}</td></tr>`}
 document.getElementById('tbl').innerHTML=h;
 const B=P.buffers,b0=B.find(x=>x.v==='baseline');const num=x=>x==null?'—':(+x).toLocaleString();
 let g='<tr><th>variant</th><th>B buffer</th><th>cycles (lat 1)</th><th>speedup</th><th>cycles (lat 23)</th><th>speedup</th><th>total reads</th><th>read change</th></tr>';
 for(const r of B){g+=`<tr class="${r.v==='s3_all'?'best':''}"><td>${r.name}</td><td>${r.buf}</td><td>${num(r.c1)}</td><td>${b0&&r.c1?(b0.c1/r.c1).toFixed(1)+'×':''}</td>
  <td>${num(r.c23)}</td><td>${b0&&r.c23?(b0.c23/r.c23).toFixed(1)+'×':''}</td><td>${num(r.reads)}</td><td>${b0&&r.v!=='baseline'?((r.reads/b0.reads-1)*100).toFixed(1)+'%':'—'}</td></tr>`}
 document.getElementById('buf').innerHTML=g;
}
document.getElementById('fit').onclick=()=>{for(const k in views)views[k].fit=true;render()};
document.getElementById('sync').onchange=()=>{for(const k in views)views[k].fit=true;render()};
window.addEventListener('resize',()=>{for(const k in views)if(views[k].cv&&views[k].cv.isConnected)draw(views[k])});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',render);
table();render();
</script></body></html>
'''

if __name__ == '__main__':
    main()
