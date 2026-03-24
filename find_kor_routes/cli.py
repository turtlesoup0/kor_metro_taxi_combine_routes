"""CLI 인터페이스"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Config
from .models import Coord, Route, TransportMode
from .router import HybridRouter


MODE_ICONS = {
    TransportMode.WALK: "[도보]",
    TransportMode.TAXI: "[택시]",
    TransportMode.BUS: "[버스]",
    TransportMode.SUBWAY: "[지하철]",
    TransportMode.TRAIN: "[열차]",
    TransportMode.EXPRESS_BUS: "[고속버스]",
    TransportMode.INTERCITY_BUS: "[시외버스]",
    TransportMode.BUS_SUBWAY: "[대중교통]",
}


def format_route(route: Route, index: int) -> str:
    """경로를 사람이 읽기 좋은 형태로 포맷팅한다."""
    lines = []
    lines.append(f"{'='*56}")
    lines.append(f"  경로 {index}: {route.label}")
    lines.append(f"  총 소요: {route.total_time_min:.0f}분 | "
                 f"총 비용: {route.total_cost_won:,}원 | "
                 f"환승: {route.transfer_count}회 | "
                 f"피로도: {route.fatigue:.0f}")

    # 상세 메타: 도보, 택시비, 배차 대기
    meta_parts = [f"도보 {route.walk_time_min:.0f}분"]
    if route.taxi_cost_won > 0:
        meta_parts.append(f"택시비 {route.taxi_cost_won:,}원")
    if route.expected_wait_min > 0:
        meta_parts.append(f"예상대기 ~{route.expected_wait_min:.0f}분")
    lines.append(f"  ({', '.join(meta_parts)})")

    # 배차간격 상세
    intervals = [(l.detail, l.interval_min) for l in route.legs
                 if l.mode in (TransportMode.BUS, TransportMode.SUBWAY) and l.interval_min > 0]
    if intervals:
        interval_strs = [f"{name} {iv:.0f}분" for name, iv in intervals]
        lines.append(f"  배차간격: {' / '.join(interval_strs)}")

    lines.append(f"{'─'*56}")

    for i, leg in enumerate(route.legs):
        icon = MODE_ICONS.get(leg.mode, "[?]")
        # 도보 구간은 인접 leg의 이름을 참조하여 표시
        if leg.mode == TransportMode.WALK:
            prev_name = route.legs[i - 1].end_name if i > 0 else "출발지"
            next_name = route.legs[i + 1].start_name if i < len(route.legs) - 1 else "도착지"
            name_from = leg.start_name or prev_name or "출발지"
            name_to = leg.end_name or next_name or "도착지"
        else:
            name_from = leg.start_name or str(leg.start)
            name_to = leg.end_name or str(leg.end)

        if leg.mode == TransportMode.WALK:
            lines.append(f"  {icon} {name_from} → {name_to} "
                         f"({leg.duration_min:.0f}분, {leg.distance_m:.0f}m)")
        elif leg.mode == TransportMode.TAXI:
            taxi_km = leg.distance_m / 1000
            lines.append(f"  {icon} {name_from} → {name_to} "
                         f"({leg.duration_min:.0f}분, {taxi_km:.1f}km, {leg.cost_won:,}원)")
        else:
            interval_str = f", 배차 {leg.interval_min:.0f}분" if leg.interval_min > 0 else ""
            lines.append(f"  {icon} {leg.detail}: {name_from} → {name_to} "
                         f"({leg.duration_min:.0f}분{interval_str})")

    lines.append(f"{'='*56}")
    return "\n".join(lines)


def parse_coord(s: str) -> Coord:
    """'위도,경도' 형식의 문자열을 Coord로 파싱한다."""
    parts = s.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"좌표 형식 오류: '{s}' (위도,경도 형식으로 입력)")
    lat, lng = float(parts[0].strip()), float(parts[1].strip())
    return Coord(lng=lng, lat=lat)


async def run(origin: Coord, dest: Coord, config: Config) -> None:
    missing = config.keys.validate()
    if missing:
        print(f"오류: 다음 API 키가 .env에 설정되지 않았습니다: {', '.join(missing)}")
        print(f"  .env.example을 참고하여 .env 파일을 생성해주세요.")
        sys.exit(1)

    print(f"\n출발: {origin}")
    print(f"도착: {dest}")
    print(f"혼합 경로 탐색 중 (반경 3km/5km/10km)...\n")

    router = HybridRouter(config)
    routes = await router.search(origin, dest)

    if not routes:
        print("경로를 찾을 수 없습니다.")
        return

    for i, route in enumerate(routes, 1):
        print(format_route(route, i))
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="택시+대중교통 혼합 경로 탐색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 강남역 → 여의도역
  find-route 37.4979,127.0276 37.5216,126.9243

  # 좌표는 '위도,경도' 형식
  python -m find_kor_routes.cli 37.5665,126.9780 37.4004,127.1030
        """,
    )
    parser.add_argument("origin", type=parse_coord, help="출발지 좌표 (위도,경도)")
    parser.add_argument("dest", type=parse_coord, help="도착지 좌표 (위도,경도)")
    parser.add_argument("--max-hubs", type=int, default=7,
                        help="최대 허브 후보 수 (기본: 7)")

    args = parser.parse_args()

    config = Config()
    config.search.max_hub_candidates = args.max_hubs

    asyncio.run(run(args.origin, args.dest, config))


if __name__ == "__main__":
    main()
