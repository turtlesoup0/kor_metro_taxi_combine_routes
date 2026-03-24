"""설정 및 환경변수 관리"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    """프로젝트 루트의 .env 파일을 로드한다."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)


_load_env()


@dataclass
class APIKeys:
    kakao_rest: str = field(default_factory=lambda: os.getenv("KAKAO_REST_API_KEY", ""))
    odsay: str = field(default_factory=lambda: os.getenv("ODSAY_API_KEY", ""))

    def validate(self) -> list[str]:
        """누락된 키 목록을 반환한다."""
        missing = []
        if not self.kakao_rest:
            missing.append("KAKAO_REST_API_KEY")
        if not self.odsay:
            missing.append("ODSAY_API_KEY")
        return missing


@dataclass
class ScoringWeights:
    """경로 스코어링 가중치. 사용자가 조절 가능."""
    time: float = 1.0        # 분당 패널티
    cost: float = 0.5        # 1000원당 패널티
    transfers: float = 3.0   # 환승 1회당 패널티 (분 환산)
    walking: float = 1.5     # 도보 1분당 패널티
    wait: float = 0.8        # 배차 대기 1분당 패널티 (expected_wait = interval/2)
    fatigue: float = 0.3     # 피로도 지수 가중치


@dataclass
class SearchConfig:
    """탐색 파라미터"""
    hub_search_radius_m: int = 3000       # 허브 탐색 반경 (미터)
    max_hub_candidates: int = 7           # 최대 허브 후보 수
    min_time_save_ratio: float = 0.85     # 기준 대비 이 비율 이하여야 추천
    max_taxi_distance_m: int = 5000       # 택시 구간 최대 거리


@dataclass
class Config:
    keys: APIKeys = field(default_factory=APIKeys)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    search: SearchConfig = field(default_factory=SearchConfig)
