`timescale 1ns/1ps

`ifndef PR
`define PR 4
`endif
`ifndef PC
`define PC 4
`endif
`ifndef DATA_DIR
`define DATA_DIR "."
`endif

module tb_gemm;
    localparam R=`PR, C=`PC, P=R*C;
    reg clk=0,rst=1,start=0;
    always #5 clk=~clk;
    reg [15:0] m=0,n=0,k=0;
    reg [15:0] host_m=0,host_n=0,host_k=0;
    wire busy,done,error,qv,qr,qb,sv,sr,ov,ordy;
    wire [31:0] qa,oa,od;
    wire [7:0] sd;
    wire [P-1:0] mac_event,nonzero_event;
    wire operand_wait,compute_stall;
    reg rsp_valid=0,pending=0;
    reg [7:0] rsp_data=0,pending_data=0;
    integer delay_left=0,tick=0;
    reg [31:0] rng=1;
    reg [7:0] a_mem[0:65535],b_mem[0:131071];
    reg [31:0] golden[0:8191],config_data[0:8];
    reg seen[0:8191];
    integer latency=1,stalls=0,output_stalls=0,runs=1;
    integer cycles=0,mac_cycles=0,slots=0,nonzero_slots=0,reads_a=0,reads_b=0;
    integer req_stall=0,rsp_stall=0,wait_cycles=0,compute_wait=0,out_stall=0,outputs=0;
    integer per_pe[0:P-1];
    integer i,run_id,fd,pfd,unused,expected_a,expected_b,invalid_id;
    longint unsigned timeout,max_runtime_cycles,watchdog_cycles;
    reg config_loaded=0;
    reg profile=0,held_q=0,held_o=0,done_d=0;
    reg [32:0] saved_q;
    reg [63:0] saved_o;
    assign qr=!rst && !pending && !rsp_valid &&
        ((stalls==0) || (stalls==1 && tick%5!=0) || (stalls==2 && (rng[0] || rng[3])));
    assign sv=rsp_valid; assign sd=rsp_data;
    assign ordy=!rst && ((output_stalls==0) ||
        (output_stalls==1 && tick%13>=8) || (output_stalls==2 && (rng[1] || rng[7])));
    gemm_top #(.PE_ROWS(R),.PE_COLS(C)) dut(
        .clk(clk),.rst(rst),.start(start),.cfg_m(host_m),.cfg_n(host_n),.cfg_k(host_k),
        .busy(busy),.done(done),.error(error),
        .mem_req_valid(qv),.mem_req_ready(qr),.mem_req_is_b(qb),.mem_req_addr(qa),
        .mem_rsp_valid(sv),.mem_rsp_ready(sr),.mem_rsp_data(sd),
        .out_valid(ov),.out_ready(ordy),.out_addr(oa),.out_data(od),
        .event_mac(mac_event),.event_nonzero_mac(nonzero_event),
        .event_operand_wait(operand_wait),.event_compute_stall(compute_stall));

    rv_monitor #(.WIDTH(82+R+C)) descriptor_contract(clk,rst,dut.dv,dut.dr,
        {dut.aa,dut.ba,dut.astride,dut.rm,dut.cm,dut.first,dut.last});
    rv_monitor #(.WIDTH(9*(R+C)+2)) operand_contract(clk,rst,dut.ov,dut.ordy,
        {dut.oa,dut.ob,dut.orm,dut.ocm,dut.ofirst,dut.olast});
    rv_monitor #(.WIDTH(9*(R+C)+2+P)) compute_contract(clk,rst,dut.cv,dut.cr,
        {dut.ca,dut.cb,dut.crm,dut.ccm,dut.cf,dut.cl,dut.work_mask});
    rv_monitor #(.WIDTH(33*P)) result_contract(clk,rst,dut.rv,dut.rr,{dut.data,dut.mask});

    // One outstanding request, in-order response; response held until accepted.
    always @(posedge clk) begin
        if(rst) begin pending<=0;rsp_valid<=0;tick<=0;held_q<=0;held_o<=0;done_d<=0;end
        else begin
            tick<=tick+1;
            rng<={rng[30:0],rng[31]^rng[21]^rng[1]^rng[0]};
            if(sv && sr) rsp_valid<=0;
            if(pending) begin
                if(delay_left==0) begin rsp_valid<=1;rsp_data<=pending_data;pending<=0;end
                else delay_left<=delay_left-1;
            end
            if(qv && qr) begin
                if((^{qb,qa})===1'bx) $fatal(1,"Unknown memory address");
                if(pending || rsp_valid) $fatal(1,"Multiple outstanding requests");
                if((!qb && qa>=m*k) || (qb && qa>=k*n)) $fatal(1,"Read address out of range");
                pending_data<=qb?b_mem[qa]:a_mem[qa];pending<=1;
                delay_left<=latency-1+((stalls==2)?(rng[5:4]):0);
            end
            if(held_q && (!qv || {qb,qa}!==saved_q)) $fatal(1,"Request unstable");
            if(held_o && (!ov || {oa,od}!==saved_o)) $fatal(1,"Output unstable");
            held_q<=qv&&!qr;saved_q<={qb,qa};
            held_o<=ov&&!ordy;saved_o<={oa,od};
            if(done && done_d) $fatal(1,"done wider than one cycle");
            done_d<=done;
            if(profile && busy) begin
                cycles=cycles+1;
                if(|mac_event) mac_cycles=mac_cycles+1;
                for(integer j=0;j<P;j=j+1) begin
                    if(mac_event[j]) begin slots=slots+1;per_pe[j]=per_pe[j]+1;end
                    if(nonzero_event[j]) nonzero_slots=nonzero_slots+1;
                end
                if(qv&&qr) begin if(qb) reads_b=reads_b+1;else reads_a=reads_a+1;end
                if(qv&&!qr) req_stall=req_stall+1;
                if(sr&&!sv) rsp_stall=rsp_stall+1;
                if(operand_wait) wait_cycles=wait_cycles+1;
                if(compute_stall) compute_wait=compute_wait+1;
                if(ov&&!ordy) out_stall=out_stall+1;
                if(ov&&ordy) begin
                    if((^{oa,od})===1'bx) $fatal(1,"Unknown output address/data");
                    if(oa>=m*n) $fatal(1,"Output address out of bounds");
                    if(seen[oa]) $fatal(1,"Duplicate output");
                    if(od!==golden[oa]) $fatal(1,"Mismatch addr=%0d got=%0d expected=%0d",oa,$signed(od),$signed(golden[oa]));
                    seen[oa]=1;outputs=outputs+1;
                end
            end
        end
    end

    task clear_counters;
        begin
            cycles=0;mac_cycles=0;slots=0;nonzero_slots=0;reads_a=0;reads_b=0;
            req_stall=0;rsp_stall=0;wait_cycles=0;compute_wait=0;out_stall=0;outputs=0;
            for(i=0;i<8192;i=i+1) seen[i]=0;
            for(i=0;i<P;i=i+1) per_pe[i]=0;
        end
    endtask

    task launch_and_check(input integer id);
        begin
            @(negedge clk);clear_counters();profile=1;host_m=m;host_n=n;host_k=k;start=1;
            @(negedge clk);start=0;timeout=0;
            if($test$plusargs("HOST_TEST")) begin
                // Configuration and a second start while busy must be ignored.
                host_m=0;host_n=0;host_k=0;start=1;
                @(negedge clk);start=0;
            end
            while(!done && timeout<max_runtime_cycles) begin @(negedge clk);timeout=timeout+1;end
            profile=0;
            if(!done || error || outputs!=m*n) $fatal(1,"Timeout/error/missing result");
            for(i=0;i<m*n;i=i+1) if(!seen[i]) $fatal(1,"Missing address");
            expected_a=m*k*((n+C-1)/C);expected_b=n*k*((m+R-1)/R);
            // Only baseline runs enforce traffic/work formulas. Optimized RTL
            // still must pass the full output and protocol scoreboard.
            if($test$plusargs("SPARSE_MODEL")) begin
                if(reads_a!=expected_a || reads_b!=expected_b || slots!=m*n*k-config_data[8] ||
                   nonzero_slots!=slots) $fatal(1,"Sparse integration counter mismatch");
            end else if(!$test$plusargs("OPTIMIZED")) begin
                if(reads_a!=expected_a || reads_b!=expected_b || slots!=m*n*k)
                    $fatal(1,"Baseline traffic/MAC counter mismatch");
                if(nonzero_slots!=m*n*k-config_data[8]) $fatal(1,"Nonzero counter mismatch");
                if(mac_cycles!=((m+R-1)/R)*((n+C-1)/C)*k) $fatal(1,"MAC cycle mismatch");
            end
            $fdisplay(fd,"%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                id,m,n,k,R,C,cycles,mac_cycles,slots,nonzero_slots,reads_a,reads_b,req_stall,rsp_stall,wait_cycles,compute_wait,out_stall);
            for(i=0;i<P;i=i+1) $fdisplay(pfd,"%0d,%0d,%0d",id,i,per_pe[i]);
            $display("PASS run=%0d M=%0d N=%0d K=%0d cycles=%0d MACs=%0d",id,m,n,k,cycles,slots);
            repeat(4) begin @(negedge clk);if(busy||done||ov||qv) $fatal(1,"Unexpected activity after completion");end
        end
    endtask

    initial begin
        $readmemh({`DATA_DIR,"/config.hex"},config_data);
        for(i=0;i<9;i=i+1) if((^config_data[i])===1'bx) $fatal(1,"Missing/invalid config.hex");
        if(config_data[0]<1 || config_data[0]>64 || config_data[1]<1 || config_data[1]>128 ||
           config_data[2]<1 || config_data[2]>1024 || config_data[6]<1 || config_data[6]>16 ||
           config_data[3]<1 || config_data[3]>1024 || config_data[4]>2 || config_data[5]>2)
            $fatal(1,"Invalid fixture dimensions/runs");
        m=config_data[0];n=config_data[1];k=config_data[2];
        latency=config_data[3];stalls=config_data[4];output_stalls=config_data[5];runs=config_data[6];
        rng=config_data[7]|1;
        // Bounds scale with dimensions, memory latency and physical PE shape.
        max_runtime_cycles=64'd10000+((64'd1*m+R-1)/R)*((n+C-1)/C)*k*((R+C)*(latency+32)+128);
        config_loaded=1;
        $readmemh({`DATA_DIR,"/a.hex"},a_mem,0,m*k-1);
        $readmemh({`DATA_DIR,"/b.hex"},b_mem,0,k*n-1);
        $readmemh({`DATA_DIR,"/c.hex"},golden,0,m*n-1);
        for(i=0;i<m*k;i=i+1) if((^a_mem[i])===1'bx) $fatal(1,"Invalid A fixture");
        for(i=0;i<k*n;i=i+1) if((^b_mem[i])===1'bx) $fatal(1,"Invalid B fixture");
        for(i=0;i<m*n;i=i+1) if((^golden[i])===1'bx) $fatal(1,"Invalid golden fixture");
        fd=$fopen("metrics.csv","w");pfd=$fopen("pe.csv","w");
        if(!fd||!pfd) $fatal(1,"Cannot open metrics");
        $fdisplay(fd,"run,m,n,k,pe_rows,pe_cols,cycles,mac_cycles,mac_slots,nonzero_mac_slots,a_reads,b_reads,request_stall,response_stall,operand_wait,compute_stall,output_stall");
        $fdisplay(pfd,"run,pe,mac_slots");
        repeat(4) @(negedge clk);rst=0;
        for(run_id=0;run_id<runs;run_id=run_id+1) launch_and_check(run_id);
        if($test$plusargs("RESET_TEST")) begin
            // Abort an in-flight read, then abort while C is being presented.
            @(negedge clk);host_m=m;host_n=n;host_k=k;start=1;
            @(negedge clk);start=0;
            wait(pending);@(negedge clk);rst=1;
            repeat(3) @(negedge clk);rst=0;
            launch_and_check(runs);
            @(negedge clk);host_m=m;host_n=n;host_k=k;start=1;
            @(negedge clk);start=0;
            wait(ov);@(negedge clk);rst=1;
            repeat(3) @(negedge clk);rst=0;
            launch_and_check(runs+1);
            // Reject invalid dimensions without memory activity.
            for(invalid_id=0;invalid_id<6;invalid_id=invalid_id+1) begin
                @(negedge clk);host_m=m;host_n=n;host_k=k;
                case(invalid_id)
                    0:host_m=0;1:host_n=0;2:host_k=0;
                    3:host_m=65;4:host_n=129;5:host_k=1025;
                endcase
                start=1;@(negedge clk);start=0;
                if(!done||!error||busy||qv) $fatal(1,"Invalid dimensions not rejected");
                repeat(2) @(negedge clk);
            end
            launch_and_check(runs+2);
        end
        $fclose(fd);$fclose(pfd);
        $display("GEMM REGRESSION PASS");$finish;
    end
    initial begin
        wait(config_loaded);watchdog_cycles=0;
        while(watchdog_cycles<(runs+12)*max_runtime_cycles) begin
            @(posedge clk);watchdog_cycles=watchdog_cycles+1;
        end
        $fatal(1,"Global timeout");
    end
endmodule
