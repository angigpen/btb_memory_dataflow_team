`timescale 1ns/1ps
// Tile scheduler for C[M,N] = A[M,K] * B[K,N], row-major storage.
// Memory/Dataflow change: loop order is unchanged (row tile -> col tile -> kk).
// Only new OUTPUTS are added so the loader can reuse data:
//   req_kk        : k index of this packet            (A reuse buffer address)
//   req_reuse_a   : col tile != 0 -> A for this kk was already read in col tile 0
//   req_reuse_b   : row tile != 0 -> B for this (col tile, kk) was already read in row tile 0
//   req_bbuf_addr : (col tile index) * K + kk          (B reuse buffer address, Set 2 only)
//   req_last_row  : this is the last row tile -> no later row tile will reuse B (Set 2 only)
module gemm_control #(parameter PE_ROWS=4, PE_COLS=4, MAX_M=64, MAX_N=128, MAX_K=1024)(
    input clk, rst, start,
    input [15:0] cfg_m, cfg_n, cfg_k,
    output reg busy, done, error,
    output req_valid, input req_ready,
    output [31:0] req_a_addr, req_b_addr,
    output [15:0] req_a_stride,
    output [PE_ROWS-1:0] req_rows, output [PE_COLS-1:0] req_cols,
    output req_first, req_last,
    output [15:0] req_kk,
    output req_reuse_a, req_reuse_b,
    output [31:0] req_bbuf_addr,
    output req_last_row,
    output result_enable, output [31:0] result_base,
    output [15:0] result_stride,
    input result_accepted, write_done
);
    localparam IDLE=0, ISSUE=1, RESULT=2, WRITE=3;
    reg [1:0] state;
    reg [15:0] m,n,k,row,col,kk;
    reg [31:0] a_tile,c_tile,a_ptr,b_ptr;
    reg [31:0] bb_base;                       // NEW: (col tile index) * K
    genvar g;
    assign req_valid=(state==ISSUE) && !rst;
    assign req_a_addr=a_ptr; assign req_b_addr=b_ptr; assign req_a_stride=k;
    assign req_first=(kk==0); assign req_last=(kk==k-1);
    assign req_kk=kk;                          // NEW
    assign req_reuse_a=(col!=0);               // NEW
    assign req_reuse_b=(row!=0);               // NEW
    assign req_bbuf_addr=bb_base+kk;           // NEW
    assign req_last_row=(row+PE_ROWS>=m);      // NEW
    generate for(g=0;g<PE_ROWS;g=g+1) begin: rows_mask
        assign req_rows[g]=(row+g<m);
    end
    for(g=0;g<PE_COLS;g=g+1) begin: cols_mask
        assign req_cols[g]=(col+g<n);
    end endgenerate
    assign result_enable=(state==RESULT) && !rst;
    assign result_base=c_tile+col; assign result_stride=n;
    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE; busy<=0; done<=0; error<=0;
            m<=0;n<=0;k<=0;row<=0;col<=0;kk<=0;
            a_tile<=0;c_tile<=0;a_ptr<=0;b_ptr<=0;bb_base<=0;
        end else begin
            done<=0;
            case(state)
                IDLE: if(start) begin
                    error<=0;
                    if(cfg_m==0 || cfg_n==0 || cfg_k==0 ||
                       cfg_m>MAX_M || cfg_n>MAX_N || cfg_k>MAX_K) begin
                        error<=1;done<=1;
                    end else begin
                        m<=cfg_m;n<=cfg_n;k<=cfg_k;row<=0;col<=0;kk<=0;
                        a_tile<=0;c_tile<=0;a_ptr<=0;b_ptr<=0;bb_base<=0;
                        busy<=1;state<=ISSUE;
                    end
                end
                ISSUE: if(req_valid && req_ready) begin
                    if(req_last) state<=RESULT;
                    else begin kk<=kk+1'b1;a_ptr<=a_ptr+1'b1;b_ptr<=b_ptr+n;end
                end
                RESULT: if(result_accepted) state<=WRITE;
                WRITE: if(write_done) begin
                    if(col+PE_COLS<n) begin
                        col<=col+PE_COLS;kk<=0;a_ptr<=a_tile;
                        b_ptr<=col+PE_COLS;bb_base<=bb_base+k;state<=ISSUE;
                    end else if(row+PE_ROWS<m) begin
                        row<=row+PE_ROWS;col<=0;kk<=0;
                        a_tile<=a_tile+PE_ROWS*k;a_ptr<=a_tile+PE_ROWS*k;
                        c_tile<=c_tile+PE_ROWS*n;b_ptr<=0;bb_base<=0;state<=ISSUE;
                    end else begin busy<=0;done<=1;state<=IDLE;end
                end
            endcase
        end
    end
endmodule
