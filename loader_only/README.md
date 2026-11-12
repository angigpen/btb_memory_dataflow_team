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

기본 예제는 **A 8×6 × B 6×8 → C 8×8** 관찰용 행렬입니다. 4×4 출력 타일이
2행×2열이고 k=0~5라서 A/B 재사용을 모두 따라갈 수 있습니다. 입력값은 −3~3이며,
첫 4행의 k=2 A packet은 모두 0으로 만들어 B zero skip과 다음 행 타일의 B 미저장분
재읽기를 확인할 수 있습니다. 행렬의 숫자, 준비된 패킷 값, 해당 사건으로 이동하는
버튼을 표시합니다. 작은 B가 모두 들어가므로 이 예제에서 Set 3은 한 그룹으로 동작합니다.
기존 큰 파형은 `큰 성능 측정용` 버튼으로 남겨 두었습니다.

Icarus가 설치된 환경에서 작은 예제만 실제 RTL로 다시 실행하고 기존 HTML에 넣으려면:

```
python3 scripts/make_inspection_demo.py
```

공식 TB로 네 버전의 전체 계산·출력·handshake 검사를 수행하며 기존 큰 파형과 측정값은
보존합니다. 실행 로그와 VCD는 `build/inspection/`에 생성합니다.

`build/waves.html`을 브라우저로 열면 됩니다. 인터넷 연결 없이도 열리는 파일 하나입니다(약 10 MB).

다시 만들려면 (Icarus 필요, 약 15분):

```
./make_waves.sh                     # 공식 테스트벤치로 파형 + 64×128×128 buffer 크기 비교를 build/에 생성
python3 scripts/vcd_to_html.py      # -> build/waves.html
```

- 파형: `rectangular`, `sparse_90`, `long_latency`, 64×128×128 (latency 23). baseline과 Set 1의 큰 케이스는 너무 길어서 처음 160,000 cycle만 저장합니다(결과 숫자는 전체 실행값).
- 파형 저장은 `tb/wave_dump.v`가 합니다. 공식 `tb_gemm.sv`는 고치지 않고 옆에 같이 컴파일합니다 (`run_official.py --vcd`).
- 표 숫자: `results/*.csv` (전체 suite)와 `build/**/summary.csv` (파형·sweep 실행).

### 애니메이션의 A/B reuse buffer 내용

각 다이어그램 아래에 현재 RAM entry의 INT8 값, 원래 행/열·k 좌표, A의 zero flag를 표시합니다.
슬라이더를 되돌리면 그 시점까지의 write만 다시 적용합니다. `미저장`, 값이 모두 `0`인 word,
zero skip으로 `valid=0`인 B entry, 이전 행/그룹의 잔여 데이터를 구분합니다.
현재 접근한 entry를 자동으로 따라가거나 체크를 풀고 entry 페이지를 직접 넘길 수 있습니다.

RAM 배열 자체는 기존 VCD에 없으므로, 기록된 `aw_pend`/`bw_pend` write-enable과 완료된
메모리 read 주소를 공식 `a.hex`/`b.hex`와 대조해 재구성합니다. 요청을 보낸 것만으로
버퍼가 채워졌다고 표시하지 않으며, 상승 에지 c의 write는 기존 파형의 샘플 규칙에 맞춰
c+1부터 보입니다. 다른 입력을 런타임에 덮어쓰는 테스트에는 그 변경 이력까지 필요합니다.

저장소에 원본 VCD가 없으면 기존 HTML의 파형·측정값을 보존해 재생성할 수 있습니다:

```
python3 scripts/vcd_to_html.py --from-html build/waves.html
python3 -m unittest discover -s scripts -p test_buffer_trace.py
```

`--from-html`은 새 시뮬레이션을 실행하지 않습니다. RTL 및 성능 측정값도 변경하지 않습니다.

### 패킷 슬롯(Ping/Pong)에 준비된 값

Set 1의 2개 슬롯과 Set 2·3의 4개 슬롯마다 `op_a`/`op_b`의 lane별 INT8 값,
원래 C 타일 위치, k, first/last를 표시합니다. 미수신 값은 `대기`이며, 값이 0인 것과
구분합니다. 각 값에는 `메모리`(응답 완료), `재사용`(내부 RAM 복사 완료), `패딩`,
`생략`(zero skip으로 B=0) 중 출처를 표시합니다. 빈 슬롯의 이전 물리 비트는 숨깁니다.

예전의 포괄적인 `loading` 대신 내부 RAM 준비, 외부 읽기 차례 대기, 요청/응답 대기,
값 준비 완료/앞 패킷 대기, MAC ready 대기, 전송 가능을 구분합니다. 슬롯이 여러 개
사용 중이어도 외부 읽기 경로는 하나이며, 진행 중인 메모리 요청도 최대 하나입니다.

`packet_trace.py`는 기록된 descriptor 수락·메모리 handshake·reuse RAM write 이력과
공식 입력으로 현재 패킷에 유효하게 준비된 lane만 재구성합니다. 요청 수락만으로 값이
채워지지 않으며 응답 수락 에지 c의 byte는 c+1부터 표시됩니다. 기록된 MAC handshake마다
모든 lane 준비 여부와 입력값을 검사합니다. 긴 파형은 변경 이벤트만 저장합니다.

뷰어의 재생/되감기·슬롯 상태·값 도착 시점은 다음으로 검사합니다(Node 필요):

```
node scripts/test_buffer_view.cjs
```

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
