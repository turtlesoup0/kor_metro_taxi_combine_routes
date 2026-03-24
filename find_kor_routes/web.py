"""웹 API 서버 (FastAPI)"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config, ScoringWeights
from .models import Coord, TransportMode
from .router import HybridRouter

app = FastAPI(title="Find Kor Routes")

STATIC_DIR = Path(__file__).parent / "static"
_config = Config()  # 앱 시작 시 한 번만 생성


# ── 요청/응답 모델 ──

class SearchRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    # 선호도 옵션
    priority: str = "balanced"        # balanced / low_cost / low_transfer / low_fatigue
    allow_walk: bool = True
    allow_taxi: bool = True
    max_walk_m: int = 1500            # 최대 도보 허용 거리 (미터)
    max_taxi_m: int = 7000            # 최대 택시 허용 거리 (미터)


class LegResponse(BaseModel):
    mode: str
    start_lat: float
    start_lng: float
    end_lat: float
    end_lng: float
    start_name: str
    end_name: str
    duration_min: float
    distance_m: float
    cost_won: int
    detail: str
    interval_min: float


class RouteResponse(BaseModel):
    label: str
    total_time_min: float
    total_cost_won: int
    transfer_count: int
    walk_time_min: float
    taxi_cost_won: int
    expected_wait_min: float
    fatigue: float
    score: float
    legs: list[LegResponse]


class SearchResponse(BaseModel):
    routes: list[RouteResponse]


# ── API 엔드포인트 ──

@app.post("/api/search", response_model=SearchResponse)
async def search_routes(req: SearchRequest):
    config = Config()  # 요청마다 fresh config (가중치 변경 위해)
    missing = config.keys.validate()
    if missing:
        raise HTTPException(400, f"API 키 누락: {', '.join(missing)}")

    # 선호도에 따라 가중치 조정
    presets = {
        "balanced":     ScoringWeights(),
        "low_cost":     ScoringWeights(time=0.5, cost=2.0, transfers=2.0, walking=1.0, wait=0.5, fatigue=0.2),
        "low_transfer": ScoringWeights(time=0.8, cost=0.3, transfers=8.0, walking=1.5, wait=1.0, fatigue=0.5),
        "low_fatigue":  ScoringWeights(time=0.6, cost=0.3, transfers=5.0, walking=3.0, wait=1.5, fatigue=1.5),
    }
    config.weights = presets.get(req.priority, ScoringWeights())

    origin = Coord(lng=req.origin_lng, lat=req.origin_lat)
    dest = Coord(lng=req.dest_lng, lat=req.dest_lat)

    router = HybridRouter(config)
    routes = await router.search(
        origin, dest,
        allow_walk=req.allow_walk,
        allow_taxi=req.allow_taxi,
        max_walk_m=req.max_walk_m,
        max_taxi_m=req.max_taxi_m,
    )

    w = config.weights
    result = []
    for route in routes:
        legs = []
        for leg in route.legs:
            legs.append(LegResponse(
                mode=leg.mode.value,
                start_lat=leg.start.lat, start_lng=leg.start.lng,
                end_lat=leg.end.lat, end_lng=leg.end.lng,
                start_name=leg.start_name, end_name=leg.end_name,
                duration_min=round(leg.duration_min, 1),
                distance_m=round(leg.distance_m, 0),
                cost_won=leg.cost_won,
                detail=leg.detail,
                interval_min=leg.interval_min,
            ))
        result.append(RouteResponse(
            label=route.label,
            total_time_min=round(route.total_time_min, 0),
            total_cost_won=route.total_cost_won,
            transfer_count=route.transfer_count,
            walk_time_min=round(route.walk_time_min, 0),
            taxi_cost_won=route.taxi_cost_won,
            expected_wait_min=round(route.expected_wait_min, 0),
            fatigue=round(route.fatigue, 0),
            score=round(route.score(w.time, w.cost, w.transfers,
                                     w.walking, w.wait, w.fatigue), 1),
            legs=legs,
        ))

    return SearchResponse(routes=result)


# ── 지오코딩 (카카오 키워드 검색 프록시) ──

class GeocodeResult(BaseModel):
    lat: float
    lng: float
    name: str


@app.get("/api/geocode", response_model=list[GeocodeResult])
async def geocode(q: str = Query(..., min_length=1)):
    """장소명 → 좌표 변환 (카카오 키워드 검색 API 프록시)."""
    if not _config.keys.kakao_rest:
        raise HTTPException(400, "카카오 API 키 누락")

    headers = {"Authorization": f"KakaoAK {_config.keys.kakao_rest}"}
    async with httpx.AsyncClient(timeout=10) as client:
        # 1차: 키워드 검색 (장소명)
        resp = await client.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers=headers, params={"query": q, "size": 5},
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", [])

        # 2차: 키워드 결과 없으면 주소 검색 fallback
        if not docs:
            resp2 = await client.get(
                "https://dapi.kakao.com/v2/local/search/address.json",
                headers=headers, params={"query": q, "size": 5},
            )
            resp2.raise_for_status()
            addr_docs = resp2.json().get("documents", [])
            return [
                GeocodeResult(
                    lat=float(d["y"]),
                    lng=float(d["x"]),
                    name=d.get("address_name", q),
                )
                for d in addr_docs
            ]

    return [
        GeocodeResult(
            lat=float(d["y"]),
            lng=float(d["x"]),
            name=d["place_name"],
        )
        for d in docs
    ]


# ── 정적 파일 서빙 ──

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
