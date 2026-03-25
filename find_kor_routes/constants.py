"""프로젝트 전역 상수"""

# ── 도보 ──
WALK_SPEED_M_PER_MIN = 80          # 도보 속도 (m/분)

# ── 택시 ──
TAXI_BASE_FARE = 4800              # 기본요금 (원)
TAXI_BASE_DISTANCE_M = 2000       # 기본요금 거리 (m)
TAXI_FARE_PER_M = 0.763           # 추가 m당 요금 (원)
TAXI_AVG_SPEED_M_PER_MIN = 500    # 평균 속도 (m/분, ~30km/h)
ROAD_DISTANCE_FACTOR = 1.3        # 직선→도로 보정계수

# ── 대중교통 요금 ──
TRANSIT_BASE_FARE = 1250           # 기본요금 (원)
TRANSIT_MAX_FARE = 2500            # 최대 추정 요금 (원)
TRANSIT_FARE_PER_5KM = 100         # 10km 초과 시 5km당 추가 (원)

# ── 환승 ──
TRANSFER_WALK_THRESHOLD_M = 200    # 환승 도보 인식 거리 (m)
TRANSFER_WALK_MIN = 3.0            # 환승 도보 소요시간 (분)
TRANSFER_WALK_MAX_MIN = 5.0        # 환승 도보 최대 시간 (분, 이하면 숨김)

# ── 탐색 기준 ──
LONG_DISTANCE_THRESHOLD_M = 30_000  # 장거리 판정 기준 (m)
TAXI_PENALTY_THRESHOLD_KM = 30     # 택시 직행 패널티 시작 거리 (km)
TAXI_PENALTY_PER_KM = 2.0          # 장거리 택시 km당 추가 스코어
INCOMPLETE_ROUTE_THRESHOLD_M = 3000 # 불완전 경로 필터 기준 (m)
FIRST_LAST_MILE_THRESHOLD_M = 1000  # first/last-mile 보충 기준 (m)

# ── 효율성 필터 ──
TAXI_MIN_TIME_SAVE_MIN = 5         # 택시 혼합 최소 시간 절약 (분)
TAXI_COST_PER_MIN_THRESHOLD = 1500  # 분당 택시비 효율 한계 (원/분)

# ── Dijkstra 가중치 ──
WALK_WEIGHT_FACTOR = 1.5           # 도보 가중치 배수
TAXI_COST_PENALTY_DIVISOR = 2000   # 택시비 패널티 제수

# ── 배차간격 Dijkstra 패널티 ──
INTERVAL_HIGH_THRESHOLD = 30       # 높은 배차간격 기준 (분)
INTERVAL_MID_THRESHOLD = 20        # 중간 배차간격 기준 (분)
INTERVAL_HIGH_WEIGHT = 0.8         # 30분+ 가중치
INTERVAL_MID_WEIGHT = 0.5          # 20~30분 가중치
INTERVAL_LOW_WEIGHT = 0.3          # 20분 미만 가중치

# ── 배차간격 피로도 ──
FATIGUE_INTERVAL_VERY_HIGH = 40    # 매우 높은 배차간격 (분)
FATIGUE_INTERVAL_HIGH = 30         # 높은 배차간격 (분)
FATIGUE_PENALTY_VERY_HIGH = 10.0   # 40분+ 추가 피로도
FATIGUE_PENALTY_HIGH = 5.0         # 30~40분 추가 피로도
