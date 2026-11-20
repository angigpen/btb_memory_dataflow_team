"""Sample pre-edge VCD values and check accepted NM payloads independently."""
def parse_vcd(path, wanted):
    """Return {path: [[cycle, value], ...]} (change list, value None = x/z) and the cycle count."""
    ids, stack = {}, []        # vcd id -> list of wanted paths
    clk_id = None
    with open(path) as f:
        for line in f:
            t = line.split()
            if not t: continue
            if t[0] == '$scope': stack.append(t[2])
            elif t[0] == '$upscope': stack.pop()
            elif t[0] == '$var':
                p = '.'.join(stack + [t[4]])
                if p == 'tb_gemm.dut.clk': clk_id = t[3]
                if p in wanted: ids.setdefault(t[3], []).append(p)
            elif t[0] == '$enddefinitions': break
        cur = {i: None for i in ids}; clk = 0
        assert clk_id is not None, f'no dut.clk in {path}'
        out = {p: [] for p in wanted}; last = {p: object() for p in wanted}
        cyc = 0; pending = []

        def flush():
            nonlocal clk, cyc
            rising = False
            for i, v in pending:
                if i == clk_id:
                    nv = 1 if v == 1 else 0
                    rising = rising or (nv == 1 and clk == 0); clk = nv
            if rising:                      # sample values as they were *before* this timestep
                for i, ps in ids.items():
                    for p in ps:
                        if cur[i] != last[p]:
                            out[p].append([cyc, cur[i]]); last[p] = cur[i]
                cyc += 1
            for i, v in pending:
                if i in cur: cur[i] = v
            pending.clear()

        for line in f:
            if not line or line[0] == '$': continue
            c = line[0]
            if c == '#': flush(); continue
            if c in '01xzXZ':
                pending.append((line[1:].strip(), None if c in 'xzXZ' else int(c)))
            elif c == 'b':
                bits, i = line[1:].split()
                pending.append((i, None if any(ch in bits for ch in 'xzXZ') else int(bits, 2)))
        flush()
    return out, cyc


def check_wave(path, records):
    prefix="tb_gemm.dut."
    fields=["cv","cr","ca","cb","column_k_position","cb_tag","work_mask","cf","cl","cbank"]
    data,cycles=parse_vcd(path,{prefix+f for f in fields})
    pointers={f:0 for f in fields}
    current={f:None for f in fields}
    accepted=[]
    for cycle in range(cycles):
        for f in fields:
            seq=data[prefix+f]
            while pointers[f]<len(seq) and seq[pointers[f]][0]<=cycle:
                current[f]=seq[pointers[f]][1]
                pointers[f]+=1
        if current["cv"] and current["cr"]:
            accepted.append(tuple(current[f] for f in fields[2:]))
    expected=[tuple(int(row[f],16) for f in ("a","b","k_positions","tag","work_mask"))+
              tuple(int(row[f]) for f in ("first","last","bank")) for row in records]
    assert accepted==expected,("VCD and handshake trace differ",len(accepted),len(expected))
    return cycles
