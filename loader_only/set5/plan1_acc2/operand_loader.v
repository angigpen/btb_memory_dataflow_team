`timescale 1ns/1ps
// =====================================================================
// operand_loader  --  SET 4 : A/B reuse buffers + B share across ACC banks
// Memory/Dataflow team. Works with set4/gemm_control.v, which tells the loader the loop
// position directly (side-band ports), so nothing is inferred from addresses here.
//
//   A reuse buffer : ABUF_DEPTH x 32 bit (16 KB), address kk*NBANK + bank (the row block's A).
//   B reuse buffer : BBUF_DEPTH x 32 bit, address (column tile in group)*K + kk.
//   B share        : packets of bank 1.. use the B word of the preceding bank-0 packet
//                    (same kk, same column tile) -> no B read, no B buffer read.
//   Zero read skip : not used in Set 4 (one B packet serves several A tiles, so an all-zero A of
//                    one tile no longer means the B is unnecessary).
//
// Packet slots (NSLOTS, power of 2) and the memory contract are the same as Set 2/3:
// one byte request at a time, response routed into its slot, oldest complete slot is supplied.
// Slots never wait for the result writer, so reads continue while results are being written.
//
// Read-after-write safety: a descriptor that reads a buffer entry waits (req_ready=0) while an
// older slot that fills that buffer is still in flight. This only happens at a column-tile
// (A) or row-block (B) change and only when K is tiny.
// =====================================================================
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
                        parameter NBANK=2, NSLOTS=4,
                        parameter USE_A_REUSE=1, USE_B_REUSE=1,
                        parameter ABUF_DEPTH=4096, BBUF_DEPTH=1024,
                        parameter BKW=(NBANK>1)?$clog2(NBANK):1)(
    input clk,rst,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,
    input [BKW-1:0] req_bank,
    input req_a_hit, req_a_fill, input [31:0] req_abuf_addr,
    input req_b_hit, req_b_fill, req_b_share, input [31:0] req_bbuf_addr,
    output mem_req_valid,input mem_req_ready,
    output mem_req_is_b,output [31:0] mem_req_addr,
    input mem_rsp_valid,output mem_rsp_ready,input [7:0] mem_rsp_data,
    output op_valid,input op_ready,
    output [8*PE_ROWS-1:0] op_a,output [8*PE_COLS-1:0] op_b,
    output [PE_ROWS-1:0] op_rows,output [PE_COLS-1:0] op_cols,
    output op_first,op_last,output [BKW-1:0] op_bank,output loader_busy
);
    localparam NI=PE_ROWS+PE_COLS;
    localparam IW=$clog2(NI);
    localparam SB=(NSLOTS>1)?$clog2(NSLOTS):1;
    localparam AWID=8*PE_ROWS, BWID=8*PE_COLS;
    localparam AAW=(ABUF_DEPTH>1)?$clog2(ABUF_DEPTH):1;
    localparam BAW=(BBUF_DEPTH>1)?$clog2(BBUF_DEPTH):1;
    localparam [SB-1:0] SLOT_LAST=NSLOTS-1;

    function integer popc(input [NI-1:0] v); integer j; begin popc=0; for(j=0;j<NI;j=j+1) popc=popc+v[j]; end endfunction
    function [IW-1:0] lowbit(input [NI-1:0] v); integer j; begin lowbit=0; for(j=NI-1;j>=0;j=j-1) if(v[j]) lowbit=j; end endfunction
    function [SB-1:0] nxt(input [SB-1:0] p); nxt=(p==SLOT_LAST)?{SB{1'b0}}:p+1'b1; endfunction

    // ---------------- packet slots ----------------
    reg [AWID-1:0]    s_a    [0:NSLOTS-1];
    reg [BWID-1:0]    s_b    [0:NSLOTS-1];
    reg [PE_ROWS-1:0] s_rows [0:NSLOTS-1];
    reg [PE_COLS-1:0] s_cols [0:NSLOTS-1];
    reg               s_first[0:NSLOTS-1], s_last[0:NSLOTS-1], s_share[0:NSLOTS-1];
    reg [BKW-1:0]     s_bank [0:NSLOTS-1];
    reg [31:0]        s_aaddr[0:NSLOTS-1], s_baddr[0:NSLOTS-1];
    reg [AAW-1:0]     s_aba  [0:NSLOTS-1];
    reg [BAW-1:0]     s_bba  [0:NSLOTS-1];
    reg [NI-1:0]      s_rem  [0:NSLOTS-1];        // bytes still to REQUEST
    reg [IW:0]        s_left [0:NSLOTS-1];        // bytes still to RECEIVE
    reg [IW:0]        s_aleft[0:NSLOTS-1], s_bleft[0:NSLOTS-1];
    reg [NSLOTS-1:0]  alloc, prep, idone, filla, fillb;
    reg [SB-1:0] wptr, ip, rd;
    reg [15:0] stride;
    reg [BWID-1:0] b_latch;                       // B of the last supplied bank-0 packet

    // ---------------- request intake ----------------
    reg aw_pend, bw_pend; reg [SB-1:0] aw_s, bw_s;
    wire hit_a = (USE_A_REUSE!=0) && req_a_hit;
    wire hit_b = (USE_B_REUSE!=0) && req_b_hit;
    wire a_busy = (|(alloc & filla)) || aw_pend;   // an A buffer fill is still in flight
    wire b_busy = (|(alloc & fillb)) || bw_pend;
    assign req_ready = !alloc[wptr] && !rst && !(hit_a && a_busy) && !(hit_b && b_busy);
    wire accept = req_valid && req_ready;
    reg cap_v, cap_hita, cap_hitb; reg [SB-1:0] cap_s;

    // ---------------- on-chip RAMs ----------------
    wire [AWID-1:0] acq; wire [BWID-1:0] bq;
    generate if(USE_A_REUSE!=0) begin: g_abuf
        sdp_ram #(.WIDTH(AWID),.DEPTH(ABUF_DEPTH)) a_buf(.clk(clk),
            .we(aw_pend),.waddr(s_aba[aw_s]),.wdata(s_a[aw_s]),
            .re(accept && hit_a),.raddr(req_abuf_addr[AAW-1:0]),.rdata(acq));
    end else begin: g_no_abuf
        assign acq={AWID{1'b0}};
    end endgenerate
    generate if(USE_B_REUSE!=0) begin: g_bbuf
        sdp_ram #(.WIDTH(BWID),.DEPTH(BBUF_DEPTH)) b_buf(.clk(clk),
            .we(bw_pend),.waddr(s_bba[bw_s]),.wdata(s_b[bw_s]),
            .re(accept && hit_b),.raddr(req_bbuf_addr[BAW-1:0]),.rdata(bq));
    end else begin: g_no_bbuf
        assign bq={BWID{1'b0}};
    end endgenerate

    // ---------------- prep ----------------
    wire [PE_ROWS-1:0] need_a = cap_hita ? {PE_ROWS{1'b0}} : s_rows[cap_s];
    wire [PE_COLS-1:0] need_b = (cap_hitb || s_share[cap_s]) ? {PE_COLS{1'b0}} : s_cols[cap_s];

    // ---------------- load control (one request outstanding) ----------------
    reg          busy_q;
    reg [SB-1:0] q_s;
    reg [IW-1:0] q_i;
    wire [NI-1:0] cur   = s_rem[ip];
    // B-first priority, with the original four packet slots retained.
    wire [NI-1:0] pending_b = {cur[NI-1:PE_ROWS], {PE_ROWS{1'b0}}};
    wire [IW-1:0] idx = lowbit((|pending_b) ? pending_b : cur);
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
    assign op_a=s_a[rd]; assign op_b=s_share[rd] ? b_latch : s_b[rd];
    assign op_rows=s_rows[rd]; assign op_cols=s_cols[rd];
    assign op_first=s_first[rd]; assign op_last=s_last[rd]; assign op_bank=s_bank[rd];
    assign loader_busy = |alloc;

    integer i;
    always @(posedge clk) begin
        if(rst) begin
            alloc<=0; prep<=0; idone<=0; filla<=0; fillb<=0; wptr<=0; ip<=0; rd<=0; stride<=0;
            cap_v<=0; cap_hita<=0; cap_hitb<=0; cap_s<=0; b_latch<=0;
            aw_pend<=0; bw_pend<=0; aw_s<=0; bw_s<=0;
            busy_q<=0; q_s<=0; q_i<=0;
            for(i=0;i<NSLOTS;i=i+1) begin
                s_a[i]<=0; s_b[i]<=0; s_rows[i]<=0; s_cols[i]<=0; s_first[i]<=0; s_last[i]<=0;
                s_share[i]<=0; s_bank[i]<=0;
                s_rem[i]<=0; s_left[i]<=0; s_aleft[i]<=0; s_bleft[i]<=0;
            end
        end else begin
            aw_pend<=0; bw_pend<=0;
            // ---- intake ----
            cap_v<=accept;
            if(accept) begin
                alloc[wptr]<=1'b1;
                s_rows[wptr]<=req_rows; s_cols[wptr]<=req_cols;
                s_first[wptr]<=req_first; s_last[wptr]<=req_last;
                s_share[wptr]<=req_b_share; s_bank[wptr]<=req_bank;
                s_aaddr[wptr]<=req_a_addr; s_baddr[wptr]<=req_b_addr; stride<=req_a_stride;
                s_aba[wptr]<=req_abuf_addr[AAW-1:0]; s_bba[wptr]<=req_bbuf_addr[BAW-1:0];
                filla[wptr]<=(USE_A_REUSE!=0) && req_a_fill;
                fillb[wptr]<=(USE_B_REUSE!=0) && req_b_fill;
                cap_s<=wptr; cap_hita<=hit_a; cap_hitb<=hit_b;
                wptr<=nxt(wptr);
            end
            // ---- prep: buffer data arrived, decide what must be read ----
            if(cap_v) begin
                s_a[cap_s]<=cap_hita ? acq : {AWID{1'b0}};
                s_b[cap_s]<=cap_hitb ? bq  : {BWID{1'b0}};
                s_rem[cap_s]<={need_b,need_a};
                s_left[cap_s]<=popc({need_b,need_a});
                s_aleft[cap_s]<=popc({{PE_COLS{1'b0}},need_a});
                s_bleft[cap_s]<=popc({need_b,{PE_ROWS{1'b0}}});
                prep[cap_s]<=1'b1;
            end
            // ---- load control ----
            if(req_fire) begin
                s_rem[ip]<=cur_after;
                busy_q<=1'b1; q_s<=ip; q_i<=idx;
                if(cur_after==0) begin idone[ip]<=1'b1; ip<=nxt(ip); end
            end else if(prep[ip] && !idone[ip] && cur==0) begin
                idone[ip]<=1'b1; ip<=nxt(ip);
            end
            // ---- response router: last byte of a fill packet -> buffer write next cycle ----
            if(rsp_fire) begin
                busy_q<=1'b0;
                s_left[rs]<=s_left[rs]-1'b1;
                if(ri<PE_ROWS) begin
                    s_a[rs][8*ri +: 8]<=mem_rsp_data;
                    s_aleft[rs]<=s_aleft[rs]-1'b1;
                    if(s_aleft[rs]==1 && filla[rs]) begin aw_pend<=1'b1; aw_s<=rs; end
                end else begin
                    s_b[rs][8*rcol +: 8]<=mem_rsp_data;
                    s_bleft[rs]<=s_bleft[rs]-1'b1;
                    if(s_bleft[rs]==1 && fillb[rs]) begin bw_pend<=1'b1; bw_s<=rs; end
                end
            end
            // ---- supply ----
            if(op_valid && op_ready) begin
                if(!s_share[rd]) b_latch<=s_b[rd];
                alloc[rd]<=1'b0; prep[rd]<=1'b0; idone[rd]<=1'b0; filla[rd]<=1'b0; fillb[rd]<=1'b0;
                rd<=nxt(rd);
            end
        end
    end
endmodule

// Simple dual-port RAM: one write port, one read port, 1-cycle read latency (BRAM/LUTRAM pattern).
module sdp_ram #(parameter WIDTH=32, DEPTH=1024,
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
