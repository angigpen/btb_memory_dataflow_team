`timescale 1ns/1ps
// Dense pass-through boundary reserved for the Sparsity team.
// No zero detection or skipping is implemented in this baseline.

module sparsity_frontend #(parameter PE_ROWS=4,PE_COLS=4)(
    input clk,rst,                                                        //clock and rest
    input in_valid,output in_ready,                                       //input handshake?
    input [8*PE_ROWS-1:0] in_a,input [8*PE_COLS-1:0] in_b,                //input a,b
    input [PE_ROWS-1:0] in_rows,input [PE_COLS-1:0] in_cols,
    input in_first,in_last,
    output out_valid,input out_ready,
    output [8*PE_ROWS-1:0] out_a,output [8*PE_COLS-1:0] out_b,
    output [PE_ROWS-1:0] out_rows,output [PE_COLS-1:0] out_cols,
    output out_first,out_last,output [PE_ROWS*PE_COLS-1:0] work_mask
);
    assign in_ready=out_ready && !rst;assign out_valid=in_valid && !rst;
    assign out_a=in_a;assign out_b=in_b;assign out_rows=in_rows;assign out_cols=in_cols;
    assign out_first=in_first;assign out_last=in_last;
    genvar r,c;
    generate for(r=0;r<PE_ROWS;r=r+1) begin: rows
        for(c=0;c<PE_COLS;c=c+1) begin: cols
            assign work_mask[r*PE_COLS+c]=in_rows[r] && in_cols[c];
        end
    end endgenerate
endmodule
