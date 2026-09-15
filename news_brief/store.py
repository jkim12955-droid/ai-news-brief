"""기사 적재와 조회를 맡는 저장소 모듈.

sqlite 파일 하나에 기사와 실행 기록을 담는다.
AI 판단 기록은 이 모듈이 맡지 않는다. judge 가 judge_runs, judge_decisions,
judge_groups, judge_group_urls 를 자기 손으로 만들어 쓴다. 예전에는 여기에도
decisions 표를 만들어 두었는데 넣고 빼는 코드가 한 줄도 없었다. 판단을 담는
표가 둘이면 나중에 어느 쪽이 진짜인지 헷갈리므로 이쪽을 지웠다.
같은 날을 몇 번 다시 돌려도 집계 숫자가 흔들리면 안 되기 때문에
기사는 url 을 기본키로 두고 INSERT OR IGNORE 로 멱등하게 넣는다.
한 번 들어간 기사의 kst_date 와 first_inserted_at 은 나중 실행이 덮어쓰지 않는다.
즉 기사는 처음 본 날에 계속 매달려 있다.

시각 문자열은 모두 ISO 형식으로 저장한다.
현재 시각을 읽는 자리는 now 인자로 바꿔 끼울 수 있게 열어 두었다.
테스트에서는 고정된 값을 넣어 쓴다.
"""

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 계약에 적힌 공개 함수와, 집계를 돕는 보조 함수 몇 개를 함께 내보낸다.
__all__ = [
    "open_db",
    "upsert_articles",
    "articles_for_date",
    "keyword_counts",
    "keyword_hit",
    "first_seen_keywords",
    "daily_counts",
    "recent_average",
    "runs_for_date",
]

# 기사 한 건을 이룰 컬럼 순서. INSERT 와 조회에서 같은 순서를 쓴다.
ARTICLE_COLUMNS = (
    "url",
    "kst_date",
    "seendate_utc",
    "domain",
    "title",
    "title_raw",
    "language",
    "first_inserted_at",
)

# 스키마는 열 때마다 없으면 만든다. 이미 있으면 아무 일도 일어나지 않는다.
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS articles (
        url TEXT PRIMARY KEY,
        kst_date TEXT NOT NULL,
        seendate_utc TEXT,
        domain TEXT,
        title TEXT,
        title_raw TEXT,
        language TEXT,
        first_inserted_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_articles_kst_date ON articles (kst_date)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kst_date TEXT NOT NULL,
        started_at TEXT,
        inserted INTEGER,
        duplicated INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_kst_date ON runs (kst_date)",
)


def open_db(db_path):
    """sqlite 연결을 열고 스키마가 없으면 만들어 돌려준다.

    db_path 는 파일 경로나 ':memory:' 를 받는다.
    파일 경로면 상위 폴더가 없을 때 만들어 준다.
    행은 sqlite3.Row 로 돌려주므로 이름으로도, 번호로도 꺼낼 수 있다.
    """
    path_text = str(db_path)
    if path_text != ":memory:" and not path_text.startswith("file:"):
        target = Path(path_text).expanduser()
        if target.parent and str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        path_text = str(target)

    conn = sqlite3.connect(path_text)
    conn.row_factory = sqlite3.Row
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
    return conn


def upsert_articles(conn, date_str, articles, now=None):
    """하루치 기사를 멱등하게 넣고 건수를 돌려준다.

    같은 url 이 이미 있으면 건너뛰고 duplicated 로 센다.
    한 번의 호출 안에서 같은 url 이 여러 번 들어와도 한 건만 넣는다.
    url 이 비어 있는 항목은 셈에 넣지 않고 그냥 버린다. 기본키가 될 수 없어서다.
    돌려주는 값은 {inserted, duplicated, total_for_date} 이고,
    total_for_date 는 그 날짜에 달린 기사의 전체 건수다.
    같은 날짜를 두 번 넣어도 이 값은 변하지 않는다.

    적재를 시도할 때마다 runs 에 기록을 한 줄 남긴다.
    이 기록은 집계에 쓰이지 않고 언제 몇 건이 들어왔는지 되짚어 보는 용도다.
    """
    day = _parse_kst_date(date_str)
    stamp = _resolve_now(now)

    prepared = []
    seen_in_batch = set()
    duplicated = 0
    for item in articles or []:
        row = _article_row(item, day, stamp)
        if row is None:
            continue
        url = row[0]
        if url in seen_in_batch:
            duplicated += 1
            continue
        seen_in_batch.add(url)
        prepared.append(row)

    placeholders = ", ".join("?" for _ in ARTICLE_COLUMNS)
    sql = "INSERT OR IGNORE INTO articles ({}) VALUES ({})".format(
        ", ".join(ARTICLE_COLUMNS), placeholders
    )

    inserted = 0
    cursor = conn.cursor()
    for row in prepared:
        cursor.execute(sql, row)
        if cursor.rowcount == 1:
            inserted += 1
        else:
            # 이미 있던 url 이라 sqlite 가 조용히 넘긴 경우다.
            duplicated += 1

    conn.execute(
        "INSERT INTO runs (kst_date, started_at, inserted, duplicated) VALUES (?, ?, ?, ?)",
        (day, stamp, inserted, duplicated),
    )
    conn.commit()

    total_row = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE kst_date = ?", (day,)
    ).fetchone()
    return {
        "inserted": inserted,
        "duplicated": duplicated,
        "total_for_date": int(total_row[0]),
    }


