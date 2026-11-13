# Memory/Dataflow 팀 RTL — Set 1~4

배포본 README의 규칙상 **Memory/Dataflow 팀 파일은 `rtl/operand_loader.v`** 입니다.
Set 1·2는 그 파일만 고쳤고, Set 3·4는 다른 파일도 고쳐서 **규칙 밖**입니다(진행자 허락 필요).
**어느 Set도 zero read skip을 쓰지 않습니다** (모든 packet이 B를 읽거나 buffer에서 가져옴).

## 폴더 구성

| 폴더 | 내용 |
|---|---|
| `set1/operand_loader.v` | **Set 1**: A reuse buffer + Ping/Pong (Phase 1 + 2a) |
| `set2/operand_loader.v` | **Set 2**: Set 1 + B reuse buffer 16 KB (Phase 3), packet 슬롯 4개 |
| `set3/` | **Set 3**: Set 2 loader (B buffer 4 KB) + loop 순서를 바꾼 `gemm_control.v` (규칙 밖) |
| `set4/` | **Set 4**: Set 3 + `mac_array` ACC 4벌 + B packet 공유 (`gemm_top`, `gemm_control`, `operand_loader`, `mac_array`; 규칙 밖) |
| `baseline/` | 배포본 RTL 6개 (수정 없음) |
| `tb/` | 배포본 공식 테스트벤치 (`tb_gemm.sv`, `rv_monitor.sv`, `rv_delay.v`) + 우리가 추가한 `wave_dump.v`, `pkt_trace.v` (공식 tb는 수정 안 함) |
| `scripts/run_official.py` | 공식 테스트벤치를 **Vivado 대신 Icarus Verilog**로 돌리는 스크립트 |
| `scripts/summarize_official.py` | baseline 대비 cycle·read 비교 표 (`results/summary.txt`) |
| `scripts/vcd_to_html.py`, `make_waves.sh` | 발표용 HTML 만들기 (아래) |
| `results/` | 측정 결과 (variant별 csv, `summary.txt`) |

## 공식 flow로 쓰는 법

1. 배포본의 `rtl/operand_loader.v`를 `set1/` 또는 `set2/`의 파일로 바꿉니다 (Set 3·4는 폴더의 파일 전부).
2. 실행합니다: `python scripts/run.py --suite --extended --optimized --build build/candidate`
3. 비교합니다: `python scripts/compare.py build/baseline/results.json build/candidate/results.json`

Vivado가 없으면 Icarus로 같은 테스트벤치를 돌릴 수 있습니다:

```
python scripts/run_official.py --rtl-dir baseline --suite --extended --baseline-checks --build build/baseline
python scripts/run_official.py --rtl-dir set1 --suite --extended --build build/set1      # +OPTIMIZED
python scripts/run_official.py --rtl-dir set2 --extended --cases rectangular --param USE_DBUF=0 --build build/x
python scripts/run_official.py --rtl-dir set4 --extended --cases bench64 --param top.NBANK=1 --build build/y
python scripts/summarize_official.py results
```

- `run_official.py`는 `run.py`와 같은 fixture, 같은 plusarg(`+HOST_TEST`, `restart`에 `+RESET_TEST`, `+OPTIMIZED`), 같은 PASS 조건을 씁니다.
- 폴더에 없는 파일은 `baseline/`에서 가져옵니다.
- `--param X=1` → operand_loader, `control.X=1` → gemm_control, `top.X=1` → gemm_top (Set 4 설정).
- 공식 suite + 추가 case: `bench64`(64³), `bench64_lat23`, `bench64_sp90`, `bench_64x128x128`(+`_l23`),
  `bench_64x64x256`(B = 16 KB), `bench_64x128x512`(B = 64 KB), 그리고 **값이 −3…3인 데모** `demo_8x8x4`, `demo_20x12x6`.

## loader가 위치를 스스로 알아내는 방법

