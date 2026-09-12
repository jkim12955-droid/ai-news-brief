"""설정을 한 덩어리로 모으고, 한국 날짜를 GDELT 가 알아듣는 UTC 구간으로 바꾼다.

다른 모듈은 여기서 나온 dict 하나만 들고 다닌다.
설정은 세 겹으로 쌓인다. 코드에 박아 둔 기본값이 맨 아래,
config/settings.json 과 config/keywords.json 이 그 위, 환경변수가 맨 위다.
설정 파일이 통째로 없어도 기본값으로 돌아가게 해 두었다. 처음 받아 본 사람이
파일부터 만들지 않아도 한 번은 돌려 볼 수 있어야 하기 때문이다.

비밀값은 이 모듈이 읽지 않는다. 웹훅 주소와 토큰은 notify 와 judge 가 필요할 때
환경변수에서 직접 읽는다. 여기서는 값이 있는지 없는지만 참거짓으로 적어 둔다.
설정 dict 가 로그나 파일로 새어 나가도 비밀값이 따라 나가지 않게 하려는 것이다.
"""

import copy
import datetime as dt
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("news_brief.config")

# GDELT 가 받는 시각 표기. 한국 날짜를 이 모양의 UTC 문자열 두 개로 바꿔 준다.
WINDOW_FORMAT = "%Y%m%d%H%M%S"

# 한국은 서머타임을 쓰지 않는다. zoneinfo 를 못 쓰는 자리에서만 이 값으로 버틴다.
KST_OFFSET_HOURS = 9

CONFIG_DIR_NAME = "config"
SETTINGS_FILE_NAME = "settings.json"
KEYWORDS_FILE_NAME = "keywords.json"

# 설정 파일이 없을 때 쓰는 값이다. config/settings.json 과 같은 값을 적어 둔다.
DEFAULT_SETTINGS = {
    "query": '("artificial intelligence" OR OpenAI) sourcelang:korean',
    "maxrecords": 250,
    "sleep_seconds": 5,
    "max_split_depth": 4,
    "judge": {
        "backend": "rules",
        "model": "claude-opus-5",
        "similarity_threshold": 0.4,
        "max_tokens": 4000,
        "timeout_seconds": 60,
    },
    "brief": {
        "min_groups": 3,
        "max_groups": 5,
        "max_keywords": 8,
        "first_seen_lookback_days": 7,
        "average_window_days": 7,
    },
    "report": {
        "top_keywords": 5,
        "font_candidates": [
            "Apple SD Gothic Neo",
            "AppleGothic",
            "Malgun Gothic",
            "NanumGothic",
            "Noto Sans CJK KR",
        ],
    },
    "qa": {
        "lookback_days": 7,
        "volume_low_ratio": 0.5,
        "volume_high_ratio": 2.0,
        "volume_min_average": 3.0,
        "max_drop_ratio": 0.7,
        "max_stray_ratio": 0.1,
        "link_check": {
            "enabled": False,
            "sample": 3,
            "timeout_seconds": 10,
            "min_overlap": 0.2,
        },
    },
    "notify": {
        "dry_run": True,
        "slack_enabled": True,
        "notion_enabled": False,
    },
    "paths": {
        "data_dir": "data",
        "db": "data/news.db",
        "briefs_dir": "data/briefs",
        "reports_dir": "data/reports",
        "raw_dir": "data/raw",
    },
}

# 절대 경로로 펴 줄 설정들. 상대 경로로 적혀 있으면 프로젝트 뿌리에 붙인다.
_PATH_KEYS = ("data_dir", "db", "briefs_dir", "reports_dir", "raw_dir")

