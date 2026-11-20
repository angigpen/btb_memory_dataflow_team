"""Audit early-B modes from VCD, and compare direct-ingress RTL with the baseline."""
import csv
import html
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"tests"));sys.path.insert(0,str(ROOT))
from run import verify_trace
from early_wave_analysis import check_early_wave

rows=list(csv.DictReader((ROOT/"comparison.csv").open()))
baseline={(r["variant"],r["case"],r["run"]):r for r in
          csv.DictReader((ROOT/"baseline_comparison.csv").open())}
for r in rows:
    old=baseline.get((r["variant"],r["case"],r["run"]))
    r["baseline_cycles"]=old["cycles"] if old else ""
    r["cycle_change"]=int(r["cycles"])-int(old["cycles"]) if old else ""
    r["baseline_reads"]=(int(old["a_reads"])+int(old["b_reads"])) if old else ""
    r["baseline_buffer_acc_bytes"]=old["buffer_acc_bytes"] if old else ""
with (ROOT/"before_after.csv").open("w",newline="") as f:
    writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

audits={}
for plan in (1,2,3):
    for banks in (2,4):
        name=f"plan{plan}_acc{banks}";audits[name]={}
        for case in ("nm20_2of4","nm_tail","nm20_zeros"):
            row=next(r for r in rows if r["variant"]==name and r["case"]==case)
            work=ROOT/"build"/name/"vectors"/case
            if not work.exists():work=ROOT/"waveforms"/name/case
            audits[name][case]={**verify_trace(work,row),
                               "early_b":check_early_wave(work/"wave.vcd")}
            if plan>1:
                result=audits[name][case]["early_b"]
                assert result["mode_before_a_groups"]==result["groups"]
(ROOT/"wave_analysis.json").write_text(json.dumps(audits,indent=2))

def fmt(value,digits=0):
    return "—" if value=="" else f"{float(value):,.{digits}f}"
headers=["버전","기존 cycles","수정 cycles","차이","시간 µs","A+B read","기존 read",
         "버퍼+ACC B","전력 예시 mW","에너지 예시 nJ","첫 B 모드 확정",
         "첫 A 패킷","첫 MAC"]
cases=list(dict.fromkeys(r["case"] for r in rows))
tables=[]
for case in cases:
    selected=[r for r in rows if r["case"]==case]
    first=selected[0];body=[]
    for r in selected:
        values=[r["variant"],fmt(r["baseline_cycles"]),fmt(r["cycles"]),fmt(r["cycle_change"]),
                fmt(r["time_us"],2),fmt(int(r["a_reads"])+int(r["b_reads"])),fmt(r["baseline_reads"]),
                fmt(r["buffer_acc_bytes"],1),fmt(r["average_mw_example"],3),
                fmt(r["energy_nj_example"],3),r["first_b_mode_cycle"],
                r["first_a_cycle"],r["first_compute_cycle"]]
        body.append("<tr>"+"".join("<td>"+html.escape(str(v))+"</td>" for v in values)+"</tr>")
    tables.append(f'<section data-case="{case}"><h2>{case}</h2>'+
                  f'<p>A {first["m"]}×{first["k"]} · B {first["k"]}×{first["n"]} · latency {first["latency"]}</p>'+
                  '<div class="scroll"><table><thead><tr>'+
                  "".join("<th>"+h+"</th>" for h in headers)+
                  "</tr></thead><tbody>"+"".join(body)+"</tbody></table></div></section>")
