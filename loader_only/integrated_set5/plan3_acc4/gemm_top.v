`timescale 1ns/1ps
// SET5 loader -> N:4 group frontend -> team NM MAC (A is K-major 128 bits).
module gemm_top #(parameter PE_ROWS=4,PE_COLS=4,MAX_M=64,MAX_N=128,MAX_K=1024,
    // ---- Set 4 settings (one place) ----
    parameter NBANK=4,              // ACC banks (agreed integration: four sets)
    parameter SET5_PLAN=3,
    parameter USE_A_REUSE=1, USE_B_REUSE=1,
    parameter USE_B_SHARE=1,        // banks 1.. reuse bank 0's B packet
    parameter NSLOTS=4,
    parameter ABUF_DEPTH=4096,      // A reuse buffer entries x 4 B (holds NBANK x K; 4096 = 16 KB = K 1024)
    parameter BBUF_DEPTH=1024,      // B reuse buffer entries x 4 B (holds G x K)
    parameter BKW=(NBANK>1)?$clog2(NBANK):1)(
    input clk,rst,start,
    input [15:0] cfg_m,cfg_n,cfg_k,
    output busy,done,error,

    output mem_req_valid,
    input mem_req_ready,
    output mem_req_is_b,
    output [31:0] mem_req_addr,

    input mem_rsp_valid,
    output mem_rsp_ready,
    input [7:0] mem_rsp_data,

    output out_valid,
    input out_ready,
    output [31:0] out_addr,out_data,

    output [PE_ROWS*PE_COLS-1:0] event_mac,event_nonzero_mac,
    output event_operand_wait,event_compute_stall
);
    wire dv,dr,first,last,enable,rv,rr,wr,wd,ov,ordy,ofirst,olast,lb;
    wire [31:0] aa,ba,base;
    wire [15:0] astride,cstride;
    wire [PE_ROWS-1:0] rm,orm;
    wire [PE_COLS-1:0] cm,ocm;
    wire [8*PE_ROWS-1:0] oa;
    wire [8*PE_COLS-1:0] ob;
    wire [32*PE_ROWS*PE_COLS-1:0] data;
    wire [PE_ROWS*PE_COLS-1:0] mask;
    wire cv,cr,cf,cl;
    wire [4*8*PE_ROWS-1:0] ca;
    wire [4*PE_COLS-1:0] cb_tag;
    wire [2*PE_COLS-1:0] column_k_position;
    wire [15:0] group_k_base;
    wire group_end;
    wire [2:0] group_len;
    wire [4*PE_ROWS-1:0] group_mask_a;
    wire [4*PE_COLS-1:0] group_mask_b;
    wire [2*PE_ROWS-1:0] group_mode_a;
    wire [2*PE_COLS-1:0] group_mode_b;
    wire [8*PE_COLS-1:0] cb;
    wire [PE_ROWS-1:0] crm;
    wire [PE_COLS-1:0] ccm;
    wire [PE_ROWS*PE_COLS-1:0] work_mask;
    wire [BKW-1:0] dbank, obank, cbank;
    wire dahit, dafill, dbhit, dbfill, dbshare;
    wire [31:0] daba, dbba;
    // One input K packet does not map to one output round: no bank FIFO.
    // The frontend emits round -> bank order for the NM MAC's ACC rotation.
    assign rr=wr && enable;
    assign event_operand_wait=ordy && !ov && lb;
    assign event_compute_stall=ov && !ordy;

    gemm_control #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),.MAX_M(MAX_M),.MAX_N(MAX_N),.MAX_K(MAX_K),
        .NBANK(NBANK),.USE_A_REUSE(USE_A_REUSE),.USE_B_REUSE(USE_B_REUSE),.USE_B_SHARE(USE_B_SHARE),
        .ABUF_DEPTH(ABUF_DEPTH),.BBUF_DEPTH(BBUF_DEPTH)) control(
        .clk(clk),.rst(rst),.start(start),.cfg_m(cfg_m),.cfg_n(cfg_n),.cfg_k(cfg_k),
        .busy(busy),.done(done),.error(error),
        .req_valid(dv),.req_ready(dr),.req_a_addr(aa),.req_b_addr(ba),.req_a_stride(astride),
        .req_rows(rm),.req_cols(cm),.req_first(first),.req_last(last),
        .req_bank(dbank),.req_a_hit(dahit),.req_a_fill(dafill),.req_abuf_addr(daba),
        .req_b_hit(dbhit),.req_b_fill(dbfill),.req_b_share(dbshare),.req_bbuf_addr(dbba),
        .result_enable(enable),.result_base(base),.result_stride(cstride),
        .result_accepted(rv && rr),.write_done(wd));


    wire loader_mem_req_valid, loader_mem_req_ready, loader_mem_req_is_b;
    wire [31:0] loader_mem_req_addr;
    wire loader_mem_rsp_valid, loader_mem_rsp_ready;
    wire [7:0] loader_mem_rsp_data;
    generate if(SET5_PLAN==1) begin: plan1
        assign mem_req_valid=loader_mem_req_valid;
        assign loader_mem_req_ready=mem_req_ready;
        assign mem_req_is_b=loader_mem_req_is_b;
        assign mem_req_addr=loader_mem_req_addr;
        assign loader_mem_rsp_valid=mem_rsp_valid;
        assign mem_rsp_ready=loader_mem_rsp_ready;
        assign loader_mem_rsp_data=mem_rsp_data;
    end else begin: plan23
    b_prefetch_bridge #(.PE_COLS(PE_COLS),.MODE(SET5_PLAN-1)) b_schedule(
        .clk(clk),.rst(rst),.start(start && !busy),.cfg_n(cfg_n),.cfg_k(cfg_k),
        .in_valid(loader_mem_req_valid),.in_ready(loader_mem_req_ready),
        .in_is_b(loader_mem_req_is_b),.in_addr(loader_mem_req_addr),
        .out_valid(loader_mem_rsp_valid),.out_ready(loader_mem_rsp_ready),.out_data(loader_mem_rsp_data),
        .mem_req_valid(mem_req_valid),.mem_req_ready(mem_req_ready),
        .mem_req_is_b(mem_req_is_b),.mem_req_addr(mem_req_addr),
        .mem_rsp_valid(mem_rsp_valid),.mem_rsp_ready(mem_rsp_ready),.mem_rsp_data(mem_rsp_data));
    end endgenerate
    operand_loader #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),.NBANK(NBANK),.NSLOTS(NSLOTS),
        .USE_A_REUSE(USE_A_REUSE),.USE_B_REUSE(USE_B_REUSE),
        .ABUF_DEPTH(ABUF_DEPTH),.BBUF_DEPTH(BBUF_DEPTH)) loader(
        .clk(clk),.rst(rst),.req_valid(dv),.req_ready(dr),
        .req_a_addr(aa),.req_b_addr(ba),.req_a_stride(astride),
        .req_rows(rm),.req_cols(cm),.req_first(first),.req_last(last),
        .req_bank(dbank),.req_a_hit(dahit),.req_a_fill(dafill),.req_abuf_addr(daba),
        .req_b_hit(dbhit),.req_b_fill(dbfill),.req_b_share(dbshare),.req_bbuf_addr(dbba),
        .mem_req_valid(loader_mem_req_valid),.mem_req_ready(loader_mem_req_ready),
        .mem_req_is_b(loader_mem_req_is_b),.mem_req_addr(loader_mem_req_addr),
        .mem_rsp_valid(loader_mem_rsp_valid),.mem_rsp_ready(loader_mem_rsp_ready),.mem_rsp_data(loader_mem_rsp_data),
        .op_valid(ov),.op_ready(ordy),.op_a(oa),.op_b(ob),.op_rows(orm),.op_cols(ocm),
        .op_first(ofirst),.op_last(olast),.op_bank(obank),.loader_busy(lb));

    sparsity_frontend #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),.NBANK(NBANK)) sparsity(
        .clk(clk),.rst(rst),
        .in_valid(ov),.in_ready(ordy),.in_a(oa),.in_b(ob),.in_rows(orm),.in_cols(ocm),
        .in_first(ofirst),.in_last(olast),.in_bank(obank),.out_valid(cv),.out_ready(cr),.out_a(ca),.out_b(cb),
        .out_rows(crm),.out_cols(ccm),.out_first(cf),.out_last(cl),.work_mask(work_mask),
        .out_b_tag(cb_tag),.out_k_mask(column_k_position),.out_bank(cbank),.out_k(group_k_base),
        .out_group_end(group_end),.out_group_len(group_len),
        .out_group_mask_a(group_mask_a),.out_group_mask_b(group_mask_b),
        .out_group_mode_a(group_mode_a),.out_group_mode_b(group_mode_b));

    mac_array_compute #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),.ACC_dup(NBANK>1),.PE_Folding(0),.REUSE(NBANK)) compute(
        .clk(clk),.rst(rst),.op_valid(cv),.op_ready(cr),.op_a(ca),.op_b(cb),
        .op_rows(crm),.op_cols(ccm),.op_first(cf),.op_last(cl),.work_mask(work_mask),
        .op_b_tag(cb_tag),.column_k_position(column_k_position),
        .result_valid(rv),.result_ready(rr),.result_data(data),.result_mask(mask),
        .event_mac(event_mac),.event_nonzero_mac(event_nonzero_mac));

    result_writer #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS)) writer(
        .clk(clk),.rst(rst),.result_valid(rv && enable),.result_ready(wr),
        .result_base(base),.result_stride(cstride),.result_data(data),.result_mask(mask),
        .out_valid(out_valid),.out_ready(out_ready),.out_addr(out_addr),.out_data(out_data),.done(wd));

    // This integration targets the teams' agreed 4x4, ACC 4-set interface.
    initial begin
        if(PE_ROWS!=4 || PE_COLS!=4 || (NBANK!=2 && NBANK!=4) || SET5_PLAN<1 || SET5_PLAN>3)
            $error("Supported configuration: PE=4x4, NBANK=2/4, SET5_PLAN=1/2/3");
    end
endmodule

