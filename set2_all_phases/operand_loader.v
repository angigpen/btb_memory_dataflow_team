`timescale 1ns/1ps
// =====================================================================
// operand_loader  --  SET 2 : all phases
//   Phase 1  : A reuse (A cache) + zero read skip
//   Phase 2a : packet slots (NSLOTS=2 is Ping/Pong double buffering)
//   Phase 2b : up to MAX_OUT memory requests in flight (responses must come back in order)
//   Phase 3  : on-chip buffer = A reuse + B reuse (B buffer filled in row tile 0,
//              read in every later row tile)
// Memory/Dataflow team
//
// Block diagram (same names as the slides):
//   Request intake --> [slot i meta]  --(1-cycle RAM read)--> Prep (A cache / B buffer data,
//                                                               zero-skip decision, "need" mask)
//   Load control   : walks slots in order, issues 1 byte request per cycle for every bit
//                    of the slot's need mask, pushes {slot, byte index} into the tag FIFO
//   Response router: pops the tag FIFO for every response and writes the byte into its slot;
//                    the last A byte of a col-tile-0 packet writes the A cache,
//                    the last B byte of a row-tile-0 packet writes the B buffer
//   Supply control : op_valid when the oldest slot has all its bytes; MAC handshake frees it
//
// Switches (parameters):
//   USE_A_REUSE, USE_ZERO_SKIP, USE_B_REUSE : 0/1
//   NSLOTS  : packet slots, power of 2 (2 = Ping/Pong, 4 recommended with MAX_OUT > 8)
//   MAX_OUT : max requests in flight (1 = baseline-like memory use). Little's law: ~latency+1.
//   ACACHE_DEPTH : A cache entries (one per kk);             kk >= depth  -> A read from memory
//   BBUF_DEPTH   : B buffer entries (one per col tile x kk); addr >= depth -> B read from memory
//                  default 4096 x 32 bit = 16 KB = all of B for K=128, N=128
//   All switches 0 with NSLOTS=2, MAX_OUT=1 -> same reads as the baseline.
// =====================================================================
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
                        parameter USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=1,
                        parameter NSLOTS=4, MAX_OUT=32,
                        parameter ACACHE_DEPTH=1024, BBUF_DEPTH=4096)(
    input clk,rst,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,
    input [15:0] req_kk,              // NEW (from gemm_control)
    input req_reuse_a,                // NEW: col tile != 0
    input req_reuse_b,                // NEW: row tile != 0
    input [31:0] req_bbuf_addr,       // NEW: col tile index * K + kk
    input req_last_row,               // NEW: no later row tile -> don't fill the B buffer
    output mem_req_valid,input mem_req_ready,
    output mem_req_is_b,output [31:0] mem_req_addr,
    input mem_rsp_valid,output mem_rsp_ready,input [7:0] mem_rsp_data,
    output op_valid,input op_ready,
    output [8*PE_ROWS-1:0] op_a,output [8*PE_COLS-1:0] op_b,
    output [PE_ROWS-1:0] op_rows,output [PE_COLS-1:0] op_cols,
    output op_first,op_last,output loader_busy
);
    localparam NI=PE_ROWS+PE_COLS;                    // byte positions per packet (A rows, then B cols)
    localparam IW=$clog2(NI);
    localparam SB=(NSLOTS>1)?$clog2(NSLOTS):1;
    localparam TB=(MAX_OUT>1)?$clog2(MAX_OUT):1;
    localparam AWID=8*PE_ROWS, BWID=8*PE_COLS;
    localparam AAW=(ACACHE_DEPTH>1)?$clog2(ACACHE_DEPTH):1;
    localparam BAW=(BBUF_DEPTH>1)?$clog2(BBUF_DEPTH):1;
    localparam ZS=(USE_ZERO_SKIP!=0) && (USE_A_REUSE!=0);
    localparam [SB-1:0] SLOT_LAST=NSLOTS-1;
    localparam [TB-1:0] TAG_LAST=MAX_OUT-1;

    function integer popc(input [NI-1:0] v); integer j; begin popc=0; for(j=0;j<NI;j=j+1) popc=popc+v[j]; end endfunction
    function [IW-1:0] lowbit(input [NI-1:0] v); integer j; begin lowbit=0; for(j=NI-1;j>=0;j=j-1) if(v[j]) lowbit=j; end endfunction
    function [SB-1:0] nxt(input [SB-1:0] p); nxt=(p==SLOT_LAST)?{SB{1'b0}}:p+1'b1; endfunction

    // ---------------- packet slots ----------------
    reg [AWID-1:0]    s_a    [0:NSLOTS-1];
    reg [BWID-1:0]    s_b    [0:NSLOTS-1];
    reg [PE_ROWS-1:0] s_rows [0:NSLOTS-1];
    reg [PE_COLS-1:0] s_cols [0:NSLOTS-1];
    reg               s_first[0:NSLOTS-1], s_last[0:NSLOTS-1];
    reg [31:0]        s_aaddr[0:NSLOTS-1], s_baddr[0:NSLOTS-1];
    reg [15:0]        s_kk   [0:NSLOTS-1];
    reg [31:0]        s_bba  [0:NSLOTS-1];
    reg               s_filla[0:NSLOTS-1], s_fillb[0:NSLOTS-1];
    reg [NI-1:0]      s_rem  [0:NSLOTS-1];        // bytes still to REQUEST
    reg [IW:0]        s_left [0:NSLOTS-1];        // bytes still to RECEIVE
    reg [IW:0]        s_aleft[0:NSLOTS-1], s_bleft[0:NSLOTS-1];
    reg [NSLOTS-1:0]  alloc, prep, idone;         // slot in use / prep done / all requests issued
    reg [SB-1:0] wptr, ip, rd;                    // intake, issue, supply pointers
    reg [15:0] stride;

    // ---------------- request intake ----------------
    wire accept = req_valid && req_ready;
    wire hit_a  = (USE_A_REUSE!=0) &&  req_reuse_a && (req_kk < ACACHE_DEPTH);
    wire fill_a = (USE_A_REUSE!=0) && !req_reuse_a && (req_kk < ACACHE_DEPTH);
    wire hit_b  = (USE_B_REUSE!=0) &&  req_reuse_b && (req_bbuf_addr < BBUF_DEPTH);
    wire fill_b = (USE_B_REUSE!=0) && !req_reuse_b && !req_last_row && (req_bbuf_addr < BBUF_DEPTH);
    assign req_ready = !alloc[wptr] && !rst;
    reg cap_v, cap_hita, cap_hitb; reg [SB-1:0] cap_s;   // prep happens 1 cycle after intake

    // ---------------- on-chip RAMs ----------------
    reg aw_pend, bw_pend; reg [SB-1:0] aw_s, bw_s;
    wire [AWID:0] acq; wire [BWID-1:0] bq;
    generate if(USE_A_REUSE!=0) begin: g_acache
        wire [AWID-1:0] w = s_a[aw_s];
        sdp_ram #(.WIDTH(AWID+1),.DEPTH(ACACHE_DEPTH)) a_cache(.clk(clk),
            .we(aw_pend),.waddr(s_kk[aw_s][AAW-1:0]),.wdata({(w=={AWID{1'b0}}),w}),
            .re(accept && hit_a),.raddr(req_kk[AAW-1:0]),.rdata(acq));
    end else begin: g_no_acache
        assign acq={(AWID+1){1'b0}};
    end endgenerate
    generate if(USE_B_REUSE!=0) begin: g_bbuf
        sdp_ram #(.WIDTH(BWID),.DEPTH(BBUF_DEPTH)) b_buf(.clk(clk),
            .we(bw_pend),.waddr(s_bba[bw_s][BAW-1:0]),.wdata(s_b[bw_s]),
            .re(accept && hit_b),.raddr(req_bbuf_addr[BAW-1:0]),.rdata(bq));
    end else begin: g_no_bbuf
        assign bq={BWID{1'b0}};
    end endgenerate

    // ---------------- prep (data from RAMs, zero-skip decision) ----------------
    wire zf     = cap_hita && acq[AWID];
    wire skip_b = ZS && zf && !cap_hitb && !s_fillb[cap_s];   // never skip while filling the B buffer
    wire [PE_ROWS-1:0] need_a = cap_hita ? {PE_ROWS{1'b0}} : s_rows[cap_s];
    wire [PE_COLS-1:0] need_b = (cap_hitb || skip_b) ? {PE_COLS{1'b0}} : s_cols[cap_s];

    // ---------------- load control (issue) ----------------
    reg [TB:0] out_cnt;
    reg [SB-1:0] tag_s [0:MAX_OUT-1];
    reg [IW-1:0] tag_i [0:MAX_OUT-1];
    reg [TB-1:0] th, tt;
    wire [NI-1:0] cur   = s_rem[ip];
    wire [IW-1:0] idx   = lowbit(cur);
    wire          isb   = (idx >= PE_ROWS);
    wire [IW-1:0] bcol  = idx - PE_ROWS;
    assign mem_req_valid = !rst && prep[ip] && (cur!=0) && (out_cnt < MAX_OUT);
    assign mem_req_is_b  = isb;
    assign mem_req_addr  = isb ? s_baddr[ip] + bcol : s_aaddr[ip] + idx*stride;
    wire req_fire = mem_req_valid && mem_req_ready;
    wire [NI-1:0] cur_after = cur & ~({{(NI-1){1'b0}},1'b1} << idx);

    // ---------------- response router ----------------
    assign mem_rsp_ready = !rst && (out_cnt!=0);
    wire rsp_fire = mem_rsp_valid && mem_rsp_ready;
    wire [SB-1:0] rs = tag_s[th];
    wire [IW-1:0] ri = tag_i[th];
    wire [IW-1:0] rcol = ri - PE_ROWS;

    // ---------------- supply control ----------------
    assign op_valid = !rst && alloc[rd] && prep[rd] && (s_left[rd]==0);
    assign op_a=s_a[rd]; assign op_b=s_b[rd]; assign op_rows=s_rows[rd]; assign op_cols=s_cols[rd];
    assign op_first=s_first[rd]; assign op_last=s_last[rd];
    assign loader_busy = |alloc;

    integer i;
    always @(posedge clk) begin
        if(rst) begin
            alloc<=0; prep<=0; idone<=0; wptr<=0; ip<=0; rd<=0; stride<=0;
            cap_v<=0; cap_hita<=0; cap_hitb<=0; cap_s<=0;
            aw_pend<=0; bw_pend<=0; aw_s<=0; bw_s<=0;
            out_cnt<=0; th<=0; tt<=0;
            for(i=0;i<NSLOTS;i=i+1) begin
                s_a[i]<=0; s_b[i]<=0; s_rows[i]<=0; s_cols[i]<=0; s_first[i]<=0; s_last[i]<=0;
                s_rem[i]<=0; s_left[i]<=0; s_aleft[i]<=0; s_bleft[i]<=0;
                s_filla[i]<=0; s_fillb[i]<=0;
            end
        end else begin
            aw_pend<=0; bw_pend<=0;
            // ---- intake: claim slot wptr ----
            cap_v<=accept;
            if(accept) begin
                alloc[wptr]<=1'b1;
                s_rows[wptr]<=req_rows; s_cols[wptr]<=req_cols;
                s_first[wptr]<=req_first; s_last[wptr]<=req_last;
                s_aaddr[wptr]<=req_a_addr; s_baddr[wptr]<=req_b_addr; stride<=req_a_stride;
                s_kk[wptr]<=req_kk; s_bba[wptr]<=req_bbuf_addr;
                s_filla[wptr]<=fill_a; s_fillb[wptr]<=fill_b;
                cap_s<=wptr; cap_hita<=hit_a; cap_hitb<=hit_b;
                wptr<=nxt(wptr);
            end
            // ---- prep: RAM data arrived, decide what must be read ----
            if(cap_v) begin
                s_a[cap_s]<=cap_hita ? acq[AWID-1:0] : {AWID{1'b0}};
                s_b[cap_s]<=cap_hitb ? bq : {BWID{1'b0}};
                s_rem[cap_s]<={need_b,need_a};
                s_left[cap_s]<=popc({need_b,need_a});
                s_aleft[cap_s]<=popc({{PE_COLS{1'b0}},need_a});
                s_bleft[cap_s]<=popc({need_b,{PE_ROWS{1'b0}}});
                prep[cap_s]<=1'b1;
            end
            // ---- load control: issue one byte request per cycle ----
            if(req_fire) begin
                s_rem[ip]<=cur_after;
                tag_s[tt]<=ip; tag_i[tt]<=idx;
                tt<=(tt==TAG_LAST)?{TB{1'b0}}:tt+1'b1;
                if(cur_after==0) begin idone[ip]<=1'b1; ip<=nxt(ip); end
            end else if(prep[ip] && !idone[ip] && cur==0) begin   // nothing to read for this slot
                idone[ip]<=1'b1; ip<=nxt(ip);
            end
            // ---- response router ----
            if(rsp_fire) begin
                th<=(th==TAG_LAST)?{TB{1'b0}}:th+1'b1;
                s_left[rs]<=s_left[rs]-1'b1;
                if(ri<PE_ROWS) begin
                    s_a[rs][8*ri +: 8]<=mem_rsp_data;
                    s_aleft[rs]<=s_aleft[rs]-1'b1;
                    if(s_aleft[rs]==1 && s_filla[rs]) begin aw_pend<=1'b1; aw_s<=rs; end
                end else begin
                    s_b[rs][8*rcol +: 8]<=mem_rsp_data;
                    s_bleft[rs]<=s_bleft[rs]-1'b1;
                    if(s_bleft[rs]==1 && s_fillb[rs]) begin bw_pend<=1'b1; bw_s<=rs; end
                end
            end
            out_cnt<=out_cnt+req_fire-rsp_fire;
            // ---- supply: MAC took the oldest packet ----
            if(op_valid && op_ready) begin
                alloc[rd]<=1'b0; prep[rd]<=1'b0; idone[rd]<=1'b0; rd<=nxt(rd);
            end
        end
    end
endmodule

// Simple dual-port RAM: one write port, one read port, 1-cycle read latency.
// Kept inside this file because run.py compiles a fixed list of six .v files.
// Pattern Vivado infers as BRAM (large) or LUTRAM (small): no reset, registered read.
module sdp_ram #(parameter WIDTH=33, DEPTH=1024,
                 parameter AW=(DEPTH>1)?$clog2(DEPTH):1)(
    input clk,
    input we, input [AW-1:0] waddr, input [WIDTH-1:0] wdata,
    input re, input [AW-1:0] raddr, output reg [WIDTH-1:0] rdata
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    always @(posedge clk) begin
        if(we) mem[waddr]<=wdata;
        if(re) rdata<=mem[raddr];
    end
endmodule
