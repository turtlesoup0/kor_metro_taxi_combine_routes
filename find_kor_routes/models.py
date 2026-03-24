"""경로 탐색에 사용되는 데이터 모델"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .constants import (
    FATIGUE_INTERVAL_HIGH,
    FATIGUE_INTERVAL_VERY_HIGH,
    FATIGUE_PENALTY_HIGH,
    FATIGUE_PENALTY_VERY_HIGH,
)


class TransportMode(Enum):
    WALK = "walk"
    TAXI = "taxi"
    BUS = "bus"
    SUBWAY = "subway"
    TRAIN = "train"                # 열차 (KTX/SRT/무궁화/ITX 등)
    EXPRESS_BUS = "express_bus"     # 고속버스
    INTERCITY_BUS = "intercity_bus" # 시외버스
    BUS_SUBWAY = "bus+subway"      # 대중교통 복합

    @classmethod
    def transit_modes(cls) -> tuple[TransportMode, ...]:
        """택시/도보를 제외한 대중교통 모드."""
        return (cls.BUS, cls.SUBWAY, cls.TRAIN, cls.EXPRESS_BUS, cls.INTERCITY_BUS)

    @property
    def is_transit(self) -> bool:
        """대중교통 모드 여부."""
        return self in self.transit_modes()

    @property
    def is_bus_like(self) -> bool:
        """버스류 모드 (배차간격 피로도 적용 대상)."""
        return self in (self.BUS, self.EXPRESS_BUS, self.INTERCITY_BUS)


@dataclass
class Coord:
    """위경도 좌표 (WGS84)"""
    lng: float  # 경도
    lat: float  # 위도

    def __str__(self) -> str:
        return f"({self.lat:.5f}, {self.lng:.5f})"


@dataclass
class TransitHub:
    """환승 허브 (지하철역, 버스정류장 등)"""
    name: str
    coord: Coord
    category: str       # "SW8" (지하철), "BK9" (버스정류장) 등
    distance_m: float   # 기준점으로부터 거리
    score: float = 0.0  # 허브 우선순위 스코어


@dataclass
class MidStation:
    """기준 경로의 중간역 — 택시 전환 후보"""
    name: str
    coord: Coord
    line_name: str          # 노선명
    leg_index: int          # 몇 번째 대중교통 구간인지
    station_index: int      # 해당 구간 내 몇 번째 역인지
    cumulative_time: float  # 출발지부터 이 역까지 누적 소요시간 (분)


@dataclass
class RouteLeg:
    """경로의 한 구간"""
    mode: TransportMode
    start: Coord
    end: Coord
    start_name: str = ""
    end_name: str = ""
    duration_min: float = 0.0     # 소요시간 (분)
    distance_m: float = 0.0      # 거리 (미터)
    cost_won: int = 0            # 비용 (원)
    detail: str = ""             # 노선명 등 상세 정보
    interval_min: float = 0.0    # 배차간격 (분, 대중교통만 해당)


@dataclass
class Route:
    """전체 경로 (여러 구간의 조합)"""
    legs: list[RouteLeg] = field(default_factory=list)
    label: str = ""
    total_interval_min: float = 0.0

    @property
    def total_time_min(self) -> float:
        return sum(leg.duration_min for leg in self.legs)

    @property
    def total_cost_won(self) -> int:
        return sum(leg.cost_won for leg in self.legs)

    @property
    def total_distance_m(self) -> float:
        return sum(leg.distance_m for leg in self.legs)

    @property
    def transfer_count(self) -> int:
        count = 0
        for i in range(1, len(self.legs)):
            if self.legs[i].mode != self.legs[i - 1].mode:
                count += 1
        return count

    @property
    def walk_time_min(self) -> float:
        return sum(leg.duration_min for leg in self.legs if leg.mode == TransportMode.WALK)

    @property
    def taxi_cost_won(self) -> int:
        return sum(leg.cost_won for leg in self.legs if leg.mode == TransportMode.TAXI)

    @property
    def taxi_time_min(self) -> float:
        return sum(leg.duration_min for leg in self.legs if leg.mode == TransportMode.TAXI)

    @property
    def expected_wait_min(self) -> float:
        """예상 대기시간: 배차간격 구간별 가중 합산.

        20분 미만: interval/2
        20~30분: interval/2 * 1.3
        30분 초과: interval/2 * 1.6
        """
        total = 0.0
        for l in self.legs:
            if l.mode.is_transit and l.interval_min > 0:
                base = l.interval_min / 2
                if l.interval_min >= 30:
                    total += base * 1.6
                elif l.interval_min >= 20:
                    total += base * 1.3
                else:
                    total += base
        return total

    @property
    def max_interval_min(self) -> float:
        """대중교통 구간 중 최대 배차간격"""
        intervals = [l.interval_min for l in self.legs
                     if l.mode.is_transit and l.interval_min > 0]
        return max(intervals) if intervals else 0.0

    @property
    def fatigue(self) -> float:
        """피로도 지수: 환승·도보·대기·최대배차간격의 복합 지표 (낮을수록 편함)

        고배차간격 버스(30분+) 추가 페널티:
        - 30~40분: 구간당 +5
        - 40분 초과: 구간당 +10
        """
        base = (
            self.transfer_count * 4.0
            + self.walk_time_min * 1.5
            + self.expected_wait_min * 1.0
            + self.max_interval_min * 0.5
        )
        for l in self.legs:
            if l.mode.is_bus_like and l.interval_min > 0:
                if l.interval_min >= FATIGUE_INTERVAL_VERY_HIGH:
                    base += FATIGUE_PENALTY_VERY_HIGH
                elif l.interval_min >= FATIGUE_INTERVAL_HIGH:
                    base += FATIGUE_PENALTY_HIGH
        return base

    def score(self, w_time: float = 1.0, w_cost: float = 0.5,
              w_transfer: float = 3.0, w_walk: float = 1.5,
              w_wait: float = 0.8, w_fatigue: float = 0.3) -> float:
        """낮을수록 좋은 통합 스코어."""
        return (
            self.total_time_min * w_time
            + (self.total_cost_won / 1000) * w_cost
            + self.transfer_count * w_transfer
            + self.walk_time_min * w_walk
            + self.expected_wait_min * w_wait
            + self.fatigue * w_fatigue
        )
