`timescale 1ns/1ps
// BTB common dense INT8 GEMM baseline. Configuration latched at start.
// Profiling events are public so a replacement module can have variable latency.
// SET 3 (Memory/Dataflow): Set 2 + grouped ("staged") B reuse.
//   Same loader as Set 2. gemm_control changes the loop to group -> row -> col -> kk
//   and picks G = BBUF_DEPTH / K column tiles per group at start.
// Variant switches: edit the defaults below (or use scripts/make_variants.py).
//   baseline-equivalent reads : USE_A_REUSE=0 USE_ZERO_SKIP=0 USE_B_REUSE=0 NSLOTS=2 MAX_OUT=1
module gemm_top #(parameter PE_ROWS=4,PE_COLS=4,MAX_M=64,MAX_N=128,MAX_K=1024,
                  parameter USE_A_REUSE=1,      // Phase 1a
                  parameter USE_ZERO_SKIP=1,    // Phase 1b (needs USE_A_REUSE)
                  parameter USE_B_REUSE=1,      // Phase 3 (B reuse buffer)
                  parameter NSLOTS=4,           // Phase 2a: packet slots (2 = Ping/Pong)
                  parameter MAX_OUT=32,         // Phase 2b: requests in flight (1 = off)
                  parameter ABUF_DEPTH=1024,  // A reuse buffer entries (1024 x 33 bit, about 1 BRAM36)
                  parameter BBUF_DEPTH=1024     // B reuse buffer entries x 4 byte: 1024 = 4 KB = 1 BRAM36 (G=8 at K=128)
                 )(
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
    wire [8*PE_ROWS-1:0] ca;
    wire [8*PE_COLS-1:0] cb;
    wire [PE_ROWS-1:0] crm;
    wire [PE_COLS-1:0] ccm;
    wire [PE_ROWS*PE_COLS-1:0] work_mask;
    wire [15:0] kk;          // NEW: control -> loader
    wire reuse_a,reuse_b;    // NEW
    wire last_row;           // NEW
    wire [31:0] bbuf_addr;   // NEW
    assign rr=wr && enable;
    assign event_operand_wait=ordy && !ov && lb;
    assign event_compute_stall=ov && !ordy;

    gemm_control #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),.MAX_M(MAX_M),.MAX_N(MAX_N),.MAX_K(MAX_K),
        .USE_B_REUSE(USE_B_REUSE),.BBUF_DEPTH(BBUF_DEPTH)) control(
        .clk(clk),.rst(rst),.start(start),.cfg_m(cfg_m),.cfg_n(cfg_n),.cfg_k(cfg_k),
        .busy(busy),.done(done),.error(error),
        .req_valid(dv),.req_ready(dr),.req_a_addr(aa),.req_b_addr(ba),.req_a_stride(astride),
        .req_rows(rm),.req_cols(cm),.req_first(first),.req_last(last),
        .req_kk(kk),.req_reuse_a(reuse_a),.req_reuse_b(reuse_b),.req_bbuf_addr(bbuf_addr),.req_last_row(last_row),
        .result_enable(enable),.result_base(base),.result_stride(cstride),
        .result_accepted(rv && rr),.write_done(wd));

    operand_loader #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS),
        .USE_A_REUSE(USE_A_REUSE),.USE_ZERO_SKIP(USE_ZERO_SKIP),.USE_B_REUSE(USE_B_REUSE),
        .NSLOTS(NSLOTS),.MAX_OUT(MAX_OUT),.ABUF_DEPTH(ABUF_DEPTH),.BBUF_DEPTH(BBUF_DEPTH)) loader(
        .clk(clk),.rst(rst),.req_valid(dv),.req_ready(dr),
        .req_a_addr(aa),.req_b_addr(ba),.req_a_stride(astride),
        .req_rows(rm),.req_cols(cm),.req_first(first),.req_last(last),
        .req_kk(kk),.req_reuse_a(reuse_a),.req_reuse_b(reuse_b),.req_bbuf_addr(bbuf_addr),.req_last_row(last_row),
        .mem_req_valid(mem_req_valid),.mem_req_ready(mem_req_ready),
        .mem_req_is_b(mem_req_is_b),.mem_req_addr(mem_req_addr),
        .mem_rsp_valid(mem_rsp_valid),.mem_rsp_ready(mem_rsp_ready),.mem_rsp_data(mem_rsp_data),
        .op_valid(ov),.op_ready(ordy),.op_a(oa),.op_b(ob),.op_rows(orm),.op_cols(ocm),
        .op_first(ofirst),.op_last(olast),.loader_busy(lb));

    sparsity_frontend #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS)) sparsity(
        .clk(clk),.rst(rst),
        .in_valid(ov),.in_ready(ordy),.in_a(oa),.in_b(ob),.in_rows(orm),.in_cols(ocm),
        .in_first(ofirst),.in_last(olast),.out_valid(cv),.out_ready(cr),.out_a(ca),.out_b(cb),
        .out_rows(crm),.out_cols(ccm),.out_first(cf),.out_last(cl),.work_mask(work_mask));

    mac_array #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS)) compute(
        .clk(clk),.rst(rst),.op_valid(cv),.op_ready(cr),.op_a(ca),.op_b(cb),
        .op_rows(crm),.op_cols(ccm),.op_first(cf),.op_last(cl),.work_mask(work_mask),
        .result_valid(rv),.result_ready(rr),.result_data(data),.result_mask(mask),
        .event_mac(event_mac),.event_nonzero_mac(event_nonzero_mac));

    result_writer #(.PE_ROWS(PE_ROWS),.PE_COLS(PE_COLS)) writer(
        .clk(clk),.rst(rst),.result_valid(rv && enable),.result_ready(wr),
        .result_base(base),.result_stride(cstride),.result_data(data),.result_mask(mask),
        .out_valid(out_valid),.out_ready(out_ready),.out_addr(out_addr),.out_data(out_data),.done(wd));

endmodule