gemm_control을 고칠 수 없으므로, 이미 받는 descriptor에서 계산합니다.

| 필요한 정보 | 계산 |
|---|---|
| k 번호 (kk) | `req_first`면 0, 이후 descriptor마다 +1 |
| 새 row tile인가 (A reuse buffer를 채울 때) | Set 1: tile 첫 descriptor의 `req_b_addr` < 4 (column tile 0). Set 2·3: tile 첫 A 주소가 이전 tile과 다르거나, 작업 시작(A·B 주소 모두 0) |
| row tile 0인가 (B reuse buffer를 채울 때) | tile 첫 descriptor의 `req_a_addr` (= row·K) == 0 |
| B reuse buffer 주소 | (같은 row 안에서 몇 번째 column tile인지) × K + kk, K = `req_a_stride` |

Set 2·3 방식은 baseline loop 순서와 Set 3의 묶음 순서 모두에서 맞게 동작하므로, Set 3도 같은 loader를 씁니다.

gemm_control은 tile 하나를 끝까지 마친 뒤 다음 tile을 시작하므로, column tile 0에서 쓴 A와 row tile 0에서 쓴 B는 다음 tile이 읽기 전에 항상 저장이 끝나 있습니다. 새 작업(job)은 항상 row 0, column 0 tile부터 시작하므로 이전 작업의 내용을 쓰지 않습니다.

## Set 1 — FSM 방식 (baseline과 구조가 비슷함)

| parameter | 기본값 | 의미 |
|---|---|---|
| `USE_A_REUSE` | 1 | column tile 0에서 A 4 byte를 A reuse buffer[kk]에 저장, 다음 column tile부터 buffer에서 읽음 |
| `USE_DBUF` | 1 | packet 슬롯 2개 (Ping/Pong) |
| `ABUF_DEPTH` | 1024 | A reuse buffer entry 수 (× 4 B = 4 KB). kk ≥ depth면 A를 메모리에서 읽음 |

두 switch를 모두 0으로 하면 **baseline과 cycle·read가 완전히 같습니다**.

## Set 2 — 블록 구조 (Request intake → Prep → Load control → Response router → Supply control)

요청은 한 번에 1개만 보냅니다(응답을 받은 뒤 다음 요청). 진행 중인 요청의 {슬롯, byte 위치}는 레지스터 하나에 저장합니다.

| parameter | 기본값 | 의미 |
|---|---|---|
| `USE_A_REUSE` | 1 | Set 1과 같음 |
| `USE_B_REUSE` | 1 | row tile 0에서 B를 B reuse buffer에 저장, 다음 row tile부터 buffer에서 읽음 (B는 한 번만 읽음) |
| `NSLOTS` | 4 | packet 슬롯 수. 메모리를 안 읽는 packet이 앞 packet 뒤에 줄 서서 기다릴 수 있음 |
| `ABUF_DEPTH` | 1024 | A reuse buffer entry |
| `BBUF_DEPTH` | 4096 | B reuse buffer entry × 4 byte = 16 KB = K=N=128일 때 B 전체 |

파라미터는 `gemm_top`이 넘겨주지 않으므로 `operand_loader.v`의 기본값을 고쳐서 바꿉니다.

## 메모리 계약 (INTERFACES.md, tb_gemm.sv)

- 요청 1개 = 1 byte. **미완료 요청은 최대 1개**입니다.
- `mem_req_ready = !pending && !rsp_valid`: 처리 중이거나 응답을 아직 안 가져갔으면 0.
- 그래서 byte 하나에 latency + 2 cycle이 걸립니다. 요청을 여러 개 미리 보내는 방식(2b)은 쓸 수 없어서 코드에서 뺐습니다. 이전 버전(최대 4개)과 cycle 수가 완전히 같은 것을 확인했습니다.
- 남는 방법은 **read 횟수 자체를 줄이는 것**(A·B reuse, Set 3·4의 loop 순서)입니다.

