"""GDELT DOC 2.0 API 에서 기사 목록(artlist)을 받아오는 모듈.

키가 필요 없는 공개 API 라 인증은 없다. 대신 호출이 잦으면 서버가 JSON 대신
안내 문구나 빈 본문을 돌려준다. 그 상황을 GdeltRateLimited 로 구분해 두면
위쪽 수집기가 다시 부를지 멈출지 정할 수 있다.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .netssl import CA_BUNDLE_CANDIDATES, _ca_bundle_path, ssl_context

logger = logging.getLogger(__name__)

# 인증서 컨텍스트를 만드는 일은 netssl 이 맡는다. 수집만 겪는 문제가 아니라
# 앤스로픽 호출과 슬랙 전송도 같은 자리에서 막히기 때문에 공용으로 옮겼다.
# 예전에 이 모듈에서 부르던 이름을 그대로 쓸 수 있게 여기서 다시 내보낸다.
__all__ = [
    "API_URL",
    "CA_BUNDLE_CANDIDATES",
    "DEFAULT_MAXRECORDS",
    "DEFAULT_SLEEP_SECONDS",
    "GdeltError",
    "GdeltRateLimited",
    "RETRY_WAITS",
    "build_url",
    "fetch_artlist",
    "parse_response",
    "ssl_context",
]

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# 한 번에 받을 수 있는 상한. GDELT 문서에 적힌 값이다.
# 응답 건수가 이 값에 딱 맞으면 잘렸다고 보고 시간 구간을 쪼개야 한다.
DEFAULT_MAXRECORDS = 250

# 요청 사이 간격. 5초에 한 번으로 약속했다.
DEFAULT_SLEEP_SECONDS = 5

# 응답이 느릴 때가 있어 넉넉히 잡았다.
TIMEOUT_SECONDS = 40

# 429 를 받았을 때 다시 부르기 전에 쉴 시간이다. 앞에서부터 하나씩 쓴다.
# 5초 간격을 지켜도 GDELT 가 막는 날이 있어서, 한 번 막혔다고 하루치를
# 통째로 버리지 않도록 세 번까지 다시 불러 본다. 다 더해도 1분 반이라
# 하루 한 번 도는 일정에는 부담이 없다.
RETRY_WAITS = (12, 24, 48)

# urllib 기본 User-Agent 로 부르면 거절당하는 경우가 있어 따로 붙인다.
USER_AGENT = "ai-news-brief/1.0 (portfolio project; python-urllib)"

class GdeltError(RuntimeError):
    """GDELT 호출이 실패했을 때 쓰는 기본 예외.

    RuntimeError 를 물려받은 것은 cli 가 잡아 사람이 읽을 한 줄로 바꿔 주기
    때문이다. Exception 에서 바로 물려받으면 수집이 막힌 날 명령이 파이썬
    역추적만 토하고 끝나서, 무엇이 어긋났는지 로그에서 알아보기 어렵다.
    """


class GdeltRateLimited(GdeltError):
    """JSON 이 아닌 응답이 왔을 때. 대개 호출이 너무 잦아 막힌 경우다."""


def build_url(query, start_utc, end_utc, maxrecords=DEFAULT_MAXRECORDS):
    """호출할 주소를 만든다. 시각은 'YYYYMMDDHHMMSS' 형태의 UTC 문자열이다."""
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "startdatetime": start_utc,
        "enddatetime": end_utc,
        "maxrecords": str(int(maxrecords)),
        "sort": "datedesc",
    }
    return API_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _read_body(url):
    """주소를 열어 본문 문자열을 돌려준다. 막힌 응답은 예외로 바꾼다."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        head = ""
        try:
            head = error.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        if error.code in (429, 503):
            raise GdeltRateLimited(
                "GDELT 가 %d 로 응답했다. 호출이 너무 잦은 것으로 보인다. 응답 앞부분: %s"
                % (error.code, head)
            ) from error
        raise GdeltError(
            "GDELT 가 %d 로 응답했다. 응답 앞부분: %s" % (error.code, head)
        ) from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise GdeltError(
                "GDELT 인증서를 검증하지 못했다. 파이썬이 들고 있는 CA 묶음이 비어 있는 "
                "경우가 대부분이다. 맥에서 python.org 파이썬을 쓰면 "
                "'Install Certificates.command' 를 한 번 돌리거나, SSL_CERT_FILE 에 "
                "/etc/ssl/cert.pem 를 적어 주면 된다. 원래 사유: %s" % (error.reason,)
            ) from error
        raise GdeltError("GDELT 에 닿지 못했다. 이유: %s" % (error.reason,)) from error

    return raw.decode("utf-8", errors="replace")


