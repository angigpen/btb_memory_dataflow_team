`timescale 1ns/1ps
module rv_monitor #(parameter WIDTH=1)(
    input clk,rst,valid,ready,input [WIDTH-1:0] payload);
    reg held=0;
    reg [WIDTH-1:0] saved;
    always @(posedge clk) begin
        if(rst) held<=0;
        else begin
            if((^{valid,ready})===1'bx) $fatal(1,"Unknown handshake: %m");
            if(valid && (^payload)===1'bx) $fatal(1,"Unknown payload: %m");
            if(held && (!valid || payload!==saved)) $fatal(1,"Unstable packet: %m");
            held<=valid&&!ready;saved<=payload;
        end
    end
endmodule
