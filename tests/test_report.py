"""주간 리포트 시험.

가짜 DB(메모리 sqlite)를 만들어 놓고 거기서 센 숫자가 표와 마크다운, 차트로
그대로 흘러가는지 본다. 다른 모듈이 아직 없어도 돌아가도록 집계 함수와 판단 기록
읽기는 시험용으로 갈아 끼워서 부른다. 저장소 모듈이 준비되면 마지막 시험이
계약대로 맞물리는지 한 번 더 확인한다.
"""

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from news_brief import report

WEEK_START = "2026-09-07"  # 월요일
WEEK_END = "2026-09-13"
PREV_START = "2026-08-31"
KEYWORDS = ["규제", "오픈AI", "반도체", "생성형", "저작권", "투자", "일자리"]


def _store_ready():
    """저장소 모듈이 계약대로 준비됐는지 본다."""
    try:
        from news_brief import store
    except Exception:
        return False
    return all(
        hasattr(store, name)
        for name in ("open_db", "upsert_articles", "keyword_counts")
    )


class ReportFixture(unittest.TestCase):
    """가짜 DB 와 설정만 차려 놓는 바탕이다. 시험은 아래 클래스들이 들고 있다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="news-brief-report-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE articles ("
            " url TEXT PRIMARY KEY,"
            " date TEXT NOT NULL,"
            " title TEXT NOT NULL)"
        )
        self.serial = 0
        self.decisions = {}
        self.cfg = {"root": self.tmp, "keywords": list(KEYWORDS)}

    # 시험 거들기 ---------------------------------------------------------

    def add_articles(self, day, keyword, count):
        """제목에 키워드가 든 기사를 하루치로 넣는다."""
        for _ in range(count):
            self.serial += 1
            self.conn.execute(
                "INSERT INTO articles (url, date, title) VALUES (?, ?, ?)",
                (
                    "https://example.test/%d" % self.serial,
                    day,
                    "%s 관련 소식 %d" % (keyword, self.serial),
                ),
            )

    def counts_fn(self, conn, start_date, end_date, keywords):
        """store.keyword_counts 자리에 끼우는 가짜. 제목에 걸린 기사를 센다."""
        counted = {}
        for keyword in keywords:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM articles"
                " WHERE date BETWEEN ? AND ? AND title LIKE ?",
                (start_date, end_date, "%" + keyword + "%"),
            )
            counted[keyword] = cursor.fetchone()[0]
        return counted

    def loader(self, conn, date_str):
        """judge.load_decisions 자리에 끼우는 가짜."""
        return self.decisions.get(date_str)

    def build(self, week_start=WEEK_START):
        return report.build_report(
            week_start,
            self.conn,
            self.cfg,
            decisions_loader=self.loader,
            counts_fn=self.counts_fn,
        )

    def fill_two_weeks(self):
        """이번 주와 지난주에 서로 다른 분포로 기사를 깔아 둔다."""
        self.add_articles("2026-09-07", "규제", 2)
        self.add_articles("2026-09-09", "규제", 2)
        self.add_articles("2026-09-08", "오픈AI", 3)
        self.add_articles("2026-09-10", "반도체", 2)
        self.add_articles("2026-09-11", "생성형", 1)
        self.add_articles("2026-09-13", "저작권", 1)

        self.add_articles("2026-08-31", "규제", 1)
        self.add_articles("2026-09-01", "오픈AI", 5)
        self.add_articles("2026-09-02", "반도체", 2)
        self.add_articles("2026-09-05", "투자", 2)

    def chart_file(self, week_key=WEEK_START):
        return Path(self.tmp) / "data" / "reports" / week_key / "keywords.png"

    def section(self, markdown, title):
        """마크다운에서 제목 하나에 딸린 몫만 잘라 낸다."""
        head = "## " + title
        self.assertIn(head, markdown)
        body = markdown.split(head, 1)[1]
        return body.split("\n## ", 1)[0]


class ReportTestCase(ReportFixture):
    # 표 계산 -------------------------------------------------------------

    def test_table_matches_db_counts(self):
        self.fill_two_weeks()
        table = self.build()["table"]

        self.assertEqual(
            [(row["keyword"], row["this_week"], row["last_week"]) for row in table],
            [
                ("규제", 4, 1),
                ("오픈AI", 3, 5),
                ("반도체", 2, 2),
                ("생성형", 1, 0),
                ("저작권", 1, 0),
            ],
        )

    def test_delta_signs(self):
        self.fill_two_weeks()
        table = {row["keyword"]: row for row in self.build()["table"]}

        self.assertEqual(table["규제"]["delta"], 3)
        self.assertEqual(table["규제"]["delta_text"], "+3")
        self.assertEqual(table["오픈AI"]["delta"], -2)
        self.assertEqual(table["오픈AI"]["delta_text"], "-2")
        self.assertEqual(table["반도체"]["delta"], 0)
        self.assertEqual(table["반도체"]["delta_text"], "0")
        self.assertEqual(table["생성형"]["note"], "지난주에는 없었다")

    def test_top_five_is_the_cap(self):
        for index, keyword in enumerate(KEYWORDS):
            self.add_articles("2026-09-08", keyword, index + 1)
        table = self.build()["table"]

        self.assertEqual(len(table), 5)
        self.assertEqual(table[0]["keyword"], KEYWORDS[-1])

    def test_keyword_that_vanished_this_week(self):
        self.cfg["report"] = {"top_keywords": 7}
        self.fill_two_weeks()
        table = {row["keyword"]: row for row in self.build()["table"]}

        self.assertIn("투자", table)
        self.assertEqual(table["투자"]["this_week"], 0)
        self.assertEqual(table["투자"]["last_week"], 2)
        self.assertEqual(table["투자"]["delta_text"], "-2")
        self.assertEqual(table["투자"]["note"], "이번 주에는 안 나왔다")
        # 양쪽 다 0인 키워드는 표에 올리지 않는다
        self.assertNotIn("일자리", table)

    # 마크다운과 표가 같은 숫자를 쓰는지 ------------------------------------

    def test_markdown_numbers_match_table(self):
        self.fill_two_weeks()
        result = self.build()
        markdown = result["markdown"]

        for row in result["table"]:
            line = "| %s | %d | %d | %s |" % (
                row["keyword"],
                row["this_week"],
                row["last_week"],
                row["delta_text"],
            )
            self.assertIn(line, markdown)

        top = result["table"][0]
        self.assertIn(
            "이번 주에 가장 많이 나온 키워드는 %s로 %d건이다." % (top["keyword"], top["this_week"]),
            markdown,
        )
        self.assertIn("지난주보다 %d건 늘었다." % top["delta"], markdown)

    def test_markdown_has_every_required_part(self):
        self.fill_two_weeks()
        markdown = self.build()["markdown"]

        for title in ("이번 주에 새로 나온 도구", "규제와 정책", "많이 나온 키워드", "차트", "사람 코멘트", "한계"):
            self.assertIn("## " + title, markdown)
        self.assertIn(
            "대상 기간은 한국 시간으로 %s 월요일부터 %s 일요일까지다." % (WEEK_START, WEEK_END),
            markdown,
        )
        # 사람이 채울 빈 인용 블록
        self.assertIn("\n>\n>\n", markdown)
        # 한계 문단에 세 가지가 다 있어야 한다
        limits = self.section(markdown, "한계")
        self.assertIn("색인해 둔 매체만", limits)
        self.assertIn("250건", limits)
        self.assertIn("제목만 보고 한다", limits)

    def test_written_by_hand_rules(self):
        self.fill_two_weeks()
        markdown = self.build()["markdown"]

        self.assertNotIn("—", markdown)
        self.assertNotIn("결론적으로", markdown)

    # 묶음 목록 -----------------------------------------------------------

    def test_tool_and_policy_sections(self):
        self.decisions["2026-09-08"] = {
            "kept": [
                {"url": "https://example.test/a", "reason": "AI 도구 소식이다"},
                {"url": "https://example.test/b", "reason": "AI 소식이다"},
            ],
            "dropped": [{"url": "https://example.test/z", "reason": "AI 소식이 아니다"}],
            "groups": [
                {
                    "headline": "오픈AI가 새 코딩 도구를 내놨다",
                    "urls": ["https://example.test/a"],
                    "kind": "tool",
                },
                {
                    "headline": "방통위가 생성형 AI 표시 의무를 예고했다",
                    "urls": ["https://example.test/b"],
                    "kind": "policy",
                },
                {
                    "headline": "AI 주가가 출렁였다",
                    "urls": ["https://example.test/c"],
                    "kind": "other",
                },
            ],
        }
        self.decisions["2026-09-09"] = {
            "kept": [{"url": "https://example.test/a", "reason": "어제와 같은 기사다"}],
            "groups": [
                {
                    "headline": "오픈AI가 새 코딩 도구를 내놨다",
                    "urls": ["https://example.test/d"],
                    "kind": "tool",
                }
            ],
        }
        self.decisions["2026-09-01"] = {
            "kept": [{"url": "https://example.test/old", "reason": "지난주 기사다"}],
            "groups": [],
        }

        markdown = self.build()["markdown"]
        tools = self.section(markdown, "이번 주에 새로 나온 도구")
        policies = self.section(markdown, "규제와 정책")

        self.assertIn("오픈AI가 새 코딩 도구를 내놨다", tools)
        self.assertEqual(markdown.count("오픈AI가 새 코딩 도구를 내놨다"), 1)
        self.assertIn("기사 2건", tools)  # 이틀치 링크를 합쳐 센다
        self.assertIn("09-08", tools)
        self.assertIn("방통위가 생성형 AI 표시 의무를 예고했다", policies)
        self.assertNotIn("방통위", tools)
        self.assertNotIn("AI 주가가 출렁였다", markdown)
        # 같은 링크가 이틀에 걸쳐도 기사 수는 한 번만 센다
        self.assertIn("이번 주에 AI가 남긴 기사는 2건, 지난주는 1건이다.", markdown)

    def test_sections_say_so_when_empty(self):
        self.fill_two_weeks()
        markdown = self.build()["markdown"]

        self.assertIn("새 도구 소식으로 묶인 기사가 이번 주에는 없다.", markdown)
        self.assertIn("규제나 정책으로 묶인 기사가 이번 주에는 없다.", markdown)

    # 차트 ---------------------------------------------------------------

    def test_chart_file_is_written_and_linked(self):
        self.fill_two_weeks()
        result = self.build()

        self.assertEqual(result["chart_path"], str(self.chart_file()))
        self.assertTrue(self.chart_file().exists())
        self.assertGreater(self.chart_file().stat().st_size, 0)
        self.assertIn("](keywords.png)", result["markdown"])

    def test_chart_falls_back_when_no_korean_font(self):
        self.cfg["report"] = {"font_candidates": ["있을리없는폰트이름"]}
        self.fill_two_weeks()

        with self.assertLogs("news_brief.report", level="WARNING") as logs:
            result = self.build()

        self.assertTrue(any("한글 폰트를 못 찾았다" in line for line in logs.output))
        self.assertTrue(self.chart_file().exists())
        for row in result["table"]:
            self.assertTrue(row["chart_label"].isascii())
        self.assertIn("차트 축 라벨은 한글 폰트가 없어 로마자로 적었다.", result["markdown"])

    def test_roman_key_from_config_becomes_chart_label(self):
        self.cfg["keywords"] = [{"keyword": "규제", "roman": "regulation"}]
        self.cfg["report"] = {"font_candidates": ["있을리없는폰트이름"]}
        self.add_articles("2026-09-08", "규제", 2)

        table = self.build()["table"]
        self.assertEqual(table[0]["chart_label"], "regulation")

    # 빈 주간 -------------------------------------------------------------

    def test_empty_week(self):
        result = self.build()

        self.assertEqual(result["table"], [])
        self.assertIsNone(result["chart_path"])
        self.assertFalse(self.chart_file().exists())
        self.assertIn("표를 비워 둔다", result["markdown"])
        self.assertIn("이번 주 차트는 만들지 않았다", result["markdown"])
        self.assertIn("이번 주에 AI가 남긴 기사는 0건, 지난주는 0건이다.", result["markdown"])

    def test_counts_source_missing_is_reported(self):
        def broken(conn, start_date, end_date, keywords):
            raise AttributeError("아직 store.keyword_counts 가 없다")

        result = report.build_report(
            WEEK_START,
            self.conn,
            self.cfg,
            decisions_loader=self.loader,
            counts_fn=broken,
        )
        self.assertEqual(result["table"], [])
        self.assertIn("키워드 집계를 불러오지 못해", result["markdown"])

    # 주 경계와 되풀이 ----------------------------------------------------

    def test_week_start_that_is_not_monday_snaps_back(self):
        self.fill_two_weeks()

        with self.assertLogs("news_brief.report", level="WARNING") as logs:
            result = self.build(week_start="2026-09-09")

        self.assertTrue(any("월요일이 아니다" in line for line in logs.output))
        self.assertIn(
            "대상 기간은 한국 시간으로 %s 월요일부터 %s 일요일까지다." % (WEEK_START, WEEK_END),
            result["markdown"],
        )
        self.assertEqual(result["chart_path"], str(self.chart_file()))

    def test_bad_date_raises(self):
        with self.assertRaises(ValueError):
            self.build(week_start="2026/09/07")

    def test_same_week_twice_gives_the_same_numbers(self):
        self.fill_two_weeks()
        first = self.build()
        second = self.build()

        self.assertEqual(first["markdown"], second["markdown"])
        self.assertEqual(first["table"], second["table"])

    def test_previous_week_window_is_the_week_before(self):
        # 지난주 마지막 날과 이번 주 첫날만 채워서 경계가 맞는지 본다
        self.add_articles("2026-09-06", "규제", 3)
        self.add_articles("2026-09-07", "규제", 1)
        table = self.build()["table"]

        self.assertEqual(table[0]["this_week"], 1)
        self.assertEqual(table[0]["last_week"], 3)
        self.assertEqual(table[0]["delta_text"], "-2")

    def test_articles_outside_both_weeks_are_ignored(self):
        self.add_articles("2026-08-30", "규제", 4)  # 지지난주
        self.add_articles("2026-09-14", "규제", 4)  # 다음 주
        result = self.build()

        self.assertEqual(result["table"], [])

    def test_particles_stick_to_the_word_before(self):
        """조사가 앞말에 붙고 받침에 맞게 골라지는지 본다."""
        self.assertEqual(report._josa("규제", "으로", "로"), "로")
        self.assertEqual(report._josa("저작권", "으로", "로"), "으로")
        self.assertEqual(report._josa("오픈AI", "으로", "로"), "로")  # 아이로 끝난다
        self.assertEqual(report._josa("KW1", "은", "는"), "은")  # 일로 끝난다
        self.assertEqual(report._josa("KW2", "은", "는"), "는")  # 이로 끝난다

        self.cfg["keywords"] = ["오픈AI", "저작권"]
        self.add_articles("2026-09-08", "오픈AI", 3)
        self.add_articles("2026-09-08", "저작권", 1)
        markdown = self.build()["markdown"]

        self.assertIn("이번 주에 가장 많이 나온 키워드는 오픈AI로 3건이다.", markdown)
        self.assertIn("올라온 키워드는 오픈AI, 저작권이다.", markdown)
        self.assertNotIn(" 로 ", markdown)

    # 저장소 계약 맞물림 --------------------------------------------------

    @unittest.skipUnless(_store_ready(), "store 모듈이 아직 계약대로 없어서 건너뛴다")
    def test_runs_on_real_store(self):
        from news_brief import store

        db_path = str(Path(self.tmp) / "smoke.db")
        conn = store.open_db(db_path)
        self.addCleanup(conn.close)
        store.upsert_articles(
            conn,
            "2026-09-08",
            [
                {
                    "url": "https://example.test/real",
                    "title": "규제 당국이 AI 지침을 냈다",
                    "title_raw": "규제 당국이 AI 지침을 냈다",
                    "domain": "example.test",
                    "seendate": "20260908T010000Z",
                    "language": "Korean",
                }
            ],
        )

        result = report.build_report(
            WEEK_START, conn, self.cfg, decisions_loader=self.loader
        )
        self.assertIn("markdown", result)
        self.assertIn("chart_path", result)
        self.assertIsInstance(result["table"], list)


class JosaTest(unittest.TestCase):
    """으로/로 는 ㄹ 받침에서 '로' 를 쓴다. 구글으로가 아니라 구글로다."""

    def test_ㄹ_받침에는_로_를_쓴다(self):
        for 말 in ("구글", "애플", "서울", "파일"):
            self.assertEqual(report._josa(말, "으로", "로"), "로", 말)

    def test_ㄹ_이_아닌_받침에는_으로_를_쓴다(self):
        for 말 in ("저작권", "삼성", "반도침", "온디바이스칩"):
            self.assertEqual(report._josa(말, "으로", "로"), "으로", 말)

    def test_받침이_없으면_로_를_쓴다(self):
        for 말 in ("규제", "메타", "카카오"):
            self.assertEqual(report._josa(말, "으로", "로"), "로", 말)

    def test_로마자와_숫자도_읽는_소리를_따진다(self):
        self.assertEqual(report._josa("Google", "으로", "로"), "로")  # 엘로 끝난다
        self.assertEqual(report._josa("KW1", "으로", "로"), "로")  # 일로 끝난다
        self.assertEqual(report._josa("Samsung", "으로", "로"), "로")  # 지로 끝난다
        self.assertEqual(report._josa("Telecom", "으로", "로"), "으로")  # 엠으로 끝난다
        self.assertEqual(report._josa("Meta", "으로", "로"), "로")

    def test_다른_조사는_받침_규칙을_그대로_따른다(self):
        # ㄹ 예외는 으로/로 에만 있다. 은/는 과 이다/다 는 그대로다.
        self.assertEqual(report._josa("구글", "은", "는"), "은")
        self.assertEqual(report._josa("구글", "이다", "다"), "이다")


class ReportJosaSentenceTest(ReportFixture):
    """리포트 문장에 조사가 맞게 붙는지 본다."""

    def test_ㄹ_받침_키워드에_로_를_붙인다(self):
        self.cfg["keywords"] = ["구글", "삼성"]
        self.add_articles("2026-09-08", "구글", 3)
        self.add_articles("2026-09-08", "삼성", 1)
        markdown = self.build()["markdown"]

        self.assertIn("이번 주에 가장 많이 나온 키워드는 구글로 3건이다.", markdown)
        self.assertNotIn("구글으로", markdown)


class RomanLabelTest(ReportFixture):
    """한글 글꼴이 없는 환경에서 라벨 풀이가 어색한 한국어를 내지 않는지 본다.

    ubuntu-latest 에는 한글 글꼴이 없어 액션이 만드는 리포트가 이 경로로 간다.
    """

    def setUp(self):
        super().setUp()
        self.cfg["report"] = {"font_candidates": ["있을리없는폰트이름"]}

    def test_로마자_라벨에는_조사를_붙이지_않는다(self):
        self.cfg["keywords"] = [
            {"keyword": "구글", "roman": "Google"},
            {"keyword": "메타", "roman": "Meta"},
            {"keyword": "삼성", "roman": "Samsung"},
        ]
        self.add_articles("2026-09-08", "구글", 3)
        self.add_articles("2026-09-08", "메타", 2)
        self.add_articles("2026-09-08", "삼성", 1)
        markdown = self.build()["markdown"]

        self.assertIn("Google = 구글, Meta = 메타, Samsung = 삼성이다.", markdown)
        for 틀린것 in ("Google는", "Google은", "Samsung는", "Samsung은", "Meta는", "Meta은"):
            self.assertNotIn(틀린것, markdown)

    def test_맺음_조사는_풀이_목록의_마지막을_따른다(self):
        # 표의 마지막 줄이 아니라 라벨 풀이의 마지막 이름으로 골라야 한다.
        # GPT 는 그대로 로마자라 풀이 목록에 오르지 않는다.
        self.cfg["keywords"] = [{"keyword": "구글", "roman": "Google"}, {"keyword": "GPT"}]
        self.add_articles("2026-09-08", "구글", 3)
        self.add_articles("2026-09-08", "GPT", 1)
        markdown = self.build()["markdown"]

        self.assertIn("차트 축 라벨은 한글 폰트가 없어 로마자로 적었다. Google = 구글이다.", markdown)


class SameInputSameFileTest(ReportFixture):
    """같은 주를 다른 날 다시 만들어도 같은 파일이 나와야 한다."""

    def test_만든_날짜를_본문에_적지_않는다(self):
        self.fill_two_weeks()
        self.assertNotIn("에 만들었다", self.build()["markdown"])

    def test_다른_날_돌려도_같은_글이_나온다(self):
        self.fill_two_weeks()
        with mock.patch.object(report, "_kst_today", return_value=date(2026, 9, 14)):
            첫번째 = self.build()["markdown"]
        with mock.patch.object(report, "_kst_today", return_value=date(2026, 9, 21)):
            두번째 = self.build()["markdown"]
        self.assertEqual(첫번째, 두번째)


class UpcomingDayTest(ReportFixture):
    """주 중간에 만들면 아직 오지 않은 날을 빈 날로 세지 않는지 본다."""

    def 주중간에_만들기(self):
        # 대상 주는 09-07 부터 09-13 까지다. 오늘을 09-10 으로 두면 뒤 사흘은
        # 아직 오지 않은 날이다.
        with mock.patch.object(report, "_kst_today", return_value=date(2026, 9, 10)):
            return self.build()["markdown"]

    def 빈날줄(self, markdown):
        줄들 = [줄 for 줄 in markdown.splitlines() if 줄.startswith("판단 기록이 없는 날")]
        self.assertEqual(len(줄들), 1, markdown)
        return 줄들[0]

    def test_아직_오지_않은_날은_빈_날에_넣지_않는다(self):
        self.decisions["2026-09-08"] = {"kept": [{"url": "https://e.test/a"}], "groups": []}
        줄 = self.빈날줄(self.주중간에_만들기())

        self.assertIn("3일 있다", 줄)
        for 지난날 in ("2026-09-07", "2026-09-09", "2026-09-10"):
            self.assertIn(지난날, 줄)
        for 앞날 in ("2026-09-11", "2026-09-12", "2026-09-13"):
            self.assertNotIn(앞날, 줄)

    def test_아직_오지_않은_날은_따로_적는다(self):
        markdown = self.주중간에_만들기()
        self.assertIn(
            "아직 오지 않은 날이 3일 있다. 그 날짜는 다음과 같다. "
            "2026-09-11, 2026-09-12, 2026-09-13",
            markdown,
        )

    def test_지난_주를_만들면_앞날_줄이_없다(self):
        with mock.patch.object(report, "_kst_today", return_value=date(2026, 9, 20)):
            markdown = self.build()["markdown"]
        self.assertNotIn("아직 오지 않은 날", markdown)
        self.assertIn("7일 있다", self.빈날줄(markdown))


class HumanCommentTest(ReportFixture):
    """사람이 채운 코멘트가 다시 만들 때 지워지지 않는지 본다."""

    빈블록머리 = "아래 인용 블록은 비워 둔다. 리포트를 읽고 직접 채운다.\n\n>\n>"

    def report_file(self, week_key=WEEK_START):
        return Path(self.tmp) / "data" / "reports" / week_key / "report.md"

    def 파일로_남기기(self, markdown):
        길 = self.report_file()
        길.parent.mkdir(parents=True, exist_ok=True)
        길.write_text(markdown, encoding="utf-8")
        return 길

    def 코멘트_채우기(self, markdown, 줄들):
        """빈 인용 블록 자리에 사람이 쓴 줄을 채워 넣는다."""
        self.assertIn(self.빈블록머리, markdown)
        return markdown.replace(self.빈블록머리, "\n".join(줄들))

    def test_이미_있는_코멘트를_그대로_옮긴다(self):
        self.fill_two_weeks()
        self.파일로_남기기(
            self.코멘트_채우기(
                self.build()["markdown"],
                ["> 규제 기사가 몰린 주다.", "> 다음 주에 다시 본다."],
            )
        )

        두번째 = self.build()["markdown"]
        self.assertIn("> 규제 기사가 몰린 주다.", 두번째)
        self.assertIn("> 다음 주에 다시 본다.", 두번째)
        self.assertIn("앞서 만든 리포트에 적힌 것을 그대로 옮겼다.", 두번째)
        # 빈 인용 블록을 새로 찍지 않는다.
        self.assertNotIn("아래 인용 블록은 비워 둔다.", 두번째)

    def test_세_번째로_만들어도_코멘트가_남는다(self):
        self.fill_two_weeks()
        self.파일로_남기기(
            self.코멘트_채우기(self.build()["markdown"], ["> 사람이 쓴 한 줄이다."])
        )
        self.파일로_남기기(self.build()["markdown"])
        self.assertIn("> 사람이 쓴 한 줄이다.", self.build()["markdown"])

    def test_비어_있는_인용_블록은_옮기지_않는다(self):
        self.fill_two_weeks()
        self.파일로_남기기(self.build()["markdown"])

        두번째 = self.build()["markdown"]
        self.assertIn(self.빈블록머리, 두번째)

    def test_파일이_없으면_빈_블록을_찍는다(self):
        self.fill_two_weeks()
        self.assertFalse(self.report_file().exists())
        self.assertIn(self.빈블록머리, self.build()["markdown"])

    def test_코멘트만_따로_뽑아_온다(self):
        마크다운 = "\n".join([
            "# 주간 AI 뉴스 리포트",
            "",
            "## 사람 코멘트",
            "",
            "아래 인용 블록은 비워 둔다.",
            "",
            "> 첫째 줄이다.",
            "> 둘째 줄이다.",
            "",
            "## 한계",
            "",
            "> 여기 인용은 코멘트가 아니다.",
        ])
        self.assertEqual(
            report.extract_human_comments(마크다운),
            ["> 첫째 줄이다.", "> 둘째 줄이다."],
        )
        self.assertEqual(report.extract_human_comments("## 사람 코멘트\n\n>\n>\n"), [])
        self.assertEqual(report.extract_human_comments(""), [])


class SourceLineTest(ReportFixture):
    """출처가 한계 문단 안에 섞이지 않고 맨 아래 따로 있는지 본다."""

    def test_맨_아래에_출처_절을_따로_둔다(self):
        self.cfg["query"] = '("artificial intelligence" OR OpenAI) sourcelang:korean'
        self.fill_two_weeks()
        markdown = self.build()["markdown"]

        마지막줄 = markdown.strip().splitlines()[-1]
        self.assertIn("GDELT DOC 2.0", 마지막줄)
        self.assertIn("하루 경계는 한국 시간으로 끊었다", 마지막줄)
        self.assertIn("집계한 날짜는 %s 부터 %s 까지다" % (WEEK_START, WEEK_END), 마지막줄)
        self.assertIn('("artificial intelligence" OR OpenAI) sourcelang:korean', 마지막줄)
        self.assertEqual(self.section(markdown, "출처").strip(), 마지막줄)

    def test_한계_문단_안에_출처를_묻지_않는다(self):
        self.fill_two_weeks()
        limits = self.section(self.build()["markdown"], "한계")
        self.assertNotIn("DOC 2.0", limits)

    def test_검색어가_없으면_그_대목만_뺀다(self):
        self.fill_two_weeks()
        마지막줄 = self.build()["markdown"].strip().splitlines()[-1]
        self.assertIn("GDELT DOC 2.0", 마지막줄)
        self.assertNotIn("검색어는", 마지막줄)


class ReportCleanTextTest(ReportFixture):
    """리포트도 브리핑과 같은 잣대로 줄표와 이모지를 걸러내는지 본다."""

    def test_묶음_제목의_줄표와_이모지를_걷어_낸다(self):
        self.decisions["2026-09-08"] = {
            "kept": [{"url": "https://example.test/a"}],
            "groups": [
                {
                    "headline": "오픈AI — 새 모델 🚀 공개, 무료 배포",
                    "urls": ["https://example.test/a"],
                    "kind": "tool",
                },
                {
                    "headline": "국회가 AI 기본법을 손봤다 – 시행은 내년이다 ⚖️",
                    "urls": ["https://example.test/b"],
                    "kind": "policy",
                },
            ],
        }
        markdown = self.build()["markdown"]

        for 줄표 in ("—", "–", "―", "‒"):
            self.assertNotIn(줄표, markdown)
        self.assertNotIn("🚀", markdown)
        self.assertNotIn("⚖", markdown)
        self.assertIn("오픈AI, 새 모델 공개, 무료 배포", markdown)
        self.assertIn("국회가 AI 기본법을 손봤다, 시행은 내년이다", markdown)


if __name__ == "__main__":
    unittest.main()
