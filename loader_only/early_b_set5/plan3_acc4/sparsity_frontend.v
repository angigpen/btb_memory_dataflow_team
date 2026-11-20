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
    input in_group,input [1:0] in_pos,
    input group_valid,output group_ready,output group_ticket,
    input [15:0] group_base_k,input [2:0] group_len_in,
    input [PE_COLS-1:0] group_cols,input [BKW-1:0] group_last_bank,
    input group_first,group_last,
    input b_valid,output b_ready,input b_group,input [1:0] b_pos,
    input [8*PE_COLS-1:0] b_data,input [PE_COLS-1:0] b_mask,
    input b_peek_group,input [1:0] b_peek_pos,
    output [8*PE_COLS-1:0] b_peek_word,
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
    reg [1:0] full,reserved,b_complete;
    reg allocate_slot,read_slot;
    reg [BKW-1:0] read_bank_offset;
    reg [1:0] read_round;
    reg [4:0] b_received[0:1],b_expected[0:1];
    reg [3:0] b_seen[0:1][0:PE_COLS-1];

    reg [8*PE_ROWS-1:0] a_values[0:1][0:NBANK-1][0:3];
    reg [8*PE_COLS-1:0] b_values[0:1][0:3];
    reg [4*PE_COLS-1:0] b_tags[0:1][0:3];
    reg [4*PE_ROWS-1:0] a_positions[0:1][0:NBANK-1];
    reg [4*PE_COLS-1:0] b_positions[0:1];
    reg [PE_ROWS-1:0] rows[0:1][0:NBANK-1];
    reg [PE_COLS-1:0] cols[0:1];
    reg [BKW-1:0] first_bank[0:1],last_bank[0:1];
    reg [15:0] base_k[0:1];
    reg [2:0] group_len[0:1];
    reg [1:0] first_group,last_group;
    reg [2*PE_COLS-1:0] b_modes[0:1];
    reg [PE_COLS-1:0] b_column_complete[0:1];

    function integer col_popcount(input [PE_COLS-1:0] mask);
        integer j;
        begin col_popcount=0;for(j=0;j<PE_COLS;j=j+1)col_popcount=col_popcount+mask[j];end
    endfunction
    assign group_ticket=allocate_slot;
    assign group_ready=!rst&&!reserved[allocate_slot];
    wire reserve_group=group_valid&&group_ready;
    assign in_ready=!rst&&reserved[in_group]&&!full[in_group];
    wire accept_input=in_valid&&in_ready;
    wire seal=(in_pos==group_len[in_group]-1)&&in_bank==last_bank[in_group];
    assign b_ready=!rst&&reserved[b_group]&&!full[b_group]&&!b_complete[b_group];
    wire accept_b=b_valid&&b_ready;
    assign b_peek_word=b_values[b_peek_group][b_peek_pos];

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

    wire [4*PE_ROWS-1:0] old_a_positions=a_positions[in_group][in_bank];
    wire [4*PE_COLS-1:0] old_b_positions=b_positions[b_group];
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
            assign input_tags[4*c +: 4]=classify_b(b_data[8*c +: 8]);
            for(q=0;q<4;q=q+1) begin: position
                assign new_b_positions[4*c+q]=(b_mask[c] && b_pos==q) ? (|input_tags[4*c +: 4]) : old_b_positions[4*c+q];
            end

            wire [3:0] positions=out_group_mask_b[4*c +: 4];
            wire [1:0] mode=b_modes[read_slot][2*c +: 2];
            assign out_group_mode_b[2*c +: 2]=b_modes[read_slot][2*c +: 2];
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

    integer bank,pos,lane,slot;
    always @(posedge clk) begin
        if(rst) begin
            full<=0;reserved<=0;b_complete<=0;allocate_slot<=0;
            read_slot<=0;read_bank_offset<=0;read_round<=0;
            first_group<=0;last_group<=0;
            for(slot=0;slot<2;slot=slot+1)begin
                b_received[slot]<=0;b_expected[slot]<=0;
                b_positions[slot]<=0;b_modes[slot]<=0;b_column_complete[slot]<=0;
                for(lane=0;lane<PE_COLS;lane=lane+1)b_seen[slot][lane]<=0;
                cols[slot]<=0;first_bank[slot]<=0;last_bank[slot]<=0;
                base_k[slot]<=0;group_len[slot]<=0;
                for(pos=0;pos<4;pos=pos+1)begin b_values[slot][pos]<=0;b_tags[slot][pos]<=0;end
                for(bank=0;bank<NBANK;bank=bank+1)begin
                    a_positions[slot][bank]<=0;rows[slot][bank]<=0;
                    for(pos=0;pos<4;pos=pos+1)a_values[slot][bank][pos]<=0;
                end
            end
        end else begin
            if(reserve_group)begin
                reserved[allocate_slot]<=1;b_complete[allocate_slot]<=0;
                b_received[allocate_slot]<=0;
                b_expected[allocate_slot]<=group_len_in*col_popcount(group_cols);
                b_positions[allocate_slot]<=0;b_modes[allocate_slot]<=0;
                b_column_complete[allocate_slot]<=0;
                for(lane=0;lane<PE_COLS;lane=lane+1)b_seen[allocate_slot][lane]<=0;
                cols[allocate_slot]<=group_cols;base_k[allocate_slot]<=group_base_k;
                group_len[allocate_slot]<=group_len_in;
                first_group[allocate_slot]<=group_first;last_group[allocate_slot]<=group_last;
                first_bank[allocate_slot]<=0;last_bank[allocate_slot]<=group_last_bank;
                for(pos=0;pos<4;pos=pos+1)begin
                    b_values[allocate_slot][pos]<=0;b_tags[allocate_slot][pos]<=0;
                end
                for(bank=0;bank<NBANK;bank=bank+1)begin
                    a_positions[allocate_slot][bank]<=0;rows[allocate_slot][bank]<=0;
                end
                allocate_slot<=~allocate_slot;
            end
            if(accept_b)begin
                b_positions[b_group]<=new_b_positions;
                b_received[b_group]<=b_received[b_group]+col_popcount(b_mask);
                if(b_received[b_group]+col_popcount(b_mask)==b_expected[b_group])
                    b_complete[b_group]<=1;
                for(lane=0;lane<PE_COLS;lane=lane+1)if(b_mask[lane])begin
                    b_values[b_group][b_pos][8*lane +: 8]<=b_data[8*lane +: 8];
                    b_tags[b_group][b_pos][4*lane +: 4]<=input_tags[4*lane +: 4];
                    // Each column's mode is finalized as soon as its last K arrives.
                    
