`timescale 1ns/1ps
// =====================================================================
// operand_loader  --  SET 1 : Phase 1 (A reuse + zero read skip) + Phase 2a (Ping/Pong)
// Memory/Dataflow team.  Only this file differs from the baseline (README: our file).
//
// Memory contract (INTERFACES.md): 1 byte per request, at most ONE outstanding request.
//
// The loader works out everything from the descriptors gemm_control already sends:
//   kk          = 0 on req_first, +1 on every later descriptor of the tile
//   column tile = req_b_addr of the tile's first descriptor (B addr at k=0 = first column)
//   -> column tile 0 : read A from memory and store it in the A reuse buffer
//      column tile >0: same row tile, same A  -> read A from the A reuse buffer
//   (gemm_control finishes every tile before the next starts, so all buffer writes of
//    column tile 0 are done before column tile 1 begins.)
//
// Switches (parameters; gemm_top passes only PE_ROWS/PE_COLS, so edit the defaults here):
//   USE_A_REUSE   A reuse buffer: K entries x (4 A bytes + zero flag)          (Phase 1a)
//   USE_ZERO_SKIP if the buffered A word is all zero, skip the 4 B reads; the packet still
//                 goes to the MAC with B=0 so op_first/op_last stay intact     (Phase 1b)
//   USE_DBUF      two packet slots (Ping/Pong): fill one while the other waits for the MAC (2a)
//   ACACHE_DEPTH  A reuse buffer entries; kk >= depth reads A from memory (any K works)
//   All three switches 0 -> identical to the baseline loader (cycles and reads).
// =====================================================================
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
                        parameter USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=1,
                        parameter ACACHE_DEPTH=1024)(
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
    localparam IDLE=0,A_CRD=1,A_REQ=2,A_RSP=3,B_REQ=4,B_RSP=5;
    localparam RB=(PE_ROWS>1)?$clog2(PE_ROWS):1, CB=(PE_COLS>1)?$clog2(PE_COLS):1;
    localparam NS=(USE_DBUF!=0)?2:1;                 // packet slots
    localparam AW=(ACACHE_DEPTH>1)?$clog2(ACACHE_DEPTH):1;
    localparam AWID=8*PE_ROWS;
    localparam ZS=(USE_ZERO_SKIP!=0) && (USE_A_REUSE!=0);
    localparam [0:0] FLIP=(USE_DBUF!=0);

    // ---------------- packet slots (Ping / Pong) ----------------
    reg [8*PE_ROWS-1:0] s_a    [0:NS-1];
    reg [8*PE_COLS-1:0] s_b    [0:NS-1];
    reg [PE_ROWS-1:0]   s_rows [0:NS-1];
    reg [PE_COLS-1:0]   s_cols [0:NS-1];
    reg                 s_first[0:NS-1];
    reg                 s_last [0:NS-1];
    reg [1:0] full;
    reg wr,rd;

    // ---------------- load FSM ----------------
    reg [2:0] state;
    reg [31:0] a_addr,b_addr;
    reg [15:0] stride;
    reg [RB-1:0] ar;
    reg [CB-1:0] bc;
    reg [15:0] kk_w;                                 // kk of the packet being filled
    reg fill_a;

    // ---------------- descriptor tracking (replaces signals from gemm_control) ----------------
    reg [15:0] kk_r;                                 // kk of the last accepted descriptor
    reg        col0_r;                               // current tile is column tile 0
    wire accept   = req_valid && req_ready;
    wire [15:0] kk_now = req_first ? 16'd0 : kk_r + 1'b1;
    wire        col0_now = req_first ? (req_b_addr < PE_COLS) : col0_r;
    wire kk_fits  = (kk_now < ACACHE_DEPTH);
    wire hit_a    = (USE_A_REUSE!=0) && !col0_now && kk_fits;   // A from the reuse buffer
    wire do_fill  = (USE_A_REUSE!=0) &&  col0_now && kk_fits;   // A from memory, store it

    // ---------------- A reuse buffer (RAM, 1-cycle read) ----------------
    reg  aw_pend;
    reg  [15:0] aw_kk;
    wire [AWID:0] cache_q;                           // {zero_flag, A word}
    wire [AWID-1:0] aw_word = s_a[wr];
    generate if(USE_A_REUSE!=0) begin: g_acache
        sdp_ram #(.WIDTH(AWID+1),.DEPTH(ACACHE_DEPTH)) a_cache(
            .clk(clk),
            .we(aw_pend),.waddr(aw_kk[AW-1:0]),.wdata({(aw_word=={AWID{1'b0}}),aw_word}),
            .re(accept && hit_a),.raddr(kk_now[AW-1:0]),.rdata(cache_q));
    end else begin: g_no_acache
        assign cache_q={(AWID+1){1'b0}};
    end endgenerate

    // ---------------- handshakes ----------------
    assign req_ready=(state==IDLE) && !full[wr] && !rst;
    assign mem_req_valid=!rst && ((state==A_REQ && s_rows[wr][ar]) || (state==B_REQ && s_cols[wr][bc]));
    assign mem_req_is_b=(state==B_REQ);
    assign mem_req_addr=(state==A_REQ)?a_addr:b_addr;
    assign mem_rsp_ready=!rst && (state==A_RSP || state==B_RSP);
    assign op_valid=full[rd] && !rst;
    assign op_a=s_a[rd];   assign op_b=s_b[rd];
    assign op_rows=s_rows[rd]; assign op_cols=s_cols[rd];
    assign op_first=s_first[rd]; assign op_last=s_last[rd];
    assign loader_busy=(state!=IDLE) || (|full);

    integer i;
    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE;ar<=0;bc<=0;a_addr<=0;b_addr<=0;stride<=0;kk_w<=0;fill_a<=0;
            kk_r<=0;col0_r<=1'b1;
            full<=0;wr<=0;rd<=0;aw_pend<=0;aw_kk<=0;
            for(i=0;i<NS;i=i+1) begin
                s_a[i]<=0;s_b[i]<=0;s_rows[i]<=0;s_cols[i]<=0;s_first[i]<=0;s_last[i]<=0;
            end
        end else begin
            aw_pend<=0;
            if(op_valid && op_ready) begin full[rd]<=1'b0; rd<=rd^FLIP; end
            case(state)
                IDLE: if(accept) begin
                    kk_r<=kk_now; col0_r<=col0_now; kk_w<=kk_now; fill_a<=do_fill;
                    a_addr<=req_a_addr;b_addr<=req_b_addr;stride<=req_a_stride;
                    s_rows[wr]<=req_rows;s_cols[wr]<=req_cols;s_first[wr]<=req_first;s_last[wr]<=req_last;
                    s_a[wr]<=0;s_b[wr]<=0;ar<=0;bc<=0;
                    state<=hit_a?A_CRD:A_REQ;
                end
                A_CRD: begin                                   // buffer data arrives this cycle
                    s_a[wr]<=cache_q[AWID-1:0];
                    if(ZS && cache_q[AWID]) begin              // all-zero A: no B reads
                        full[wr]<=1'b1; wr<=wr^FLIP; state<=IDLE;
                    end else state<=B_REQ;
                end
                A_REQ: if(!s_rows[wr][ar]) begin
                    if(ar==PE_ROWS-1) begin state<=B_REQ; aw_pend<=fill_a; aw_kk<=kk_w; end
                    else begin ar<=ar+1'b1;a_addr<=a_addr+stride;end
                end else if(mem_req_ready) state<=A_RSP;
                A_RSP: if(mem_rsp_valid) begin
                    s_a[wr][8*ar +: 8]<=mem_rsp_data;
                    if(ar==PE_ROWS-1) begin state<=B_REQ; aw_pend<=fill_a; aw_kk<=kk_w; end
                    else begin ar<=ar+1'b1;a_addr<=a_addr+stride;state<=A_REQ;end
                end
                B_REQ: if(!s_cols[wr][bc]) begin
                    if(bc==PE_COLS-1) begin full[wr]<=1'b1; wr<=wr^FLIP; state<=IDLE; end
                    else begin bc<=bc+1'b1;b_addr<=b_addr+1'b1;end
                end else if(mem_req_ready) state<=B_RSP;
                B_RSP: if(mem_rsp_valid) begin
                    s_b[wr][8*bc +: 8]<=mem_rsp_data;
                    if(bc==PE_COLS-1) begin full[wr]<=1'b1; wr<=wr^FLIP; state<=IDLE; end
                    else begin bc<=bc+1'b1;b_addr<=b_addr+1'b1;state<=B_REQ;end
                end
                default: state<=IDLE;
            endcase
        end
    end
endmodule

// Simple dual-port RAM, 1-cycle read. No reset on the array, registered read data:
// the pattern Vivado infers as BRAM (or LUTRAM when small). Kept in this file because
// the flow compiles a fixed list of six .v files.
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
