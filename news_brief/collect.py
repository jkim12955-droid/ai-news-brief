"""하루치 기사를 모으는 모듈.

한국 날짜 하나를 받아 UTC 구간으로 바꾸고, GDELT 를 불러 기사 목록을 채운다.
한 번에 250건이 상한이라 응답이 상한에 딱 걸리면 뒤가 잘렸다고 보고 시간 구간을
절반으로 쪼개 다시 받는다. 받은 원본은 data/raw 에 날짜별로 남긴다.

수집기는 판단을 하지 않는다. 제목을 다듬고 같은 링크를 걸러내는 데까지만 하고,
어떤 기사가 AI 소식인지 가리는 일은 judge 쪽에 맡긴다.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import urllib.parse
from pathlib import Path

from . import config, gdelt

logger = logging.getLogger(__name__)

# 설정 파일에 아무것도 없을 때 쓰는 기본 검색어.
DEFAULT_QUERY = '("artificial intelligence" OR OpenAI) sourcelang:korean'

# 구간을 절반으로 쪼개는 횟수의 한계. 기본 4면 하루가 90분 토막까지 잘게 나뉜다.
DEFAULT_MAX_SPLIT_DEPTH = 4

# 설정이 더 큰 값을 줘도 여기서 끊는다.
#
# 깊이가 하나 늘면 최악의 호출 수가 배로 늘고, 구간마다 5초를 쉰 뒤 응답을
# 기다린다. 실측으로 응답이 16초쯤 걸렸으니 구간 하나에 21초로 잡는다.
# 깊이 5면 최악이 63번에 스물두 분이라 daily.yml 의 timeout-minutes: 30 안에
# 들지만, 깊이 6이면 127번에 마흔네 분으로 넘긴다. 작업이 잘리면 산출물도
# 실패 알림도 남지 않는다. 놓친 구간을 truncated_windows 에 적어 남기고
# 끝내는 편이, 아무 기록도 없이 잘리는 것보다 낫다.
#
# 깊이 5면 하루를 마흔다섯 분 토막까지 나누므로, 상한 250건으로도 하루
# 팔천 건까지 받을 수 있다. 하루 수백 건인 이 검색어에는 넉넉하다.
MAX_SPLIT_DEPTH_LIMIT = 5

# GDELT 시각 형식. kst_day_window 가 돌려주는 값도 이 모양이다.
WINDOW_FORMAT = "%Y%m%d%H%M%S"

# 설정 값을 찾을 때 들여다볼 하위 묶음. 설정 파일 모양이 조금 달라도 견디게 했다.
_SETTING_SECTIONS = ("collect", "gdelt", "settings", "collection")

# 링크에서 버릴 질의 파라미터.
#
# 처음에는 살려 둘 이름만 열거하는 허용 목록이었다. 그 방식은 목록에 없는
# 이름으로 기사 번호를 들고 있는 매체를 통째로 망가뜨린다. idx, nno,
# contents_id, newsIdx 처럼 국내 CMS 에서 흔한 이름이 실제로 그랬다.
# news.site.co.kr/view.php?idx=1001 과 ?idx=1002 가 같은 열쇠로 무너져
# 서로 다른 기사가 한 건으로 합쳐졌다. 더 나쁜 것은 그 손실이 어느 점검에도
# 걸리지 않는다는 점이다. 합치는 일이 점검보다 먼저 끝나기 때문이다.
#
# 그래서 정책을 뒤집었다. 모르는 이름은 남기고, 추적과 화면 표시에만 쓰이는
# 이름만 버린다. 둘을 견주면 한쪽은 같은 기사가 둘로 갈라지는 일이고 다른
# 한쪽은 다른 기사가 한 건으로 합쳐지는 일인데, 앞쪽은 브리핑에 기사가 두 번
# 올라 사람 눈에 곧 보이고 뒤쪽은 기사가 조용히 사라져 아무도 모른다.
#
_DROP_PARAMS = frozenset(
    {
        # 유입 경로를 적어 두는 꼬리표
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader",
        "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "ttclid", "yclid",
        "igshid", "mc_cid", "mc_eid", "spm",
        "ref", "referer", "referrer", "referral", "source", "src", "from",
        "cmpid", "cmp", "campaign",
        # 화면 모양만 바꾸는 이름
        "cloc", "dablewidget", "outputtype", "amp",
    }
)

# 이 접두사로 시작하는 이름도 버린다. utm_ 처럼 변종이 끝없이 붙는 것들이다.
_DROP_PREFIXES = ("utm_", "_ga", "ga_", "nil_", "t__nil", "pk_", "mtm_", "piwik_")

# 분야를 가리키는 이름. 버리되 아래 호스트에서만 버린다.
#
# 네이버 주소는 ?sid=105 로 분야를 적고 기사 번호는 경로에 둔다. 그래서
# 네이버에서는 sid 를 남기면 같은 기사가 분야 표시에 따라 둘로 갈라진다.
# 반대로 이름 없는 매체에서 sid 가 기사 번호인 경우가 있는데, 그때 sid 를
# 버리면 서로 다른 기사가 한 건으로 합쳐진다. 두 잘못의 무게가 달라서
# (합쳐지면 기사가 조용히 사라지고, 갈라지면 사람 눈에 곧 보인다)
# 버리는 자리를 아는 호스트로만 좁혔다.
_SECTION_PARAMS = frozenset({"sid", "sid1", "sid2", "mid"})
_SECTION_PARAM_HOSTS = ("naver.com", "nate.com", "daum.net")


def _포털분야파라미터인가(소문자이름, netloc):
    """분야를 가리키는 이름인지, 그것이 분야로 쓰이는 호스트인지 함께 본다."""
    if 소문자이름 not in _SECTION_PARAMS:
        return False
    호스트 = str(netloc or "").lower()
    return any(
        호스트 == 자리 or 호스트.endswith("." + 자리) for 자리 in _SECTION_PARAM_HOSTS
    )


def _버릴파라미터인가(이름, netloc=""):
    """질의 파라미터 이름 하나가 추적용인지 본다."""
    소문자 = str(이름).lower()
    if 소문자 in _DROP_PARAMS:
        return True
    if _포털분야파라미터인가(소문자, netloc):
        return True
    return 소문자.startswith(_DROP_PREFIXES)

_WHITESPACE = re.compile(r"[\s\u00a0\u200b\u200c\u200d\ufeff]+")
_SPACE_BEFORE_CLOSING = re.compile(r"\s+([,.!?;:%…)\]}）］｝»›」』〉》，。、！？：；％])")
_SPACE_AFTER_OPENING = re.compile(r"([(\[{（［｛«‹「『〈《])\s+")
_PADDED_DOUBLE_QUOTE = re.compile(r'"\s*([^"]*?)\s*"')
_PADDED_CURLY_DOUBLE = re.compile(r"“\s*([^”]*?)\s*”")
_PADDED_CURLY_SINGLE = re.compile(r"‘\s*([^’]*?)\s*’")
_PADDED_SINGLE_QUOTE = re.compile(r"'\s+([^']+?)\s+'")
_TRAILING_SEPARATORS = " \t-|·,:;/"


def clean_title(title):
    """제목에서 군더더기 공백을 덜어 낸다. 낱말 자체는 건드리지 않는다.

    GDELT 제목에는 HTML 기호가 그대로 실려 오거나, 괄호와 따옴표 안쪽에 공백이
    끼어 있는 경우가 많다. 묶음을 만들 때 같은 제목이 서로 다른 것으로 보이지
    않도록 여기서 모양을 맞춘다.
    """
    if not title:
        return ""

    text = html.unescape(str(title))
    if "&amp;" in text or "&#" in text:
        # 두 번 감싸인 제목이 섞여 들어와 한 번 더 푼다.
        text = html.unescape(text)

    text = _WHITESPACE.sub(" ", text)

    # 짝이 맞는 따옴표 안쪽 공백을 먼저 붙인다.
    text = _PADDED_DOUBLE_QUOTE.sub(lambda m: '"%s"' % m.group(1), text)
    text = _PADDED_CURLY_DOUBLE.sub(lambda m: "“%s”" % m.group(1), text)
    text = _PADDED_CURLY_SINGLE.sub(lambda m: "‘%s’" % m.group(1), text)
    text = _PADDED_SINGLE_QUOTE.sub(lambda m: "'%s'" % m.group(1), text)

    text = _SPACE_BEFORE_CLOSING.sub(r"\1", text)
    text = _SPACE_AFTER_OPENING.sub(r"\1", text)

    text = _WHITESPACE.sub(" ", text).strip()

    # 매체 이름이 떨어져 나가고 남은 꼬리 기호를 정리한다.
    trimmed = text.rstrip(_TRAILING_SEPARATORS).lstrip(_TRAILING_SEPARATORS)
    return trimmed if trimmed else text


def normalize_url(url):
    """링크를 견줄 때 쓸 열쇠를 만든다.

    주소 체계와 www, 모바일 접두사, 끝의 빗금, 추적용 질의 파라미터를 걷어 낸다.
    모르는 파라미터는 남긴다. 기사 번호를 파라미터로만 들고 있는 매체가 있어서,
    이름을 몰라 버리면 서로 다른 기사가 한 건으로 합쳐진다.
    분야를 가리키는 sid 류는 그것이 분야로 쓰이는 포털 호스트에서만 버린다.
    """
    text = (url or "").strip()
    if not text:
        return ""

    parsed = urllib.parse.urlsplit(text)
    if not parsed.netloc:
        # 주소 체계가 빠진 값은 그대로 열쇠로 쓴다.
        return text.lower().rstrip("/")

    netloc = parsed.netloc.lower()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if parsed.scheme.lower() == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if parsed.scheme.lower() == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    for prefix in ("www.", "m.", "mobile."):
        if netloc.startswith(prefix):
            netloc = netloc[len(prefix) :]
            break

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [
        (name, value)
        for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if not _버릴파라미터인가(name, netloc)
    ]
    kept.sort()

    key = netloc + path
    if kept:
        key += "?" + urllib.parse.urlencode(kept)
    return key


def _setting(cfg, names, default):
    """설정에서 값을 하나 집어 온다. 위쪽에 없으면 하위 묶음도 뒤진다."""
    if not isinstance(cfg, dict):
        return default
    for name in names:
        if cfg.get(name) is not None:
            return cfg[name]
    for section in _SETTING_SECTIONS:
        block = cfg.get(section)
        if isinstance(block, dict):
            for name in names:
                if block.get(name) is not None:
                    return block[name]
    return default


def _project_root(cfg):
    """산출물을 쌓을 뿌리 경로를 정한다."""
    for name in ("root", "project_root", "base_dir"):
        value = _setting(cfg, (name,), None)
        if value:
            return Path(value)
    paths = cfg.get("paths") if isinstance(cfg, dict) else None
    if isinstance(paths, dict):
        for name in ("root", "project_root", "base_dir"):
            if paths.get(name):
                return Path(paths[name])
    return Path(__file__).resolve().parents[1]


def _raw_dir(cfg):
    """원본 응답을 남길 폴더."""
    data_dir = _setting(cfg, ("data_dir", "data_path"), None)
    if not data_dir:
        paths = cfg.get("paths") if isinstance(cfg, dict) else None
        if isinstance(paths, dict):
            data_dir = paths.get("data_dir") or paths.get("data")
    base = Path(data_dir) if data_dir else _project_root(cfg) / "data"
    return base / "raw"


def _parse_window(value):
    """'YYYYMMDDHHMMSS' 문자열을 시각으로 바꾼다."""
    return dt.datetime.strptime(str(value), WINDOW_FORMAT)


def _split_window(start_utc, end_utc):
    """구간을 절반으로 나눈다. 더 나눌 수 없으면 None 을 돌려준다.

    가운데 시각을 양쪽이 함께 쓰게 둔 것은, 경계에서 기사가 새는 것보다
    같은 기사를 두 번 받는 편이 낫기 때문이다. 겹치는 몫은 뒤에서 걸러진다.
    """
    try:
        start = _parse_window(start_utc)
        end = _parse_window(end_utc)
    except (ValueError, TypeError):
        return None
    if end <= start or (end - start) < dt.timedelta(seconds=2):
        return None
    middle = start + (end - start) / 2
    middle = middle.replace(microsecond=0)
    if middle <= start or middle >= end:
        return None
    return [
        (start.strftime(WINDOW_FORMAT), middle.strftime(WINDOW_FORMAT)),
        (middle.strftime(WINDOW_FORMAT), end.strftime(WINDOW_FORMAT)),
    ]


def _normalize_seendate(value):
    """GDELT 시각을 '20260911T103000Z' 모양으로 맞춘다."""
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 14:
        return "%sT%sZ" % (digits[:8], digits[8:])
    return text


def _normalize_article(item):
    """GDELT 기사 한 건을 우리 모양으로 바꾼다. 링크가 없으면 버린다."""
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None

    title_raw = str(item.get("title") or "")
    domain = str(item.get("domain") or "").strip().lower()
    if not domain:
        domain = urllib.parse.urlsplit(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[len("www.") :]

    return {
        "url": url,
        "title": clean_title(title_raw),
        "title_raw": title_raw,
        "domain": domain,
        "seendate": _normalize_seendate(item.get("seendate")),
        "language": str(item.get("language") or "").strip(),
    }


def _dedupe(articles):
    """같은 링크를 가리킨 기사를 한 건으로 줄인다. 먼저 온 쪽을 남긴다.

    돌려주는 값은 (남긴 기사, 겹친 건수, 글자가 달랐는데 합친 건수) 세 개다.
    마지막 값을 따로 센 까닭이 있다. 구간을 쪼개면 경계 기사가 글자까지 똑같이
    두 번 들어오는데 그건 정상이다. 반면 링크 글자는 다른데 열쇠만 같아 합쳐진
    건은 normalize_url 이 살려야 할 파라미터를 버렸다는 신호일 수 있다.
    두 숫자를 한데 세면 그 신호가 정상 중복에 묻혀 보이지 않는다.
    """
    seen = {}
    kept = []
    duplicated = 0
    merged = 0
    for article in articles:
        key = normalize_url(article["url"]) or article["url"]
        먼저온링크 = seen.get(key)
        if 먼저온링크 is not None:
            duplicated += 1
            if 먼저온링크 != article["url"]:
                merged += 1
                logger.debug(
                    "링크 글자는 다른데 열쇠가 같아 한 건으로 합쳤다. 열쇠 %s, 먼저 온 것 %s, 뒤에 온 것 %s",
                    key, 먼저온링크, article["url"],
                )
            continue
        seen[key] = article["url"]
        kept.append(article)
    return kept, duplicated, merged


def _sort_key(article):
    """같은 날을 다시 돌려도 차례가 흔들리지 않도록 시각과 링크로 정렬한다.

    늦게 나온 기사가 앞에 오되, 시각이 같으면 링크를 오름차순으로 둔다.
    시각만으로는 GDELT 가 같은 분에 여러 건을 주는 날 차례가 흔들린다.
    """
    return (article.get("seendate") or "", _InvertedText(article.get("url") or ""))


class _InvertedText:
    """정렬 방향만 뒤집어 주는 껍데기.

    목록 전체를 내림차순으로 세우면서 링크만 오름차순으로 두려고 쓴다.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, _InvertedText) and self.value == other.value

    def __lt__(self, other):
        return self.value > other.value


