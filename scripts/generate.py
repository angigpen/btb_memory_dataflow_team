"""Deterministic INT8 GEMM fixtures and an independent integer golden model."""
from pathlib import Path
import argparse
import hashlib
import json
import random

MAX_M, MAX_N, MAX_K = 64, 128, 1024

def matmul(a, b, m, n, k):
    if len(a) != m*k or len(b) != k*n:
        raise ValueError('Matrix storage does not match M/N/K')
    # Plain mathematical definition, independent of RTL scheduling/tiling.
    return [sum(a[row*k+t]*b[t*n+col] for t in range(k))
            for row in range(m) for col in range(n)]

def make_case(path, *, m, n, k, seed=7, sparsity=0.0, pattern='random',
              latency=1, stalls=0, output_stalls=0, runs=1):
    if not (1 <= m <= MAX_M and 1 <= n <= MAX_N and 1 <= k <= MAX_K):
        raise ValueError('Dimensions exceed supported limits')
    if not 0 <= sparsity <= 1 or not 1 <= latency <= 1024 or not 1 <= runs <= 16 or stalls not in (0,1,2) or output_stalls not in (0,1,2):
        raise ValueError('Invalid sparsity, latency or runs')
    if pattern not in ('random','max_positive','max_negative','zeros','identity','zero_boundaries'):
        raise ValueError('Unknown data pattern')
    rng = random.Random(seed)
    def value(side):
        if pattern == 'max_positive': return -128
        if pattern == 'max_negative': return -128 if side == 'a' else 127
        if pattern == 'zeros': return 0
        if pattern == 'identity': return 0
        return 0 if rng.random() < sparsity else rng.randint(-128, 127)
    a=[value('a') for _ in range(m*k)]
    b=[value('b') for _ in range(k*n)]
    if pattern == 'identity':
        a=[rng.randint(-128,127) for _ in range(m*k)]
        b=[int(row==col) for row in range(k) for col in range(n)]
    if pattern == 'zero_boundaries':
        for row in range(m):
            a[row*k]=0;a[row*k+k-1]=0
    c=matmul(a,b,m,n,k)
    assert all(-(1<<31) <= x < (1<<31) for x in c)
    path=Path(path); path.mkdir(parents=True, exist_ok=True)
    def hexfile(name, values, bits):
        (path/name).write_text(''.join(f'{x & ((1<<bits)-1):0{bits//4}x}\n' for x in values),encoding='ascii')
    hexfile('a.hex',a,8); hexfile('b.hex',b,8); hexfile('c.hex',c,32)
    zeros=sum(a[row*k+t]==0 or b[t*n+col]==0
              for row in range(m) for col in range(n) for t in range(k))
    hexfile('config.hex',[m,n,k,latency,stalls,output_stalls,runs,seed,zeros],32)
    metadata=dict(m=m,n=n,k=k,seed=seed,sparsity=sparsity,pattern=pattern,
                  latency=latency,stalls=stalls,output_stalls=output_stalls,runs=runs,
                  dense_mac_count=m*n*k,zero_product_count=zeros,
                  sha256={name:hashlib.sha256((path/name).read_bytes()).hexdigest()
                          for name in ('a.hex','b.hex','c.hex','config.hex')})
    (path/'case.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    return metadata

def suite(root, extended=False):
    cases=[
        ('scalar',dict(m=1,n=1,k=1)),
        ('rectangular',dict(m=3,n=7,k=5)),
        ('identity',dict(m=3,n=8,k=8,pattern='identity')),
        ('k_one',dict(m=4,n=9,k=1)),
        ('n_one',dict(m=5,n=1,k=11)),
        ('all_zero',dict(m=3,n=7,k=5,pattern='zeros')),
        ('max_positive',dict(m=2,n=5,k=31,pattern='max_positive')),
        ('max_negative',dict(m=2,n=5,k=31,pattern='max_negative')),
        ('sparse_90',dict(m=4,n=9,k=13,sparsity=.9)),
        ('periodic_stalls',dict(m=3,n=7,k=5,latency=3,stalls=1,output_stalls=1)),
        ('random_stalls',dict(m=5,n=11,k=17,latency=2,stalls=2,output_stalls=2)),
        ('restart',dict(m=3,n=7,k=5,runs=2,output_stalls=1)),
        ('long_k',dict(m=1,n=5,k=1024)),
        ('wide_n',dict(m=2,n=128,k=3)),
        ('tall_m',dict(m=64,n=3,k=2)),
    ]
    for i in range(5):
        r=random.Random(300+i)
        cases.append((f'random_{i}',dict(m=r.randint(1,8),n=r.randint(1,16),
                    k=r.randint(1,32),seed=300+i,stalls=2,output_stalls=2)))
    if extended:
        cases += [
            ('dense_benchmark',dict(m=16,n=16,k=64)),
            ('sparse_benchmark',dict(m=16,n=16,k=64,sparsity=.9)),
            ('zero_boundaries',dict(m=5,n=7,k=9,pattern='zero_boundaries',stalls=2,output_stalls=2)),
            ('zero_k_one',dict(m=5,n=7,k=1,pattern='zeros',stalls=2,output_stalls=2)),
            ('long_latency',dict(m=3,n=5,k=11,latency=23,stalls=2,output_stalls=2)),
            ('positive_long_k',dict(m=1,n=1,k=1024,pattern='max_positive')),
            ('negative_long_k',dict(m=1,n=1,k=1024,pattern='max_negative')),
            ('max_dimensions',dict(m=64,n=128,k=1024,seed=91)),
        ]
        for i in range(12):
            r=random.Random(500+i)
            cases.append((f'extended_{i}',dict(m=r.randint(1,12),n=r.randint(1,19),
                          k=r.randint(1,65),seed=500+i,sparsity=r.choice([0,.5,.9]),
                          latency=r.randint(1,6),stalls=2,output_stalls=2)))
    for name,params in cases: make_case(Path(root)/name,**params)
    return [name for name,_ in cases]

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,default=Path(__file__).resolve().parents[1]/'build/vectors')
    p.add_argument('--suite',action='store_true')
    p.add_argument('--extended',action='store_true')
    p.add_argument('--m',type=int,default=4); p.add_argument('--n',type=int,default=8)
    p.add_argument('--k',type=int,default=16); p.add_argument('--seed',type=int,default=7)
    p.add_argument('--sparsity',type=float,default=0)
    p.add_argument('--latency',type=int,default=1)
    p.add_argument('--stalls',type=int,choices=[0,1,2],default=0)
    p.add_argument('--output-stalls',type=int,choices=[0,1,2],default=0)
    p.add_argument('--runs',type=int,default=1)
    args=p.parse_args()
    if args.suite: print('\n'.join(suite(args.out,args.extended)))
    else: print(json.dumps(make_case(args.out,m=args.m,n=args.n,k=args.k,
                                    seed=args.seed,sparsity=args.sparsity,latency=args.latency,
                                    stalls=args.stalls,output_stalls=args.output_stalls,runs=args.runs),indent=2))
