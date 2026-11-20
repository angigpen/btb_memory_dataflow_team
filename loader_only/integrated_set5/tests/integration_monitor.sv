`timescale 1ns/1ps
module integration_monitor;
    integer fd, trace_fd, reads_fd, packets_in=0, packets_out=0;
    integer memory_wait=0, frontend_stall=0, mac_stall=0, groups=0, mixed_k=0;
    integer mul_count=0, pass_count=0, neg_count=0, shift_count=0, ram_count=0;
    integer classify_values=0, op_toggles=0;
    reg [3:0] previous_tag=0;
    reg [127:0] previous_a=0;
    reg [31:0] previous_b=0;
    reg [7:0] previous_k=0;
    reg [15:0] previous_mask=0;
    integer i,j,c,r,tag_value,offset,read_a=0,read_b=0;
    reg [7:0] k_offsets;
    reg pending_b;
    reg [31:0] pending_address;
    reg trace_on=0, dumping=0;
    reg [31:0] captured_n=1, captured_k=1;
    reg [31:0] input_row[0:7], input_col[0:7];

    rv_monitor #(.WIDTH(16*8+4*8+4*4+2*4+4+4+2+16)) nm_output_contract(
        tb_gemm.clk,tb_gemm.rst,tb_gemm.dut.cv,tb_gemm.dut.cr,
        {tb_gemm.dut.ca,tb_gemm.dut.cb,tb_gemm.dut.cb_tag,
         tb_gemm.dut.column_k_position,tb_gemm.dut.crm,tb_gemm.dut.ccm,
         tb_gemm.dut.cf,tb_gemm.dut.cl,tb_gemm.dut.work_mask});

    initial begin
        fd=$fopen("integration_metrics.csv","w");
        reads_fd=$fopen("reads.csv","w");
        $fwrite(reads_fd,"cycle,is_b,address\n");
        $fwrite(fd,"input_packets,compute_packets,group_events,mixed_k_packets,frontend_stall,mac_stall,memory_wait,mul,pass,neg,shift,ram_accesses,classify_values,payload_toggles\n");
        trace_on=$test$plusargs("TRACE");
        if(trace_on) begin
            trace_fd=$fopen("nm_trace.csv","w");
            $fwrite(trace_fd,"cycle,bank,row,col,k_base,a,b,k_positions,tag,work_mask,first,last\n");
        end
        if($test$plusargs("WAVE")) begin
            $dumpfile("wave.vcd");
            $dumpvars(0,tb_gemm.dut);
        end
    end

    always @(posedge tb_gemm.clk) begin
        if(tb_gemm.rst || !tb_gemm.profile) begin
            packets_in=0; packets_out=0; groups=0; mixed_k=0;
            frontend_stall=0; mac_stall=0; memory_wait=0;
            mul_count=0; pass_count=0; neg_count=0; shift_count=0;
            ram_count=0; classify_values=0; op_toggles=0;
            read_a=0;read_b=0;previous_a=0;previous_b=0;previous_k=0;previous_mask=0;
        end else begin
            if(tb_gemm.start && !tb_gemm.busy) begin
                captured_n=tb_gemm.host_n;captured_k=tb_gemm.host_k;
            end
            if(tb_gemm.qv && tb_gemm.qr) begin
                $fwrite(reads_fd,"%0d,%0d,%0d\n",tb_gemm.tick,tb_gemm.qb,tb_gemm.qa);
            end
            if(tb_gemm.sr && !tb_gemm.sv) memory_wait=memory_wait+1;
            if(tb_gemm.dut.ov && !tb_gemm.dut.ordy) frontend_stall=frontend_stall+1;
            if(tb_gemm.dut.cv && !tb_gemm.dut.cr) mac_stall=mac_stall+1;
            if(tb_gemm.dut.loader.aw_pend) ram_count=ram_count+1;
            if(tb_gemm.dut.loader.bw_pend) ram_count=ram_count+1;
            if(tb_gemm.dut.loader.accept) begin
                if(tb_gemm.dut.loader.hit_a) ram_count=ram_count+1;
                if(tb_gemm.dut.loader.hit_b) ram_count=ram_count+1;
            end
            if(tb_gemm.dut.ov && tb_gemm.dut.ordy) begin
                packets_in=packets_in+1;
                i=4*tb_gemm.dut.sparsity.write_slot+tb_gemm.dut.obank;
                input_row[i]=tb_gemm.dut.loader.s_aaddr[tb_gemm.dut.loader.rd]/captured_k;
                input_col[i]=tb_gemm.dut.loader.s_baddr[tb_gemm.dut.loader.rd]%captured_n;
                // The frontend records shared B only on bank zero (or K=1).
                if(tb_gemm.dut.obank==0 || (tb_gemm.dut.ofirst && tb_gemm.dut.olast))
                    for(c=0;c<4;c=c+1) if(tb_gemm.dut.ocm[c]) classify_values=classify_values+1;
            end
            if(tb_gemm.dut.group_end) groups=groups+1;
            if(tb_gemm.dut.cv && tb_gemm.dut.cr) begin
                packets_out=packets_out+1;
                if(tb_gemm.dut.compute.cur_idx>=tb_gemm.dut.NBANK)
                    $fatal(1,"Physical accumulator out of range");
                if(!(tb_gemm.dut.cf && tb_gemm.dut.cl) &&
                   tb_gemm.dut.compute.cur_idx!=tb_gemm.dut.cbank)
                    $fatal(1,"Logical/physical accumulator mismatch");
                k_offsets=tb_gemm.dut.column_k_position;
                if((k_offsets[1:0]!=k_offsets[3:2]) ||
                   (k_offsets[1:0]!=k_offsets[5:4]) ||
                   (k_offsets[1:0]!=k_offsets[7:6])) mixed_k=mixed_k+1;
                for(i=0;i<128;i=i+1) if(previous_a[i]!=tb_gemm.dut.ca[i]) op_toggles=op_toggles+1;
                for(i=0;i<32;i=i+1) if(previous_b[i]!=tb_gemm.dut.cb[i]) op_toggles=op_toggles+1;
                for(i=0;i<8;i=i+1) if(previous_k[i]!=k_offsets[i]) op_toggles=op_toggles+1;
                for(i=0;i<16;i=i+1) if(previous_mask[i]!=tb_gemm.dut.work_mask[i]) op_toggles=op_toggles+1;
                previous_a=tb_gemm.dut.ca;previous_b=tb_gemm.dut.cb;
                previous_k=k_offsets;previous_mask=tb_gemm.dut.work_mask;
                for(c=0;c<4;c=c+1) begin
                    tag_value=tb_gemm.dut.cb_tag[4*c +: 4];
                    for(r=0;r<4;r=r+1) if(tb_gemm.dut.work_mask[4*r+c]) begin
                        if(tag_value==1) mul_count=mul_count+1;
                        else if(tag_value==2) pass_count=pass_count+1;
                        else if(tag_value==3) neg_count=neg_count+1;
                        else if(tag_value>=9 && tag_value<=14) shift_count=shift_count+1;
                        else $fatal(1,"Active PE has invalid B tag");
                    end
                end
                if(trace_on) $fwrite(trace_fd,"%0d,%0d,%0d,%0d,%0d,%h,%h,%h,%h,%h,%0d,%0d\n",
                    tb_gemm.tick,tb_gemm.dut.cbank,input_row[4*tb_gemm.dut.sparsity.read_slot+tb_gemm.dut.cbank],
                    input_col[4*tb_gemm.dut.sparsity.read_slot+tb_gemm.dut.cbank],tb_gemm.dut.group_k_base,
                    tb_gemm.dut.ca,tb_gemm.dut.cb,k_offsets,tb_gemm.dut.cb_tag,
                    tb_gemm.dut.work_mask,tb_gemm.dut.cf,tb_gemm.dut.cl);
            end
        end
    end

    always @(negedge tb_gemm.profile) if(packets_in>0)
        $fwrite(fd,"%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d\n",
            packets_in,packets_out,groups,mixed_k,frontend_stall,mac_stall,memory_wait,
            mul_count,pass_count,neg_count,shift_count,ram_count,classify_values,op_toggles);
endmodule