def articles_for_date(conn, date_str):
    """그 날짜에 달린 기사를 시각 순으로 돌려준다.

    항목은 수집기가 넘겨주던 모양 그대로 url, title, title_raw, domain,
    seendate, language 를 담고, 저장소가 아는 kst_date 와 first_inserted_at 을 더한다.
    컬럼 이름이 seendate_utc 라 헷갈릴 수 있어 seendate 로도 같은 값을 넣어 둔다.
    정렬은 시각 다음에 url 로 한 번 더 하므로 같은 날을 다시 조회해도 순서가 같다.
    """
    day = _parse_kst_date(date_str)
    rows = conn.execute(
        """
        SELECT url, kst_date, seendate_utc, domain, title, title_raw, language, first_inserted_at
        FROM articles
        WHERE kst_date = ?
        ORDER BY seendate_utc ASC, url ASC
        """,
        (day,),
    ).fetchall()
    return [_row_to_article(row) for row in rows]


def keyword_hit(text, keyword):
    """제목에 키워드가 낱말로 들어 있는지 본다. 대소문자는 가리지 않는다.

    부분 문자열로만 견주면 숫자가 부풀고 겹친다. 실제로 이런 일이 있었다.
    '구글 딥마인드, 새 algorithm 공개' 한 건이 키워드 LG 로 잡혔다. algorithm
    안에 lg 가 있어서다. '메타버스 열풍은 끝났다' 도 메타로 잡혔다.
    그래서 두 가지 잣대를 쓴다.

    로마자와 숫자로만 된 키워드는 앞뒤가 로마자나 숫자면 걸리지 않게 한다.
    AI 가 AIDS 나 Hair 안에서 잡히는 일을 막는 자리다.

    한글이 섞인 키워드는 앞이 낱말의 첫머리일 때만, 곧 글 머리이거나 앞 글자가
    공백이나 구두점일 때만 센다. 뒤쪽은 막지 않았다. 뒤도 막아 보니 '삼성전자
    온디바이스 AI 탑재' 가 삼성에서 빠지고 '개인정보위 조사 착수' 가 개인정보에서
    빠졌다. 한국어는 조사와 준말이 낱말에 그대로 붙기 때문에, 뒤를 막으면
    잘못 세는 쪽보다 못 세는 쪽이 더 커진다.

    뒤를 막지 않은 대신 메타버스처럼 앞머리만 같고 뜻이 다른 말은
    DIFFERENT_WORDS 에 적어 둔다.
    """
    haystack = (text or "").casefold()
    needle = (keyword or "").strip().casefold()
    if not haystack or not needle:
        return False
    for 걸린자리 in _keyword_pattern(needle).finditer(haystack):
        if not _다른말인가(haystack, 걸린자리.start(), needle):
            return True
    return False


