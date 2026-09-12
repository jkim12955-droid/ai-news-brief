"""명령 한 줄로 파이프라인을 부르는 자리다.

    python -m news_brief run            어제치를 받아 브리핑까지 만든다
    python -m news_brief collect        받아서 넣기만 한다
    python -m news_brief qa             쌓인 자료가 쓸 만한지 본다
    python -m news_brief brief          이미 받아 둔 자료로 브리핑만 다시 만든다
    python -m news_brief report         지난주 리포트를 만든다

날짜를 적지 않으면 어제(한국 날짜)를 쓴다. 아침 9시에 도는 일이라 어제가 기본이다.
전송은 기본이 미리보기다. 실제로 보내려면 --no-dry-run 을 붙인다.
한 번 받은 날짜를 다시 돌려도 숫자가 흔들리지 않게, 판단은 저장해 둔 것을 다시 쓴다.
다시 판단하게 하려면 --force 를 붙인다.

run 의 단계 순서에는 까닭이 있다. 받아 온 다음 곧바로 DB 에 넣지 않고, 멈춤 조건
다섯 가지를 먼저 본다. 날짜가 어긋난 기사는 한 번 들어가면 url 을 기본키로 쓰는
store 가 kst_date 를 다시 덮어쓰지 않기 때문에, 원인을 고친 뒤에도 그 기사가
영원히 잘못된 날에 매달린다. 그래서 적재 전에 거를 것은 적재 전에 거른다.
판단도 점검보다 먼저 돌린다. 걸러낸 비율 점검은 판단 결과가 있어야 재는데,
점검이 먼저면 그 날짜의 첫 실행에서는 늘 건너뛰게 된다.
"""

import argparse
import logging
import sys

from . import config

logger = logging.getLogger("news_brief.cli")

# 종료 코드. GitHub Actions 가 이 값으로 성공과 실패를 가른다.
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_BROKEN = 2

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
_STATUS_MARK = {"ok": "정상", "warn": "주의", "fail": "실패"}


# ---------------------------------------------------------------------------
# 잔손질
# ---------------------------------------------------------------------------

