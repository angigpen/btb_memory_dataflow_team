"""Display-only trace tests; no RTL or regression metrics are changed."""
import tempfile
import unittest
from pathlib import Path

from buffer_trace import add_buffer_traces


def flat(changes):
    result, previous = [], 0
    for cycle, value in changes:
        result.extend((cycle - previous, value))
        previous = cycle
    return result


class BufferTraceTest(unittest.TestCase):
    def make_payload(self, kind, write_cycle=13):
        top, loader = 'tb_gemm.dut.', 'tb_gemm.dut.loader.'
        data = {
            top+'mem_req_valid': flat([(0,0),(1,1),(2,0),(4,1),(5,0),(7,1),(8,0),(10,1),(11,0)]),
            top+'mem_req_ready': [0,1],
            top+'mem_req_is_b': [0,int(kind == 'b')],
            top+'mem_req_addr': flat([(0,0),(4,1),(7,2),(10,3)]),
            top+'mem_rsp_valid': flat([(0,0),(3,1),(4,0),(6,1),(7,0),(9,1),(10,0),(12,1),(13,0)]),
            top+'mem_rsp_ready': [0,1],
            loader+('aw_pend' if kind == 'a' else 'bw_pend'): flat([(0,0),(write_cycle,1),(write_cycle+1,0)]),
        }
        variant = 's1_all' if kind == 'a' else 's2_all'
        wave = dict(n=16, data=data)
        spec = dict(params=dict(PE_ROWS=4,PE_COLS=4,ABUF_DEPTH=2,BBUF_DEPTH=2 if kind == 'b' else 0),
                    waves={'tiny':wave})
        return dict(sets={variant:spec},metrics={variant+'|tiny':dict(m=4 if kind=='a' else 1,n=1 if kind=='a' else 4,k=1)}),wave

    def enrich(self, payload, a, b):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'set2/vectors/tiny'
            folder.mkdir(parents=True)
            for name, values in [('a',a),('b',b)]:
                (folder/(name+'.hex')).write_text('\n'.join(f'{v & 255:02x}' for v in values))
            add_buffer_traces(payload, Path(tmp))

    def test_a_word_is_visible_after_write_edge_and_signed(self):
        payload,wave = self.make_payload('a')
        self.enrich(payload,[0,-128,3,-1],[7])
        self.assertEqual(wave['buffers']['a']['writes'],[[14,0,0,[0,-128,3,-1],0]])

    def test_b_word_has_original_k_and_columns(self):
        payload,wave = self.make_payload('b')
        self.enrich(payload,[7],[0,-128,3,-1])
        self.assertEqual(wave['buffers']['b']['writes'],[[14,0,0,0,[0,-128,3,-1]]])

    def test_zero_word_is_stored_not_empty(self):
        payload,wave = self.make_payload('a')
        self.enrich(payload,[0,0,0,0],[7])
        self.assertEqual(wave['buffers']['a']['writes'][0][3:],[[0,0,0,0],1])

    def test_write_cannot_include_data_arriving_on_same_edge(self):
        payload,_ = self.make_payload('a',write_cycle=12)
        with self.assertRaises(AssertionError):
            self.enrich(payload,[0,1,2,3],[7])


if __name__ == '__main__':
    unittest.main()