`ifndef SYNTHESIS
                    if(b_seen[b_group][lane][b_pos])$fatal(1,"Duplicate B coordinate");
`endif
                    b_seen[b_group][lane]<=b_seen[b_group][lane]|(4'b0001<<b_pos);
                    if((b_seen[b_group][lane]|(4'b0001<<b_pos))==((1<<group_len[b_group])-1))begin
                        b_modes[b_group][2*lane +: 2]<=choose_mode(new_b_positions[4*lane +: 4]);
                        b_column_complete[b_group][lane]<=1;
                    end
                end
            end
            if(accept_input)begin
                a_values[in_group][in_bank][in_pos]<=in_a;
                a_positions[in_group][in_bank]<=new_a_positions;
                rows[in_group][in_bank]<=in_rows;
                if(seal)begin
                    full[in_group]<=1;
                    
`ifndef SYNTHESIS
                    if(!b_complete[in_group])$fatal(1,"A group sealed before B completed");
`endif
                end
            end
            if(advance)begin
                if(final_bank)begin
                    read_bank_offset<=0;
                    if(final_round)begin
                        full[read_slot]<=0;reserved[read_slot]<=0;
                        read_slot<=~read_slot;read_round<=0;
                    end else read_round<=read_round+1'b1;
                end else read_bank_offset<=read_bank_offset+1'b1;
            end
        end
    end
endmodule
