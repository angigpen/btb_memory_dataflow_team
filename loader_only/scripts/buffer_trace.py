"""Reconstruct reuse-RAM writes from sampled write enables and completed reads.

The VCD used by the viewer does not dump RAM arrays. Do not guess contents from
the controller's current tile or from request acceptance: a word exists only
after the recorded RAM write-enable edge. Byte values come from the official
fixture, checked against the addresses of completed memory transactions.
"""
from array import array
from pathlib import Path


def _dense(wave, path):
    flat = wave['data'].get(path, [0, None])
    out = array('q')
    c = 0
    for i in range(0, len(flat), 2):
        c += flat[i]
        end = c + flat[i + 2] if i + 2 < len(flat) else wave['n']
        out.extend(array('q', [flat[i + 1] or 0]) * max(0, end - c))
    return out


def _fixture(build, case, kind):
    for folder in ('set2', 'set3', 'set1', 'baseline', 'baseline_big', 'set1_big'):
        p = build / folder / 'vectors' / case / (kind + '.hex')
        if p.exists():
            return [int(x, 16) for x in p.read_text().split()]
    raise FileNotFoundError(f'Missing official fixture: {case}/{kind}.hex')


def add_buffer_traces(payload, build):
    """Add compact write/access events. Cycles use the viewer's pre-edge convention."""
    build = Path(build)
    for variant, spec in payload['sets'].items():
        if variant == 'baseline':
            continue
        for case, wave in spec['waves'].items():
            params = {**spec['params'], **wave.get('params', {})}
            m = payload['metrics'][variant + '|' + case]
            M, N, K = (int(m[k]) for k in ('m', 'n', 'k'))
            rows, cols = params['PE_ROWS'], params['PE_COLS']
            aa, bb = _fixture(build, case, 'a'), _fixture(build, case, 'b')
            assert len(aa) == M * K and len(bb) == K * N
            L, C, T = 'tb_gemm.dut.loader.', 'tb_gemm.dut.control.', 'tb_gemm.dut.'
            cache = {}

            def d(path):
                if path not in cache:
                    cache[path] = _dense(wave, path)
                return cache[path]

            def v(path, cycle):
                return d(path)[max(0, cycle)]

            A = dict(depth=params.get('ABUF_DEPTH', params.get('ACACHE_DEPTH', 0)),
                     writes=[], reads=[])
            B = dict(depth=params.get('BBUF_DEPTH', 0), writes=[], reads=[])
            pending = None
            received = {'a': {}, 'b': {}}
            last = {'a': None, 'b': None}
            for cycle in range(wave['n']):
                # Responses are matched to the accepted in-order request, never
                # to the controller position (which may already be ahead).
                if v(T + 'mem_rsp_valid', cycle) and v(T + 'mem_rsp_ready', cycle):
                    if pending is None:
                        raise ValueError(f'{variant}/{case}: response without request')
                    kind, address = pending
                    received[kind][address] = cycle
                    last[kind] = address
                    pending = None
                if v(T + 'mem_req_valid', cycle) and v(T + 'mem_req_ready', cycle):
                    if pending is not None:
                        raise ValueError(f'{variant}/{case}: unexpected multiple outstanding reads')
                    pending = ('b' if v(T + 'mem_req_is_b', cycle) else 'a',
                               v(T + 'mem_req_addr', cycle))

                if v(L + 'aw_pend', cycle):
                    address = last['a']
                    if address is None:
                        raise ValueError('A RAM write before any A response')
                    row, kk = (address // K // rows) * rows, address % K
                    word = []
                    for lane in range(rows):
                        ad = (row + lane) * K + kk
                        if row + lane < M:
                            assert ad in received['a'] and received['a'][ad] < cycle
                            byte = aa[ad]
                            word.append(byte - 256 if byte >= 128 else byte)
                        else:
                            word.append(0)
                    assert kk < A['depth']
                    # Write at edge c becomes visible in pre-edge sample c+1.
                    A['writes'].append([cycle + 1, kk, row, word, int(not any(word))])

                if B['depth'] and v(L + 'bw_pend', cycle):
                    address = last['b']
                    if address is None:
                        raise ValueError('B RAM write before any B response')
                    kk, col = address // N, (address % N // cols) * cols
                    group = v(C + 'grp_start', cycle) if variant == 's3_all' else 0
                    index = ((col - group) // cols) * K + kk
                    word = []
                    for lane in range(cols):
                        ad = kk * N + col + lane
                        if col + lane < N:
                            assert ad in received['b'] and received['b'][ad] < cycle
                            byte = bb[ad]
                            word.append(byte - 256 if byte >= 128 else byte)
                        else:
                            word.append(0)
                    assert 0 <= index < B['depth'], (variant, case, cycle, index)
                    B['writes'].append([cycle + 1, index, col, kk, word])

                if variant == 's1_all':
                    if v(L + 'accept', cycle) and v(L + 'hit_a', cycle):
                        A['reads'].append([cycle, v(C + 'kk', cycle)])
                elif v(L + 'cap_v', cycle):
                    # cap_* describe the descriptor accepted on the prior edge.
                    previous = max(0, cycle - 1)
                    kk, col, row = (v(C + p, previous) for p in ('kk', 'col', 'row'))
                    group = v(C + 'grp_start', previous) if variant == 's3_all' else 0
                    index = ((col - group) // cols) * K + kk
                    if v(L + 'cap_hita', cycle):
                        A['reads'].append([cycle, kk])
                    if v(L + 'cap_hitb', cycle):
                        B['reads'].append([cycle, index])
                    # First-row zero skip writes valid=0, not a B data word.
                    if row == 0 and 0 <= index < B['depth'] and v(L + 'skip_b', cycle):
                        B['writes'].append([cycle + 1, index, col, kk, None])

            B['writes'].sort(key=lambda e: e[0])
            wave['buffers'] = dict(a=A, b=B, source='write-enables + completed reads + official fixtures')
            print(f'  buffers {variant}/{case}: A {len(A["writes"])} writes, B {len(B["writes"])} updates')
