`timescale 1ns/1ps
// Waveform dump for the official testbench, compiled next to tb_gemm.sv (which is not modified).
// +VCD=<file>        write a VCD of the top level, gemm_top, operand_loader and gemm_control
// +VCDCYCLES=<n>     stop dumping after n clock cycles (10 ns each) to keep long runs small
module wave_dump;
    reg [1023:0] f;
    integer lim;
    initial if($value$plusargs("VCD=%s", f)) begin
        $dumpfile(f);
        $dumpvars(1, tb_gemm);
        $dumpvars(1, tb_gemm.dut);
        $dumpvars(1, tb_gemm.dut.loader);
        $dumpvars(1, tb_gemm.dut.control);
        if($value$plusargs("VCDCYCLES=%d", lim)) begin #(lim*10); $dumpoff; end
    end
endmodule
