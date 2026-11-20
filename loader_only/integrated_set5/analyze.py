"""Generate a self-contained comparison report from Icarus metrics and VCD audits."""
import csv
import html
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"tests"))
sys.path.insert(0,str(ROOT))
from run import verify_trace

rows=list(csv.DictReader((ROOT/"comparison.csv").open()))
audits={}
for plan in (1,2,3):
    for banks in (2,4):
        name=f"plan{plan}_acc{banks}"
        audits[name]={}
        for case in ("nm20_2of4","nm_tail","nm20_zeros"):
            row=next(r for r in rows if r["variant"]==name and r["case"]==case)
            work=ROOT/"build"/name/"vectors"/case
            if not work.exists(): work=ROOT/"waveforms"/name/case
            audits[name][case]=verify_trace(work,row)
(ROOT/"wave_analysis.json").write_text(json.dumps(audits,indent=2))
provenance=json.loads((ROOT/"provenance.json").read_text())
energy=json.loads((ROOT/"energy_assumptions.json").read_text())

def number(value,digits=0):
    return f"{float(value):,.{digits}f}"

headers=["버전","ACC","cycles","시간 µs","A read","B read","read 합계",
         "입력→NM 패킷","버퍼+ACC B","동적 mW 예시","에너지 nJ 예시","Sparsity 대기","MAC 대기"]
cases=list(dict.fromkeys(r["case"] for r in rows))
tables=[]
for case in cases:
    selected=[r for r in rows if r["case"]==case]
    if not selected:continue
    first=selected[0]
    body=[]
    for row in selected:
        values=[row["variant"],row["variant"][-1],number(row["cycles"]),
                number(row["time_us"],2),number(row["a_reads"]),number(row["b_reads"]),
                number(int(row["a_reads"])+int(row["b_reads"])),
                row["input_packets"]+" → "+row["compute_packets"],
                number(math.ceil(float(row["buffer_acc_bytes"]))),
                number(row["average_mw_example"],3),number(row["energy_nj_example"],3),
                number(row["frontend_stall"]),number(row["mac_stall"])]
        body.append("<tr>"+"".join("<td>"+html.escape(str(v))+"</td>" for v in values)+"</tr>")
    tables.append(f'<section class="case" data-case="{case}"><h2>{case}</h2>'+
                  f'<p>A {first["m"]}×{first["k"]}, B {first["k"]}×{first["n"]} · 요청 latency {first["latency"]} · run별 행</p>'+
                  '<div class="scroll"><table><thead><tr>'+
                  "".join("<th>"+h+"</th>" for h in headers)+
                  "</tr></thead><tbody>"+"".join(body)+"</tbody></table></div></section>")
