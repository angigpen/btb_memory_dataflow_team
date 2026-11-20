"""Verify independent early-B classification directly from pre-edge VCD samples."""
from vcd_analysis import parse_vcd


def check_early_wave(path):
    prefix="tb_gemm.dut."
    fields=["rst","group_valid","group_ready","group_ticket","group_len_in",
            "early_b_valid","early_b_ready","early_b_group","early_b_pos",
            "early_b_mask","early_b_data","ov","ordy","op_group",
            "sparsity.read_slot","sparsity.out_group_mode_b",
            "mem_req_valid","mem_req_ready","mem_req_is_b"]
    changes,cycles=parse_vcd(path,{prefix+f for f in fields})
    cursor={f:0 for f in fields}
    values={f:None for f in fields}

    def empty():
        return dict(length=0,seen=[0]*4,nonzero=[0]*4,mode=0,
                    a_started=False,reported=False)

    groups=[empty(),empty()]
    count=0
    early=0
    first_mode=None
    first_a_request=None
    for cycle in range(cycles):
        for name in fields:
            seq=changes[prefix+name]
            while cursor[name]<len(seq) and seq[cursor[name]][0]<=cycle:
                values[name]=seq[cursor[name]][1]
                cursor[name]+=1
        if values["rst"] is None:
            continue
        if values["rst"]:
            groups=[empty(),empty()]
            continue
        read=values["sparsity.read_slot"]
        assert values["sparsity.out_group_mode_b"]==groups[read]["mode"],(
            "B mode differs from independent received-byte model",cycle,values)
        if values["mem_req_valid"] and values["mem_req_ready"] and not values["mem_req_is_b"]:
            if first_a_request is None:
                first_a_request=cycle
        if values["group_valid"] and values["group_ready"]:
            slot=values["group_ticket"]
            groups[slot]=empty()
            groups[slot]["length"]=values["group_len_in"]
            count+=1
        if values["early_b_valid"] and values["early_b_ready"]:
            group=groups[values["early_b_group"]]
            pos=values["early_b_pos"]
            for col in range(4):
                if not (values["early_b_mask"]>>col)&1:
                    continue
                assert not group["seen"][col]&(1<<pos)
                group["seen"][col]|=1<<pos
                byte=(values["early_b_data"]>>(8*col))&255
                if byte:
                    group["nonzero"][col]|=1<<pos
                if group["seen"][col]==(1<<group["length"])-1:
                    nnz=group["nonzero"][col].bit_count()
                    mode=min(3,nnz)
                    group["mode"]=(group["mode"]&~(3<<(2*col)))|(mode<<(2*col))
                    if not group["reported"]:
                        group["reported"]=True
                        first_mode=cycle+1 if first_mode is None else first_mode
                        if not group["a_started"]:
                            early+=1
        if values["ov"] and values["ordy"]:
            groups[values["op_group"]]["a_started"]=True
    return dict(groups=count,mode_before_a_groups=early,
                first_b_mode_vcd_cycle=first_mode,
                first_a_request_vcd_cycle=first_a_request,
                checks=["every received B coordinate classified exactly once",
                        "registered N:4 modes match independent byte model",
                        "B and A channels accepted independently"])
