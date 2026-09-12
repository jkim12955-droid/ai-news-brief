"""하루치 브리핑 문장을 만든다.

받아오기와 세기, 묶기는 다른 모듈이 끝낸 뒤라고 보고, 여기서는 그 결과를 사람이 읽는
마크다운과 슬랙 블록으로 옮기는 일만 한다. 그래서 이 파일은 다른 프로젝트 모듈을
가져오지 않는다. 같은 입력을 넣으면 언제 돌려도 같은 글이 나와야 하므로
정렬과 자르기는 모두 정해진 순서를 따른다.

묶음 개수와 키워드 개수의 한계는 설정에서 읽는다. 상수만 보고 있으면 설정 파일의
값을 고쳐도 결과가 달라지지 않아 설명이 사실과 어긋난다. 한계에 걸려 잘라낸 것이
있으면 몇 개를 잘랐는지 브리핑에 한 줄 적는다.

바깥에 내놓는 함수
    build_brief(date_str, judged, stats, cfg)
        -> {markdown, slack_blocks, summary_line, group_total, group_shown}
    save_brief(markdown, path) -> 저장한 파일 경로
    default_brief_path(root, date_str) -> data/briefs/<날짜>.md 경로
"""

import json
import os
import re
from datetime import date
from urllib.parse import urlsplit

# 설정에 값이 없을 때 쓰는 기본값이다. 설정의 brief.max_groups 와 brief.min_groups,
# brief.max_keywords 가 있으면 그쪽이 이긴다. 상수만 보고 있으면 설정 파일의
# 값을 고쳐도 결과가 달라지지 않아 설명이 사실과 어긋난다.
# 대표 묶음과 정책 묶음까지 모두 합쳐 이 수를 넘기지 않는다.
MAX_GROUPS = 5
# 묶음이 이 수보다 적으면 브리핑에 그 사실을 한 줄 적는다.
MIN_GROUPS = 3
# 처음 보는 키워드도 한 줄에 담을 만큼만 적는다.
MAX_KEYWORDS = 8

_WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")

# 줄표류는 효준님 취향대로 쓰지 않는다. 제목에 섞여 들어오면 쉼표로 바꾼다.
_DASHES = ("—", "–", "―", "‒", "−")
# 붙임표류(U+2010, U+2011)는 쉼표로 바꾸지 않고 아스키 붙임표로 내린다.
# 'GPT‑5' 를 'GPT, 5' 로 만들면 뜻이 달라진다. 생김새만 줄표를 닮은 글자이니
# 모양만 아스키로 맞춘다. brief, judge, report 세 곳이 같은 값을 쓴다.
_HYPHENS = ("‐", "‑")
# 이모지도 쓰지 않는다. 제목에 붙어 오면 떼어 낸다.
# U+2122(™)와 U+2139(ℹ)는 범위 밖에 홀로 있어 따로 적어 준다. 화살표나 ▶ 처럼
# 한국어 제목에서 뜻을 지고 오는 기호는 건드리지 않는다.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿™ℹ️‍]")
_SPACES = re.compile(r"\s+")

_COUNT_KEYS = ("article_count", "articles_count", "count", "total", "total_for_date", "articles")
_AVG_KEYS = ("avg_7d", "average_7d", "avg7", "avg_recent_7d", "recent_avg", "weekly_avg", "avg")
_KEYWORD_KEYS = ("first_seen_keywords", "first_seen", "new_keywords", "keywords")
_SUMMARY_KEYS = ("summary_line", "summary", "lead", "headline")
_ARTICLE_KEYS = ("articles", "kept_articles", "all_articles")


# ---------------------------------------------------------------- 잔손질