def _kst_date_of_window(start_utc):
    """UTC 구간 시작 시각이 한국 날짜로 언제인지 본다."""
    stamp = _parse_window(start_utc).replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat()


def _within(seendate, start_utc, end_utc):
    """기사 시각이 구간 안에 드는지 본다. 양쪽 끝을 모두 포함한다."""
    digits = re.sub(r"\D", "", str(seendate or ""))
    if len(digits) != 14:
        return False
    return str(start_utc) <= digits <= str(end_utc)


def fixture_fetcher(fixture_dir, date_str=None):
    """저장해 둔 응답으로 GDELT 를 대신할 부르개를 만든다.

    tests/fixtures/gdelt_<한국날짜>.json 을 찾아 읽고, 요청한 시간 구간에 드는
    기사만 골라 돌려준다. 구간을 쪼개 다시 부르는 길도 실제 API 를 부를 때와
    똑같이 돌아간다. 망을 타지 않으므로 시험과 오프라인 시연에 쓴다.
    """
    base = Path(fixture_dir)
    기억 = {}

    def _읽기(day):
        if day not in 기억:
            path = base / ("gdelt_%s.json" % day)
            if path.exists():
                with path.open(encoding="utf-8") as file:
                    기억[day] = json.load(file)
            else:
                logger.warning("%s 픽스처가 없어 빈 응답으로 대신한다. 찾은 자리는 %s", day, path)
                기억[day] = {"articles": []}
        return 기억[day]

    def fetch(query, start_utc, end_utc, maxrecords=gdelt.DEFAULT_MAXRECORDS, sleep_seconds=0):
        day = date_str or _kst_date_of_window(start_utc)
        payload = _읽기(day)
        items = [
            item
            for item in (payload.get("articles") or [])
            if _within(item.get("seendate"), start_utc, end_utc)
        ]
        logger.info(
            "픽스처에서 %s 구간 %s ~ %s 기사 %d건을 꺼냈다",
            day, start_utc, end_utc, len(items),
        )
        return {"articles": items[: int(maxrecords)]}

    return fetch


