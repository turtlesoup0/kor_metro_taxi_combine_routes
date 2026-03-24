"""그래프 기반 혼합 경로 탐색 엔진

ODsay 응답에서 역 노드를 추출하고, 택시/도보 엣지를 추가하여
Yen's K-최단경로 알고리즘으로 다양한 혼합 경로를 자동 발견한다.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

from .constants import (
    INTERVAL_HIGH_THRESHOLD,
    INTERVAL_HIGH_WEIGHT,
    INTERVAL_LOW_WEIGHT,
    INTERVAL_MID_THRESHOLD,
    INTERVAL_MID_WEIGHT,
    ROAD_DISTANCE_FACTOR,
    TAXI_COST_PENALTY_DIVISOR,
    TRANSFER_WALK_MIN,
    TRANSFER_WALK_THRESHOLD_M,
    TRANSIT_BASE_FARE,
    TRANSIT_FARE_PER_5KM,
    TRANSIT_MAX_FARE,
    WALK_WEIGHT_FACTOR,
)
from .models import Coord, Route, RouteLeg, TransportMode
from .utils import (
    haversine_m,
    estimate_taxi,
    parse_odsay_traffic_type,
    remove_instation_walks,
    walk_time_min,
)


# ── 그래프 자료구조 ──

@dataclass
class GraphNode:
    """그래프 노드."""
    id: str
    name: str
    coord: Coord
    node_type: str  # "origin" | "destination" | "station"
    line_name: str = ""


@dataclass
class GraphEdge:
    """그래프 엣지 (방향 있음)."""
    from_id: str
    to_id: str
    mode: TransportMode
    duration_min: float
    cost_won: int = 0
    distance_m: float = 0.0
    detail: str = ""
    interval_min: float = 0.0
    is_estimate: bool = False  # 택시 추정치 여부
    from_name: str = ""
    to_name: str = ""


class RouteGraph:
    """혼합 경로 탐색용 방향 그래프.

    노드: 출발지, 도착지, ODsay 중간역
    엣지: 대중교통(연속역), 환승도보, 택시(추정), 도보
    """

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.adj: dict[str, list[GraphEdge]] = {}
        self._odsay_payments: dict[int, int] = {}

    def add_node(self, node: GraphNode) -> None:
        if node.id not in self.nodes:
            self.nodes[node.id] = node
            self.adj[node.id] = []

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.from_id in self.nodes and edge.to_id in self.nodes:
            self.adj[edge.from_id].append(edge)

    # ── 그래프 구성 ──

    def build_from_odsay(
        self, raw_json: dict, origin: Coord, dest: Coord,
        connect_endpoints: bool = True,
    ) -> None:
        """ODsay 응답으로 노드 + 대중교통 엣지를 구성한다."""
        if connect_endpoints:
            self.add_node(GraphNode("origin", "출발지", origin, "origin"))
            self.add_node(GraphNode("dest", "도착지", dest, "destination"))

        result = raw_json.get("result", {})
        paths = result.get("path", [])

        for pi, path in enumerate(paths):
            info = path.get("info", {})
            self._odsay_payments[pi] = info.get("payment", 0)
            prev_last_id: Optional[str] = None
            prev_transfer_time: float = 0.0
            first_station_id: Optional[str] = None
            last_station_id: Optional[str] = None

            sub_paths = path.get("subPath", [])
            for si, sub in enumerate(sub_paths):
                parsed = parse_odsay_traffic_type(sub)
                if parsed is None:
                    continue
                mode, line_name, interval = parsed

                if mode == TransportMode.WALK:
                    prev_transfer_time = sub.get("sectionTime", 0)
                    continue

                section_time = sub.get("sectionTime", 0)
                stops = sub.get("passStopList", {}).get("stations", [])
                n_stops = len(stops)
                if n_stops == 0:
                    continue

                # 역 노드 생성
                station_ids: list[str] = []
                for stop in stops:
                    sx, sy = stop.get("x"), stop.get("y")
                    if not sx or not sy:
                        continue
                    sname = stop.get("stationName", "역")
                    node_id = f"{sname}_{line_name}"
                    self.add_node(GraphNode(
                        node_id, sname,
                        Coord(lng=float(sx), lat=float(sy)),
                        "station", line_name,
                    ))
                    station_ids.append(node_id)

                if station_ids and first_station_id is None:
                    first_station_id = station_ids[0]
                if station_ids:
                    last_station_id = station_ids[-1]

                # 연속역 간 대중교통 엣지
                per_stop = section_time / max(n_stops - 1, 1)
                for i in range(len(station_ids) - 1):
                    self.add_edge(GraphEdge(
                        from_id=station_ids[i], to_id=station_ids[i + 1],
                        mode=mode, duration_min=per_stop,
                        detail=line_name, interval_min=interval,
                        from_name=self.nodes[station_ids[i]].name,
                        to_name=self.nodes[station_ids[i + 1]].name,
                    ))

                # 환승 엣지: 이전 구간 마지막역 → 이번 구간 첫역
                if prev_last_id and station_ids:
                    self.add_edge(GraphEdge(
                        from_id=prev_last_id, to_id=station_ids[0],
                        mode=TransportMode.WALK,
                        duration_min=prev_transfer_time,
                        detail="환승도보",
                        from_name=self.nodes[prev_last_id].name,
                        to_name=self.nodes[station_ids[0]].name,
                    ))

                prev_last_id = station_ids[-1] if station_ids else prev_last_id
                prev_transfer_time = 0.0

            # 출발지 → 첫역, 끝역 → 도착지 도보 엣지
            if not connect_endpoints:
                continue

            if first_station_id:
                first_node = self.nodes[first_station_id]
                d = haversine_m(origin, first_node.coord)
                wt = walk_time_min(d)
                first_sub = sub_paths[0] if sub_paths else {}
                if first_sub.get("trafficType") == 3:
                    wt = first_sub.get("sectionTime", wt)
                    d = first_sub.get("distance", d)
                self.add_edge(GraphEdge(
                    from_id="origin", to_id=first_station_id,
                    mode=TransportMode.WALK,
                    duration_min=wt, distance_m=d,
                    detail="도보",
                    from_name="출발지", to_name=first_node.name,
                ))

            if last_station_id:
                last_node = self.nodes[last_station_id]
                d = haversine_m(last_node.coord, dest)
                wt = walk_time_min(d)
                last_sub = sub_paths[-1] if sub_paths else {}
                if last_sub.get("trafficType") == 3:
                    wt = last_sub.get("sectionTime", wt)
                    d = last_sub.get("distance", d)
                self.add_edge(GraphEdge(
                    from_id=last_station_id, to_id="dest",
                    mode=TransportMode.WALK,
                    duration_min=wt, distance_m=d,
                    detail="도보",
                    from_name=last_node.name, to_name="도착지",
                ))

    def add_walk_edges(self, max_dist_m: float = 1500) -> None:
        """출발/도착 ↔ 가까운 역 + 같은 역 다른 노선 환승."""
        origin = self.nodes.get("origin")
        dest = self.nodes.get("dest")
        stations = [n for n in self.nodes.values() if n.node_type == "station"]

        for sn in stations:
            if origin:
                d = haversine_m(origin.coord, sn.coord)
                if d <= max_dist_m:
                    self.add_edge(GraphEdge(
                        from_id="origin", to_id=sn.id,
                        mode=TransportMode.WALK,
                        duration_min=walk_time_min(d), distance_m=d,
                        detail="도보", from_name="출발지", to_name=sn.name,
                    ))
            if dest:
                d = haversine_m(sn.coord, dest.coord)
                if d <= max_dist_m:
                    self.add_edge(GraphEdge(
                        from_id=sn.id, to_id="dest",
                        mode=TransportMode.WALK,
                        duration_min=walk_time_min(d), distance_m=d,
                        detail="도보", from_name=sn.name, to_name="도착지",
                    ))

        # 같은 역 다른 노선: 환승 도보
        for i, n1 in enumerate(stations):
            for n2 in stations[i + 1:]:
                if n1.line_name == n2.line_name:
                    continue
                d = haversine_m(n1.coord, n2.coord)
                if d < TRANSFER_WALK_THRESHOLD_M:
                    for a, b in [(n1, n2), (n2, n1)]:
                        self.add_edge(GraphEdge(
                            from_id=a.id, to_id=b.id,
                            mode=TransportMode.WALK,
                            duration_min=TRANSFER_WALK_MIN, distance_m=d,
                            detail="환승도보",
                            from_name=a.name, to_name=b.name,
                        ))

    def add_taxi_edges(
        self, max_dist_m: float = 7000, min_taxi_m: float = 1000,
    ) -> None:
        """택시 엣지 추가 (추정치)."""
        origin = self.nodes.get("origin")
        dest = self.nodes.get("dest")
        stations = [n for n in self.nodes.values() if n.node_type == "station"]

        if origin:
            for sn in stations:
                d = haversine_m(origin.coord, sn.coord)
                if min_taxi_m < d <= max_dist_m:
                    t, c = estimate_taxi(d)
                    self.add_edge(GraphEdge(
                        from_id="origin", to_id=sn.id,
                        mode=TransportMode.TAXI,
                        duration_min=t, cost_won=c, distance_m=d * ROAD_DISTANCE_FACTOR,
                        detail="택시", is_estimate=True,
                        from_name="출발지", to_name=sn.name,
                    ))

        if dest:
            for sn in stations:
                d = haversine_m(sn.coord, dest.coord)
                if min_taxi_m < d <= max_dist_m:
                    t, c = estimate_taxi(d)
                    self.add_edge(GraphEdge(
                        from_id=sn.id, to_id="dest",
                        mode=TransportMode.TAXI,
                        duration_min=t, cost_won=c, distance_m=d * ROAD_DISTANCE_FACTOR,
                        detail="택시", is_estimate=True,
                        from_name=sn.name, to_name="도착지",
                    ))

        # 출발 → 도착 직행 택시
        if origin and dest:
            d = haversine_m(origin.coord, dest.coord)
            t, c = estimate_taxi(d)
            self.add_edge(GraphEdge(
                from_id="origin", to_id="dest",
                mode=TransportMode.TAXI,
                duration_min=t, cost_won=c, distance_m=d * ROAD_DISTANCE_FACTOR,
                detail="택시", is_estimate=True,
                from_name="출발지", to_name="도착지",
            ))

        # 역 ↔ 역 (다른 노선, 1~5km)
        for i, n1 in enumerate(stations):
            for n2 in stations[i + 1:]:
                if n1.line_name == n2.line_name:
                    continue
                d = haversine_m(n1.coord, n2.coord)
                if 1000 < d <= 5000:
                    t, c = estimate_taxi(d)
                    for a, b in [(n1, n2), (n2, n1)]:
                        self.add_edge(GraphEdge(
                            from_id=a.id, to_id=b.id,
                            mode=TransportMode.TAXI,
                            duration_min=t, cost_won=c,
                            distance_m=d * ROAD_DISTANCE_FACTOR,
                            detail="택시", is_estimate=True,
                            from_name=a.name, to_name=b.name,
                        ))

    # ── 경로 탐색 ──

    def _dijkstra(
        self, start: str, end: str,
        allow_taxi: bool = True,
    ) -> Optional[list[GraphEdge]]:
        """Dijkstra 최단경로."""
        heap: list[tuple[float, int, str, list[GraphEdge]]] = [(0.0, 0, start, [])]
        visited: set[str] = set()
        cnt = 0

        while heap:
            cost, _, node, path = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            if node == end:
                return path

            for edge in self.adj.get(node, []):
                nxt = edge.to_id
                if nxt in visited:
                    continue
                if not allow_taxi and edge.mode == TransportMode.TAXI:
                    continue

                w = edge.duration_min
                if edge.mode == TransportMode.WALK:
                    w *= WALK_WEIGHT_FACTOR
                if edge.interval_min > 0:
                    iv = edge.interval_min
                    if iv >= INTERVAL_HIGH_THRESHOLD:
                        w += iv * INTERVAL_HIGH_WEIGHT
                    elif iv >= INTERVAL_MID_THRESHOLD:
                        w += iv * INTERVAL_MID_WEIGHT
                    else:
                        w += iv * INTERVAL_LOW_WEIGHT
                if edge.mode == TransportMode.TAXI:
                    w += edge.cost_won / TAXI_COST_PENALTY_DIVISOR

                cnt += 1
                heapq.heappush(heap, (cost + w, cnt, nxt, path + [edge]))

        return None

    def find_diverse_routes(self, max_entries: int = 5, max_exits: int = 8) -> list[list[GraphEdge]]:
        """전략별 Dijkstra로 다양한 패턴의 경로를 탐색한다."""
        results: list[list[GraphEdge]] = []
        seen: set[tuple] = set()

        def _add(path: Optional[list[GraphEdge]]) -> bool:
            if not path:
                return False
            sig = tuple((e.from_id, e.to_id) for e in path)
            if sig in seen:
                return False
            seen.add(sig)
            results.append(path)
            return True

        # S1: 대중교통 전용
        _add(self._dijkstra("origin", "dest", allow_taxi=False))

        # 진입/탈출 택시 엣지 수집
        taxi_entries = sorted(
            [e for e in self.adj.get("origin", []) if e.mode == TransportMode.TAXI],
            key=lambda e: e.duration_min,
        )[:max_entries]

        taxi_exits: list[GraphEdge] = []
        for nid in self.adj:
            for e in self.adj[nid]:
                if e.to_id == "dest" and e.mode == TransportMode.TAXI:
                    taxi_exits.append(e)
        taxi_exits.sort(key=lambda e: e.duration_min)
        taxi_exits = taxi_exits[:max_exits]

        origin_walk_lines = self._reachable_lines("origin")
        dest_walk_lines = self._reachable_lines("dest")

        # S2: 택시→대중교통
        for entry in taxi_entries:
            transit_path = self._dijkstra(entry.to_id, "dest", allow_taxi=False)
            if not transit_path:
                continue
            first_transit = next(
                (e for e in transit_path if e.mode.is_transit), None)
            if first_transit and first_transit.detail in origin_walk_lines:
                continue
            _add([entry] + transit_path)

        # S3: 대중교통→택시
        for exit_e in taxi_exits:
            transit_path = self._dijkstra("origin", exit_e.from_id, allow_taxi=False)
            if not transit_path:
                continue
            last_transit = next(
                (e for e in reversed(transit_path) if e.mode.is_transit), None)
            if last_transit and last_transit.detail in dest_walk_lines:
                continue
            _add(transit_path + [exit_e])

        # S4: 택시→대중교통→택시
        for entry in taxi_entries[:3]:
            for exit_e in taxi_exits[:5]:
                transit_path = self._dijkstra(
                    entry.to_id, exit_e.from_id, allow_taxi=False)
                if not transit_path:
                    continue
                first_transit = next(
                    (e for e in transit_path if e.mode.is_transit), None)
                last_transit = next(
                    (e for e in reversed(transit_path) if e.mode.is_transit), None)
                if first_transit and first_transit.detail in origin_walk_lines:
                    continue
                if last_transit and last_transit.detail in dest_walk_lines:
                    continue
                _add([entry] + transit_path + [exit_e])

        return results

    def _reachable_lines(self, node_id: str) -> set[str]:
        """해당 노드에서 도보로 접근 가능한 대중교통 노선명 집합."""
        lines: set[str] = set()
        for edge in self.adj.get(node_id, []):
            if edge.mode == TransportMode.WALK:
                for e2 in self.adj.get(edge.to_id, []):
                    if e2.mode.is_transit:
                        lines.add(e2.detail)
        return lines

    # ── 결과 변환 ──

    def path_to_route(self, path: list[GraphEdge], label: str = "") -> Route:
        """그래프 경로 → Route 객체."""
        legs: list[RouteLeg] = []
        for edge in path:
            from_node = self.nodes[edge.from_id]
            to_node = self.nodes[edge.to_id]
            legs.append(RouteLeg(
                mode=edge.mode,
                start=from_node.coord, end=to_node.coord,
                start_name=edge.from_name or from_node.name,
                end_name=edge.to_name or to_node.name,
                duration_min=edge.duration_min,
                distance_m=edge.distance_m,
                cost_won=edge.cost_won,
                detail=edge.detail,
                interval_min=edge.interval_min,
            ))

        merged = self._merge_consecutive_legs(legs)
        self._assign_transit_cost(merged)

        if not label:
            label = self._auto_label(merged)

        return Route(legs=merged, label=label)

    @staticmethod
    def _merge_consecutive_legs(legs: list[RouteLeg]) -> list[RouteLeg]:
        """같은 노선의 연속 구간을 하나로 병합."""
        if not legs:
            return []

        merged = [legs[0]]
        for leg in legs[1:]:
            prev = merged[-1]
            if (prev.mode == leg.mode
                    and prev.detail == leg.detail
                    and prev.mode.is_transit):
                prev.end = leg.end
                prev.end_name = leg.end_name
                prev.duration_min += leg.duration_min
                prev.distance_m += leg.distance_m
            else:
                merged.append(leg)

        # 대중교통 구간에 distance_m 추정
        for leg in merged:
            if leg.mode.is_transit and leg.distance_m == 0:
                leg.distance_m = haversine_m(leg.start, leg.end) * 1.2

        # 역 내 환승 도보 제거
        return remove_instation_walks(merged)

    @staticmethod
    def _assign_transit_cost(legs: list[RouteLeg]) -> None:
        """대중교통 요금을 추정하여 마지막 대중교통 구간에 할당."""
        transit_legs = [l for l in legs if l.mode.is_transit]
        if not transit_legs:
            return
        total_km = sum(l.distance_m for l in transit_legs) / 1000
        fare = TRANSIT_BASE_FARE + max(0, int((total_km - 10) / 5)) * TRANSIT_FARE_PER_5KM
        fare = max(TRANSIT_BASE_FARE, min(fare, TRANSIT_MAX_FARE))
        transit_legs[-1].cost_won = fare

    @staticmethod
    def _auto_label(legs: list[RouteLeg]) -> str:
        """경로 패턴에 따라 자동 라벨 생성."""
        parts: list[str] = []
        for leg in legs:
            if leg.mode == TransportMode.WALK:
                continue
            if leg.mode == TransportMode.TAXI:
                km = leg.distance_m / 1000
                parts.append(f"택시({km:.1f}km)")
            else:
                parts.append(leg.detail)
        return "→".join(parts) if parts else "도보"

    def get_estimate_taxi_edges(self, paths: list[list[GraphEdge]]) -> dict[str, GraphEdge]:
        """모든 경로에서 추정 택시 엣지를 수집."""
        result: dict[str, GraphEdge] = {}
        for path in paths:
            for edge in path:
                if edge.is_estimate and edge.mode == TransportMode.TAXI:
                    key = f"{edge.from_id}|{edge.to_id}"
                    if key not in result:
                        result[key] = edge
        return result

    def stats(self) -> str:
        """그래프 통계."""
        n_nodes = len(self.nodes)
        n_edges = sum(len(v) for v in self.adj.values())
        n_stations = sum(1 for n in self.nodes.values() if n.node_type == "station")
        return f"노드 {n_nodes}개 (역 {n_stations}개), 엣지 {n_edges}개"
