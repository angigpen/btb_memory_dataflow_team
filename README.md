# Memory/Dataflow 팀 RTL — Set 1, Set 2, Set 3

## 폴더 구성

| 폴더 | 내용 |
|---|---|
| `baseline/` | 원래 RTL 6개 (비교 기준용, 수정 없음) |
| `set1_phase1_2a/` | **Set 1**: Phase 1 (A reuse + zero read skip) + Phase 2a (double buffering) |
| `set2_all_phases/` | **Set 2**: Phase 1 + 2a + 2b (requests in flight) + Phase 3 (on-chip buffer = A reuse + B reuse) |
| `set3_grouped_b/` | **Set 3**: Set 2 + 단계적 B reuse (column tile G개씩 묶기). loader는 Set 2와 같고 `gemm_control`만 다름 |
| `tb/tb_local.sv` | 로컬 self-checking testbench (`tb_gemm.sv`가 없어서 대신 작성). `+VCD=파일`로 파형 저장 |
| `tb/set1.gtkw`, `tb/set2.gtkw` | GTKWave 화면 설정 (주요 신호만 미리 골라 둠) |
| `scripts/` | `run_local.py` (Icarus 회귀 테스트), `summarize.py`, `make_variants.py` (공식 run.py용 variant 폴더 생성) |
| `results/` | 로컬 측정 결과 (`summary.txt`, `local_results.csv`) |

세 세트 모두 **run.py가 컴파일하는 6개 파일 이름 그대로**입니다. RAM 모듈(`sdp_ram`)은 별도 파일이 아니라 `operand_loader.v` 맨 아래에 들어 있습니다.

## 바뀐 파일

| 파일 | 변경 |
|---|---|
| `gemm_control.v` | loop 순서는 **그대로**. 출력만 추가: `req_kk`, `req_reuse_a` (col≠0), `req_reuse_b` (row≠0), `req_bbuf_addr` (col tile×K+kk), `req_last_row` |
| `operand_loader.v` | 새로 작성 (아래) |
| `gemm_top.v` | parameter 추가, 새 신호 연결 |
| `mac_array.v`, `sparsity_frontend.v`, `result_writer.v` | **수정 없음** |

Set 1과 Set 2의 `gemm_control.v`는 같은 파일입니다. Set 3은 loop 순서를 바꾼 별도 `gemm_control.v`를 씁니다 (아래).

## Set 1 — `operand_loader` (FSM 방식, baseline과 구조가 비슷함)

```
IDLE ─accept─┬─(A cache hit)─> A_CRD ──(A 전부 0 & zero skip)──> 완료 (B=0)
             │                   └──> B_REQ <-> B_RSP ──> 완료
             └─(miss)─> A_REQ <-> A_RSP ──> B_REQ <-> B_RSP ──> 완료
완료 = 슬롯 wr을 full로 표시 → MAC이 슬롯 rd를 가져감 (Ping/Pong)
```

| parameter | 의미 |
|---|---|
| `USE_A_REUSE` | col tile 0에서 A 4 byte + zero flag 1 bit를 A cache[kk]에 저장, 다음 col tile부터 cache에서 읽음 |
| `USE_ZERO_SKIP` | cache의 A가 전부 0이면 B read 생략. packet은 B=0으로 **그대로 MAC에 보냄** (first/last 유지) |
| `USE_DBUF` | packet 슬롯 2개 (Ping/Pong) |
| `ACACHE_DEPTH` | A cache entry 수 (기본 1024 × 33 bit ≈ BRAM36 1개). kk ≥ depth이면 메모리에서 읽음 → 어떤 K도 동작 |

세 switch가 모두 0이면 **baseline과 cycle·read 수가 완전히 같습니다** (검증함).

## Set 2 — `operand_loader` (슬라이드 7의 블록 그대로)

```
Request intake ─> 슬롯 할당 ─(RAM 1-cycle read)─> Prep: A cache / B buffer 데이터, zero skip 판단, need mask
Load control   : 가장 오래된 슬롯부터, need mask의 byte마다 1 cycle에 요청 1개, tag FIFO에 {slot, byte} 저장
Response router: 응답마다 tag FIFO를 pop → 해당 슬롯에 byte 기록
                 col tile 0의 마지막 A byte → A cache 쓰기 / row tile 0의 마지막 B byte → B buffer 쓰기
Supply control : 가장 오래된 슬롯이 다 차면 op_valid, MAC handshake로 슬롯 해제
```

