"""하루치 수집과 판단을 스스로 점검하는 모듈.

워크북이 정한 멈춤 조건은 셋이다. 기사가 한 건도 없을 때, 250건 상한에 걸려
뒤가 잘린 구간이 남았을 때, 기사 시각을 한국 시간으로 바꿨더니 대상 날짜를
벗어났을 때다. 여기에 링크 중복과 필수 필드 결측을 더해 다섯 가지를 fail 로 본다.
적재와 판단을 마친 뒤에 보는 판단 대조도 어긋나면 fail 이다. 판단이 다룬 건수와
DB 에 쌓인 건수가 갈리면 브리핑 본문과 건수가 서로 다른 하루를 말하게 된다.
건수가 최근 이레 평균에서 크게 벗어나거나 판단 단계가 너무 많이 걸러낸 경우는
사람이 한 번 들여다볼 일이지 멈출 일은 아니라서 warn 으로 남긴다.

날짜 범위는 셋째 멈춤 조건이지만 비율로 본다. GDELT 는 seendate 를 15분 단위로
끊어 주기 때문에 하루 경계에 놓인 기사 한 건이 반올림 탓에 옆날로 보일 수 있다.
그 한 건 때문에 하루 산출물을 통째로 버리면 손해가 더 크다. 그래서 벗어난 기사가
수집 건수의 한 할을 넘으면 멈추고, 그 아래면 몇 건이 벗어났는지 주의로 남긴다.
경계 계산이 크게 틀어진 경우는 비율로도 그대로 걸린다.

시각을 한국 날짜로 바꾸는 계산은 config 를 빌리지 않고 이 모듈이 직접 한다.
링크를 견주는 열쇠도 collect.normalize_url 을 빌리지 않고 여기서 따로 만든다.
수집이 쓴 자와 검증이 쓴 자가 같으면 그 자가 틀어졌을 때 아무도 알아채지 못한다.
점검은 따로 재야 뜻이 있다.

점검은 두 자리에서 부른다. 적재 전에는 stage="collected" 로 멈춤 조건 다섯 가지만
보고, 적재와 판단을 마친 뒤에 stage="all" 로 나머지까지 본다. 날짜가 어긋난 기사가
DB 에 박히기 전에 한 번 걸러 내려는 것이다.

점검 결과는 목록과 표 문자열로 함께 돌려준다. 표는 로그와 증거로 남기는 쪽이다.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import unicodedata
import urllib.parse

logger = logging.getLogger(__name__)

__all__ = [
    "run_checks",
    "format_checks",
    "kst_date_from_utc",
    "STAGE_ALL",
    "STAGE_COLLECTED",
    "OK",
    "WARN",
    "FAIL",
]

# 점검을 부르는 자리. 적재 전에는 멈춤 조건만 보고, 적재와 판단 뒤에 전부 본다.
STAGE_COLLECTED = "collected"
STAGE_ALL = "all"

# 점검 한 줄이 가질 수 있는 상태. 계약에 적힌 세 가지뿐이다.
OK = "ok"
WARN = "warn"
FAIL = "fail"

# 한국 시간대. tz 자료가 없는 환경도 있어 고정 오프셋을 예비로 둔다.
try:  # pragma: no cover - 환경에 따라 갈린다
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover - tz 자료가 없을 때만 온다
    KST = dt.timezone(dt.timedelta(hours=9))

UTC = dt.timezone.utc

# 기사 한 건에 반드시 있어야 할 자리.
REQUIRED_FIELDS = ("url", "title", "domain", "seendate")

# 최근 이레 평균과 견줄 때 쓰는 기본 배수.
DEFAULT_LOW_RATIO = 0.5
DEFAULT_HIGH_RATIO = 2.0

# 평균이 이보다 작으면 견주지 않는다. 하루 한두 건일 때는 배수가 쉽게 흔들려서다.
DEFAULT_MIN_AVERAGE = 3.0

# 판단 단계가 걸러낸 비율이 이 값을 넘으면 주의로 본다.
DEFAULT_DROP_RATIO = 0.7

# 평균을 낼 때 되돌아볼 날 수.
DEFAULT_LOOKBACK_DAYS = 7

# 한국 날짜를 벗어난 기사가 수집 건수의 이 비율을 넘으면 멈춘다.
# 그 아래면 주의만 남긴다. 15분 단위로 끊긴 시각이 경계에서 옆날로 보이는 것까지
# 멈춤으로 보면 하루를 통째로 잃는다.
DEFAULT_MAX_STRAY_RATIO = 0.1

# 자세한 내용에 붙일 보기의 개수. 너무 길면 표가 읽히지 않는다.
SAMPLE_LIMIT = 3

# 링크 대조 점검 기본값. 기본은 꺼 둔다.
# 픽스처 링크는 손으로 지어낸 기사 번호라 실제로는 다 어긋나고, 켜 두면 점검이
# 바깥으로 요청을 내보낸다. 무엇을 언제 부를지는 사람이 정할 일이다.
DEFAULT_LINK_CHECK_ENABLED = False
DEFAULT_LINK_CHECK_SAMPLE = 3
DEFAULT_LINK_CHECK_TIMEOUT = 10.0
DEFAULT_LINK_CHECK_MIN_OVERLAP = 0.2
LINK_CHECK_MAX_BYTES = 400000
LINK_CHECK_AGENT = "news-brief-link-check/1.0"

_DIGITS = re.compile(r"\D")
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")

# 링크 열쇠를 만들 때 떼어 낼 추적용 파라미터. 기사를 가리키는 번호는 건드리지 않는다.
_TRACKING_PREFIXES = ("utm_", "_ga")
_TRACKING_NAMES = frozenset(
    {"fbclid", "gclid", "igshid", "spm", "ref", "refer", "cmpid", "ncid"}
)


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------

def _dig(cfg, path):
    """점으로 이어진 경로를 따라 설정값을 꺼낸다. 없으면 None 이다."""
    node = cfg
    for name in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(name)
        if node is None:
            return None
    return node


def _number_setting(cfg, paths, default):
    """설정에서 숫자를 찾는다. 숫자로 읽히지 않으면 기본값을 쓴다."""
    for path in paths:
        value = _dig(cfg or {}, path)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning("%s 설정값이 숫자가 아니라 기본값을 쓴다. 받은 값 %r", path, value)
    return float(default)


# ---------------------------------------------------------------------------
# 시각 다루기
# ---------------------------------------------------------------------------

def kst_date_from_utc(value):
    """GDELT 가 준 UTC 시각을 한국 날짜 'YYYY-MM-DD' 로 바꾼다.

    '20260911T103000Z', '20260911103000', '2026-09-11 10:30:00' 처럼
    숫자 열넷을 품은 모양이면 다 받는다. 알아볼 수 없으면 None 을 돌려준다.
    """
    digits = _DIGITS.sub("", str(value or ""))
    if len(digits) != 14:
        return None
    try:
        stamp = dt.datetime.strptime(digits, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC).astimezone(KST).date().isoformat()


def _parse_day(date_str):
    """대상 날짜를 date 로 바꾼다. 모양이 어긋나면 그대로 알려 준다."""
    text = str(date_str or "").strip()
    try:
        return dt.date.fromisoformat(text)
    except ValueError as err:
        raise ValueError("대상 날짜는 'YYYY-MM-DD' 여야 한다. 받은 값 %r" % date_str) from err


# ---------------------------------------------------------------------------
# 점검 한 줄 만들기
# ---------------------------------------------------------------------------

def _check(name, status, detail):
    """점검 한 줄을 계약에 맞는 모양으로 만든다."""
    return {"name": name, "status": status, "detail": detail}


def _samples(items, limit=SAMPLE_LIMIT):
    """보기를 몇 개만 추려 한 줄로 잇는다. 남은 개수는 뒤에 적는다."""
    picked = [str(item) for item in items[:limit]]
    joined = ", ".join(picked)
    rest = len(items) - len(picked)
    if rest > 0:
        joined += " 외 %d건" % rest
    return joined


# ---------------------------------------------------------------------------
# 점검 항목
# ---------------------------------------------------------------------------

def _check_count(articles, meta=None):
    """기사가 한 건도 없으면 멈춘다. 수집이 비었다는 것은 대개 검색어나 구간 문제다.

    링크가 없어 수집기가 버린 항목이 있으면 그 건수도 함께 적는다. 그 항목은
    기사 목록에 들어오지 않기 때문에, 적어 두지 않으면 GDELT 응답 건수와 우리가
    센 건수가 갈린 까닭을 나중에 되짚을 수 없다.
    """
    total = len(articles)
    버린것 = _dropped_note(meta)
    if total == 0:
        return _check(
            "수집 건수",
            FAIL,
            "기사가 한 건도 없다. 검색어와 시간 구간을 먼저 살펴야 한다" + 버린것,
        )
    return _check("수집 건수", OK, "기사 %d건을 모았다" % total + 버린것)


def _dropped_note(meta):
    """링크 없는 항목을 몇 건 버렸는지 한 마디로 적는다. 없으면 아무 말도 하지 않는다."""
    if not isinstance(meta, dict):
        return ""
    try:
        버린수 = int(meta.get("dropped_no_url") or 0)
    except (TypeError, ValueError):
        return ""
    if 버린수 <= 0:
        return ""
    return ". 링크가 없어 수집기가 버린 항목이 %d건 더 있다" % 버린수


def _blocked(window):
    """구간 기록이 'GDELT 가 막아 받지 못한 자리' 인지 본다."""
    return isinstance(window, dict) and bool(window.get("blocked"))


def _check_truncated(truncated, known):
    """받지 못한 구간이 남아 있으면 멈춘다. 남은 만큼 기사를 놓친 것이다.

    못 받은 까닭이 두 가지라 갈라 적는다. 하나는 250건 상한에 걸린 채 더 쪼갤
    깊이가 없어 뒤가 잘린 구간이고, 다른 하나는 GDELT 가 429 로 막아 응답 자체를
    받지 못한 구간이다. 둘 다 기사를 놓친 것이라 멈춤은 같지만, 할 일이 다르다.
    앞엣것은 검색어를 좁히거나 하루를 나눠 받아야 하고, 뒤엣것은 한동안 쉬었다가
    같은 날짜를 다시 돌리면 된다. 한 줄에 뭉쳐 적으면 읽는 사람이 상한 문제로
    잘못 알아듣는다.
    """
    if not known:
        return _check(
            "상한 구간",
            WARN,
            "수집 기록을 받지 못해 상한에 걸렸는지 확인하지 못했다",
        )
    막힌것 = [w for w in truncated if _blocked(w)]
    상한것 = [w for w in truncated if not _blocked(w)]
    조각 = []
    if 상한것:
        조각.append(
            "250건 상한에 걸린 구간이 %d개 남았다. %s"
            % (len(상한것), _samples(["%s~%s" % _window_pair(w) for w in 상한것]))
        )
    if 막힌것:
        조각.append(
            "GDELT 가 막아 받지 못한 구간이 %d개다. 한동안 쉬었다가 같은 날짜를 다시 돌리면 된다. %s"
            % (len(막힌것), _samples(["%s~%s" % _window_pair(w) for w in 막힌것]))
        )
    if 조각:
        return _check("상한 구간", FAIL, " ".join(조각))
    return _check("상한 구간", OK, "상한에 걸린 채 남은 구간도, 막혀서 못 받은 구간도 없다")


def _window_pair(window):
    """구간 기록이 어떤 모양으로 와도 시작과 끝 두 값으로 펴 준다."""
    if isinstance(window, dict):
        return (
            str(window.get("start") or window.get("start_utc") or "?"),
            str(window.get("end") or window.get("end_utc") or "?"),
        )
    if isinstance(window, (list, tuple)) and len(window) >= 2:
        return (str(window[0]), str(window[1]))
    return (str(window), "?")


def _check_dates(articles, date_str, cfg=None, meta=None):
    """기사 시각을 한국 시간으로 바꿔 대상 날짜 안에 있는지 본다.

    UTC 로 자정 언저리에 걸친 기사가 옆날로 새는 일이 잦다.
    벗어난 기사가 수집 건수의 qa.max_stray_ratio 를 넘으면 하루 경계 계산이
    어긋난 것으로 보고 멈춘다. 그 아래면 몇 건이 벗어났는지만 주의로 남긴다.
    GDELT 가 시각을 15분 단위로 끊어 주기 때문에 경계에 놓인 한두 건은
    반올림만으로도 옆날로 보인다. 그 한 건에 하루를 걸지 않는다.

    보는 자리가 둘이다. 수집기가 구간 밖 기사를 스스로 버리고 그 건수를
    stray_dropped 로 들고 나오면 그 숫자를 비율로 본다. 임계값 아래면 버렸다는
    사실만 주의로 남기고, 넘으면 경계 계산이 어긋난 것으로 보고 멈춘다.
    수집기를 거치지 않고 DB 에서 읽어 온 하루(qa 명령이 그렇다)는 들고 나올
    숫자가 없으므로 기사 목록을 직접 훑어 옆날에 매달린 기사를 찾는다.
    앞엣것이 지금 경로이고 뒤엣것은 예전에 잘못 들어간 기사를 잡는 그물이다.

    시각을 아예 알아볼 수 없는 경우는 비율을 따지지 않고 바로 멈춘다.
    응답 모양이 달라졌다는 뜻이라 하루만의 문제가 아니다.
    """
    target = _parse_day(date_str).isoformat()
    limit = _number_setting(
        cfg,
        ("qa.max_stray_ratio", "qa.stray_ratio", "max_stray_ratio"),
        DEFAULT_MAX_STRAY_RATIO,
    )
    버린수, 버린것들 = _stray_dropped(meta)
    strayed = []
    unreadable = []
    for item in articles:
        raw = item.get("seendate") or item.get("seendate_utc") or ""
        if not str(raw).strip():
            # 빈 시각은 필수 필드 점검이 따로 잡는다. 여기서 두 번 세지 않는다.
            continue
        day = kst_date_from_utc(raw)
        if day is None:
            unreadable.append("%s(%s)" % (_short_url(item.get("url")), raw))
        elif day != target:
            strayed.append("%s(%s)" % (_short_url(item.get("url")), day))

    if unreadable:
        return _check(
            "날짜 범위",
            FAIL,
            "시각을 알아볼 수 없는 기사가 %d건이다. %s"
            % (len(unreadable), _samples(unreadable)),
        )
    if strayed:
        전체 = max(1, len(articles))
        비율 = len(strayed) / 전체
        자세히 = "한국 시간으로 %s 밖에 놓인 기사가 %d건이다. 수집 %d건의 %s, 임계값 %s. %s" % (
            target,
            len(strayed),
            len(articles),
            _percent(비율),
            _percent(limit),
            _samples(strayed),
        )
        if 비율 > limit:
            return _check("날짜 범위", FAIL, "하루 경계가 어긋났다. " + 자세히)
        return _check(
            "날짜 범위",
            WARN,
            "경계에 걸친 기사가 있지만 임계값 아래라 멈추지 않는다. "
            + 자세히
            + ". 수집 단계에서 버려야 할 기사다",
        )
    if 버린수:
        전체 = max(1, len(articles) + 버린수)
        비율 = 버린수 / 전체
        자세히 = "받아 온 %d건 가운데 %d건이 %s 구간 밖이어서 %s. 받은 것의 %s, 임계값 %s. %s" % (
            전체,
            버린수,
            target,
            "넣기 전에 버렸다",
            _percent(비율),
            _percent(limit),
            _samples(버린것들) if 버린것들 else "어느 기사였는지는 수집 로그에 있다",
        )
        if 비율 > limit:
            return _check("날짜 범위", FAIL, "하루 경계가 어긋났다. " + 자세히)
        return _check(
            "날짜 범위",
            WARN,
            "경계에 걸친 기사를 수집 단계에서 걸러 냈다. "
            + 자세히
            + ". DB 에 든 기사는 모두 대상 날짜 안에 있다",
        )
    return _check("날짜 범위", OK, "모든 기사가 한국 시간 %s 안에 있다" % target)


def _stray_dropped(meta):
    """수집기가 구간 밖이라 버린 건수와 보기를 꺼낸다. 없으면 (0, []) 이다."""
    if not isinstance(meta, dict):
        return 0, []
    값 = meta.get("stray_dropped")
    try:
        버린수 = int(값 or 0)
    except (TypeError, ValueError):
        버린수 = 0
    보기 = meta.get("stray_samples") or []
    if not isinstance(보기, (list, tuple)):
        보기 = []
    return max(0, 버린수), [str(항목) for 항목 in 보기]


def _short_url(url):
    """표에 들어갈 만큼 링크를 줄인다. 어느 기사인지 알아볼 정도만 남긴다."""
    text = str(url or "").strip()
    if not text:
        return "링크없음"
    text = re.sub(r"^https?://", "", text)
    if len(text) <= 48:
        return text
    return text[:45] + "..."


def _check_fields(articles):
    """url, title, domain, seendate 가운데 빈 자리가 있으면 멈춘다."""
    missing = {}
    for item in articles:
        for field in REQUIRED_FIELDS:
            value = item.get(field)
            if value is None or not str(value).strip():
                missing.setdefault(field, []).append(_short_url(item.get("url")))

    if missing:
        조각 = ["%s %d건" % (field, len(urls)) for field, urls in missing.items()]
        첫자리 = next(iter(missing))
        return _check(
            "필수 필드",
            FAIL,
            "비어 있는 자리가 있다. %s. 보기 %s"
            % (", ".join(조각), _samples(missing[첫자리])),
        )
    return _check("필수 필드", OK, "필수 네 자리가 모두 채워져 있다")


def _dup_key(url):
    """링크를 견줄 열쇠를 만든다. collect 의 것을 빌리지 않고 여기서 따로 만든다.

    지우는 것은 http 와 https 의 차이, www., 끝의 빗금, 조각(#), 그리고 추적용
    파라미터뿐이다. 기사를 가리키는 번호가 든 파라미터는 그대로 둔다. 점검이
    수집보다 더 세게 합쳐 버리면, 서로 다른 기사를 같은 기사라고 멈추게 된다.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text.split("?", 1)[0]:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parsed.port:
        host = "%s:%d" % (host, parsed.port)
    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    남긴것 = []
    for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        낮춘이름 = name.lower()
        if 낮춘이름.startswith(_TRACKING_PREFIXES) or 낮춘이름 in _TRACKING_NAMES:
            continue
        남긴것.append((name, value))
    남긴것.sort()
    질의 = urllib.parse.urlencode(남긴것)
    return "%s%s%s" % (host, path, ("?" + 질의) if 질의 else "")


def _check_duplicates(articles, meta=None):
    """같은 기사를 가리킨 링크가 두 번 들어왔으면 멈춘다. 집계가 그만큼 부풀기 때문이다.

    원본 url 을 그대로 견주면 수집 단계가 이미 지운 뒤라 무엇도 걸리지 않는다.
    그래서 이 점검은 자기 열쇠로 다시 견준다. http 와 https 만 다른 링크나
    추적 파라미터만 붙은 링크가 두 줄로 남는 경우가 여기서 걸린다.
    수집이 몇 건을 합쳤는지 알려 주면 그 숫자도 함께 적는다.
    """
    seen = {}
    repeated = []
    for item in articles:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        key = _dup_key(url) or url
        먼저온것 = seen.get(key)
        if 먼저온것 is not None:
            repeated.append("%s = %s" % (_short_url(먼저온것), _short_url(url)))
        else:
            seen[key] = url

    합친이야기 = _merge_note(meta)
    if repeated:
        return _check(
            "링크 중복",
            FAIL,
            "같은 기사를 가리킨 링크가 %d번 더 들어왔다. %s%s"
            % (len(repeated), _samples(repeated), 합친이야기),
        )
    return _check("링크 중복", OK, "겹치는 링크가 없다%s" % 합친이야기)


def _merge_note(meta):
    """수집이 합친 건수를 한 마디로 적는다. 수집 기록에 없으면 아무 말도 하지 않는다."""
    if not isinstance(meta, dict):
        return ""
    합쳐진것 = meta.get("duplicated")
    if 합쳐진것 is None:
        return ""
    try:
        합쳐진것 = int(합쳐진것)
    except (TypeError, ValueError):
        return ""
    if 합쳐진것 <= 0:
        return ". 수집 원본에서 합친 링크도 없다"
    글자다른것 = meta.get("merged_distinct_urls")
    꼬리 = ""
    try:
        if 글자다른것 is not None and int(글자다른것) > 0:
            꼬리 = ", 그 가운데 링크 글자까지 달랐던 것 %d건" % int(글자다른것)
    except (TypeError, ValueError):
        꼬리 = ""
    return ". 수집 원본에서 %d건이 합쳐졌다%s" % (합쳐진것, 꼬리)


def _stored_count(conn, date_str):
    """그 날짜로 DB 에 쌓인 기사 수를 센다. 읽지 못하면 (None, 사유) 를 돌려준다."""
    if conn is None:
        return None, "DB 를 열지 않았다"
    try:
        from . import store

        return len(store.articles_for_date(conn, date_str)), ""
    except Exception as err:
        return None, str(err)


def _check_stored(stored, sorry, articles):
    """수집한 건수가 DB 에 그대로 들어갔는지 맞춰 본다.

    어느 쪽으로 어긋나도 주의를 남긴다. DB 가 더 적으면 적재가 덜 된 것이고,
    DB 가 더 많으면 브리핑과 이 표가 서로 다른 '어제 기사 수' 를 말하게 된다.
    브리핑은 DB 에 쌓인 것을 세기 때문이다. 다시 수집했더니 GDELT 색인에서
    내려간 기사가 있을 때 이렇게 갈린다. 어느 쪽이든 두 숫자를 함께 적어 둔다.
    """
    if stored is None:
        if sorry == "DB 를 열지 않았다":
            return None
        return _check("적재 대조", WARN, "DB 를 읽지 못해 대조하지 못했다. %s" % sorry)

    모은것 = len(articles)
    if stored < 모은것:
        return _check(
            "적재 대조",
            WARN,
            "모은 기사는 %d건인데 DB 에는 %d건뿐이다. 적재가 덜 됐는지 봐야 한다"
            % (모은것, stored),
        )
    if stored > 모은것:
        return _check(
            "적재 대조",
            WARN,
            "모은 기사는 %d건인데 DB 에는 %d건이 쌓여 있다. 브리핑은 DB 쪽 %d건으로 세니 "
            "이 표의 수집 건수와 다른 숫자가 나온다" % (모은것, stored, stored),
        )
    return _check(
        "적재 대조",
        OK,
        "모은 기사 %d건, DB 에 쌓인 기사 %d건이다" % (모은것, stored),
    )


def _history_info(conn, date_str, cfg):
    """앞선 며칠 기록이 DB 에 남아 있는지 본다.

    이 판단이 브리핑의 '처음 보는 키워드' 절을 낼지 말지를 가른다. 앞선 기록이
    한 건도 없는 DB 로는 어떤 말이든 처음 보는 말이 되어 버려서, 그 절을 내보내면
    매일 거짓을 적는 셈이 된다. 깃허브 액션이 DB 를 이어 가지 못한 날이 그렇다.
    점검은 여기서 판단만 만들고, 절을 비우는 일은 브리핑 쪽이 한다.
    """
    lookback = int(
        _number_setting(
            cfg,
            ("brief.first_seen_lookback_days", "qa.lookback_days", "lookback_days"),
            DEFAULT_LOOKBACK_DAYS,
        )
    )
    비었을때 = {
        "ready": False,
        "days": lookback,
        "article_count": 0,
        "days_with_articles": 0,
    }
    if conn is None:
        비었을때["reason"] = "DB 를 열지 않아 앞선 %d일 기록을 볼 수 없다" % lookback
        return 비었을때
    try:
        from . import store

        target = _parse_day(date_str)
        start = (target - dt.timedelta(days=lookback)).isoformat()
        end = (target - dt.timedelta(days=1)).isoformat()
        counts = store.daily_counts(conn, start, end)
        값들 = [int(v) for v in (counts or {}).values()]
    except Exception as err:
        비었을때["reason"] = "앞선 기록을 읽지 못했다. %s" % err
        return 비었을때

    전체 = sum(값들)
    if 전체 <= 0:
        비었을때["reason"] = (
            "앞선 %d일 기사가 DB 에 한 건도 없다. DB 를 이어 받지 못한 실행이다" % lookback
        )
        return 비었을때
    return {
        "ready": True,
        "days": lookback,
        "article_count": 전체,
        "days_with_articles": sum(1 for v in 값들 if v > 0),
        "reason": "",
    }


def _check_history(info):
    """앞선 기록이 이어졌는지 한 줄로 남긴다. 끊겼으면 무엇을 못 하는지 적는다."""
    if info.get("ready"):
        return _check(
            "기록 이어짐",
            OK,
            "앞선 %d일 가운데 %d일에 기사 %d건이 남아 있다. 처음 보는 키워드를 견줄 수 있다"
            % (info["days"], info.get("days_with_articles", 0), info["article_count"]),
        )
    return _check(
        "기록 이어짐",
        WARN,
        "%s. 처음 보는 키워드 절은 비우고 평균 대비도 건너뛴다"
        % info.get("reason", "앞선 기록을 확인하지 못했다"),
    )


def _recent_average(conn, date_str, lookback_days):
    """대상 날짜 앞의 며칠을 평균 낸다. 기사가 없던 날도 0 으로 넣어 센다."""
    from . import store

    target = _parse_day(date_str)
    start = (target - dt.timedelta(days=lookback_days)).isoformat()
    end = (target - dt.timedelta(days=1)).isoformat()

    counts_fn = getattr(store, "daily_counts", None)
    if counts_fn is not None:
        counts = counts_fn(conn, start, end)
        values = [int(v) for v in counts.values()]
    else:
        # daily_counts 가 없는 경우를 대비해 계약에 적힌 함수만으로도 셀 수 있게 해 둔다.
        values = []
        cursor = target - dt.timedelta(days=lookback_days)
        while cursor < target:
            values.append(len(store.articles_for_date(conn, cursor.isoformat())))
            cursor += dt.timedelta(days=1)

    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def _check_volume(conn, date_str, total, cfg):
    """건수가 최근 이레 평균의 절반 아래이거나 두 배 위면 주의로 남긴다."""
    if conn is None:
        return _check("최근 이레 대비", OK, "견줄 기록이 없어 건너뛰었다")

    lookback = int(
        _number_setting(
            cfg,
            ("qa.lookback_days", "qa.average_days", "lookback_days"),
            DEFAULT_LOOKBACK_DAYS,
        )
    )
    low = _number_setting(cfg, ("qa.volume_low_ratio", "volume_low_ratio"), DEFAULT_LOW_RATIO)
    high = _number_setting(
        cfg, ("qa.volume_high_ratio", "volume_high_ratio"), DEFAULT_HIGH_RATIO
    )
    floor = _number_setting(
        cfg, ("qa.volume_min_average", "volume_min_average"), DEFAULT_MIN_AVERAGE
    )

    try:
        average, days = _recent_average(conn, date_str, lookback)
    except Exception as err:
        return _check("최근 이레 대비", WARN, "최근 기록을 읽지 못했다. %s" % err)

    if not average or average < floor:
        return _check(
            "최근 이레 대비",
            OK,
            "앞선 %d일 평균이 %s건이라 견주기에는 이르다"
            % (days, _round(average or 0)),
        )

    ratio = total / average
    자세히 = "어제 %d건, 앞선 %d일 평균 %s건, 평균의 %s배다" % (
        total,
        days,
        _round(average),
        _round(ratio),
    )
    if ratio <= low:
        return _check("최근 이레 대비", WARN, "건수가 평균의 절반 아래로 줄었다. " + 자세히)
    if ratio >= high:
        return _check("최근 이레 대비", WARN, "건수가 평균의 두 배 위로 늘었다. " + 자세히)
    return _check("최근 이레 대비", OK, 자세히)


def _round(value):
    """표에 적을 만큼만 소수를 줄인다."""
    return ("%.1f" % float(value)).rstrip("0").rstrip(".")


def _check_drop_ratio(result, sorry, cfg):
    """판단 단계가 걸러낸 비율이 임계값을 넘으면 주의로 남긴다."""
    limit = _number_setting(
        cfg, ("qa.max_drop_ratio", "judge.max_drop_ratio", "max_drop_ratio"),
        DEFAULT_DROP_RATIO,
    )

    if sorry:
        return _check("걸러낸 비율", WARN, "판단 기록을 읽지 못했다. %s" % sorry)

    if not isinstance(result, dict):
        return _check(
            "걸러낸 비율", OK, "판단을 아직 돌리지 않아 건너뛰었다"
        )

    kept = len(result.get("kept") or [])
    dropped = len(result.get("dropped") or [])
    total = kept + dropped
    if total == 0:
        return _check("걸러낸 비율", OK, "판단한 기사가 없어 건너뛰었다")

    ratio = dropped / total
    자세히 = "판단한 %d건 가운데 %d건을 걸러냈다. 비율 %s, 임계값 %s" % (
        total,
        dropped,
        _percent(ratio),
        _percent(limit),
    )
    if ratio > limit:
        return _check("걸러낸 비율", WARN, "걸러낸 비율이 임계값을 넘었다. " + 자세히)
    return _check("걸러낸 비율", OK, 자세히)


def _percent(value):
    """비율을 백분율 문자열로 바꾼다."""
    return "%d%%" % round(float(value) * 100)


def _load_judged(conn, date_str, judged):
    """판단 결과를 고른다. 넘겨받은 것이 없으면 DB 에 남은 기록을 읽는다.

    돌려주는 값은 (판단 결과 또는 None, 읽지 못한 사유) 다.
    """
    if isinstance(judged, dict):
        return judged, ""
    if judged is not None or conn is None:
        return None, ""
    try:
        from . import judge

        return judge.load_decisions(conn, date_str), ""
    except Exception as err:
        return None, str(err)


def _judged_total(result):
    """판단이 다룬 기사 수. 남긴 것과 버린 것을 합친다."""
    if not isinstance(result, dict):
        return None
    return len(result.get("kept") or []) + len(result.get("dropped") or [])


def _check_judged_count(result, stored, sorry):
    """판단이 다룬 건수가 DB 에 쌓인 건수와 같은지 본다. 다르면 멈춘다.

    판단은 DB 에 쌓인 하루치를 받아 돌고, 브리핑의 기사 수도 DB 를 센다. 그래서
    두 숫자가 갈리면 브리핑 본문과 건수가 서로 다른 하루를 말하게 된다. 같은
    날짜를 다시 수집해 기사가 늘었는데 저장해 둔 옛 판단을 그대로 쓴 경우가
    그렇다. 판단 쪽이 링크 묶음을 견주어 다시 판단하도록 고쳐져 있으니 여기서
    걸릴 일은 드물지만, 그 그물이 뚫렸을 때 어긋난 브리핑이 나가는 것을 막는
    마지막 자리다. 새로 판단하게 하려면 --force 를 붙인다.

    견줄 기준은 DB 쪽 건수뿐이다. 수집한 건수와 DB 건수가 갈리는 경우는 적재
    대조가 따로 보고 있어서, 여기서 또 세면 같은 말을 두 번 하게 된다.
    """
    if sorry:
        return _check("판단 대조", WARN, "판단 기록을 읽지 못해 대조하지 못했다. %s" % sorry)

    total = _judged_total(result)
    if total is None:
        return _check("판단 대조", OK, "판단을 아직 돌리지 않아 건너뛰었다")
    if total == 0:
        return _check("판단 대조", OK, "판단한 기사가 없어 건너뛰었다")
    if stored is None:
        return _check("판단 대조", OK, "DB 를 읽지 않아 견주지 못했다")
    if stored == 0:
        return _check(
            "판단 대조",
            OK,
            "DB 에 그 날짜 기사가 없어 견주지 못했다. 적재 대조 쪽을 본다",
        )
    if total != stored:
        return _check(
            "판단 대조",
            FAIL,
            "판단은 %d건을 다뤘는데 DB 에는 %d건이 쌓여 있다. 브리핑은 DB 쪽으로 세니 "
            "본문과 건수가 어긋난다. --force 로 다시 판단해야 한다" % (total, stored),
        )
    return _check("판단 대조", OK, "판단이 다룬 %d건이 DB 에 쌓인 건수와 같다" % total)


# ---------------------------------------------------------------------------
# 링크 대조
# ---------------------------------------------------------------------------

def _flag_setting(cfg, paths, default):
    """설정에서 참거짓을 찾는다. 읽히지 않으면 기본값을 쓴다."""
    for path in paths:
        value = _dig(cfg or {}, path)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        if isinstance(value, bool):
            return value
        글자 = str(value).strip().lower()
        if 글자 in ("1", "true", "yes", "y", "on", "참", "예"):
            return True
        if 글자 in ("0", "false", "no", "n", "off", "거짓", "아니오"):
            return False
        logger.warning("%s 설정값을 참거짓으로 읽지 못해 기본값을 쓴다. 받은 값 %r", path, value)
    return bool(default)


def _page_title(text):
    """받아 온 HTML 에서 title 을 꺼낸다. 없으면 빈 문자열이다."""
    찾은것 = _TITLE_TAG.search(text or "")
    if not 찾은것:
        return ""
    # 태그를 먼저 떼고 그 다음에 엔티티를 푼다. 순서를 뒤집으면 제목에 적힌
    # &lt;단독&gt; 이 태그로 보여 그대로 지워진다.
    벗긴것 = re.sub(r"<[^>]*>", " ", 찾은것.group(1))
    return re.sub(r"\s+", " ", html.unescape(벗긴것)).strip()


def _words(text):
    """제목을 견줄 낱말 묶음으로 쪼갠다. 두 글자 아래는 버린다."""
    return {조각.lower() for 조각 in _WORD.findall(str(text or ""))}


def _title_overlap(article_title, page_title):
    """기사 제목과 응답 제목이 얼마나 겹치는지 0 에서 1 사이로 잰다.

    짧은 쪽을 분모로 삼는다. 매체가 제목 뒤에 자기 이름을 붙여 오는 일이 흔해서,
    긴 쪽을 분모로 두면 맞는 링크도 어긋난 것으로 보인다.
    """
    왼쪽 = _words(article_title)
    오른쪽 = _words(page_title)
    if not 왼쪽 or not 오른쪽:
        return 0.0
    return len(왼쪽 & 오른쪽) / min(len(왼쪽), len(오른쪽))


def default_link_fetcher(timeout):
    """링크 하나를 받아 상태 코드와 응답 제목을 돌려주는 부르개를 만든다.

    본문은 앞쪽 일부만 읽는다. title 은 head 안에 있어서 그만큼이면 충분하고,
    기사 한 장을 통째로 받아 둘 까닭도 없다.
    """
    def fetcher(url):
        from urllib import request

        from . import netssl

        요청 = request.Request(str(url), headers={"User-Agent": LINK_CHECK_AGENT})
        with request.urlopen(요청, timeout=timeout, context=netssl.ssl_context()) as 응답:
            상태 = getattr(응답, "status", None) or 응답.getcode()
            글자셋 = 응답.headers.get_content_charset() or "utf-8"
            원본 = 응답.read(LINK_CHECK_MAX_BYTES)
        본문 = 원본.decode(글자셋, "replace")
        return {"status": int(상태), "title": _page_title(본문), "bytes": len(원본)}

    return fetcher


def _check_links(articles, cfg, fetcher=None):
    """브리핑에 오를 링크 몇 건을 실제로 받아 제목이 맞는지 본다.

    기본은 꺼져 있다. 켜려면 qa.link_check.enabled 를 참으로 둔다. 꺼 둔 까닭은
    둘이다. 하나, 이 점검만 바깥으로 요청을 내보낸다. 둘, 저장소에 든 픽스처는
    손으로 지어낸 기사 번호라 링크가 실제 기사로 이어지지 않아 늘 어긋남으로 나온다.

    상태 코드만 세지 않는다. 없는 기사 번호에도 200 과 틀만 남은 페이지를 돌려주는
    매체가 있어서, 상태 코드만 보면 그런 링크가 다 통과한다. 그래서 응답의 title 을
    꺼내 기사 제목과 낱말이 겹치는지까지 본다. 겹침이 임계값 아래면 어긋남으로 센다.

    어긋남은 주의로만 남긴다. 워크북이 정한 멈춤 조건은 다섯 가지 그대로 둔다.
    """
    if not _flag_setting(
        cfg, ("qa.link_check.enabled", "qa.link_check_enabled"),
        DEFAULT_LINK_CHECK_ENABLED,
    ):
        return _check(
            "기사 링크 대조",
            OK,
            "설정에서 꺼 두어 건너뛰었다. 켜려면 qa.link_check.enabled 를 참으로 둔다",
        )

    표본수 = int(
        _number_setting(
            cfg, ("qa.link_check.sample", "qa.link_check_sample"), DEFAULT_LINK_CHECK_SAMPLE
        )
    )
    임계값 = _number_setting(
        cfg,
        ("qa.link_check.min_overlap", "qa.link_check_min_overlap"),
        DEFAULT_LINK_CHECK_MIN_OVERLAP,
    )
    제한시간 = _number_setting(
        cfg,
        ("qa.link_check.timeout_seconds", "qa.link_check_timeout_seconds"),
        DEFAULT_LINK_CHECK_TIMEOUT,
    )

    고른것 = [
        item
        for item in articles
        if str(item.get("url") or "").strip() and str(item.get("title") or "").strip()
    ][: max(0, 표본수)]
    if not 고른것:
        return _check("기사 링크 대조", OK, "대조할 링크가 없어 건너뛰었다")

    부르개 = fetcher or default_link_fetcher(제한시간)
    맞은것 = []
    어긋난것 = []
    못본것 = []
    for item in 고른것:
        url = str(item["url"]).strip()
        try:
            응답 = 부르개(url) or {}
        except Exception as err:
            못본것.append("%s(%s)" % (_short_url(url), err))
            continue
        상태 = 응답.get("status")
        응답제목 = str(응답.get("title") or "").strip()
        if 상태 != 200:
            어긋난것.append("%s(상태 %s)" % (_short_url(url), 상태))
            continue
        if not 응답제목:
            어긋난것.append("%s(응답에 제목이 없다)" % _short_url(url))
            continue
        겹침 = _title_overlap(item.get("title"), 응답제목)
        if 겹침 < 임계값:
            어긋난것.append("%s(겹침 %s)" % (_short_url(url), _percent(겹침)))
        else:
            맞은것.append(_short_url(url))

    자세히 = "%d건을 받아 봤다. 제목이 맞은 것 %d건, 어긋난 것 %d건, 못 받은 것 %d건, 겹침 임계값 %s" % (
        len(고른것),
        len(맞은것),
        len(어긋난것),
        len(못본것),
        _percent(임계값),
    )
    if 어긋난것:
        return _check(
            "기사 링크 대조",
            WARN,
            "링크가 가리키는 곳이 기사 제목과 어긋난다. %s. %s"
            % (자세히, _samples(어긋난것)),
        )
    if 못본것:
        return _check(
            "기사 링크 대조",
            WARN,
            "받아 보지 못한 링크가 있다. %s. %s" % (자세히, _samples(못본것)),
        )
    return _check("기사 링크 대조", OK, 자세히)


# ---------------------------------------------------------------------------
# 표 만들기
# ---------------------------------------------------------------------------

def _width(text):
    """한글이 섞인 문자열의 화면 너비를 잰다. 넓은 글자는 두 칸으로 본다."""
    total = 0
    for ch in str(text):
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _pad(text, width):
    """정한 너비에 맞게 오른쪽을 공백으로 채운다."""
    text = str(text)
    return text + " " * max(0, width - _width(text))


def format_checks(checks):
    """점검 목록을 표 모양 문자열로 바꾼다. 로그와 증거로 남기는 쪽이다."""
    머리 = ("점검", "상태", "내용")
    줄들 = [(c["name"], c["status"], c["detail"]) for c in checks]
    if not 줄들:
        return "점검한 항목이 없다"

    폭 = [
        max(_width(머리[i]), max(_width(줄[i]) for 줄 in 줄들))
        for i in range(3)
    ]
    선 = "  ".join("-" * 폭[i] for i in range(3))
    쓴줄 = ["  ".join(_pad(머리[i], 폭[i]) for i in range(3)).rstrip(), 선]
    for 줄 in 줄들:
        쓴줄.append("  ".join(_pad(줄[i], 폭[i]) for i in range(3)).rstrip())
    return "\n".join(쓴줄)


def _summary_line(checks, exit_code):
    """사람이 한 줄만 읽고도 상태를 알 수 있게 정리한다."""
    통과 = sum(1 for c in checks if c["status"] == OK)
    주의 = sum(1 for c in checks if c["status"] == WARN)
    멈춤 = sum(1 for c in checks if c["status"] == FAIL)
    끝말 = "여기서 멈춘다" if exit_code else "계속 진행해도 된다"
    return "점검 %d개 가운데 통과 %d개, 주의 %d개, 멈춤 %d개다. %s" % (
        len(checks),
        통과,
        주의,
        멈춤,
        끝말,
    )


# ---------------------------------------------------------------------------
# 들어온 자료 추리기
# ---------------------------------------------------------------------------

def _resolve_collected(conn, date_str, collected):
    """수집 결과에서 기사 목록과 상한 구간, 합친 건수를 꺼낸다.

    collect_day 가 준 딕셔너리를 그대로 받는 것이 본래 쓰임이다.
    기사 목록만 넘겨도 받고, 아무것도 없으면 DB 에 쌓인 것으로 대신한다.
    상한 구간은 수집 기록에만 있어서 DB 로 대신할 때는 확인할 수 없다고 적어 둔다.
    """
    if isinstance(collected, dict):
        articles = list(collected.get("articles") or [])
        truncated = list(collected.get("truncated_windows") or [])
        묶음 = collected.get("dedupe")
        meta = 묶음 if isinstance(묶음, dict) else collected
        return articles, truncated, True, meta
    if isinstance(collected, (list, tuple)):
        return list(collected), [], False, None
    if conn is not None:
        try:
            from . import store

            return list(store.articles_for_date(conn, date_str)), [], False, None
        except Exception as err:
            logger.warning("수집 결과도 DB 도 읽지 못했다. %s", err)
    return [], [], False, None


# ---------------------------------------------------------------------------
# 바깥에서 부르는 자리
# ---------------------------------------------------------------------------

def run_checks(conn, date_str, collected, cfg=None, judged=None, stage=STAGE_ALL,
               link_fetcher=None):
    """하루치를 점검하고 결과와 종료 코드를 돌려준다.

    fail 이 하나라도 있으면 exit_code 는 1 이다. warn 만 있으면 0 이라 계속 간다.
    돌려주는 값에는 점검 목록과 종료 코드 말고도 표 문자열과 한 줄 요약이 들어 있다.
    cli 가 그대로 찍어 증거로 남기라고 같이 담았다.

    stage 로 어느 자리에서 부르는지 알려 준다. STAGE_COLLECTED 는 적재 전에 부르는
    자리라 받아 온 것만으로 볼 수 있는 멈춤 조건 다섯 가지만 본다. DB 와 판단 기록을
    보는 항목은 그 자리에서 아직 볼 것이 없다. STAGE_ALL 은 적재와 판단을 마친 뒤라
    열한 가지를 다 본다.

    judged 를 넘기면 걸러낸 비율을 그 자리에서 바로 잰다. 넘기지 않으면 DB 에 남은
    판단 기록을 읽는데, 그 날짜를 처음 돌린 실행에는 기록이 아직 없어 건너뛰게 된다.
    그래서 cli 는 판단을 먼저 돌리고 그 결과를 여기로 넘긴다.

    link_fetcher 는 링크 대조 점검이 쓸 부르개다. 시험에서 물려 주려고 열어 둔 자리다.
    """
    cfg = cfg or {}
    day = _parse_day(date_str).isoformat()
    articles, truncated, collected_known, meta = _resolve_collected(conn, date_str, collected)

    checks = [
        _check_count(articles, meta),
        _check_truncated(truncated, collected_known),
        _check_dates(articles, day, cfg, meta),
        _check_fields(articles),
        _check_duplicates(articles, meta),
    ]

    history = _history_info(None, day, cfg)
    stored = None
    if str(stage) != STAGE_COLLECTED:
        stored, 못읽은사유 = _stored_count(conn, day)
        적재 = _check_stored(stored, 못읽은사유, articles)
        if 적재 is not None:
            checks.append(적재)
        history = _history_info(conn, day, cfg)
        checks.append(_check_history(history))
        checks.append(_check_volume(conn, day, len(articles), cfg))
        판단결과, 판단못읽은사유 = _load_judged(conn, day, judged)
        checks.append(_check_drop_ratio(판단결과, 판단못읽은사유, cfg))
        checks.append(_check_judged_count(판단결과, stored, 판단못읽은사유))
        checks.append(_check_links(articles, cfg, link_fetcher))

    exit_code = 1 if any(c["status"] == FAIL for c in checks) else 0
    table = format_checks(checks)
    summary = _summary_line(checks, exit_code)

    for c in checks:
        if c["status"] == FAIL:
            logger.error("%s 점검이 막혔다. %s", c["name"], c["detail"])
        elif c["status"] == WARN:
            logger.warning("%s 점검에 주의가 붙었다. %s", c["name"], c["detail"])
    logger.info("%s 점검을 마쳤다. %s", day, summary)

    return {
        "date": day,
        "stage": str(stage),
        "checks": checks,
        "exit_code": exit_code,
        "table": table,
        "summary": summary,
        "article_count": len(articles),
        "stored_count": stored,
        "history": history,
    }
