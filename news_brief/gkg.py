"""GDELT 번역 수집 원본 파일(Translation GKG)에서 한국어 AI 기사를 뽑아 오는 모듈.

2026-09-14 부터 이틀 동안 GitHub Actions 러너에서 GDELT DOC 2.0 API 를 부르면
대부분 429 로 막혔고, 겨우 통과해도 빈 응답이 왔다. 러너 IP 를 여러 사용자가 같이
써서 우리 간격과 상관없이 막히는 것이다. 같은 데이터가 15분마다 파일로도 올라오고,
그 파일 서버는 막히지 않았다. 그래서 하루치 파일 96개를 받아 우리가 직접 거른다.

이 모듈은 collect 가 쓰는 부르개(fetcher) 모양을 그대로 따른다. 하루치를 한 번만
받아 두고, 요청한 시간 구간에 드는 기사만 돌려준다. 그래서 수집기의 구간 쪼개기,
중복 걸러내기, 원본 저장, 점검이 손대지 않고 그대로 돈다.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import logging
import re
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

from . import config, gdelt, netssl

logger = logging.getLogger(__name__)

FILE_URL = "https://data.gdeltproject.org/gdeltv2/{ts}.translation.gkg.csv.zip"
SLICE_MINUTES = 15
TIMEOUT_SECONDS = 60
ATTEMPTS = 3
DEFAULT_WORKERS = 6
DEFAULT_MAX_MISSING = 8

# 제목에 이 말 가운데 하나가 들어간 한국어 기사만 AI 소식 후보로 받는다.
# 예전 API 검색어("artificial intelligence" OR OpenAI)는 번역한 본문 전체를 봤지만
# 원본 파일에서는 제목으로만 고를 수 있다. 판단 단계가 한 번 더 거른다.
DEFAULT_TITLE_PATTERN = (
    r"(?<![A-Za-z])A\.?I(?![A-Za-z])|인공지능|생성형|챗GPT|ChatGPT|(?<![A-Za-z])GPT|LLM|"
    r"오픈AI|OpenAI|앤트로픽|Anthropic|클로드|제미나이|Gemini|코파일럿|Copilot|"
    r"딥러닝|머신러닝|에이전트|엔비디아|NVIDIA|데이터센터|휴머노이드|자율주행"
)

WINDOW_FORMAT = "%Y%m%d%H%M%S"

# GKG 2.1 열 순서. 필요한 것만 적는다.
COL_DATE = 1
COL_SOURCE = 3
COL_URL = 4
COL_TRANSLATION = 25
COL_EXTRAS = 26

_PAGE_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
_SOURCE_LANG = re.compile(r"srclc:(\w+)")


def _gkg_settings(cfg):
    section = cfg.get("gkg") if isinstance(cfg, dict) and isinstance(cfg.get("gkg"), dict) else {}
    return section


def slice_stamps(start_utc, end_utc):
    """구간 [start, end] 에 드는 15분 파일 이름 시각을 차례로 돌려준다."""
    start = dt.datetime.strptime(str(start_utc), WINDOW_FORMAT)
    end = dt.datetime.strptime(str(end_utc), WINDOW_FORMAT)
    minute = (start.minute // SLICE_MINUTES) * SLICE_MINUTES
    cursor = start.replace(minute=minute, second=0)
    if cursor < start:
        cursor += dt.timedelta(minutes=SLICE_MINUTES)
    stamps = []
    while cursor <= end:
        stamps.append(cursor.strftime(WINDOW_FORMAT))
        cursor += dt.timedelta(minutes=SLICE_MINUTES)
    return stamps


def parse_file(zip_bytes, title_pattern):
    """zip 한 개에서 한국어이고 제목에 AI 관련어가 든 기사만 뽑는다.

    돌려주는 모양은 DOC API 의 artlist 항목과 같다(url, title, domain, seendate, language).
    두 번째 값은 그 파일에 든 한국어 기사 수다. 로그와 점검에 쓴다.
    """
    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = archive.namelist()
    if not names:
        return [], 0
    text = archive.read(names[0]).decode("utf-8", errors="replace")
    matcher = re.compile(title_pattern, re.I)
    picked = []
    korean = 0
    for line in text.split("\n"):
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) <= COL_EXTRAS:
            continue
        lang = _SOURCE_LANG.search(cols[COL_TRANSLATION])
        if not lang or lang.group(1) != "kor":
            continue
        korean += 1
        found = _PAGE_TITLE.search(cols[COL_EXTRAS])
        title = html.unescape(found.group(1)).strip() if found else ""
        if not title or not matcher.search(title):
            continue
        stamp = re.sub(r"\D", "", cols[COL_DATE])
        picked.append(
            {
                "url": cols[COL_URL].strip(),
                "title": title,
                "domain": cols[COL_SOURCE].strip().lower(),
                "seendate": "%sT%sZ" % (stamp[:8], stamp[8:14]) if len(stamp) >= 14 else stamp,
                "language": "Korean",
            }
        )
    return picked, korean


def _download(url, opener=None):
    """파일 하나를 받는다. 없는 파일(404)은 None 이고, 그 밖의 실패는 몇 번 더 해 본다."""
    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            if opener is not None:
                return opener(url)
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS, context=netssl.ssl_context()) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            last_error = error
        except Exception as error:  # 연결 끊김, 시간 초과
            last_error = error
        logger.warning("%s 받기 %d번째 실패. 사유는 %s 다", url, attempt, last_error)
    logger.error("%s 를 %d번 해 보고도 받지 못했다. 사유는 %s 다", url, ATTEMPTS, last_error)
    return None


def day_fetcher(date_str, cfg=None, opener=None):
    """한국 날짜 하루치 원본 파일을 받아 두고, 구간별로 기사를 내주는 부르개를 만든다.

    받지 못한 파일이 max_missing_files 개를 넘으면 GdeltRateLimited 를 던진다.
    수집기가 그 구간을 막힌 구간으로 적고, 점검이 하루치를 멈춘다. 빈 구멍이 난
    하루를 조용한 날로 착각하지 않게 하려는 것이다.
    """
    cfg = cfg or {}
    section = _gkg_settings(cfg)
    title_pattern = section.get("title_pattern") or DEFAULT_TITLE_PATTERN
    workers = int(section.get("workers") or DEFAULT_WORKERS)
    max_missing = int(section.get("max_missing_files", DEFAULT_MAX_MISSING))
    기억 = {}

    def _하루치():
        if "articles" in 기억:
            return 기억
        start_utc, end_utc = config.kst_day_window(date_str)
        stamps = slice_stamps(start_utc, end_utc)
        urls = [FILE_URL.format(ts=ts) for ts in stamps]
        logger.info("%s 원본 파일 %d개를 받는다. 구간 %s ~ %s", date_str, len(urls), start_utc, end_utc)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            bodies = list(pool.map(lambda u: _download(u, opener), urls))
        articles = []
        missing = []
        korean_total = 0
        for url, body in zip(urls, bodies):
            if body is None:
                missing.append(url)
                continue
            try:
                picked, korean = parse_file(body, title_pattern)
            except zipfile.BadZipFile:
                logger.warning("%s 는 zip 이 아니라 읽지 못했다", url)
                missing.append(url)
                continue
            articles.extend(picked)
            korean_total += korean
        logger.info(
            "%s 원본 파일 %d개 가운데 %d개를 받았다. 한국어 기사 %d건, 제목에 AI 관련어가 든 기사 %d건이다",
            date_str,
            len(urls),
            len(urls) - len(missing),
            korean_total,
            len(articles),
        )
        기억.update({"articles": articles, "missing": missing, "total": len(urls)})
        return 기억

    def fetch(query, start_utc, end_utc, maxrecords=gdelt.DEFAULT_MAXRECORDS, sleep_seconds=0):
        data = _하루치()
        if len(data["missing"]) > max_missing:
            raise gdelt.GdeltRateLimited(
                "GDELT 원본 파일 %d개 가운데 %d개를 받지 못했다. 허용은 %d개까지다"
                % (data["total"], len(data["missing"]), max_missing)
            )
        items = [
            item
            for item in data["articles"]
            if str(start_utc) <= re.sub(r"\D", "", item["seendate"])[:14] <= str(end_utc)
        ]
        return {"articles": items[: int(maxrecords)]}

    fetch.missing = lambda: list(_하루치()["missing"])
    return fetch