# 환경변수로 덮어쓸 수 있는 값이다. 오른쪽은 설정 안의 자리와 값을 바꿀 방법이다.
ENV_OVERRIDES = {
    "NEWS_BRIEF_QUERY": ("query", "str"),
    "NEWS_BRIEF_MAXRECORDS": ("maxrecords", "int"),
    "NEWS_BRIEF_SLEEP_SECONDS": ("sleep_seconds", "float"),
    "NEWS_BRIEF_MAX_SPLIT_DEPTH": ("max_split_depth", "int"),
    "NEWS_BRIEF_BACKEND": ("judge.backend", "str"),
    "NEWS_BRIEF_MODEL": ("judge.model", "str"),
    "NEWS_BRIEF_SIMILARITY": ("judge.similarity_threshold", "float"),
    "NEWS_BRIEF_LOOKBACK_DAYS": ("brief.first_seen_lookback_days", "int"),
    "NEWS_BRIEF_TOP_KEYWORDS": ("report.top_keywords", "int"),
    "NEWS_BRIEF_DRY_RUN": ("notify.dry_run", "bool"),
    "NEWS_BRIEF_SLACK_ENABLED": ("notify.slack_enabled", "bool"),
    "NEWS_BRIEF_NOTION_ENABLED": ("notify.notion_enabled", "bool"),
    "NEWS_BRIEF_DATA_DIR": ("paths.data_dir", "path"),
    "NEWS_BRIEF_DB": ("paths.db", "path"),
}

# 위 표에 없는 값을 급히 바꿔야 할 때 쓰는 뒷문이다.
# NEWS_BRIEF_CFG__judge__model 처럼 두 밑줄로 자리를 잇는다.
ENV_FREE_PREFIX = "NEWS_BRIEF_CFG__"

# 있는지 없는지만 확인할 비밀값들. 값 자체는 설정에 담지 않는다.
SECRET_ENV_NAMES = {
    "slack_webhook": "SLACK_WEBHOOK_URL",
    "notion_token": "NOTION_TOKEN",
    "notion_parent_page_id": "NOTION_PARENT_PAGE_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}

_TRUE_WORDS = ("1", "true", "yes", "y", "on", "참", "예")
_FALSE_WORDS = ("0", "false", "no", "n", "off", "거짓", "아니오")


# ---------------------------------------------------------------------------
# 시간 다루기
# ---------------------------------------------------------------------------

def seoul_timezone():
    """한국 시간대를 돌려준다. 시간대 자료가 없는 곳에서는 고정 +9 로 버틴다."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Seoul")
    except Exception:
        logger.warning(
            "Asia/Seoul 시간대 자료를 찾지 못해 고정 +%d시간으로 대신한다. "
            "한국은 서머타임이 없어 결과는 같지만, tzdata 를 넣어 두는 편이 낫다.",
            KST_OFFSET_HOURS,
        )
        return dt.timezone(dt.timedelta(hours=KST_OFFSET_HOURS))


def _as_date(value):
    """'YYYY-MM-DD' 문자열이나 날짜 객체를 date 로 바꾼다."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("날짜가 비어 있다. 'YYYY-MM-DD' 모양으로 넘겨야 한다.")
    # 붙여 쓴 '20260912' 도 파이썬은 날짜로 읽어 주지만 여기서는 막는다.
    # GDELT 가 쓰는 시각 표기와 한국 날짜가 섞여 들어오면 하루가 어긋나도 알아채기 어렵다.
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError(
            "날짜는 'YYYY-MM-DD' 모양이어야 한다. 받은 값은 {0} 다.".format(text)
        )
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            "날짜를 알아보지 못했다. 'YYYY-MM-DD' 모양이어야 하는데 받은 값은 {0} 다.".format(text)
        ) from None


def kst_day_window(date_str):
    """한국 날짜 하루를 UTC 구간 두 개로 바꾼다.

    받는 값은 'YYYY-MM-DD' 한국 날짜이고, 돌려주는 값은 'YYYYMMDDHHMMSS' UTC 문자열 둘이다.
    구간은 그 날 0시 0분 0초부터 23시 59분 59초까지로, 양쪽 끝을 모두 포함한다.
    끝을 다음 날 0시로 잡지 않은 것은 하루의 마지막 1초가 이튿날 구간과 겹치지 않게 하려는 것이다.

    한국은 서머타임을 쓰지 않아서 차이는 늘 아홉 시간이다. 그래도 시간대를 직접 빼지 않고
    zoneinfo 에 맡긴 것은, 나중에 다른 나라 시간대를 볼 때 이 함수만 고치면 되게 하려는 것이다.
    """
    day = _as_date(date_str)
    tz = seoul_timezone()
    start_kst = dt.datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end_kst = dt.datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)
    start_utc = start_kst.astimezone(dt.timezone.utc).strftime(WINDOW_FORMAT)
    end_utc = end_kst.astimezone(dt.timezone.utc).strftime(WINDOW_FORMAT)
    return start_utc, end_utc


