"""Rename 'A cache' -> 'A reuse buffer', 'B buffer' -> 'B reuse buffer' in Verilog comments and README text.
Code identifiers (ACACHE_DEPTH, BBUF_DEPTH, a_cache, b_buf, g_acache ...) are left unchanged."""
import re, sys
from pathlib import Path
def ren(t):
    t = t.replace('A cache', 'A reuse buffer').replace('B buffer', 'B reuse buffer')
    t = re.sub(r'(?<![A-Za-z_])cache(?![A-Za-z_])', 'reuse buffer', t)   # bare word "cache" in prose
    t = t.replace('A reuse reuse buffer', 'A reuse buffer').replace('B reuse reuse buffer', 'B reuse buffer')
    return t
for p in map(Path, sys.argv[1:]):
    s = p.read_text(encoding='utf-8'); out = []
    if p.suffix == '.v':
        for line in s.split('\n'):
            i = line.find('//')
            out.append(line if i < 0 else line[:i] + ren(line[i:]))
        n = '\n'.join(out)
    else:   # markdown: rename prose, keep `code spans` untouched
        parts = re.split(r'(`[^`]*`)', s)
        n = ''.join(x if x.startswith('`') else ren(x) for x in parts)
    if n != s: p.write_text(n, encoding='utf-8'); print('updated', p)
