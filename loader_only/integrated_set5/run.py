"""Reproduce six-team-RTL integration regressions and compare measured events."""
import argparse
import concurrent.futures
import csv
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"tests"))
from generate import make_case, suite

EXTRA = {
    "nm20_2of4": dict(m=20,n=12,k=8,pattern="html_2of4"),
    "nm20_dense": dict(m=20,n=12,k=8,pattern="html_4of4"),
    "nm20_zeros": dict(m=20,n=12,k=8,pattern="html_0of4"),
    "nm20_mixed": dict(m=20,n=12,k=20,pattern="html_mixed"),
    "bank_zero": dict(m=35,n=11,k=9,pattern="bank_zero",stalls=2,output_stalls=2),
    "nm_tail": dict(m=27,n=11,k=7,pattern="html_2of4",stalls=2,output_stalls=2),
    "zero_groups": dict(m=35,n=11,k=20,pattern="zero_groups",stalls=2,output_stalls=2),
    "special_values": dict(m=35,n=11,k=19,pattern="special_values",stalls=2,output_stalls=2),
    "multirow_k1024": dict(m=19,n=17,k=1024,latency=3,stalls=2,output_stalls=2),
    "bench64_2of4": dict(m=64,n=128,k=128,pattern="html_2of4"),
    "bench64_dense": dict(m=64,n=128,k=128,pattern="html_4of4"),
    "bench64_2of4_l23": dict(m=64,n=128,k=128,pattern="html_2of4",latency=23),
}
WAVES = {"nm20_2of4","nm_tail","nm20_zeros"}
ENERGY = dict(request_pj=100,read_byte_pj=50,ram_word_pj=12,write_word_pj=30,
              classify_value_pj=2,multiply_pj=8,fast_op_pj=2,accumulate_pj=3,
              cycle_pj=15,static_mw=0,clock_mhz=100)


def read_csv(path):
    return list(csv.DictReader(path.open()))