def _setup_logging(verbose):
    """로그를 표준 에러로 흘린다. 표준 출력은 사람이 읽을 요약만 쓴다."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_LOG_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _module(name):
    """필요한 순간에만 모듈을 들인다. 없는 모듈 때문에 다른 명령까지 막히지 않게 한다."""
    from importlib import import_module

    try:
        return import_module("news_brief." + name)
    except ImportError as error:
        raise RuntimeError(
            "{0} 모듈을 들이지 못했다. 사유는 {1} 다.".format(name, error)
        ) from error


def _need(module, func_name):
    """계약에 적힌 함수가 실제로 있는지 확인한다."""
    func = getattr(module, func_name, None)
    if func is None:
        raise RuntimeError(
            "{0}.{1} 을 찾지 못했다. 모듈이 아직 다 만들어지지 않았다.".format(
                module.__name__.split(".")[-1], func_name
            )
        )
    return func


def _load_cfg(args):
    """설정을 읽는다. --root 를 주면 그 자리를 프로젝트 뿌리로 본다."""
    return config.load_config(getattr(args, "root", None))


def _date_of(args):
    """다룰 날짜를 정한다. 안 주면 어제다."""
    given = getattr(args, "date", None)
    if given:
        return config.shift_date(given, 0)
    return config.shift_date(config.kst_today(), -1)


def _backend_of(args, cfg):
    """판단 백엔드. 명령줄이 설정을 이긴다."""
    return getattr(args, "backend", None) or config.setting(cfg, "judge.backend", "rules")


def _dry_run_of(args, cfg):
    """전송을 미리보기로 끝낼지 정한다. 명령줄이 설정을 이긴다."""
    given = getattr(args, "dry_run", None)
    if given is not None:
        return bool(given)
    return bool(config.setting(cfg, "notify.dry_run", True))


def _open_db(cfg):
    """sqlite 를 연다. 파일이 없으면 만든다."""
    store = _module("store")
    path = config.db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _need(store, "open_db")(str(path))


def _say(line):
    """사람이 읽을 한 줄을 표준 출력으로 낸다."""
    print(line, flush=True)


# ---------------------------------------------------------------------------
# 단계별 일감
# ---------------------------------------------------------------------------

def _fetcher_of(args, date_str):
    """--fixtures 를 주면 저장해 둔 응답으로 GDELT 를 대신한다."""
    folder = getattr(args, "fixtures", None)
    if not folder:
        return None
    collect = _module("collect")
    maker = getattr(collect, "fixture_fetcher", None)
    if maker is None:
        raise RuntimeError("collect.fixture_fetcher 를 찾지 못해 픽스처로 돌릴 수 없다.")
    _say("GDELT 를 부르지 않고 {0} 에 저장해 둔 응답으로 돈다.".format(folder))
    return maker(folder, date_str)


def _collect_only(date_str, cfg, fetcher=None):
    """하루치를 받아 오기만 한다. DB 에는 아직 넣지 않는다.

    못 받은 구간은 까닭을 갈라 적는다. 250건 상한에 걸린 채 더 쪼갤 깊이가 없어
    뒤가 잘린 구간과, GDELT 가 막아 응답 자체를 받지 못한 구간은 할 일이 다르다.
    앞엣것은 검색어를 좁히거나 하루를 나눠 받아야 하고, 뒤엣것은 한동안 쉬었다가
    같은 날짜를 다시 돌리면 된다.
    """
    collect = _module("collect")
    collected = _need(collect, "collect_day")(date_str, cfg, fetcher=fetcher)
    articles = collected.get("articles") or []
    _say("{0} 기사 {1}건을 받았다.".format(date_str, len(articles)))

    def 막혔나(기록):
        return isinstance(기록, dict) and bool(기록.get("blocked"))

    truncated = collected.get("truncated_windows") or []
    막힌것 = [기록 for 기록 in truncated if 막혔나(기록)]
    상한것 = [기록 for 기록 in truncated if not 막혔나(기록)]
    if 상한것:
        _say(
            "250건 상한에 걸린 구간이 {0}개 남았다. 그만큼 놓친 기사가 있다. "
            "검색어를 좁히거나 하루를 나눠 받아야 한다.".format(len(상한것))
        )
    if 막힌것:
        _say(
            "GDELT 가 막아 받지 못한 구간이 {0}개다. 한동안 쉬었다가 같은 날짜를 "
            "다시 돌리면 그 구간만 메울 수 있다.".format(len(막힌것))
        )
    return collected


def _store_collected(conn, date_str, collected):
    """받아 둔 기사를 DB 에 넣는다."""
    store = _module("store")
    articles = collected.get("articles") or []
    stored = _need(store, "upsert_articles")(conn, date_str, articles)
    _say(
        "DB 에 넣었다. 새로 넣은 것 {0}건, 이미 있던 것 {1}건, 그 날짜 전체 {2}건이다.".format(
            stored.get("inserted", 0),
            stored.get("duplicated", 0),
            stored.get("total_for_date", 0),
        )
    )
    return stored


def _collect_and_store(date_str, cfg, fetcher=None):
    """하루치를 받아 DB 에 넣는다. 받은 것과 넣은 결과를 함께 돌려준다.

    collect 명령이 쓰는 길이다. 이 명령은 점검과 브리핑을 하지 않기 때문에
    받은 것을 그대로 넣는다. run 은 점검을 사이에 끼우려고 두 함수를 따로 부른다.
    """
    collected = _collect_only(date_str, cfg, fetcher)
    conn = _open_db(cfg)
    stored = _store_collected(conn, date_str, collected)
    return conn, collected, stored


def _fallback_collected(conn, date_str):
    """수집을 건너뛰고 점검만 할 때, DB 에 있는 것으로 수집 결과 모양을 흉내 낸다."""
    store = _module("store")
    articles = _need(store, "articles_for_date")(conn, date_str)
    return {
        "date": date_str,
        "articles": articles,
        "windows": [],
        "truncated_windows": [],
        "raw_path": None,
        "from_db": True,
    }


def _run_checks(conn, date_str, collected, cfg, judged=None, stage=None, title="점검 결과"):
    """점검을 돌리고 결과를 표로 찍는다.

    stage 를 주면 qa 에 그대로 넘긴다. 적재 전 자리에서는 받아 온 것만으로 볼 수 있는
    멈춤 조건만 보게 하려는 것이다. judged 를 주면 걸러낸 비율을 그 자리에서 잰다.
    """
    qa = _module("qa")
    함수 = _need(qa, "run_checks")
    덧붙일것 = {}
    if judged is not None:
        덧붙일것["judged"] = judged
    if stage is not None:
        덧붙일것["stage"] = stage
    result = 함수(conn, date_str, collected, cfg, **덧붙일것) or {}
    checks = result.get("checks") or []

    _say("")
    _say(title)
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "")).lower()
        _say(
            "  {0}  {1}  {2}".format(
                _STATUS_MARK.get(status, status or "모름"),
                check.get("name", "이름 없는 점검"),
                check.get("detail", ""),
            )
        )
    if not checks:
        _say("  돌린 점검이 하나도 없다.")
    _say("")
    return result


def _judge_day(conn, date_str, articles, cfg, backend, force):
    """판단을 부른다. 저장해 둔 것이 있으면 그것을 다시 쓴다."""
    judge = _module("judge")

    judge_day = getattr(judge, "judge_day", None)
    if judge_day is not None:
        return judge_day(conn, date_str, articles, cfg, backend=backend, force=force)

    # judge_day 가 없으면 계약에 적힌 두 함수로 같은 일을 한다.
    if not force:
        loader = getattr(judge, "load_decisions", None)
        saved = loader(conn, date_str) if loader else None
        if saved:
            saved["reused"] = True
            return saved
    result = _need(judge, "judge_articles")(articles, cfg, backend=backend)
    saver = getattr(judge, "save_decisions", None)
    if saver:
        saver(conn, date_str, result)
    result["reused"] = False
    return result


def _stats_for(conn, date_str, articles, cfg):
    """브리핑에 넣을 숫자를 모은다. 기사 수, 최근 평균, 처음 보는 키워드다.

    앞선 기간 기사가 DB 에 한 건도 없으면 처음 보는 키워드를 아예 넘기지 않는다.
    빈 DB 에서 세면 관심 키워드 전부가 처음 보는 말이 되어 버리고, 브리핑은 그 아래에
    최근 이레에 없던 말이라고 단정한다. 그러면 매일 거짓을 적는 셈이다. 깃허브 액션이
    DB 를 이어 받지 못한 날이 정확히 그 경우다. 대신 왜 비웠는지를 두 자리에 적어
    브리핑이 그 사실을 한 줄로 남길 수 있게 한다.
    """
    store = _module("store")

    window = int(config.setting(cfg, "brief.average_window_days", 7) or 7)
    start = config.shift_date(date_str, -window)
    end = config.shift_date(date_str, -1)
    counts = _need(store, "daily_counts")(conn, start, end) if hasattr(store, "daily_counts") else {}
    average = (sum(counts.values()) / len(counts)) if counts else 0.0

    lookback = int(config.setting(cfg, "brief.first_seen_lookback_days", 7) or 7)
    앞선기간 = _need(store, "daily_counts")(
        conn, config.shift_date(date_str, -lookback), config.shift_date(date_str, -1)
    ) if hasattr(store, "daily_counts") else {}
    앞선건수 = sum(int(값) for 값 in (앞선기간 or {}).values())

    watch = config.watch_keywords(cfg)
    first_seen = []
    비운까닭 = ""
    if 앞선건수 <= 0:
        비운까닭 = (
            "앞선 {0}일 기사 기록이 DB 에 없어 견줄 수가 없다. 처음 보는 키워드는 "
            "비워 두었다".format(lookback)
        )
        _say("앞선 {0}일 기록이 없어 처음 보는 키워드 절을 비운다.".format(lookback))
    elif watch:
        first_seen = _need(store, "first_seen_keywords")(conn, date_str, lookback, watch)

    stats = {
        "article_count": len(articles),
        "avg_7d": average,
        "first_seen_keywords": first_seen,
        "first_seen_lookback_days": lookback,
        "history_article_count": 앞선건수,
        "first_seen_skipped": bool(비운까닭),
        "first_seen_skipped_reason": 비운까닭,
        "articles": articles,
    }
    return stats


def _send_slack(cfg, payload, dry_run):
    """슬랙으로 보낸다. 설정에서 슬랙을 껐으면 아무것도 하지 않는다."""
    if not config.setting(cfg, "notify.slack_enabled", True):
        _say("슬랙은 설정에서 꺼 두어서 보내지 않았다.")
        return None
    notify = _module("notify")
    result = _need(notify, "send_slack")(payload, dry_run=dry_run)
    _say("슬랙: {0}. {1}".format(result.get("status"), result.get("reason", "")))
    return result


def _send_notion(cfg, title, markdown, dry_run):
    """노션에 쌓는다. 연동 전이라 기본은 미리보기다."""
    notify = _module("notify")
    enabled = bool(config.setting(cfg, "notify.notion_enabled", False))
    if not enabled and not dry_run:
        _say("노션은 아직 연동 전이라 보낼 내용만 만들어 두었다.")
    result = _need(notify, "send_notion")(title, markdown, dry_run=dry_run or not enabled)
    _say("노션: {0}. {1}".format(result.get("status"), result.get("reason", "")))
    return result


def _send_failure(cfg, summary, dry_run):
    """점검이 어긋났을 때 알린다."""
    notify = _module("notify")
    sender = getattr(notify, "send_failure", None)
    if sender is None:
        _say("실패 알림을 보낼 함수를 찾지 못했다. 로그만 남긴다.")
        logger.error("실패 알림을 보내지 못했다. 내용은 %s 다.", summary)
        return None
    result = sender(summary, dry_run=dry_run)
    _say("실패 알림: {0}. {1}".format(result.get("status"), result.get("reason", "")))
    return result


def _comment_block(text):
    """리포트 글에서 '사람 코멘트' 절의 인용 블록 줄을 찾아 돌려준다.

    돌려주는 값은 (시작 줄 번호, 끝 줄 번호, 줄 목록) 이다. 절이 없으면 None 이다.
    끝 줄 번호는 파이썬 자리표대로 끝을 포함하지 않는다.
    """
    줄들 = str(text or "").splitlines()
    머리 = None
    for 번호, 줄 in enumerate(줄들):
        if 줄.strip().startswith("#") and "사람 코멘트" in 줄:
            머리 = 번호
            break
    if 머리 is None:
        return None

    처음 = None
    끝 = None
    for 번호 in range(머리 + 1, len(줄들)):
        줄 = 줄들[번호]
        if 줄.strip().startswith("#"):
            break
        if 줄.lstrip().startswith(">"):
            if 처음 is None:
                처음 = 번호
            끝 = 번호 + 1
    if 처음 is None:
        return None
    return 처음, 끝, 줄들[처음:끝]


def _carry_human_comment(old_text, new_text):
    """먼저 있던 리포트의 사람 코멘트를 새 리포트로 옮긴다.

    리포트를 다시 만들 때마다 인용 블록이 빈 줄로 새로 찍히기 때문에, 그냥 덮어쓰면
    사람이 적어 둔 문장이 사라진다. 같은 주를 workflow_dispatch 로 한 번 더 돌리는
    길이 열려 있고 그 결과가 저장소에 커밋되므로, 사람이 적은 글은 기계가 지키게 한다.

    돌려주는 값은 (옮긴 글, 옮긴 줄 수) 다. 옮길 것이 없으면 새 글을 그대로 돌려준다.
    """
    옛것 = _comment_block(old_text)
    if not 옛것:
        return new_text, 0
    _, _, 옛줄들 = 옛것
    사람이쓴것 = [줄 for 줄 in 옛줄들 if 줄.lstrip(" >\t").strip()]
    if not 사람이쓴것:
        return new_text, 0

    새것 = _comment_block(new_text)
    if not 새것:
        return new_text, 0
    처음, 끝, _ = 새것
    줄들 = str(new_text).splitlines()
    옮긴글 = "\n".join(줄들[:처음] + 옛줄들 + 줄들[끝:])
    if str(new_text).endswith("\n"):
        옮긴글 += "\n"
    return 옮긴글, len(사람이쓴것)


def _judge_stored(conn, date_str, cfg, backend, force):
    """DB 에 쌓인 하루치를 판단한다. 기사 목록과 판단 결과를 함께 돌려준다.

    브리핑 만들기와 갈라 둔 까닭이 있다. 걸러낸 비율 점검은 판단 결과가 있어야
    재는데, 점검이 판단보다 먼저면 그 날짜 첫 실행에서는 늘 건너뛰게 된다.
    그래서 run 은 이 함수로 판단을 먼저 돌리고 그 결과를 점검에 넘긴다.
    """
    store = _module("store")

    articles = _need(store, "articles_for_date")(conn, date_str)
    if not articles:
        raise RuntimeError(
            "{0} 에 쌓인 기사가 없다. 먼저 collect 를 돌려야 한다.".format(date_str)
        )

    judged = _judge_day(conn, date_str, articles, cfg, backend, force)
    if judged.get("reused"):
        _say("이미 남아 있는 판단을 그대로 썼다. 다시 판단하려면 --force 를 붙인다.")
    elif judged.get("rejudged_reason"):
        _say(
            "남아 있던 판단을 버리고 다시 판단했다. 사유는 {0} 다.".format(
                judged["rejudged_reason"]
            )
        )
    _say(
        "판단을 마쳤다. 남긴 기사 {0}건, 버린 기사 {1}건, 묶음 {2}개다.".format(
            len(judged.get("kept") or []),
            len(judged.get("dropped") or []),
            len(judged.get("groups") or []),
        )
    )

    return articles, judged


def _write_brief(conn, date_str, articles, judged, cfg):
    """판단 결과로 브리핑 마크다운을 만들어 파일로 남긴다."""
    brief = _module("brief")

    stats = _stats_for(conn, date_str, articles, cfg)
    built = _need(brief, "build_brief")(date_str, judged, stats, cfg)

    path = config.brief_path(cfg, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = _need(brief, "save_brief")(built["markdown"], str(path))

    _say("")
    _say(built.get("summary_line", ""))
    _say("브리핑을 {0} 에 남겼다.".format(saved))
    return built, saved


def _make_brief(conn, date_str, cfg, backend, force):
    """판단부터 브리핑 파일 저장까지 한 번에 한다. brief 명령이 쓰는 길이다."""
    articles, judged = _judge_stored(conn, date_str, cfg, backend, force)
    return _write_brief(conn, date_str, articles, judged, cfg)


# ---------------------------------------------------------------------------
# 하위 명령
# ---------------------------------------------------------------------------

def cmd_collect(args):
    """받아서 DB 에 넣기만 한다."""
    cfg = _load_cfg(args)
    date_str = _date_of(args)
    _collect_and_store(date_str, cfg, _fetcher_of(args, date_str))
    return EXIT_OK


def cmd_qa(args):
    """쌓인 자료가 쓸 만한지 본다. 실패가 하나라도 있으면 1 로 끝낸다."""
    cfg = _load_cfg(args)
    date_str = _date_of(args)
    conn = _open_db(cfg)
    collected = _fallback_collected(conn, date_str)
    _say("수집 결과 파일 대신 DB 에 쌓인 것으로 점검한다. 250건 상한 점검은 수집 직후에 봐야 정확하다.")
    result = _run_checks(conn, date_str, collected, cfg)
    return int(result.get("exit_code", 0) or 0)


def cmd_brief(args):
    """이미 받아 둔 자료로 브리핑을 만든다."""
    cfg = _load_cfg(args)
    date_str = _date_of(args)
    dry_run = _dry_run_of(args, cfg)
    conn = _open_db(cfg)

    brief = _module("brief")
    built, _ = _make_brief(conn, date_str, cfg, _backend_of(args, cfg), args.force)

    payload = _need(brief, "brief_payload")(built)
    _send_slack(cfg, payload, dry_run)
    _send_notion(cfg, "{0} AI 뉴스 브리핑".format(date_str), built["markdown"], dry_run)
    return EXIT_OK


def cmd_report(args):
    """한 주를 정리한 리포트를 만든다."""
    cfg = _load_cfg(args)
    dry_run = _dry_run_of(args, cfg)

    if args.week:
        week_start = config.week_start_of(args.week)
    elif getattr(args, "date", None):
        week_start = config.week_start_of(args.date)
    else:
        # 월요일 아침에 도는 일이라 기본은 지난주다.
        week_start = config.week_start_of(config.shift_date(config.kst_today(), -7))

    conn = _open_db(cfg)
    report = _module("report")
    built = _need(report, "build_report")(week_start, conn, cfg)

    path = config.report_path(cfg, week_start)
    path.parent.mkdir(parents=True, exist_ok=True)

    새글 = built["markdown"]
    if path.exists():
        옛글 = path.read_text(encoding="utf-8")
        옮긴글, 옮긴줄수 = _carry_human_comment(옛글, 새글)
        if 옮긴줄수:
            새글 = 옮긴글
            _say("먼저 있던 리포트의 사람 코멘트 {0}줄을 그대로 옮겼다.".format(옮긴줄수))
        elif not getattr(args, "force", False) and 옛글.strip() != 새글.strip():
            _say(
                "{0} 에 리포트가 이미 있다. 덮어쓰지 않았다. 다시 만들려면 --force 를 붙인다.".format(
                    path
                )
            )
            return EXIT_OK

    path.write_text(
        새글 if 새글.endswith("\n") else 새글 + "\n",
        encoding="utf-8",
    )

    _say("{0} 부터 한 주를 정리했다. 표에 오른 키워드는 {1}개다.".format(
        week_start, len(built.get("table") or [])
    ))
    _say("리포트를 {0} 에 남겼다.".format(path))
    if built.get("chart_path"):
        _say("차트는 {0} 에 있다.".format(built["chart_path"]))
    else:
        _say("그릴 숫자가 없어 차트는 만들지 않았다.")

    _send_slack(
        cfg,
        {"text": "{0} 주간 리포트를 만들었다. 파일은 {1} 에 있다.".format(week_start, path)},
        dry_run,
    )
    _send_notion(cfg, "{0} 주간 AI 뉴스 리포트".format(week_start), built["markdown"], dry_run)
    return EXIT_OK


def cmd_run(args):
    """수집부터 알림까지 한 번에 돈다. 점검이 실패면 거기서 멈춘다.

    순서는 받기, 적재 전 점검, 적재, 판단, 전체 점검, 브리핑, 전송이다.
    적재 전 점검에서 멈추면 DB 에는 아무것도 넣지 않는다. 날짜가 어긋난 기사가
    한 번 들어가면 되돌릴 길이 없어서, 넣기 전에 거를 것은 넣기 전에 거른다.
    판단을 점검보다 앞에 둔 것은 걸러낸 비율을 첫 실행에서도 재게 하려는 것이다.
    """
    cfg = _load_cfg(args)
    date_str = _date_of(args)
    dry_run = _dry_run_of(args, cfg)
    qa = _module("qa")

    _say("{0} 치를 받아 브리핑까지 만든다. 전송은 {1}.".format(
        date_str, "미리보기로 끝낸다" if dry_run else "실제로 내보낸다"
    ))

    collected = _collect_only(date_str, cfg, _fetcher_of(args, date_str))

    문지기 = _run_checks(
        None,
        date_str,
        collected,
        cfg,
        stage=getattr(qa, "STAGE_COLLECTED", "collected"),
        title="적재 전 점검",
    )
    if int(문지기.get("exit_code", 0) or 0) != 0:
        _say("적재 전 점검이 실패로 끝나 DB 에 넣지 않고 멈춘다.")
        _send_failure(cfg, 문지기, dry_run)
        return EXIT_CHECK_FAILED

    conn = _open_db(cfg)
    _store_collected(conn, date_str, collected)

    articles, judged = _judge_stored(
        conn, date_str, cfg, _backend_of(args, cfg), args.force
    )

    checks = _run_checks(conn, date_str, collected, cfg, judged=judged)
    exit_code = int(checks.get("exit_code", 0) or 0)
    if exit_code != 0:
        _say("점검이 실패로 끝나 브리핑을 만들지 않는다.")
        _send_failure(cfg, checks, dry_run)
        return EXIT_CHECK_FAILED

    brief = _module("brief")
    built, _ = _write_brief(conn, date_str, articles, judged, cfg)

    payload = _need(brief, "brief_payload")(built)
    _send_slack(cfg, payload, dry_run)
    _send_notion(cfg, "{0} AI 뉴스 브리핑".format(date_str), built["markdown"], dry_run)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 명령줄 짜기
# ---------------------------------------------------------------------------

def _common_parser():
    """모든 하위 명령이 함께 받는 옵션."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--date",
        help="다룰 한국 날짜를 YYYY-MM-DD 로 준다. 안 주면 어제다.",
    )
    common.add_argument(
        "--backend",
        choices=("rules", "llm"),
        help="판단을 규칙으로 할지 모델로 할지 정한다. 안 주면 설정을 따른다.",
    )
    common.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="전송을 미리보기로 끝낸다. 실제로 보내려면 --no-dry-run 을 쓴다.",
    )
    common.add_argument(
        "--root",
        help="프로젝트 뿌리 경로. 안 주면 이 패키지의 부모를 쓴다.",
    )
    common.add_argument(
        "--fixtures",
        help=(
            "GDELT 를 부르지 않고 이 폴더에 저장해 둔 응답으로 돈다. "
            "파일 이름은 gdelt_<한국날짜>.json 이다."
        ),
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="로그를 더 자세히 남긴다.",
    )
    return common