def parse_response(text):
    """응답 문자열을 dict 로 바꾼다. JSON 이 아니면 GdeltRateLimited 를 던진다."""
    body = (text or "").strip()
    if not body:
        raise GdeltRateLimited("GDELT 가 빈 본문을 돌려줬다. 잠시 뒤 다시 부르는 편이 낫다.")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        # GDELT 응답에 제어문자가 섞여 들어오는 일이 있어 한 번 더 느슨하게 시도한다.
        try:
            parsed = json.loads(body, strict=False)
        except json.JSONDecodeError as error:
            raise GdeltRateLimited(
                "GDELT 응답이 JSON 이 아니다. 앞부분: %s" % (body[:200],)
            ) from error

    if isinstance(parsed, list):
        # 드물게 목록만 돌아오는 경우가 있어 모양을 맞춰 준다.
        return {"articles": parsed}
    if not isinstance(parsed, dict):
        raise GdeltRateLimited(
            "GDELT 응답을 기사 목록으로 볼 수 없다. 앞부분: %s" % (body[:200],)
        )
    return parsed


def fetch_artlist(
    query,
    start_utc,
    end_utc,
    maxrecords=DEFAULT_MAXRECORDS,
    sleep_seconds=DEFAULT_SLEEP_SECONDS,
    retry_waits=RETRY_WAITS,
    sleeper=None,
):
    """기사 목록을 받아 온다.

    부르기 전에 sleep_seconds 만큼 쉰다. 쉬는 자리를 호출 뒤가 아니라 앞에 둔 것은,
    구간을 쪼개 연달아 부를 때 앞 요청과의 간격을 확실히 벌리기 위해서다.

    GDELT 가 막으면 retry_waits 에 적힌 시간만큼 쉬고 다시 부른다. 5초 간격을
    지켜도 429 가 돌아오는 날이 있어서다. 적힌 만큼 다 해 보고도 막히면
    GdeltRateLimited 를 그대로 올려 보낸다. sleeper 는 시험에서 실제로 쉬지
    않게 하려고 열어 둔 자리다.
    """
    쉬기 = sleeper or time.sleep
    if sleep_seconds and sleep_seconds > 0:
        쉬기(sleep_seconds)

    url = build_url(query, start_utc, end_utc, maxrecords=maxrecords)
    logger.info("GDELT 를 부른다. 구간 %s ~ %s, 상한 %d건", start_utc, end_utc, maxrecords)

    대기표 = [float(w) for w in (retry_waits or ()) if float(w) > 0]
    다시부른횟수 = 0
    while True:
        try:
            payload = parse_response(_read_body(url))
            break
        except GdeltRateLimited as error:
            if 다시부른횟수 >= len(대기표):
                logger.error(
                    "GDELT 가 %d번 다시 불러도 막았다. 여기서 멈춘다. 사유: %s",
                    다시부른횟수,
                    error,
                )
                raise
            쉴시간 = 대기표[다시부른횟수]
            다시부른횟수 += 1
            logger.warning(
                "GDELT 가 막았다. %g초 쉬고 %d번째로 다시 부른다. 사유: %s",
                쉴시간,
                다시부른횟수,
                error,
            )
            쉬기(쉴시간)

    if 다시부른횟수:
        logger.info("%d번째로 다시 부른 요청이 통했다", 다시부른횟수)

    count = len(payload.get("articles") or [])
    logger.info("GDELT 응답을 받았다. 기사 %d건", count)
    return payload
