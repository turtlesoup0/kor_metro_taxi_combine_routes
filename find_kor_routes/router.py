"""혼합 경로 탐색 엔진 (그래프 기반)

파이프라인:
1. API 호출: ODsay(대중교통) + Kakao(택시 직행) + Kakao(주변역)
2. 그래프 구성: ODsay 역 + 주변 허브 + 보충 ODsay 조회
3. K-최단경로 → 택시 정밀화 → 스코어 정렬
"""

from __future__ import annotations

import asyncio

from .api_clients import KakaoClient, ODsayClient, StationInfo, close_client
from .config import Config
from .constants import (
    FIRST_LAST_MILE_THRESHOLD_M,
    INCOMPLETE_ROUTE_THRESHOLD_M,
    INTERVAL_HIGH_THRESHOLD,
    LONG_DISTANCE_THRESHOLD_M,
    MAX_TAXI_ONLY_KM,
    ROAD_DISTANCE_FACTOR,
    TAXI_COST_PER_MIN_THRESHOLD,
    TAXI_MIN_TIME_SAVE_MIN,
    TAXI_PENALTY_PER_KM,
    TAXI_PENALTY_THRESHOLD_KM,
)
from .graph import GraphEdge, GraphNode, RouteGraph
from .models import Coord, Route, RouteLeg, TransportMode
from .utils import (
    estimate_taxi,
    haversine_m,
    make_access_leg,
    parse_odsay_traffic_type,
    remove_instation_walks,
    walk_time_min,
)


