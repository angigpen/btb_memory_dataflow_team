`timescale 1ns/1ps
// =====================================================================
// operand_loader  --  SET 1 : Phase 1 (A reuse + zero read skip) + Phase 2a (double buffering)
// Memory/Dataflow team
//
// Same memory interface as the baseline: 1 byte per request, ONE request outstanding.
//
// Switches (parameters):
//   USE_A_REUSE   1: keep A[row tile, kk] (4 bytes + zero flag) in an on-chip A reuse buffer.
//                    col tile 0 fills the reuse buffer, later col tiles read it instead of memory.
//   USE_ZERO_SKIP 1: (needs USE_A_REUSE) if the buffered A word is all zero, the B reads for
//                    that packet are skipped. The packet is STILL sent to the MAC with B=0,
//                    so op_first / op_last keep the accumulation correct.
//   USE_DBUF      1: two packet slots (Ping/Pong). The loader fills one slot while the
//                    other slot waits for / is consumed by the MAC.
//   ABUF_DEPTH  number of A reuse buffer entries (one per kk). kk >= ABUF_DEPTH falls back
//                 to reading A from memory, so any K still works.
//   All switches 0  ->  same behaviour and cycle count as the baseline loader.
//
// Load FSM (fills slot wr):
//   IDLE --accept--> A_BRD (A from reuse buffer, 1-cycle RAM read) --> B_REQ ... or done (zero skip)
//                \-> A_REQ <-> A_RSP (A from memory) --> B_REQ <-> B_RSP --> done
// Supply side (slot rd): op_valid = full[rd]; MAC handshake empties the slot.
// =====================================================================
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
                        parameter USE_A_REUSE=1, USE_ZERO_SKIP=1, USE_DBUF=1,
                        parameter ABUF_DEPTH=1024)(
    input clk,rst,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,
    input [15:0] req_kk,              // NEW (from gemm_control)
    input req_reuse_a,                // NEW (from gemm_control): col tile != 0
    output mem_req_valid,input mem_req_ready,
    output mem_req_is_b,output [31:0] mem_req_addr,
    input mem_rsp_valid,output mem_rsp_ready,input [7:0] mem_rsp_data,
    output op_valid,input op_ready,
    output [8*PE_ROWS-1:0] op_a,output [8*PE_COLS-1:0] op_b,
    output [PE_ROWS-1:0] op_rows,output [PE_COLS-1:0] op_cols,
    output op_first,op_last,output loader_busy
);
    localparam IDLE=0,A_BRD=1,A_REQ=2,A_RSP=3,B_REQ=4,B_RSP=5;
    localparam RB=(PE_ROWS>1)?$clog2(PE_ROWS):1, CB=(PE_COLS>1)?$clog2(PE_COLS):1;
    localparam NS=(USE_DBUF!=0)?2:1;                 // number of packet slots
    localparam AW=(ABUF_DEPTH>1)?$clog2(ABUF_DEPTH):1;
    localparam AWID=8*PE_ROWS;                       // A word width (4 bytes)
    localparam ZS=(USE_ZERO_SKIP!=0) && (USE_A_REUSE!=0);
    localparam [0:0] FLIP=(USE_DBUF!=0);             // slot pointer toggles only with 2 slots

    // ---------------- packet slots (Ping / Pong) ----------------
    reg [8*PE_ROWS-1:0] s_a    [0:NS-1];
    reg [8*PE_COLS-1:0] s_b    [0:NS-1];
    reg [PE_ROWS-1:0]   s_rows [0:NS-1];
    reg [PE_COLS-1:0]   s_cols [0:NS-1];
    reg                 s_first[0:NS-1];
    reg                 s_last [0:NS-1];
    reg [1:0] full;                                  // full[i]: slot i holds a finished packet
    reg wr,rd;                                       // slot being filled / slot offered to MAC

    // ---------------- load FSM working registers ----------------
    reg [2:0] state;
    reg [31:0] a_addr,b_addr;
    reg [15:0] stride;
    reg [RB-1:0] ar;
    reg [CB-1:0] bc;
    reg [15:0] kk_r;
    reg fill_a;                                      // this packet writes the A reuse buffer

    // ---------------- A reuse buffer (on-chip RAM, 1-cycle read) ----------------
    wire kk_fits  = (req_kk < ABUF_DEPTH);
    wire hit_a    = (USE_A_REUSE!=0) && req_reuse_a && kk_fits;   // A comes from reuse buffer
    wire accept   = req_valid && req_ready;
    reg  aw_pend;                                    // write reuse buffer one cycle after A is complete
    reg  [15:0] aw_kk;
    wire [AWID:0] abuf_q;                           // {zero_flag, A word}
    wire [AWID-1:0] aw_word = s_a[wr];
    generate if(USE_A_REUSE!=0) begin: g_abuf
        sdp_ram #(.WIDTH(AWID+1),.DEPTH(ABUF_DEPTH)) a_buf(
            .clk(clk),
            .we(aw_pend),.waddr(aw_kk[AW-1:0]),.wdata({(aw_word=={AWID{1'b0}}),aw_word}),
            .re(accept && hit_a),.raddr(req_kk[AW-1:0]),.rdata(abuf_q));
    end else begin: g_no_abuf
        assign abuf_q={(AWID+1){1'b0}};
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
    assign loader_busy=(state!=IDLE);

    integer i;
    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE;ar<=0;bc<=0;a_addr<=0;b_addr<=0;stride<=0;kk_r<=0;fill_a<=0;
            full<=0;wr<=0;rd<=0;aw_pend<=0;aw_kk<=0;
            for(i=0;i<NS;i=i+1) begin
                s_a[i]<=0;s_b[i]<=0;s_rows[i]<=0;s_cols[i]<=0;s_first[i]<=0;s_last[i]<=0;
            end
        end else begin
            aw_pend<=0;
            // ---- supply side: MAC took the packet in slot rd ----
            if(op_valid && op_ready) begin full[rd]<=1'b0; rd<=rd^FLIP; end
            // ---- load side: fill slot wr ----
            case(state)
                IDLE: if(accept) begin
                    a_addr<=req_a_addr;b_addr<=req_b_addr;stride<=req_a_stride;kk_r<=req_kk;
                    fill_a<=(USE_A_REUSE!=0) && !req_reuse_a && kk_fits;
                    s_rows[wr]<=req_rows;s_cols[wr]<=req_cols;s_first[wr]<=req_first;s_last[wr]<=req_last;
                    s_a[wr]<=0;s_b[wr]<=0;ar<=0;bc<=0;
                    state<=hit_a?A_BRD:A_REQ;
                end
                A_BRD: begin                                   // reuse buffer data arrives this cycle
                    s_a[wr]<=abuf_q[AWID-1:0];
                    if(ZS && abuf_q[AWID]) begin              // all-zero A: skip B reads
                        full[wr]<=1'b1; wr<=wr^FLIP; state<=IDLE;
                    end else state<=B_REQ;
                end
                A_REQ: if(!s_rows[wr][ar]) begin               // masked row: no read
                    if(ar==PE_ROWS-1) begin state<=B_REQ; aw_pend<=fill_a; aw_kk<=kk_r; end
                    else begin ar<=ar+1'b1;a_addr<=a_addr+stride;end
                end else if(mem_req_ready) state<=A_RSP;
                A_RSP: if(mem_rsp_valid) begin
                    s_a[wr][8*ar +: 8]<=mem_rsp_data;
                    if(ar==PE_ROWS-1) begin state<=B_REQ; aw_pend<=fill_a; aw_kk<=kk_r; end
                    else begin ar<=ar+1'b1;a_addr<=a_addr+stride;state<=A_REQ;end
                end
                B_REQ: if(!s_cols[wr][bc]) begin               // masked column: no read
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