def kst_today():
    """지금 한국 날짜를 'YYYY-MM-DD' 로 돌려준다."""
    return dt.datetime.now(seoul_timezone()).date().isoformat()


def shift_date(date_str, days):
    """날짜를 며칠 옮긴다. 어제나 지난주를 구할 때 쓴다."""
    return (_as_date(date_str) + dt.timedelta(days=int(days))).isoformat()


def week_start_of(date_str):
    """그 날짜가 속한 주의 월요일을 돌려준다."""
    day = _as_date(date_str)
    return (day - dt.timedelta(days=day.weekday())).isoformat()


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------

def project_root():
    """이 패키지를 품고 있는 프로젝트 뿌리."""
    return Path(__file__).resolve().parent.parent


def _read_json(path, label):
    """JSON 파일 하나를 읽는다. 없으면 빈 dict, 깨져 있으면 멈춘다."""
    if not path.exists():
        logger.warning("%s 파일이 없다(%s). 기본값으로 돌린다.", label, path)
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(
            "{0} 파일을 읽지 못했다. {1} 의 {2}째 줄이 JSON 으로 맞지 않는다.".format(
                label, path, error.lineno
            )
        ) from error
    if not isinstance(loaded, dict):
        raise ValueError(
            "{0} 파일은 맨 바깥이 중괄호여야 한다. {1} 를 살펴야 한다.".format(label, path)
        )
    return loaded


