"""월요일 주간 리포트를 만든다.

지난 한 주(월요일부터 일요일까지) 동안 DB에 쌓인 AI 판단 결과와 키워드 집계를
읽어서 마크다운 한 장과 가로 막대 차트 한 장을 만든다.

숫자는 _build_rows 에서 한 번만 계산하고, 표와 문장과 차트가 모두 그 값을 그대로
쓴다. 같은 주를 다시 돌려도 결과가 흔들리지 않도록 정렬 기준에 동점 처리까지
넣어 두었다. 만든 날짜를 본문에 적지 않는 것도 같은 뜻이다. 같은 입력이면 언제
돌려도 파일이 같아야 커밋에 뜻 없는 변경이 쌓이지 않는다.

이미 있는 리포트에 사람이 채운 코멘트가 있으면 읽어서 그대로 옮겨 붙인다.
줄표와 이모지는 브리핑과 같은 잣대로 걸러내고, 출처는 한계 문단 안에 섞지 않고
맨 아래 따로 한 줄 둔다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from .collect import tidy_url
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# 기본값들
DEFAULT_TOP_N = 5
DEFAULT_FONT_CANDIDATES = (
    "AppleGothic",
    "Apple SD Gothic Neo",
    "NanumGothic",
    "NanumBarunGothic",
    "Malgun Gothic",
)
FALLBACK_FONT = "DejaVu Sans"
CHART_FILE_NAME = "keywords.png"
REPORT_FILE_NAME = "report.md"

# 사람이 코멘트를 채우는 절 제목. 다시 만들 때 이 아래 인용 블록을 옮겨 붙인다
HUMAN_SECTION = "사람 코멘트"

# 줄표류와 이모지는 쓰지 않는다. brief._text 와 judge._글다듬기 와 같은 잣대다.
# 판단이 DB 에 넣은 값이 이미 깨끗하더라도, 예전 판에서 남은 줄이 있을 수 있어
# 리포트도 한 번 더 훑는다
_DASHES = ("—", "–", "―", "‒", "−")
# 붙임표류는 쉼표가 아니라 아스키 붙임표로 내린다. 'GPT‑5' 는 'GPT-5' 가 맞다.
_HYPHENS = ("‐", "‑")
_EMOJI_RE = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿™ℹ️‍]")
_SPACES_RE = re.compile(r"\s+")

# 설정 파일에서 키워드를 읽을 때 받아 줄 열쇠 이름들
_KEYWORD_CONTAINER_KEYS = ("keywords", "keyword_list", "watch_keywords")
_NAME_KEYS = ("keyword", "name", "label", "term", "key", "word")
_ROMAN_KEYS = ("roman", "latin", "ascii", "en", "english", "slug", "chart_label")


# 로마자 한 글자를 우리말로 읽었을 때 받침으로 끝나는지. 조사를 고를 때 쓴다
_LETTERS_WITH_FINAL = set("flmnrsxFLMNRSX")
# 숫자 한 글자를 우리말로 읽었을 때 받침으로 끝나는지. 영 일 삼 육 칠 팔
_DIGITS_WITH_FINAL = set("013678")
# 받침이 ㄹ 인 것들. 으로/로 는 ㄹ 받침에서 '로' 를 쓴다
_RIEUL_LETTERS = set("lrLR")  # 엘, 아르
_RIEUL_DIGITS = set("178")  # 일, 칠, 팔
_RIEUL_JONGSEONG = 8  # 한글 종성 표에서 ㄹ 자리다


def _final_sound(word):
    """앞말의 끝소리를 본다. (받침이 있나, 그 받침이 ㄹ 인가) 를 돌려준다."""
    if not word:
        return False, False
    last = word[-1]
    if "가" <= last <= "힣":
        jong = (ord(last) - 0xAC00) % 28
        return jong != 0, jong == _RIEUL_JONGSEONG
    if last.isdigit():
        return last in _DIGITS_WITH_FINAL, last in _RIEUL_DIGITS
    if last.isalpha() and last.isascii():
        return last in _LETTERS_WITH_FINAL, last in _RIEUL_LETTERS
    return False, False


def _josa(word, with_final, without_final):
    """앞말 받침에 맞는 조사를 고른다.

    한글이면 종성이 있는지 보고, 로마자나 숫자로 끝나면 읽는 소리를 따진다.
    키워드에 오픈AI 처럼 로마자가 섞여 들어와도 문장이 어색해지지 않게 한다.

    으로/로 는 받침 규칙에 예외가 하나 있다. 받침이 ㄹ 이면 '로' 를 쓴다.
    구글으로가 아니라 구글로, 애플으로가 아니라 애플로가 맞다.
    """
    if not word:
        return without_final
    has_final, is_rieul = _final_sound(word)
    if is_rieul and with_final == "으로":
        return without_final
    return with_final if has_final else without_final


def _clean_text(value):
    """줄표를 쉼표로 바꾸고 이모지를 떼어 낸다. 브리핑과 같은 잣대다."""
    if value is None:
        return ""
    text = str(value)
    for dash in _DASHES:
        text = text.replace(dash, ", ")
    for hyphen in _HYPHENS:
        text = text.replace(hyphen, "-")
    text = _EMOJI_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    text = text.replace(" ,", ",").replace(",,", ",")
    return text.strip().strip(",").strip()


# ---------------------------------------------------------------------------
# 날짜 다루기
# ---------------------------------------------------------------------------


def _kst_today():
    """한국 시간 기준 오늘 날짜를 돌려준다."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Seoul")
    except Exception:  # tzdata 가 없는 환경이면 고정 오프셋으로 대신한다
        tz = timezone(timedelta(hours=9))
    return datetime.now(tz).date()


