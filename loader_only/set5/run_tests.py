"""Compile each independent six-file RTL folder and verify C plus read ordering."""
import concurrent.futures
import csv
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
from generate import make_case, suite

SOURCES = ["gemm_control.v", "operand_loader.v", "sparsity_frontend.v",
           "mac_array.v", "result_writer.v", "gemm_top.v"]


def simulate(exe, directory, reset=False):
    args = ["vvp", "-n", str(exe), "+HOST_TEST", "+OPTIMIZED"]
    if reset:
        args.append("+RESET_TEST")
    result = subprocess.run(args, cwd=directory, capture_output=True,
                            text=True, timeout=600)
    (directory / "simulation.log").write_text(result.stdout + result.stderr)
    if result.returncode or "GEMM REGRESSION PASS" not in result.stdout:
        raise RuntimeError(str(directory) + "\n" + result.stdout + result.stderr)
    return list(csv.DictReader((directory / "metrics.csv").open()))


def check_order(trace, plan, banks):
    """Full inspection trace for A20x6 times B6x12; all B fits in reuse RAM."""
    expected = []
    for block in range(0, 20, banks * 4):
        for col in range(0, 12, 4):
            for chunk in range(0, 6, 4):
                end = min(6, chunk + 4)
                if block == 0 and plan > 1:
                    coordinates = [(q, j) for q in range(chunk, end)
                                   for j in range(col, col + 4)]
                    if plan == 3:
                        coordinates.sort(key=lambda pair: (pair[1], pair[0]))
                    expected.extend((1, q * 12 + j) for q, j in coordinates)
                for q in range(chunk, end):
                    if block == 0 and plan == 1:
                        expected.extend((1, q * 12 + j) for j in range(col, col + 4))
                    if col == 0:
                        expected.extend((0, row * 6 + q)
                                        for row in range(block, min(20, block + banks * 4)))
    actual = [(int(row["is_b"]), int(row["address"]))
              for row in csv.DictReader(trace.open())]
    if actual != expected:
        mismatch = next((i for i, pair in enumerate(zip(actual, expected))
                         if pair[0] != pair[1]), min(len(actual), len(expected)))
        raise AssertionError(f"Read order mismatch at {mismatch}: "
                             f"{actual[mismatch:mismatch+4]} vs {expected[mismatch:mismatch+4]}")
    return len(actual)


def run_variant(plan, banks):
    name = f"plan{plan}_acc{banks}"
    build = ROOT / "build" / name
    build.mkdir(parents=True, exist_ok=True)
    vectors = build / "vectors"
    names = suite(vectors, extended=True)
    if "--max-dimensions" not in sys.argv:
        names.remove("max_dimensions")
    make_case(vectors / "inspection", m=20, n=12, k=6, latency=3,
              seed=42, pattern="inspection", stalls=2, output_stalls=2)
    names.append("inspection")
    for k in (128, 1024):
        name_k = f"multirow_k{k}"
        make_case(vectors / name_k, m=19, n=17, k=k, latency=3,
                  seed=38, stalls=2, output_stalls=2)
        names.append(name_k)
    tb = build / "tb_configured.sv"
    tb.write_text("`define PR 4\n`define PC 4\n" +
                  (ROOT / "tests/tb_gemm.sv").read_text())
    monitor = build / "read_monitor.sv"
    monitor.write_text("""module schedule_read_monitor;
integer fd, cycle=0;
initial begin
    fd=$fopen("reads.csv","w");
    $fwrite(fd,"cycle,is_b,address\\n");
end
always @(posedge tb_gemm.clk) begin
    cycle=cycle+1;
    if(!tb_gemm.rst && tb_gemm.qv && tb_gemm.qr)
        $fwrite(fd,"%0d,%0d,%0d\\n",cycle,tb_gemm.qb,tb_gemm.qa);
end
endmodule
""")
    exe = build / "simulation.vvp"
    command = ["iverilog", "-g2012", "-s", "tb_gemm", "-s",
               "schedule_read_monitor", "-o", str(exe),
               str(ROOT / "tests/rv_monitor.sv"), str(tb), str(monitor)]
    command.extend(str(ROOT / f"plan{plan}_acc{banks}" / item) for item in SOURCES)
    compiled = subprocess.run(command, capture_output=True, text=True)
    (build / "compile.log").write_text(compiled.stderr)
    compiled.check_returncode()
    rows = []
    for case in names:
        work = vectors / case
        result = simulate(exe, work, case == "restart")
        rows.extend(dict(variant=f"plan{plan}_acc{banks}", case=case, **r)
                    for r in result)
    count = check_order(vectors / "inspection/reads.csv", plan, banks)
    # Force group transitions and an A RAM overflow without changing any delivered default.
    group_tb = build / "tb_groups.sv"
    anchor = "    rv_monitor #(.WIDTH(82+R+C))"
    overrides = ("    defparam tb_gemm.dut.BBUF_DEPTH = 8;\n"
                 "    defparam tb_gemm.dut.ABUF_DEPTH = 8;\n")
    group_tb.write_text(tb.read_text().replace(anchor, overrides + anchor, 1))
    group_exe = build / "groups.vvp"
    group_command = [str(group_exe) if item == str(exe)
                     else str(group_tb) if item == str(tb) else item for item in command]
    subprocess.run(group_command, capture_output=True, check=True)
    group_work = vectors / "groups"
    make_case(group_work, m=35, n=19, k=7, latency=4, seed=51,
              stalls=2, output_stalls=2)
    result = simulate(group_exe, group_work)
    rows.extend(dict(variant=f"plan{plan}_acc{banks}", case="small_buffers", **r)
                for r in result)
    with (build / "summary.csv").open("w") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"PASS plan {plan} / ACC {banks}: {len(names)+1} cases, "
          f"inspection {count} reads in exact requested order", flush=True)
    return rows


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda p: run_variant(*p),
                                    [(plan, banks) for plan in (1, 2, 3) for banks in (2, 4)]))
    rows = [row for result in results for row in result]
    with (ROOT / "results.csv").open("w") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (ROOT / "verification.json").write_text(json.dumps(
        {"variants": 6, "result_rows": len(rows),
         "checks": ["C exact equality", "handshake stability",
                    "full external read order", "stalls", "reset/restart",
                    "K/M/N tails", "K=1024", "buffer groups and A overflow"]},
        indent=2))
