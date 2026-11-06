# Memory/Dataflow 팀 RTL — operand_loader.v만 수정한 버전

배포본 README의 규칙대로 **Memory/Dataflow 팀은 `rtl/operand_loader.v`만 수정**합니다.
`gemm_control.v`, `gemm_top.v`를 포함한 나머지 5개 파일은 baseline 그대로입니다.

## 폴더 구성

| 폴더 | 내용 |
|---|---|
| `set1/operand_loader.v` | **Set 1**: A reuse buffer + zero read skip + Ping/Pong (Phase 1 + 2a) |
| `set2/operand_loader.v` | **Set 2**: Set 1 + B reuse buffer (Phase 3), packet 슬롯 4개 |
| `set3/` | **Set 3**: Set 2 loader (B buffer 4 KB) + loop 순서를 바꾼 `gemm_control.v` (규칙 밖, 아래 참고) |
| `baseline/` | 배포본 RTL 6개 (수정 없음) |
| `tb/` | 배포본 공식 테스트벤치 (`tb_gemm.sv`, `rv_monitor.sv`, `rv_delay.v`) |
| `scripts/run_official.py` | 공식 테스트벤치를 **Vivado 대신 Icarus Verilog**로 돌리는 스크립트 |
| `scripts/summarize_official.py` | baseline 대비 cycle·read 비교 표 |
| `scripts/vcd_to_html.py`, `make_waves.sh` | 발표용 HTML 만들기 (아래) |
| `tb/wave_dump.v` | 파형 저장용 모듈 (공식 테스트벤치는 수정 안 함) |
| `results/` | 측정 결과 (`summary.txt`, variant별 `summary.csv`) |

## 공식 flow로 쓰는 법

1. 배포본의 `rtl/operand_loader.v`를 `set1/` 또는 `set2/`의 파일로 바꿉니다.
2. 실행합니다: `python scripts/run.py --suite --extended --optimized --build build/candidate`
3. 비교합니다: `python scripts/compare.py build/baseline/results.json build/candidate/results.json`

Vivado가 없으면 Icarus로 같은 테스트벤치를 돌릴 수 있습니다:

```
python scripts/run_official.py --rtl-dir baseline --suite --extended --baseline-checks --build build/baseline
python scripts/run_official.py --rtl-dir set1 --suite --extended --build build/set1      # +OPTIMIZED
python scripts/run_official.py --rtl-dir set2 --cases rectangular --param USE_DBUF=0 --build build/x
python scripts/summarize_official.py build
```

`run_official.py`는 `run.py`와 같은 fixture, 같은 plusarg(`+HOST_TEST`, `restart`에 `+RESET_TEST`, `+OPTIMIZED`), 같은 PASS 조건을 씁니다.

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
| `USE_A_REUSE` | 1 | column tile 0에서 A 4 byte + zero flag를 A reuse buffer[kk]에 저장, 다음 column tile부터 buffer에서 읽음 |
| `USE_ZERO_SKIP` | 1 | buffer의 A가 전부 0이면 B read 생략. packet은 B=0으로 그대로 MAC에 보냄 (first/last 유지) |
| `USE_DBUF` | 1 | packet 슬롯 2개 (Ping/Pong) |
| `ACACHE_DEPTH` | 1024 | A reuse buffer entry 수. kk ≥ depth면 A를 메모리에서 읽음 |

세 switch를 모두 0으로 하면 **baseline과 cycle·read가 완전히 같습니다** (공식 테스트벤치, baseline 공식 검사 모드로 확인).

## Set 2 — 블록 구조 (Request intake → Prep → Load control → Response router → Supply control)

요청은 한 번에 1개만 보냅니다(응답을 받은 뒤 다음 요청). 진행 중인 요청의 {슬롯, byte 위치}는 레지스터 하나에 저장합니다.

| parameter | 기본값 | 의미 |
|---|---|---|
| `USE_A_REUSE`, `USE_ZERO_SKIP` | 1 | Set 1과 같음 |
| `USE_B_REUSE` | 1 | row tile 0에서 B를 B reuse buffer에 저장, 다음 row tile부터 buffer에서 읽음 |
| `NSLOTS` | 4 | packet 슬롯 수. 메모리를 안 읽는 packet(두 buffer에서 다 나오는 것)이 앞 packet 뒤에 줄 서서 기다릴 수 있음. 2로 줄이면 64×64에서 44,127 → 51,102 cycle |
| `ACACHE_DEPTH` | 1024 | A reuse buffer entry |
| `BBUF_DEPTH` | 4096 | B reuse buffer entry × 4 byte = 16 KB = K=N=128일 때 B 전체 |

- B reuse buffer 옆에 1-bit valid RAM이 있습니다. row tile 0에서 zero skip으로 B를 안 읽은 칸은 valid=0입니다. 나중 row tile이 그 B가 필요하면 한 번 읽어서 저장합니다. 그래서 B는 **최대 한 번만** 읽습니다.
- 파라미터는 `gemm_top`이 넘겨주지 않으므로 `operand_loader.v`의 기본값을 고쳐서 바꿉니다 (`run_official.py --param`은 시뮬레이션용 defparam).

## 메모리 계약 (INTERFACES.md, tb_gemm.sv)

- 요청 1개 = 1 byte. **미완료 요청은 최대 1개**입니다.
- `mem_req_ready = !pending && !rsp_valid`: 처리 중이거나 응답을 아직 안 가져갔으면 0.
- 그래서 byte 하나에 latency + 2 cycle이 걸립니다. 요청을 여러 개 미리 보내는 방식(2b)은 쓸 수 없어서 코드에서 뺐습니다. 이전 버전(최대 4개)과 cycle 수가 완전히 같은 것을 확인했습니다.
- 남는 방법은 **read 횟수 자체를 줄이는 것**(A·B reuse, zero skip)입니다.