def _parse_date(value):
    """'YYYY-MM-DD' 문자열이나 date 객체를 date 로 바꾼다."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            "주 시작일은 'YYYY-MM-DD' 꼴이어야 한다. 받은 값은 다음과 같다. %r" % (value,)
        ) from exc


def _week_bounds(week_start):
    """주 시작일을 받아 그 주의 월요일과 일요일을 돌려준다."""
    start = _parse_date(week_start)
    if start.weekday() != 0:
        snapped = start - timedelta(days=start.weekday())
        LOGGER.warning(
            "주 시작일 %s는 월요일이 아니다. 같은 주 월요일인 %s로 맞춰서 만든다.",
            start.isoformat(),
            snapped.isoformat(),
        )
        start = snapped
    return start, start + timedelta(days=6)


def _date_strings(start, end):
    """시작일부터 끝일까지 날짜 문자열 목록을 만든다."""
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------


def _keyword_entries(cfg):
    """설정에서 키워드 목록을 뽑아 {name, roman} 목록으로 고른다.

    설정 파일 모양이 문자열 목록이든, 사전 목록이든, 키워드를 열쇠로 쓴 사전이든
    받아 준다. 로마자 표기가 있으면 차트 폰트가 없을 때 축 라벨로 쓴다.
    """
    raw = None
    for key in _KEYWORD_CONTAINER_KEYS:
        value = cfg.get(key)
        if value:
            raw = value
            break

    if isinstance(raw, dict):
        inner = None
        for key in ("keywords", "list", "items"):
            if raw.get(key):
                inner = raw[key]
                break
        raw = inner if inner is not None else list(raw.keys())
    if isinstance(raw, dict):
        raw = list(raw.keys())

    if not isinstance(raw, (list, tuple)):
        if raw:
            LOGGER.warning("설정의 키워드 모양을 알아보지 못했다. 키워드 없이 만든다.")
        return []

    entries = []
    seen = set()
    for item in raw:
        name = None
        roman = None
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            for key in _NAME_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
            for key in _ROMAN_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip() and value.isascii():
                    roman = value.strip()
                    break
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "roman": roman})
    return entries


def _report_cfg(cfg):
    value = cfg.get("report")
    return value if isinstance(value, dict) else {}


def _top_n(cfg):
    """표와 차트에 올릴 키워드 개수. 기본은 다섯이다."""
    for source in (_report_cfg(cfg), cfg):
        for key in ("top_keywords", "top_n"):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return DEFAULT_TOP_N


def _font_candidates(cfg):
    """한글 폰트 후보. 설정으로 갈아 끼울 수 있게 해 두었다."""
    for source in (_report_cfg(cfg), cfg):
        value = source.get("font_candidates")
        if isinstance(value, (list, tuple)) and value:
            return [str(name) for name in value]
    return list(DEFAULT_FONT_CANDIDATES)


def _project_root(cfg):
    """산출물을 떨어뜨릴 프로젝트 뿌리를 정한다."""
    paths = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
    for source in (cfg, paths):
        for key in ("root", "project_root", "base_dir"):
            value = source.get(key)
            if value:
                return Path(str(value))
    return Path(__file__).resolve().parent.parent


def _chart_path(cfg, week_key):
    """차트를 저장할 자리는 data/reports/<주시작일>/keywords.png 다."""
    paths = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
    base = None
    for source in (_report_cfg(cfg), cfg, paths):
        value = source.get("reports_dir")
        if value:
            base = Path(str(value))
            break
    if base is None:
        base = _project_root(cfg) / "data" / "reports"
    return base / week_key / CHART_FILE_NAME


# ---------------------------------------------------------------------------
# DB 에서 값 가져오기
# ---------------------------------------------------------------------------


def _default_counts_fn(conn, start_date, end_date, keywords):
    from . import store

    return store.keyword_counts(conn, start_date, end_date, keywords)


def _default_decisions_loader(conn, date_str):
    from . import judge

    return judge.load_decisions(conn, date_str)


def _keyword_counts(conn, start, end, names, counts_fn):
    """키워드별 기사 수를 받아 온다. 못 받으면 빈 사전과 False 를 돌려준다."""
    if not names:
        return {}, True
    fn = counts_fn or _default_counts_fn
    try:
        result = fn(conn, start.isoformat(), end.isoformat(), list(names))
    except (ImportError, AttributeError, NotImplementedError, sqlite3.Error) as exc:
        LOGGER.error(
            "%s부터 %s까지 키워드 집계를 못 받았다. 오류 내용은 다음과 같다. %s",
            start.isoformat(),
            end.isoformat(),
            exc,
        )
        return {}, False
    if not isinstance(result, dict):
        LOGGER.error(
            "키워드 집계가 사전이 아니다. 받은 자료형은 다음과 같다. %s",
            type(result).__name__,
        )
        return {}, False
    counts = {}
    for name in names:
        try:
            counts[name] = int(result.get(name, 0) or 0)
        except (TypeError, ValueError):
            counts[name] = 0
    return counts, True


def _item_url(item):
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _collect_groups(conn, day_strings, loader, today=None):
    """하루치 판단 기록을 모아 도구 묶음과 규제 묶음, 남긴 기사 수를 센다.

    today 를 주면 그보다 뒤 날짜는 빈 날로 세지 않고 upcoming 에 따로 담는다.
    주 중간에 리포트를 만들면 아직 오지 않은 날이 결함처럼 보이는 줄로 들어가
    읽는 사람이 수집이 빠진 것으로 오해한다.
    """
    fn = loader or _default_decisions_loader
    tools = {}
    policies = {}
    kept_urls = set()
    missing = []
    upcoming = []
    today = _parse_date(today) if today is not None else None

    for day in day_strings:
        if today is not None and _parse_date(day) > today:
            upcoming.append(day)
            continue
        try:
            decisions = fn(conn, day)
        except (ImportError, AttributeError, NotImplementedError, sqlite3.Error) as exc:
            LOGGER.warning("%s의 판단 기록을 못 읽었다. 오류 내용은 다음과 같다. %s", day, exc)
            missing.append(day)
            continue
        if not decisions:
            missing.append(day)
            continue

        for item in decisions.get("kept") or []:
            url = _item_url(item)
            if url:
                kept_urls.add(url)

        for group in decisions.get("groups") or []:
            if not isinstance(group, dict):
                continue
            kind = str(group.get("kind") or "other").strip().lower()
            if kind == "tool":
                bucket = tools
            elif kind == "policy":
                bucket = policies
            else:
                continue
            headline = _clean_text(group.get("headline"))
            if not headline:
                continue
            urls = [u for u in (_item_url(u) for u in (group.get("urls") or [])) if u]
            key = headline.casefold()
            row = bucket.get(key)
            if row is None:
                bucket[key] = {
                    "date": day,
                    "headline": headline,
                    "urls": list(dict.fromkeys(urls)),
                }
            else:
                # 같은 묶음이 며칠에 걸쳐 나오면 처음 날짜를 남기고 링크만 합친다
                merged = row["urls"] + [u for u in urls if u not in row["urls"]]
                row["urls"] = merged

    return {
        "tools": list(tools.values()),
        "policies": list(policies.values()),
        "kept": len(kept_urls),
        "missing": missing,
        "upcoming": upcoming,
    }


# ---------------------------------------------------------------------------
# 표 만들기
# ---------------------------------------------------------------------------


def _delta_text(delta):
    if delta > 0:
        return "+%d" % delta
    if delta < 0:
        return "%d" % delta
    return "0"


def _row_note(this_week, last_week):
    if last_week == 0 and this_week > 0:
        return "지난주에는 없었다"
    if this_week == 0 and last_week > 0:
        return "이번 주에는 안 나왔다"
    return ""


def _build_rows(entries, counts_this, counts_prev, top_n):
    """키워드 표를 한 번에 만든다. 이 목록이 표와 문장과 차트의 유일한 출처다."""
    rows = []
    for entry in entries:
        name = entry["name"]
        this_week = int(counts_this.get(name, 0) or 0)
        last_week = int(counts_prev.get(name, 0) or 0)
        if this_week == 0 and last_week == 0:
            continue
        delta = this_week - last_week
        rows.append(
            {
                "keyword": name,
                "roman": entry.get("roman"),
                "chart_label": name,
                "this_week": this_week,
                "last_week": last_week,
                "delta": delta,
                "delta_text": _delta_text(delta),
                "note": _row_note(this_week, last_week),
            }
        )
    # 이번 주 건수가 같으면 지난주 건수로, 그것도 같으면 이름 순으로 갈라 놓는다
    rows.sort(key=lambda row: (-row["this_week"], -row["last_week"], row["keyword"]))
    return rows[:top_n]


# ---------------------------------------------------------------------------
# 차트 그리기
# ---------------------------------------------------------------------------


def _resolve_font(font_manager, candidates):
    """한글 폰트를 찾는다. 없으면 DejaVu Sans 로 떨어지고 False 를 함께 돌려준다."""
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            return name, True
    LOGGER.warning(
        "한글 폰트를 못 찾았다. %s로 그리고 축 라벨은 로마자로 바꾼다. 찾아본 후보는 다음과 같다. %s",
        FALLBACK_FONT,
        ", ".join(candidates) if candidates else "없음",
    )
    return FALLBACK_FONT, False


def _ascii_label(row, index):
    """폰트가 없을 때 쓸 로마자 축 라벨."""
    roman = row.get("roman")
    if roman:
        return roman
    keyword = row["keyword"]
    if keyword.isascii():
        return keyword
    return "KW%d" % (index + 1)


def _draw_chart(rows, chart_path, cfg):
    """상위 키워드의 이번 주와 지난주 건수를 가로 막대로 그린다."""
    if not rows:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager, ticker
        import matplotlib.pyplot as plt
    except Exception as exc:  # matplotlib 이 없으면 차트만 건너뛴다
        LOGGER.warning("matplotlib 을 쓸 수 없어 차트를 건너뛴다. 오류 내용은 다음과 같다. %s", exc)
        return None

    font_name, korean_ok = _resolve_font(font_manager, _font_candidates(cfg))
    if not korean_ok:
        for index, row in enumerate(rows):
            row["chart_label"] = _ascii_label(row, index)

    if korean_ok:
        title = "많이 나온 키워드 %d개" % len(rows)
        xlabel = "기사 수"
        this_label = "이번 주"
        last_label = "지난주"
    else:
        title = "Top %d keywords, this week vs last week" % len(rows)
        xlabel = "articles"
        this_label = "this week"
        last_label = "last week"

    plt.rcParams["font.family"] = font_name
    plt.rcParams["axes.unicode_minus"] = False

    count = len(rows)
    labels = [row["chart_label"] for row in rows]
    this_values = [row["this_week"] for row in rows]
    last_values = [row["last_week"] for row in rows]
    positions = [count - 1 - index for index in range(count)]
    height = 0.38

    figure, axes = plt.subplots(figsize=(8.0, max(2.6, 0.78 * count + 1.3)))
    bars_this = axes.barh(
        [p + height / 2 for p in positions],
        this_values,
        height=height,
        label=this_label,
        color="#3c6ef0",
    )
    bars_last = axes.barh(
        [p - height / 2 for p in positions],
        last_values,
        height=height,
        label=last_label,
        color="#b9c2d6",
    )
    axes.set_yticks(positions)
    axes.set_yticklabels(labels)
    axes.set_xlabel(xlabel)
    axes.set_title(title)
    axes.legend(loc="best")  # 막대와 겹치지 않는 자리를 matplotlib 이 고른다
    axes.grid(axis="x", linestyle=":", alpha=0.5)
    axes.set_axisbelow(True)

    ceiling = max(this_values + last_values + [1])
    axes.set_xlim(0, ceiling * 1.18)
    # 기사 수는 정수라서 눈금도 정수로만 찍는다
    axes.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    for group in (bars_this, bars_last):
        for rect in group:
            width = rect.get_width()
            axes.text(
                width + ceiling * 0.015,
                rect.get_y() + rect.get_height() / 2,
                "%d" % int(width),
                va="center",
                fontsize=8,
            )

    figure.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(chart_path, dpi=144)
    plt.close(figure)
    LOGGER.info("차트를 %s에 저장했다. %s 폰트로 그렸다.", chart_path, font_name)
    return chart_path


# ---------------------------------------------------------------------------
# 마크다운 쓰기
# ---------------------------------------------------------------------------


def _group_lines(groups, empty_sentence):
    if not groups:
        return [empty_sentence]
    lines = []
    for group in sorted(groups, key=lambda item: (item["date"], item["headline"])):
        day = group["date"][5:]
        urls = group["urls"]
        if urls:
            # GDELT 가 붙여 주는 :443 같은 기본 포트는 보여 줄 때 뗀다.
            링크 = tidy_url(urls[0])
            if len(urls) == 1:
                tail = "대표 링크 %s" % 링크
            else:
                tail = "기사 %d건, 대표 링크 %s" % (len(urls), 링크)
        else:
            tail = "대표 링크가 없다"
        lines.append("- %s %s (%s)" % (day, _clean_text(group["headline"]), tail))
    return lines


def extract_human_comments(markdown):
    """이미 만들어 둔 리포트에서 사람이 채운 인용 블록만 뽑아 온다.

    비어 있는 '>' 줄은 사람이 쓴 것이 아니므로 버린다. 한 줄이라도 글자가
    있으면 그 절의 인용 블록 전체를 앞뒤 빈 줄만 털어 그대로 돌려준다.
    """
    if not markdown:
        return []
    picked = []
    inside = False
    for line in str(markdown).splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == HUMAN_SECTION
            continue
        if inside and line.lstrip().startswith(">"):
            picked.append(line.rstrip())
    while picked and picked[0].strip() == ">":
        picked.pop(0)
    while picked and picked[-1].strip() == ">":
        picked.pop()
    if not any(line.strip(" >") for line in picked):
        return []
    return picked


def _read_human_comments(path):
    """리포트 파일이 이미 있으면 거기 적힌 사람 코멘트를 읽어 온다."""
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning(
            "%s를 읽지 못해 사람 코멘트를 옮기지 못한다. 오류 내용은 다음과 같다. %s",
            path,
            exc,
        )
        return []
    comments = extract_human_comments(text)
    if comments:
        LOGGER.info("%s에 적힌 사람 코멘트 %d줄을 그대로 옮긴다.", path, len(comments))
    return comments


def _source_sentence(cfg, start, end):
    """맨 아래에 붙는 출처 한 줄. 브리핑과 같은 문장에 집계 기간을 더한다."""
    cfg = cfg if isinstance(cfg, dict) else {}
    query = ""
    for holder in (cfg, cfg.get("gdelt") if isinstance(cfg.get("gdelt"), dict) else {}):
        for key in ("query", "default_query", "base_query"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                query = value.strip()
                break
        if query:
            break
    line = (
        "자료는 GDELT DOC 2.0 에서 받았고 하루 경계는 한국 시간으로 끊었다. "
        "집계한 날짜는 %s 부터 %s 까지다." % (start, end)
    )
    if query:
        line += " 검색어는 %s 이다." % query
    line += " 제목과 링크, 매체, 시각만 들어오기 때문에 본문은 읽지 않고 제목만 보고 골랐다."
    return line


def _table_lines(rows):
    lines = ["| 키워드 | 이번 주 | 지난주 | 변화 |", "| --- | ---: | ---: | ---: |"]
    for row in rows:
        lines.append(
            "| %s | %d | %d | %s |"
            % (row["keyword"], row["this_week"], row["last_week"], row["delta_text"])
        )
    return lines


def _table_lines_this_week_only(rows):
    lines = ["| 키워드 | 이번 주 |", "| --- | ---: |"]
    for row in rows:
        lines.append("| %s | %d |" % (row["keyword"], row["this_week"]))
    return lines


def _headline_sentence(rows, compare=True):
    top = rows[0]
    first = "이번 주에 가장 많이 나온 키워드는 %s%s %d건이다." % (
        top["keyword"],
        _josa(top["keyword"], "으로", "로"),
        top["this_week"],
    )
    if not compare:
        return first
    if top["delta"] > 0:
        second = " 지난주보다 %d건 늘었다." % top["delta"]
    elif top["delta"] < 0:
        second = " 지난주보다 %d건 줄었다." % abs(top["delta"])
    else:
        second = " 지난주와 건수가 같다."
    return first + second


def _volume_sentence(this_kept, last_kept, last_has_data=True):
    if not last_has_data:
        # 지난주는 기사가 0건이었던 게 아니라 모으지 않았다. 0 과 견주면 '늘었다' 는 거짓이 된다.
        return "이번 주에 AI가 남긴 기사는 %d건이다. 지난주는 수집 기록이 없어 견주지 않는다." % this_kept
    first = "이번 주에 AI가 남긴 기사는 %d건, 지난주는 %d건이다." % (this_kept, last_kept)
    diff = this_kept - last_kept
    if diff > 0:
        second = " %d건 늘었다." % diff
    elif diff < 0:
        second = " %d건 줄었다." % abs(diff)
    else:
        second = " 지난주와 같다."
    return first + second


def _render_markdown(context):
    rows = context["rows"]
    lines = []

    lines.append("# 주간 AI 뉴스 리포트")
    lines.append("")
    # 만든 날짜는 본문에 적지 않는다. 같은 주를 다른 날 다시 돌렸을 때 내용이
    # 같은데 파일만 달라지면, 액션이 커밋할 때마다 뜻 없는 변경이 쌓인다.
    lines.append(
        "대상 기간은 한국 시간으로 %s 월요일부터 %s 일요일까지다."
        % (context["start"], context["end"])
    )
    지난주기록 = context.get("last_has_data", True)
    lines.append(_volume_sentence(context["this_kept"], context["last_kept"], 지난주기록))
    if context["missing"]:
        lines.append(
            "판단 기록이 없는 날이 %d일 있다. 빈 날짜는 다음과 같다. %s"
            % (len(context["missing"]), ", ".join(context["missing"]))
        )
    if context.get("upcoming"):
        lines.append(
            "아직 오지 않은 날이 %d일 있다. 그 날짜는 다음과 같다. %s"
            % (len(context["upcoming"]), ", ".join(context["upcoming"]))
        )
    lines.append("")

    lines.append("## 이번 주에 새로 나온 도구")
    lines.append("")
    lines.extend(
        _group_lines(context["tools"], "새 도구 소식으로 묶인 기사가 이번 주에는 없다.")
    )
    lines.append("")

    lines.append("## 규제와 정책")
    lines.append("")
    lines.extend(
        _group_lines(context["policies"], "규제나 정책으로 묶인 기사가 이번 주에는 없다.")
    )
    lines.append("")

    lines.append("## 많이 나온 키워드")
    lines.append("")
    if not context["counts_available"]:
        lines.append("키워드 집계를 불러오지 못해 표를 비워 둔다. 저장소 쪽을 확인해야 한다.")
    elif not rows:
        lines.append("이번 주에도 지난주에도 걸린 키워드가 없어서 표를 비워 둔다.")
    else:
        if 지난주기록:
            lines.extend(_table_lines(rows))
        else:
            lines.extend(_table_lines_this_week_only(rows))
        lines.append("")
        lines.append(_headline_sentence(rows, compare=지난주기록))
        if not 지난주기록:
            lines.append("지난주는 수집 기록이 없어 늘고 줄어든 것은 적지 않는다.")
        fresh = [row["keyword"] for row in rows if 지난주기록 and row["note"] == "지난주에는 없었다"]
        gone = [row["keyword"] for row in rows if 지난주기록 and row["note"] == "이번 주에는 안 나왔다"]
        if fresh:
            lines.append(
                "지난주에 없다가 이번 주에 올라온 키워드는 %s%s."
                % (", ".join(fresh), _josa(fresh[-1], "이다", "다"))
            )
        if gone:
            lines.append(
                "지난주에 있다가 이번 주에 빠진 키워드는 %s%s."
                % (", ".join(gone), _josa(gone[-1], "이다", "다"))
            )
    lines.append("")

    lines.append("## 차트")
    lines.append("")
    if context["chart_relative"]:
        lines.append(
            "![상위 키워드의 이번 주와 지난주 건수](%s)" % context["chart_relative"]
        )
        # 로마자 라벨에는 조사를 붙이지 않는다. Google는, Samsung는 처럼
        # 낱자 이름으로 읽는 규칙이 낱말에는 맞지 않아 거의 다 틀린다.
        # 맺음 조사는 표의 마지막 줄이 아니라 이 목록의 마지막 이름으로 고른다.
        mapping = [
            (row["chart_label"], row["keyword"])
            for row in rows
            if row["chart_label"] != row["keyword"]
        ]
        if mapping:
            lines.append("")
            lines.append(
                "차트 축 라벨은 한글 폰트가 없어 로마자로 적었다. %s%s."
                % (
                    ", ".join("%s = %s" % (label, name) for label, name in mapping),
                    _josa(mapping[-1][1], "이다", "다"),
                )
            )
    else:
        lines.append("그릴 숫자가 없어서 이번 주 차트는 만들지 않았다.")
    lines.append("")

    lines.append("## " + HUMAN_SECTION)
    lines.append("")
    comments = context.get("comments") or []
    if comments:
        # 같은 주를 다시 만들 때 사람이 채운 문장을 지우지 않는다.
        lines.append("아래 인용 블록은 앞서 만든 리포트에 적힌 것을 그대로 옮겼다.")
        lines.append("")
        lines.extend(comments)
    else:
        lines.append("아래 인용 블록은 비워 둔다. 리포트를 읽고 직접 채운다.")
        lines.append("")
        lines.append(">")
        lines.append(">")
    lines.append("")

    lines.append("## 한계")
    lines.append("")
    lines.append(
        "GDELT 는 색인해 둔 매체만 훑는다. 국내 매체 가운데 빠진 곳이 있어서, "
        "여기 없는 기사가 실제로 없었다는 뜻은 아니다."
    )
    lines.append(
        "한 번에 받아 오는 기사는 250건이 상한이다. 상한에 걸린 구간은 시간을 쪼개 "
        "다시 받지만, 쪼갠 경계에서 빠지는 기사가 있을 수 있다."
    )
    lines.append(
        "분류와 묶기는 제목만 보고 한다. 본문을 읽지 않으니 제목이 모호한 기사는 "
        "엉뚱한 묶음에 들어가기도 한다."
    )
    lines.append(
        "키워드 건수는 제목에 그 말이 들어갔는지로 센다. 한 기사가 키워드 여러 개에 "
        "겹쳐 잡히므로 표의 숫자를 다 더해도 기사 수와 맞지 않는다."
    )
    lines.append(
        "표에 오른 기사 가운데 몇 건은 대표 링크를 눌러 원본 제목과 날짜를 직접 맞춰 "
        "본 뒤 코멘트에 적는다."
    )
    lines.append("")

    # 출처는 한계 문단 안에 섞지 않고 브리핑처럼 맨 아래 한 줄로 따로 둔다.
    # 리포트만 따로 떼어 보아도 자료가 어디서 왔는지 바로 보이게 하려는 것이다.
    lines.append("## 출처")
    lines.append("")
    lines.append(context["source"])
    lines.append("")

    return "\n".join(_clean_text_lines(lines))


def _clean_text_lines(lines):
    """마크다운 줄에서 줄표와 이모지를 걷어 낸다. 빈 줄과 들여쓰기는 그대로 둔다.

    묶음 줄은 이미 한 줄씩 훑었지만, 예전 판이 DB 에 남긴 값이나 사람이 옮겨 온
    코멘트에 줄표가 섞여 있을 수 있어 마지막에 한 번 더 본다.
    """
    cleaned = []
    for line in lines:
        if not line.strip():
            cleaned.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        cleaned.append(indent + _clean_text(line))
    return cleaned


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------


def build_report(week_start, conn, cfg, decisions_loader=None, counts_fn=None):
    """주 시작일(월요일)을 받아 그 주 월요일부터 일요일까지의 리포트를 만든다.

    돌려주는 것은 {markdown, chart_path, table} 이다. chart_path 는 저장한 차트의
    절대 경로이고, 그릴 숫자가 없으면 None 이다. table 은 키워드 표의 행 목록이라
    마크다운 안의 숫자와 항상 같다.

    decisions_loader 와 counts_fn 은 시험용으로 갈아 끼우는 자리다. 비워 두면
    judge.load_decisions 와 store.keyword_counts 를 쓴다.

    같은 주를 다시 만들면 이미 있는 report.md 의 사람 코멘트를 읽어 옮겨 붙인다.
    만든 날짜는 본문에 적지 않으므로, 같은 입력이면 언제 돌려도 같은 글이 나온다.
    """
    cfg = cfg if isinstance(cfg, dict) else {}

    start_date, end_date = _week_bounds(week_start)
    prev_start = start_date - timedelta(days=7)
    prev_end = start_date - timedelta(days=1)
    week_key = start_date.isoformat()

    LOGGER.info(
        "%s부터 %s까지 주간 리포트를 만든다. 견줄 지난주는 %s부터 %s까지다.",
        week_key,
        end_date.isoformat(),
        prev_start.isoformat(),
        prev_end.isoformat(),
    )

    entries = _keyword_entries(cfg)
    names = [entry["name"] for entry in entries]
    counts_this, ok_this = _keyword_counts(conn, start_date, end_date, names, counts_fn)
    counts_prev, ok_prev = _keyword_counts(conn, prev_start, prev_end, names, counts_fn)
    counts_available = ok_this and ok_prev

    rows = _build_rows(entries, counts_this, counts_prev, _top_n(cfg))

    today = _kst_today()
    this_week = _collect_groups(
        conn, _date_strings(start_date, end_date), decisions_loader, today
    )
    last_week = _collect_groups(
        conn, _date_strings(prev_start, prev_end), decisions_loader, today
    )

    chart_path = _draw_chart(rows, _chart_path(cfg, week_key), cfg)

    # 같은 주를 다시 만들 때 사람이 채운 코멘트를 지우지 않는다. 리포트 파일은
    # 차트와 같은 폴더에 떨어지므로 그 자리를 그대로 보고 읽어 온다.
    report_file = _chart_path(cfg, week_key).parent / REPORT_FILE_NAME
    comments = _read_human_comments(report_file)

    LOGGER.info("%s 주 리포트를 %s에 만들었다.", week_key, today.isoformat())

    markdown = _render_markdown(
        {
            "start": week_key,
            "end": end_date.isoformat(),
            "rows": rows,
            "counts_available": counts_available,
            "tools": this_week["tools"],
            "policies": this_week["policies"],
            "this_kept": this_week["kept"],
            "last_kept": last_week["kept"],
            # 지난주 이레 가운데 판단 기록이 남은 날이 하루라도 있어야 견준다.
            "last_has_data": (len(last_week["missing"]) + len(last_week["upcoming"]) < 7)
            or any(int(v or 0) > 0 for v in (counts_prev or {}).values()),
            "missing": this_week["missing"],
            "upcoming": this_week["upcoming"],
            "comments": comments,
            "source": _source_sentence(cfg, week_key, end_date.isoformat()),
            "chart_relative": CHART_FILE_NAME if chart_path else None,
        }
    )

    return {
        "markdown": markdown,
        "chart_path": str(chart_path) if chart_path else None,
        "table": rows,
    }
