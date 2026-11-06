`timescale 1ns/1ps
// =====================================================================
// operand_loader  --  SET 3 loader = Set 2 loader with a 4 KB B reuse buffer (BBUF_DEPTH=1024).
//   Must match BBUF_DEPTH in set3 gemm_control.v.
// (Set 2 text follows)
// operand_loader  --  SET 2 : Phase 1 + 2a + 3, inside operand_loader.v only
// Memory/Dataflow team.  gemm_control.v / gemm_top.v are the baseline files.
//   Phase 1  : A reuse buffer + zero read skip
//   Phase 2a : packet slots (NSLOTS=2 is Ping/Pong; 4 lets packets that need no memory read
//              queue up behind one that does)
//   Phase 3  : on-chip buffer = A reuse + B reuse. B reuse buffer is filled in row tile 0 and
//              read in every later row tile (each B value fetched once per job).
//   Memory contract (INTERFACES.md): at most ONE outstanding request, so the loader sends a
//   request only when the previous response has been taken (no requests in flight).
//
// The loader works out the loop position from the descriptors it already receives
// (works for the baseline loop order and for Set 3's grouped order):
//   kk         = 0 on req_first, +1 afterwards
//   new row    = first descriptor of a tile whose A start address differs from the previous
//                tile's, or the job start (A addr 0 and B addr 0)  -> fill the A reuse buffer
//   col index  = 0 on a new row, +1 for each further tile of the same row
//   row tile 0 = A start address == 0                             -> fill the B reuse buffer
//   B reuse buffer address = col index * K + kk                   (K = req_a_stride)
//
// Block diagram (same names as the slides):
//   Request intake -> slot -> (1-cycle RAM read) -> Prep: buffer data, zero-skip decision, need mask
//   Load control   : one byte request at a time for each bit of the oldest slot's need mask;
//                    {slot, byte} of the outstanding request is kept in one register
//   Response router: writes the returned byte into that slot;
//                    last A byte of a column-tile-0 packet -> A reuse buffer,
//                    last B byte of a row-tile-0 packet    -> B reuse buffer
//   Supply control : op_valid when the oldest slot is complete; MAC handshake frees it
//
// Switches (gemm_top passes only PE_ROWS/PE_COLS, so edit the defaults here):
//   USE_A_REUSE, USE_ZERO_SKIP, USE_B_REUSE : 0/1
//   NSLOTS       : packet slots, power of 2
//   ACACHE_DEPTH : A reuse buffer entries (one per kk); kk >= depth reads A from memory
//   BBUF_DEPTH   : B reuse buffer entries x 4 byte (one per column tile x kk); 4096 = 16 KB =
//                  all of B at K=N=128. Entries beyond the depth read B from memory.
//                  A 1-bit valid RAM marks entries whose B was zero-skipped in row tile 0;
//                  the first later row tile that needs such a B fetches and stores it.
// =====================================================================
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
                        parameter USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_B_REUSE=1,
                        parameter NSLOTS=4,
                        parameter ACACHE_DEPTH=1024, BBUF_DEPTH=1024)(
    input clk,rst,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,
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
    localparam AWID=8*PE_ROWS, BWID=8*PE_COLS;
    localparam AAW=(ACACHE_DEPTH>1)?$clog2(ACACHE_DEPTH):1;
    localparam BAW=(BBUF_DEPTH>1)?$clog2(BBUF_DEPTH):1;
    localparam ZS=(USE_ZERO_SKIP!=0) && (USE_A_REUSE!=0);
    localparam [SB-1:0] SLOT_LAST=NSLOTS-1;

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
    // ---- descriptor tracking: what gemm_control's loop position is, worked out here ----
    //   kk          : 0 on req_first, +1 on each later descriptor of the tile
    //   first column: req_b_addr at k=0 (B address k*N+col)  -> column tile 0 when < PE_COLS
    //   row tile    : req_a_addr at k=0 (A address row*K)    -> row tile 0 when == 0
    reg [15:0] kk_r; reg [31:0] afirst_r; reg [7:0] cidx_r; reg row0_r, col0_r;
    wire        jobstart = (req_a_addr == 0) && (req_b_addr == 0);
    wire        newrow   = jobstart || (req_a_addr != afirst_r);
    wire [15:0] req_kk   = req_first ? 16'd0 : kk_r + 1'b1;
    wire [7:0]  cidx_now = req_first ? (newrow ? 8'd0 : cidx_r + 1'b1) : cidx_r;
    wire        col0_now = req_first ? newrow : col0_r;
    wire        row0_now = req_first ? (req_a_addr == 0) : row0_r;
    wire [31:0] req_bbuf_addr = cidx_now * req_a_stride + req_kk;   // col index * K + kk
    wire hit_a  = (USE_A_REUSE!=0) && !col0_now && (req_kk < ACACHE_DEPTH);
    wire fill_a = (USE_A_REUSE!=0) &&  col0_now && (req_kk < ACACHE_DEPTH);
    wire hit_b  = (USE_B_REUSE!=0) && !row0_now && (req_bbuf_addr < BBUF_DEPTH);
    wire fill_b = (USE_B_REUSE!=0) &&  row0_now && (req_bbuf_addr < BBUF_DEPTH);
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
    // B reuse buffer valid bits (separate 1-bit RAM, own write port): written in row tile 0
    // for every filled entry; 0 when that B was zero-skipped and so never fetched.
    wire bvq;
    wire bv_we;
    wire bv_wd;
    generate if(USE_B_REUSE!=0) begin: g_bbuf
        sdp_ram #(.WIDTH(BWID),.DEPTH(BBUF_DEPTH)) b_buf(.clk(clk),
            .we(bw_pend),.waddr(s_bba[bw_s][BAW-1:0]),.wdata(s_b[bw_s]),
            .re(accept && hit_b),.raddr(req_bbuf_addr[BAW-1:0]),.rdata(bq));
        sdp_ram #(.WIDTH(1),.DEPTH(BBUF_DEPTH)) b_valid(.clk(clk),
            .we(bv_we),.waddr(s_bba[cap_s][BAW-1:0]),.wdata(bv_wd),
            .re(accept && hit_b),.raddr(req_bbuf_addr[BAW-1:0]),.rdata(bvq));
    end else begin: g_no_bbuf
        assign bq={BWID{1'b0}}; assign bvq=1'b0;
    end endgenerate

    // ---------------- prep (data from RAMs, zero-skip decision) ----------------
    wire zf     = cap_hita && acq[AWID];
    wire b_ok   = cap_hitb && bvq;                              // B is in the reuse buffer
    wire skip_b = ZS && zf && !b_ok;                            // A all zero: B not needed
    wire [PE_ROWS-1:0] need_a = cap_hita ? {PE_ROWS{1'b0}} : s_rows[cap_s];
    wire [PE_COLS-1:0] need_b = (b_ok || skip_b) ? {PE_COLS{1'b0}} : s_cols[cap_s];
    // B reuse buffer fill: row tile 0 writes every entry (valid=0 if zero-skipped).
    // A later row that finds an entry invalid and does need B fetches it once and stores it.
    wire refill_b = cap_hitb && !bvq && !skip_b;
    assign bv_we = cap_v && (s_fillb[cap_s] || refill_b);
    assign bv_wd = !skip_b;                                     // data written when it arrives

    // ---------------- load control (issue) ----------------
    reg          busy_q;                              // one request outstanding
    reg [SB-1:0] q_s;                                 // its slot
    reg [IW-1:0] q_i;                                 // its byte position
    wire [NI-1:0] cur   = s_rem[ip];
    wire [IW-1:0] idx   = lowbit(cur);
    wire          isb   = (idx >= PE_ROWS);
    wire [IW-1:0] bcol  = idx - PE_ROWS;
    assign mem_req_valid = !rst && prep[ip] && (cur!=0) && !busy_q;
    assign mem_req_is_b  = isb;
    assign mem_req_addr  = isb ? s_baddr[ip] + bcol : s_aaddr[ip] + idx*stride;
    wire req_fire = mem_req_valid && mem_req_ready;
    wire [NI-1:0] cur_after = cur & ~({{(NI-1){1'b0}},1'b1} << idx);

    // ---------------- response router ----------------
    assign mem_rsp_ready = !rst && busy_q;
    wire rsp_fire = mem_rsp_valid && mem_rsp_ready;
    wire [SB-1:0] rs = q_s;
    wire [IW-1:0] ri = q_i;
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
            kk_r<=0; afirst_r<=0; cidx_r<=0; row0_r<=1'b1; col0_r<=1'b1;
            cap_v<=0; cap_hita<=0; cap_hitb<=0; cap_s<=0;
            aw_pend<=0; bw_pend<=0; aw_s<=0; bw_s<=0;
            busy_q<=0; q_s<=0; q_i<=0;
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
                kk_r<=req_kk; cidx_r<=cidx_now; row0_r<=row0_now; col0_r<=col0_now;
                if(req_first) afirst_r<=req_a_addr;
                cap_s<=wptr; cap_hita<=hit_a; cap_hitb<=hit_b;
                wptr<=nxt(wptr);
            end
            // ---- prep: RAM data arrived, decide what must be read ----
            if(cap_v) begin
                s_a[cap_s]<=cap_hita ? acq[AWID-1:0] : {AWID{1'b0}};
                s_b[cap_s]<=b_ok ? bq : {BWID{1'b0}};
                s_rem[cap_s]<={need_b,need_a};
                s_left[cap_s]<=popc({need_b,need_a});
                s_aleft[cap_s]<=popc({{PE_COLS{1'b0}},need_a});
                s_bleft[cap_s]<=popc({need_b,{PE_ROWS{1'b0}}});
                prep[cap_s]<=1'b1;
                if(refill_b) s_fillb[cap_s]<=1'b1;
            end
            // ---- load control: issue one byte request per cycle ----
            if(req_fire) begin
                s_rem[ip]<=cur_after;
                busy_q<=1'b1; q_s<=ip; q_i<=idx;
                if(cur_after==0) begin idone[ip]<=1'b1; ip<=nxt(ip); end
            end else if(prep[ip] && !idone[ip] && cur==0) begin   // nothing to read for this slot
                idone[ip]<=1'b1; ip<=nxt(ip);
            end
            // ---- response router ----
            if(rsp_fire) begin
                busy_q<=1'b0;
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
