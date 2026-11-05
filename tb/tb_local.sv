`timescale 1ns/1ps
// Local self-checking testbench (stand-in for tb_gemm.sv, which is not in the project files).
// Memory model: 1-byte port shared by A/B, in-order responses, response LAT cycles after accept,
// pipelined (accepts a new request every cycle) unless +MEMMAXOUT=n limits outstanding requests.
// Reads the same a.hex / b.hex / c.hex / config.hex fixtures that generate.py writes.
module tb_local;
    reg clk=0, rst=1, start=0;
    always #5 clk=~clk;
    reg [15:0] cfg_m,cfg_n,cfg_k;
    wire busy,done,error;
    wire mem_req_valid, mem_req_is_b; wire [31:0] mem_req_addr; reg mem_req_ready;
    wire mem_rsp_valid; wire mem_rsp_ready; wire [7:0] mem_rsp_data;
    wire out_valid; reg out_ready; wire [31:0] out_addr,out_data;
    wire [15:0] ev_mac,ev_nz; wire ev_wait,ev_stall;

    gemm_top dut(.clk(clk),.rst(rst),.start(start),.cfg_m(cfg_m),.cfg_n(cfg_n),.cfg_k(cfg_k),
        .busy(busy),.done(done),.error(error),
        .mem_req_valid(mem_req_valid),.mem_req_ready(mem_req_ready),.mem_req_is_b(mem_req_is_b),
        .mem_req_addr(mem_req_addr),.mem_rsp_valid(mem_rsp_valid),.mem_rsp_ready(mem_rsp_ready),
        .mem_rsp_data(mem_rsp_data),.out_valid(out_valid),.out_ready(out_ready),
        .out_addr(out_addr),.out_data(out_data),.event_mac(ev_mac),.event_nonzero_mac(ev_nz),
        .event_operand_wait(ev_wait),.event_compute_stall(ev_stall));
`include "tb_params.vh"

    reg [7:0]  A [0:65535*2];
    reg [7:0]  B [0:65535*3];
    reg [31:0] C [0:8191];
    reg [31:0] cfg [0:8];
    reg        written [0:8191];
    integer M,N,K,LAT,STALLS,OSTALLS,RUNS,MEMMAXOUT;
    integer cyc=0, errors=0;
    // ---- memory request queue ----
    reg        q_isb [0:4095];
    reg [31:0] q_addr[0:4095];
    integer    q_due [0:4095];
    integer qh=0, qt=0, qcnt=0, max_out_seen=0;
    wire req_fire = mem_req_valid && mem_req_ready;
    wire rsp_fire = mem_rsp_valid && mem_rsp_ready;
    assign mem_rsp_valid = (qcnt>0) && (cyc>=q_due[qh]);
    assign mem_rsp_data  = q_isb[qh] ? B[q_addr[qh]] : A[q_addr[qh]];
    // counters
    integer a_reads, b_reads, wait_cyc, mac_slots, outs;
    integer seed=1;
    // stability bookkeeping
    reg p_rv, p_rr, p_isb; reg [31:0] p_addr;
    reg p_ov, p_or; reg [31:0] p_oa, p_ob; reg [3:0] p_orm, p_ocm; reg p_of, p_ol;

    function integer popc16(input [15:0] v); integer j; begin popc16=0; for(j=0;j<16;j=j+1) popc16=popc16+v[j]; end endfunction

    always @(posedge clk) begin
        cyc <= cyc+1;
        if(rst) begin
            qh<=0; qt<=0; qcnt<=0;
        end else begin
            if(req_fire) begin
                if(!mem_req_is_b && mem_req_addr>=M*K) begin $display("ERROR A addr %0d out of range",mem_req_addr); errors=errors+1; end
                if( mem_req_is_b && mem_req_addr>=K*N) begin $display("ERROR B addr %0d out of range",mem_req_addr); errors=errors+1; end
                q_isb[qt%4096]<=mem_req_is_b; q_addr[qt%4096]<=mem_req_addr; q_due[qt%4096]<=cyc+LAT;
                qt<=(qt+1)%4096;
                if(mem_req_is_b) b_reads=b_reads+1; else a_reads=a_reads+1;
            end
            if(rsp_fire) qh<=(qh+1)%4096;
            qcnt <= qcnt + req_fire - rsp_fire;
            if(qcnt>max_out_seen) max_out_seen=qcnt;
            if(mem_rsp_ready && qcnt==0) ; // fine
            if(busy && ev_wait) wait_cyc=wait_cyc+1;
            mac_slots = mac_slots + popc16(ev_mac);
            // ---- handshake stability checks ----
            if(p_rv && !p_rr && (!mem_req_valid || mem_req_addr!==p_addr || mem_req_is_b!==p_isb)) begin
                $display("ERROR mem_req changed while waiting for ready @%0d",cyc); errors=errors+1; end
            if(p_ov && !p_or && (!dut.ov || dut.oa!==p_oa || dut.ob!==p_ob || dut.orm!==p_orm ||
                dut.ocm!==p_ocm || dut.ofirst!==p_of || dut.olast!==p_ol)) begin
                $display("ERROR op packet changed while waiting for ready @%0d",cyc); errors=errors+1; end
            p_rv<=mem_req_valid; p_rr<=mem_req_ready; p_isb<=mem_req_is_b; p_addr<=mem_req_addr;
            p_ov<=dut.ov; p_or<=dut.ordy; p_oa<=dut.oa; p_ob<=dut.ob; p_orm<=dut.orm; p_ocm<=dut.ocm;
            p_of<=dut.ofirst; p_ol<=dut.olast;
            // ---- output check ----
            if(out_valid && out_ready) begin
                if(out_addr>=M*N) begin $display("ERROR out addr %0d",out_addr); errors=errors+1; end
                else begin
                    if(written[out_addr]) begin $display("ERROR double write %0d",out_addr); errors=errors+1; end
                    if(out_data!==C[out_addr]) begin
                        if(errors<10) $display("ERROR C[%0d]=%0d expected %0d",out_addr,$signed(out_data),$signed(C[out_addr]));
                        errors=errors+1; end
                    written[out_addr]=1; outs=outs+1;
                end
            end
        end
        // next-cycle ready signals (stall patterns)
        mem_req_ready <= (STALLS==0 ? 1 : STALLS==1 ? ((cyc%3)!=2) : (($random(seed)&3)!=0))
                         && !(MEMMAXOUT>0 && (qcnt + (req_fire?1:0) - (rsp_fire?1:0)) >= MEMMAXOUT);
        out_ready <= (OSTALLS==0 ? 1 : OSTALLS==1 ? ((cyc%4)!=3) : (($random(seed)&3)!=0));
    end

    integer r, t0, cycles, i;
    reg [1023:0] dir;
    // Waveform: run with +VCD=wave.vcd and open the file in GTKWave.
    // Only the top level, the loader and the control are dumped to keep the file small.
    reg [1023:0] vcdfile;
    initial if($value$plusargs("VCD=%s",vcdfile)) begin
        $dumpfile(vcdfile);
        $dumpvars(1, tb_local);
        $dumpvars(1, dut);
        $dumpvars(1, dut.loader);
        $dumpvars(1, dut.control);
    end

    initial begin
        if(!$value$plusargs("CASEDIR=%s",dir)) begin $display("need +CASEDIR"); $finish; end
        if(!$value$plusargs("MEMMAXOUT=%d",MEMMAXOUT)) MEMMAXOUT=0;
        $readmemh({dir,"/config.hex"},cfg);
        M=cfg[0];N=cfg[1];K=cfg[2];LAT=cfg[3];STALLS=cfg[4];OSTALLS=cfg[5];RUNS=cfg[6];
        $readmemh({dir,"/a.hex"},A); $readmemh({dir,"/b.hex"},B); $readmemh({dir,"/c.hex"},C);
        mem_req_ready=1; out_ready=1;
        repeat(5) @(posedge clk); rst<=0;
        for(r=0;r<RUNS;r=r+1) begin
            for(i=0;i<M*N;i=i+1) written[i]=0;
            a_reads=0;b_reads=0;wait_cyc=0;mac_slots=0;outs=0;
            @(posedge clk); cfg_m<=M; cfg_n<=N; cfg_k<=K; start<=1; t0=cyc;
            @(posedge clk); start<=0;
            while(!done) begin
                @(posedge clk);
                if(cyc-t0 > 60000000) begin $display("ERROR timeout"); errors=errors+1; $finish; end
            end
            cycles=cyc-t0;
            if(error) begin $display("ERROR dut error flag"); errors=errors+1; end
            repeat(3) @(posedge clk);
            if(outs!=M*N) begin $display("ERROR %0d outputs, expected %0d",outs,M*N); errors=errors+1; end
            $display("RESULT run=%0d m=%0d n=%0d k=%0d lat=%0d cycles=%0d a_reads=%0d b_reads=%0d operand_wait=%0d mac_slots=%0d max_outstanding=%0d errors=%0d",
                r,M,N,K,LAT,cycles,a_reads,b_reads,wait_cyc,mac_slots,max_out_seen,errors);
        end
        if(errors==0) $display("LOCAL PASS"); else $display("LOCAL FAIL");
        $finish;
    end
endmodule
