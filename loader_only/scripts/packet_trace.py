"""Replay logical packet lanes from handshakes; never label reset zeros as received data.

Arrays are absent from the saved VCD. Values are reconstructed from the official
fixtures, completed byte reads and the recorded reuse-RAM history. A snapshot at
c+1 contains changes made on edge c, matching the viewer's pre-edge convention.
"""
from buffer_trace import _dense, _fixture


def add_packet_traces(payload, build):
    for variant, spec in payload['sets'].items():
        if variant == 'baseline':
            continue
        params = spec['params']
        nr, nc = params['PE_ROWS'], params['PE_COLS']
        ns = (2 if params.get('USE_DBUF', 1) else 1) if variant == 's1_all' else params['NSLOTS']
        for case, wave in spec['waves'].items():
            m = payload['metrics'][variant + '|' + case]
            M, N, K = (int(m[x]) for x in ('m', 'n', 'k'))
            fixture = {x: _fixture(build, case, x) for x in ('a', 'b')}
            L, T, C = 'tb_gemm.dut.loader.', 'tb_gemm.dut.', 'tb_gemm.dut.control.'
            cache = {}

            def v(path, c):
                if path not in cache:
                    cache[path] = _dense(wave, path)
                return cache[path][c]

            def signed(byte):
                return byte - 256 if byte >= 128 else byte

            slots, events = [None] * ns, []
            ram, ri = {'a': {}, 'b': {}}, {'a': 0, 'b': 0}
            intake, pending, capture = 0, None, None
            consumed = 0
            for c in range(wave['n']):
                for kind in ('a', 'b'):
                    writes = wave['buffers'][kind]['writes']
                    while ri[kind] < len(writes) and writes[ri[kind]][0] <= c:
                        e = writes[ri[kind]]
                        ram[kind][e[1]] = e
                        ri[kind] += 1

                if v(L + 'op_valid', c) and v(L + 'op_ready', c):
                    slot = v(L + 'rd', c)
                    packet = slots[slot]
                    assert packet and packet['prepared']
                    assert all(x is not None for x in packet['a'] + packet['b']), (variant, case, c)
                    assert not packet['pending']
                    # Catch reconstruction/ordering mistakes against the exact dense result operands.
                    for lane in range(nr):
                        if packet['row'] + lane < M:
                            assert packet['a'][lane] == signed(fixture['a'][(packet['row'] + lane) * K + packet['k']])
                    for lane in range(nc):
                        if packet['col'] + lane < N and packet['bs'][lane] != 'z':
                            assert packet['b'][lane] == signed(fixture['b'][packet['k'] * N + packet['col'] + lane])
                    slots[slot] = None
                    events.append([c + 1, slot, 0])  # release
                    consumed += 1

                if v(T + 'mem_rsp_valid', c) and v(T + 'mem_rsp_ready', c):
                    assert pending is not None
                    slot, kind, lane, address = pending
                    packet = slots[slot]
                    assert packet and packet[kind][lane] is None
                    packet[kind][lane] = signed(fixture[kind][address])
                    packet[kind + 's'][lane] = 'm'
                    packet['pending'] = None
                    pending = None
                    events.append([c + 1, slot, 4, int(kind == 'b'), lane, packet[kind][lane]])

                # S1 A_CRD / S2 cap_v writes the cached words on this edge.
                preparing = (v(L + 'state', c) == 1 if variant == 's1_all' else v(L + 'cap_v', c))
                if preparing:
                    assert capture is not None
                    slot, aword, bword = capture
                    packet = slots[slot]
                    if aword is not None:
                        packet['a'] = list(aword)
                        packet['as'] = ['r' if packet['row'] + i < M else 'p' for i in range(nr)]
                    if bword is not None:
                        packet['b'] = list(bword)
                        packet['bs'] = ['r' if packet['col'] + i < N else 'p' for i in range(nc)]
                    skip = (params.get('USE_ZERO_SKIP', 1) and aword is not None and not any(aword)
                            and (variant == 's1_all' or bword is None))
                    if skip:
                        packet['b'] = [0] * nc
                        packet['bs'] = ['z' if packet['col'] + i < N else 'p' for i in range(nc)]
                    packet['prepared'] = True
                    events.append([c + 1, slot, 2, aword, bword, int(bool(skip))])
                    capture = None

                if v(L + 'req_valid', c) and v(L + 'req_ready', c):
                    slot = v(L + 'wr', c) if variant == 's1_all' else intake
                    intake = (intake + 1) % ns
                    assert slots[slot] is None, (variant, case, c, slot)
                    row, col, kk = (v(C + x, c) for x in ('row', 'col', 'kk'))
                    aword = bword = None
                    # The synchronous RAM captures its pre-edge value at intake.
                    hita = v(L + 'hit_a', c) if variant == 's1_all' else v(L + 'cap_hita', c + 1) if c + 1 < wave['n'] else 0
                    hitb = 0 if variant == 's1_all' else v(L + 'cap_hitb', c + 1) if c + 1 < wave['n'] else 0
                    if hita:
                        entry = ram['a'].get(kk)
                        assert entry and entry[2] == row, (variant, case, c, 'A read before write')
                        aword = entry[3]
                    if hitb:
                        group = v(C + 'grp_start', c) if variant == 's3_all' else 0
                        index = ((col - group) // nc) * K + kk
                        entry = ram['b'].get(index)
                        if entry and entry[4] is not None:
                            assert entry[2:4] == [col, kk]
                            bword = entry[4]
                    slots[slot] = dict(row=row, col=col, k=kk, first=int(kk == 0), last=int(kk == K - 1),
                                       a=[None if row + i < M else 0 for i in range(nr)],
                                       b=[None if col + i < N else 0 for i in range(nc)],
                                       **{'as': [None if row + i < M else 'p' for i in range(nr)]},
                                       bs=[None if col + i < N else 'p' for i in range(nc)],
                                       prepared=variant == 's1_all' and not hita, pending=None)
                    if variant != 's1_all' or hita:
                        capture = (slot, aword, bword)
                    events.append([c + 1, slot, 1, row, col, kk, int(slots[slot]['prepared'])])

                if v(T + 'mem_req_valid', c) and v(T + 'mem_req_ready', c):
                    assert pending is None
                    slot = v(L + ('wr' if variant == 's1_all' else 'ip'), c)
                    packet = slots[slot]
                    assert packet and packet['prepared']
                    kind = 'b' if v(T + 'mem_req_is_b', c) else 'a'
                    address = v(T + 'mem_req_addr', c)
                    lane = address - (packet['k'] * N + packet['col']) if kind == 'b' else (address - packet['row'] * K - packet['k']) // K
                    assert 0 <= lane < (nc if kind == 'b' else nr)
                    assert packet[kind][lane] is None
                    pending = (slot, kind, lane, address)
                    packet['pending'] = [kind, lane]
                    events.append([c + 1, slot, 3, int(kind == 'b'), lane])

            wave['packets'] = dict(count=ns, rows=nr, cols=nc, events=events,
                                   source='handshakes + reuse-RAM history + official fixtures')
            print(f'  packets {variant}/{case}: {len(events)} updates, {consumed} checked MAC handshakes')
