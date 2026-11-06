# Memory/Dataflow 팀 RTL — Set 1, Set 2, Set 3

**현재 버전은 [`loader_only/`](loader_only/README.md)입니다.** 배포본 규칙대로 `operand_loader.v`만 고쳤고(Set 3만 `gemm_control.v`도 고침), 공식 테스트벤치(`tb_gemm.sv`)로 검증했습니다. 자세한 설명·표·사용법은 [`loader_only/README.md`](loader_only/README.md)에 있습니다.

## 폴더 구성

```
proj_btb/
├── loader_only/                ← 현재 버전 (제출용)
│   ├── baseline/               배포본 RTL 6개 (수정 없음)
│   ├── set1/operand_loader.v   Set 1: A reuse buffer + zero read skip + Ping/Pong (Phase 1 + 2a)
│   ├── set2/operand_loader.v   Set 2: Set 1 + B reuse buffer 16 KB (Phase 3), packet 슬롯 4개
│   ├── set3/                   Set 3: Set 2 loader (B 4 KB) + loop 순서를 바꾼 gemm_control.v (규칙 밖)
│   ├── tb/                     공식 테스트벤치 (tb_gemm.sv, rv_monitor.sv, rv_delay.v) + 파형 저장용 wave_dump.v
│   ├── scripts/                run_official.py (공식 TB를 Icarus로 실행), summarize_official.py,
│   │                           vcd_to_html.py (발표용 HTML), generate.py (fixture)
│   ├── make_waves.sh           HTML용 파형 + 64×128×128 buffer 크기 비교를 build/에 생성 (약 15분)
│   ├── results/                전체 suite 결과 (variant별 csv, summary.txt)
│   └── build/                  실행별 summary.csv, waves.html (결과 + 파형 + 애니메이션, 약 10 MB)
│
├── baseline/                   배포본 RTL 6개 (loader_only/baseline/과 같음)
├── set1_phase1_2a/             이전 버전 Set 1 (gemm_control·gemm_top도 고친 버전)
├── tb/tb_local.sv, tb/*.gtkw   이전 버전 로컬 테스트벤치 / GTKWave 설정
├── scripts/                    이전 버전 스크립트 (run_local.py 등)
└── Claude outputs/             zip, 발표 자료(pptx), rename_terms.py
```

## 이전 버전과 바뀐 점

| | 이전 버전 (최상위 폴더) | 현재 버전 (`loader_only/`) |
|---|---|---|
| 고친 파일 | `gemm_control.v`, `gemm_top.v`, `operand_loader.v` | **`operand_loader.v`만** (Set 3만 `gemm_control.v` 추가) |
| 테스트벤치 | 직접 만든 `tb_local.sv` | **공식 `tb_gemm.sv`** (Icarus로 실행) |
| 메모리 | 요청을 응답 전에 여러 개 받음 (pipelined) | **미완료 요청 최대 1개** (INTERFACES.md 계약) |
| 요청 여러 개 동시 진행 (Phase 2b) | 있음 (tag FIFO, `MAX_OUT`) | **뺌** — 계약상 쓸 수 없음. 요청 1개를 레지스터 하나(`busy_q`)로 추적 |
| loader의 위치 정보 | `gemm_control`이 `req_kk`, `req_reuse_a/b` 등을 출력 | loader가 descriptor(`req_first`, `req_a_addr`, `req_b_addr`)에서 스스로 계산 |
| B reuse buffer | — | 1-bit valid RAM 추가: zero skip으로 안 읽은 B도 나중에 한 번만 읽음 |

예전 `set2_all_phases/`, `set3_grouped_b/`, `results/`는 지웠습니다. 남은 최상위 `set1_phase1_2a/`, `tb/tb_local.sv`, `scripts/`는 참고용이며, `scripts/run_local.py`는 `baseline`, `s1_*` variant만 실행됩니다 (`s2_*`, `s3_*`는 지운 폴더를 가리킴).

## 결과 요약 (공식 테스트벤치, Icarus)

| case | variant | cycles | 배속 | 총 read | read 변화 |
|---|---|---|---|---|---|
| 64×64×64, latency 1 | baseline | 430,592 | 1.00× | 131,072 | — |
| | Set 1 | 245,504 | 1.75× | 69,632 | −46.9% |
| | Set 2 | 44,127 | 9.76× | 8,192 | −93.8% |
| 64×64×64, latency 23 | baseline | 3,314,176 | 1.00× | 131,072 | — |
| | Set 1 | 1,777,408 | 1.86× | 69,632 | −46.9% |
| | Set 2 | 224,351 | 14.77× | 8,192 | −93.8% |
| 64×128×128, latency 1 | baseline | 1,713,152 | 1.00× | 524,288 | — |
| | Set 1 | 949,760 | 1.80× | 270,336 | −48.4% |
| | Set 2 (B 16 KB) | 143,535 | 11.94× | 24,576 | −95.3% |
| | Set 2 (B 4 KB) | 650,775 | 2.63× | 208,896 | −60.2% |
| | Set 3 (B 4 KB) | 211,556 | 8.10× | 49,152 | −90.6% |

- 공식 suite + extended 44 case: baseline(공식 검사 포함), Set 1, Set 2, Set 3 **전부 PASS**.
- 요청을 하나씩만 보낼 수 있으므로, 속도는 **read 횟수를 줄이는 것**(A·B reuse, zero skip)에서 나옵니다.
- 전체 표: [`loader_only/results/summary.txt`](loader_only/results/summary.txt). 자원 추정(Yosys): Set 1 BRAM36 1개, Set 2 BRAM36 5개 + BRAM18 1개, Set 3 BRAM36 2개. 정확한 값은 `synth.py`(Vivado)로 확인해야 합니다.

## 이름

A reuse buffer, B reuse buffer (팀 카톡과 Sparsity 팀에서는 A 쪽을 "A cache"라고도 부름).

- `loader_only/`: 코드 이름은 그대로 `ACACHE_DEPTH`/`a_cache`, `BBUF_DEPTH`/`b_buf`, Set 1 FSM state `A_CRD`.
- 이전 버전 `set1_phase1_2a/`: `ABUF_DEPTH`/`a_buf`, `A_BRD`로 바꿔 둠.

## 진행자님께 확인할 것

1. B reuse buffer 16 KB(BRAM36 4개) 사용이 괜찮은지. Set 3은 4 KB(BRAM36 1개)로 64×128×128에서 −91%까지 줄일 수 있습니다.
2. Set 3은 `gemm_control.v`의 loop 순서를 바꾸므로 규칙 밖입니다. 써도 되는지, Compute 팀과 분담을 어떻게 할지.
