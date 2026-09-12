"""제목만 보고 기사를 가려내고 묶는 단계를 맡는다.

이 단계가 하는 일은 네 가지다.
첫째, AI 소식이 맞는지 제목으로 가린다. 뺀 기사는 왜 뺐는지 한국어로 남긴다.
둘째, 같은 사건을 다룬 기사끼리 묶는다. 묶는 기준은 제목 토큰의 자카드 유사도다.
셋째, 묶음이 새 도구 소식인지 규제나 정책 소식인지 가려낸다.
넷째, 묶음마다 한 줄짜리 한국어 문장을 붙인다.

백엔드는 둘이다. rules 는 설정에 적힌 키워드만 쓰고 네트워크를 타지 않는다.
llm 은 ANTHROPIC_API_KEY 가 있을 때만 앤스로픽 메시지 API 를 불러 같은 구조를 받아온다.
키가 없거나 호출이 실패하거나 응답 형식이 어긋나면 곧바로 rules 로 내려앉고,
그 사실을 로그와 결과의 fallback_reason 에 남긴다.

판단 결과는 DB 에 남긴다. 같은 날짜를 같은 기사로 다시 돌리면 저장된 판단을 그대로
읽어 쓰므로 몇 번을 돌려도 숫자가 흔들리지 않는다. 다만 같은 날짜를 다시 수집해
기사가 늘거나 링크가 바뀌었으면 옛 판단을 쓰지 않고 다시 판단한다. 그때 숫자를
지키는 것은 틀린 숫자를 지키는 일이기 때문이다.

줄표와 이모지는 이 단계에서 걷어 낸다. 브리핑만 사후에 정리하면 같은 값을 읽어
가는 리포트와 슬랙, 노션에는 그대로 새어 나간다.

설정의 낱말 목록은 자리가 아예 없을 때만 기본값으로 내려간다. keywords.json 에
빈 배열이 적혀 있으면 그것을 사람의 뜻으로 보고 빈 목록을 쓴다.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import netssl

로그 = logging.getLogger(__name__)

# 설정이 비어 있어도 돌아가도록 기본 키워드를 들고 있는다.
# 설정 파일에 같은 이름의 목록이 있으면 그쪽이 이긴다.
DEFAULT_INTEREST = [
    "ai", "a.i.", "인공지능", "에이아이",
    "openai", "오픈ai", "오픈에이아이",
    "chatgpt", "챗gpt", "챗지피티", "gpt",
    "llm", "거대언어모델", "대규모 언어모델", "언어모델",
    "생성형", "생성 ai", "파운데이션 모델",
    "머신러닝", "기계학습", "딥러닝", "신경망",
    "anthropic", "앤스로픽", "claude", "클로드",
    "gemini", "제미나이", "딥마인드", "코파일럿", "copilot",
    "챗봇", "휴머노이드", "자율주행", "ai반도체", "온디바이스",
    "데이터센터", "빅데이터", "알고리즘",
]

DEFAULT_EXCLUDE = [
    "주가", "증시", "코스피", "코스닥", "상한가", "하한가", "목표주가",
    "수혜주", "테마주", "관련주", "급등", "급락", "공모주", "배당",
    "부고", "인사이동", "포토", "날씨", "운세", "로또", "부동산 분양",
    "프로야구", "프로축구", "연예", "아이돌",
]

DEFAULT_REGULATION = [
    "규제", "규정", "지침", "가이드라인", "법안", "입법", "제정", "개정안",
    "시행령", "시행규칙", "의무화", "제재", "과징금", "처벌", "단속",
    "개인정보위", "개인정보보호위원회", "과기정통부", "방통위",
    "방송통신위원회", "공정위", "공정거래위원회", "금융위", "금감원",
    "국회", "정부", "청문회", "국정감사", "소송", "저작권", "윤리",
    "ai기본법", "ai 기본법", "eu ai법", "인공지능법",
]

DEFAULT_TOOL = [
    "출시", "공개", "내놨", "내놓", "선보", "론칭", "런칭", "첫 공개",
    "정식 서비스", "베타 서비스", "오픈베타", "업데이트", "새 모델",
    "신모델", "신제품", "새 기능", "탑재", "도입", "적용", "무료 배포",
]

DEFAULT_STOPWORDS = [
    "및", "등", "위한", "위해", "통해", "대한", "관련", "올해", "지난",
    "오늘", "내년", "이번", "우리", "국내", "속보", "단독", "종합",
    "인터뷰", "기자", "뉴스", "사진", "영상", "전망", "분석", "가장",
]

DEFAULT_SIMILARITY = 0.4
# config.DEFAULT_SETTINGS 의 judge.model 과 같은 값으로 둔다. 두 곳이 어긋나면
# 설정을 거쳐 부를 때와 judge 를 직접 부를 때 다른 모델로 나간다.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 4000

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_KIND_VALUES = ("tool", "policy", "other")
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_HANGUL_RE = re.compile(r"[가-힣]")
_TAG_RE = re.compile(r"^\s*[\[\(<【〔][^\]\)>】〕]{1,14}[\]\)>】〕]\s*")
_BAR_TAIL_RE = re.compile(r"\s*[|｜]\s*[^|｜]{1,24}$")
_DASH_TAIL_RE = re.compile(r"\s+-\s+[^-]{1,24}$")
_TAIL_PUNCT_RE = re.compile(r"[\s.,·…'\"“”‘’]+$")

# 줄표류와 이모지는 쓰지 않는다. 제목에 섞여 들어오면 여기서 걷어 낸다.
# brief._text 와 같은 잣대를 쓴다. 판단이 만든 한 줄 문장은 브리핑과 리포트,
# 슬랙, 노션이 모두 같은 값을 읽으므로 정리는 이 자리에서 한 번만 한다.
_DASHES = ("—", "–", "―", "‒", "−")
# 붙임표류는 쉼표가 아니라 아스키 붙임표로 내린다. 'GPT‑5' 는 'GPT-5' 가 맞다.
_HYPHENS = ("‐", "‑")
_EMOJI_RE = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿™ℹ️‍]")
_SPACES_RE = re.compile(r"\s+")

# 한글 토큰 끝에 붙는 조사와 어미다. 길이가 긴 것부터 떼어 본다.
_JOSA = (
    "으로써", "에서는", "에게서", "이라는", "이라고", "으로는",
    "라는", "라고", "라며", "에서", "에게", "까지", "부터", "보다",
    "처럼", "이나", "으로", "에는", "한다", "했다", "하는", "되는",
    "된다", "이다", "들이", "들은",
    "의", "가", "이", "은", "는", "을", "를", "에", "와", "과",
    "도", "로", "만", "랑",
)

_정규식_캐시 = {}


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------

def _파고들기(cfg, 경로):
    """점으로 이어진 경로를 따라 설정값을 꺼낸다. 없으면 None 이다."""
    현재 = cfg
    for 조각 in 경로.split("."):
        if not isinstance(현재, dict) or 조각 not in 현재:
            return None
        현재 = 현재[조각]
    return 현재


def _자리있나(cfg, 경로):
    """점으로 이어진 자리에 값이 놓여 있는지와 그 값을 함께 돌려준다.

    빈 목록도 값으로 본다. 있는지 없는지를 값의 참거짓으로 가리면
    keywords.json 의 목록을 비워 좁히려는 사람이 정반대 결과를 받는다.
    """
    현재 = cfg
    for 조각 in 경로.split("."):
        if not isinstance(현재, dict) or 조각 not in 현재:
            return False, None
        현재 = 현재[조각]
    return True, 현재


def _목록설정(cfg, 경로들, 기본값, 이름=""):
    """설정에서 문자열 목록을 찾는다. 자리가 아예 없을 때만 기본 목록을 쓴다.

    자리가 있고 값이 빈 목록이면 그것을 사람의 뜻으로 받아들여 빈 목록을 쓴다.
    목록을 비우는 것이 그 검사를 끄는 가장 단순한 방법이기 때문이다.
    어느 목록을 어디서 읽었는지는 로그에 한 줄 남긴다.
    """
    이름 = 이름 or (경로들[0] if 경로들 else "목록")
    for 경로 in 경로들:
        있나, 값 = _자리있나(cfg, 경로)
        if not 있나 or not isinstance(값, (list, tuple)):
            continue
        다듬은것 = [str(항목).strip().lower() for 항목 in 값 if str(항목).strip()]
        로그.info("%s 목록은 설정의 %s 에서 읽었다. %d개다", 이름, 경로, len(다듬은것))
        return 다듬은것
    로그.info("%s 목록이 설정에 없어 코드에 박아 둔 기본값 %d개를 쓴다", 이름, len(기본값))
    return [항목.lower() for 항목 in 기본값]


def _수설정(cfg, 경로들, 기본값):
    """설정에서 숫자를 찾는다. 숫자가 아니면 기본값으로 둔다."""
    for 경로 in 경로들:
        값 = _파고들기(cfg, 경로)
        if isinstance(값, bool):
            continue
        if isinstance(값, (int, float)):
            return float(값)
        if isinstance(값, str):
            try:
                return float(값)
            except ValueError:
                continue
    return float(기본값)


def _글설정(cfg, 경로들, 기본값):
    """설정에서 문자열을 찾는다. 비어 있으면 기본값으로 둔다."""
    for 경로 in 경로들:
        값 = _파고들기(cfg, 경로)
        if isinstance(값, str) and 값.strip():
            return 값.strip()
    return 기본값


def _규칙설정(cfg):
    """판단에 쓰는 값을 한 군데로 모아 둔다."""
    return {
        "interest": _목록설정(cfg, (
            "keywords.interest", "keywords.관심", "interest_keywords",
            "judge.interest", "interest",
        ), DEFAULT_INTEREST, "관심어"),
        "exclude": _목록설정(cfg, (
            "keywords.exclude", "keywords.제외", "exclude_keywords",
            "judge.exclude", "exclude",
        ), DEFAULT_EXCLUDE, "제외어"),
        "regulation": _목록설정(cfg, (
            "keywords.regulation", "keywords.policy", "keywords.규제",
            "regulation_keywords", "policy_keywords", "judge.regulation",
        ), DEFAULT_REGULATION, "규제어"),
        "tool": _목록설정(cfg, (
            "keywords.tool", "keywords.tool_release", "keywords.도구",
            "tool_keywords", "judge.tool",
        ), DEFAULT_TOOL, "도구어"),
        "stopwords": _목록설정(cfg, (
            "keywords.stopwords", "stopwords", "judge.stopwords",
        ), DEFAULT_STOPWORDS, "불용어"),
        "threshold": _수설정(cfg, (
            "judge.similarity_threshold", "similarity_threshold",
            "judge.jaccard", "grouping.similarity_threshold",
        ), DEFAULT_SIMILARITY),
        "model": _글설정(cfg, (
            "judge.model", "llm.model", "anthropic.model", "model",
        ), DEFAULT_MODEL),
        "max_tokens": int(_수설정(cfg, (
            "judge.max_tokens", "llm.max_tokens",
        ), DEFAULT_MAX_TOKENS)),
        "timeout": _수설정(cfg, (
            "judge.timeout_seconds", "llm.timeout_seconds",
        ), 60),
    }


# ---------------------------------------------------------------------------
# 잔손질 도구
# ---------------------------------------------------------------------------

def _받침있나(말):
    """마지막 글자에 받침이 있는지 본다. 조사를 고르려고 쓴다."""
    말 = (말 or "").strip().strip("'\"()[]")
    if not 말:
        return False
    끝 = 말[-1]
    if "가" <= 끝 <= "힣":
        return (ord(끝) - 0xAC00) % 28 != 0
    끝 = 끝.lower()
    if 끝 in "lmnr":
        return True
    # 숫자는 읽는 소리로 따진다. 영 일 삼 육 칠 팔에는 받침이 있다.
    if 끝.isdigit():
        return 끝 in "013678"
    return False


def _조사(말, 받침용, 민말용):
    """앞말에 맞는 조사를 골라 준다."""
    return 받침용 if _받침있나(말) else 민말용


def _글다듬기(글):
    """줄표를 쉼표로 바꾸고 이모지를 떼어 낸다. brief._text 와 같은 잣대다.

    판단이 만든 한 줄 문장은 브리핑만 쓰는 것이 아니다. 리포트와 슬랙, 노션이
    DB 에 남은 값을 그대로 읽어 가므로, 정리를 표현 단계에 맡기면 브리핑만
    깨끗하고 나머지에는 줄표와 이모지가 그대로 새어 나간다.
    """
    글 = "" if 글 is None else str(글)
    for 줄표 in _DASHES:
        글 = 글.replace(줄표, ", ")
    for 붙임표 in _HYPHENS:
        글 = 글.replace(붙임표, "-")
    글 = _EMOJI_RE.sub("", 글)
    글 = _SPACES_RE.sub(" ", 글)
    글 = 글.replace(" ,", ",").replace(",,", ",")
    return 글.strip().strip(",").strip()


def _정규식(패턴):
    """같은 패턴을 여러 번 컴파일하지 않으려고 캐시해 둔다."""
    만든것 = _정규식_캐시.get(패턴)
    if 만든것 is None:
        만든것 = re.compile(패턴)
        _정규식_캐시[패턴] = 만든것
    return 만든것


def _키워드있나(소문자제목, 키워드):
    """제목에 키워드가 있는지 본다.

    영문 키워드는 앞뒤가 영문이나 숫자면 걸리지 않게 한다.
    ai 가 email 이나 Thailand 안에서 잡히는 일을 막으려는 것이다.
    한 글자짜리 한글 키워드도 앞뒤가 한글이면 걸리지 않게 한다.
    법 이라는 키워드가 방법이나 법인에서 잡히면 곤란하기 때문이다.
    """
    키워드 = (키워드 or "").strip().lower()
    if not 키워드:
        return False
    if all(ord(글자) < 128 for 글자 in 키워드):
        패턴 = r"(?<![0-9a-z])" + re.escape(키워드) + r"(?![0-9a-z])"
        return _정규식(패턴).search(소문자제목) is not None
    if len(키워드) == 1 and _HANGUL_RE.match(키워드):
        패턴 = r"(?<![가-힣])" + re.escape(키워드) + r"(?![가-힣])"
        return _정규식(패턴).search(소문자제목) is not None
    return 키워드 in 소문자제목


def _첫키워드(소문자제목, 키워드들):
    """제목에 걸린 키워드 가운데 가장 긴 것을 돌려준다. 없으면 None 이다.

    ai 보다 오픈ai 가 사람이 보기에 더 또렷해서 긴 쪽을 먼저 본다.
    길이가 같으면 설정에 적힌 차례를 따른다.
    """
    차례대로 = sorted(
        enumerate(키워드들),
        key=lambda 짝: (-len(짝[1] or ""), 짝[0]),
    )
    for _, 키워드 in 차례대로:
        if _키워드있나(소문자제목, 키워드):
            return 키워드
    return None


def _키워드수(소문자제목, 키워드들):
    """제목에 걸린 키워드가 몇 개인지 센다."""
    return sum(1 for 키워드 in 키워드들 if _키워드있나(소문자제목, 키워드))


def _조사떼기(토큰):
    """한글 토큰 끝에 붙은 조사를 떼어 낸다. 너무 짧아지면 그대로 둔다.

    떼고 남은 몸통이 영문이나 숫자면 한 글자라도 떼어 낸다.
    AI를 과 AI 가 다른 낱말로 갈리면 같은 사건이 안 묶이기 때문이다.
    """
    if not _HANGUL_RE.search(토큰):
        return 토큰
    for 꼬리 in _JOSA:
        if not 토큰.endswith(꼬리):
            continue
        몸통 = 토큰[: -len(꼬리)]
        if len(몸통) >= 2:
            return 몸통
        if 몸통 and all(글자.isascii() and 글자.isalnum() for 글자 in 몸통):
            return 몸통
    return 토큰


def _토큰(제목, 불용어):
    """제목을 비교하기 좋은 토큰 집합으로 바꾼다."""
    낱말 = [조각.lower() for 조각 in _TOKEN_RE.findall(제목 or "")]
    다듬은것 = {_조사떼기(낱말하나) for 낱말하나 in 낱말}
    골라낸것 = {
        낱말하나 for 낱말하나 in 다듬은것
        if len(낱말하나) >= 2 and 낱말하나 not in 불용어
    }
    if 골라낸것:
        return 골라낸것
    # 걸러내고 나니 아무것도 안 남으면 다듬기 전 낱말을 그대로 쓴다.
    return set(낱말) or {(제목 or "").strip().lower()}


def _자카드(왼쪽, 오른쪽):
    """두 토큰 집합이 얼마나 겹치는지 본다."""
    if not 왼쪽 or not 오른쪽:
        return 0.0
    겹침 = len(왼쪽 & 오른쪽)
    if not 겹침:
        return 0.0
    return 겹침 / len(왼쪽 | 오른쪽)


def _매체(기사):
    """기사에서 매체 이름을 꺼낸다. 없으면 링크에서 뽑아 본다."""
    매체이름 = (기사.get("domain") or "").strip().lower()
    if 매체이름:
        return 매체이름
    try:
        return (urllib.parse.urlparse(기사.get("url") or "").netloc or "").lower()
    except ValueError:
        return ""


def _제목고르기(기사):
    """판단에 쓸 제목을 고른다. 다듬은 제목이 없으면 원문 제목을 쓴다."""
    return (기사.get("title") or 기사.get("title_raw") or "").strip()


def _제목다듬기(제목):
    """한 줄 문장에 넣을 수 있게 제목의 군더더기를 걷어 낸다."""
    다듬은것 = _글다듬기(제목)
    for _ in range(3):
        새것 = _TAG_RE.sub("", 다듬은것)
        if 새것 == 다듬은것:
            break
        다듬은것 = 새것
    다듬은것 = _BAR_TAIL_RE.sub("", 다듬은것)
    다듬은것 = _DASH_TAIL_RE.sub("", 다듬은것)
    다듬은것 = re.sub(r"\s+", " ", 다듬은것).strip()
    다듬은것 = _TAIL_PUNCT_RE.sub("", 다듬은것)
    if len(다듬은것) > 60:
        잘린것 = 다듬은것[:60]
        빈칸 = 잘린것.rfind(" ")
        if 빈칸 >= 30:
            잘린것 = 잘린것[:빈칸]
        다듬은것 = _TAIL_PUNCT_RE.sub("", 잘린것)
    return 다듬은것


def _한줄문장(대표제목, 매체수):
    """묶음에 붙일 한 줄 한국어 문장을 만든다."""
    속 = _제목다듬기(대표제목)
    if not 속:
        속 = "제목을 알 수 없는 기사"
    # 제목이 서술형으로 끝나면 그대로 이어 붙이고, 아니면 따옴표로 묶는다.
    # 선보여 나 발표 처럼 끊긴 제목을 그냥 이으면 말이 어그러지기 때문이다.
    if 속.endswith("다"):
        if 매체수 <= 1:
            return f"{속}고 한 곳이 전했다"
        return f"{속}고 {매체수}개 매체가 전했다"
    if 매체수 <= 1:
        return f"'{속}' 소식이 한 곳에서 나왔다"
    return f"'{속}' 소식을 {매체수}개 매체가 다뤘다"


# ---------------------------------------------------------------------------
# 규칙 백엔드
# ---------------------------------------------------------------------------

def _규칙으로판단(기사들, 설정):
    """설정에 적힌 키워드만 보고 거르고 묶는다. 네트워크를 타지 않는다."""
    남김, 버림, 남은기사 = [], [], []
    본링크 = set()

    for 기사 in (기사들 or []):
        if not isinstance(기사, dict):
            continue
        링크 = (기사.get("url") or "").strip()
        제목 = _제목고르기(기사)
        소문자제목 = 제목.lower()

        if not 링크:
            버림.append({"url": "", "reason": "링크가 없어 원문을 확인할 길이 없어 뺐다"})
            continue
        if not 제목:
            버림.append({"url": 링크, "reason": "제목이 비어 있어 무슨 기사인지 가릴 수 없어 뺐다"})
            continue
        if 링크 in 본링크:
            버림.append({"url": 링크, "reason": "같은 링크가 앞에 이미 있어 한 번만 남겼다"})
            continue
        본링크.add(링크)

        제외어 = _첫키워드(소문자제목, 설정["exclude"])
        if 제외어:
            조사 = _조사(제외어, "이", "가")
            버림.append({
                "url": 링크,
                "reason": f"제목에 '{제외어}'{조사} 있어 AI 소식이 아니라고 봤다",
            })
            continue

        관심어 = _첫키워드(소문자제목, 설정["interest"])
        if not 관심어:
            버림.append({
                "url": 링크,
                "reason": "제목에 AI나 데이터 이야기가 보이지 않아 뺐다",
            })
            continue

        조사 = _조사(관심어, "이", "가")
        남김.append({
            "url": 링크,
            "reason": f"제목에 '{관심어}'{조사} 있어 AI 소식으로 봤다",
        })
        남은기사.append({
            "url": 링크,
            "title": 제목,
            "lower": 소문자제목,
            "domain": _매체(기사),
        })

    묶음 = _묶기(남은기사, 설정)
    return {
        "kept": 남김,
        "dropped": 버림,
        "groups": 묶음,
        "backend": "rules",
        "requested_backend": "rules",
        "fallback_reason": None,
    }


def _묶기(남은기사, 설정):
    """제목 토큰이 겹치는 기사끼리 묶는다."""
    임계값 = 설정["threshold"]
    불용어 = set(설정["stopwords"])
    무리들 = []

    for 순번, 기사 in enumerate(남은기사):
        토큰 = _토큰(기사["title"], 불용어)
        가장닮은무리, 가장높은값 = None, 0.0
        for 무리 in 무리들:
            닮은값 = max(_자카드(토큰, 식구["tokens"]) for 식구 in 무리["members"])
            if 닮은값 >= 임계값 and 닮은값 > 가장높은값:
                가장닮은무리, 가장높은값 = 무리, 닮은값
        식구 = dict(기사, tokens=토큰, order=순번)
        if 가장닮은무리 is None:
            무리들.append({"members": [식구], "order": 순번})
        else:
            가장닮은무리["members"].append(식구)

    빚은묶음 = [_묶음만들기(무리, 설정) for 무리 in 무리들]
    # 다룬 매체가 많은 순서로 놓되 같으면 먼저 나온 순서를 지킨다.
    빚은묶음.sort(key=lambda 묶음: (-묶음["_domains"], -묶음["_size"], 묶음["_order"]))
    for 묶음 in 빚은묶음:
        묶음.pop("_domains", None)
        묶음.pop("_size", None)
        묶음.pop("_order", None)
    return 빚은묶음


def _묶음만들기(무리, 설정):
    """무리 하나를 계약에 맞는 묶음 하나로 빚는다."""
    식구들 = 무리["members"]
    대표 = _대표고르기(식구들)
    매체들 = {식구["domain"] for 식구 in 식구들 if 식구["domain"]}
    매체수 = len(매체들) or len(식구들)
    종류 = _종류가리기([식구["lower"] for 식구 in 식구들], 설정)
    return {
        "headline": _한줄문장(대표["title"], 매체수),
        "urls": [식구["url"] for 식구 in 식구들],
        "kind": 종류,
        "_domains": len(매체들),
        "_size": len(식구들),
        "_order": 무리["order"],
    }


def _대표고르기(식구들):
    """무리 한가운데에 있는 기사를 대표로 삼는다."""
    if len(식구들) == 1:
        return 식구들[0]
    가장좋은것, 가장높은점수 = 식구들[0], -1.0
    for 식구 in 식구들:
        점수 = sum(
            _자카드(식구["tokens"], 다른식구["tokens"])
            for 다른식구 in 식구들 if 다른식구 is not 식구
        )
        if 점수 > 가장높은점수 + 1e-9:
            가장좋은것, 가장높은점수 = 식구, 점수
    return 가장좋은것


def _종류가리기(소문자제목들, 설정):
    """묶음이 규제 소식인지 새 도구 소식인지 가린다."""
    규제점수 = sum(_키워드수(제목, 설정["regulation"]) for 제목 in 소문자제목들)
    도구점수 = sum(_키워드수(제목, 설정["tool"]) for 제목 in 소문자제목들)
    if 규제점수 and 규제점수 >= 도구점수:
        return "policy"
    if 도구점수:
        return "tool"
    return "other"


# ---------------------------------------------------------------------------
# LLM 백엔드
# ---------------------------------------------------------------------------

def _call_anthropic(payload, api_key, timeout=60):
    """앤스로픽 메시지 API 를 부른다. 표준 라이브러리만 쓴다.

    테스트에서는 이 함수를 갈아 끼워 네트워크를 타지 않는다.
    """
    요청 = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    # 컨텍스트를 꼭 넘긴다. 맥에 python.org 파이썬을 깔면 CA 묶음이 비어 있어
    # 기본 컨텍스트로는 손잡기에서 끊긴다. netssl 이 운영체제 묶음을 찾아 얹고
    # 검증은 그대로 켜 둔다.
    with urllib.request.urlopen(
        요청, timeout=timeout, context=netssl.ssl_context()
    ) as 응답:
        return json.loads(응답.read().decode("utf-8"))


def _프롬프트(후보들):
    """모델에게 넘길 제목 목록과 지시문을 만든다."""
    줄들 = [f"{순번}. {기사['title']}" for 순번, 기사 in enumerate(후보들, start=1)]
    지시문 = (
        "너는 한국 AI 뉴스 편집자다. 아래 기사 제목만 보고 세 가지를 해라.\n"
        "첫째, 각 제목이 AI나 데이터 소식인지 가려라. 아니면 채택을 false 로 두고 "
        "왜 뺐는지 이유를 한국어 한 문장으로 적어라.\n"
        "둘째, 같은 사건을 다룬 제목끼리 묶어라. 한 제목은 한 묶음에만 넣어라.\n"
        "셋째, 묶음마다 한 줄짜리 한국어 문장을 제목으로 달고, 종류를 "
        "tool 과 policy 와 other 가운데 하나로 적어라. "
        "새 도구나 모델 출시면 tool, 규제나 정책이나 법 이야기면 policy, "
        "나머지는 other 다.\n"
        "문장은 사람이 쓴 것처럼 자연스럽게 써라. 줄표와 이모지를 쓰지 마라.\n"
        "설명을 덧붙이지 말고 JSON 만 내놔라. 형식은 이렇다.\n"
        '{"판단": [{"번호": 1, "채택": true, "이유": "한 문장"}], '
        '"묶음": [{"제목": "한 줄 문장", "번호들": [1, 2], "종류": "tool"}]}'
    )
    return 지시문, "\n".join(줄들)


def _응답에서본문(응답):
    """메시지 API 응답에서 글자 부분만 이어 붙인다."""
    덩어리들 = 응답.get("content")
    if not isinstance(덩어리들, list):
        raise ValueError("응답에 content 가 없다")
    본문 = "".join(
        덩어리.get("text", "")
        for 덩어리 in 덩어리들
        if isinstance(덩어리, dict) and 덩어리.get("type") == "text"
    )
    if not 본문.strip():
        raise ValueError("응답에 글자가 없다")
    return 본문


def _JSON뽑기(본문):
    """모델이 앞뒤로 말을 덧붙였어도 JSON 덩어리만 건져 낸다."""
    다듬은것 = 본문.strip()
    if 다듬은것.startswith("```"):
        다듬은것 = re.sub(r"^```[a-zA-Z]*\s*", "", 다듬은것)
        다듬은것 = re.sub(r"\s*```$", "", 다듬은것)
    try:
        return json.loads(다듬은것)
    except json.JSONDecodeError:
        처음 = 다듬은것.find("{")
        끝 = 다듬은것.rfind("}")
        if 처음 == -1 or 끝 <= 처음:
            raise ValueError("응답에서 JSON 을 찾지 못했다")
        return json.loads(다듬은것[처음:끝 + 1])


def _LLM으로판단(기사들, 설정):
    """모델에게 제목을 보여 주고 같은 구조를 받아 온다.

    잘 받아오면 결과 딕셔너리를 돌려주고, 어디서든 어긋나면 (None, 이유) 를 돌려준다.
    """
    열쇠 = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not 열쇠:
        return None, "ANTHROPIC_API_KEY 가 없어 규칙으로 판단했다"

    후보들, 버림 = [], []
    본링크 = set()
    for 기사 in (기사들 or []):
        if not isinstance(기사, dict):
            continue
        링크 = (기사.get("url") or "").strip()
        제목 = _제목고르기(기사)
        if not 링크:
            버림.append({"url": "", "reason": "링크가 없어 원문을 확인할 길이 없어 뺐다"})
            continue
        if not 제목:
            버림.append({"url": 링크, "reason": "제목이 비어 있어 무슨 기사인지 가릴 수 없어 뺐다"})
            continue
        if 링크 in 본링크:
            버림.append({"url": 링크, "reason": "같은 링크가 앞에 이미 있어 한 번만 남겼다"})
            continue
        본링크.add(링크)
        후보들.append({
            "url": 링크,
            "title": 제목,
            "lower": 제목.lower(),
            "domain": _매체(기사),
        })

    if not 후보들:
        return {
            "kept": [],
            "dropped": 버림,
            "groups": [],
            "backend": "llm",
            "requested_backend": "llm",
            "fallback_reason": None,
        }, None

    지시문, 제목목록 = _프롬프트(후보들)
    보낼것 = {
        "model": 설정["model"],
        "max_tokens": 설정["max_tokens"],
        "system": 지시문,
        "messages": [{"role": "user", "content": 제목목록}],
    }

    try:
        응답 = _call_anthropic(보낼것, 열쇠, timeout=설정["timeout"])
    except urllib.error.HTTPError as 오류:
        return None, f"모델 호출이 {오류.code} 로 막혀 규칙으로 판단했다"
    except Exception as 오류:  # 네트워크든 시간초과든 여기서 받아 낸다
        return None, f"모델 호출이 {type(오류).__name__} 로 실패해 규칙으로 판단했다"

    try:
        속내용 = _JSON뽑기(_응답에서본문(응답))
        결과 = _LLM결과빚기(속내용, 후보들, 버림, 설정)
    except Exception as 오류:
        return None, f"모델 응답 형식이 어긋나 규칙으로 판단했다 ({type(오류).__name__})"
    return 결과, None


def _LLM결과빚기(속내용, 후보들, 미리버림, 설정):
    """모델이 준 JSON 을 계약에 맞는 구조로 옮긴다."""
    if not isinstance(속내용, dict):
        raise ValueError("JSON 이 객체가 아니다")
    판단들 = 속내용.get("판단") or 속내용.get("decisions")
    if not isinstance(판단들, list) or not 판단들:
        raise ValueError("판단 목록이 없다")

    말한것 = {}
    for 한줄 in 판단들:
        if not isinstance(한줄, dict):
            continue
        try:
            번호 = int(한줄.get("번호", 한줄.get("index", 0)))
        except (TypeError, ValueError):
            continue
        if not 1 <= 번호 <= len(후보들):
            continue
        채택 = 한줄.get("채택", 한줄.get("keep"))
        이유 = str(한줄.get("이유") or 한줄.get("reason") or "").strip()
        말한것[번호] = (bool(채택), 이유)

    if not 말한것:
        raise ValueError("쓸 만한 판단이 하나도 없다")

    남김, 버림 = [], list(미리버림)
    남은기사 = []
    for 순번, 기사 in enumerate(후보들, start=1):
        채택, 이유 = 말한것.get(순번, (False, ""))
        if 채택:
            남김.append({"url": 기사["url"], "reason": 이유 or "모델이 AI 소식으로 봤다"})
            남은기사.append(기사)
        else:
            if not 이유:
                이유 = (
                    "모델이 AI 소식이 아니라고 봤다" if 순번 in 말한것
                    else "모델이 따로 언급하지 않아 뺐다"
                )
            버림.append({"url": 기사["url"], "reason": 이유})

    남은링크 = {기사["url"] for 기사 in 남은기사}
    기사찾기 = {기사["url"]: 기사 for 기사 in 남은기사}
    묶음들, 이미묶인것 = [], set()

    for 한묶음 in (속내용.get("묶음") or 속내용.get("groups") or []):
        if not isinstance(한묶음, dict):
            continue
        번호들 = 한묶음.get("번호들") or 한묶음.get("indexes") or []
        if not isinstance(번호들, (list, tuple)):
            continue
        링크들 = []
        for 번호 in 번호들:
            try:
                번호 = int(번호)
            except (TypeError, ValueError):
                continue
            if not 1 <= 번호 <= len(후보들):
                continue
            링크 = 후보들[번호 - 1]["url"]
            if 링크 in 남은링크 and 링크 not in 이미묶인것:
                링크들.append(링크)
                이미묶인것.add(링크)
        if not 링크들:
            continue
        종류 = str(한묶음.get("종류") or 한묶음.get("kind") or "").strip().lower()
        if 종류 not in _KIND_VALUES:
            종류 = _종류가리기([기사찾기[링크]["lower"] for 링크 in 링크들], 설정)
        # 모델에게 줄표와 이모지를 쓰지 말라고 부탁하지만 어길 때가 있다.
        # 부탁만 하고 검사하지 않으면 어긴 문장이 그대로 DB 에 남는다.
        한줄 = _글다듬기(한묶음.get("제목") or 한묶음.get("headline") or "")
        if not 한줄:
            매체들 = {기사찾기[링크]["domain"] for 링크 in 링크들 if 기사찾기[링크]["domain"]}
            한줄 = _한줄문장(기사찾기[링크들[0]]["title"], len(매체들) or len(링크들))
        묶음들.append({"headline": 한줄, "urls": 링크들, "kind": 종류})

    # 모델이 묶음에 넣지 않고 흘린 기사는 규칙으로 묶어 붙인다.
    흘린것 = [기사 for 기사 in 남은기사 if 기사["url"] not in 이미묶인것]
    if 흘린것:
        묶음들.extend(_묶기(흘린것, 설정))

    return {
        "kept": 남김,
        "dropped": 버림,
        "groups": 묶음들,
        "backend": "llm",
        "requested_backend": "llm",
        "fallback_reason": None,
    }


# ---------------------------------------------------------------------------
# 바깥에서 부르는 함수
# ---------------------------------------------------------------------------

def judge_articles(articles, cfg, backend="rules"):
    """제목만 보고 기사를 거르고 묶어서 돌려준다.

    articles 는 collect 가 준 목록이다. 항목은 url 과 title 을 가지고 있어야 한다.
    backend 는 rules 와 llm 둘이다. llm 은 열쇠가 있을 때만 돌고, 안 되면 rules 로 내려앉는다.
    돌려주는 값은 kept 와 dropped 와 groups 를 담은 딕셔너리다.
    어느 백엔드로 판단했는지는 backend 에, 내려앉은 까닭은 fallback_reason 에 적힌다.
    """
    설정 = _규칙설정(cfg)
    바란백엔드 = str(backend or "rules").strip().lower()
    내려앉은까닭 = None

    if 바란백엔드 == "llm":
        결과, 까닭 = _LLM으로판단(articles, 설정)
        if 결과 is not None:
            return 결과
        내려앉은까닭 = 까닭
        로그.warning("LLM 백엔드를 쓰지 못했다. %s", 까닭)
    elif 바란백엔드 != "rules":
        내려앉은까닭 = f"{바란백엔드} 라는 백엔드는 없어 규칙으로 판단했다"
        로그.warning("모르는 백엔드를 받았다. %s", 내려앉은까닭)

    결과 = _규칙으로판단(articles, 설정)
    결과["requested_backend"] = 바란백엔드
    결과["fallback_reason"] = 내려앉은까닭
    return 결과


# ---------------------------------------------------------------------------
# 판단 결과 남기기
# ---------------------------------------------------------------------------

def _스키마만들기(conn):
    """판단을 담을 표가 없으면 만든다. 다른 모듈의 표는 건드리지 않는다."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS judge_runs (
            date TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            requested_backend TEXT,
            fallback_reason TEXT,
            saved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS judge_decisions (
            date TEXT NOT NULL,
            position INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            url TEXT,
            reason TEXT,
            PRIMARY KEY (date, position)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS judge_groups (
            date TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            headline TEXT,
            kind TEXT,
            PRIMARY KEY (date, group_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS judge_group_urls (
            date TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (date, group_id, position)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_judge_decisions_date "
        "ON judge_decisions (date, verdict)"
    )


def _판정줄(항목):
    """저장하기 좋게 한 줄을 다듬는다."""
    if not isinstance(항목, dict):
        return {"url": "", "reason": ""}
    return {
        "url": str(항목.get("url") or ""),
        "reason": str(항목.get("reason") or ""),
    }


def save_decisions(conn, date_str, result):
    """하루치 판단을 DB 에 남긴다. 같은 날짜가 이미 있으면 새것으로 갈아 끼운다."""
    if conn is None:
        raise ValueError("DB 연결이 없어 판단을 남기지 못한다")
    결과 = result or {}
    남김 = [_판정줄(항목) for 항목 in (결과.get("kept") or [])]
    버림 = [_판정줄(항목) for 항목 in (결과.get("dropped") or [])]
    묶음들 = [항목 for 항목 in (결과.get("groups") or []) if isinstance(항목, dict)]

    _스키마만들기(conn)
    with conn:
        conn.execute("DELETE FROM judge_runs WHERE date = ?", (date_str,))
        conn.execute("DELETE FROM judge_decisions WHERE date = ?", (date_str,))
        conn.execute("DELETE FROM judge_groups WHERE date = ?", (date_str,))
        conn.execute("DELETE FROM judge_group_urls WHERE date = ?", (date_str,))

        conn.execute(
            "INSERT INTO judge_runs "
            "(date, backend, requested_backend, fallback_reason, saved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                date_str,
                str(결과.get("backend") or "rules"),
                str(결과.get("requested_backend") or 결과.get("backend") or "rules"),
                결과.get("fallback_reason"),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

        자리 = 0
        for 항목 in 남김:
            conn.execute(
                "INSERT INTO judge_decisions (date, position, verdict, url, reason) "
                "VALUES (?, ?, 'kept', ?, ?)",
                (date_str, 자리, 항목["url"], 항목["reason"]),
            )
            자리 += 1
        for 항목 in 버림:
            conn.execute(
                "INSERT INTO judge_decisions (date, position, verdict, url, reason) "
                "VALUES (?, ?, 'dropped', ?, ?)",
                (date_str, 자리, 항목["url"], 항목["reason"]),
            )
            자리 += 1

        for 묶음번호, 묶음 in enumerate(묶음들):
            종류 = str(묶음.get("kind") or "other").strip().lower()
            if 종류 not in _KIND_VALUES:
                종류 = "other"
            conn.execute(
                "INSERT INTO judge_groups (date, group_id, headline, kind) "
                "VALUES (?, ?, ?, ?)",
                (date_str, 묶음번호, _글다듬기(묶음.get("headline")), 종류),
            )
            for 링크자리, 링크 in enumerate(묶음.get("urls") or []):
                conn.execute(
                    "INSERT INTO judge_group_urls (date, group_id, position, url) "
                    "VALUES (?, ?, ?, ?)",
                    (date_str, 묶음번호, 링크자리, str(링크)),
                )

    로그.info(
        "%s 판단을 남겼다. 남긴 기사 %d건, 뺀 기사 %d건, 묶음 %d개다",
        date_str, len(남김), len(버림), len(묶음들),
    )
    return {
        "date": date_str,
        "kept": len(남김),
        "dropped": len(버림),
        "groups": len(묶음들),
    }


def load_decisions(conn, date_str):
    """저장해 둔 하루치 판단을 그대로 읽어 온다. 없으면 None 이다."""
    if conn is None:
        return None
    _스키마만들기(conn)
    한줄 = conn.execute(
        "SELECT backend, requested_backend, fallback_reason "
        "FROM judge_runs WHERE date = ?",
        (date_str,),
    ).fetchone()
    if 한줄 is None:
        return None
    백엔드, 바란백엔드, 내려앉은까닭 = 한줄[0], 한줄[1], 한줄[2]

    남김, 버림 = [], []
    for 판정, 링크, 이유 in conn.execute(
        "SELECT verdict, url, reason FROM judge_decisions "
        "WHERE date = ? ORDER BY position",
        (date_str,),
    ):
        항목 = {"url": 링크 or "", "reason": 이유 or ""}
        (남김 if 판정 == "kept" else 버림).append(항목)

    링크모음 = {}
    for 묶음번호, 링크 in conn.execute(
        "SELECT group_id, url FROM judge_group_urls "
        "WHERE date = ? ORDER BY group_id, position",
        (date_str,),
    ):
        링크모음.setdefault(묶음번호, []).append(링크)

    묶음들 = [
        {"headline": 한줄문장 or "", "urls": 링크모음.get(묶음번호, []), "kind": 종류 or "other"}
        for 묶음번호, 한줄문장, 종류 in conn.execute(
            "SELECT group_id, headline, kind FROM judge_groups "
            "WHERE date = ? ORDER BY group_id",
            (date_str,),
        )
    ]

    return {
        "kept": 남김,
        "dropped": 버림,
        "groups": 묶음들,
        "backend": 백엔드 or "rules",
        "requested_backend": 바란백엔드 or 백엔드 or "rules",
        "fallback_reason": 내려앉은까닭,
    }


def _다룬링크들(저장된것):
    """저장된 판단이 다룬 링크를 차례대로 늘어놓는다.

    남긴 기사와 뺀 기사를 더하면 그날 판단에 들어간 기사 전부가 된다.
    링크가 없어 뺀 기사는 빈 문자열로 남아 있어 건수도 그대로 맞는다.
    """
    항목들 = list(저장된것.get("kept") or []) + list(저장된것.get("dropped") or [])
    return sorted(str(항목.get("url") or "") for 항목 in 항목들 if isinstance(항목, dict))


def _넘긴링크들(articles):
    """이번에 판단하라고 넘긴 기사의 링크를 같은 방식으로 늘어놓는다."""
    return sorted(
        str(기사.get("url") or "").strip()
        for 기사 in (articles or [])
        if isinstance(기사, dict)
    )


def _다시판단할까닭(저장된것, articles, backend):
    """저장된 판단을 그대로 쓰면 안 되는 까닭을 찾는다. 없으면 None 이다."""
    바란백엔드 = str(backend or "rules").strip().lower()
    저장된백엔드 = str(저장된것.get("requested_backend") or 저장된것.get("backend") or "rules")
    if 바란백엔드 != 저장된백엔드.strip().lower():
        return "저장된 판단은 %s 로 한 것인데 이번에는 %s 를 달라고 했다" % (
            저장된백엔드,
            바란백엔드,
        )

    저장된링크 = _다룬링크들(저장된것)
    넘긴링크 = _넘긴링크들(articles)
    if 저장된링크 == 넘긴링크:
        return None
    if len(저장된링크) != len(넘긴링크):
        return "판단할 기사가 %d건에서 %d건으로 달라졌다" % (len(저장된링크), len(넘긴링크))
    return "기사 건수는 같은데 링크 묶음이 저장된 판단과 다르다"


def judge_day(conn, date_str, articles, cfg, backend="rules", force=False):
    """하루치를 판단하되 이미 남겨 둔 판단이 있으면 그것을 그대로 쓴다.

    같은 날을 다시 돌려도 숫자가 흔들리지 않게 하려는 것이다.
    다시 판단하게 하려면 force 를 참으로 준다.
    돌려주는 값에는 저장된 판단을 읽어 썼는지 reused 로 적어 둔다.

    저장된 판단을 그대로 쓰는 것은 넘긴 기사가 저장할 때와 같을 때뿐이다.
    같은 날짜를 다시 수집해 기사가 늘어난 날에도 옛 판단을 쓰면, 새로 들어온
    기사가 브리핑에 한 건도 오르지 않는다. 그건 숫자를 지키는 것이 아니라
    틀린 숫자를 지키는 것이다. 링크 묶음이 달라졌거나 요청한 백엔드가
    달라졌으면 다시 판단하고, 그 까닭을 로그와 rejudged_reason 에 남긴다.
    """
    if not force:
        저장된것 = load_decisions(conn, date_str)
        if 저장된것 is not None:
            까닭 = _다시판단할까닭(저장된것, articles, backend)
            if 까닭 is None:
                로그.info("%s 판단이 이미 있어 저장된 것을 그대로 쓴다", date_str)
                저장된것["reused"] = True
                return 저장된것
            로그.warning(
                "%s 판단이 남아 있지만 다시 판단한다. 사유는 %s 다", date_str, 까닭
            )
            결과 = judge_articles(articles, cfg, backend=backend)
            save_decisions(conn, date_str, 결과)
            결과["reused"] = False
            결과["rejudged_reason"] = 까닭
            return 결과

    결과 = judge_articles(articles, cfg, backend=backend)
    save_decisions(conn, date_str, 결과)
    결과["reused"] = False
    return 결과