def build_parser():
    """argparse 파서를 짠다."""
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="python -m news_brief",
        description="AI 뉴스를 받아 아침 브리핑과 주간 리포트를 만든다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="명령")

    collect = subparsers.add_parser(
        "collect", parents=[common], help="GDELT 에서 하루치를 받아 DB 에 넣는다."
    )
    collect.set_defaults(func=cmd_collect)

    qa = subparsers.add_parser(
        "qa", parents=[common], help="쌓인 자료가 쓸 만한지 점검한다."
    )
    qa.set_defaults(func=cmd_qa)

    brief = subparsers.add_parser(
        "brief", parents=[common], help="받아 둔 자료로 브리핑을 만든다."
    )
    brief.add_argument(
        "--force", action="store_true", help="저장된 판단을 무시하고 다시 판단한다."
    )
    brief.set_defaults(func=cmd_brief)

    report = subparsers.add_parser(
        "report", parents=[common], help="한 주를 정리한 리포트를 만든다."
    )
    report.add_argument(
        "--week", help="주 시작일(월요일)을 YYYY-MM-DD 로 준다. 안 주면 지난주다."
    )
    report.add_argument(
        "--force",
        action="store_true",
        help=(
            "이미 있는 리포트를 덮어쓴다. 사람 코멘트는 어느 쪽이든 옮겨 오지만, "
            "그 밖의 손질은 이 옵션 없이는 지우지 않는다."
        ),
    )
    report.set_defaults(func=cmd_report)

    run = subparsers.add_parser(
        "run", parents=[common], help="수집부터 알림까지 한 번에 돈다."
    )
    run.add_argument(
        "--force", action="store_true", help="저장된 판단을 무시하고 다시 판단한다."
    )
    run.set_defaults(func=cmd_run)

    return parser


def main(argv=None):
    """명령을 골라 부른다. 돌려주는 값이 그대로 종료 코드가 된다."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_BROKEN

    _setup_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _say("사람이 중간에 멈췄다.")
        return EXIT_BROKEN
    except (RuntimeError, ValueError, OSError) as error:
        logger.error("%s", error)
        _say("멈췄다. 사유는 {0} 다.".format(error))
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
