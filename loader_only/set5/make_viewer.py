"""Capture real memory responses, MAC handshakes and C writes for the Set 5 viewer."""
import csv
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
from generate import make_case

SOURCES = ["gemm_control.v", "operand_loader.v", "sparsity_frontend.v",
           "mac_array.v", "result_writer.v", "gemm_top.v"]
MONITOR = r"""
module viewer_monitor;
integer fd, read_a=0, read_b=0;
reg pending_b;
reg [31:0] pending_addr;
initial fd=$fopen("trace.jsonl","w");
always @(posedge tb_gemm.clk) begin
    if(tb_gemm.rst) begin read_a=0; read_b=0; end
    else begin
        if(tb_gemm.sv && tb_gemm.sr) begin
            if(pending_b) read_b=read_b+1; else read_a=read_a+1;
            $fwrite(fd,"{\"cycle\":%0d,\"type\":\"read\",\"is_b\":%0d,\"addr\":%0d,\"value\":%0d}\n",
                tb_gemm.tick,pending_b,pending_addr,$signed(tb_gemm.sd));
        end
        if(tb_gemm.qv && tb_gemm.qr) begin
            pending_b=tb_gemm.qb; pending_addr=tb_gemm.qa;
        end
        if(tb_gemm.dut.cv && tb_gemm.dut.cr) begin
            $fwrite(fd,"{\"cycle\":%0d,\"type\":\"op\",\"bank\":%0d,\"row\":%0d,\"col\":%0d,\"k\":%0d,\"a\":\"%h\",\"b\":\"%h\",\"first\":%0d,\"last\":%0d}\n",
                tb_gemm.tick,tb_gemm.dut.cbank,
                tb_gemm.dut.loader.s_aaddr[tb_gemm.dut.loader.rd]/tb_gemm.dut.control.k,
                tb_gemm.dut.loader.s_baddr[tb_gemm.dut.loader.rd]%tb_gemm.dut.control.n,
                tb_gemm.dut.loader.s_baddr[tb_gemm.dut.loader.rd]/tb_gemm.dut.control.n,
                tb_gemm.dut.ca,tb_gemm.dut.cb,tb_gemm.dut.cf,tb_gemm.dut.cl);
        end
        if(tb_gemm.ov && tb_gemm.ordy)
            $fwrite(fd,"{\"cycle\":%0d,\"type\":\"write\",\"addr\":%0d,\"value\":%0d}\n",
                tb_gemm.tick,tb_gemm.oa,$signed(tb_gemm.od));
    end
end
endmodule
"""


def signed(value):
    return value - 256 if value >= 128 else value


def capture(plan, banks):
    work = ROOT / "build" / f"viewer_plan{plan}_acc{banks}"
    make_case(work, m=20, n=12, k=6, latency=3, seed=42,
              pattern="inspection", stalls=2, output_stalls=2)
    tb = work / "tb.sv"
    tb.write_text("`define PR 4\n`define PC 4\n" + (ROOT / "tests/tb_gemm.sv").read_text())
    monitor = work / "monitor.sv"
    monitor.write_text(MONITOR)
    exe = work / "simulation.vvp"
    args = ["iverilog", "-g2012", "-s", "tb_gemm", "-s", "viewer_monitor",
            "-o", str(exe), str(ROOT / "tests/rv_monitor.sv"), str(tb), str(monitor)]
    args += [str(ROOT / f"plan{plan}_acc{banks}" / name) for name in SOURCES]
    compiled = subprocess.run(args, capture_output=True, text=True)
    if compiled.returncode:
        raise RuntimeError(compiled.stderr)
    result = subprocess.run(["vvp", "-n", str(exe), "+HOST_TEST", "+OPTIMIZED"],
                            cwd=work, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "GEMM REGRESSION PASS" in result.stdout, result.stdout
    events = [json.loads(line) for line in (work / "trace.jsonl").read_text().splitlines()]
    matrices = {name: [signed(int(x, 16)) for x in (work / (name + ".hex")).read_text().split()]
                for name in ("a", "b")}
    golden = [int(x, 16) for x in (work / "c.hex").read_text().split()]
    golden = [v - 2**32 if v >= 2**31 else v for v in golden]
    sums = [0] * 240
    reads = []
    ops = []
    writes = []
    for event in events:
        if event["type"] == "read":
            is_b, addr = event["is_b"], event["addr"]
            assert event["value"] == matrices["b" if is_b else "a"][addr]
            reads.append((is_b, addr))
        elif event["type"] == "op":
            a, b = (int(event[name], 16) for name in ("a", "b"))
            event["a"] = [signed((a >> (8*i)) & 255) for i in range(4)]
            event["b"] = [signed((b >> (8*i)) & 255) for i in range(4)]
            row, col, k = (event[name] for name in ("row", "col", "k"))
            ops.append((row, col, k, event["bank"]))
            assert event["first"] == int(k == 0)
            assert event["last"] == int(k == 5)
            for i in range(4):
                assert event["a"][i] == matrices["a"][(row+i)*6+k]
                for j in range(4):
                    assert event["b"][j] == matrices["b"][k*12+col+j]
                    sums[(row+i)*12+col+j] += event["a"][i]*event["b"][j]
        else:
            assert event["value"] == golden[event["addr"]]
            writes.append(event["addr"])
    expected_ops = [(row, col, k, bank)
                    for block in range(0, 20, 4*banks) for col in range(0, 12, 4)
                    for k in range(6) for bank, row in enumerate(range(block, min(20, block+4*banks), 4))]
    assert ops == expected_ops
    assert sums == golden and sorted(writes) == list(range(240))
    # Compare actual responses with the complete previously verified request order.
    request_file = ROOT / "verification_reports" / f"plan{plan}_acc{banks}_reads.csv"
    expected_reads = [(int(r["is_b"]), int(r["address"])) for r in csv.DictReader(request_file.open())]
    assert reads == expected_reads
    metrics = next(csv.DictReader((work / "metrics.csv").open()))
    print(f"PASS viewer plan {plan} / ACC {banks}: {len(ops)} MAC packets, {len(reads)} reads", flush=True)
    return dict(plan=plan, banks=banks, events=events, metrics=metrics, **matrices)


if __name__ == "__main__":
    runs = [capture(p,b) for p in (1,2,3) for b in (2,4)]
    template = (ROOT / "viewer_template.html").read_text()
    payload = dict(m=20, n=12, k=6, runs=runs)
    (ROOT / "set5.html").write_text(template.replace("/*DATA*/null", json.dumps(payload, separators=(",", ":"))))
    (ROOT / "viewer_verification.json").write_text(json.dumps(
        dict(runs=6, checks=["read values and full order", "MAC order and operands",
                            "first/last", "C partial sum reconstruction", "all C writes"]),
        indent=2))