def _merge(base, layer):
    """dict 두 개를 겹친다. 안쪽 dict 는 파고들고, 목록은 통째로 갈아 끼운다.

    목록을 이어 붙이지 않는 이유는, 키워드 목록에서 뭘 빼려고 파일을 고쳤는데
    기본값이 다시 섞여 들어오면 고친 사람이 이유를 찾기 어렵기 때문이다.
    """
    merged = copy.deepcopy(base)
    for key, value in (layer or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _dig_set(cfg, dotted, value):
    """점으로 이어진 자리에 값을 넣는다. 중간 자리가 없으면 만들어 가며 내려간다."""
    parts = [part for part in str(dotted).split(".") if part]
    if not parts:
        return
    target = cfg
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[parts[-1]] = value


def _to_bool(text, env_name):
    """환경변수 문자열을 참거짓으로 바꾼다."""
    lowered = str(text).strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    raise ValueError(
        "{0} 값을 참거짓으로 읽지 못했다. true 나 false 로 적어야 하는데 받은 값은 {1} 다.".format(
            env_name, text
        )
    )


def _cast(raw, kind, env_name, root):
    """환경변수 문자열을 설정에 맞는 자료형으로 바꾼다."""
    if kind == "str":
        return raw
    if kind == "bool":
        return _to_bool(raw, env_name)
    if kind == "path":
        path = Path(os.path.expanduser(raw))
        return str(path if path.is_absolute() else root / path)
    try:
        return int(raw) if kind == "int" else float(raw)
    except ValueError:
        raise ValueError(
            "{0} 값을 숫자로 읽지 못했다. 받은 값은 {1} 다.".format(env_name, raw)
        ) from None


def _apply_env(cfg, environ, root):
    """환경변수로 설정을 덮어쓴다. 덮어쓴 자리는 기록해 두었다가 로그로 보여 준다."""
    touched = []
    for env_name, (dotted, kind) in ENV_OVERRIDES.items():
        raw = environ.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        _dig_set(cfg, dotted, _cast(str(raw).strip(), kind, env_name, root))
        touched.append(dotted)

    for env_name, raw in environ.items():
        if not env_name.startswith(ENV_FREE_PREFIX):
            continue
        dotted = env_name[len(ENV_FREE_PREFIX):].replace("__", ".").lower()
        if not dotted:
            continue
        text = str(raw).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = text
        _dig_set(cfg, dotted, value)
        touched.append(dotted)

    if touched:
        logger.info("환경변수로 덮어쓴 설정: %s", ", ".join(sorted(set(touched))))
    return touched


def _resolve_paths(cfg, root):
    """경로 설정을 절대 경로로 편다. 어디서 실행해도 같은 자리를 가리키게 하려는 것이다."""
    paths = cfg.get("paths")
    if not isinstance(paths, dict):
        paths = {}
        cfg["paths"] = paths
    for key in _PATH_KEYS:
        value = paths.get(key)
        if not value:
            continue
        path = Path(os.path.expanduser(str(value)))
        paths[key] = str(path if path.is_absolute() else root / path)
    paths.setdefault("root", str(root))
    return paths


def load_config(root=None):
    """설정 파일 두 개를 읽어 한 덩어리로 돌려준다.

    root 는 프로젝트 뿌리 경로다. 비우면 이 패키지의 부모를 쓴다.
    돌려주는 dict 는 이렇게 생겼다.
        query, maxrecords, sleep_seconds, max_split_depth  수집이 쓰는 값
        judge, brief, report, qa, notify                    각 단계가 쓰는 값
        keywords                                            config/keywords.json 내용 그대로
        paths                                               절대 경로로 편 산출물 자리
        root                                                프로젝트 뿌리
        secrets                                             비밀값이 있는지만 참거짓으로
    """
    root = Path(root).expanduser().resolve() if root else project_root()
    config_dir = root / CONFIG_DIR_NAME

    settings = _read_json(config_dir / SETTINGS_FILE_NAME, "설정")
    keywords = _read_json(config_dir / KEYWORDS_FILE_NAME, "키워드")

    cfg = _merge(DEFAULT_SETTINGS, settings)
    cfg["keywords"] = keywords
    cfg["root"] = str(root)
    cfg["config_dir"] = str(config_dir)

    _apply_env(cfg, os.environ, root)
    _resolve_paths(cfg, root)

    cfg["secrets"] = {
        name: bool(str(os.environ.get(env_name, "")).strip())
        for name, env_name in SECRET_ENV_NAMES.items()
    }
    return cfg


# ---------------------------------------------------------------------------
# 설정에서 자주 꺼내 쓰는 것들
# ---------------------------------------------------------------------------

def watch_keywords(cfg):
    """리포트와 처음 보는 키워드 찾기에 쓸 관심 키워드 이름 목록.

    keywords.json 의 keywords 항목은 문자열이거나 {keyword, roman} 모양이다.
    둘 다 받아 이름만 뽑아 준다.
    """
    block = (cfg or {}).get("keywords")
    if not isinstance(block, dict):
        return []
    items = block.get("keywords") or block.get("list") or block.get("items") or []
    names = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("keyword") or item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def path_of(cfg, key):
    """paths 에 적힌 자리를 Path 로 돌려준다."""
    paths = (cfg or {}).get("paths") or {}
    value = paths.get(key)
    if value:
        return Path(str(value))
    fallback = DEFAULT_SETTINGS["paths"].get(key, key)
    return Path(str((cfg or {}).get("root") or project_root())) / fallback


def db_path(cfg):
    """sqlite 파일 자리."""
    return path_of(cfg, "db")


def brief_path(cfg, date_str):
    """그 날짜 브리핑 마크다운을 남길 자리."""
    return path_of(cfg, "briefs_dir") / "{0}.md".format(date_str)


def report_path(cfg, week_start):
    """그 주 리포트 마크다운을 남길 자리. 차트도 같은 폴더에 떨어진다."""
    return path_of(cfg, "reports_dir") / str(week_start) / "report.md"


def setting(cfg, dotted, default=None):
    """점으로 이어진 자리에서 설정값을 꺼낸다. 없으면 default 다."""
    current = cfg
    for part in str(dotted).split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
