"""저장소 모듈 시험.

멱등성, 키워드 집계, 처음 보는 키워드를 주로 본다.
같은 날을 다시 돌려도 숫자가 흔들리면 안 된다는 약속이
코드에서 실제로 지켜지는지 확인하는 것이 이 시험의 목적이다.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_brief import store

# 시험 내내 쓰는 고정 시각. 시각 함수를 주입해 결과를 붙박이로 만든다.
FIRST_RUN_AT = "2026-09-11T00:10:00+00:00"
SECOND_RUN_AT = "2026-09-11T06:20:00+00:00"

DAY_ONE = "2026-09-10"
DAY_TWO = "2026-09-11"


def article(url, title, domain, seendate, title_raw=None, language="Korean"):
    """수집기가 넘겨주는 모양으로 기사 하나를 만든다."""
    return {
        "url": url,
        "title": title,
        "title_raw": title_raw if title_raw is not None else title,
        "domain": domain,
        "seendate": seendate,
        "language": language,
    }


def day_one_articles():
    return [
        article(
            "https://aa.example/1",
            "OpenAI 가 새 추론 모델을 공개했다",
            "aa.example",
            "20260910T013000Z",
        ),
        article(
            "https://bb.example/2",
            "정부가 AI 규제 초안을 내놨다",
            "bb.example",
            "20260910T090000Z",
        ),
    ]


def day_two_articles():
    return [
        article(
            "https://cc.example/3",
            "openai 가 요금을 내렸다",
            "cc.example",
            "20260911T003000Z",
        ),
        article(
            "https://dd.example/4",
            "국산 소버린 AI 사업이 출범했다",
            "dd.example",
            "20260911T051500Z",
        ),
    ]


class StoreTestCase(unittest.TestCase):
    """임시 폴더에 sqlite 파일을 두고 시험한다."""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.db_path = os.path.join(self.workdir.name, "data", "news.sqlite3")
        self.conn = store.open_db(self.db_path)
        self.addCleanup(self.conn.close)

    def table_names(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}


class SchemaTest(StoreTestCase):
    def test_열면_두_테이블이_만들어진다(self):
        names = self.table_names(self.conn)
        self.assertIn("articles", names)
        self.assertIn("runs", names)

    def test_판단_표는_저장소가_만들지_않는다(self):
        # 판단 기록은 judge 가 judge_ 로 시작하는 자기 표에 넣는다. 여기에도
        # decisions 표를 만들어 두었더니 넣고 빼는 코드가 한 줄도 없는 채로
        # 남아, 판단을 담는 표가 둘로 보였다.
        self.assertNotIn("decisions", self.table_names(self.conn))

    def test_상위_폴더가_없어도_파일을_만든다(self):
        self.assertTrue(os.path.exists(self.db_path))

    def test_이미_있는_파일을_다시_열어도_탈이_없다(self):
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        again = store.open_db(self.db_path)
        self.addCleanup(again.close)
        self.assertEqual(len(store.articles_for_date(again, DAY_ONE)), 2)

    def test_메모리_DB_도_연다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        self.assertIn("articles", self.table_names(conn))


class UpsertIdempotencyTest(StoreTestCase):
    def test_같은_날을_두_번_넣어도_건수가_그대로다(self):
        first = store.upsert_articles(
            self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT
        )
        second = store.upsert_articles(
            self.conn, DAY_ONE, day_one_articles(), now=SECOND_RUN_AT
        )

        self.assertEqual(first["inserted"], 2)
        self.assertEqual(first["duplicated"], 0)
        self.assertEqual(first["total_for_date"], 2)

        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicated"], 2)
        self.assertEqual(second["total_for_date"], first["total_for_date"])

        self.assertEqual(len(store.articles_for_date(self.conn, DAY_ONE)), 2)

    def test_한_번의_호출_안_중복_url_은_한_건만_들어간다(self):
        rows = day_one_articles()
        rows.append(day_one_articles()[0])
        result = store.upsert_articles(self.conn, DAY_ONE, rows, now=FIRST_RUN_AT)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["duplicated"], 1)
        self.assertEqual(result["total_for_date"], 2)

    def test_처음_들어간_시각은_나중_실행이_덮지_않는다(self):
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=SECOND_RUN_AT)
        stamps = {
            row["first_inserted_at"] for row in store.articles_for_date(self.conn, DAY_ONE)
        }
        self.assertEqual(stamps, {FIRST_RUN_AT})

    def test_먼저_본_날짜가_유지된다(self):
        # 어제 본 기사가 오늘 수집에 또 걸려도 어제 것으로 남겨야 하루치 숫자가 안 흔들린다.
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        again = store.upsert_articles(
            self.conn, DAY_TWO, day_one_articles(), now=SECOND_RUN_AT
        )
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["duplicated"], 2)
        self.assertEqual(again["total_for_date"], 0)
        self.assertEqual(len(store.articles_for_date(self.conn, DAY_ONE)), 2)

    def test_url_이_없는_항목은_건너뛴다(self):
        rows = day_one_articles()
        rows.append(article("", "url 이 빠진 기사", "zz.example", "20260910T100000Z"))
        rows.append({"title": "url 열쇠 자체가 없다"})
        result = store.upsert_articles(self.conn, DAY_ONE, rows, now=FIRST_RUN_AT)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["total_for_date"], 2)

    def test_빈_목록을_넣어도_넘어간다(self):
        result = store.upsert_articles(self.conn, DAY_ONE, [], now=FIRST_RUN_AT)
        self.assertEqual(
            result, {"inserted": 0, "duplicated": 0, "total_for_date": 0}
        )

    def test_시각_함수를_넣으면_그_값을_쓴다(self):
        store.upsert_articles(
            self.conn, DAY_ONE, day_one_articles(), now=lambda: FIRST_RUN_AT
        )
        rows = store.articles_for_date(self.conn, DAY_ONE)
        self.assertEqual(rows[0]["first_inserted_at"], FIRST_RUN_AT)

    def test_적재할_때마다_실행_기록이_쌓인다(self):
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=SECOND_RUN_AT)
        runs = store.runs_for_date(self.conn, DAY_ONE)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["inserted"], 2)
        self.assertEqual(runs[0]["started_at"], FIRST_RUN_AT)
        self.assertEqual(runs[1]["inserted"], 0)
        self.assertEqual(runs[1]["duplicated"], 2)


class ArticlesForDateTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_TWO, day_two_articles(), now=SECOND_RUN_AT)

    def test_그_날짜_기사만_돌려준다(self):
        rows = store.articles_for_date(self.conn, DAY_TWO)
        self.assertEqual(
            [row["url"] for row in rows],
            ["https://cc.example/3", "https://dd.example/4"],
        )

    def test_수집기가_쓰던_열쇠를_그대로_담는다(self):
        row = store.articles_for_date(self.conn, DAY_ONE)[0]
        for key in ("url", "title", "title_raw", "domain", "seendate", "language"):
            self.assertIn(key, row)
        self.assertEqual(row["domain"], "aa.example")
        self.assertEqual(row["seendate"], "20260910T013000Z")
        self.assertEqual(row["kst_date"], DAY_ONE)

    def test_시각_순으로_정렬한다(self):
        rows = store.articles_for_date(self.conn, DAY_ONE)
        self.assertEqual(
            [row["seendate"] for row in rows],
            ["20260910T013000Z", "20260910T090000Z"],
        )

    def test_기사가_없는_날은_빈_목록이다(self):
        self.assertEqual(store.articles_for_date(self.conn, "2026-09-09"), [])


class KeywordHitTest(unittest.TestCase):
    """제목에 키워드가 낱말로 들어 있는지 보는 잣대만 따로 본다.

    부분 문자열로만 견주던 때는 리포트 표의 숫자가 부풀고 겹쳤다.
    algorithm 이 LG 로 잡히고, 메타버스가 메타로 잡혔다.
    """

    def test_로마자_키워드는_낱말_안에서_걸리지_않는다(self):
        self.assertFalse(store.keyword_hit("AIDS 치료에 쓰인 기술", "AI"))
        self.assertFalse(store.keyword_hit("Hair 관리 앱이 나왔다", "AI"))
        self.assertFalse(store.keyword_hit("구글 딥마인드, 새 algorithm 공개", "LG"))
        self.assertFalse(store.keyword_hit("Bailout 소식", "AI"))

    def test_로마자_키워드는_낱말로_서면_걸린다(self):
        self.assertTrue(store.keyword_hit("AI 반도체 수출이 늘었다", "AI"))
        self.assertTrue(store.keyword_hit("AI반도체 수출이 늘었다", "AI"))
        self.assertTrue(store.keyword_hit("LG전자, AI 가전 내놨다", "LG"))
        self.assertTrue(store.keyword_hit("정부, 'AI' 기본법 내놨다", "AI"))

    def test_앞머리만_같은_딴_말은_세지_않는다(self):
        self.assertFalse(store.keyword_hit("메타버스 열풍은 끝났다", "메타"))
        self.assertTrue(store.keyword_hit("메타, 새 모델을 공개했다", "메타"))
        # 메타버스 자체를 키워드로 넣으면 그때는 그대로 센다.
        self.assertTrue(store.keyword_hit("메타버스 열풍은 끝났다", "메타버스"))

    def test_조사와_준말이_붙어도_센다(self):
        # 한국어는 조사가 낱말에 그대로 붙는다. 뒤를 막으면 못 세는 쪽이 커진다.
        self.assertTrue(store.keyword_hit("구글이 요금을 내렸다", "구글"))
        self.assertTrue(store.keyword_hit("삼성전자 온디바이스 AI 탑재", "삼성"))
        self.assertTrue(store.keyword_hit("개인정보위 조사 착수", "개인정보"))

    def test_한글_키워드도_낱말_첫머리에서만_센다(self):
        self.assertFalse(store.keyword_hit("빅데이터센터 이야기", "데이터센터"))
        self.assertTrue(store.keyword_hit("새 데이터센터를 짓는다", "데이터센터"))

    def test_빈_값은_걸리지_않는다(self):
        self.assertFalse(store.keyword_hit("", "AI"))
        self.assertFalse(store.keyword_hit("AI 소식", ""))
        self.assertFalse(store.keyword_hit(None, "AI"))
        self.assertFalse(store.keyword_hit("AI 소식", None))


class KeywordCountsTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_TWO, day_two_articles(), now=SECOND_RUN_AT)

    def test_대소문자를_가리지_않고_센다(self):
        counts = store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, ["OpenAI"])
        self.assertEqual(counts["OpenAI"], 2)
        self.assertEqual(
            store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, ["openai"])["openai"], 2
        )

    def test_기간_밖_기사는_세지_않는다(self):
        counts = store.keyword_counts(self.conn, DAY_ONE, DAY_ONE, ["OpenAI", "소버린"])
        self.assertEqual(counts["OpenAI"], 1)
        self.assertEqual(counts["소버린"], 0)

    def test_양쪽_끝_날짜를_모두_포함한다(self):
        counts = store.keyword_counts(self.conn, DAY_TWO, DAY_TWO, ["소버린"])
        self.assertEqual(counts["소버린"], 1)

    def test_없는_키워드는_0으로_돌려준다(self):
        counts = store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, ["로봇청소기"])
        self.assertEqual(counts, {"로봇청소기": 0})

    def test_한_기사에_여러_번_나와도_한_건으로_센다(self):
        store.upsert_articles(
            self.conn,
            DAY_TWO,
            [
                article(
                    "https://ee.example/5",
                    "OpenAI 와 OpenAI 출신들이 다시 만났다",
                    "ee.example",
                    "20260911T080000Z",
                )
            ],
            now=SECOND_RUN_AT,
        )
        counts = store.keyword_counts(self.conn, DAY_TWO, DAY_TWO, ["OpenAI"])
        self.assertEqual(counts["OpenAI"], 2)

    def test_받은_키워드_순서와_표기를_지킨다(self):
        counts = store.keyword_counts(
            self.conn, DAY_ONE, DAY_TWO, ["규제", "OpenAI", "규제"]
        )
        self.assertEqual(list(counts.keys()), ["규제", "OpenAI"])

    def test_날짜를_거꾸로_줘도_같은_값을_낸다(self):
        바른순서 = store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, ["OpenAI"])
        뒤집은순서 = store.keyword_counts(self.conn, DAY_TWO, DAY_ONE, ["OpenAI"])
        self.assertEqual(바른순서, 뒤집은순서)

    def test_키워드_목록이_비면_빈_dict_다(self):
        self.assertEqual(store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, []), {})

    def test_낱말_안에_묻힌_말은_세지_않는다(self):
        # 리포트 표가 이 함수를 쓴다. 부분 문자열로 세던 때는 algorithm 한 건이
        # LG 한 건으로, 메타버스 한 건이 메타 한 건으로 올라갔다.
        store.upsert_articles(
            self.conn,
            DAY_TWO,
            [
                article(
                    "https://hh.example/8",
                    "구글 딥마인드, 새 algorithm 공개",
                    "hh.example",
                    "20260911T100000Z",
                ),
                article(
                    "https://ii.example/9",
                    "메타버스 열풍은 끝났다",
                    "ii.example",
                    "20260911T110000Z",
                ),
            ],
            now=SECOND_RUN_AT,
        )
        counts = store.keyword_counts(self.conn, DAY_TWO, DAY_TWO, ["LG", "메타", "구글"])
        self.assertEqual(counts["LG"], 0)
        self.assertEqual(counts["메타"], 0)
        self.assertEqual(counts["구글"], 1)

    def test_퍼센트_기호가_섞여도_엉뚱하게_걸리지_않는다(self):
        # LIKE 로 짰다면 % 가 아무 글자나 다 먹었을 자리다.
        counts = store.keyword_counts(self.conn, DAY_ONE, DAY_TWO, ["%AI%"])
        self.assertEqual(counts["%AI%"], 0)


class FirstSeenKeywordsTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_TWO, day_two_articles(), now=SECOND_RUN_AT)

    def test_어제_없다가_오늘_나온_것만_돌려준다(self):
        새말 = store.first_seen_keywords(
            self.conn, DAY_TWO, 1, ["OpenAI", "규제", "소버린"]
        )
        self.assertEqual(새말, ["소버린"])

    def test_기준_날짜에_없는_키워드는_빠진다(self):
        새말 = store.first_seen_keywords(self.conn, DAY_TWO, 1, ["규제"])
        self.assertEqual(새말, [])

    def test_돌아보는_기간을_늘리면_예전에_나온_말은_빠진다(self):
        store.upsert_articles(
            self.conn,
            "2026-09-05",
            [
                article(
                    "https://ff.example/6",
                    "소버린 AI 논의가 처음 나왔다",
                    "ff.example",
                    "20260905T020000Z",
                )
            ],
            now=FIRST_RUN_AT,
        )
        self.assertEqual(
            store.first_seen_keywords(self.conn, DAY_TWO, 1, ["소버린"]), ["소버린"]
        )
        self.assertEqual(store.first_seen_keywords(self.conn, DAY_TWO, 7, ["소버린"]), [])

    def test_기준_날짜_자신은_비교_기간에_넣지_않는다(self):
        # 같은 날 기사가 여러 건이어도 그것 때문에 처음 보는 말이 사라지면 안 된다.
        store.upsert_articles(
            self.conn,
            DAY_TWO,
            [
                article(
                    "https://gg.example/7",
                    "소버린 AI 예산이 늘었다",
                    "gg.example",
                    "20260911T090000Z",
                )
            ],
            now=SECOND_RUN_AT,
        )
        self.assertEqual(
            store.first_seen_keywords(self.conn, DAY_TWO, 7, ["소버린"]), ["소버린"]
        )

    def test_돌아보는_기간이_하루도_안_되면_거부한다(self):
        with self.assertRaises(ValueError):
            store.first_seen_keywords(self.conn, DAY_TWO, 0, ["소버린"])

    def test_다시_불러도_같은_값을_돌려준다(self):
        첫번째 = store.first_seen_keywords(self.conn, DAY_TWO, 7, ["OpenAI", "소버린"])
        두번째 = store.first_seen_keywords(self.conn, DAY_TWO, 7, ["OpenAI", "소버린"])
        self.assertEqual(첫번째, 두번째)


class DailyCountsTest(StoreTestCase):
    def test_기사가_없는_날은_0으로_채운다(self):
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_TWO, day_two_articles(), now=SECOND_RUN_AT)
        counts = store.daily_counts(self.conn, "2026-09-08", DAY_TWO)
        self.assertEqual(
            counts,
            {
                "2026-09-08": 0,
                "2026-09-09": 0,
                "2026-09-10": 2,
                "2026-09-11": 2,
            },
        )


class RecentAverageTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_articles(self.conn, DAY_ONE, day_one_articles(), now=FIRST_RUN_AT)
        store.upsert_articles(self.conn, DAY_TWO, day_two_articles(), now=SECOND_RUN_AT)

    def test_기사가_없는_날은_빼고_평균을_낸다(self):
        결과 = store.recent_average(self.conn, "2026-09-12", 7, min_days=2)
        self.assertEqual(결과, {"average": 2.0, "days_used": 2, "window": 7})

    def test_기록이_모자라면_평균을_내지_않는다(self):
        결과 = store.recent_average(self.conn, "2026-09-12", 7, min_days=3)
        self.assertIsNone(결과["average"])
        self.assertEqual(결과["days_used"], 2)

    def test_출처를_바꾸기_전_날은_뺀다(self):
        결과 = store.recent_average(self.conn, "2026-09-12", 7, since=DAY_TWO, min_days=1)
        self.assertEqual((결과["average"], 결과["days_used"]), (2.0, 1))


class DateValidationTest(StoreTestCase):
    def test_형식이_틀린_날짜는_거부한다(self):
        for 잘못된값 in ("20260911", "2026/09/11", "", None, "어제"):
            with self.subTest(값=잘못된값):
                with self.assertRaises(ValueError):
                    store.articles_for_date(self.conn, 잘못된값)

    def test_적재에서도_날짜를_먼저_확인한다(self):
        with self.assertRaises(ValueError):
            store.upsert_articles(self.conn, "20260911", day_one_articles())


if __name__ == "__main__":
    unittest.main()
