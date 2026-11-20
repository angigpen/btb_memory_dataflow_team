# B 조기 분석 · 로더 직접 연결 · 6개 RTL

메모리 응답을 operand_loader가 직접 받습니다. B는 별도 입력으로 sparsity_frontend의 기존 두 그룹 버퍼에 저장하고 즉시 nonzero/특수값 태그를 기록합니다. 각 B 열의 K 위치가 모이면 해당 열의 N:4 모드를 등록합니다. A는 독립적인 입력으로 ACC별 그룹 공간에 모읍니다.

## 구성

| 폴더 | B 외부 miss 읽기 | ACC |
|---|---|---:|
| plan1_acc2 | 같은 k의 가로 4개 → 해당 k의 A | 2 |
| plan1_acc4 | 위와 같음 | 4 |
| plan2_acc2 | 4×4 B 가로 우선 → A | 2 |
| plan2_acc4 | 위와 같음 | 4 |
| plan3_acc2 | 4×4 B 세로 우선 → A | 2 |
| plan3_acc4 | 위와 같음 | 4 |

각 폴더의 RTL 6개만 컴파일합니다. 기존 integrated_set5 결과는 baseline_comparison.csv에 복사했습니다. compute와 gemm_control 및 writer는 그 통합본과 같습니다. 각 버전의 A/B reuse RAM은 16 KiB/4 KiB, A packet slot은 4개입니다.

B 값 16개를 loader의 중계 캐시에 복제하지 않습니다. B 저장은 sparsity의 두 그룹 버퍼가 담당합니다. B reuse RAM에 쓸 word도 해당 그룹 버퍼에서 읽어 원래 RAM에 저장합니다. B cache hit은 독립 RAM 포트에서 읽어 A 외부 읽기와 병행할 수 있습니다.

## 새로운 연결

1. loader가 새 K 그룹을 시작할 때 group_valid/group_ready로 frontend의 빈 그룹을 예약합니다. group_ticket은 두 버퍼 중 어느 것인지 나타냅니다. K 시작/길이, 유효 열, 마지막 ACC, 첫/마지막 그룹 여부를 함께 전달합니다.
2. B 응답은 loader의 b_valid/b_ready 채널로 직접 들어갑니다. b_group과 b_pos가 그룹/K 위치를 지정하며, b_data는 최대 네 열, b_mask는 이번에 갱신할 열을 나타냅니다. 외부 scalar 응답은 한 lane, cache 응답은 유효한 word lanes를 전달합니다.
3. frontend는 B data/tag/위치를 받아 즉시 저장합니다. 영값도 도착 여부를 기록하는 b_seen을 따로 두며, 한 열의 모든 유효 K가 도착하면 모드를 확정합니다. cache hit과 scalar miss가 섞여 순서가 바뀌어도 처리합니다.
4. A는 기존 op_valid/op_ready에 op_group/op_pos를 추가해 전달합니다. 그룹의 모든 ACC/K가 준비되면 출력 가능한 full 상태가 됩니다. 기존 in_b 입력은 호환 선언이며 새 B 채널을 사용합니다.
5. NM 출력의 A128/B32/tag16/K선택8/work_mask16 및 round→bank 순서와 Compute의 ACC 선택 방식은 유지합니다.

원본의 A+B 묶음 handshake에서 B를 기다리지 않아도 분석을 시작할 수 있습니다. 한 그룹의 B가 먼저 준비되어도 MAC은 필요한 A까지 모인 뒤 시작합니다. B 분류는 수락 클록에 태그·위치를 갱신하는 회로이므로 별도의 긴 분석 지연을 임의로 넣지 않았습니다.

## 검증과 실행

필요 도구는 Python 3, iverilog, vvp입니다.

    python3 run.py
    python3 tests/check_groups.py
    python3 analyze.py

특정 입력을 재실행할 때 기존 fixture가 있으면 그대로 사용합니다.

    python3 run.py --cases nm20_2of4,nm_tail,nm20_zeros
    python3 analyze.py

run.py는 C 정답, first/last, 물리 ACC, A/B 독립 handshake 및 NM payload 안정성을 검사합니다. K=1024, M/N/K tail, reset/restart, random stall, all-zero/특수값을 포함합니다. check_groups.py는 A depth=8, B depth=8/16에서 partial cache word와 실제 column-group 전환을 검사합니다.

waveforms에는 18개 VCD와 NM trace/입력/metrics가 있습니다. VCD에서 직접 샘플링한 B 입력을 독립적으로 재구성해 실제 등록된 N:4 mode와 대조합니다. 특히 2·3안에서 첫 B mode가 A packet 수락보다 먼저 준비되는 것을 각 그룹마다 검사합니다.

comparison.html은 전후 비교표, before_after.csv는 원본 수치입니다. 첫 B 모드/첫 A 패킷/첫 MAC은 testbench clock 번호, cycles는 busy 구간입니다. 시간은 100 MHz 가정입니다.

전력은 예시 pJ 계수를 적용한 활동량 추정치입니다. FPGA 합성·배치·Fmax·전력 실측을 수행하지 않았습니다. 버퍼 용량은 논리 data/추적 배열 및 그룹 상태 기준이며 일부 scalar 제어 레지스터와 BRAM 단위 반올림을 제외합니다.

## 성능 해석

중계 지연을 제거해 2·3안의 비용을 줄였지만, 모든 입력에서 기존 1안보다 빠르다는 의미는 아닙니다. 새 그룹 예약/독립 B 채널과 cache 공급 방식에 따른 비용도 있습니다. 비교표에 기존/수정 cycle 수를 모두 표시했습니다.

3안은 한 B 열의 N:4를 가장 먼저 확정할 수 있습니다. 다만 같은 총 바이트를 읽고 모든 A가 필요한 경우에는 첫 MAC과 전체 cycle이 2안과 같을 수 있습니다. 현재 외부 메모리 포트는 한 요청에 1 byte, outstanding 1개입니다.