class HybridRouter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.kakao = KakaoClient(config.keys.kakao_rest)
        self.odsay = ODsayClient(config.keys.odsay)

    # ═══════════════════════════════════════════════════
    #  메인 파이프라인
    # ═══════════════════════════════════════════════════

    async def search(
        self, origin: Coord, dest: Coord,
        allow_walk: bool = True, allow_taxi: bool = True,
        max_walk_m: int = 1500, max_taxi_m: int = 7000,
    ) -> list[Route]:
        w = self.config.weights
        od_distance = haversine_m(origin, dest)
        is_long_distance = od_distance > LONG_DISTANCE_THRESHOLD_M

        # ── 1단계: API 병렬 호출 ──
        fetch = await self._fetch_initial_data(origin, dest, is_long_distance)

        odsay_has_result = (
            _has_paths(fetch["raw_all"]) or _has_paths(fetch["raw_subway"])
        )

        # ODsay 결과 없음 → fallback
        if not odsay_has_result:
            return await self._handle_no_odsay_result(
                origin, dest, fetch, is_long_distance,
                allow_walk, allow_taxi, max_walk_m, max_taxi_m,
            )

        # ── 2단계: 그래프 구성 ──
        graph = self._build_graph(
            origin, dest, fetch, is_long_distance, max_walk_m,
        )

        # 허브역 + 터미널 보충 검색
        await self._add_hub_edges(
            graph, origin, dest, fetch, is_long_distance, max_walk_m,
        )

        if allow_walk:
            graph.add_walk_edges(max_dist_m=max_walk_m)
        if allow_taxi:
            graph.add_taxi_edges(max_dist_m=max_taxi_m)

        print(f"  그래프: {graph.stats()}")

        # ── 3단계: K-최단경로 + 택시 정밀화 ──
        paths = graph.find_diverse_routes()
        if paths:
            estimate_edges = graph.get_estimate_taxi_edges(paths)
            if estimate_edges:
                await self._refine_taxi_edges(estimate_edges, graph)

        # ── 4단계: 경로 변환 + 조립 ──
        routes = await self._assemble_routes(
            origin, dest, fetch, graph, paths, w,
            allow_taxi,
        )

        # ── 4.5단계: 고배차 버스 택시 대체 변형 생성 ──
        routes.extend(self._replace_high_interval_legs(routes))

        result = self._rank_and_filter(routes)
        await close_client()
        return result

    # ═══════════════════════════════════════════════════
    #  1단계: API 호출
    # ═══════════════════════════════════════════════════

    async def _fetch_initial_data(
        self, origin: Coord, dest: Coord, is_long_distance: bool,
    ) -> dict:
        """ODsay + Kakao API를 병렬 호출하여 원본 데이터를 수집."""
        tasks = [
            self.odsay.search_raw(origin, dest, path_type="0"),
            self.odsay.search_raw(origin, dest, path_type="1"),
            self.kakao.get_taxi_route(origin, dest),
            self.kakao.search_nearby_stations(origin, 5000, keyword="지하철역", category="SW8"),
            self.kakao.search_nearby_stations(dest, 5000, keyword="지하철역", category="SW8"),
            self.kakao.search_nearby_stations(origin, 3000, keyword="버스정류장", category="BK9"),
            self.kakao.search_nearby_stations(dest, 3000, keyword="버스정류장", category="BK9"),
            self.kakao.search_nearby_stations(origin, 10000, keyword="기차역", category="SW8"),
            self.kakao.search_nearby_stations(dest, 10000, keyword="기차역", category="SW8"),
        ]
        if is_long_distance:
            tasks.append(self.kakao.search_nearby_terminals(origin, 20000))
            tasks.append(self.kakao.search_nearby_terminals(dest, 20000))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 지하철 + 버스 + 기차역 합산
        stations_origin: list[StationInfo] = []
        for r in (results[3], results[5], results[7]):
            if not isinstance(r, Exception):
                stations_origin.extend(r)
        stations_dest: list[StationInfo] = []
        for r in (results[4], results[6], results[8]):
            if not isinstance(r, Exception):
                stations_dest.extend(r)

        return {
            "raw_all": results[0],
            "raw_subway": results[1],
            "taxi_leg": results[2],
            "stations_origin": stations_origin,
            "stations_dest": stations_dest,
            "terminals_origin": results[9] if is_long_distance else [],
            "terminals_dest": results[10] if is_long_distance else [],
        }

    # ═══════════════════════════════════════════════════
    #  2단계: 그래프 구성
    # ═══════════════════════════════════════════════════

    def _build_graph(
        self, origin: Coord, dest: Coord, fetch: dict,
        is_long_distance: bool, max_walk_m: int,
    ) -> RouteGraph:
        """수집한 데이터로 경로 탐색 그래프를 구성한다.

        동기 메서드이나, 보충 ODsay 검색은 호출자가 별도로 처리한다.
        (현재는 search()에서 인라인으로 실행)
        """
        graph = RouteGraph()
        raw_all = fetch["raw_all"]
        raw_subway = fetch["raw_subway"]

        raw_json = raw_all
        if not isinstance(raw_all, Exception):
            graph.build_from_odsay(raw_all, origin, dest)
        if not isinstance(raw_subway, Exception):
            if isinstance(raw_all, Exception):
                graph.build_from_odsay(raw_subway, origin, dest)
                raw_json = raw_subway
            else:
                graph.build_from_odsay(raw_subway, origin, dest, connect_endpoints=True)
        fetch["raw_json"] = raw_json
        return graph

    async def _add_hub_edges(
        self, graph: RouteGraph, origin: Coord, dest: Coord,
        fetch: dict, is_long_distance: bool, max_walk_m: int,
    ) -> None:
        """주변 허브역 + 터미널을 그래프에 추가하고 보충 ODsay 검색을 실행."""
        stations_origin = fetch["stations_origin"]
        stations_dest = fetch["stations_dest"]
        terminals_origin = fetch["terminals_origin"]
        terminals_dest = fetch["terminals_dest"]

        # 허브 후보 수집 (유형별 제한)
        hub_stations: list[StationInfo] = []
        for src in (stations_origin,):
            hub_stations.extend([s for s in src if s.category == "SW8"][:5])
            hub_stations.extend([s for s in src if s.category == "BK9"][:3])
        for src in (stations_dest,):
            hub_stations.extend([s for s in src if s.category == "SW8"][:5])
            hub_stations.extend([s for s in src if s.category == "BK9"][:3])

        if not isinstance(terminals_origin, Exception) and terminals_origin:
            hub_stations.extend(terminals_origin[:3])
        if not isinstance(terminals_dest, Exception) and terminals_dest:
            hub_stations.extend(terminals_dest[:3])

        # 중복 제거
        seen_names: set[str] = set()
        new_hubs: list[StationInfo] = []
        for s in hub_stations:
            clean = s.name.replace(" ", "")
            if clean not in seen_names:
                seen_names.add(clean)
                new_hubs.append(s)

        # 보충 ODsay 검색 (병렬)
        if new_hubs:
            hub_tasks = []
            for hub in new_hubs:
                hub_tasks.append(self.odsay.search_raw(hub.coord, dest))
                hub_tasks.append(self.odsay.search_raw(origin, hub.coord))

            hub_results = await asyncio.gather(*hub_tasks, return_exceptions=True)
            for i, hub in enumerate(new_hubs):
                if not isinstance(hub_results[i * 2], Exception):
                    graph.build_from_odsay(
                        hub_results[i * 2], hub.coord, dest, connect_endpoints=False)
                if not isinstance(hub_results[i * 2 + 1], Exception):
                    graph.build_from_odsay(
                        hub_results[i * 2 + 1], origin, hub.coord, connect_endpoints=False)

        # 장거리: 터미널 허브 + 교차 검색
        if is_long_distance:
            await self._add_terminal_hub_edges(
                graph, origin, dest, terminals_origin, terminals_dest, max_walk_m,
            )

    async def _add_terminal_hub_edges(
        self, graph: RouteGraph, origin: Coord, dest: Coord,
        terminals_origin, terminals_dest, max_walk_m: int,
    ) -> None:
        """장거리 터미널 노드를 그래프에 추가하고 교차 검색을 실행."""
        t_origins = ([t for t in terminals_origin[:2]]
                     if not isinstance(terminals_origin, Exception) else [])
        t_dests = ([t for t in terminals_dest[:2]]
                   if not isinstance(terminals_dest, Exception) else [])

        # 터미널 노드 + origin/dest 연결
        for t in t_origins:
            tid = f"terminal_{t.name}"
            graph.add_node(GraphNode(tid, t.name, t.coord, "station", "터미널"))
            d = haversine_m(origin, t.coord)
            time_min, cost = estimate_taxi(d)
            graph.add_edge(GraphEdge(
                from_id="origin", to_id=tid,
                mode=TransportMode.TAXI,
                duration_min=time_min, cost_won=cost,
                distance_m=d * ROAD_DISTANCE_FACTOR, detail="택시",
                is_estimate=True, from_name="출발지", to_name=t.name,
            ))
            if d <= max_walk_m:
                graph.add_edge(GraphEdge(
                    from_id="origin", to_id=tid,
                    mode=TransportMode.WALK,
                    duration_min=walk_time_min(d), distance_m=d,
                    detail="도보", from_name="출발지", to_name=t.name,
                ))

        for t in t_dests:
            tid = f"terminal_{t.name}"
            graph.add_node(GraphNode(tid, t.name, t.coord, "station", "터미널"))
            d = haversine_m(t.coord, dest)
            time_min, cost = estimate_taxi(d)
            graph.add_edge(GraphEdge(
                from_id=tid, to_id="dest",
                mode=TransportMode.TAXI,
                duration_min=time_min, cost_won=cost,
                distance_m=d * ROAD_DISTANCE_FACTOR, detail="택시",
                is_estimate=True, from_name=t.name, to_name="도착지",
            ))
            if d <= max_walk_m:
                graph.add_edge(GraphEdge(
                    from_id=tid, to_id="dest",
                    mode=TransportMode.WALK,
                    duration_min=walk_time_min(d), distance_m=d,
                    detail="도보", from_name=t.name, to_name="도착지",
                ))

        # 터미널 간 교차 검색
        if t_origins and t_dests:
            cross_tasks = []
            cross_pairs = []
            for to_ in t_origins:
                for td in t_dests:
                    cross_tasks.append(self.odsay.search_raw(to_.coord, td.coord))
                    cross_pairs.append((to_, td))

            cross_results = await asyncio.gather(*cross_tasks, return_exceptions=True)
            for (to_, td), raw in zip(cross_pairs, cross_results):
                if isinstance(raw, Exception):
                    continue
                graph.build_from_odsay(raw, to_.coord, td.coord, connect_endpoints=False)
                self._connect_terminal_to_odsay_stations(
                    graph, raw, to_, td)

    @staticmethod
    def _connect_terminal_to_odsay_stations(
        graph: RouteGraph, raw: dict,
        origin_terminal: StationInfo, dest_terminal: StationInfo,
    ) -> None:
        """ODsay 교차검색 결과의 첫/끝 역을 터미널 노드와 도보 연결."""
        for pd in raw.get("result", {}).get("path", []):
            subs = pd.get("subPath", [])
            # 첫 대중교통 구간의 첫 역 → origin 터미널 연결
            for s in subs:
                if s.get("trafficType") in (1, 2, 4, 5, 6):
                    stops = s.get("passStopList", {}).get("stations", [])
                    if stops and stops[0].get("x") and stops[0].get("y"):
                        lane = s.get("lane", [{}])[0]
                        line = lane.get("name", "") or lane.get("busNo", "")
                        first_id = f"{stops[0].get('stationName', '')}_{line}"
                        to_tid = f"terminal_{origin_terminal.name}"
                        if first_id in graph.nodes and to_tid in graph.nodes:
                            graph.add_edge(GraphEdge(
                                from_id=to_tid, to_id=first_id,
                                mode=TransportMode.WALK,
                                duration_min=3, distance_m=200,
                                detail="터미널도보",
                                from_name=origin_terminal.name,
                                to_name=stops[0].get("stationName", ""),
                            ))
                    break
            # 마지막 대중교통 구간의 끝 역 → dest 터미널 연결
            for s in reversed(subs):
                if s.get("trafficType") in (1, 2, 4, 5, 6):
                    stops = s.get("passStopList", {}).get("stations", [])
                    if stops and stops[-1].get("x") and stops[-1].get("y"):
                        lane = s.get("lane", [{}])[0]
                        line = lane.get("name", "") or lane.get("busNo", "")
                        last_id = f"{stops[-1].get('stationName', '')}_{line}"
                        td_tid = f"terminal_{dest_terminal.name}"
                        if last_id in graph.nodes and td_tid in graph.nodes:
                            graph.add_edge(GraphEdge(
                                from_id=last_id, to_id=td_tid,
                                mode=TransportMode.WALK,
                                duration_min=3, distance_m=200,
                                detail="터미널도보",
                                from_name=stops[-1].get("stationName", ""),
                                to_name=dest_terminal.name,
                            ))
                    break

    # ═══════════════════════════════════════════════════
    #  4단계: 경로 조립
    # ═══════════════════════════════════════════════════

    async def _assemble_routes(
        self, origin: Coord, dest: Coord, fetch: dict,
        graph: RouteGraph, paths: list, w,
        allow_taxi: bool,
    ) -> list[Route]:
        """baseline + 그래프 + 택시직행 경로를 조립한다."""
        routes: list[Route] = []

        # baseline ODsay 경로
        raw_json = fetch.get("raw_json", fetch["raw_all"])
        raw_subway = fetch["raw_subway"]
        taxi_leg = fetch["taxi_leg"]

        baseline_routes = self._parse_odsay_routes(raw_json, origin, dest)
        if not isinstance(raw_subway, Exception) and raw_subway is not raw_json:
            baseline_routes.extend(self._parse_odsay_routes(raw_subway, origin, dest))
        baseline_routes = [br for br in baseline_routes if br.legs]

        # first-mile/last-mile 보충 (필터 전에)
        baseline_routes = await self._supplement_first_last_mile(
            baseline_routes, origin, dest)

        # 불완전 경로 필터 (보충 후)
        baseline_routes = [
            br for br in baseline_routes
            if br.legs and haversine_m(br.legs[-1].end, dest) <= INCOMPLETE_ROUTE_THRESHOLD_M
        ]

        if baseline_routes:
            type_seen: dict[str, float] = {}
            for br in sorted(baseline_routes, key=lambda r: r.score(
                    w.time, w.cost, w.transfers, w.walking, w.wait, w.fatigue)):
                mode_sig = "+".join(sorted(set(
                    l.mode.value for l in br.legs if l.mode != TransportMode.WALK)))
                if mode_sig not in type_seen:
                    br.label = _auto_route_label(br)
                    routes.append(br)
                    type_seen[mode_sig] = br.score(
                        w.time, w.cost, w.transfers, w.walking, w.wait, w.fatigue)

        # 그래프 경로
        for path in paths:
            route = graph.path_to_route(path)
            if route.legs:
                routes.append(route)

        # 택시 직행
        if allow_taxi and not isinstance(taxi_leg, Exception):
            km = taxi_leg.distance_m / 1000
            if km <= MAX_TAXI_ONLY_KM:
                routes.append(Route(
                    legs=[taxi_leg],
                    label=f"택시 ({km:.1f}km 전구간)",
                ))

        return routes

    # ═══════════════════════════════════════════════════
    #  fallback: ODsay 결과 없을 때
    # ═══════════════════════════════════════════════════

    async def _handle_no_odsay_result(
        self, origin: Coord, dest: Coord, fetch: dict,
        is_long_distance: bool,
        allow_walk: bool, allow_taxi: bool,
        max_walk_m: int, max_taxi_m: int,
    ) -> list[Route]:
        """ODsay 직접 검색 실패 시 인근 거점 경유 경로를 탐색."""
        taxi_leg = fetch["taxi_leg"]
        fallback = await self._fallback_via_nearby_hubs(
            origin, dest,
            fetch["stations_origin"], fetch["stations_dest"],
            fetch["terminals_origin"] if is_long_distance else [],
            fetch["terminals_dest"] if is_long_distance else [],
        )
        if fallback:
            if allow_taxi and not isinstance(taxi_leg, Exception):
                km = taxi_leg.distance_m / 1000
                if km <= MAX_TAXI_ONLY_KM:
                    fallback.append(Route(
                        legs=[taxi_leg], label=f"택시 ({km:.1f}km 전구간)"))
            result = self._rank_and_filter(fallback)
            await close_client()
            return result

        if allow_taxi and not isinstance(taxi_leg, Exception):
            await close_client()
            return [Route(legs=[taxi_leg], label="택시 (전구간)")]
        await close_client()
        return []

    async def _fallback_via_nearby_hubs(
        self, origin: Coord, dest: Coord,
        stations_origin: list, stations_dest: list,
        terminals_origin: list, terminals_dest: list,
    ) -> list[Route]:
        """인근 거점(터미널/역) 경유 경로 탐색."""
        routes: list[Route] = []

        dest_hubs = _collect_hubs(terminals_dest, stations_dest)
        origin_hubs = _collect_hubs(terminals_origin, stations_origin)

        # 전략A: 출발지→거점(대중교통) + 거점→도착지(택시/도보)
        tasks_a = [self.odsay.search_raw(origin, h.coord) for h in dest_hubs]
        # 전략B: 출발지→거점(택시/도보) + 거점→도착지(대중교통)
        tasks_b = [self.odsay.search_raw(h.coord, dest) for h in origin_hubs]

        all_results = await asyncio.gather(
            *(tasks_a + tasks_b), return_exceptions=True)

        for hub, raw in zip(dest_hubs, all_results[:len(tasks_a)]):
            if isinstance(raw, Exception):
                continue
            parsed = self._parse_odsay_routes(raw, origin, None)
            valid = [r for r in parsed if r.legs]
            if not valid:
                continue
            best = min(valid, key=lambda r: r.total_time_min)
            last = make_access_leg(hub.coord, dest, hub.name, "도착지")
            new_legs = list(best.legs)
            while new_legs and new_legs[-1].mode == TransportMode.WALK:
                new_legs.pop()
            new_legs.append(last)
            routes.append(Route(legs=new_legs, label=f"대중교통→{hub.name}→택시"))

        for hub, raw in zip(origin_hubs, all_results[len(tasks_a):]):
            if isinstance(raw, Exception):
                continue
            parsed = self._parse_odsay_routes(raw, None, dest)
            valid = [r for r in parsed if r.legs]
            if not valid:
                continue
            best = min(valid, key=lambda r: r.total_time_min)
            first = make_access_leg(origin, hub.coord, "출발지", hub.name)
            new_legs = list(best.legs)
            while new_legs and new_legs[0].mode == TransportMode.WALK:
                new_legs.pop(0)
            new_legs.insert(0, first)
            routes.append(Route(legs=new_legs, label=f"택시→{hub.name}→대중교통"))

        if routes:
            routes = await self._supplement_first_last_mile(routes, origin, dest)
        return routes

    # ═══════════════════════════════════════════════════
    #  first-mile / last-mile 보충
    # ═══════════════════════════════════════════════════

    async def _supplement_first_last_mile(
        self, routes: list[Route], origin: Coord, dest: Coord,
        threshold_m: float = FIRST_LAST_MILE_THRESHOLD_M,
    ) -> list[Route]:
        """출발지→첫역, 끝역→도착지가 먼 경우 대중교통/택시로 보충."""
        first_searches: dict[str, Coord] = {}
        last_searches: dict[str, Coord] = {}

        for r in routes:
            if not r.legs:
                continue
            ft = next((l for l in r.legs if l.mode.is_transit), None)
            if ft:
                d = haversine_m(origin, ft.start)
                if d > threshold_m:
                    key = f"{ft.start.lat:.4f},{ft.start.lng:.4f}"
                    first_searches[key] = ft.start
            lt = next((l for l in reversed(r.legs) if l.mode.is_transit), None)
            if lt:
                d = haversine_m(lt.end, dest)
                if d > threshold_m:
                    key = f"{lt.end.lat:.4f},{lt.end.lng:.4f}"
                    last_searches[key] = lt.end

        if not first_searches and not last_searches:
            return routes

        # 보충 검색 (병렬)
        tasks, task_keys = [], []
        for key, coord in first_searches.items():
            tasks.append(self.odsay.search_raw(origin, coord))
            task_keys.append(("first", key))
        for key, coord in last_searches.items():
            tasks.append(self.odsay.search_raw(coord, dest))
            task_keys.append(("last", key))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        first_routes: dict[str, Route] = {}
        last_routes: dict[str, Route] = {}
        for (mtype, key), raw in zip(task_keys, results):
            if isinstance(raw, Exception):
                continue
            parsed = self._parse_odsay_routes(
                raw, origin if mtype == "first" else None,
                dest if mtype == "last" else None)
            valid = [r for r in parsed if r.legs]
            if not valid:
                continue
            best = min(valid, key=lambda r: r.total_time_min)
            (first_routes if mtype == "first" else last_routes)[key] = best

        # 접합
        supplemented: list[Route] = []
        for r in routes:
            if not r.legs:
                supplemented.append(r)
                continue
            new_legs = list(r.legs)

            # 첫 구간 보충
            ft = next((l for l in new_legs if l.mode.is_transit), None)
            if ft and haversine_m(origin, ft.start) > threshold_m:
                key = f"{ft.start.lat:.4f},{ft.start.lng:.4f}"
                fm = first_routes.get(key)
                if fm and fm.legs:
                    while new_legs and new_legs[0].mode == TransportMode.WALK:
                        new_legs.pop(0)
                    fm_legs = list(fm.legs)
                    if fm_legs and fm_legs[-1].mode == TransportMode.WALK:
                        fm_legs.pop()
                    new_legs = fm_legs + new_legs
                else:
                    d = haversine_m(origin, ft.start)
                    access = make_access_leg(origin, ft.start, "출발지",
                                             ft.start_name or "터미널")
                    while new_legs and new_legs[0].mode == TransportMode.WALK:
                        new_legs.pop(0)
                    new_legs.insert(0, access)

            # 마지막 구간 보충
            lt = next((l for l in reversed(new_legs) if l.mode.is_transit), None)
            if lt and haversine_m(lt.end, dest) > threshold_m:
                key = f"{lt.end.lat:.4f},{lt.end.lng:.4f}"
                lm = last_routes.get(key)
                if lm and lm.legs:
                    while new_legs and new_legs[-1].mode == TransportMode.WALK:
                        new_legs.pop()
                    lm_legs = list(lm.legs)
                    if lm_legs and lm_legs[0].mode == TransportMode.WALK:
                        lm_legs.pop(0)
                    new_legs = new_legs + lm_legs
                else:
                    access = make_access_leg(lt.end, dest,
                                             lt.end_name or "터미널", "도착지")
                    while new_legs and new_legs[-1].mode == TransportMode.WALK:
                        new_legs.pop()
                    new_legs.append(access)

            supplemented.append(Route(legs=new_legs, label=r.label))
        return supplemented

    # ═══════════════════════════════════════════════════
    #  택시 정밀화
    # ═══════════════════════════════════════════════════

    async def _refine_taxi_edges(
        self, edges: dict[str, GraphEdge], graph: RouteGraph,
    ) -> None:
        """추정 택시 엣지를 Kakao API 실측치로 교체."""
        async def refine_one(edge: GraphEdge) -> None:
            from_node = graph.nodes.get(edge.from_id)
            to_node = graph.nodes.get(edge.to_id)
            if not from_node or not to_node:
                return
            try:
                real = await self.kakao.get_taxi_route(from_node.coord, to_node.coord)
                edge.duration_min = real.duration_min
                edge.cost_won = real.cost_won
                edge.distance_m = real.distance_m
                edge.is_estimate = False
            except Exception:
                pass

        await asyncio.gather(
            *[refine_one(e) for e in edges.values()],
            return_exceptions=True,
        )

    # ═══════════════════════════════════════════════════
    #  고배차 버스 택시 대체
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _replace_high_interval_legs(routes: list[Route]) -> list[Route]:
        """배차간격 30분+ 버스 구간을 택시로 대체한 변형 경로를 생성."""
        variants: list[Route] = []
        for route in routes:
            for idx, leg in enumerate(route.legs):
                if not leg.mode.is_bus_like:
                    continue
                if leg.interval_min < INTERVAL_HIGH_THRESHOLD:
                    continue
                # 해당 구간을 택시로 대체
                dist = haversine_m(leg.start, leg.end)
                taxi_time, taxi_cost = estimate_taxi(dist)
                taxi_leg = RouteLeg(
                    mode=TransportMode.TAXI,
                    start=leg.start, end=leg.end,
                    start_name=leg.start_name, end_name=leg.end_name,
                    duration_min=taxi_time,
                    distance_m=dist * ROAD_DISTANCE_FACTOR,
                    cost_won=taxi_cost,
                    detail="택시",
                    interval_min=0.0,
                )
                new_legs = route.legs[:idx] + [taxi_leg] + route.legs[idx + 1:]
                label = _auto_route_label(Route(legs=new_legs))
                variants.append(Route(legs=new_legs, label=label))
        return variants

    # ═══════════════════════════════════════════════════
    #  스코어 정렬 + 필터링
    # ═══════════════════════════════════════════════════

    def _rank_and_filter(self, routes: list[Route], max_results: int = 8) -> list[Route]:
        """스코어 정렬 + 유형별 다양성 보장 + 불합리 경로 제거."""
        w = self.config.weights

        def score(r: Route) -> float:
            s = r.score(w.time, w.cost, w.transfers, w.walking, w.wait, w.fatigue)
            # 장거리 택시 직행 패널티
            modes = [l.mode for l in r.legs if l.mode != TransportMode.WALK]
            if modes and all(m == TransportMode.TAXI for m in modes):
                total_km = r.total_distance_m / 1000
                if total_km > TAXI_PENALTY_THRESHOLD_KM:
                    s += (total_km - TAXI_PENALTY_THRESHOLD_KM) * TAXI_PENALTY_PER_KM
            return s

        transit_only = [r for r in routes
                        if all(l.mode != TransportMode.TAXI for l in r.legs)]
        best_transit_time = min((r.total_time_min for r in transit_only), default=999)

        def _transit_sig(r: Route) -> str:
            return "|".join(l.detail for l in r.legs if l.mode.is_transit)

        filtered: list[Route] = []
        for r in routes:
            # 불연속 경로 제거 (leg 끝 좌표 ≠ 다음 leg 시작 좌표, 3km+ 갭)
            if _has_gap(r):
                continue

            # 전구간 택시: 다구간 분할 택시 제거 (단일 구간 택시 직행은 유지)
            modes = [l.mode for l in r.legs if l.mode != TransportMode.WALK]
            if modes and all(m == TransportMode.TAXI for m in modes):
                taxi_legs_only = [l for l in r.legs if l.mode == TransportMode.TAXI]
                if len(taxi_legs_only) > 1:
                    continue

            # 개별 택시 구간이 30km 초과하면 비합리적 (first/last mile 제외)
            taxi_legs = [l for l in r.legs if l.mode == TransportMode.TAXI]
            if len(taxi_legs) > 0 and any(l.mode.is_transit for l in r.legs):
                max_taxi_km = max(l.distance_m / 1000 for l in taxi_legs)
                if max_taxi_km > 30:
                    continue

            transit_legs = [l for l in r.legs if l.mode.is_transit]
            if max(0, len(transit_legs) - 1) > 4:
                continue

            has_taxi = any(l.mode == TransportMode.TAXI for l in r.legs)
            if has_taxi and len(r.legs) >= 2:
                taxi_cost = sum(l.cost_won for l in r.legs if l.mode == TransportMode.TAXI)
                my_sig = _transit_sig(r)
                similar = [t for t in transit_only
                           if _transit_sig(t) == my_sig
                           or my_sig in _transit_sig(t)
                           or _transit_sig(t) in my_sig]
                if similar:
                    best_similar = min(t.total_time_min for t in similar)
                    saved = best_similar - r.total_time_min
                    if saved <= TAXI_MIN_TIME_SAVE_MIN:
                        continue
                    if saved > 0 and taxi_cost / saved > TAXI_COST_PER_MIN_THRESHOLD:
                        continue

                if r.legs[-1].mode == TransportMode.TAXI:
                    if r.legs[-1].distance_m / 1000 < 5 and r.total_time_min >= best_transit_time:
                        continue
                if r.legs[0].mode == TransportMode.TAXI:
                    if r.legs[0].distance_m / 1000 < 5 and r.total_time_min >= best_transit_time:
                        continue

            filtered.append(r)

        scored = sorted(filtered, key=score)
        if not scored:
            return []

        # 유형별 최선 보장
        type_best: dict[str, Route] = {}
        for r in scored:
            rt = _route_type(r)
            if rt not in type_best:
                type_best[rt] = r

        result: list[Route] = []
        seen_sigs: set[str] = set()

        def route_sig(r: Route) -> str:
            parts = []
            for l in r.legs:
                if l.mode == TransportMode.WALK and l.duration_min < 1:
                    continue
                parts.append(f"{l.mode.value}:{l.detail}")
            return "|".join(parts)

        # 유형별 최선을 반드시 포함하되, 전체 스코어 순으로 정렬
        must_include: set[int] = set()
        for r in type_best.values():
            must_include.add(id(r))

        for r in scored:
            sig = route_sig(r)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            result.append(r)
            if len(result) >= max_results and id(r) not in must_include:
                break

        # 최종 스코어 순 정렬
        result.sort(key=score)
        return result[:max_results]

    # ═══════════════════════════════════════════════════
    #  ODsay 파싱
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _parse_odsay_routes(
        raw_json: dict, origin: Coord | None = None, dest: Coord | None = None,
    ) -> list[Route]:
        """ODsay 원본 JSON에서 대중교통 경로를 직접 파싱."""
        result = raw_json.get("result", {})
        paths = result.get("path", [])
        routes: list[Route] = []

        for path in paths:
            info = path.get("info", {})
            legs: list[RouteLeg] = []

            for sub in path.get("subPath", []):
                parsed = parse_odsay_traffic_type(sub)
                if parsed is None:
                    continue
                mode, detail, interval = parsed

                legs.append(RouteLeg(
                    mode=mode,
                    start=Coord(lng=float(sub.get("startX", 0)),
                                lat=float(sub.get("startY", 0))),
                    end=Coord(lng=float(sub.get("endX", 0)),
                              lat=float(sub.get("endY", 0))),
                    start_name=sub.get("startName", ""),
                    end_name=sub.get("endName", ""),
                    duration_min=sub.get("sectionTime", 0),
                    distance_m=sub.get("distance", 0),
                    detail=detail,
                    interval_min=interval,
                ))

            # 역 내 환승 도보 제거
            legs = remove_instation_walks(legs)

            # 좌표 누락 보정
            if legs and origin:
                first = legs[0]
                if first.mode == TransportMode.WALK and first.start.lat == 0:
                    first.start = origin
                    first.start_name = first.start_name or "출발지"
                if first.mode == TransportMode.WALK and first.end.lat == 0:
                    if len(legs) > 1:
                        first.end = legs[1].start
            if legs and dest:
                last = legs[-1]
                if last.mode == TransportMode.WALK and last.end.lat == 0:
                    last.end = dest
                    last.end_name = last.end_name or "도착지"
                if last.mode == TransportMode.WALK and last.start.lat == 0:
                    if len(legs) > 1:
                        last.start = legs[-2].end

            route = Route(legs=legs, label="대중교통")
            total_payment = info.get("payment") or 0
            if total_payment == 0:
                total_payment = sum(
                    sub.get("payment", 0) or 0
                    for sub in path.get("subPath", []))
            transit_legs = [l for l in legs if l.mode.is_transit]
            if transit_legs:
                transit_legs[-1].cost_won = total_payment
            routes.append(route)

        return routes