def expected_packets(m,n,k,b):
    rounds=0
    for col in range(0,n,4):
        for base in range(0,k,4):
            end=min(base+4,k)
            nnz=max(sum(b[q*n+c]!=0 for q in range(base,end))
                    for c in range(col,min(col+4,n)))
            count=end-base if nnz>=3 else nnz
            rounds+=max(1,count) if base==0 or end==k else count
    return ((m+3)//4)*rounds


def signed(x,bits=8):
    return x-(1<<bits) if x>=(1<<(bits-1)) else x


def verify_trace(work,metrics):
    case=json.loads((work/"case.json").read_text())
    m,n,k=(case[p] for p in ("m","n","k"))
    a=[signed(int(x,16)) for x in (work/"a.hex").read_text().split()]
    b=[signed(int(x,16)) for x in (work/"b.hex").read_text().split()]
    golden=[signed(int(x,16),32) for x in (work/"c.hex").read_text().split()]
    accum=[0]*(m*n)
    rows=read_csv(work/"nm_trace.csv")
    for event in rows:
        row,col,base=(int(event[p]) for p in ("row","col","k_base"))
        av,bv,positions,mask=(int(event[p],16) for p in ("a","b","k_positions","work_mask"))
        for c in range(4):
            offset=(positions>>(2*c))&3
            value_b=signed((bv>>(8*c))&255)
            for r in range(4):
                if not ((mask>>(4*r+c))&1):
                    continue
                i,j,q=row+r,col+c,base+offset
                assert i<m and j<n and q<k
                value_a=signed((av>>(8*(4*offset+r)))&255)
                assert value_a==a[i*k+q] and value_b==b[q*n+j],event
                accum[i*n+j]+=value_a*value_b
    assert accum==golden, "Trace partial sums differ from independent golden"
    assert len(rows)==int(metrics["compute_packets"])
    # Every byte requested in the captured waveform corresponds to a real source value.
    requests=read_csv(work/"reads.csv")
    assert sum(int(r["is_b"])==0 for r in requests)==int(metrics["a_reads"])
    assert sum(int(r["is_b"])==1 for r in requests)==int(metrics["b_reads"])
    from vcd_analysis import check_wave
    waveform_cycles=check_wave(work/"wave.vcd",rows)
    return dict(vcd_cycles=waveform_cycles,packets=len(rows),mixed_k=int(metrics["mixed_k_packets"]),
                checks=["full NM payload stable under backpressure",
                        "physical ACC selection", "selected A/B values",
                        "C reconstructed from actual MAC handshakes", "read totals"])


def storage(plan,banks):
    width=1 if banks==2 else 2
    # Declared data/metadata arrays. Control/address scalar registers are excluded.
    loader_slot_bits=4*(32+32+4+4+3+width+32+32+12+10+8+4+4+4)
    frontend_bits=(2+1+1+2*width+2+16*banks
                   +2*banks*4*32+2*4*32+2*4*16
                   +2*banks*16+2*16+2*banks*4+2*4
                   +4*width+2*16+2*3+2+2)
    return dict(reuse_ram_bytes=20480,loader_slot_bits=loader_slot_bits,
                shared_b_bytes=4,extra_b_bytes=0 if plan==1 else 16,
                frontend_bits=frontend_bits,control_result_fifo_bytes=32,
                result_payload_bytes=132,acc_bytes=banks*64,
                buffer_bytes=20480+loader_slot_bits/8+4+(0 if plan==1 else 16)
                             +frontend_bits/8+32+132,
                buffer_acc_bytes=20480+loader_slot_bits/8+4+(0 if plan==1 else 16)
                                 +frontend_bits/8+32+132+banks*64)


def model_energy(row,coef):
    time_us=int(row["cycles"])/coef["clock_mhz"]
    energy_pj=((int(row["a_reads"])+int(row["b_reads"]))*coef["read_byte_pj"]+
               (int(row["a_reads"])+int(row["b_reads"]))*coef["request_pj"]+
               int(row["ram_accesses"])*coef["ram_word_pj"]+
               int(row["m"])*int(row["n"])*coef["write_word_pj"]+
               int(row["classify_values"])*coef["classify_value_pj"]+
               int(row["mul"])*coef["multiply_pj"]+
               sum(int(row[p]) for p in ("pass","neg","shift"))*coef["fast_op_pj"]+
               sum(int(row[p]) for p in ("mul","pass","neg","shift"))*coef["accumulate_pj"]+
               int(row["cycles"])*coef["cycle_pj"])
    energy_nj=energy_pj/1000+coef["static_mw"]*time_us
    return dict(time_us=time_us,energy_nj_example=energy_nj,
                average_mw_example=energy_nj/time_us)


def execute(exe,work,wave=False,reset=False):
    command=["vvp","-n",str(exe),"+HOST_TEST","+OPTIMIZED"]
    if wave: command+=["+TRACE","+WAVE"]
    if reset: command+=["+RESET_TEST"]
    start=time.monotonic()
    result=subprocess.run(command,cwd=work,capture_output=True,text=True,timeout=600)
    elapsed=time.monotonic()-start
    (work/"simulation.log").write_text(result.stdout+result.stderr)
    assert result.returncode==0 and "GEMM REGRESSION PASS" in result.stdout, result.stdout+result.stderr
    base,extra=read_csv(work/"metrics.csv"),read_csv(work/"integration_metrics.csv")
    assert len(base)==len(extra),(work,len(base),len(extra))
    output=[]
    fixture=json.loads((work/"case.json").read_text())
    b=[signed(int(x,16)) for x in (work/"b.hex").read_text().split()]
    expected=expected_packets(fixture["m"],fixture["n"],fixture["k"],b)
    for dense,nm in zip(base,extra):
        merged={**dense,**nm,"simulator_wall_seconds":elapsed}
        assert int(nm["compute_packets"])==expected,(work,nm,expected)
        assert int(dense["mac_slots"])==fixture["dense_mac_count"]-fixture["zero_product_count"]
        assert int(nm["mul"])+int(nm["pass"])+int(nm["neg"])+int(nm["shift"])==int(dense["mac_slots"])
        output.append(merged)
    return output


def run_variant(plan,banks,cases):
    name=f"plan{plan}_acc{banks}"
    build=ROOT/"build"/name
    build.mkdir(parents=True,exist_ok=True)
    names=suite(build/"vectors",extended=True)
    names.remove("max_dimensions")
    for case,params in EXTRA.items():
        make_case(build/"vectors"/case,**params)
        names.append(case)
    if cases:names=cases.split(",")
    tb=build/"tb.sv"
    tb.write_text("`define PR 4\n`define PC 4\n"+(ROOT/"tests/tb_gemm.sv").read_text().replace("rv_monitor #(.WIDTH(9*(R+C)+2+P))", "rv_monitor #(.WIDTH(4*8*R+8*C+R+C+2+P))"))
    exe=build/"sim.vvp"
    rtl=sorted((ROOT/name).glob("*.v"))
    command=["iverilog","-g2012","-s","tb_gemm","-s","integration_monitor","-o",str(exe),
             str(ROOT/"tests/rv_monitor.sv"),str(tb),str(ROOT/"tests/integration_monitor.sv"),*map(str,rtl)]
    compiled=subprocess.run(command,capture_output=True,text=True)
    (build/"compile.log").write_text(compiled.stderr)
    assert compiled.returncode==0,compiled.stderr
    output=[];waves={}
    for case in names:
        work=build/"vectors"/case
        measured=execute(exe,work,case in WAVES,case=="restart")
        for row in measured:
            result=dict(variant=name,case=case,latency=json.loads((work/"case.json").read_text())["latency"],**row,
                        **storage(plan,banks))
            result.update(model_energy(result,ENERGY))
            output.append(result)
        if case in WAVES:
            waves[case]=verify_trace(work,output[-1])
        print("PASS",name,case,flush=True)
    if not cases:
        smaller=build/"tb_small.sv"
        smaller.write_text(tb.read_text().replace("    rv_monitor #(.WIDTH(82+R+C))",
            "    defparam tb_gemm.dut.BBUF_DEPTH=8;\n    defparam tb_gemm.dut.ABUF_DEPTH=8;\n    rv_monitor #(.WIDTH(82+R+C))",1))
        small_exe=build/"small.vvp"
        small_command=[str(smaller) if x==str(tb) else str(small_exe) if x==str(exe) else x for x in command]
        subprocess.run(small_command,check=True,capture_output=True)
        work=build/"vectors/small_buffers"
        make_case(work,m=35,n=19,k=7,latency=4,pattern="html_2of4",stalls=2,output_stalls=2)
        execute(small_exe,work)
        print("PASS",name,"small_buffers",flush=True)
    return output,waves


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--cases",default="")
    args=parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda p:run_variant(*p,args.cases),
                              [(p,b) for p in (1,2,3) for b in (2,4)]))
    rows=[r for result,_ in results for r in result]
    with (ROOT/"comparison.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    (ROOT/"wave_analysis.json").write_text(json.dumps(
        {f"plan{p}_acc{b}":waves for (p,b),(_,waves) in zip(
            [(p,b) for p in (1,2,3) for b in (2,4)],results)},indent=2))
    (ROOT/"energy_assumptions.json").write_text(json.dumps(ENERGY,indent=2))
    print("ALL PASS",len(rows),"measurement rows")