| parameter | 기본값 | 의미 |
|---|---|---|
| `USE_A_REUSE`, `USE_ZERO_SKIP` | 1 | Set 1과 같음 |
| `USE_B_REUSE` | 1 | Phase 3: row tile 0에서 B를 B buffer에 저장, 다음 row tile부터 buffer에서 읽음 |
| `NSLOTS` | 4 | packet 슬롯 수 (2 = Ping/Pong). 2의 거듭제곱 |
| `MAX_OUT` | 32 | 동시에 보낼 수 있는 요청 수 (1 = 2b 끔). Little's law로 latency+1 이상 |
| `ACACHE_DEPTH` | 1024 | A cache entry |
| `BBUF_DEPTH` | 4096 | B buffer entry (4 byte씩) = 16 KB = 128×128의 B 전체. 주소 ≥ depth인 B는 메모리에서 읽음 |

규칙:

- B buffer를 채우는 중(row tile 0)에는 zero skip을 하지 않습니다. buffer에 빈칸이 생기기 때문입니다.
- 마지막 row tile이면 B buffer를 채우지 않습니다. row tile이 1개뿐인 입력에서 zero skip이 계속 동작합니다.
- 응답은 **요청 순서대로** 온다고 가정합니다 (인터페이스에 ID가 없음).

## Set 3 — 단계적 B reuse (`gemm_control`만 변경)

loop 순서: **묶음 → row tile → 묶음 안의 col tile → kk**

- 묶음 = column tile G개. 그 B(K × 4G byte)를 B buffer에 보관합니다.
- 묶음의 row tile 0에서 B를 읽어 buffer를 채우고, 다음 row tile부터는 buffer에서 꺼냅니다.
- A는 (묶음, row tile)마다 한 번 읽고 묶음 안의 col tile에서 재사용합니다.
- read 수 = A: M·K·(묶음 수), B: K·N (B는 항상 한 번만)

**G는 시작할 때 자동으로 정합니다:** G = `BBUF_DEPTH` / K (최대 col tile 수까지). 최대 32 cycle이 걸립니다.

| B buffer (`BBUF_DEPTH`) | K=64 | K=128 | K=256 | K=1024 |
|---|---|---|---|---|
| 1 KB (256) | G=4 | G=2 | 묶기 안 함 | 묶기 안 함 |
| 2 KB (512) | G=8 | G=4 | G=2 | 묶기 안 함 |
| **4 KB (1024, 기본값)** | G=16 | **G=8** | G=4 | 묶기 안 함 |
| 16 KB (4096) | G=32 | G=32 (B 전체) | G=16 | G=4 |

묶기 안 함 = Set 2와 같은 순서입니다. 이렇게 되는 경우는 세 가지입니다.
- G < 2일 때 (묶으면 A를 col tile마다 다시 읽게 되어 손해)
- row tile이 1개뿐일 때 (B를 재사용할 다음 row tile이 없음)
- `USE_B_REUSE=0`일 때

기본값 4 KB를 고른 이유: 128×128 기준으로 B buffer 4 KB에서 read가 약 −92% 줄어듭니다. 그보다 크게 해도 추가 감소가 작습니다. BRAM36 1개에 딱 들어갑니다.

### 버퍼 크기 비교 (64×128×128, 측정값)

| variant | B buffer | cycles (lat 1) | cycles (lat 23) | 총 read | read 변화 |
|---|---|---|---|---|---|
| baseline | — | 1,188,866 | 12,723,202 | 524,288 | — |
| Set 1 | — | 679,426 | 6,626,818 | 270,336 | −48.4% |
| Set 2 | 4 KB | 233,392 | 418,612 | 208,896 | −60.2% |
| Set 3 | 1 KB (G=2) | 188,964 | 307,044 | 147,456 | −71.9% |
| Set 3 | 2 KB (G=4) | 138,550 | 204,886 | 81,920 | −84.4% |
| **Set 3** | **4 KB (G=8)** | **113,346** | **153,810** | **49,152** | **−90.6%** |
| Set 3 | 8 KB (G=16) | 100,750 | 128,278 | 32,768 | −93.8% |
| Set 2 = Set 3 | 16 KB (B 전체) | 94,432 | 115,492 | 24,576 | −95.3% |