# ── 헬퍼 함수 ──

def _has_paths(raw: dict | Exception) -> bool:
    """ODsay 응답에 실제 경로가 있는지 확인."""
    if isinstance(raw, Exception):
        return False
    return bool(raw.get("result", {}).get("path"))


def _collect_hubs(terminals, stations, max_terminals: int = 3, max_stations: int = 5) -> list:
    """터미널 + 역에서 허브 후보를 수집."""
    hubs: list = []
    if not isinstance(terminals, Exception) and terminals:
        hubs.extend(terminals[:max_terminals])
    if not isinstance(stations, Exception) and stations:
        hubs.extend(stations[:max_stations])
    return hubs


def _has_gap(r: Route, max_gap_m: float = 3000) -> bool:
    """경로에 불연속 구간(leg 끝 ≠ 다음 leg 시작)이 있는지 확인."""
    for i in range(len(r.legs) - 1):
        gap = haversine_m(r.legs[i].end, r.legs[i + 1].start)
        if gap > max_gap_m:
            return True
    return False


def _auto_route_label(r: Route) -> str:
    """경로 구간 정보로 라벨 자동 생성."""
    parts: list[str] = []
    for leg in r.legs:
        if leg.mode == TransportMode.WALK:
            continue
        if leg.mode == TransportMode.TAXI:
            km = leg.distance_m / 1000
            parts.append(f"택시({km:.1f}km)")
        else:
            parts.append(leg.detail)
    return "→".join(parts) if parts else "대중교통"