summary={
    "variants":6,"measurement_rows":len(rows),
    "cases_per_variant":{v:len({r["case"] for r in rows if r["variant"]==v})+1
                         for v in audits},
    "waveforms":18,
    "clock_mhz_assumed":energy["clock_mhz"],
    "power_basis":"Illustrative activity coefficients, not measured FPGA power",
    "sources":{k:v for k,v in provenance.items() if k.endswith("_commit")}
}
(ROOT/"verification_summary.json").write_text(json.dumps(summary,indent=2))
template="""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>메모리 · Sparsity · NM Compute 통합 비교</title><style>
body{font:15px/1.6 system-ui,sans-serif;margin:0;background:#f6f8fb;color:#1a2c40}
main{max-width:1500px;margin:auto;padding:28px 18px}h1{font-size:28px}h2{font-size:19px}
p{color:#53657a}section{background:white;border:1px solid #d4deea;border-radius:10px;padding:16px;margin:18px 0}
.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
td,th{padding:7px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid #d9e2ec}
th{background:#e9f0f8;font-size:12px}td:first-child,th:first-child{text-align:left}
select{padding:8px;font:inherit}pre{white-space:pre-wrap;background:#eaf0f6;padding:10px}a{color:#176196}
</style><main><h1>메모리 · Sparsity · NM Compute 통합 비교</h1>
<p>실제 RTL 6개 · 이카루스 실행 · C 정답 및 NM K 선택 검증. 100 MHz를 가정하면 1 cycle = 10 ns입니다.</p>
<label>입력 선택 <select id="cases">OPTIONS</select></label>
TABLES
<section><h2>해석과 가정</h2>
<p>세 안 모두 기존 A/B reuse RAM과 4개 operand slot을 유지합니다. 2·3안의 B 모으기 공간은 16 B입니다.
Sparsity는 두 개의 그룹 버퍼를 이용해 ACC별 A 16개와 공유 B 16개, 태그·위치를 저장합니다.
NM compute는 열마다 K를 고르고, 실제 ACC 저장 세트는 2 또는 4입니다. PE_Folding=0입니다.</p>
<p>사이클과 read 수는 testbench 측정값입니다. 시간은 cycles ÷ 100 MHz이며 합성 후 달성 가능한 Fmax는 측정하지 않았습니다.
버퍼는 A/B RAM, operand slot 데이터/추적 배열, B latch/선읽기, NM 그룹 저장과 상태, 결과 FIFO, MAC·writer의 결과 payload와 ACC의 선언 용량입니다.
주소·제어 scalar 레지스터 및 BRAM 단위 반올림은 제외하며 전체 FPGA 면적과 구분합니다. CSV에는 각 항목을 분리했습니다.</p>
<p>전력은 계수를 임의 설정한 동적 활동량 예시입니다. 단위 연산, RAM/외부 read, 기록, tag 처리 및 cycle의 에너지를 합해 실행 시간으로 나눴습니다.
FPGA 부품·전압·합성·배치·SAIF 측정이 없으므로 실측 mW나 감소율을 예측하는 근거로 쓰면 안 됩니다.
평균 전력이 작아도 실행이 길어지면 에너지가 늘 수 있어 두 값을 함께 표시합니다. static_mw=0입니다.</p>
<pre>ENERGY</pre>
<p>2·3안은 B를 먼저 읽지만 sparsity는 원래 A+B packet에서 입력을 받습니다.
별도 B 조기 분석 경로가 없어, 1바이트 공유 포트에서 2·3안의 주소 순서 변경만으로 NM 판정이 일찍 시작되지는 않습니다.
K가 커서 B RAM에 일부 열만 들어가면, ACC를 늘려 한 B 패킷을 더 많은 행에 사용하는 것이 외부 B 재읽기를 줄일 수 있습니다. multirow_k1024에서 이 차이를 확인할 수 있습니다.
후보들을 순차로 통과시키는 16 B bridge의 응답 중계 cycle도 측정에 포함됩니다.
서로 다른 칸의 메모리 지연을 동일하게 둔 현재 메모리 모델에서는 2·3안의 cycle 수가 같습니다.</p>
<p>동일한 결과를 유지하기 위해 모든 유효 ACC에 같은 round 수를 전달하고, 초기화와 마지막 출력용 zero-mask 패킷도 보존합니다.
wave_analysis.json은 18개 VCD를 직접 샘플링해 MAC 수락 payload와 기록이 같은지 확인하고, 실제 선택한 A/B와 work_mask로 C를 재계산한 결과입니다.</p>
<p>SOURCES</p></section></main>
<script>
const sel=document.getElementById('cases');function show(){document.querySelectorAll('.case').forEach(e=>e.hidden=e.dataset.case!==sel.value)}
sel.value='bench64_2of4';sel.onchange=show;show();
</script></html>"""
options="".join(f'<option value="{c}">{c}</option>' for c in cases)
sources=(
    f'<a href="https://github.com/angigpen/btb_memory_dataflow_team/tree/{provenance["memory_commit"]}/loader_only/set5">Memory Set 5</a> · '
    f'<a href="https://github.com/2wodud/BTB_hands_Sparsity/tree/{provenance["sparsity_commit"]}/Sparsity_SET5_NM_Final_GitHub/Sparsity_SET5_NM_Final/rtl">Sparsity NM final</a> · '
    f'<a href="https://github.com/leejunjunjun/BTB_GEMM_compute-pipeline/tree/{provenance["compute_commit"]}/NM/rtl">Compute NM</a>')
(ROOT/"comparison.html").write_text(template.replace("OPTIONS",options).replace("TABLES","".join(tables))
    .replace("ENERGY",html.escape(json.dumps(energy,indent=2))).replace("SOURCES",sources))
print(json.dumps(summary,indent=2))
