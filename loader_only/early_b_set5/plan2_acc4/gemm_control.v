`timescale 1ns/1ps
// Tile scheduler for C[M,N] = A[M,K] * B[K,N], row-major storage.
// SET 4 (Memory/Dataflow, outside the loader-only rule): Set 3 + ACC banks (output blocking).
//
// Loop order:  column group -> row block -> column tile -> kk -> bank
//   row block  = NBANK row tiles (16 rows for NBANK=4). Each row tile of the block has its own
//                accumulator bank in mac_array, so the block's tiles are computed together.
//   bank loop  = innermost: for one kk the same B packet (4 bytes) is used by every bank.
//                Only bank 0 fetches B (memory or B reuse buffer); banks 1.. reuse it (B share).
//   column group (as Set 3): G column tiles whose B (K x 4G bytes) fits the B reuse buffer.
//                Row block 0 of the group fills the buffer, later row blocks read it.
//   A reuse buffer holds the row block's A (NBANK x K entries): filled in the group's first
//                column tile, read in the other column tiles.
//   Reads: A = M*K*(number of groups), B = K*N  (same as Set 3).
//
// Unlike Set 1-3 the scheduler does NOT wait for each tile's write: descriptors keep flowing and
// the C base address of every tile is queued (result FIFO) when its last descriptor is accepted.
// Results leave mac_array in that same order, so the FIFO head is the base of the next result.
//
// The loader no longer guesses the loop position; this module tells it directly:
//   req_bank, A buffer hit/fill + address, B buffer hit/fill + address, B share.
module gemm_control #(parameter PE_ROWS=4, PE_COLS=4, MAX_M=64, MAX_N=128, MAX_K=1024,
                      parameter NBANK=4,                 // accumulator banks = row tiles per block
                      parameter USE_A_REUSE=1, USE_B_REUSE=1, USE_B_SHARE=1,
                      parameter ABUF_DEPTH=4096, BBUF_DEPTH=1024,
                      parameter BKW=(NBANK>1)?$clog2(NBANK):1)(
    input clk, rst, start,
    input [15:0] cfg_m, cfg_n, cfg_k,
    output reg busy, done, error,
    output req_valid, input req_ready,
    output [31:0] req_a_addr, req_b_addr,
    output [15:0] req_a_stride,
    output [PE_ROWS-1:0] req_rows, output [PE_COLS-1:0] req_cols,
    output req_first, req_last,
    // Set 4 side-band to the loader
    output [BKW-1:0] req_bank,
    output req_a_hit, req_a_fill, output [31:0] req_abuf_addr,
    output req_b_hit, req_b_fill, req_b_share, output [31:0] req_bbuf_addr,
    output result_enable, output [31:0] result_base,
    output [15:0] result_stride,
    input result_accepted, write_done
);
    localparam IDLE=0, ISSUE=1, DRAIN=2, SETUP=4;
    localparam FD=8;                                   // result FIFO depth
    localparam [15:0] BLK=PE_ROWS*NBANK;               // rows per row block
    reg [2:0] state;
    reg [15:0] m,n,k,rb,col,kk;
    reg [BKW-1:0] bank, nb_m1;                         // current bank, (banks in this block) - 1
    reg [31:0] a_row, a_kk, a_boff;                    // A: row-block base, +kk, +bank offset
    reg [31:0] c_row, c_boff;                          // C: row-block base, +bank offset
    reg [31:0] b_ptr, bb_col, bb_ptr;                  // B memory address, B buffer col base / addr
    reg [15:0] grp_start, grp_end, gw, g_cnt;
    reg [31:0] g_acc;
    wire [15:0] ntiles = (n + PE_COLS - 1) / PE_COLS;
    // banks in the row block that starts at row r
    function [BKW-1:0] nbm1(input [15:0] mm, input [15:0] r);
        reg [15:0] t; begin
            t = (mm - r + PE_ROWS - 1) / PE_ROWS;
            nbm1 = (t >= NBANK) ? NBANK-1 : t-1;
        end
    endfunction

    // ---- descriptor ----
    wire [15:0] row = rb + bank*PE_ROWS;               // first row of this bank's tile (viewer reads it)
    genvar g;
    assign req_a_addr = a_kk + a_boff;
    assign req_b_addr = b_ptr;
    assign req_a_stride = k;
    assign req_first = (kk==0);
    assign req_last  = (kk==k-1);
    generate for(g=0;g<PE_ROWS;g=g+1) begin: rows_mask assign req_rows[g]=(row+g<m); end
             for(g=0;g<PE_COLS;g=g+1) begin: cols_mask assign req_cols[g]=(col+g<n);   end endgenerate
    assign req_bank = bank;
    wire a_fits = (kk < ABUF_DEPTH/NBANK);
    assign req_a_fill   = (USE_A_REUSE!=0) && a_fits && (col==grp_start);
    assign req_a_hit    = (USE_A_REUSE!=0) && a_fits && (col!=grp_start);
    assign req_abuf_addr= kk*NBANK + bank;
    wire b_fits  = (bb_ptr < BBUF_DEPTH);
    wire b_owner = (USE_B_SHARE==0) || (bank==0);      // this packet fetches B itself
    assign req_b_share  = !b_owner;
    assign req_b_fill   = (USE_B_REUSE!=0) && b_fits && (rb==0) && (bank==0);
    assign req_b_hit    = (USE_B_REUSE!=0) && b_fits && (rb!=0) && b_owner;
    assign req_bbuf_addr= bb_ptr;

    // ---- result FIFO: C base of each tile, pushed with its last descriptor ----
    reg [31:0] fifo [0:FD-1];
    reg [$clog2(FD)-1:0] wp, rp;
    reg [$clog2(FD):0] cnt;
    reg [7:0] wr_out;                                   // results accepted by the writer, not yet written
    wire full = (cnt == FD);
    assign req_valid = (state==ISSUE) && !rst && !(req_last && full);
    wire fire = req_valid && req_ready;
    wire push = fire && req_last;
    wire pop  = result_accepted;
    assign result_enable = (cnt != 0) && !rst;
    assign result_base   = fifo[rp];
    assign result_stride = n;

    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE; busy<=0; done<=0; error<=0;
            m<=0;n<=0;k<=0;rb<=0;col<=0;kk<=0;bank<=0;nb_m1<=0;
            a_row<=0;a_kk<=0;a_boff<=0;c_row<=0;c_boff<=0;b_ptr<=0;bb_col<=0;bb_ptr<=0;
            grp_start<=0;grp_end<=0;gw<=0;g_cnt<=0;g_acc<=0;
            wp<=0; rp<=0; cnt<=0; wr_out<=0;
        end else begin
            done<=0;
            // result FIFO / write bookkeeping
            if(push) begin fifo[wp]<=c_row+c_boff+col; wp<=wp+1'b1; end
            if(pop) rp<=rp+1'b1;
            cnt <= cnt + push - pop;
            wr_out <= wr_out + pop - write_done;
            case(state)
                IDLE: if(start) begin
                    error<=0;
                    if(cfg_m==0 || cfg_n==0 || cfg_k==0 ||
                       cfg_m>MAX_M || cfg_n>MAX_N || cfg_k>MAX_K) begin
                        error<=1;done<=1;
                    end else begin
                        m<=cfg_m;n<=cfg_n;k<=cfg_k;rb<=0;col<=0;kk<=0;bank<=0;
                        nb_m1<=nbm1(cfg_m,16'd0);
                        a_row<=0;a_kk<=0;a_boff<=0;c_row<=0;c_boff<=0;b_ptr<=0;bb_col<=0;bb_ptr<=0;
                        grp_start<=0; g_cnt<=1; g_acc<=cfg_k;
                        busy<=1;state<=SETUP;
                    end
                end
                // choose G (as Set 3): largest G with G*K <= BBUF_DEPTH, one group if it cannot help
                SETUP: begin
                    if(g_cnt<ntiles && g_acc+k<=BBUF_DEPTH) begin
                        g_cnt<=g_cnt+1'b1; g_acc<=g_acc+k;
                    end else begin
                        if(USE_B_REUSE==0 || m<=BLK || g_acc>BBUF_DEPTH || g_cnt<2 || g_cnt>=ntiles) begin
                            gw<=ntiles*PE_COLS;  grp_end<=ntiles*PE_COLS;
                        end else begin
                            gw<=g_cnt*PE_COLS;   grp_end<=g_cnt*PE_COLS;
                        end
                        state<=ISSUE;
                    end
                end
                ISSUE: if(fire) begin
                    if(bank != nb_m1) begin                        // next bank, same kk and B
                        bank<=bank+1'b1; a_boff<=a_boff+PE_ROWS*k; c_boff<=c_boff+PE_ROWS*n;
                    end else begin
                        bank<=0; a_boff<=0; c_boff<=0;
                        if(!req_last) begin                        // next kk
                            kk<=kk+1'b1; a_kk<=a_kk+1'b1; b_ptr<=b_ptr+n; bb_ptr<=bb_ptr+1'b1;
                        end else begin
                            kk<=0;
                            if(col+PE_COLS<n && col+PE_COLS<grp_end) begin
                                // next column tile of the group: same A (buffer)
                                col<=col+PE_COLS; a_kk<=a_row; b_ptr<=col+PE_COLS;
                                bb_col<=bb_col+k; bb_ptr<=bb_col+k;
                            end else if(rb+BLK<m) begin
                                // next row block: back to the group's first column, B from buffer
                                rb<=rb+BLK; nb_m1<=nbm1(m,rb+BLK); col<=grp_start;
                                a_row<=a_row+BLK*k; a_kk<=a_row+BLK*k; c_row<=c_row+BLK*n;
                                b_ptr<=grp_start; bb_col<=0; bb_ptr<=0;
                            end else if(grp_end<n) begin
                                // next group: row block 0 again, refill the B buffer
                                grp_start<=grp_end; grp_end<=grp_end+gw;
                                rb<=0; nb_m1<=nbm1(m,16'd0); col<=grp_end;
                                a_row<=0; a_kk<=0; c_row<=0; b_ptr<=grp_end; bb_col<=0; bb_ptr<=0;
                            end else state<=DRAIN;
                        end
                    end
                end
                // all descriptors issued: wait until every tile is written
                DRAIN: if(cnt==0 && wr_out==0 && !push && !pop) begin busy<=0; done<=1; state<=IDLE; end
            endcase
        end
    end
endmodule