def _route_type(r: Route) -> str:
    """경로를 패턴 유형으로 분류."""
    modes = [l.mode for l in r.legs if l.mode != TransportMode.WALK]
    if not modes:
        return "walk"
    if all(m == TransportMode.TAXI for m in modes):
        return "taxi"
    if all(m != TransportMode.TAXI for m in modes):
        return "transit"

    first_transit = next((i for i, m in enumerate(modes) if m != TransportMode.TAXI), -1)
    last_transit = len(modes) - 1 - next(
        (i for i, m in enumerate(reversed(modes)) if m != TransportMode.TAXI), -1)
    has_taxi_start = first_transit > 0
    has_taxi_end = last_transit < len(modes) - 1

    if has_taxi_start and has_taxi_end:
        return "taxi-transit-taxi"
    if has_taxi_start:
        return "taxi-transit"
    if has_taxi_end:
        return "transit-taxi"

    # 중간 택시 (대중교통→택시→대중교통)
    has_mid_taxi = any(
        modes[i] == TransportMode.TAXI
        and any(m != TransportMode.TAXI for m in modes[:i])
        and any(m != TransportMode.TAXI for m in modes[i + 1:])
        for i in range(len(modes))
    )
    if has_mid_taxi:
        return "transit-taxi-transit"

    return "transit"
