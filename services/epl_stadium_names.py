"""Korean -> English EPL stadium name translation, mirroring
epl_team_names.py's pattern exactly (same reasoning: Naver's API only
returns the Korean name, so the server translates before the client ever
sees it — team names, stadiums, anything else user-facing should all go
through this same server-side step rather than a client-side table, since
the client never gets the original Korean text to translate from once a
team-name-style client-side map is used for something else).

Not every possible Naver spelling is guaranteed to be covered here (unlike
team names, this hasn't been cross-checked against live API responses for
every club) — en_stadium() falls back to the original Korean string when a
name isn't in the map, same as en_team(), so an unmapped stadium just stays
Korean rather than breaking anything.
"""

_STADIUM_NAME_EN: dict[str, str] = {
    "에미레이츠 스타디움": "Emirates Stadium",
    "스탬퍼드 브리지": "Stamford Bridge",
    "안필드": "Anfield",
    "에티하드 스타디움": "Etihad Stadium",
    "올드 트래포드": "Old Trafford",
    "올드 트래퍼드": "Old Trafford",
    "토트넘 홋스퍼 스타디움": "Tottenham Hotspur Stadium",
    "런던 스타디움": "London Stadium",
    "세인트 제임스 파크": "St James' Park",
    "아멕스 스타디움": "Amex Stadium",
    "아메리칸 익스프레스 스타디움": "Amex Stadium",
    "빌라 파크": "Villa Park",
    "크레이븐 코티지": "Craven Cottage",
    "몰리뉴 스타디움": "Molineux Stadium",
    "셀허스트 파크": "Selhurst Park",
    "지테크 커뮤니티 스타디움": "Gtech Community Stadium",
    "시티 그라운드": "City Ground",
    "더 시티 그라운드": "City Ground",
    "코벤트리 빌딩 소사이어티 아레나": "Coventry Building Society Arena",
    "바이탈리티 스타디움": "Vitality Stadium",
    "킹 파워 스타디움": "King Power Stadium",
    "포트먼 로드": "Portman Road",
    "구디슨 파크": "Goodison Park",
    "힐 디킨슨 스타디움": "Hill Dickinson Stadium",
    "세인트 메리스 스타디움": "St Mary's Stadium",
    "터프 무어": "Turf Moor",
    "케닐워스 로드": "Kenilworth Road",
    "브라마 레인": "Bramall Lane",
    "스타디움 오브 라이트": "Stadium of Light",
    "엘런드 로드": "Elland Road",
    "리버사이드 스타디움": "Riverside Stadium",
    "코번트리 빌딩 소사이어티 아레나": "Coventry Building Society Arena",
    "MKM 스타디움": "MKM Stadium",
    "비카리지 로드": "Vicarage Road",
    "더 호손스": "The Hawthorns",
    "스완지닷컴 스타디움": "Swansea.com Stadium",
    "벳365 스타디움": "bet365 Stadium",
    "이우드 파크": "Ewood Park",
    "존 스미스 스타디움": "John Smith's Stadium",
    "카디프 시티 스타디움": "Cardiff City Stadium",
}


def en_stadium(name: str) -> str:
    return _STADIUM_NAME_EN.get(name, name)
