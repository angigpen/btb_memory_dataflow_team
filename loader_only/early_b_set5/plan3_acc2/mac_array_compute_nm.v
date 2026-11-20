`timescale 1ns/1ps
module mac_array_compute #(parameter PE_ROWS = 4, PE_COLS=4, ACC_dup=0, PE_Folding=0, REUSE=2)(
    input clk, rst, input op_valid, output op_ready,
    input [4*8*PE_ROWS-1:0] op_a, input [8*PE_COLS-1:0] op_b, input [4*PE_COLS-1:0] op_b_tag, //a input tile : 32bit*4 
    input [PE_ROWS-1:0] op_rows, input [PE_COLS-1:0] op_cols, input op_first, op_last,
    input [4*PE_COLS-1:0] work_mask, output reg result_valid, input result_ready,
    output reg [32*4*PE_COLS-1:0] result_data, output reg [4*PE_COLS-1:0] result_mask,
    output [4*PE_COLS-1:0] event_mac, event_nonzero_mac,

    input [2*PE_COLS-1:0] column_k_position // B input, K position (8bit)
);
    localparam P=PE_ROWS*PE_COLS;
    localparam PHASE_ROWS = PE_Folding ? (PE_ROWS/2) : PE_ROWS;
    localparam MAC=PHASE_ROWS*PE_COLS;
    // REUSE is parameterized for the memory team ACC=2/4 variants.
    localparam BANKS = ((REUSE>1)&&(ACC_dup))? $clog2(REUSE) : 1;

    reg phase; // for PE folding
    reg  [BANKS-1:0] acc_idx; reg  prev_last; reg [BANKS-1:0] grp_last; //acc idx logic
    wire group_start = op_first && prev_last;
    wire [BANKS-1:0] cur_idx = (!ACC_dup || group_start) ? {BANKS{1'b0}} : 
                                   op_first    ? acc_idx + 1'b1 : 
                                   ((acc_idx == grp_last) ? {BANKS{1'b0}} : acc_idx + 1'b1);
    reg signed [31:0] acc[0:P-1][0:REUSE-1];   //[#PE][#set]
    wire signed [15:0] product[0:P-1];
    wire signed [31:0] next_sum[0:P-1];
    wire [P-1:0] mask;

    // acc choose logic
    always @(posedge clk) begin
        if(rst) begin acc_idx<=0; prev_last<=1'b1; grp_last<=0; end
        else if(ACC_dup && op_valid && op_ready) begin
            acc_idx<=cur_idx;
            prev_last<=op_last;
            if(op_first) grp_last<=cur_idx;
        end
    end

    // Without folding, accept the operation in a single handshake.
    assign op_ready=(!PE_Folding || phase) && (!result_valid || result_ready) && !rst;
    genvar c,r;
    generate for(c=0;c<PE_COLS;c=c+1) begin: col
        wire [1:0] k;
        assign k = column_k_position[2*c +: 2];

        //측정용 event들
        for(r=0;r<PE_ROWS;r=r+1) begin: eventRow
            localparam I=r*PE_COLS+c;
            wire signed [7:0] a_in;
            assign a_in = op_a[8*(PE_ROWS*k+r) +: 8];
            assign mask[I]=op_rows[r] && op_cols[c];
            assign event_mac[I]=op_valid && !rst && mask[I] && work_mask[I] && ((PE_Folding && r<PHASE_ROWS) ? !phase : op_ready);
            assign event_nonzero_mac[I]=event_mac[I] && (a_in!=0) && (op_b_tag[c*4 +: 4]!=0);
        end
        
        //PE 연산
        for(r=0;r<PHASE_ROWS;r=r+1) begin: macRow
            localparam I=r*PE_COLS+c;
            wire signed [7:0] operand_a;
            assign operand_a = op_a[8*(PE_ROWS*k+phase*PHASE_ROWS+r) +: 8];
            product product_unit (.a(operand_a), .b(op_b[c*8 +: 8]), .b_tag(op_b_tag[c*4 +: 4]), .p(product[I]));
            assign next_sum[I]=(op_first?32'sd0:acc[phase*MAC+I][cur_idx])+(work_mask[phase*MAC+I]?{{16{product[I][15]}},product[I]}:32'sd0);
        end
    end endgenerate

    integer i,j;
    always @(posedge clk) begin
        if(rst) begin
            result_valid<=0; result_data<=0; result_mask<=0; phase<=0;
            for(i=0;i<P;i=i+1)
                for(j=0;j<REUSE;j=j+1) acc[i][j]<=0;
        end else begin
            if(result_valid && result_ready) result_valid<=0;
            if(PE_Folding && op_valid && !phase) begin
                for(i=0;i<MAC;i=i+1) acc[i][cur_idx]<=mask[i]?next_sum[i]:32'sd0;
                phase<=1;
            end
            if(op_valid && op_ready) begin
                if(PE_Folding) begin
                    for(i=0;i<MAC;i=i+1) begin
                        acc[MAC+i][cur_idx]<=mask[MAC+i]?next_sum[i]:32'sd0;
                        if(op_last) begin
                            result_data[32*i +: 32]<=acc[i][cur_idx];
                            result_data[32*(MAC+i) +: 32]<=mask[MAC+i]?next_sum[i]:32'sd0;
                        end
                    end
                    phase<=0;
                end else begin
                    for(i=0;i<P;i=i+1) begin
                        acc[i][cur_idx]<=mask[i]?next_sum[i]:32'sd0;
                        if(op_last) result_data[32*i +: 32]<=mask[i]?next_sum[i]:32'sd0;
                    end
                end
                if(op_last) begin result_valid<=1; result_mask<=mask; end
            end
        end
    end
endmodule

module mul (input wire enable, input wire signed [7:0] a, b, output wire signed [15:0] p);
    wire signed [7:0] active_a = enable ? a : 8'sd0;
    wire signed [7:0] active_b = enable ? b : 8'sd0;
    function signed [9:0] booth_pp;
        input signed [7:0] multiplicand;
        input        [2:0] code;
        reg signed [9:0] a_ext;
        begin
            a_ext = {{2{multiplicand[7]}}, multiplicand};
            case (code)
                3'b000, 3'b111: booth_pp = 10'sd0;
                3'b001, 3'b010: booth_pp = a_ext;
                3'b011:         booth_pp = a_ext <<< 1;
                3'b100:         booth_pp = -(a_ext <<< 1);
                3'b101, 3'b110: booth_pp = -a_ext;
                default:        booth_pp = 10'sd0;
            endcase
        end
    endfunction
    wire signed [9:0] pp0, pp1, pp2, pp3;
    assign pp0 = booth_pp(active_a, {active_b[1:0], 1'b0});
    assign pp1 = booth_pp(active_a, active_b[3:1]);
    assign pp2 = booth_pp(active_a, active_b[5:3]);
    assign pp3 = booth_pp(active_a, active_b[7:5]);
    // Sign extension and alignment: shifts of 0, 2, 4, 6.
    wire [15:0] row0, row1, row2, row3;
    assign row0 = {{6{pp0[9]}}, pp0};
    assign row1 = {{4{pp1[9]}}, pp1, 2'b0};
    assign row2 = {{2{pp2[9]}}, pp2, 4'b0};
    assign row3 = {pp3, 6'b0};
    // CSA stage 1: three rows -> two rows.
    wire [15:0] sum1, carry1;
    assign sum1   = row0 ^ row1 ^ row2;
    assign carry1 = ((row0 & row1) | (row1 & row2) | (row2 & row0)) << 1;
    // CSA stage 2: three rows -> two rows.
    wire [15:0] sum2, carry2;
    assign sum2   = sum1 ^ carry1 ^ row3;
    assign carry2 = ((sum1 & carry1) | (carry1 & row3) | (row3   & sum1)) << 1;
    // Final carry-propagate adder.
    assign p = sum2 + carry2;
endmodule

module product (input wire signed [7:0] a, b, input wire [3:0] b_tag, output reg signed [15:0] p);
    wire mul_enable = (b_tag == 4'b0001);
    wire signed [15:0] mul_p;
    mul mul_unit (.enable(mul_enable), .a(a), .b(b), .p(mul_p));
    wire signed [15:0] a_ext = {{8{a[7]}}, a};
    always @* begin
        if (b_tag[3]) begin
            p = a_ext <<< b_tag[2:0];
        end else begin
            case (b_tag[2:0])
                3'b001: p = mul_p;
                3'b010: p = a_ext;
                3'b011: p = -a_ext;
                default: p = 16'sd0;
            endcase
        end
    end
endmodule
