`timescale 1ns/1ps
// Verification-only latency/backpressure injection. Never part of baseline synthesis.
module rv_delay #(parameter WIDTH=1, DELAY=3)(
    input clk,rst,input iv,output ir,input [WIDTH-1:0] ip,
    output ov,input ordy,output [WIDTH-1:0] op);
    reg full=0;
    integer remaining=0;
    reg [WIDTH-1:0] payload=0;
    assign ir=!rst && !full;
    assign ov=!rst && full && remaining==0;
    assign op=payload;
    always @(posedge clk) begin
        if(rst) begin full<=0;remaining<=0;payload<=0;end
        else begin
            if(full && remaining>0) remaining<=remaining-1;
            if(ov && ordy) full<=0;
            if(iv && ir) begin full<=1;payload<=ip;remaining<=DELAY;end
        end
    end
endmodule
