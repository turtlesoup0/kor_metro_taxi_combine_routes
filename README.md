# find_Kor_Routes

대중교통 기반 혼합 경로 탐색 — 필요한 구간에만 택시를 개입시켜 이동 편의성과 비용 효율성을 최대화한다.

## 핵심 아이디어

기존 길찾기 서비스는 대중교통과 택시를 완전히 분리한다. 하지만 실제로는 **환승 과정에서 택시가 더 효율적인 구간**이 존재한다. 이 프로젝트는 대중교통 경로를 기반으로, 택시 개입이 실질적 시간/비용 개선을 가져오는 구간만 자동으로 식별하여 혼합 경로를 제안한다.

## 알고리즘

```
1. 기준 데이터 수집 (병렬)
   - ODsay: 대중교통 경로 (전체 + 지하철 우선)
   - Kakao: 택시 직행, 주변 허브 (지하철/버스/기차역/터미널)

2. 그래프 구성
   - 노드: 출발지, 도착지, ODsay 중간역, 주변 허브역
   - 엣지: 대중교통(연속역), 택시(추정), 도보, 환승
   - 장거리(30km+): 버스터미널 교차 검색 추가

3. 전략별 Dijkstra 탐색
   - S1: 대중교통 전용
   - S2: 택시→대중교통 (진입역별)
   - S3: 대중교통→택시 (탈출역별)
   - S4: 택시→대중교통→택시

4. 불합리 경로 필터링
   - 택시 시간 절약 ≤5분 → 제거
   - 분당 택시비 >1,500원 → 제거
   - 출발지에서 도보 접근 가능한 노선에 택시 태워주기 → 제거

5. first-mile/last-mile 보충
   - 첫역/끝역이 출발지/도착지에서 1km 이상 → ODsay 보충 검색 or 택시 추정
   - ODsay 결과 없음(오지 등) → 인근 거점 경유 fallback

6. 스코어 정렬 + 다양성 보장
   - 시간·비용·환승·도보·대기·피로도 복합 스코어
   - 유형별(transit, taxi-transit, transit-taxi 등) 최선 보장
```

## 지원 교통수단

| 모드 | 설명 | ODsay trafficType |
|------|------|------------------|
| subway | 지하철 | 1 |
| bus | 시내버스 | 2 |
| train | 열차 (KTX/SRT/무궁화/ITX) | 4 |
| express_bus | 고속버스 | 5 |
| intercity_bus | 시외버스 | 6 |
| taxi | 택시 (Kakao Navi) | — |
| walk | 도보 | 3 |

## 설치 및 실행

```bash
cd find_Kor_Routes
pip install -e .

# API 키 설정
cp .env.example .env
# .env에 KAKAO_REST_API_KEY, ODSAY_API_KEY 입력

# 웹 UI
python3 -m uvicorn find_kor_routes.web:app --port 8000
# → http://localhost:8000

# CLI
python -m find_kor_routes.cli 37.4979,127.0276 37.5216,126.9243
```

## 웹 UI 기능

- 출발지/도착지: 장소명 또는 주소 검색 (카카오 키워드 + 주소 fallback)
- 선호도 옵션: 균형 / 저비용 / 적은환승 / 낮은피로
- 허용 수단: 도보·택시 on/off, 최대 도보·택시 거리 조절
- 경로 카드: 스코어 배지 (초록=최적, 노랑=양호, 빨강=보통)
- 지도: Leaflet 기반 경로 시각화
- 비교 테이블: 경로 간 시간/비용/환승/피로도 한눈에 비교

## 프로젝트 구조

```
find_kor_routes/
├── __init__.py
├── __main__.py        # python -m 진입점
├── cli.py             # CLI 인터페이스
├── config.py          # 환경변수·설정·가중치 관리
├── constants.py       # 전역 상수 (요금, 속도, 임계값 등)
├── models.py          # 데이터 모델 (TransportMode, Coord, Route, RouteLeg)
├── utils.py           # 공용 유틸리티 (거리계산, 택시추정, ODsay파싱)
├── api_clients.py     # 카카오/ODsay API 클라이언트
├── graph.py           # 그래프 자료구조 + Dijkstra 탐색
├── router.py          # 혼합 경로 탐색 파이프라인
├── web.py             # FastAPI 웹 서버
└── static/
    └── index.html     # 웹 UI (Leaflet 지도 + 경로 카드)
```

## 스코어링 공식

```
score = 시간×w_time + (비용/1000)×w_cost + 환승×w_transfer
      + 도보시간×w_walk + 예상대기×w_wait + 피로도×w_fatigue
```

| 프리셋 | time | cost | transfer | walk | wait | fatigue |
|--------|------|------|----------|------|------|---------|
| 균형 | 1.0 | 0.5 | 3.0 | 1.5 | 0.8 | 0.3 |
| 저비용 | 0.5 | 2.0 | 2.0 | 1.0 | 0.5 | 0.2 |
| 적은환승 | 0.8 | 0.3 | 8.0 | 1.5 | 1.0 | 0.5 |
| 낮은피로 | 0.6 | 0.3 | 5.0 | 3.0 | 1.5 | 1.5 |

## 피로도 지수

```
피로도 = 환승×4 + 도보시간×1.5 + 예상대기×1.0 + 최대배차간격×0.5
       + 고배차간격 버스 추가 패널티 (30분+: +5, 40분+: +10)
```

## API 호출량 (1회 탐색 기준)

| API | 호출 수 | 비고 |
|-----|--------|------|
| ODsay 대중교통 | 2 + H×2 + F | 기준 2회 + 허브별 + first/last-mile 보충 |
| 카카오 키워드 | 6~8 | 지하철·버스·기차역 × 출발·도착 + 터미널 |
| 카카오 내비 | 1 + T | 직행 택시 + 그래프 추정 택시 정밀화 |

H=허브 수(~10), F=보충 검색(0~4), T=추정 택시(0~5) → 약 **30~40회**

## 필요 API 키

- **KAKAO_REST_API_KEY**: [카카오 개발자](https://developers.kakao.com) → REST API 키
- **ODSAY_API_KEY**: [ODsay Lab](https://lab.odsay.com) → API 키