def _collect_window(
    query,
    start_utc,
    end_utc,
    maxrecords,
    sleep_seconds,
    depth,
    max_depth,
    fetcher,
    state,
):
    """구간 하나를 받아 온다. 상한에 걸리면 절반으로 쪼개 다시 부른다.

    GDELT 가 다시 부르기까지 다 써도 막으면 그 구간만 접고 나머지는 계속 받는다.
    한 구간 때문에 하루치를 통째로 버리면 다음 실행이 하루 뒤라서다.
    막힌 자리는 truncated_windows 에 남아 점검에서 걸린다.
    """
    try:
        payload = fetcher(
            query,
            start_utc,
            end_utc,
            maxrecords=maxrecords,
            sleep_seconds=sleep_seconds,
        )
    except gdelt.GdeltRateLimited as error:
        logger.warning(
            "구간 %s ~ %s 은 GDELT 가 막아 받지 못했다. 이 구간만 접고 나머지를 계속 받는다. 사유는 %s 다",
            start_utc,
            end_utc,
            error,
        )
        막힌기록 = {
            "start": start_utc,
            "end": end_utc,
            "depth": depth,
            "count": 0,
            "reason": "GDELT 가 막아 이 구간을 받지 못했다. 사유는 %s 다" % (error,),
            "blocked": True,
        }
        state["blocked"].append(error)
        state["truncated"].append(막힌기록)
        state["windows"].append(
            {
                "start": start_utc,
                "end": end_utc,
                "depth": depth,
                "count": 0,
                "hit_cap": False,
                "split": False,
                "blocked": True,
            }
        )
        return

    raw_articles = []
    if isinstance(payload, dict):
        raw_articles = payload.get("articles") or []
    count = len(raw_articles)

    응답기록 = {
        "start": start_utc,
        "end": end_utc,
        "depth": depth,
        "count": count,
        "payload": payload,
    }
    state["responses"].append(응답기록)
    # 받은 즉시 이어 쓴다. 뒤쪽 구간에서 끊겨도 여기까지는 남는다.
    _append_partial(state.get("partial_path"), 응답기록)
    window = {
        "start": start_utc,
        "end": end_utc,
        "depth": depth,
        "count": count,
        "hit_cap": count >= maxrecords,
        "split": False,
        "blocked": False,
    }
    state["windows"].append(window)

    day_start, day_end = state.get("day_window") or (start_utc, end_utc)
    for item in raw_articles:
        normalized = _normalize_article(item)
        if normalized:
            if not _within(normalized.get("seendate"), day_start, day_end):
                # 하루 구간 밖 기사다. 여기서 버리지 않으면 DB 에 잘못된 날짜로
                # 박히고, store 는 url 을 기본키로 써서 나중 실행이 kst_date 를
                # 덮어쓰지 않는다. 원인을 고친 뒤에도 그 기사는 영원히 잘못된 날에
                # 매달려 daily_counts 와 주간 리포트에 섞인다. 그래서 넣기 전에
                # 거른다. 버린 건수는 점검이 보도록 결과에 담는다.
                state["stray"].append(
                    {
                        "url": normalized.get("url"),
                        "seendate": normalized.get("seendate"),
                    }
                )
                continue
            state["articles"].append(normalized)
        else:
            # 링크가 없는 항목이다. 조용히 버리면 GDELT 가 준 건수와 우리가
            # 센 건수가 갈린 이유를 나중에 되짚을 수 없어 세어 둔다.
            state["dropped_no_url"] += 1

    if count < maxrecords:
        return

    halves = _split_window(start_utc, end_utc)
    if depth >= max_depth or halves is None:
        reason = "쪼개기 깊이 한계에 닿았다" if depth >= max_depth else "더 쪼갤 수 없는 구간이다"
        logger.warning(
            "구간 %s ~ %s 이 상한 %d건에 걸렸는데 %s. 뒤쪽 기사를 놓쳤을 수 있다.",
            start_utc,
            end_utc,
            maxrecords,
            reason,
        )
        state["truncated"].append(
            {
                "start": start_utc,
                "end": end_utc,
                "depth": depth,
                "count": count,
                "reason": reason,
            }
        )
        return

    window["split"] = True
    logger.info(
        "구간 %s ~ %s 이 상한에 걸려 절반으로 쪼갠다. 현재 깊이 %d",
        start_utc,
        end_utc,
        depth,
    )
    for half_start, half_end in halves:
        _collect_window(
            query,
            half_start,
            half_end,
            maxrecords,
            sleep_seconds,
            depth + 1,
            max_depth,
            fetcher,
            state,
        )


