`timescale 1ns/1ps
// Packet trace for the HTML viewer, compiled next to tb_gemm.sv (which is not modified).
// +PKT=<file>  one line per packet taken by mac_array:
//   cycle a_addr b_addr op_a op_b rows cols first last
// a_addr/b_addr come from the descriptor that produced the packet (descriptors and packets are
// 1:1 and in order in every set), so the viewer knows the tile row (a_addr / K), kk and column.
// cycle = rising-edge index from time 0, the same count the VCD parser uses.
module pkt_trace;
    reg [1023:0] f; integer fd=0, cyc=0, wp=0, rp=0;
    reg [63:0] q [0:255];
    initial if($value$plusargs("PKT=%s", f)) fd=$fopen(f, "w");
    always @(posedge tb_gemm.clk) begin
        if(fd) begin
            if(tb_gemm.rst) begin wp=0; rp=0; end
            else begin
                if(tb_gemm.dut.cv && tb_gemm.dut.cr) begin
                    $fdisplay(fd, "%0d %0d %0d %h %h %b %b %0d %0d", cyc, q[rp[7:0]][63:32], q[rp[7:0]][31:0],
                              tb_gemm.dut.ca, tb_gemm.dut.cb, tb_gemm.dut.crm, tb_gemm.dut.ccm,
                              tb_gemm.dut.cf, tb_gemm.dut.cl);
                    rp=rp+1;
                end
                if(tb_gemm.dut.dv && tb_gemm.dut.dr) begin q[wp[7:0]]={tb_gemm.dut.aa, tb_gemm.dut.ba}; wp=wp+1; end
            end
        end
        cyc=cyc+1;
    end
endmodule