## 측정 결과 (공식 테스트벤치, Icarus)

- baseline: `--suite --extended` 44 case, baseline 공식 검사 포함 **전부 PASS**
- Set 1, Set 2: 같은 44 case, `+OPTIMIZED` **전부 PASS** (정답·주소·중복·handshake 검사 유지)
- Set 3: 같은 44 case 전부 PASS
- variant 비교: 9개 case × 5개 variant 전부 PASS

| case | variant | cycles | 배속 | 총 read | read 변화 |
|---|---|---|---|---|---|
| rectangular (3×7×5) | baseline | 266 | 1.00× | 65 | — |
| | Set 1 | 213 | 1.25× | 50 | −23.1% |
| | Set 2 | 192 | 1.39× | 50 | −23.1% |
| sparse_90 (4×9×13) | Set 1 | 488 | 2.03× | 119 | −56.4% |
| | Set 2 | 436 | 2.27× | 119 | −56.4% |
| 64×64×64, latency 1 | baseline | 430,592 | 1.00× | 131,072 | — |
| | Set 1 | 245,504 | 1.75× | 69,632 | −46.9% |
| | Set 2 | 44,127 | **9.76×** | 8,192 | −93.8% |
| 64×64×64, latency 23 | baseline | 3,314,176 | 1.00× | 131,072 | — |
| | Set 1 | 1,777,408 | 1.86× | 69,632 | −46.9% |
| | Set 2 | 224,351 | **14.77×** | 8,192 | −93.8% |
| 64×64×64, 0이 90% | Set 1 | 129,044 | 3.34× | 30,812 | −76.5% |
| | Set 2 | 43,887 | 9.81× | 8,192 | −93.8% |
| 64×128×128 | Set 1 | 949,760 | 1.80× | 270,336 | −48.4% |
| | Set 2 (16 KB) | 143,535 | **11.94×** | 24,576 | −95.3% |
| | Set 2 (4 KB) | 650,775 | 2.63× | 208,896 | −60.2% |
| | Set 3 (4 KB, loop 변경) | 211,556 | 8.10× | 49,152 | −90.6% |
| 64×128×1024 (max) | Set 1 | 7,529,984 | 1.81× | 2,162,688 | −48.4% |
| | Set 2 (16 KB) | 5,807,571 | 2.35× | 1,916,928 | −54.3% |
| | Set 3 (4 KB) | 6,314,497 | 2.16× | 2,101,248 | −49.9% |

기능별 기여 (64×64×64, latency 1, Set 1 내부): A reuse만 1.65×, Ping/Pong만 1.04×, 0이 90%일 때 zero skip을 더하면 1.65× → 2.97×.
전체 표는 `results/summary.txt`에 있습니다. cycle 수는 Icarus로 측정한 값이며, 테스트벤치가 같으므로 Vivado xsim에서도 같아야 합니다.

## 발표용 HTML (결과 + 파형 + 애니메이션)

`build/waves.html`을 브라우저로 열면 됩니다. 인터넷 연결 없이도 열리는 파일 하나입니다(약 10 MB).

다시 만들려면 (Icarus 필요, 약 15분):

```
./make_waves.sh                     # 공식 테스트벤치로 파형 + 64×128×128 buffer 크기 비교를 build/에 생성
python3 scripts/vcd_to_html.py      # -> build/waves.html
```

- 파형: `rectangular`, `sparse_90`, `long_latency`, 64×128×128 (latency 23). baseline과 Set 1의 큰 케이스는 너무 길어서 처음 160,000 cycle만 저장합니다(결과 숫자는 전체 실행값).
- 파형 저장은 `tb/wave_dump.v`가 합니다. 공식 `tb_gemm.sv`는 고치지 않고 옆에 같이 컴파일합니다 (`run_official.py --vcd`).
- 표 숫자: `results/*.csv` (전체 suite)와 `build/**/summary.csv` (파형·sweep 실행).

## 자원 (Yosys로 추정, Vivado 아님)

| | BRAM | FF | DSP |
|---|---|---|---|
| baseline | 0 | 2,041 | 16 |
| Set 1 | BRAM36 1개 | 2,149 | 16 |
| Set 2 | BRAM36 5개 + BRAM18 1개 + LUTRAM 일부 | 2,367 | 18 |
| Set 3 | BRAM36 2개 + LUTRAM 일부 | 2,463 | 18 |

정확한 값은 `synth.py`(Vivado)로 확인해야 합니다.

## Set 3 (단계적 B reuse)는?

Set 3은 `gemm_control.v`의 loop 순서(묶음 → row → col → k)를 바꿔야 하므로 **규칙 밖**입니다. `set3/`에는 `operand_loader.v`(Set 2와 같고 B buffer만 4 KB)와 `gemm_control.v` 두 파일만 있고, `gemm_top.v`는 baseline 그대로입니다. 두 파일의 `BBUF_DEPTH`는 같아야 합니다. 공식 테스트벤치 44 case를 통과했고, 64×128×128에서 같은 4 KB buffer로 Set 2(−60%)보다 훨씬 좋은 −91%입니다. 쓰려면 진행자님 허락이 필요합니다.

## 이름

A reuse buffer (팀 카톡과 Sparsity 팀에서는 "A cache"라고도 부름), B reuse buffer. 코드의 parameter·instance 이름(`ACACHE_DEPTH`, `a_cache`, `BBUF_DEPTH`, `b_buf`)은 그대로 두었습니다.
