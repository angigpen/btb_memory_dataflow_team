`timescale 1ns/1ps
// Tile result -> addressed C writes. Output order is tile-major.
// Consumers must use out_addr; do not assume globally sequential addresses.
module result_writer #(parameter PE_ROWS=4,PE_COLS=4)(
    input clk,rst,input result_valid,output result_ready,
    input [31:0] result_base,input [15:0] result_stride,
    input [32*PE_ROWS*PE_COLS-1:0] result_data,
    input [PE_ROWS*PE_COLS-1:0] result_mask,
    output out_valid,input out_ready,output [31:0] out_addr,out_data,
    output reg done
);
    localparam P=PE_ROWS*PE_COLS, LB=(P>1)?$clog2(P):1;
    reg active;
    reg [LB-1:0] lane;
    reg [31:0] row_addr,addr;
    reg [15:0] stride;
    reg [32*P-1:0] data;
    reg [P-1:0] mask;
    assign result_ready=!active && !rst;
    assign out_valid=active && mask[lane] && !rst;
    assign out_addr=addr;
    assign out_data=data[32*lane +: 32];
    always @(posedge clk) begin
        if(rst) begin
            active<=0;lane<=0;row_addr<=0;addr<=0;stride<=0;data<=0;mask<=0;done<=0; // initialize all registers on reset
        end else begin
            done<=0;
            if(result_valid && result_ready) begin                                   // if new result is valid and ready to be processed
                active<=1;lane<=0;row_addr<=result_base;addr<=result_base;
                stride<=result_stride;data<=result_data;mask<=result_mask;
            end else if(active && (!mask[lane] || out_ready)) begin
                if(lane==P-1) begin active<=0;done<=1;end
                else begin
                    lane<=lane+1'b1;
                    if((lane % PE_COLS)==PE_COLS-1) begin
                        row_addr<=row_addr+stride;addr<=row_addr+stride;
                    end else addr<=addr+1'b1;
                end
            end
        end
    end
endmodule
