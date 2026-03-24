"""공용 유틸리티 함수"""

from __future__ import annotations

import math

from .constants import (
    ROAD_DISTANCE_FACTOR,
    TAXI_AVG_SPEED_M_PER_MIN,
    TAXI_BASE_DISTANCE_M,
    TAXI_BASE_FARE,
    TAXI_FARE_PER_M,
    WALK_SPEED_M_PER_MIN,
)
from .models import Coord, RouteLeg, TransportMode


def haversine_m(a: Coord, b: Coord) -> float:
    """두 좌표 사이 직선거리 (미터)."""
    R = 6_371_000
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def estimate_taxi(dist_m: float) -> tuple[float, int]:
    """직선거리 기반 택시 시간/비용 추정.

    Returns: (소요시간_분, 비용_원)
    """
    road_dist = dist_m * ROAD_DISTANCE_FACTOR
    time_min = road_dist / TAXI_AVG_SPEED_M_PER_MIN
    cost = TAXI_BASE_FARE + max(0, road_dist - TAXI_BASE_DISTANCE_M) * TAXI_FARE_PER_M
    return round(time_min, 1), int(cost)


def walk_time_min(dist_m: float) -> float:
    """거리 기반 도보 소요시간 (분)."""
    return dist_m / WALK_SPEED_M_PER_MIN


def make_access_leg(
    start: Coord, end: Coord,
    start_name: str = "", end_name: str = "",
    walk_threshold_m: float = 300,
) -> RouteLeg:
    """출발지↔거점 간 접근 구간 생성 (가까우면 도보, 멀면 택시)."""
    d = haversine_m(start, end)
    if d <= walk_threshold_m:
        return RouteLeg(
            mode=TransportMode.WALK, start=start, end=end,
            start_name=start_name, end_name=end_name,
            duration_min=walk_time_min(d), distance_m=d, detail="도보",
        )
    time_min, cost = estimate_taxi(d)
    return RouteLeg(
        mode=TransportMode.TAXI, start=start, end=end,
        start_name=start_name, end_name=end_name,
        duration_min=time_min, distance_m=d * ROAD_DISTANCE_FACTOR,
        cost_won=cost, detail="택시",
    )


def parse_odsay_traffic_type(sub: dict) -> tuple[TransportMode, str, float] | None:
    """ODsay subPath의 trafficType → (모드, 노선명, 배차간격) 변환.

    None을 반환하면 해당 subPath는 스킵한다.
    trafficType: 1=지하철, 2=버스, 3=도보, 4=열차, 5=고속버스, 6=시외버스
    """
    tt = sub.get("trafficType")
    interval = sub.get("intervalTime", 0)
    lane = sub.get("lane", [{}])[0]

    if tt == 1:
        return TransportMode.SUBWAY, lane.get("name", "지하철"), interval
    if tt == 2:
        return TransportMode.BUS, lane.get("busNo", "버스"), interval
    if tt == 3:
        return TransportMode.WALK, "도보", 0
    if tt == 4:
        name = lane.get("name", "") or lane.get("busNo", "열차")
        return TransportMode.TRAIN, name, interval
    if tt == 5:
        return TransportMode.EXPRESS_BUS, lane.get("busNo", "고속버스"), interval
    if tt == 6:
        return TransportMode.INTERCITY_BUS, lane.get("busNo", "시외버스"), interval
    return None


def remove_instation_walks(legs: list[RouteLeg]) -> list[RouteLeg]:
    """대중교통 사이의 짧은 도보(역 내 환승)를 제거.

    대중교통→도보(≤5분)→대중교통 패턴에서 도보를 건너뛰고
    환승 시간을 다음 대중교통 구간에 합산한다.
    """
    if not legs:
        return []

    result: list[RouteLeg] = []
    i = 0
    while i < len(legs):
        leg = legs[i]
        if (leg.mode == TransportMode.WALK
                and leg.duration_min <= 5
                and i > 0 and i < len(legs) - 1
                and result and result[-1].mode.is_transit
                and legs[i + 1].mode.is_transit):
            legs[i + 1].duration_min += leg.duration_min
            i += 1
            continue
        # 0분 & 0m 도보 구간 제거
        if (leg.mode == TransportMode.WALK
                and leg.duration_min < 0.5 and leg.distance_m < 50):
            i += 1
            continue
        result.append(leg)
        i += 1
    return result
