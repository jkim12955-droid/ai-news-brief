"""수집기 시험.

GDELT 를 실제로 부르지 않는다. 저장해 둔 응답 모양을 fetcher 로 물려 주고,
상한에 걸렸을 때 구간을 쪼개는지, 제목을 다듬는지, 같은 링크를 걸러내는지,
빈 응답을 견디는지 확인한다.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from news_brief import collect, config, gdelt

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
WINDOW_FORMAT = "%Y%m%d%H%M%S"


def setUpModule():
    # 상한에 걸렸을 때 남기는 경고가 시험 결과에 섞이지 않게 잠시 막아 둔다.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _standin_kst_day_window(date_str):
    """config.kst_day_window 가 아직 없을 때 대신 쓰는 구간 계산.

    한국 시간 그날 0시부터 다음 날 0시까지를 UTC 로 바꾼다. config 쪽이 자리를
    잡으면 이 대역은 쓰이지 않고 진짜 함수가 불린다.
    """
    day = dt.date.fromisoformat(date_str)
    start = dt.datetime.combine(day, dt.time(0, 0, 0), tzinfo=ZoneInfo("Asia/Seoul"))
    end = start + dt.timedelta(days=1)
    return (
        start.astimezone(dt.timezone.utc).strftime(WINDOW_FORMAT),
        end.astimezone(dt.timezone.utc).strftime(WINDOW_FORMAT),
    )


def has_real_config():
    """config 쪽 구간 계산이 붙었는지 본다."""
    return callable(getattr(config, "kst_day_window", None))


@contextlib.contextmanager
def window_provider():
    """config 가 아직 비어 있어도 수집기를 돌려 볼 수 있게 한다."""
    if has_real_config():
        yield "config.kst_day_window"
    else:
        with mock.patch.object(
            config, "kst_day_window", _standin_kst_day_window, create=True
        ):
            yield "대역"


def sample_article(number, at="103000", title=None, url=None, domain="zdnet.co.kr"):
    """GDELT artlist 응답에 실려 오는 기사 한 건의 모양을 흉내 낸다."""
    return {
        "url": url or "https://www.%s/view/?no=2026091100%s" % (domain, number),
        "url_mobile": "",
        "title": title or "AI 소식 %s번" % number,
        "seendate": "20260911T%sZ" % at,
        "socialimage": "",
        "domain": domain,
        "language": "Korean",
        "sourcecountry": "South Korea",
    }


class RecordingFetcher:
    """fetcher 자리에 끼워 넣는 가짜 호출기. 부른 내역을 남긴다."""

    def __init__(self, responses=None, always=None):
        self.responses = list(responses or [])
        self.always = always
        self.calls = []

    def __call__(self, query, start_utc, end_utc, maxrecords=250, sleep_seconds=5):
        self.calls.append(
            {
                "query": query,
                "start": start_utc,
                "end": end_utc,
                "maxrecords": maxrecords,
                "sleep_seconds": sleep_seconds,
            }
        )
        if self.always is not None:
            return {"articles": list(self.always(len(self.calls) - 1))}
        if self.responses:
            return {"articles": list(self.responses.pop(0))}
        return {"articles": []}


class CleanTitleTest(unittest.TestCase):
    def test_구두점에_낀_공백을_붙인다(self):
        self.assertEqual(
            collect.clean_title("정부 , AI 기본법 ( 가칭 ) 내놨다"),
            "정부, AI 기본법 (가칭) 내놨다",
        )

    def test_따옴표_안쪽_공백을_붙인다(self):
        self.assertEqual(
            collect.clean_title("  OpenAI ,  새 모델 ' 오리온 ' 공개   "),
            "OpenAI, 새 모델 '오리온' 공개",
        )

    def test_연속_공백을_하나로_줄인다(self):
        self.assertEqual(
            collect.clean_title("삼성전자 AI    반도체   양산"),
            "삼성전자 AI 반도체 양산",
        )

    def test_줄바꿈과_보이지_않는_공백도_줄인다(self):
        self.assertEqual(
            collect.clean_title("AI 반도체\n수출 늘었다"),
            "AI 반도체 수출 늘었다",
        )

    def test_HTML_기호를_되돌린다(self):
        self.assertEqual(
            collect.clean_title("AT&amp;T 와 &#39;AI&#39; 맞손"),
            "AT&T 와 'AI' 맞손",
        )

    def test_말줄임표와_퍼센트도_붙인다(self):
        self.assertEqual(
            collect.clean_title("네이버 “ 하이퍼클로바X ” 공개 … 매출 30 % 늘어"),
            "네이버 “하이퍼클로바X” 공개… 매출 30% 늘어",
        )

    def test_남은_꼬리_기호를_떼어_낸다(self):
        self.assertEqual(collect.clean_title("AI 수출 늘었다 -"), "AI 수출 늘었다")

    def test_아포스트로피는_건드리지_않는다(self):
        # 짝이 맞지 않는 따옴표까지 붙여 버리면 낱말이 깨진다.
        self.assertEqual(collect.clean_title("What's next for AI"), "What's next for AI")

    def test_빈_제목은_빈_문자열이다(self):
        self.assertEqual(collect.clean_title(None), "")
        self.assertEqual(collect.clean_title(""), "")


class NormalizeUrlTest(unittest.TestCase):
    def test_추적용_파라미터를_버린다(self):
        self.assertEqual(
            collect.normalize_url(
                "https://www.zdnet.co.kr/view/?no=20260911001&utm_source=twitter"
            ),
            "zdnet.co.kr/view?no=20260911001",
        )

    def test_기사_번호_파라미터는_남긴다(self):
        first = collect.normalize_url(
            "https://www.ohmynews.com/NWS_Web/View/at_pg.aspx?CNTN_CD=A0003001"
        )
        second = collect.normalize_url(
            "https://www.ohmynews.com/NWS_Web/View/at_pg.aspx?CNTN_CD=A0003002"
        )
        self.assertNotEqual(first, second)

    def test_네이버_분야_파라미터는_버린다(self):
        # 기사 번호가 경로에 있으니 sid 를 남기면 같은 기사가 둘로 갈라진다.
        self.assertEqual(
            collect.normalize_url(
                "https://n.news.naver.com/mnews/article/001/0014512345?sid=105"
            ),
            collect.normalize_url(
                "https://n.news.naver.com/mnews/article/001/0014512345"
            ),
        )

    def test_네이버_예전_주소는_oid_와_aid_를_남긴다(self):
        # 예전 네이버 주소는 기사 번호를 oid 와 aid 로 들고 있다. sid1 만 버려야 한다.
        첫번째 = collect.normalize_url(
            "https://news.naver.com/main/read.naver?oid=001&aid=0012345678&sid1=105"
        )
        두번째 = collect.normalize_url(
            "https://news.naver.com/main/read.naver?oid=001&aid=0012345679&sid1=105"
        )
        self.assertNotEqual(첫번째, 두번째)
        self.assertNotIn("sid1", 첫번째)
        self.assertIn("aid=0012345678", 첫번째)

    def test_국내_매체_기사_번호_이름을_모두_남긴다(self):
        # 국내 CMS 가 기사 번호에 쓰는 이름들이다. 하나라도 버리면 그 매체의
        # 같은 경로 기사가 전부 한 열쇠로 무너져 조용히 사라진다.
        for 이름 in (
            "idxno", "no", "aid", "article_id", "oid", "seq", "nkey",
            "arcid", "news_id", "cntn_cd", "key", "num",
        ):
            with self.subTest(파라미터=이름):
                첫번째 = collect.normalize_url(
                    "https://news.site.co.kr/view.php?%s=1001" % 이름
                )
                두번째 = collect.normalize_url(
                    "https://news.site.co.kr/view.php?%s=1002" % 이름
                )
                self.assertNotEqual(첫번째, 두번째, "%s 를 버리면 기사가 합쳐진다" % 이름)

    def test_포털이_아닌_곳의_sid_는_기사_번호로_본다(self):
        # sid 가 분야를 뜻하는 곳은 포털이다. 이름 모를 매체에서 sid 는 기사
        # 번호인 경우가 있어서, 거기서 버리면 서로 다른 기사가 한 건이 된다.
        첫번째 = collect.normalize_url("https://news.site.co.kr/view.php?sid=1001")
        두번째 = collect.normalize_url("https://news.site.co.kr/view.php?sid=1002")
        self.assertNotEqual(첫번째, 두번째)

    def test_주소체계와_모바일_접두사를_무시한다(self):
        self.assertEqual(
            collect.normalize_url("http://m.zdnet.co.kr/view?no=20260911001"),
            collect.normalize_url("https://www.zdnet.co.kr/view/?no=20260911001"),
        )

    def test_조각_표시를_떼어_낸다(self):
        self.assertEqual(
            collect.normalize_url("https://zdnet.co.kr/view/?no=20260911001#comment"),
            "zdnet.co.kr/view?no=20260911001",
        )

    def test_이름을_모르는_기사_번호도_살린다(self):
        # 살릴 이름만 열거하던 때는 여기 적힌 이름이 다 목록에 없어서, 같은
        # 경로의 기사가 전부 한 열쇠로 무너졌다. 서로 다른 기사가 조용히 한
        # 건으로 합쳐지는 것이라 어느 점검에도 걸리지 않았다.
        for 이름 in ("idx", "nno", "contents_id", "newsIdx", "wr_id", "ncode"):
            첫번째 = collect.normalize_url(
                "https://news.site.co.kr/view.php?%s=1001" % 이름
            )
            두번째 = collect.normalize_url(
                "https://news.site.co.kr/view.php?%s=1002" % 이름
            )
            self.assertNotEqual(첫번째, 두번째, "%s 를 버리면 기사가 합쳐진다" % 이름)

    def test_처음_보는_추적_꼬리표도_버린다(self):
        바탕 = collect.normalize_url("https://news.site.co.kr/view.php?idx=1001")
        for 꼬리표 in (
            "utm_source=twitter",
            "utm_어쩌고=값",
            "fbclid=abc123",
            "nil_id=999",
            "_gaexp=1",
            "ref=kakao",
        ):
            self.assertEqual(
                collect.normalize_url(
                    "https://news.site.co.kr/view.php?idx=1001&%s" % 꼬리표
                ),
                바탕,
                "%s 는 버려야 같은 기사가 둘로 갈라지지 않는다" % 꼬리표,
            )

    def test_파라미터_차례가_달라도_같은_열쇠다(self):
        self.assertEqual(
            collect.normalize_url("https://news.site.co.kr/v?idx=1&page=2"),
            collect.normalize_url("https://news.site.co.kr/v?page=2&idx=1"),
        )


class CollectDayTest(unittest.TestCase):
    date_str = "2026-09-11"

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="news-brief-collect-"))
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.cfg = {
            "query": '("artificial intelligence" OR OpenAI) sourcelang:korean',
            "maxrecords": 4,
            "max_split_depth": 2,
            "sleep_seconds": 0,
            "data_dir": str(self.workdir),
        }

    def run_collect(self, fetcher, cfg=None):
        with window_provider():
            return collect.collect_day(self.date_str, cfg or self.cfg, fetcher=fetcher)

    def test_상한에_걸리면_구간을_절반으로_쪼갠다(self):
        # 하루 통째로는 상한 4건에 딱 걸리고, 절반씩 나눠 받으면 상한 아래로 내려간다.
        fetcher = RecordingFetcher(
            responses=[
                [sample_article(n) for n in "1234"],
                [sample_article("1"), sample_article("2")],
                [sample_article("3"), sample_article("4")],
            ]
        )
        result = self.run_collect(fetcher)

        self.assertEqual(len(fetcher.calls), 3, "하루 한 번, 절반 두 번을 불러야 한다")
        self.assertEqual(len(result["windows"]), 3)
        self.assertTrue(result["windows"][0]["split"])
        self.assertEqual(result["truncated_windows"], [])
        self.assertEqual(len(result["articles"]), 4)

        # 쪼갠 두 구간이 원래 구간의 양 끝을 그대로 물고 있어야 한다.
        root, left, right = result["windows"]
        self.assertEqual(left["start"], root["start"])
        self.assertEqual(left["end"], right["start"])
        self.assertEqual(right["end"], root["end"])
        self.assertEqual(left["depth"], 1)

    def test_끝까지_상한이면_truncated_windows_에_남긴다(self):
        # 어떤 구간을 불러도 상한이 차는 상황. 깊이 2에서 멈추고 자국을 남겨야 한다.
        fetcher = RecordingFetcher(
            always=lambda turn: [
                sample_article("%d%d" % (turn, n), at="1030%02d" % n) for n in range(4)
            ]
        )
        result = self.run_collect(fetcher)

        self.assertEqual(len(fetcher.calls), 7, "1 + 2 + 4 번 불러야 한다")
        self.assertEqual(len(result["truncated_windows"]), 4)
        mark = result["truncated_windows"][0]
        for key in ("start", "end", "depth", "count", "reason"):
            self.assertIn(key, mark)
        self.assertEqual(mark["depth"], 2)
        self.assertEqual(mark["count"], 4)

    def test_같은_링크는_한_건으로_줄인다(self):
        same = [
            sample_article(
                "1", url="https://www.zdnet.co.kr/view/?no=20260911001&utm_source=twitter"
            ),
            sample_article("1", url="http://m.zdnet.co.kr/view?no=20260911001"),
            sample_article("1", url="https://zdnet.co.kr/view/?no=20260911001#comment"),
            sample_article("2", url="https://zdnet.co.kr/view/?no=20260911002"),
        ]
        result = self.run_collect(RecordingFetcher(responses=[same]))

        self.assertEqual(len(result["articles"]), 2)
        keys = {collect.normalize_url(item["url"]) for item in result["articles"]}
        self.assertEqual(len(keys), 2)

    def test_쪼갠_구간에서_겹친_기사도_한_건이_된다(self):
        # 가운데 시각을 양쪽이 함께 쓰기 때문에 경계 기사는 두 번 들어온다.
        edge = sample_article("9", url="https://zdnet.co.kr/view/?no=20260911009")
        fetcher = RecordingFetcher(
            responses=[
                [sample_article(n) for n in "1234"],
                [sample_article("1"), edge],
                [edge, sample_article("4")],
            ]
        )
        result = self.run_collect(fetcher)
        urls = [item["url"] for item in result["articles"]]
        self.assertEqual(len(urls), len(set(urls)))

    def test_빈_응답을_견딘다(self):
        result = self.run_collect(RecordingFetcher(responses=[[]]))

        self.assertEqual(result["articles"], [])
        self.assertEqual(result["truncated_windows"], [])
        self.assertEqual(len(result["windows"]), 1)
        self.assertTrue(Path(result["raw_path"]).exists(), "빈 날도 원본은 남겨야 한다")

    def test_기사_묶음이_없는_응답도_견딘다(self):
        # GDELT 는 걸리는 기사가 없으면 빈 객체만 돌려주기도 한다.
        result = self.run_collect(mock.Mock(return_value={}))
        self.assertEqual(result["articles"], [])

    def test_기사_열쇠가_계약대로다(self):
        result = self.run_collect(
            RecordingFetcher(responses=[[sample_article("1", title="정부 , AI 기본법 ( 가칭 )")]])
        )

        item = result["articles"][0]
        self.assertEqual(
            set(item), {"url", "title", "title_raw", "domain", "seendate", "language"}
        )
        self.assertEqual(item["title"], "정부, AI 기본법 (가칭)")
        self.assertEqual(item["title_raw"], "정부 , AI 기본법 ( 가칭 )")
        self.assertEqual(item["domain"], "zdnet.co.kr")
        self.assertEqual(item["seendate"], "20260911T103000Z")
        self.assertEqual(item["language"], "Korean")

    def test_결과_열쇠가_계약대로다(self):
        result = self.run_collect(RecordingFetcher(responses=[[sample_article("1")]]))
        self.assertEqual(
            set(result),
            {
                "date",
                "articles",
                "windows",
                "truncated_windows",
                "raw_path",
                "duplicated",
                "merged_distinct_urls",
                "dropped_no_url",
                "stray_dropped",
                "stray_samples",
                "blocked_windows",
            },
        )
        self.assertEqual(result["date"], self.date_str)

    def test_겹친_링크_건수를_결과에_담는다(self):
        # _dedupe 가 먼저 지우기 때문에 DB 만 보면 몇 건이 합쳐졌는지 알 수 없다.
        # 점검이 그 숫자를 볼 수 있도록 수집기가 들고 나와야 한다.
        묶음 = [
            sample_article("1", url="https://www.zdnet.co.kr/view/?no=1&utm_source=x"),
            sample_article("1", url="https://zdnet.co.kr/view/?no=1"),
            sample_article("2", url="https://zdnet.co.kr/view/?no=2"),
        ]
        result = self.run_collect(
            RecordingFetcher(responses=[묶음]), cfg=dict(self.cfg, maxrecords=10)
        )
        self.assertEqual(result["duplicated"], 1)
        self.assertEqual(result["merged_distinct_urls"], 1)
        self.assertEqual(result["dropped_no_url"], 0)

    def test_링크_없는_항목의_건수를_남긴다(self):
        # 조용히 버리면 GDELT 응답 건수와 우리가 센 건수가 갈린 이유를
        # 나중에 되짚을 수 없다.
        묶음 = [
            sample_article("1"),
            {"title": "링크가 빠진 항목", "seendate": "20260911T103000Z"},
            {"url": "   ", "title": "빈 링크"},
        ]
        result = self.run_collect(
            RecordingFetcher(responses=[묶음]), cfg=dict(self.cfg, maxrecords=10)
        )
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["dropped_no_url"], 2)
        문서 = json.loads(Path(result["raw_path"]).read_text(encoding="utf-8"))
        self.assertEqual(문서["dedupe"]["dropped_no_url"], 2)

    def test_쪼개기_깊이는_상한에서_끊는다(self):
        # 깊이가 하나 늘 때마다 최악의 호출 수가 배로 늘고 호출마다 5초를 쉰다.
        # 설정에 큰 값이 적혀 있어도 작업 시간 제한을 넘기기 전에 멈춰야 한다.
        fetcher = RecordingFetcher(
            always=lambda turn: [
                sample_article("%d%d" % (turn, n), at="1030%02d" % n) for n in range(4)
            ]
        )
        result = self.run_collect(fetcher, cfg=dict(self.cfg, max_split_depth=20))

        self.assertEqual(
            len(fetcher.calls),
            2 ** (collect.MAX_SPLIT_DEPTH_LIMIT + 1) - 1,
            "상한 깊이까지만 쪼개야 한다",
        )
        self.assertEqual(
            max(구간["depth"] for 구간 in result["windows"]),
            collect.MAX_SPLIT_DEPTH_LIMIT,
        )
        for 자국 in result["truncated_windows"]:
            self.assertEqual(자국["depth"], collect.MAX_SPLIT_DEPTH_LIMIT)
            self.assertIn("깊이", 자국["reason"])

    def test_쪼개기_깊이가_0이면_쪼개지_않는다(self):
        fetcher = RecordingFetcher(
            always=lambda turn: [sample_article(str(n)) for n in range(4)]
        )
        result = self.run_collect(fetcher, cfg=dict(self.cfg, max_split_depth=-3))
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(len(result["truncated_windows"]), 1)

    def test_원본_응답을_날짜별로_남긴다(self):
        result = self.run_collect(
            RecordingFetcher(responses=[[sample_article("1"), sample_article("2")]])
        )

        path = Path(result["raw_path"])
        self.assertEqual(path.name, "%s.json" % self.date_str)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["date"], self.date_str)
        self.assertEqual(len(saved["responses"]), 1)
        self.assertEqual(len(saved["responses"][0]["payload"]["articles"]), 2)

    def test_중간에_끊겨도_받아_둔_구간이_파일에_남는다(self):
        # 하루치를 다 받은 뒤에야 파일을 쓰면, 뒤쪽 구간에서 끊길 때 앞서 받아
        # 둔 응답이 전부 사라진다. 5초 간격을 지켜 가며 몇 분을 들인 호출이
        # 헛일이 되는 셈이라, 구간을 받는 즉시 이어 쓴다.
        부른횟수 = {"수": 0}

        def 끊기는부르개(query, start_utc, end_utc, maxrecords=250, sleep_seconds=5):
            부른횟수["수"] += 1
            if 부른횟수["수"] == 1:
                return {"articles": [sample_article(n) for n in "1234"]}
            raise gdelt.GdeltError("GDELT 가 429 로 막았다")

        with self.assertRaises(gdelt.GdeltError):
            self.run_collect(끊기는부르개)

        남은것 = collect.partial_path(self.workdir / "raw", self.date_str)
        self.assertTrue(남은것.exists(), "끊긴 뒤에도 받아 둔 구간은 남아야 한다")
        줄들 = [
            json.loads(줄)
            for 줄 in 남은것.read_text(encoding="utf-8").splitlines()
            if 줄.strip()
        ]
        self.assertEqual(len(줄들), 1)
        self.assertEqual(len(줄들[0]["payload"]["articles"]), 4)
        self.assertFalse(
            (self.workdir / "raw" / ("%s.json" % self.date_str)).exists(),
            "온전한 원본 파일은 다 받은 뒤에만 생겨야 한다",
        )

    def test_다_받고_나면_이어_쓰기_파일을_치운다(self):
        result = self.run_collect(RecordingFetcher(responses=[[sample_article("1")]]))
        self.assertTrue(Path(result["raw_path"]).exists())
        self.assertFalse(
            collect.partial_path(self.workdir / "raw", self.date_str).exists()
        )

    def test_링크_글자가_다른데_합친_건은_따로_센다(self):
        # 구간을 쪼개 같은 기사를 두 번 받는 것은 정상이다. 링크 글자가 서로
        # 다른데 한 건이 된 것은 열쇠를 너무 짧게 깎았다는 신호일 수 있어
        # 따로 센다. 이 숫자가 없으면 손실이 정상 중복에 묻힌다.
        묶음 = [
            sample_article("1", url="https://www.zdnet.co.kr/view/?no=1&utm_source=x"),
            sample_article("1", url="https://zdnet.co.kr/view/?no=1"),
            sample_article("2", url="https://zdnet.co.kr/view/?no=2"),
            sample_article("2", url="https://zdnet.co.kr/view/?no=2"),
        ]
        result = self.run_collect(
            RecordingFetcher(responses=[묶음]), cfg=dict(self.cfg, maxrecords=10)
        )

        self.assertEqual(len(result["articles"]), 2)
        문서 = json.loads(Path(result["raw_path"]).read_text(encoding="utf-8"))
        self.assertEqual(문서["dedupe"]["duplicated"], 2)
        self.assertEqual(문서["dedupe"]["merged_distinct_urls"], 1)
        self.assertEqual(문서["dedupe"]["kept"], 2)

    def test_같은_날을_다시_돌려도_차례가_같다(self):
        def shuffled():
            return [
                sample_article("3", at="090000"),
                sample_article("1", at="180000"),
                sample_article("2", at="120000"),
            ]

        first = self.run_collect(RecordingFetcher(responses=[shuffled()]))
        second = self.run_collect(RecordingFetcher(responses=[shuffled()]))

        self.assertEqual(
            [item["url"] for item in first["articles"]],
            [item["url"] for item in second["articles"]],
        )
        seen = [item["seendate"] for item in first["articles"]]
        self.assertEqual(seen, sorted(seen, reverse=True), "늦은 기사가 앞에 와야 한다")

    def test_설정한_검색어와_상한을_그대로_넘긴다(self):
        fetcher = RecordingFetcher(responses=[[sample_article("1")]])
        self.run_collect(fetcher)

        first = fetcher.calls[0]
        self.assertEqual(first["query"], self.cfg["query"])
        self.assertEqual(first["maxrecords"], 4)
        self.assertEqual(first["sleep_seconds"], 0)
        self.assertEqual(len(first["start"]), 14)
        self.assertEqual(len(first["end"]), 14)
        self.assertLess(first["start"], first["end"])

    def test_설정이_비어도_기본값으로_돈다(self):
        fetcher = RecordingFetcher(responses=[[sample_article("1")]])
        result = self.run_collect(fetcher, cfg={"data_dir": str(self.workdir)})

        self.assertEqual(fetcher.calls[0]["query"], collect.DEFAULT_QUERY)
        self.assertEqual(fetcher.calls[0]["maxrecords"], gdelt.DEFAULT_MAXRECORDS)
        self.assertEqual(len(result["articles"]), 1)

    @unittest.skipUnless(has_real_config(), "config.kst_day_window 이 아직 없어 건너뛴다")
    def test_진짜_config_의_구간을_쓴다(self):
        fetcher = RecordingFetcher(responses=[[sample_article("1")]])
        collect.collect_day(self.date_str, self.cfg, fetcher=fetcher)
        self.assertEqual(
            (fetcher.calls[0]["start"], fetcher.calls[0]["end"]),
            config.kst_day_window(self.date_str),
        )


class 막힌_구간Test(unittest.TestCase):
    """GDELT 가 한 구간을 막았을 때 하루치를 통째로 버리지 않는지 본다.

    gdelt.fetch_artlist 가 다시 부르기를 다 써도 막히면 GdeltRateLimited 가
    수집기까지 올라온다. 그때 앞서 받아 둔 구간까지 버리면 그날 브리핑이
    통째로 비는데, 다음 실행은 하루 뒤다.
    """

    date_str = "2026-09-11"

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="news-brief-blocked-"))
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.cfg = {
            "maxrecords": 4,
            "max_split_depth": 1,
            "sleep_seconds": 0,
            "data_dir": str(self.workdir),
        }

    def run_collect(self, fetcher):
        with window_provider():
            return collect.collect_day(self.date_str, self.cfg, fetcher=fetcher)

    def test_한_구간이_막혀도_나머지는_받는다(self):
        부른횟수 = {"수": 0}

        def 두번째만_막힘(query, start_utc, end_utc, maxrecords=250, sleep_seconds=5):
            부른횟수["수"] += 1
            if 부른횟수["수"] == 1:
                return {"articles": [sample_article(n) for n in "1234"]}
            if 부른횟수["수"] == 2:
                raise gdelt.GdeltRateLimited("GDELT 가 429 로 막았다")
            # 시각은 하루 구간 안에 둔다. 구간 밖 기사는 수집기가 넣기 전에
            # 버리므로(날짜가 잘못 박히는 것을 막는다) 여기서 구간 밖 시각을
            # 쓰면 막힌 구간과 무관하게 건수가 줄어 이 시험이 헛것을 잰다.
            return {"articles": [sample_article("5", at="120000")]}

        result = self.run_collect(두번째만_막힘)

        self.assertEqual(부른횟수["수"], 3, "막힌 뒤에도 남은 구간을 계속 불러야 한다")
        링크 = {기사["url"] for 기사 in result["articles"]}
        self.assertEqual(len(링크), 5)
        self.assertEqual(len(result["blocked_windows"]), 1)
        막힌것 = result["blocked_windows"][0]
        self.assertTrue(막힌것["blocked"])
        self.assertIn("429", 막힌것["reason"])
        # 점검이 걸러낼 수 있게 상한 구간과 같은 자리에도 남긴다.
        self.assertIn(막힌것, result["truncated_windows"])
        self.assertTrue(Path(result["raw_path"]).exists())

    def test_한_구간도_받지_못하면_멈춘다(self):
        def 늘_막힘(query, start_utc, end_utc, maxrecords=250, sleep_seconds=5):
            raise gdelt.GdeltRateLimited("GDELT 가 429 로 막았다")

        with self.assertRaises(gdelt.GdeltRateLimited):
            self.run_collect(늘_막힘)

        self.assertFalse(
            (self.workdir / "raw" / ("%s.json" % self.date_str)).exists(),
            "한 건도 받지 못한 날을 조용한 날로 넘기면 브리핑이 거짓이 된다",
        )

    def test_막힌_것이_아닌_오류는_그대로_올려_보낸다(self):
        # 인증서나 주소가 어긋난 것은 다시 불러도 같은 결과다. 접고 넘어가면
        # 설정이 잘못된 채로 매일 반쪽짜리 브리핑이 나간다.
        def 어긋남(query, start_utc, end_utc, maxrecords=250, sleep_seconds=5):
            raise gdelt.GdeltError("인증서를 검증하지 못했다")

        with self.assertRaises(gdelt.GdeltError):
            self.run_collect(어긋남)


class GdeltResponseTest(unittest.TestCase):
    """망을 타지 않고 응답 해석과 주소 만들기만 확인한다."""

    def test_JSON_이_아니면_막힌_것으로_본다(self):
        with self.assertRaises(gdelt.GdeltRateLimited):
            gdelt.parse_response("Your query was too complex")

    def test_빈_본문도_막힌_것으로_본다(self):
        with self.assertRaises(gdelt.GdeltRateLimited):
            gdelt.parse_response("   ")

    def test_정상_응답은_dict_로_돌려준다(self):
        parsed = gdelt.parse_response('{"articles": [{"url": "https://a.kr/1"}]}')
        self.assertEqual(len(parsed["articles"]), 1)

    def test_주소에_구간과_상한이_들어간다(self):
        url = gdelt.build_url("AI", "20260910150000", "20260911150000", maxrecords=250)
        for 조각 in (
            "startdatetime=20260910150000",
            "enddatetime=20260911150000",
            "maxrecords=250",
            "format=json",
            "mode=artlist",
        ):
            self.assertIn(조각, url)


class SavedFixtureTest(unittest.TestCase):
    """tests/fixtures 에 실제 응답이 놓이면 그 모양으로도 한 번 돌려 본다."""

    def test_픽스처가_있으면_그대로_수집된다(self):
        fixtures = sorted(FIXTURE_DIR.glob("*.json")) if FIXTURE_DIR.exists() else []
        usable = []
        for path in fixtures:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "articles" in payload:
                usable.append((path, payload))
        if not usable:
            self.skipTest("tests/fixtures 에 저장된 GDELT 응답이 아직 없다")

        workdir = Path(tempfile.mkdtemp(prefix="news-brief-fixture-"))
        self.addCleanup(shutil.rmtree, workdir, True)

        for path, payload in usable:
            with self.subTest(픽스처=path.name):
                # 날짜는 파일 이름에서 읽는다. 어느 픽스처든 2026-09-11 로 돌리면
                # 다른 날 파일은 기사가 모두 구간 밖이라 버려지고, 이 시험이
                # 빈 목록을 상대로 아무것도 확인하지 못한 채 통과한다.
                날짜 = path.stem.replace("gdelt_", "")
                with window_provider():
                    result = collect.collect_day(
                        날짜,
                        {"maxrecords": 250, "sleep_seconds": 0, "data_dir": str(workdir)},
                        fetcher=lambda *args, _payload=payload, **kwargs: _payload,
                    )
                self.assertTrue(
                    result["articles"],
                    "%s 픽스처에서 그 날짜 구간에 드는 기사가 한 건도 나오지 않았다" % path.name,
                )
                for item in result["articles"]:
                    self.assertTrue(item["url"])
                    self.assertLessEqual(len(item["title"]), len(item["title_raw"]))


class 구간밖기사Test(unittest.TestCase):
    """하루 구간 밖 기사를 넣기 전에 버리는지 본다.

    적재가 점검보다 뒤로 갔어도 임계값 아래 한두 건은 여전히 DB 에 들어간다.
    store 는 url 을 기본키로 써서 한 번 들어간 기사의 kst_date 를 나중 실행이
    덮어쓰지 않으므로, 잘못된 날짜는 원인을 고친 뒤에도 영원히 남는다.
    그래서 수집기가 스스로 거르고 버린 건수를 점검이 보도록 들고 나온다.
    """

    date_str = "2026-09-11"

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="news-brief-stray-"))
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.cfg = {
            "maxrecords": 250,
            "max_split_depth": 0,
            "sleep_seconds": 0,
            "data_dir": str(self.workdir),
        }

    def 돌린다(self, articles):
        def fetch(query, start_utc, end_utc, maxrecords=250, sleep_seconds=0):
            return {"articles": list(articles)}

        with window_provider():
            return collect.collect_day(self.date_str, self.cfg, fetcher=fetch)

    def test_구간_밖_기사는_기사_목록에_넣지_않는다(self):
        # 20260911T160000Z 는 한국 시간 09-12 라 이 하루의 구간 밖이다.
        result = self.돌린다([
            sample_article("1"),
            sample_article("2", at="160000"),
        ])
        self.assertEqual(len(result["articles"]), 1)
        링크 = {기사["url"] for 기사 in result["articles"]}
        self.assertNotIn(sample_article("2", at="160000")["url"], 링크)

    def test_버린_건수와_보기를_결과에_담는다(self):
        result = self.돌린다([
            sample_article("1"),
            sample_article("2", at="160000"),
        ])
        self.assertEqual(result["stray_dropped"], 1)
        self.assertEqual(len(result["stray_samples"]), 1)
        self.assertIn("20260911T160000Z", result["stray_samples"][0])

    def test_구간_안_기사만_오면_버린_것이_없다(self):
        result = self.돌린다([sample_article("1"), sample_article("2", at="120000")])
        self.assertEqual(result["stray_dropped"], 0)
        self.assertEqual(result["stray_samples"], [])
        self.assertEqual(len(result["articles"]), 2)

    def test_구간_밖_기사만_오면_빈_하루가_된다(self):
        # 이때 브리핑은 조용한 날이 되지만, 점검의 수집 건수 0건이 먼저 멈춘다.
        result = self.돌린다([sample_article("1", at="160000")])
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["stray_dropped"], 1)


if __name__ == "__main__":
    unittest.main()


class 빈응답다시부르기Test(unittest.TestCase):
    """GDELT 가 막는 동안 준 빈 응답을 0건으로 받아들이지 않고 다시 부르는지 본다."""

    date_str = "2026-09-11"

    def run_collect(self, fetcher, cfg=None):
        base = {"maxrecords": 250, "sleep_seconds": 0, "empty_retry_seconds": 0}
        base.update(cfg or {})
        with tempfile.TemporaryDirectory() as tmp:
            base.setdefault("paths", {"data_dir": tmp})
            base["data_dir"] = tmp
            with window_provider():
                return collect.collect_day(self.date_str, base, fetcher=fetcher)

    def test_처음이_비면_다시_불러_기사를_받는다(self):
        fetcher = RecordingFetcher(responses=[[], [sample_article("1"), sample_article("2")]])
        result = self.run_collect(fetcher)
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(len(result["windows"]), 1, "다시 부른 시도의 구간만 남아야 한다")

    def test_계속_비면_정해진_만큼만_부르고_빈_하루로_넘긴다(self):
        fetcher = RecordingFetcher(responses=[])
        result = self.run_collect(fetcher)
        self.assertEqual(result["articles"], [])
        self.assertEqual(len(fetcher.calls), 1 + collect.DEFAULT_EMPTY_RETRIES)

    def test_다시_부르기를_끄면_한_번만_부른다(self):
        fetcher = RecordingFetcher(responses=[])
        self.run_collect(fetcher, {"empty_retries": 0})
        self.assertEqual(len(fetcher.calls), 1)

    def test_기사가_오면_다시_부르지_않는다(self):
        fetcher = RecordingFetcher(responses=[[sample_article("1")]])
        self.run_collect(fetcher)
        self.assertEqual(len(fetcher.calls), 1)

    def test_끼워_넣은_부르개는_기다리지_않는다(self):
        fetcher = RecordingFetcher(responses=[])
        with mock.patch.object(collect.time, "sleep") as 잠:
            self.run_collect(fetcher, {"empty_retry_seconds": 60})
        잠.assert_not_called()
