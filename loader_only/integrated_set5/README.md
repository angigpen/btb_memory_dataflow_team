# Set 5 메모리 + 최종 NM sparsity + NM compute 통합

각 폴더는 독립적인 RTL 여섯 파일입니다. 한 폴더의 .v 여섯 개만 프로젝트에 추가합니다.

| 폴더 | 외부 B 읽기 순서 | 실제 ACC 세트 |
|---|---|---:|
| plan1_acc2 | k별 B 가로 4개 → 해당 k의 ACC별 A | 2 |
| plan1_acc4 | 위와 같음 | 4 |
| plan2_acc2 | 4×4 B를 가로 우선 수집 → k별 A | 2 |
| plan2_acc4 | 위와 같음 | 4 |
| plan3_acc2 | 4×4 B를 세로 우선 수집 → k별 A | 2 |
| plan3_acc4 | 위와 같음 | 4 |

## 소스와 연결

정확한 커밋과 파일 해시는 provenance.json에 있습니다.

- 메모리 팀 GitHub의 loader_only/set5에 있는 각 버전의 operand_loader, gemm_control, result_writer를 그대로 복사했습니다.
- sparsity 팀이 지정한 Sparsity_SET5_NM_Final_GitHub/Sparsity_SET5_NM_Final/rtl/sparsity_frontend.v를 그대로 복사했습니다.
- compute 팀의 NM/rtl/mac_array_compute_nm.v를 사용했습니다. 고정 localparam REUSE=4를 parameter REUSE로 노출하고 top에서 NBANK를 전달합니다. 따라서 ACC 2세트는 실제 배열도 두 세트입니다. 산술/마스크/ACC 순환 로직은 유지했습니다.
- top은 sparsity 팀 최종 top의 연결을 사용하고, 각 폴더의 기본 NBANK와 SET5_PLAN을 해당 버전으로 설정했습니다. 네 세트만 허용하던 설정 검사에 두 세트도 허용합니다.

로더의 A32/B32, bank, first/last → NBANK별 그룹 수집 → A128/B32/tag16/K선택8/work_mask16 → NM compute → 기존 writer 순서입니다.

두 개의 그룹 버퍼에서 다음 입력 수집과 현재 출력 발행을 겹칩니다. 출력은 round → bank 순서입니다. B의 열마다 원래 K offset을 전달하고 compute가 각 열의 A를 선택합니다. A 위치는 PE 마스크에 반영하며, 한 bank의 A가 전부 0이어도 ACC 순서를 위해 해당 bank의 패킷은 보존합니다. B의 0:4·1:4·2:4·dense(3 또는 4 NNZ) 모드를 사용합니다. 초기화/최종 출력용 zero-mask 패킷도 유지합니다.

2/3안의 선읽기는 B 임시 공간 16 B를 사용합니다. sparsity로 들어가는 것은 여전히 A+B packet이므로 별도의 B 조기 분석 경로는 없습니다. 외부 메모리는 scalar INT8, outstanding 1개이며 주소에 따른 추가 지연은 없습니다. 같은 요청 수라면 2/3안은 동일 cycle 수가 나올 수 있습니다.

## 실행과 결과

Python 3, Icarus Verilog의 iverilog/vvp를 사용합니다.

    python3 run.py
    python3 analyze.py

일부만 재실행하려면 다음과 같이 지정합니다. comparison.csv는 이번 실행 결과로 갱신됩니다.

    python3 run.py --cases nm20_2of4,nm_tail,nm20_zeros
    python3 analyze.py

comparison.html은 입력을 골라 여섯 버전을 비교하는 표입니다. comparison.csv는 모든 run의 원본 측정치와 에너지 가정 계산을 담습니다. run.py의 기본 측정은 원본 testbench에 맞춰 MAX_M=64, MAX_N=128, MAX_K=1024 범위를 사용하며 K=1024, 64×128×128, M/N/K tail, reset/restart, random stall, zero 그룹, 특수값, 작은 reuse RAM을 검사합니다.

- cycles는 busy 구간의 실제 cycle 수, time_us는 동일한 100 MHz를 가정한 시간입니다.
- simulator_wall_seconds는 PC에서 이카루스가 실제로 실행된 벽시계 시간입니다. 가속기의 time_us와 별개입니다.
- input_packets/compute_packets로 N:M 압축 전후 전달 수를 비교합니다.
- frontend_stall/mac_stall은 해당 경계의 valid=1,ready=0 cycle 수입니다.
- read 수는 실제 외부 요청 수입니다. 1요청=1바이트입니다.
- buffer_bytes는 RAM, slot의 data/추적 배열, B latch/선읽기, NM 그룹 data/상태, 결과 FIFO와 결과 payload의 논리 저장량입니다. acc_bytes는 별도입니다. buffer_acc_bytes는 둘의 합이며 표에서는 바이트로 올림합니다.
- 주소·제어용 scalar FF, 조합 회로, BRAM 단위 반올림은 제외합니다. FPGA 합성 자원 수나 전체 면적은 측정하지 않았습니다.

전력은 energy_assumptions.json의 예시 pJ 계수로 산출한 동적 활동량 모델입니다. 단위 연산/외부 read/RAM/기록/tag 및 cycle의 에너지를 합해 시간으로 나눈 average_mw_example입니다. 실제 보드 전력, static power, 배치배선, Fmax 및 SAIF 분석은 수행하지 않았습니다. 이 수치만으로 실제 전력 우열을 확정하면 안 됩니다. energy_nj_example도 같이 확인하세요.

파형은 waveforms/<버전>/<입력>/wave.vcd입니다. nm_trace.csv는 실제 NM MAC handshake payload이며 reads.csv는 외부 요청입니다. wave_analysis.json에는 VCD의 상승 에지 직전 값을 직접 읽어 trace와 대조한 결과를 남깁니다. 선택된 A/B와 K index/work_mask를 독립 정답 입력과 비교하고, 실제 수락된 연산으로 C 전체를 다시 구성합니다.