## 측정 결과 (공식 테스트벤치, Icarus)

baseline, Set 1~4 모두 **49 case 전부 PASS** (공식 suite + extended + 추가 case, 정답·주소·중복·handshake 검사).
전체 표는 `results/summary.txt`. cycle(배속), latency 1:

| case | baseline | Set 1 | Set 2 | Set 3 | Set 4 |
|---|---|---|---|---|---|
| demo 8×8×4 | 488 | 388 (1.26×) | 279 (1.75×) | 281 | **236 (2.07×)** |
| demo 20×12×6 | 2,610 | 1,875 | 931 | 934 | **716 (3.65×)** |
| 16×16×64 (dense) | 26,912 | 17,456 | 7,047 | 7,051 | **6,872 (3.92×)** |
| 64×64×64 | 430,592 | 245,504 (1.75×) | 44,127 (9.76×) | 44,143 | **41,657 (10.34×)** |
| 64×128×128 | 1,713,152 | 949,760 | **143,535 (11.94×)** | 211,556 | 205,882 |
| 64×64×256 | 1,708,544 | 967,424 | **161,055 (10.6×)** | 297,040 | 293,606 |
| 64×128×512 | 6,824,960 | 3,769,856 | 2,571,159 | 1,902,866 | **1,893,400 (3.6×)** |
| 64×128×1024 (max) | 13,640,704 | 7,529,984 | 5,807,571 | 6,314,497 | **2,118,075 (6.44×)** |

- zero skip을 뺐으므로 sparse case는 dense와 cycle이 같습니다 (예: `sparse_benchmark` Set 1 11,480 → 17,456).
- B가 buffer에 다 들어가면 Set 2(16 KB)가 가장 적게 읽습니다. B가 어떤 buffer에도 안 들어가면(K=1024) Set 4의 B 공유가 B read를 1/4로 줄입니다.
- Set 4 ablation (64×128×128): `NBANK=1` 201,449 / `USE_B_SHARE=0` 341,678 / `USE_B_REUSE=0` 273,334.

## 발표용 HTML (결과 + 파형 + 애니메이션)

`build/waves.html`을 브라우저로 열면 됩니다. 인터넷 없이 열리는 파일 하나입니다(약 14 MB).

- **case**: demo 8×8×4, demo 20×12×6 (값 표시), rectangular, long_latency, 64×64×64, 64×128×128 (latency 23).
- **데모 case**: A·B의 모든 값과 C의 부분합(각 PE의 ACC 값)을 숫자로 보여줍니다. mac_array가 방금 받은 packet의
  A 열·B 행·C tile에 테두리가 쳐지고, 아래 줄에 `C[..] += A[..][k] × B[k][..]`가 실제 값으로 나옵니다. "1 ▶"로 한 cycle씩 진행.
- **확대·축소**: 그림 크기가 CSS px로 고정되어 있어서 브라우저 확대/축소(Cmd − / Cmd +)를 따라갑니다. 축소하면 한 줄에 더 많이 들어갑니다. `size` 버튼은 그림 하나의 폭(1400/760/540/420 px).
- 300,000 cycle보다 긴 실행은 처음 300,000 cycle만 저장합니다 (baseline·Set 1의 큰 case는 160,000). 표 숫자는 전체 실행값.

다시 만들려면 (Icarus 필요, 약 45분):

```
./make_waves.sh                     # 전체 suite + 파형 + buffer 크기 비교 -> build/, results/
python3 scripts/vcd_to_html.py      # -> build/waves.html
```

- 파형은 `tb/wave_dump.v`, packet 값은 `tb/pkt_trace.v`가 저장합니다 (`run_official.py --vcd`).

## 자원 (Yosys로 추정, Vivado 아님)