def _text(value):
    """어떤 값이 와도 한 줄짜리 깨끗한 문자열로 만든다."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    for dash in _DASHES:
        value = value.replace(dash, ", ")
    for hyphen in _HYPHENS:
        value = value.replace(hyphen, "-")
    value = _EMOJI.sub("", value)
    value = _SPACES.sub(" ", value)
    value = value.replace(" ,", ",").replace(",,", ",")
    return value.strip().strip(",").strip()


def _sentence(value):
    """문장 끝에 마침표가 없으면 붙인다."""
    value = _text(value)
    if not value:
        return ""
    if value[-1] in ".!?…":
        return value
    return value + "."


def _num(value):
    """30.0 은 30 으로, 30.46 은 30.5 로 적는다."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    rounded = round(number, 1)
    if abs(rounded - int(rounded)) < 1e-9:
        return str(int(rounded))
    return str(rounded)


def _first(mapping, keys):
    """여러 이름 가운데 먼저 잡히는 값을 돌려준다."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_count(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _setting_count(cfg, keys, default, floor=1):
    """설정에서 개수 하나를 꺼낸다. 숫자가 아니거나 너무 작으면 기본값으로 둔다."""
    holders = [cfg if isinstance(cfg, dict) else {}]
    brief_cfg = holders[0].get("brief")
    if isinstance(brief_cfg, dict):
        holders.insert(0, brief_cfg)
    for holder in holders:
        for key in keys:
            value = holder.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number >= floor:
                return number
    return default


def _group_limits(cfg):
    """브리핑에 올릴 묶음 수의 위아래 한계를 설정에서 읽는다."""
    top = _setting_count(cfg, ("max_groups", "group_max"), MAX_GROUPS)
    bottom = _setting_count(cfg, ("min_groups", "group_min"), MIN_GROUPS, floor=0)
    if bottom > top:
        # 설정이 서로 어긋나면 위 한계를 믿는다. 넘치게 싣는 쪽이 더 나쁘다.
        bottom = top
    return top, bottom


def _as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _keyword_text(item):
    if isinstance(item, dict):
        for key in ("keyword", "word", "term", "name", "text"):
            if item.get(key):
                return _text(item[key])
        return ""
    return _text(item)


def _domain_of(url, mapping):
    """링크에서 매체 이름 노릇을 할 도메인을 뽑는다. 수집이 준 값이 있으면 그걸 쓴다."""
    url = _text(url)
    if not url:
        return ""
    if url in mapping and mapping[url]:
        return _text(mapping[url]).lower()
    host = urlsplit(url).netloc.lower()
    if not host and "/" in url:
        host = url.split("/")[0].lower()
    if not host:
        host = url.lower()
    host = host.split("@")[-1].split(":")[0]
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def _domain_map(judged, stats):
    """수집 단계가 넘겨준 기사 목록이 있으면 링크와 매체를 짝지어 둔다."""
    mapping = {}
    for holder in (stats, judged):
        if not isinstance(holder, dict):
            continue
        for key in _ARTICLE_KEYS:
            items = holder.get(key)
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("url") and item.get("domain"):
                    mapping.setdefault(_text(item["url"]), _text(item["domain"]))
        extra = holder.get("domains")
        if isinstance(extra, dict):
            for url, domain in extra.items():
                mapping.setdefault(_text(url), _text(domain))
    return mapping


# ---------------------------------------------------------------- 묶음 고르기


def _prepare_groups(judged, domains):
    """묶음마다 매체 수와 대표 링크를 붙이고 크게 다뤄진 순서로 줄 세운다."""
    raw = []
    if isinstance(judged, dict):
        raw = judged.get("groups") or []
    prepared = []
    for index, group in enumerate(raw):
        if not isinstance(group, dict):
            continue
        headline = _sentence(group.get("headline"))
        urls = [_text(u) for u in (group.get("urls") or []) if _text(u)]
        if not headline and not urls:
            continue
        seen = []
        for url in urls:
            domain = _domain_of(url, domains)
            if domain and domain not in seen:
                seen.append(domain)
        kind = _text(group.get("kind")).lower() or "other"
        if kind not in ("tool", "policy", "other"):
            kind = "other"
        prepared.append(
            {
                "headline": headline or "제목을 옮기지 못한 묶음이다.",
                "urls": urls,
                "link": urls[0] if urls else "",
                "domains": seen,
                "domain_count": len(seen) or len(urls),
                "article_count": len(urls),
                "kind": kind,
                "order": index,
            }
        )
    prepared.sort(key=lambda g: (-g["domain_count"], -g["article_count"], g["order"]))
    return prepared


def _split_groups(groups, lead_taken, max_groups):
    """대표 묶음을 뺀 나머지를 일반 묶음과 정책 묶음으로 가른다.

    자리는 대표 묶음과 정책 묶음까지 합쳐 max_groups 개다. 일반 묶음을 먼저
    끊고 정책 묶음을 따로 더 붙이면 화면에 오르는 묶음이 한계의 두 배까지
    늘어난다. 그래서 크게 다뤄진 차례대로 자리만큼 먼저 떼어 낸 다음
    그것을 일반과 정책으로 가른다.
    """
    rest = groups[1:] if lead_taken else groups
    room = max_groups - (1 if lead_taken else 0)
    if room < 0:
        room = 0
    taken = rest[:room]
    general, policy = [], []
    for group in taken:
        if group["kind"] == "policy":
            policy.append(group)
        else:
            general.append(group)
    return general, policy


# ---------------------------------------------------------------- 문장 만들기


def _title_line(date_str):
    label = _text(date_str)
    try:
        day = date.fromisoformat(label)
        return "{0} {1} AI 뉴스 브리핑".format(label, _WEEKDAYS[day.weekday()])
    except (ValueError, TypeError):
        return "{0} AI 뉴스 브리핑".format(label) if label else "AI 뉴스 브리핑"


def _lead_detail(group):
    """대표 묶음에 붙일 매체 수 문장."""
    count = group["domain_count"]
    if count >= 2:
        return "어제 이 소식을 다룬 매체가 {0}곳으로 가장 많았다.".format(count)
    return "다룬 매체는 한 곳뿐이지만 어제 나온 소식 가운데 가장 컸다."


def _volume_sentence(count, avg):
    """기사 수와 최근 7일 평균 대비를 한 문장으로 적는다. 평균이 0 이면 건수만 적는다."""
    if count is None:
        return ""
    if count == 0:
        if avg and avg > 0:
            return "어제는 기사가 한 건도 모이지 않았다. 최근 7일 평균이 {0}건이라 수집이 제대로 돌았는지 봐야 한다.".format(
                _num(avg)
            )
        return "어제는 기사가 한 건도 모이지 않았다."
    base = "어제 모인 기사는 {0}건이다.".format(count)
    if not avg or avg <= 0:
        return base
    diff = count - avg
    tolerance = max(1.0, avg * 0.05)
    if abs(diff) < tolerance:
        return base + " 최근 7일 평균 {0}건과 비슷하다.".format(_num(avg))
    word = "많다" if diff > 0 else "적다"
    return base + " 최근 7일 평균 {0}건보다 {1}건 {2}.".format(_num(avg), _num(abs(diff)), word)


def _group_detail(group):
    """묶음 한 줄 아래에 붙는 매체 수와 대표 링크."""
    bits = ["매체 {0}곳".format(group["domain_count"])]
    if group["kind"] == "tool":
        bits.append("새로 나온 도구")
    if group["link"]:
        bits.append(group["link"])
    return " · ".join(bits)


def _group_notes(total, shown, max_groups, min_groups):
    """묶음을 몇 개 잘랐는지, 너무 적지는 않은지 적는다.

    잘랐다는 사실을 적지 않으면 읽는 사람은 그날 묶음이 그것뿐이라고 읽는다.
    """
    notes = []
    if total > shown:
        notes.append(
            "어제 묶인 소식은 {0}개인데 브리핑에는 {1}개까지만 올리기로 해서 {2}개는 실지 않았다.".format(
                total, max_groups, total - shown
            )
        )
    if min_groups and shown < min_groups:
        notes.append(
            "올린 묶음이 {0}개다. 설정에는 {1}개는 되어야 한다고 적어 두었으니 수집이나 판단 쪽을 한 번 보는 편이 좋겠다.".format(
                shown, min_groups
            )
        )
    return notes


def _history_note(stats):
    """처음 보는 키워드 절을 왜 비웠는지 한 줄 적는다.

    앞선 기간 기사가 DB 에 없으면 집계 쪽이 처음 보는 키워드를 아예 넘기지 않는다.
    빈 DB 에서 세면 관심 키워드 전부가 새 말이 되어 버리기 때문이다. 그런데 절만
    조용히 사라지면 읽는 사람은 어제 새로 올라온 말이 하나도 없었다고 읽는다.
    없어서 비운 것과 비교할 기록이 없어서 비운 것은 다른 말이니 그 사실을 적는다.
    """
    if not isinstance(stats, dict):
        return ""
    비웠나 = stats.get("first_seen_skipped")
    까닭 = _text(stats.get("first_seen_skipped_reason") or "")
    if not 비웠나 and not 까닭:
        return ""
    if 까닭:
        return _sentence(까닭)
    days = stats.get("first_seen_lookback_days")
    if days:
        return "앞선 {0}일 기사 기록이 없어 처음 보는 키워드는 비워 두었다.".format(days)
    return "견줄 기록이 없어 처음 보는 키워드는 비워 두었다."


def _keyword_note(total, shown):
    """처음 보는 키워드를 몇 개에서 끊었는지 적는다."""
    if total <= shown:
        return ""
    return "처음 보는 말은 모두 {0}개인데 여기에는 {1}개만 적었다.".format(total, shown)


def _keyword_line(keywords, total):
    """키워드를 한 줄로 잇는다. 끊은 것이 있으면 몇 개를 끊었는지 꼬리에 붙인다."""
    line = " · ".join(keywords)
    cut = total - len(keywords)
    if cut > 0:
        line += " 외 {0}개".format(cut)
    return line


def _source_sentence(cfg):
    """맨 아래에 붙는 출처 한 줄. 설정에 검색어가 있으면 같이 적는다."""
    cfg = cfg if isinstance(cfg, dict) else {}
    query = _text(_first(cfg, ("query", "default_query", "base_query")))
    if not query and isinstance(cfg.get("gdelt"), dict):
        query = _text(_first(cfg["gdelt"], ("query", "default_query", "base_query")))
    line = "자료는 GDELT DOC 2.0 에서 받았고 하루 경계는 한국 시간으로 끊었다."
    if query:
        line += " 검색어는 {0} 이다.".format(query)
    line += " 제목과 링크, 매체, 시각만 들어오기 때문에 본문은 읽지 않고 제목만 보고 골랐다."
    return line


# ---------------------------------------------------------------- 마크다운과 슬랙


def _markdown(title, summary, lead, volume, general, policy, keywords, source, quiet,
              notes=(), keyword_total=0):
    parts = ["## " + title, ""]
    parts.append(summary)
    if lead is not None:
        parts.append(_lead_detail(lead))
        if lead["link"]:
            parts.append("링크: " + lead["link"])
    tail = [line for line in ([volume] + list(notes or [])) if line]
    if tail:
        parts.append("")
        parts.extend(tail)
    if general:
        heading = "그 밖의 묶음" if lead is not None else "묶어 본 소식"
        parts.extend(["", "### " + heading, ""])
        for group in general:
            parts.append("- " + group["headline"])
            parts.append("  " + _group_detail(group))
    if keywords:
        parts.extend(["", "### 처음 보는 키워드", ""])
        parts.append(_keyword_line(keywords, keyword_total))
        parts.append("")
        line = "최근 7일 기사에는 없다가 어제 처음 올라온 말이다."
        note = _keyword_note(keyword_total, len(keywords))
        if note:
            line += " " + note
        parts.append(line)
    if policy:
        parts.extend(["", "### 규제와 정책", ""])
        for group in policy:
            parts.append("- " + group["headline"])
            parts.append("  " + _group_detail(group))
    parts.extend(["", source, ""])
    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if quiet:
        # 조용한 날은 짧게 끝낸다. 군더더기 줄이 남지 않게 한 번 더 훑는다.
        text = text.rstrip() + "\n"
    return text.lstrip("\n")


def _cap(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _slack_link(group):
    if not group["link"]:
        return ""
    label = group["domains"][0] if group["domains"] else "기사 보기"
    return "<{0}|{1}>".format(group["link"], label)


def _slack_group_text(group):
    detail = ["매체 {0}곳".format(group["domain_count"])]
    if group["kind"] == "tool":
        detail.append("새로 나온 도구")
    link = _slack_link(group)
    if link:
        detail.append(link)
    return "• {0}\n{1}".format(group["headline"], " · ".join(detail))


def _slack_blocks(title, summary, lead, volume, general, policy, keywords, source,
                  notes=(), keyword_total=0):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _cap(title, 150), "emoji": False}}
    ]
    lead_text = summary
    if lead is not None:
        lead_text += "\n" + _lead_detail(lead)
        link = _slack_link(lead)
        if link:
            lead_text += "\n" + link
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _cap(lead_text, 3000)}})
    tail = [line for line in ([volume] + list(notes or [])) if line]
    if tail:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": _cap("\n".join(tail), 3000)}]}
        )
    if general:
        heading = "그 밖의 묶음" if lead is not None else "묶어 본 소식"
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*{0}*".format(heading)}})
        for group in general:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": _cap(_slack_group_text(group), 3000)}}
            )
    if keywords:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cap(
                        "*처음 보는 키워드*\n" + _keyword_line(keywords, keyword_total), 3000
                    ),
                },
            }
        )
    if policy:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*규제와 정책*"}})
        for group in policy:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": _cap(_slack_group_text(group), 3000)}}
            )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _cap(source, 3000)}]})
    return blocks


# ---------------------------------------------------------------- 바깥에 내놓는 함수


def build_brief(date_str, judged, stats, cfg):
    """하루치 브리핑을 만들어 마크다운과 슬랙 블록, 한 줄 요약을 함께 돌려준다.

    date_str 은 한국 날짜 'YYYY-MM-DD' 이다.
    judged 는 judge.judge_articles 가 돌려준 {kept, dropped, groups} 이고,
    groups 항목은 {headline, urls, kind} 를 갖는다.
    stats 는 집계 숫자를 담은 딕셔너리다. 이름이 조금 달라도 알아보게 해 뒀다.
        기사 수: article_count, count, total, total_for_date 가운데 하나
        최근 7일 평균: avg_7d, average_7d, recent_avg 가운데 하나
        처음 보는 키워드: first_seen_keywords 또는 new_keywords
        처음 보는 키워드를 비운 까닭이 있으면 first_seen_skipped 와
        first_seen_skipped_reason 에 담아 준다. 그러면 본문에 그 사실을 한 줄 적는다.
        앞선 기간 기록이 없어 비운 것과 새 말이 없어 비운 것은 다른 말이다.
        따로 쓴 한 줄 요약이 있으면 summary_line 에 넣으면 그대로 쓴다.
    cfg 는 config.load_config 결과다. query 가 있으면 출처 줄에 검색어를 적는다.
        brief.max_groups   대표 묶음과 정책 묶음까지 합쳐 올릴 묶음 수 상한. 기본 5
        brief.min_groups   이보다 적으면 그 사실을 브리핑에 한 줄 적는다. 기본 3
        brief.max_keywords 처음 보는 키워드를 몇 개까지 적을지. 기본 8
    돌려주는 값에는 묶음을 몇 개 가운데 몇 개 실었는지도 group_total 과
    group_shown 으로 적어 둔다. 점검 쪽에서 세어 보라고 남기는 값이다.
    """
    stats = stats if isinstance(stats, dict) else {}
    judged = judged if isinstance(judged, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}

    domains = _domain_map(judged, stats)
    groups = _prepare_groups(judged, domains)

    count = _as_count(_first(stats, _COUNT_KEYS))
    avg = _as_float(_first(stats, _AVG_KEYS))
    keywords = _first(stats, _KEYWORD_KEYS) or []
    if isinstance(keywords, dict):
        keywords = list(keywords.keys())
    if not isinstance(keywords, (list, tuple)):
        keywords = []
    seen_words = []
    for item in keywords:
        word = _keyword_text(item)
        if word and word not in seen_words:
            seen_words.append(word)
    keyword_total = len(seen_words)
    keyword_limit = _setting_count(cfg, ("max_keywords", "keyword_max"), MAX_KEYWORDS)
    keywords = seen_words[:keyword_limit]

    max_groups, min_groups = _group_limits(cfg)

    given_summary = _sentence(_first(stats, _SUMMARY_KEYS) or _first(judged, _SUMMARY_KEYS))
    title = _title_line(date_str)
    source = _source_sentence(cfg)
    volume = _volume_sentence(count, avg)

    history_note = _history_note(stats)

    if not groups:
        # 조용한 날. 빈 섹션 제목을 남기지 않고 짧게 끝낸다.
        if count == 0:
            summary = "어제는 조용했다. 기사가 한 건도 모이지 않았다."
            volume = ""
            if avg and avg > 0:
                volume = "최근 7일 평균은 {0}건이다. 수집이 제대로 돌았는지 확인하는 편이 좋겠다.".format(_num(avg))
        else:
            summary = "어제는 조용했다. 묶을 만한 소식이 없었다."
        quiet_notes = [line for line in [history_note] if line]
        markdown = _markdown(
            title, summary, None, volume, [], [], keywords, source, True,
            notes=quiet_notes, keyword_total=keyword_total,
        )
        blocks = _slack_blocks(
            title, summary, None, volume, [], [], keywords, source,
            notes=quiet_notes, keyword_total=keyword_total,
        )
        return {
            "markdown": markdown,
            "slack_blocks": blocks,
            "summary_line": summary,
            "group_total": 0,
            "group_shown": 0,
        }

    if given_summary:
        # 사람이나 판단 단계가 써 준 한 줄이 있으면 그 문장을 쓰고, 묶음은 목록에 다 남긴다.
        summary = given_summary
        lead = None
    else:
        # 따로 받은 요약이 없으면 가장 크게 다뤄진 묶음을 그날의 한 줄로 삼는다.
        lead = groups[0]
        summary = lead["headline"]

    general, policy = _split_groups(groups, lead is not None, max_groups)
    shown = len(general) + len(policy) + (1 if lead is not None else 0)
    notes = _group_notes(len(groups), shown, max_groups, min_groups)
    if history_note:
        notes.append(history_note)
    markdown = _markdown(
        title, summary, lead, volume, general, policy, keywords, source, False,
        notes=notes, keyword_total=keyword_total,
    )
    blocks = _slack_blocks(
        title, summary, lead, volume, general, policy, keywords, source,
        notes=notes, keyword_total=keyword_total,
    )
    return {
        "markdown": markdown,
        "slack_blocks": blocks,
        "summary_line": summary,
        "group_total": len(groups),
        "group_shown": shown,
    }


def save_brief(markdown, path):
    """브리핑 마크다운을 파일로 남기고 저장한 경로를 돌려준다."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown if markdown.endswith("\n") else markdown + "\n")
    return path


def default_brief_path(root, date_str):
    """저장 자리를 정해 두지 않았을 때 쓰는 기본 경로다."""
    return os.path.join(str(root), "data", "briefs", "{0}.md".format(_text(date_str) or "unknown"))


def brief_payload(built):
    """슬랙으로 보낼 때 쓰는 모양으로 바꾼다. notify.send_slack 에 그대로 넘기면 된다."""
    built = built or {}
    return {"text": built.get("summary_line", ""), "blocks": built.get("slack_blocks", [])}


def _dump(built):
    """눈으로 확인할 때 쓰는 잔손. 슬랙 블록을 보기 좋게 찍는다."""
    return json.dumps(built.get("slack_blocks", []), ensure_ascii=False, indent=2)