- **같은 4 KB일 때 Set 2는 −60%, Set 3은 −91%**입니다. Set 2는 buffer에 들어가는 앞쪽 col tile만 재사용하고, Set 3은 순서를 바꿔 buffer를 매번 꽉 채워 씁니다.
- 최대 크기(64×128×1024)에서는 K가 커서 4 KB면 묶기를 안 합니다(Set 2와 같음, −49.9%). 16 KB면 G=4로 **−84.4%**가 됩니다(Set 2 16 KB는 −54.3%).

## 검증 (로컬, Icarus Verilog)

- Set 1·2: 13개 variant × 공식 suite + extended 전부 + 64×64 벤치마크 5개: **793 runs, 0 fail**
- Set 3: 3개 variant × 같은 suite + 묶기가 실제로 일어나는 경계 케이스(13×37×40) + 버퍼 크기 비교: **155 runs, 0 fail**. read 수가 식과 정확히 일치합니다.
- golden 결과 비교, 모든 출력이 정확히 한 번씩 쓰였는지, 주소 범위, handshake 안정성(`valid && !ready`일 때 값 유지)을 확인했습니다.
- 메모리가 요청을 **하나씩만** 받는 경우(`--memmaxout 1`)에도 결과가 맞습니다. 이때는 2b의 이득만 사라집니다.

## 로컬 측정 결과 (요약)

| case | variant | cycles | 배속 | 총 read | read 변화 |
|---|---|---|---|---|---|
| 64×64×64, latency 1 | baseline | 299,522 | 1.00× | 131,072 | — |
| | Set 1 전체 | 175,874 | 1.70× | 69,632 | −46.9% |
| | Set 2 전체 | 27,776 | 10.8× | 8,192 | −93.8% |
| 64×64×64, latency 23 | baseline | 3,183,106 | 1.00× | 131,072 | — |
| | Set 1 전체 | 1,707,778 | 1.86× | 69,632 | −46.9% |
| | Set 2, B reuse 끔 | 131,778 | 24.2× | 69,632 | −46.9% |
| | Set 2 전체 | 34,788 | 91.5× | 8,192 | −93.8% |
| 64×64×64, 0이 90%, latency 23 | Set 1 전체 | 776,098 | 4.10× | 30,812 | −76.5% |
| 64×128×1024 (max) | Set 1 전체 | 5,367,298 | 1.76× | 2,162,688 | −48.4% |
| | Set 2 전체 | 1,974,184 | 4.78× | 1,916,928 | −54.3% |

read 수는 앞서 계산한 값과 정확히 같습니다 (64³: 131,072 → 69,632 → 8,192). 전체 표는 `results/summary.txt`에 있습니다.

**주의**

- cycle 수는 제가 만든 메모리 모델 기준입니다. 공식 `tb_gemm.sv`의 latency 정의에 따라 몇 % 달라질 수 있습니다. read 수는 모델과 상관없이 같습니다.
- max 크기에서 Set 2의 이득이 작은 이유: B 전체가 128 KB라 16 KB buffer에는 1/8만 들어갑니다. 단계적 B reuse(G 묶기)는 loop 순서를 바꿔야 해서 이번에는 넣지 않았습니다.
- Yosys로 합성해 본 추정치 (Vivado 아님, 참고용): Set 1은 BRAM36 1개, FF 약 +100개. Set 2는 BRAM36 5개, FF 약 +300개, LUT 약 +700개. 정확한 값은 `synth.py`(Vivado)로 확인해야 합니다.

## 공식 flow로 돌리기

```
python scripts/make_variants.py --src set1_phase1_2a  --out variants
python scripts/make_variants.py --src set2_all_phases --out variants
python scripts/make_variants.py --src set3_grouped_b  --out variants
python run.py --suite --extended            --rtl-dir baseline        --build build/baseline
python run.py --suite --extended --optimized --rtl-dir variants/s1_all --build build/s1_all
python compare.py build/baseline/results.json build/s1_all/results.json --out s1_all.csv
```

read 수가 바뀌므로 개선 버전에는 `--optimized`가 필요합니다.

## 진행자님께 확인할 것

1. 테스트 메모리가 응답을 기다리지 않고 다음 요청을 받는지 (2b). 받지 않아도 결과는 맞고, 속도 이득만 사라집니다.
2. `gemm_control`에 출력 포트를 추가해도 되는지.
3. B buffer 16 KB (BRAM36 4개) 사용이 괜찮은지. Set 3은 4 KB (BRAM36 1개)로 같은 수준까지 줄일 수 있습니다.
4. Set 3은 `gemm_control`의 loop 순서를 바꿉니다. Compute 팀과 분담을 맞춰야 합니다.