def partial_path(raw_dir, date_str):
    """구간을 받을 때마다 이어 쓰는 파일 자리."""
    return Path(raw_dir) / ("%s.partial.jsonl" % date_str)


def _start_partial(raw_dir, date_str):
    """이어 쓰기 파일을 새로 비운다. 자리를 만들지 못하면 None 을 돌려준다."""
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = partial_path(raw_dir, date_str)
        path.write_text("", encoding="utf-8")
        return path
    except OSError as error:
        logger.warning("구간별 이어 쓰기 파일을 만들지 못했다. 사유는 %s 다", error)
        return None


def _append_partial(path, record):
    """구간 하나를 받은 즉시 한 줄로 이어 쓴다.

    하루치를 다 받은 뒤에야 파일을 쓰면, 중간에 끊길 때 이미 받아 둔 응답이
    전부 사라진다. 5초 간격을 지켜 가며 몇 분을 들인 호출이 헛일이 되는 셈이다.
    이어 쓰기가 어긋나도 수집 자체를 멈추지는 않는다. 이 파일은 되짚기용이다.
    """
    if path is None:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as error:
        logger.warning("구간 응답을 이어 쓰지 못했다. 사유는 %s 다", error)


def _save_raw(raw_dir, date_str, query, window, state, dedupe=None):
    """받은 응답을 그대로 파일에 남긴다. 나중에 숫자를 되짚을 때 쓴다."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / ("%s.json" % date_str)
    document = {
        "date": date_str,
        "query": query,
        "window": {"start": window[0], "end": window[1]},
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windows": state["windows"],
        "truncated_windows": state["truncated"],
        "dedupe": dedupe or {},
        "responses": state["responses"],
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(document, file, ensure_ascii=False, indent=2)

    # 온전한 파일을 남겼으니 이어 쓰던 파일은 치운다.
    남은것 = state.get("partial_path")
    if 남은것:
        try:
            Path(남은것).unlink()
        except OSError:
            pass
    return path


def collect_day(date_str, cfg, fetcher=None):
    """한국 날짜 하루치 기사를 모아 돌려준다.

    fetcher 를 넣으면 그것으로 부른다. 테스트에서 저장해 둔 응답을 물려 주려고
    열어 둔 자리다. 넣지 않으면 gdelt.fetch_artlist 로 실제 API 를 부른다.

    하루 구간 밖 시각을 들고 온 기사는 여기서 버린다. 넘겨 주면 DB 에 잘못된
    날짜로 박히고 store 가 그 날짜를 덮어쓰지 않아 되돌릴 길이 없다. 버린 건수는
    stray_dropped 에, 어느 기사였는지는 stray_samples 에 담아 점검이 보게 한다.
    """
    cfg = cfg or {}
    fetcher = fetcher or gdelt.fetch_artlist

    window_fn = getattr(config, "kst_day_window", None)
    if window_fn is None:
        raise RuntimeError(
            "config.kst_day_window 을 찾지 못했다. 한국 날짜를 UTC 구간으로 바꿀 수 없다."
        )
    start_utc, end_utc = window_fn(date_str)

    query = _setting(cfg, ("query", "gdelt_query", "search_query"), DEFAULT_QUERY)
    maxrecords = int(
        _setting(cfg, ("maxrecords", "max_records"), gdelt.DEFAULT_MAXRECORDS)
    )
    sleep_seconds = _setting(
        cfg,
        ("sleep_seconds", "request_sleep_seconds", "sleep"),
        gdelt.DEFAULT_SLEEP_SECONDS,
    )
    요청한깊이 = int(
        _setting(
            cfg,
            ("max_split_depth", "split_max_depth", "max_depth"),
            DEFAULT_MAX_SPLIT_DEPTH,
        )
    )
    max_depth = max(0, min(요청한깊이, MAX_SPLIT_DEPTH_LIMIT))
    if 요청한깊이 > MAX_SPLIT_DEPTH_LIMIT:
        logger.warning(
            "max_split_depth 를 %d 로 받았지만 %d 로 끊는다. 깊이가 하나 늘 때마다 "
            "최악의 호출 수가 배로 늘고 구간마다 %g초를 쉬어서, 더 내려가면 작업 "
            "시간 제한에 걸려 산출물도 실패 알림도 남지 않는다. 그래도 상한에 걸린 "
            "구간이 남으면 깊이를 올리는 대신 검색어를 좁히거나 하루를 나눠 두 번 돌려야 한다",
            요청한깊이,
            MAX_SPLIT_DEPTH_LIMIT,
            float(sleep_seconds or 0),
        )
    elif 요청한깊이 < 0:
        logger.warning("max_split_depth 가 %d 라 쪼개지 않는다", 요청한깊이)

    logger.info("%s 하루치를 모은다. UTC 구간 %s ~ %s", date_str, start_utc, end_utc)

    raw_dir = _raw_dir(cfg)
    state = {
        "articles": [],
        "windows": [],
        "truncated": [],
        "responses": [],
        "blocked": [],
        "dropped_no_url": 0,
        "stray": [],
        "day_window": (start_utc, end_utc),
        "partial_path": _start_partial(raw_dir, date_str),
    }
    _collect_window(
        query,
        start_utc,
        end_utc,
        maxrecords,
        sleep_seconds,
        0,
        max_depth,
        fetcher,
        state,
    )

    if state["blocked"] and not state["responses"]:
        # 한 구간도 받지 못했다. 이때 빈 하루로 넘기면 브리핑이 '조용한 날' 이라고
        # 거짓을 말한다. 사람이 읽을 한 줄로 바꿔 주는 cli 까지 올려 보낸다.
        raise state["blocked"][0]

    articles, duplicated, merged = _dedupe(state["articles"])
    articles.sort(key=_sort_key, reverse=True)

    raw_path = _save_raw(
        raw_dir,
        date_str,
        query,
        (start_utc, end_utc),
        state,
        dedupe={
            "duplicated": duplicated,
            "merged_distinct_urls": merged,
            "kept": len(articles),
            "dropped_no_url": state["dropped_no_url"],
            "stray_dropped": len(state["stray"]),
        },
    )

    logger.info(
        "%s 수집을 마쳤다. 구간 %d개, 기사 %d건, 겹친 링크 %d건, 상한에 걸린 구간 %d개",
        date_str,
        len(state["windows"]),
        len(articles),
        duplicated,
        len(state["truncated"]),
    )
    if state["dropped_no_url"]:
        logger.warning(
            "링크가 없어 버린 항목이 %d건이다. GDELT 응답 건수와 우리가 센 건수가 그만큼 갈린다",
            state["dropped_no_url"],
        )
    if state["stray"]:
        logger.warning(
            "하루 구간 밖 기사 %d건을 넣기 전에 버렸다. 구간은 %s ~ %s 다. 한 건이라도 "
            "DB 에 들어가면 잘못된 날짜가 영원히 남는다",
            len(state["stray"]),
            start_utc,
            end_utc,
        )
    if merged:
        # 구간을 쪼개 같은 기사를 두 번 받는 것과는 다른 일이다. 링크 글자가
        # 다른데 한 건이 됐다는 것은 normalize_url 이 살려야 할 파라미터를
        # 버렸다는 신호일 수 있어 따로 남긴다.
        logger.warning(
            "겹친 링크 %d건 가운데 %d건은 링크 글자가 서로 달랐다. "
            "서로 다른 기사를 한 건으로 합쳤는지 -v 로 다시 돌려 살펴야 한다",
            duplicated,
            merged,
        )
    if not articles:
        logger.warning("%s 에 모인 기사가 한 건도 없다. 검색어나 구간을 살펴야 한다.", date_str)

    return {
        "date": date_str,
        "articles": articles,
        "windows": state["windows"],
        "truncated_windows": state["truncated"],
        "raw_path": str(raw_path),
        # 아래 세 숫자는 점검 쪽에서 쓴다. _dedupe 가 이미 지운 뒤라 DB 만
        # 보면 링크가 몇 건 합쳐졌는지 알 길이 없어서 수집기가 들고 나온다.
        "duplicated": duplicated,
        "merged_distinct_urls": merged,
        "dropped_no_url": state["dropped_no_url"],
        # 하루 구간 밖이라 넣기 전에 버린 기사다. 점검이 이 숫자를 비율로 보고
        # 경계 계산이 어긋난 것인지 반올림에 걸린 한두 건인지 가른다.
        "stray_dropped": len(state["stray"]),
        "stray_samples": [
            "%s(%s)" % (기록.get("url"), 기록.get("seendate")) for 기록 in state["stray"][:5]
        ],
        "blocked_windows": [기록 for 기록 in state["truncated"] if 기록.get("blocked")],
    }
