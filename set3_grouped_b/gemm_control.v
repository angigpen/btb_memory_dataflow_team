`timescale 1ns/1ps
// Tile scheduler for C[M,N] = A[M,K] * B[K,N], row-major storage.
// SET 3 (Memory/Dataflow): grouped ("staged") B reuse.
//
// Loop order:  group -> row tile -> col tile inside the group -> kk
//   A group is G column tiles. Their B (K x 4G bytes) is kept in the B buffer:
//     - row tile 0 of the group reads B from memory and fills the buffer
//     - later row tiles of the group read B from the buffer
//   A is read once per (group, row tile) and reused across the group's column tiles (A cache).
//   Reads: A = M*K*(number of groups), B = K*N  (each B value read once)
//
// G is chosen at start from the buffer size:  G = floor(BBUF_DEPTH / K), capped at all col tiles.
//   Fallback to the Set 2 order (one group = all col tiles) when grouping cannot help:
//     USE_B_REUSE == 0, only one row tile (M <= PE_ROWS), or G < 2 (K > BBUF_DEPTH/2).
//   Example, BBUF_DEPTH=1024 (4 KB): K=64 -> G=16, K=128 -> G=8, K=256 -> G=4, K=1024 -> fallback.
//
// Outputs added for the loader (same names as Set 1/2):
//   req_kk, req_reuse_a (col != first col of group), req_reuse_b (row != 0),
//   req_bbuf_addr (col tile index inside group * K + kk), req_last_row
module gemm_control #(parameter PE_ROWS=4, PE_COLS=4, MAX_M=64, MAX_N=128, MAX_K=1024,
                      parameter USE_B_REUSE=1, BBUF_DEPTH=1024)(
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
    localparam IDLE=0, ISSUE=1, RESULT=2, WRITE=3, SETUP=4;
    reg [2:0] state;
    reg [15:0] m,n,k,row,col,kk;
    reg [31:0] a_tile,c_tile,a_ptr,b_ptr;
    reg [31:0] bb_base;                       // (col tile index inside group) * K
    reg [15:0] grp_start, grp_end;            // column range of the current group [start, end)
    reg [15:0] gw;                            // group width in columns = G * PE_COLS
    reg [15:0] g_cnt;                         // SETUP: G being tried
    reg [31:0] g_acc;                         // SETUP: G * K
    wire [15:0] ntiles = (n + PE_COLS - 1) / PE_COLS;   // column tiles (computed once in SETUP)
    genvar g;
    assign req_valid=(state==ISSUE) && !rst;
    assign req_a_addr=a_ptr; assign req_b_addr=b_ptr; assign req_a_stride=k;
    assign req_first=(kk==0); assign req_last=(kk==k-1);
    assign req_kk=kk;
    assign req_reuse_a=(col!=grp_start);
    assign req_reuse_b=(row!=0);
    assign req_bbuf_addr=bb_base+kk;
    assign req_last_row=(row+PE_ROWS>=m);
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
            grp_start<=0;grp_end<=0;gw<=0;g_cnt<=0;g_acc<=0;
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
                        grp_start<=0; g_cnt<=1; g_acc<=cfg_k;
                        busy<=1;state<=SETUP;
                    end
                end
                // ---- choose G: largest G with G*K <= BBUF_DEPTH (at most ntiles, so <= 32 cycles) ----
                SETUP: begin
                    if(g_cnt<ntiles && g_acc+k<=BBUF_DEPTH) begin
                        g_cnt<=g_cnt+1'b1; g_acc<=g_acc+k;
                    end else begin
                        if(USE_B_REUSE==0 || m<=PE_ROWS || g_acc>BBUF_DEPTH || g_cnt<2 || g_cnt>=ntiles) begin
                            gw<=ntiles*PE_COLS;  grp_end<=ntiles*PE_COLS;   // one group = Set 2 order
                        end else begin
                            gw<=g_cnt*PE_COLS;   grp_end<=g_cnt*PE_COLS;
                        end
                        state<=ISSUE;
                    end
                end
                ISSUE: if(req_valid && req_ready) begin
                    if(req_last) state<=RESULT;
                    else begin kk<=kk+1'b1;a_ptr<=a_ptr+1'b1;b_ptr<=b_ptr+n;end
                end
                RESULT: if(result_accepted) state<=WRITE;
                WRITE: if(write_done) begin
                    kk<=0; state<=ISSUE;
                    if(col+PE_COLS<n && col+PE_COLS<grp_end) begin
                        // next column tile inside the group: same A (cache), new B
                        col<=col+PE_COLS; a_ptr<=a_tile;
                        b_ptr<=col+PE_COLS; bb_base<=bb_base+k;
                    end else if(row+PE_ROWS<m) begin
                        // next row tile, back to the first column of the group: B from buffer
                        row<=row+PE_ROWS; col<=grp_start;
                        a_tile<=a_tile+PE_ROWS*k; a_ptr<=a_tile+PE_ROWS*k;
                        c_tile<=c_tile+PE_ROWS*n; b_ptr<=grp_start; bb_base<=0;
                    end else if(grp_end<n) begin
                        // next group: back to row tile 0, refill the B buffer
                        grp_start<=grp_end; grp_end<=grp_end+gw;
                        row<=0; col<=grp_end;
                        a_tile<=0; a_ptr<=0; c_tile<=0; b_ptr<=grp_end; bb_base<=0;
                    end else begin busy<=0;done<=1;state<=IDLE; end
                end
            endcase
        end
    end
endmodule
