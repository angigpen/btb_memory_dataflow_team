`timescale 1ns/1ps
// Collect K groups (4 positions) in Set 4 order.
// MAC-compatible A packing: out_a contains all 4 K-position A vectors.
// B is selected independently per column; out_k_mask is the per-column
// K-position INDEX connected to MAC.column_k_position (not a bit mask).
// Two buffers let the loader fill the next group while the MAC reads this one.
module sparsity_frontend #(parameter PE_ROWS=4,PE_COLS=4,
    parameter NBANK=4, BKW=(NBANK>1)?$clog2(NBANK):1)(
    input clk,rst,
    input in_valid,output in_ready,
    input [8*PE_ROWS-1:0] in_a,input [8*PE_COLS-1:0] in_b,
    input [PE_ROWS-1:0] in_rows,input [PE_COLS-1:0] in_cols,
    input in_first,in_last,
    input [BKW-1:0] in_bank,
    output out_valid,input out_ready,
    // out_a layout: K0 A[rows], K1 A[rows], K2 A[rows], K3 A[rows].
    // Width is 4 * 8 * PE_ROWS, matching mac_array_compute.op_a.
    output [4*8*PE_ROWS-1:0] out_a,output [8*PE_COLS-1:0] out_b,
    output [PE_ROWS-1:0] out_rows,output [PE_COLS-1:0] out_cols,
    output out_first,out_last,output [PE_ROWS*PE_COLS-1:0] work_mask,
    output [4*PE_COLS-1:0] out_b_tag,
    // Two-bit K INDEX per B column, paired with out_b and out_b_tag.
    // Connect out_k_mask directly to MAC.column_k_position.
    // PE_COLS=4: col0=[1:0], col1=[3:2], col2=[5:4], col3=[7:6].
    // Actual K is out_k + out_k_mask[2*c +: 2].
    // An inactive column has offset 0, B tag ZERO and all PE masks clear.
    output [2*PE_COLS-1:0] out_k_mask,
    output [15:0] out_k,
    output [BKW-1:0] out_bank,
    // One observation per original group and ACC, including skipped groups.
    output out_group_end,output [2:0] out_group_len,
    output [4*PE_ROWS-1:0] out_group_mask_a,
    output [4*PE_COLS-1:0] out_group_mask_b,
    output [2*PE_ROWS-1:0] out_group_mode_a,
    output [2*PE_COLS-1:0] out_group_mode_b
);
    reg [1:0] full;
    reg write_slot, read_slot;
    reg [BKW-1:0] read_bank_offset, last_input_bank;
    reg [1:0] read_round;
    reg [15:0] next_k[0:NBANK-1];

    reg [8*PE_ROWS-1:0] a_values[0:1][0:NBANK-1][0:3];
    reg [8*PE_COLS-1:0] b_values[0:1][0:3];
    reg [4*PE_COLS-1:0] b_tags[0:1][0:3];
    reg [4*PE_ROWS-1:0] a_positions[0:1][0:NBANK-1];
    reg [4*PE_COLS-1:0] b_positions[0:1];
    reg [PE_ROWS-1:0] rows[0:1][0:NBANK-1];
    reg [PE_COLS-1:0] cols[0:1];
    reg [BKW-1:0] first_bank[0:1], last_bank[0:1];
    reg [15:0] base_k[0:1];
    reg [2:0] group_len[0:1];
    reg [1:0] first_group, last_group;

    wire [15:0] in_k=in_first ? 16'd0 : next_k[in_bank];
    wire [1:0] in_pos=in_k[1:0];
    wire single_k=in_first && in_last;
    // For K=1 each packet completes its own tile. For larger K the K=0
    // packets establish the bank count before any group can be sealed.
    wire seal=(in_pos==2'd3 || in_last) &&
              (single_k || in_bank==last_input_bank);
    wire store_b=(in_bank==0) || single_k;
    assign in_ready=!rst && !full[write_slot];
    wire accept_input=in_valid && in_ready;

    // 00=0:4, 01=1:4, 10=2:4, 11=dense capacity (3 or 4 NNZ).
    // Mode is NOT an ALU opcode and does not replace the position bitmap.
    function [1:0] choose_mode;
        input [3:0] positions;
        reg [2:0] nnz;
        begin
            nnz={2'd0,positions[0]}+{2'd0,positions[1]}+
                {2'd0,positions[2]}+{2'd0,positions[3]};
            if(nnz==0) choose_mode=2'b00;
            else if(nnz==1) choose_mode=2'b01;
            else if(nnz==2) choose_mode=2'b10;
            else choose_mode=2'b11;
        end
    endfunction
    // INTEGRATED CHANGE: ten states need at least four bits. Shift tags
    // have bit3=1 and carry n directly in [2:0]. No separate exponent bus.
    // 0=ZERO, 1=MUL, 2=PASS(+1), 3=NEG(-1), 9..E=SHL1..SHL6.
    function [3:0] classify_b;
        input [7:0] value;
        begin
            case(value)
                8'h00: classify_b=4'h0;
                8'h01: classify_b=4'h2;
                8'hff: classify_b=4'h3;
                8'h02: classify_b=4'h9;
                8'h04: classify_b=4'ha;
                8'h08: classify_b=4'hb;
                8'h10: classify_b=4'hc;
                8'h20: classify_b=4'hd;
                8'h40: classify_b=4'he;
                default: classify_b=4'h1;
            endcase
        end
    endfunction

    // Pick the nth set bit in ascending K order. Bit 2 says a lane exists.
    function [2:0] select_k;
        input [3:0] positions;
        input [1:0] round_index;
        integer q, rank;
        begin
            select_k=3'd0;
            rank=0;
            for(q=0;q<4;q=q+1) begin
                if(positions[q]) begin
                    if(rank==round_index) select_k={1'b1,q[1:0]};
                    rank=rank+1;
                end
            end
        end
    endfunction

    wire [4*PE_ROWS-1:0] old_a_positions=a_positions[write_slot][in_bank];
    wire [4*PE_COLS-1:0] old_b_positions=b_positions[write_slot];
    wire [4*PE_ROWS-1:0] new_a_positions;
    wire [4*PE_COLS-1:0] new_b_positions;
    wire [4*PE_COLS-1:0] input_tags;
    wire [2:0] needed_rounds[0:PE_COLS-1];
    reg [2:0] round_count;

    wire [BKW-1:0] read_bank=first_bank[read_slot]+read_bank_offset;
    assign out_bank=read_bank;
    assign out_k=base_k[read_slot];
    assign out_rows=rows[read_slot][read_bank];
    assign out_cols=cols[read_slot];
    assign out_group_len=group_len[read_slot];
    assign out_group_mask_a=a_positions[read_slot][read_bank];
    assign out_group_mask_b=b_positions[read_slot];

    // A zero-only middle group has no MAC operations. A first/last zero
    // group still initializes or publishes the accumulator in bank order.
    wire skip_group=(round_count==0) && !first_group[read_slot] && !last_group[read_slot];
    wire final_round=(round_count<=1) || ({1'b0,read_round}==round_count-3'd1);
    wire final_bank=(read_bank==last_bank[read_slot]);
    assign out_valid=!rst && full[read_slot] && !skip_group;
    assign out_first=first_group[read_slot] && (read_round==0);
    assign out_last=last_group[read_slot] && final_round;
    wire advance=!rst && full[read_slot] && (skip_group || out_ready);
    assign out_group_end=advance && final_round;

    genvar r,c,q;
    generate
        for(r=0;r<PE_ROWS;r=r+1) begin: row_mask
            assign out_group_mode_a[2*r +: 2]=choose_mode(out_group_mask_a[4*r +: 4]);
            for(q=0;q<4;q=q+1) begin: position
                assign new_a_positions[4*r+q]=(in_pos==q) ?
                    (in_rows[r] && (|in_a[8*r +: 8])) :
                    (in_pos!=0 && old_a_positions[4*r+q]);

                // IMPORTANT: A is packed by K position, NOT by B column.
                // MAC picks A with op_a[8*(PE_ROWS*k+r) +: 8],
                // where k = column_k_position[2*c +: 2].
                // Mask invalid or unfilled K/row entries to prevent stale data.
                assign out_a[8*(PE_ROWS*q+r) +: 8] =
                    out_group_mask_a[4*r+q] ?
                    a_values[read_slot][read_bank][q][8*r +: 8] : 8'd0;
            end
        end
        for(c=0;c<PE_COLS;c=c+1) begin: column
            assign input_tags[4*c +: 4]=in_cols[c] ? classify_b(in_b[8*c +: 8]) : 4'd0;
            for(q=0;q<4;q=q+1) begin: position
                assign new_b_positions[4*c+q]=(in_pos==q) ?
                    (|input_tags[4*c +: 4]) : (in_pos!=0 && old_b_positions[4*c+q]);
            end

            wire [3:0] positions=out_group_mask_b[4*c +: 4];
            wire [1:0] mode=choose_mode(positions);
            assign out_group_mode_b[2*c +: 2]=mode;
            assign needed_rounds[c]=(mode==2'd3) ? out_group_len : {1'b0,mode};
            wire [2:0] lane_k=(mode==2'd3) ?
                {({1'b0,read_round}<out_group_len),read_round} : select_k(positions,read_round);
            wire lane_live=lane_k[2];
            wire [1:0] k_pos=lane_k[1:0];
            assign out_k_mask[2*c +: 2]=lane_live ? k_pos : 2'd0;
            assign out_b[8*c +: 8]=lane_live ? b_values[read_slot][k_pos][8*c +: 8] : 8'd0;
            assign out_b_tag[4*c +: 4]=lane_live ? b_tags[read_slot][k_pos][4*c +: 4] : 4'd0;
            for(r=0;r<PE_ROWS;r=r+1) begin: mask
                assign work_mask[r*PE_COLS+c]=lane_live && positions[k_pos] &&
                    out_group_mask_a[4*r+k_pos];
            end
        end
    endgenerate

    integer col;
    always @* begin
        round_count=0;
        for(col=0;col<PE_COLS;col=col+1)
            if(needed_rounds[col]>round_count) round_count=needed_rounds[col];
    end

    integer bank;
    always @(posedge clk) begin
        if(rst) begin
            full<=0;
            write_slot<=0;
            read_slot<=0;
            read_bank_offset<=0;
            read_round<=0;
            last_input_bank<=0;
            for(bank=0;bank<NBANK;bank=bank+1) next_k[bank]<=0;
        end else begin
            if(accept_input) begin
                a_values[write_slot][in_bank][in_pos]<=in_a;
                a_positions[write_slot][in_bank]<=new_a_positions;
                rows[write_slot][in_bank]<=in_rows;
                next_k[in_bank]<=in_last ? 16'd0 : in_k+16'd1;
                if(in_first) last_input_bank<=in_bank;
                if(store_b) begin
                    b_values[write_slot][in_pos]<=in_b;
                    b_tags[write_slot][in_pos]<=input_tags;
                    b_positions[write_slot]<=new_b_positions;
                    cols[write_slot]<=in_cols;
                end
                if(seal) begin
                    full[write_slot]<=1'b1;
                    base_k[write_slot]<={in_k[15:2],2'b00};
                    group_len[write_slot]<={1'b0,in_pos}+3'd1;
                    first_group[write_slot]<=(in_k<16'd4);
                    last_group[write_slot]<=in_last;
                    first_bank[write_slot]<=single_k ? in_bank : {BKW{1'b0}};
                    last_bank[write_slot]<=in_bank;
                    write_slot<=~write_slot;
                end
            end
            if(advance) begin
                if(final_bank) begin
                    read_bank_offset<=0;
                    if(final_round) begin
                        full[read_slot]<=1'b0;
                        read_slot<=~read_slot;
                        read_round<=0;
                    end else begin
                        read_round<=read_round+2'd1;
                    end
                end else begin
                    read_bank_offset<=read_bank_offset+1'b1;
                end
            end
        end
    end
endmodule