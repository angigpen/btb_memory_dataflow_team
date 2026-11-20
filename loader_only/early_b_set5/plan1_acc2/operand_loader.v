`timescale 1ns/1ps
// Direct scalar memory issue. B responses stream to the frontend's own two group buffers.
// A retains four packet slots and the original A/B reuse RAM capacities.
module operand_loader #(parameter PE_ROWS=4, PE_COLS=4,
    NBANK=2, NSLOTS=4, PLAN=1, USE_A_REUSE=1, USE_B_REUSE=1,
    ABUF_DEPTH=4096, BBUF_DEPTH=1024,
    BKW=(NBANK>1)?$clog2(NBANK):1)(
    input clk,rst,start,
    input [15:0] cfg_m,cfg_n,cfg_k,
    input req_valid,output req_ready,
    input [31:0] req_a_addr,req_b_addr,input [15:0] req_a_stride,
    input [PE_ROWS-1:0] req_rows,input [PE_COLS-1:0] req_cols,
    input req_first,req_last,input [BKW-1:0] req_bank,
    input req_a_hit,req_a_fill,input [31:0] req_abuf_addr,
    input req_b_hit,req_b_fill,req_b_share,input [31:0] req_bbuf_addr,
    output mem_req_valid,input mem_req_ready,output mem_req_is_b,
    output [31:0] mem_req_addr,
    input mem_rsp_valid,output mem_rsp_ready,input [7:0] mem_rsp_data,
    output op_valid,input op_ready,
    output [8*PE_ROWS-1:0] op_a,output [8*PE_COLS-1:0] op_b,
    output [PE_ROWS-1:0] op_rows,output [PE_COLS-1:0] op_cols,
    output op_first,op_last,output [BKW-1:0] op_bank,output loader_busy,
    output op_group,output [1:0] op_pos,
    output group_valid,input group_ready,input group_ticket,
    output [15:0] group_base_k,output [2:0] group_len,
    output [PE_COLS-1:0] group_cols,output [BKW-1:0] group_last_bank,
    output group_first,group_last,
    output b_valid,input b_ready,output b_group,output [1:0] b_pos,
    output [8*PE_COLS-1:0] b_data,output [PE_COLS-1:0] b_mask,
    output b_peek_group,output [1:0] b_peek_pos,
    input [8*PE_COLS-1:0] b_peek_word
);
    localparam SB=(NSLOTS>1)?$clog2(NSLOTS):1;
    localparam AAW=(ABUF_DEPTH>1)?$clog2(ABUF_DEPTH):1;
    localparam BAW=(BBUF_DEPTH>1)?$clog2(BBUF_DEPTH):1;
    localparam CHECK=0, WAIT_A=1, WAIT_B=2, CACHE_B=3;
    reg [1:0] state;
    reg [15:0] m,n,k;
    reg [8*PE_ROWS-1:0] s_a[0:NSLOTS-1];
    reg [PE_ROWS-1:0] s_rows[0:NSLOTS-1];
    reg [PE_COLS-1:0] s_cols[0:NSLOTS-1];
    reg s_first[0:NSLOTS-1],s_last[0:NSLOTS-1],s_group[0:NSLOTS-1];
    reg [1:0] s_pos[0:NSLOTS-1];
    reg [BKW-1:0] s_bank[0:NSLOTS-1];
    reg [31:0] s_aaddr[0:NSLOTS-1],s_baddr[0:NSLOTS-1];
    reg [AAW-1:0] s_aba[0:NSLOTS-1];
    reg [PE_ROWS-1:0] s_rem[0:NSLOTS-1];
    reg [$clog2(PE_ROWS+1)-1:0] s_left[0:NSLOTS-1];
    reg [NSLOTS-1:0] alloc,prep,idone,filla;
    reg [SB-1:0] wptr,ip,rd;
    reg [15:0] stride;
    reg last_ticket;
    reg cap_v,cap_hita;
    reg [SB-1:0] cap_s;
    reg aw_pend,bw_pend;
    reg [SB-1:0] aw_s;
    reg [BAW-1:0] bw_address;
    reg bw_group;
    reg [1:0] bw_pos;

    // Per-group routing/completion metadata; B values are stored only in the frontend.
    reg [31:0] g_mem_base[0:1],g_cache_base[0:1];
    reg [2:0] g_len[0:1];
    reg [PE_COLS-1:0] g_cols[0:1];
    reg g_hit[0:1],g_fill[0:1];
    reg [PE_COLS-1:0] received[0:1][0:3];
    reg [1:0] q_pos;
    reg q_group;
    integer q_col;
    reg cache_pending,cache_group;
    reg [1:0] cache_pos;
    reg [SB-1:0] q_slot;
    integer q_lane;

    function [SB-1:0] nxt(input [SB-1:0] p);
        nxt=(p==NSLOTS-1)?0:p+1'b1;
    endfunction
    function integer row_popcount(input [PE_ROWS-1:0] mask);
        integer j;
        begin row_popcount=0;for(j=0;j<PE_ROWS;j=j+1)row_popcount=row_popcount+mask[j];end
    endfunction
    function integer first_a(input [PE_ROWS-1:0] mask);
        integer j;
        begin first_a=0;for(j=PE_ROWS-1;j>=0;j=j-1)if(mask[j])first_a=j;end
    endfunction

    wire [15:0] req_k=req_b_addr/n;
    wire new_group=(req_bank==0 && req_k[1:0]==0);
    wire [31:0] first_row=req_a_addr/k;
    wire [31:0] remaining_tiles=(m-first_row+PE_ROWS-1)/PE_ROWS;
    wire [BKW-1:0] last_bank=(remaining_tiles>=NBANK)?NBANK-1:remaining_tiles-1;
    wire hit_a=(USE_A_REUSE!=0)&&req_a_hit;
    wire a_busy=(|(alloc&filla))||aw_pend;
    wire admission=!alloc[wptr]&&!rst&&!(hit_a&&a_busy);
    assign group_valid=req_valid&&admission&&new_group;
    assign group_base_k={req_k[15:2],2'b00};
    assign group_len=((k-group_base_k)<4)?k-group_base_k:4;
    assign group_cols=req_cols;
    assign group_last_bank=last_bank;
    assign group_first=(group_base_k==0);
    assign group_last=(group_base_k+group_len==k);
    assign req_ready=admission&&(!new_group||group_ready);
    wire accept=req_valid&&req_ready;

    wire [8*PE_ROWS-1:0] acq;
    wire [8*PE_COLS-1:0] bq;
    generate if(USE_A_REUSE!=0) begin:g_abuf
        sdp_ram #(.WIDTH(8*PE_ROWS),.DEPTH(ABUF_DEPTH)) a_buf(
            .clk(clk),.we(aw_pend),.waddr(s_aba[aw_s]),.wdata(s_a[aw_s]),
            .re(accept&&hit_a),.raddr(req_abuf_addr[AAW-1:0]),.rdata(acq));
    end else begin:g_no_abuf
        assign acq=0;
    end endgenerate

    wire active=alloc[ip]&&prep[ip]&&!idone[ip];
    wire issue_group=s_group[ip];
    integer rr,cc;
    reg need_b;
    reg [1:0] next_pos;
    integer next_col;
    always @* begin
        need_b=0;next_pos=0;next_col=0;
        // Reverse traversal with overwrite picks the earliest missing coordinate.
        if(PLAN==3) begin
            for(cc=PE_COLS-1;cc>=0;cc=cc-1)
                for(rr=3;rr>=0;rr=rr-1)
                    if(rr<g_len[issue_group]&&g_cols[issue_group][cc]&&
                       !received[issue_group][rr][cc]) begin
                        need_b=1;next_pos=rr;next_col=cc;
                    end
        end else begin
            for(rr=3;rr>=0;rr=rr-1)
                for(cc=PE_COLS-1;cc>=0;cc=cc-1)
                    if(rr<g_len[issue_group]&&(PLAN!=1||rr==s_pos[ip])&&
                       g_cols[issue_group][cc]&&!received[issue_group][rr][cc]) begin
                        need_b=1;next_pos=rr;next_col=cc;
                    end
        end
    end
    wire [31:0] cache_address=g_cache_base[issue_group]+next_pos;
    wire cached_b=(USE_B_REUSE!=0)&&g_hit[issue_group]&&cache_address<BBUF_DEPTH;
    integer cg,cp,ci;
    reg fill_pending,cache_candidate;
    reg candidate_group;
    reg [1:0] candidate_pos;
    reg [31:0] candidate_address;
    always @*begin
        fill_pending=bw_pend;
        for(cg=0;cg<2;cg=cg+1)
            for(cp=0;cp<4;cp=cp+1)
                if(cg==issue_group&&g_fill[cg]&&cp<g_len[cg]&&received[cg][cp]!=g_cols[cg])
                    fill_pending=1;
        cache_candidate=0;candidate_group=0;candidate_pos=0;candidate_address=0;
        for(ci=1;ci>=0;ci=ci-1)begin
            cg=issue_group^ci;
            for(cp=3;cp>=0;cp=cp-1)
                if(USE_B_REUSE!=0&&g_hit[cg]&&cp<g_len[cg]&&
                   (g_cache_base[cg]+cp)<BBUF_DEPTH&&received[cg][cp]!=g_cols[cg])begin
                    cache_candidate=1;candidate_group=cg;
                    candidate_pos=cp;candidate_address=g_cache_base[cg]+cp;
                end
        end
    end
    // B RAM has its own port: prefill the frontend while external A reads proceed.
    wire cache_re=!rst&&!cache_pending&&cache_candidate&&!fill_pending;
    generate if(USE_B_REUSE!=0) begin:g_bbuf
        sdp_ram #(.WIDTH(8*PE_COLS),.DEPTH(BBUF_DEPTH)) b_buf(
            .clk(clk),.we(bw_pend),.waddr(bw_address),.wdata(b_peek_word),
            .re(cache_re),.raddr(candidate_address[BAW-1:0]),.rdata(bq));
    end else begin:g_no_bbuf
        assign bq=0;
    end endgenerate

    wire issue_b=active&&need_b&&!cached_b&&!cache_pending&&b_ready;
    wire issue_a=active&&!need_b&&(|s_rem[ip]);
    wire ext_a=state==CHECK&&issue_a;
    integer lane;
    always @* lane=first_a(s_rem[ip]);
    assign mem_req_valid=!rst&&state==CHECK&&(issue_b||issue_a);
    assign mem_req_is_b=need_b;
    assign mem_req_addr=need_b?
        g_mem_base[issue_group]+next_pos*n+next_col:
        s_aaddr[ip]+lane*stride;
    wire req_fire=mem_req_valid&&mem_req_ready;
    assign mem_rsp_ready=!rst&&((state==WAIT_A)||(state==WAIT_B&&b_ready));
    wire rsp_fire=mem_rsp_valid&&mem_rsp_ready;

    assign b_group=(state==WAIT_B)?q_group:cache_pending?cache_group:issue_group;
    assign b_pos=(state==WAIT_B)?q_pos:cache_pos;
    assign b_mask=(state!=WAIT_B)?(g_cols[cache_group]&~received[cache_group][cache_pos]):
                  ({{(PE_COLS-1){1'b0}},1'b1}<<q_col);
    assign b_data=(state!=WAIT_B)?bq:
                  ({{(8*PE_COLS-8){1'b0}},mem_rsp_data}<<(8*q_col));
    assign b_valid=!rst&&((state!=WAIT_B&&cache_pending)||(state==WAIT_B&&mem_rsp_valid));
    wire b_fire=b_valid&&b_ready;
    assign b_peek_group=bw_group;
    assign b_peek_pos=bw_pos;

    // A cache hit can be supplied on the same edge that issue control retires it.
    wire retiring_cached_a=state==CHECK&&active&&ip==rd&&!need_b&&(s_rem[ip]==0);
    assign op_valid=!rst&&alloc[rd]&&prep[rd]&&(idone[rd]||retiring_cached_a)&&(s_left[rd]==0);
    assign op_a=s_a[rd];assign op_b=0; // B is already in the independently filled frontend group.
    assign op_rows=s_rows[rd];assign op_cols=s_cols[rd];
    assign op_first=s_first[rd];assign op_last=s_last[rd];assign op_bank=s_bank[rd];
    assign op_group=s_group[rd];assign op_pos=s_pos[rd];
    assign loader_busy=(|alloc)||(state!=CHECK);

    integer i,j;
    always @(posedge clk) begin
        if(rst) begin
            state<=CHECK;m<=1;n<=1;k<=1;alloc<=0;prep<=0;idone<=0;filla<=0;
            wptr<=0;ip<=0;rd<=0;stride<=0;last_ticket<=0;
            cap_v<=0;cap_hita<=0;cap_s<=0;aw_pend<=0;aw_s<=0;
            bw_pend<=0;bw_address<=0;bw_group<=0;bw_pos<=0;
            q_group<=0;q_pos<=0;q_col<=0;q_slot<=0;q_lane<=0;cache_pending<=0;cache_group<=0;cache_pos<=0;
            for(i=0;i<2;i=i+1) begin
                g_mem_base[i]<=0;g_cache_base[i]<=0;g_len[i]<=0;
                g_cols[i]<=0;g_hit[i]<=0;g_fill[i]<=0;
                for(j=0;j<4;j=j+1)received[i][j]<=0;
            end
            for(i=0;i<NSLOTS;i=i+1) begin
                s_a[i]<=0;s_rows[i]<=0;s_cols[i]<=0;s_first[i]<=0;s_last[i]<=0;
                s_group[i]<=0;s_pos[i]<=0;s_bank[i]<=0;s_aaddr[i]<=0;s_baddr[i]<=0;
                s_aba[i]<=0;s_rem[i]<=0;s_left[i]<=0;
            end
        end else begin
            if(start)begin m<=cfg_m;n<=cfg_n;k<=cfg_k;end
            aw_pend<=0;bw_pend<=0;cap_v<=accept;
            if(accept) begin
                alloc[wptr]<=1;prep[wptr]<=0;idone[wptr]<=0;
                s_aaddr[wptr]<=req_a_addr;s_baddr[wptr]<=req_b_addr;stride<=req_a_stride;
                s_rows[wptr]<=req_rows;s_cols[wptr]<=req_cols;
                s_first[wptr]<=req_first;s_last[wptr]<=req_last;s_bank[wptr]<=req_bank;
                s_aba[wptr]<=req_abuf_addr[AAW-1:0];
                s_group[wptr]<=new_group?group_ticket:last_ticket;s_pos[wptr]<=req_k[1:0];
                filla[wptr]<=(USE_A_REUSE!=0)&&req_a_fill;
                cap_s<=wptr;cap_hita<=hit_a;wptr<=nxt(wptr);
                if(new_group) begin
                    last_ticket<=group_ticket;
                    g_mem_base[group_ticket]<=req_b_addr-req_k[1:0]*n;
                    g_cache_base[group_ticket]<=req_bbuf_addr-req_k[1:0];
                    g_len[group_ticket]<=group_len;g_cols[group_ticket]<=req_cols;
                    g_hit[group_ticket]<=req_b_hit;g_fill[group_ticket]<=req_b_fill;
                    for(j=0;j<4;j=j+1)received[group_ticket][j]<=0;
                end
            end
            if(cap_v) begin
                s_a[cap_s]<=cap_hita?acq:0;
                s_rem[cap_s]<=cap_hita?0:s_rows[cap_s];
                s_left[cap_s]<=cap_hita?0:row_popcount(s_rows[cap_s]);
                prep[cap_s]<=1;
            end
            if(cache_re)begin
                cache_pending<=1;cache_group<=candidate_group;cache_pos<=candidate_pos;
            end
            if(state==CHECK&&active) begin
                if(req_fire) begin
                    if(need_b) begin
                        q_group<=issue_group;q_pos<=next_pos;q_col<=next_col;
                        state<=WAIT_B;
                    end else begin
                        q_slot<=ip;q_lane<=lane;state<=WAIT_A;
                        s_rem[ip]<=s_rem[ip]&~(1<<lane);
                        if((s_rem[ip]&~(1<<lane))==0)begin idone[ip]<=1;ip<=nxt(ip);end
                    end
                end else if(!need_b&&s_rem[ip]==0)begin idone[ip]<=1;ip<=nxt(ip);end
            end
            if(state==WAIT_A&&rsp_fire) begin
                s_a[q_slot][8*q_lane +: 8]<=mem_rsp_data;
                s_left[q_slot]<=s_left[q_slot]-1'b1;
                if(s_left[q_slot]==1&&filla[q_slot])begin aw_pend<=1;aw_s<=q_slot;end
                state<=CHECK;
            end
            if(b_fire) begin
                received[b_group][b_pos]<=received[b_group][b_pos]|b_mask;
                if((received[b_group][b_pos]|b_mask)==g_cols[b_group]&&
                   g_fill[b_group]&&(g_cache_base[b_group]+b_pos)<BBUF_DEPTH&&USE_B_REUSE!=0) begin
                    bw_pend<=1;bw_address<=g_cache_base[b_group]+b_pos;
                    bw_group<=b_group;bw_pos<=b_pos;
                end
                if(state==WAIT_B)state<=CHECK;
                else cache_pending<=0;
            end
            if(op_valid&&op_ready)begin
                alloc[rd]<=0;prep[rd]<=0;idone[rd]<=0;filla[rd]<=0;rd<=nxt(rd);
            end
        end
    end
endmodule

module sdp_ram #(parameter WIDTH=32,DEPTH=1024,AW=(DEPTH>1)?$clog2(DEPTH):1)(
    input clk,input we,input [AW-1:0] waddr,input [WIDTH-1:0] wdata,
    input re,input [AW-1:0] raddr,output reg [WIDTH-1:0] rdata
);
    reg [WIDTH-1:0] mem[0:DEPTH-1];
    always @(posedge clk)begin
        if(we)mem[waddr]<=wdata;
        if(re)rdata<=mem[raddr];
    end
endmodule
