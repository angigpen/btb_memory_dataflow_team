`timescale 1ns/1ps
// One outstanding scalar read; one operand packet buffered.
// No DMA prefetch, local cache, banking or ping-pong buffering.
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4)(
    input clk,rst,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,
    output mem_req_valid,input mem_req_ready,
    output mem_req_is_b,output [31:0] mem_req_addr,
    input mem_rsp_valid,output mem_rsp_ready,input [7:0] mem_rsp_data,
    output op_valid,input op_ready,
    output reg [8*PE_ROWS-1:0] op_a,output reg [8*PE_COLS-1:0] op_b,
    output reg [PE_ROWS-1:0] op_rows,output reg [PE_COLS-1:0] op_cols,
    output reg op_first,op_last,output loader_busy
);
    localparam IDLE=0,A_REQ=1,A_RSP=2,B_REQ=3,B_RSP=4,OFFER=5;
    localparam RB=(PE_ROWS>1)?$clog2(PE_ROWS):1, CB=(PE_COLS>1)?$clog2(PE_COLS):1;
    reg [2:0] state;
    reg [31:0] a_addr,b_addr;
    reg [15:0] stride;
    reg [RB-1:0] ar;
    reg [CB-1:0] bc;
    assign req_ready=(state==IDLE) && !rst;
    assign mem_req_valid=!rst && ((state==A_REQ && op_rows[ar]) || (state==B_REQ && op_cols[bc]));
    assign mem_req_is_b=(state==B_REQ);
    assign mem_req_addr=(state==A_REQ)?a_addr:b_addr;
    assign mem_rsp_ready=!rst && (state==A_RSP || state==B_RSP);
    assign op_valid=(state==OFFER) && !rst;
    assign loader_busy=(state!=IDLE);
    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE;ar<=0;bc<=0;a_addr<=0;b_addr<=0;stride<=0;
            op_a<=0;op_b<=0;op_rows<=0;op_cols<=0;op_first<=0;op_last<=0;
        end else case(state)
            IDLE: if(req_valid && req_ready) begin
                a_addr<=req_a_addr;b_addr<=req_b_addr;stride<=req_a_stride;
                op_rows<=req_rows;op_cols<=req_cols;op_first<=req_first;op_last<=req_last;
                op_a<=0;op_b<=0;ar<=0;bc<=0;state<=A_REQ;
            end
            A_REQ: if(!op_rows[ar]) begin
                if(ar==PE_ROWS-1) state<=B_REQ;
                else begin ar<=ar+1'b1;a_addr<=a_addr+stride;end
            end else if(mem_req_ready) state<=A_RSP;
            A_RSP: if(mem_rsp_valid) begin
                op_a[8*ar +: 8]<=mem_rsp_data;
                if(ar==PE_ROWS-1) state<=B_REQ;
                else begin ar<=ar+1'b1;a_addr<=a_addr+stride;state<=A_REQ;end
            end
            B_REQ: if(!op_cols[bc]) begin
                if(bc==PE_COLS-1) state<=OFFER;
                else begin bc<=bc+1'b1;b_addr<=b_addr+1'b1;end
            end else if(mem_req_ready) state<=B_RSP;
            B_RSP: if(mem_rsp_valid) begin
                op_b[8*bc +: 8]<=mem_rsp_data;
                if(bc==PE_COLS-1) state<=OFFER;
                else begin bc<=bc+1'b1;b_addr<=b_addr+1'b1;state<=B_REQ;end
            end
            OFFER: if(op_ready) state<=IDLE;
            default: state<=IDLE;
        endcase
    end
endmodule