def keyword_counts(conn, start_date, end_date, keywords):
    """기간 안에서 제목에 키워드가 들어간 기사 수를 센다.

    양쪽 끝 날짜를 모두 포함한다. 대소문자는 구분하지 않는다.
    낱말 경계를 보는 잣대는 keyword_hit 에 적어 두었다.
    한 기사 제목에 같은 키워드가 여러 번 나와도 한 건으로 센다.
    돌려주는 dict 의 열쇠는 받은 키워드 표기를 그대로 쓰고 순서도 유지한다.
    비교는 sqlite 의 LIKE 대신 파이썬에서 한다.
    sqlite 의 대소문자 변환이 아스키 밖을 다루지 않고,
    키워드에 % 나 _ 가 섞이면 LIKE 가 엉뚱하게 걸리기 때문이다.
    """
    start = _parse_kst_date(start_date)
    end = _parse_kst_date(end_date)
    if start > end:
        start, end = end, start

    wanted = _unique_keywords(keywords)
    counts = {keyword: 0 for keyword in wanted}
    needles = [keyword for keyword in wanted if keyword.strip()]
    if not needles:
        return counts

    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(title, ''), title_raw) AS haystack
        FROM articles
        WHERE kst_date >= ? AND kst_date <= ?
        """,
        (start, end),
    )
    for row in rows:
        haystack = row[0] or ""
        if not haystack:
            continue
        for keyword in needles:
            if keyword_hit(haystack, keyword):
                counts[keyword] += 1
    return counts


def first_seen_keywords(conn, date_str, lookback_days, keywords):
    """기준 날짜에는 보이고 앞선 기간에는 없던 키워드를 돌려준다.

    비교 구간은 기준 날짜의 하루 전부터 lookback_days 일 전까지다.
    기준 날짜 자신은 비교 구간에 넣지 않는다.
    돌려주는 순서는 받은 키워드 순서를 따른다.
    """
    day = _parse_kst_date(date_str)
    lookback = int(lookback_days)
    if lookback < 1:
        raise ValueError("lookback_days 는 1 이상이어야 한다. 받은 값: {}".format(lookback_days))

    anchor = date.fromisoformat(day)
    window_end = (anchor - timedelta(days=1)).isoformat()
    window_start = (anchor - timedelta(days=lookback)).isoformat()

    today = keyword_counts(conn, day, day, keywords)
    before = keyword_counts(conn, window_start, window_end, keywords)
    return [
        keyword
        for keyword, count in today.items()
        if count > 0 and before.get(keyword, 0) == 0
    ]


def daily_counts(conn, start_date, end_date):
    """기간 안 날짜별 기사 수를 돌려준다.

    기사가 한 건도 없는 날도 0 으로 채워 넣는다. 어느 날이 비었는지 보여 주려는 것이다.
    평균은 이 값을 그대로 나누지 않고 recent_average 가 빈 날을 빼고 낸다.
    """
    start = _parse_kst_date(start_date)
    end = _parse_kst_date(end_date)
    if start > end:
        start, end = end, start

    counts = {}
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cursor <= last:
        counts[cursor.isoformat()] = 0
        cursor += timedelta(days=1)

    rows = conn.execute(
        """
        SELECT kst_date, COUNT(*) AS total
        FROM articles
        WHERE kst_date >= ? AND kst_date <= ?
        GROUP BY kst_date
        """,
        (start, end),
    )
    for row in rows:
        counts[row[0]] = int(row[1])
    return counts


def recent_average(conn, date_str, days, since=None, min_days=3):
    """대상 날짜 앞 며칠의 하루 평균 기사 수를 낸다.

    기사가 한 건도 없는 날은 빼고 낸다. 하루 수백 건이 모이는 지금 0건인 날은
    조용한 날이 아니라 수집이 돌지 않은 날이다. 9월 14일 브리핑이 그런 날을 0으로
    넣어 평균을 59.3건으로 낮게 잡았고, 어제 339건을 평균의 5.7배라고 적었다.
    since 보다 앞선 날도 뺀다. 출처를 바꾼 날 앞의 기록은 모으는 방식이 달라
    건수를 견줄 수 없어서다. 남은 날이 min_days 보다 적으면 평균을 내지 않는다.
    """
    counts = daily_counts(
        conn,
        (date.fromisoformat(_parse_kst_date(date_str)) - timedelta(days=int(days))).isoformat(),
        (date.fromisoformat(_parse_kst_date(date_str)) - timedelta(days=1)).isoformat(),
    )
    floor = _parse_kst_date(since) if since else ""
    used = [count for day, count in counts.items() if count > 0 and day >= floor]
    if not used or len(used) < int(min_days or 1):
        return {"average": None, "days_used": len(used), "window": int(days)}
    return {"average": sum(used) / len(used), "days_used": len(used), "window": int(days)}


def runs_for_date(conn, date_str):
    """그 날짜에 남은 적재 기록을 오래된 것부터 돌려준다."""
    day = _parse_kst_date(date_str)
    rows = conn.execute(
        """
        SELECT run_id, kst_date, started_at, inserted, duplicated
        FROM runs
        WHERE kst_date = ?
        ORDER BY run_id ASC
        """,
        (day,),
    ).fetchall()
    return [
        {
            "run_id": int(row[0]),
            "kst_date": row[1],
            "started_at": row[2],
            "inserted": int(row[3] or 0),
            "duplicated": int(row[4] or 0),
        }
        for row in rows
    ]


def _article_row(item, day, stamp):
    """수집기가 준 기사 하나를 INSERT 에 넣을 튜플로 바꾼다.

    url 이 없으면 None 을 돌려준다. 기본키가 비면 넣을 수 없어서다.
    """
    if not isinstance(item, dict):
        return None
    url = _clean(item.get("url"))
    if not url:
        return None
    seendate = _clean(item.get("seendate")) or _clean(item.get("seendate_utc"))
    title_raw = _clean(item.get("title_raw")) or _clean(item.get("title"))
    return (
        url,
        day,
        seendate,
        _clean(item.get("domain")),
        _clean(item.get("title")),
        title_raw,
        _clean(item.get("language")),
        stamp,
    )


def _row_to_article(row):
    """DB 한 줄을 기사 dict 로 바꾼다.

    행을 이름이 아니라 번호로 읽는다.
    다른 모듈이 row_factory 를 안 건 연결을 넘겨도 똑같이 돌아가게 하려는 것이다.
    번호는 articles_for_date 의 SELECT 순서를 따른다.
    """
    return {
        "url": row[0],
        "title": row[4] or "",
        "title_raw": row[5] or "",
        "domain": row[3] or "",
        "seendate": row[2] or "",
        "seendate_utc": row[2] or "",
        "language": row[6] or "",
        "kst_date": row[1],
        "first_inserted_at": row[7] or "",
    }


def _clean(value):
    """None 과 앞뒤 공백을 걷어내고 문자열로 맞춘다."""
    if value is None:
        return ""
    return str(value).strip()


# 앞머리만 같고 뜻이 다른 말. 이 말로 시작하는 자리는 키워드로 세지 않는다.
# 메타버스는 회사 메타 소식이 아니라 딴 이야기라서 적어 두었다.
# 여기 적은 말 자체가 키워드로 들어오면 그때는 그대로 센다.
DIFFERENT_WORDS = ("메타버스",)

_패턴_캐시 = {}


def _keyword_pattern(소문자키워드):
    """키워드 하나를 찾는 정규식을 만든다. 같은 것을 다시 컴파일하지 않는다."""
    만든것 = _패턴_캐시.get(소문자키워드)
    if 만든것 is None:
        if 소문자키워드.isascii():
            # 로마자와 숫자만이면 앞뒤로 낱말 경계를 본다.
            패턴 = r"(?<![0-9a-z])" + re.escape(소문자키워드) + r"(?![0-9a-z])"
        else:
            # 한글이 섞였으면 낱말의 첫머리인지만 본다.
            패턴 = r"(?<![0-9a-z가-힣])" + re.escape(소문자키워드)
        만든것 = re.compile(패턴)
        _패턴_캐시[소문자키워드] = 만든것
    return 만든것


def _다른말인가(소문자제목, 자리, 소문자키워드):
    """걸린 자리가 앞머리만 같은 딴 낱말인지 본다."""
    for 낱말 in DIFFERENT_WORDS:
        다른말 = 낱말.casefold()
        if 다른말 == 소문자키워드 or not 다른말.startswith(소문자키워드):
            continue
        if 소문자제목.startswith(다른말, 자리):
            return True
    return False


def _unique_keywords(keywords):
    """키워드 목록에서 중복을 걷어내되 받은 순서는 지킨다."""
    ordered = []
    seen = set()
    for keyword in keywords or []:
        if keyword is None:
            continue
        text = str(keyword)
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        ordered.append(text)
    return ordered


def _parse_kst_date(value):
    """한국 날짜 문자열을 확인하고 'YYYY-MM-DD' 로 맞춰 돌려준다.

    줄표가 들어간 열 글자만 받는다.
    파이썬 3.11 부터는 fromisoformat 이 '20260911' 같은 붙임표 없는 꼴도 받아 주는데,
    GDELT 시각 문자열이 딱 그 모양이라 날짜 자리에 잘못 흘러들면 조용히 통과한다.
    그런 실수는 여기서 막는 편이 낫다.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _clean(value)
    if not text:
        raise ValueError("날짜가 비어 있다. 'YYYY-MM-DD' 형식이 필요하다")
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError("날짜 형식이 맞지 않는다. 'YYYY-MM-DD' 가 필요한데 받은 값: {!r}".format(value))
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise ValueError(
            "날짜 형식이 맞지 않는다. 'YYYY-MM-DD' 가 필요한데 받은 값: {!r}".format(value)
        ) from error


def _resolve_now(now):
    """기록할 시각 문자열을 정한다.

    now 에는 시각을 돌려주는 함수, datetime, 문자열 중 아무거나 넣을 수 있다.
    비워 두면 실행 시점의 UTC 시각을 쓴다.
    테스트에서는 고정된 값을 넣어 결과를 붙박이로 만든다.
    """
    if now is None:
        return _utc_now_iso()
    if callable(now):
        return _resolve_now(now())
    if isinstance(now, datetime):
        return now.isoformat(timespec="seconds")
    if isinstance(now, date):
        return now.isoformat()
    return _clean(now)


def _utc_now_iso():
    """지금 시각을 초 단위 ISO 문자열로 돌려준다."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