page="""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>B 먼저 분석 · 직접 연결 비교</title>
<style>body{font:15px/1.6 system-ui,sans-serif;background:#f5f8fc;color:#213448;margin:0}
main{max-width:1600px;margin:auto;padding:25px 18px}h1{font-size:29px}h2{font-size:19px}p{color:#53687d}
section,.box{padding:16px;background:white;border:1px solid #d2deeb;border-radius:9px;margin:16px 0}
select{padding:8px;font:inherit}.scroll{overflow:auto}table{border-collapse:collapse;width:100%}
th,td{padding:8px;border-bottom:1px solid #d8e2ed;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
th{background:#e9f0f9;font-size:12px}td:first-child,th:first-child{text-align:left}
.flow{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.flow b{background:#e6f0fc;border:1px solid #b3cae3;border-radius:8px;padding:12px}
pre{white-space:pre-wrap;background:#eef3f9;padding:12px}
</style><main><h1>B를 먼저 분석하는 직접 연결</h1>
<div class="box"><div class="flow"><b>메모리</b><span>→</span><b>operand_loader</b><span>→ B 별도 입력</span><b>sparsity의 그룹 버퍼·태그·N:4</b><span>→</span><b>NM compute</b></div>
<p>A는 별도 입력으로 같은 그룹의 ACC별 공간에 들어옵니다. 기존 두 그룹 버퍼가 B 저장을 담당하고, loader는 B 값 16개를 중복 저장하지 않습니다.
B 열의 네 K 위치가 모이면 그 열의 모드를 즉시 확정합니다. A와 B가 모두 준비된 그룹은 기존 round→bank 순서로 compute에 발행합니다.</p>
<p>첫 B 모드·A 패킷·MAC 값은 실제 testbench clock 번호입니다. 전체 cycles는 busy 구간입니다. 2·3안은 B 모드가 A 패킷 수락보다 먼저 확정되는 것을 VCD로 검사했습니다.</p></div>
<label>입력 <select id="cases">OPTIONS</select></label>
TABLES
<div class="box"><h2>지표의 의미</h2>
<p>시간은 100 MHz 가정입니다. 전력과 에너지는 energy_assumptions.json의 예시 pJ 계수를 실제 활동 횟수에 곱한 값입니다.
FPGA 합성·배치·전력 실측을 수행한 수치와 구분해서 보세요. 버퍼는 데이터·추적 배열·NM 그룹 상태·결과 저장·ACC의 선언량이며 일부 주소·제어 scalar 및 BRAM 반올림은 제외합니다.</p>
<p>조기 B 분석이 가능해져도 전체 연산은 필요한 A가 준비되어야 시작합니다. 분석 자체는 B가 들어오는 클록에 태그와 위치를 갱신하는 구조입니다.
세로 방향은 첫 B 열의 N:4가 가장 빨리 확정될 수 있지만, 모든 B와 A가 필요한 첫 MAC 시점은 다른 안과 같을 수 있습니다.</p>
<p>기존 비교본은 integrated_set5입니다. 2·3안의 메모리 응답 중계 FSM을 제거했고, A cache hit을 같은 issue cycle에 전달합니다.
A/B reuse RAM 용량과 A packet slot 4개는 유지합니다. B는 sparsity의 두 그룹 버퍼를 공유해 저장합니다.</p>
<p>1안은 B 한 K 패킷→해당 K의 ACC별 A, 2안은 B 가로 네 줄→A, 3안은 B 세로 네 열→A입니다.
Cache hit인 B는 RAM word를 한 번 읽어 해당 K의 유효 열을 함께 넘기며, 외부 scalar miss는 안별 주소 순서대로 직접 받습니다.</p></div></main>
<script>const s=document.getElementById('cases');function show(){document.querySelectorAll('section[data-case]').forEach(e=>e.hidden=e.dataset.case!==s.value)}s.value='nm20_2of4';s.onchange=show;show();</script></html>"""
options="".join(f'<option value="{c}">{c}</option>' for c in cases)
(ROOT/"comparison.html").write_text(page.replace("OPTIONS",options).replace("TABLES","".join(tables)))
summary=dict(variants=6,measurement_rows=len(rows),cases_per_variant=len(cases)+1,
             vcd_waveforms=18,checks=["C exact equality","independent B/A handshake stability",
                                    "physical ACC routing","early N:4 mode timing"],
             sources=json.loads((ROOT/"provenance.json").read_text()))
(ROOT/"verification_summary.json").write_text(json.dumps(summary,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k!="sources"},indent=2))
