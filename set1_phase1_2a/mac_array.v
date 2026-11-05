`timescale 1ns/1ps
// PE_ROWS x PE_COLS output-stationary MAC array, NOT a systolic array.
// a[r] is broadcast across a row; b[c] is broadcast down a column.
// Dense baseline: multiplication+accumulation in the same clock stage.
module mac_array #(parameter PE_ROWS=4,PE_COLS=4)(
    input clk,rst,input op_valid,output op_ready,
    input [8*PE_ROWS-1:0] op_a,input [8*PE_COLS-1:0] op_b,
    input [PE_ROWS-1:0] op_rows,input [PE_COLS-1:0] op_cols,
    input op_first,op_last,
    input [PE_ROWS*PE_COLS-1:0] work_mask,
    output reg result_valid,input result_ready,
    output reg [32*PE_ROWS*PE_COLS-1:0] result_data,
    output reg [PE_ROWS*PE_COLS-1:0] result_mask,
    output [PE_ROWS*PE_COLS-1:0] event_mac,event_nonzero_mac
);
    localparam P=PE_ROWS*PE_COLS;
    reg signed [31:0] acc[0:P-1];
    wire signed [15:0] product[0:P-1];
    wire signed [31:0] next_sum[0:P-1];
    wire [P-1:0] mask;
    assign op_ready=(!result_valid || result_ready) && !rst;
    genvar r,c;
    generate for(r=0;r<PE_ROWS;r=r+1) begin: row
        for(c=0;c<PE_COLS;c=c+1) begin: col
            localparam I=r*PE_COLS+c;
            assign mask[I]=op_rows[r] && op_cols[c];
            assign product[I]=$signed(op_a[r*8 +: 8])*$signed(op_b[c*8 +: 8]);
            assign next_sum[I]=(op_first?32'sd0:acc[I])+
                (work_mask[I]?{{16{product[I][15]}},product[I]}:32'sd0);
            assign event_mac[I]=op_valid && op_ready && mask[I] && work_mask[I];
            assign event_nonzero_mac[I]=event_mac[I] && (op_a[r*8 +: 8]!=0) && (op_b[c*8 +: 8]!=0);
        end
    end endgenerate
    integer i;
    always @(posedge clk) begin
        if(rst) begin
            result_valid<=0;result_data<=0;result_mask<=0;
            for(i=0;i<P;i=i+1) acc[i]<=0;
        end else begin
            if(result_valid && result_ready) result_valid<=0;
            if(op_valid && op_ready) begin
                for(i=0;i<P;i=i+1) begin
                    acc[i]<=mask[i]?next_sum[i]:32'sd0;
                    if(op_last) result_data[32*i +: 32]<=mask[i]?next_sum[i]:32'sd0;
                end
                if(op_last) begin result_valid<=1;result_mask<=mask;end
            end
        end
    end
endmodule