| | BRAM | FF | DSP |
|---|---|---|---|
| baseline | 0 | 2,041 | 16 |
| Set 1 | BRAM36 1개 (A 4 KB) | 2,149 | 16 |
| Set 2 | BRAM36 5개 (A 4 KB + B 16 KB) + LUTRAM 일부 | 2,367 | 18 |
| Set 3 | BRAM36 2개 (A 4 KB + B 4 KB) + LUTRAM 일부 | 2,462 | 18 |
| Set 4 | BRAM36 5개 (A 16 KB + B 4 KB) + LUTRAM 일부 | 4,144 (ACC 3벌 추가) | 17 |

정확한 값은 `synth.py`(Vivado)로 확인해야 합니다.

## Set 3 (단계적 B reuse)는?

Set 3은 `gemm_control.v`의 loop 순서(묶음 → row → col → k)를 바꿔야 하므로 **규칙 밖**입니다. `set3/`에는 `operand_loader.v`(Set 2와 같고 B buffer만 4 KB)와 `gemm_control.v` 두 파일만 있고, `gemm_top.v`는 baseline 그대로입니다. 두 파일의 `BBUF_DEPTH`는 같아야 합니다. 공식 테스트벤치 49 case를 통과했고, 64×128×128에서 같은 4 KB buffer로 Set 2(−60%)보다 훨씬 좋은 −91%입니다. 쓰려면 진행자님 허락이 필요합니다.

## 이름

A reuse buffer (팀 카톡과 Sparsity 팀에서는 "A cache"라고도 부름), B reuse buffer. 코드 이름은 `ABUF_DEPTH`/`a_buf`, `BBUF_DEPTH`/`b_buf`.

## Set 4 — ACC 4개 (output blocking) + B share (규칙 밖)

`set4/`에 4개 파일: `gemm_top.v`, `gemm_control.v`, `operand_loader.v`, `mac_array.v`.
`sparsity_frontend.v`, `result_writer.v`는 baseline 그대로입니다. 설정은 모두 `gemm_top.v` parameter 한 곳에 있습니다.

**loop 순서:** column 그룹 → row 블록(row tile 4개) → column tile → k → **ACC 번호(bank)**

| 부분 | 하는 일 |
|---|---|
| `mac_array` | 부분합 ACC를 `NBANK`(4)벌 둠. packet의 `op_bank`가 어느 tile의 부분합을 갱신할지 선택 |
| `gemm_control` | 위 loop 순서로 descriptor를 냄. tile마다 write를 기다리지 않고, tile의 C 주소를 결과 FIFO에 넣어 순서대로 writer에 줌. loader에 bank·buffer hit/fill·주소를 직접 알려줌 |
| `operand_loader` | bank 0 packet만 B를 읽고, bank 1~3은 직전 bank 0의 B를 그대로 씀 (B share). A buffer 16 KB(row 블록 4 tile × K), B buffer 4 KB(Set 3과 같은 그룹 방식). zero skip 없음 |
| `gemm_top` | 새 신호 연결, bank 번호를 sparsity_frontend 옆으로 작은 FIFO로 전달 |

| parameter (`gemm_top`) | 기본값 | 의미 |
|---|---|---|
| `NBANK` | 4 | ACC 수 = 같이 계산하는 row tile 수. 1이면 Set 3 순서 |
| `USE_A_REUSE`, `USE_B_REUSE` | 1 | A·B reuse buffer |
| `USE_B_SHARE` | 1 | bank 1~3이 bank 0의 B packet 재사용 |
| `NSLOTS` | 4 | packet 슬롯 |
| `ABUF_DEPTH` | 4096 | A buffer entry × 4 B = 16 KB (NBANK × K, K=1024까지) |
| `BBUF_DEPTH` | 1024 | B buffer entry × 4 B = 4 KB |

시뮬레이션에서 바꾸기: `--param top.NBANK=1` 처럼 `top.` 접두사.

결과는 위 "측정 결과" 표를 보세요. 받은 set4.html의 이전 구현(순차 처리)은 64×128×128에서 353,800 cycle이었습니다.
