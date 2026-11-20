"""Exercise both partial B-cache words and multi-column cache-group transitions."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from run import make_case,execute

for plan in (1,2,3):
    for banks in (2,4):
        name=f"plan{plan}_acc{banks}"
        for depth in (8,16):
            work=ROOT/"build"/"group_checks"/name/f"b{depth}"
            make_case(work,m=35,n=19,k=7,latency=4,pattern="html_2of4",
                      stalls=2,output_stalls=2)
            text=(ROOT/"tests/tb_gemm.sv").read_text()
            text=text.replace("    rv_monitor #(.WIDTH(82+R+C))",
                f"    defparam tb_gemm.dut.BBUF_DEPTH={depth};\n"
                "    defparam tb_gemm.dut.ABUF_DEPTH=8;\n"
                "    rv_monitor #(.WIDTH(82+R+C))",1)
            text=text.replace("rv_monitor #(.WIDTH(9*(R+C)+2+P))",
                              "rv_monitor #(.WIDTH(4*8*R+8*C+R+C+2+P))")
            tb=work/"tb.sv"
            tb.write_text("`define PR 4\n`define PC 4\n"+text)
            exe=work/"sim.vvp"
            cmd=["iverilog","-g2012","-s","tb_gemm","-s","integration_monitor",
                 "-o",str(exe),str(ROOT/"tests/rv_monitor.sv"),str(tb),
                 str(ROOT/"tests/integration_monitor.sv")]
            cmd+=list(map(str,(ROOT/name).glob("*.v")))
            r=subprocess.run(cmd,capture_output=True,text=True)
            assert r.returncode==0,r.stderr
            execute(exe,work)
            print("PASS",name,"A depth8 / B depth",depth,flush=True)
print("ALL PASS 12 small-cache cases")
