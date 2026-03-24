"""외부 API 클라이언트 (카카오, ODsay)

세마포어로 동시 요청 수를 제한하여 API 안정성을 보장한다.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import urllib.parse

import httpx

from dataclasses import dataclass

from .models import Coord, RouteLeg, TransportMode


# ── 공유 클라이언트 + 동시성 제한 ──

_shared_client: Optional[httpx.AsyncClient] = None
_semaphore: Optional[asyncio.Semaphore] = None


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(5)
    return _semaphore


async def get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=15,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _shared_client


async def close_client() -> None:
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None


# ── 카카오 API ──

@dataclass
class StationInfo:
    """주변 역 검색 결과."""
    name: str
    coord: Coord
    category: str  # "SW8" (지하철), "BK9" (버스정류장)
    distance_m: float


class KakaoClient:
    LOCAL_BASE = "https://dapi.kakao.com/v2/local"
    NAVI_BASE = "https://apis-navi.kakaomobility.com/v1"

    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"KakaoAK {api_key}"}

    async def search_nearby_stations(
        self, center: Coord, radius_m: int = 5000, keyword: str = "지하철역",
        category: str = "SW8",
    ) -> list[StationInfo]:
        """주변 지하철역/버스정류장 검색."""
        client = await get_client()
        async with get_semaphore():
            resp = await client.get(
                f"{self.LOCAL_BASE}/search/keyword.json",
                headers=self._headers,
                params={
                    "query": keyword, "x": str(center.lng), "y": str(center.lat),
                    "radius": radius_m, "sort": "distance", "size": 10,
                },
            )
        resp.raise_for_status()
        return [
            StationInfo(
                name=doc["place_name"],
                coord=Coord(lng=float(doc["x"]), lat=float(doc["y"])),
                category=category,
                distance_m=float(doc.get("distance", 0)),
            )
            for doc in resp.json().get("documents", [])
        ]

    async def search_nearby_terminals(
        self, center: Coord, radius_m: int = 20000,
    ) -> list[StationInfo]:
        """주변 버스터미널 검색 (시외/고속/종합)."""
        client = await get_client()
        results: list[StationInfo] = []
        for keyword in ["버스터미널", "고속버스터미널"]:
            async with get_semaphore():
                resp = await client.get(
                    f"{self.LOCAL_BASE}/search/keyword.json",
                    headers=self._headers,
                    params={
                        "query": keyword, "x": str(center.lng), "y": str(center.lat),
                        "radius": radius_m, "sort": "distance", "size": 5,
                    },
                )
            resp.raise_for_status()
            for doc in resp.json().get("documents", []):
                # 카테고리로 실제 터미널인지 확인 (SW8=지하철, BK9=버스정류장)
                cat = doc.get("category_group_code", "")
                name = doc["place_name"]
                # "터미널"이 이름에 포함된 것만
                if "터미널" in name:
                    results.append(StationInfo(
                        name=name,
                        coord=Coord(lng=float(doc["x"]), lat=float(doc["y"])),
                        category="BT",  # Bus Terminal
                        distance_m=float(doc.get("distance", 0)),
                    ))
        # 중복 제거 (이름 기준)
        seen: set[str] = set()
        unique: list[StationInfo] = []
        for s in results:
            if s.name not in seen:
                seen.add(s.name)
                unique.append(s)
        return unique

    async def get_taxi_route(self, origin: Coord, dest: Coord) -> RouteLeg:
        """카카오 내비 API로 택시 경로 조회."""
        client = await get_client()
        async with get_semaphore():
            resp = await client.get(
                f"{self.NAVI_BASE}/directions",
                headers=self._headers,
                params={
                    "origin": f"{origin.lng},{origin.lat}",
                    "destination": f"{dest.lng},{dest.lat}",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        route_data = data["routes"][0]
        summary = route_data["summary"]
        fare = summary.get("fare", {})
        return RouteLeg(
            mode=TransportMode.TAXI,
            start=origin, end=dest,
            duration_min=summary["duration"] / 60,
            distance_m=summary["distance"],
            cost_won=fare.get("taxi", 0),
            detail="택시",
        )


# ── ODsay API ──

class ODsayClient:
    BASE = "https://api.odsay.com/v1/api"

    def __init__(self, api_key: str) -> None:
        # httpx params가 자동 인코딩하므로 원본 그대로 사용
        self._key = api_key

    async def search_raw(
        self, origin: Coord, dest: Coord, path_type: str = "0",
    ) -> dict:
        """대중교통 경로 원본 JSON 반환.

        path_type: "0"=전체, "1"=지하철, "2"=버스
        """
        client = await get_client()
        async with get_semaphore():
            resp = await client.get(
                f"{self.BASE}/searchPubTransPathT",
                params={
                    "SX": str(origin.lng), "SY": str(origin.lat),
                    "EX": str(dest.lng), "EY": str(dest.lat),
                    "apiKey": self._key,
                    "OPT": "0", "SearchType": "0",
                    "SearchPathType": path_type,
                },
            )
        resp.raise_for_status()
        return resp.json()
