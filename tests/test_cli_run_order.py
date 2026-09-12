"""cli 가 단계를 어떤 순서로 밟는지 보는 시험.

여기서 보는 것은 세 가지다.
첫째, 날짜가 어긋난 기사는 DB 에 들어가기 전에 걸러져야 한다. url 을 기본키로 쓰는
store 는 한 번 들어간 기사의 kst_date 를 다시 덮어쓰지 않으므로, 적재가 점검보다
먼저면 잘못된 날에 매달린 기사가 영원히 남는다.
둘째, 판단이 점검보다 먼저 돌아야 걸러낸 비율을 그 날짜 첫 실행에서도 잰다.
셋째, 앞선 기록이 없는 DB 에서는 처음 보는 키워드를 브리핑에 넘기지 않아야 한다.
빈 DB 에서 세면 관심 키워드 전부가 처음 보는 말이 되어 매일 거짓을 적게 된다.

망은 타지 않는다. GDELT 자리에는 시험이 만든 부르개를 물린다.
"""

import io
import json
import contextlib
import datetime as dt
import logging
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from news_brief import cli, config

날짜 = "2026-09-11"


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def 기사(번호, seendate, title="오픈AI가 새 모델을 공개했다", domain="zdnet.co.kr"):
    """GDELT 응답 한 줄 모양."""
    return {
        "url": "https://%s/view/?no=%d" % (domain, 번호),
        "title": title,
        "seendate": seendate,
        "domain": domain,
        "language": "Korean",
    }


def 부르개(articles):
    """정해 둔 기사를 그대로 돌려주는 GDELT 대역. 구간과 무관하게 같은 것을 준다.

    구간으로 걸러 내지 않는 것은 일부러다. 픽스처 부르개는 구간 밖 기사를 스스로
    빼 버려서, 날짜가 어긋난 기사가 들어오는 상황을 재현할 수 없다.
    """

    def fetch(query, start_utc, end_utc, maxrecords=250, sleep_seconds=0):
        return {"articles": list(articles)}

    return fetch


class 뿌리(unittest.TestCase):
    """설정만 둔 임시 뿌리를 만들어 그 안에서만 돌린다."""

    def setUp(self):
        self.임시 = tempfile.TemporaryDirectory()
        self.addCleanup(self.임시.cleanup)
        self.뿌리 = Path(self.임시.name).resolve()
        (self.뿌리 / "config").mkdir(parents=True, exist_ok=True)
        (self.뿌리 / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "sleep_seconds": 0,
                    "max_split_depth": 1,
                    "notify": {"dry_run": True, "slack_enabled": False},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.뿌리 / "config" / "keywords.json").write_text(
            json.dumps(
                {
                    "keywords": [{"keyword": "오픈AI", "roman": "OpenAI"}],
                    "interest": ["오픈AI", "모델", "인공지능"],
                    "exclude": ["주가"],
                    "regulation": ["시행령"],
                    "tool": ["출시"],
                    "stopwords": ["오늘"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def 인자(self, **덮어쓸것):
        값 = {
            "date": 날짜,
            "backend": "rules",
            "dry_run": True,
            "root": str(self.뿌리),
            "fixtures": None,
            "verbose": False,
            "force": False,
        }
        값.update(덮어쓸것)
        return Namespace(**값)

    def 돌린다(self, articles, **덮어쓸것):
        """cmd_run 을 돌리고 (종료 코드, 화면에 찍은 글) 을 돌려준다."""
        찍힌것 = io.StringIO()
        with mock.patch.object(cli, "_fetcher_of", return_value=부르개(articles)):
            with contextlib.redirect_stdout(찍힌것):
                코드 = cli.cmd_run(self.인자(**덮어쓸것))
        return 코드, 찍힌것.getvalue()

    def DB의기사(self, date_str=날짜):
        from news_brief import store

        경로 = config.db_path(config.load_config(self.뿌리))
        if not Path(경로).exists():
            return []
        conn = store.open_db(str(경로))
        self.addCleanup(conn.close)
        return store.articles_for_date(conn, date_str)


class 적재순서시험(뿌리):
    """점검보다 적재가 먼저면 날짜가 어긋난 기사가 DB 에 박힌다."""

    def test_날짜가_어긋나면_DB_에_아무것도_넣지_않는다(self):
        묶음 = [
            기사(1, "20260911T013000Z"),
            기사(2, "20260911T043000Z"),
            기사(3, "20260910T233000Z"),
            # 한국 시간으로 09-12 인 기사. 이 한 건이 전체의 넷의 하나라 임계값을 넘는다.
            기사(4, "20260911T160000Z"),
        ]
        코드, 글 = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_CHECK_FAILED)
        self.assertIn("DB 에 넣지 않고 멈춘다", 글)
        self.assertEqual(self.DB의기사(), [], "멈춘 실행인데 기사가 DB 에 들어갔다")

    def test_점검을_통과하면_그때_넣는다(self):
        묶음 = [기사(1, "20260911T013000Z"), 기사(2, "20260911T043000Z")]
        코드, 글 = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_OK)
        self.assertEqual(len(self.DB의기사()), 2)
        self.assertLess(글.index("적재 전 점검"), 글.index("DB 에 넣었다"))


class 판단순서시험(뿌리):
    """걸러낸 비율 점검이 그 날짜 첫 실행에서도 도는지 본다."""

    def test_첫_실행에서도_걸러낸_비율을_잰다(self):
        묶음 = [
            기사(1, "20260911T013000Z"),
            기사(2, "20260911T043000Z", title="인공지능 기본법 시행령 입법예고"),
            기사(3, "20260911T053000Z", title="코스피 주가 급등"),
        ]
        코드, 글 = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_OK)
        self.assertIn("판단한 3건 가운데", 글)
        self.assertNotIn("판단을 아직 돌리지 않아 건너뛰었다", 글)
        # 판단이 점검보다 먼저 찍혀야 한다.
        self.assertLess(글.index("판단을 마쳤다"), 글.rindex("점검 결과"))


class 처음보는키워드시험(뿌리):
    """앞선 기록이 없으면 처음 보는 키워드를 브리핑에 넘기지 않는다."""

    def cfg(self):
        return config.load_config(self.뿌리)

    def 연결(self):
        from news_brief import store

        cfg = self.cfg()
        경로 = config.db_path(cfg)
        Path(경로).parent.mkdir(parents=True, exist_ok=True)
        conn = store.open_db(str(경로))
        self.addCleanup(conn.close)
        return conn, cfg

    def test_앞선_기록이_없으면_비우고_까닭을_남긴다(self):
        from news_brief import store

        conn, cfg = self.연결()
        묶음 = [기사(1, "20260911T013000Z")]
        store.upsert_articles(conn, 날짜, 묶음)
        with contextlib.redirect_stdout(io.StringIO()):
            stats = cli._stats_for(conn, 날짜, 묶음, cfg)
        self.assertEqual(stats["first_seen_keywords"], [])
        self.assertTrue(stats["first_seen_skipped"])
        self.assertIn("견줄", stats["first_seen_skipped_reason"])
        self.assertEqual(stats["history_article_count"], 0)

    def test_앞선_기록이_있으면_그대로_센다(self):
        from news_brief import store

        conn, cfg = self.연결()
        어제 = (dt.date.fromisoformat(날짜) - dt.timedelta(days=1)).isoformat()
        store.upsert_articles(
            conn,
            어제,
            [기사(9, "%sT013000Z" % 어제.replace("-", ""), title="반도체 소식")],
        )
        묶음 = [기사(1, "20260911T013000Z")]
        store.upsert_articles(conn, 날짜, 묶음)
        with contextlib.redirect_stdout(io.StringIO()):
            stats = cli._stats_for(conn, 날짜, 묶음, cfg)
        self.assertFalse(stats["first_seen_skipped"])
        self.assertEqual(stats["first_seen_skipped_reason"], "")
        self.assertEqual(stats["history_article_count"], 1)
        self.assertIn("오픈AI", stats["first_seen_keywords"])

    def test_브리핑에_처음_보는_키워드_절이_실리지_않는다(self):
        묶음 = [기사(1, "20260911T013000Z"), 기사(2, "20260911T043000Z")]
        코드, 글 = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_OK)
        브리핑 = config.brief_path(self.cfg(), 날짜).read_text(encoding="utf-8")
        self.assertNotIn("### 처음 보는 키워드", 브리핑)
        self.assertIn("처음 보는 키워드 절을 비운다", 글)

    def test_비운_까닭이_브리핑_본문까지_이어진다(self):
        # 절만 조용히 사라지면 읽는 사람은 어제 새 말이 하나도 없었다고 읽는다.
        # 집계가 담아 준 까닭이 브리핑 본문에 한 줄로 남아야 한다.
        묶음 = [기사(1, "20260911T013000Z"), 기사(2, "20260911T043000Z")]
        코드, _ = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_OK)
        브리핑 = config.brief_path(self.cfg(), 날짜).read_text(encoding="utf-8")
        self.assertIn("견줄 수가 없다.", 브리핑)
        self.assertIn("처음 보는 키워드는 비워 두었다.", 브리핑)


class 사람코멘트시험(unittest.TestCase):
    """리포트를 다시 만들 때 사람이 적은 글이 사라지지 않는지 본다."""

    옛글 = "\n".join(
        [
            "# 2026-09-07 주간 리포트",
            "",
            "## 키워드",
            "",
            "표",
            "",
            "## 사람 코멘트",
            "",
            "아래 인용 블록은 비워 둔다. 리포트를 읽고 직접 채운다.",
            "",
            "> 규제 소식이 우리 일정에 걸린다.",
            "> 다음 주에 법무팀과 한 번 본다.",
            "",
            "## 한계",
            "",
            "GDELT 는 색인해 둔 매체만 훑는다.",
            "",
        ]
    )
    새글 = "\n".join(
        [
            "# 2026-09-07 주간 리포트",
            "",
            "## 키워드",
            "",
            "새 표",
            "",
            "## 사람 코멘트",
            "",
            "아래 인용 블록은 비워 둔다. 리포트를 읽고 직접 채운다.",
            "",
            ">",
            ">",
            "",
            "## 한계",
            "",
            "GDELT 는 색인해 둔 매체만 훑는다.",
            "",
        ]
    )

    def test_사람이_적은_인용_블록을_새_리포트로_옮긴다(self):
        옮긴글, 줄수 = cli._carry_human_comment(self.옛글, self.새글)
        self.assertEqual(줄수, 2)
        self.assertIn("> 규제 소식이 우리 일정에 걸린다.", 옮긴글)
        self.assertIn("> 다음 주에 법무팀과 한 번 본다.", 옮긴글)
        # 나머지 본문은 새로 만든 쪽을 쓴다.
        self.assertIn("새 표", 옮긴글)
        self.assertNotIn("\n표\n", 옮긴글)

    def test_빈_인용_블록은_옮길_것이_없다(self):
        옮긴글, 줄수 = cli._carry_human_comment(self.새글, self.새글)
        self.assertEqual(줄수, 0)
        self.assertEqual(옮긴글, self.새글)

    def test_코멘트_절이_없는_글도_그냥_넘어간다(self):
        옮긴글, 줄수 = cli._carry_human_comment("# 제목만 있다\n", self.새글)
        self.assertEqual(줄수, 0)
        self.assertEqual(옮긴글, self.새글)


class 리포트덮어쓰기시험(뿌리):
    """같은 주를 다시 돌릴 때 리포트 파일을 어떻게 다루는지 본다."""

    def 가짜리포트(self, markdown):
        """report 모듈 자리에 정해진 마크다운만 돌려주는 대역을 물린다."""
        가짜 = mock.Mock()
        가짜.__name__ = "news_brief.report"
        가짜.build_report = mock.Mock(
            return_value={"markdown": markdown, "table": [], "chart_path": None}
        )
        본래 = cli._module

        def 골라준다(name):
            return 가짜 if name == "report" else 본래(name)

        return mock.patch.object(cli, "_module", side_effect=골라준다)

    def 리포트를만든다(self, markdown, **덮어쓸것):
        찍힌것 = io.StringIO()
        with self.가짜리포트(markdown):
            with mock.patch.object(cli, "_send_slack"), mock.patch.object(cli, "_send_notion"):
                with contextlib.redirect_stdout(찍힌것):
                    코드 = cli.cmd_report(self.인자(week="2026-09-07", **덮어쓸것))
        return 코드, 찍힌것.getvalue()

    def 리포트경로(self):
        return config.report_path(config.load_config(self.뿌리), "2026-09-07")

    def test_사람_코멘트를_옮기고_본문은_새로_쓴다(self):
        self.리포트를만든다(사람코멘트시험.새글)
        경로 = self.리포트경로()
        경로.write_text(사람코멘트시험.옛글, encoding="utf-8")
        코드, 글 = self.리포트를만든다(사람코멘트시험.새글.replace("새 표", "더 새 표"))
        self.assertEqual(코드, cli.EXIT_OK)
        남은글 = 경로.read_text(encoding="utf-8")
        self.assertIn("> 규제 소식이 우리 일정에 걸린다.", 남은글)
        self.assertIn("더 새 표", 남은글)
        self.assertIn("사람 코멘트", 글)

    def test_코멘트가_없어도_이미_있는_파일은_덮어쓰지_않는다(self):
        self.리포트를만든다(사람코멘트시험.새글)
        경로 = self.리포트경로()
        코드, 글 = self.리포트를만든다(사람코멘트시험.새글.replace("새 표", "더 새 표"))
        self.assertEqual(코드, cli.EXIT_OK)
        self.assertNotIn("더 새 표", 경로.read_text(encoding="utf-8"))
        self.assertIn("--force", 글)

    def test_force_를_붙이면_덮어쓴다(self):
        self.리포트를만든다(사람코멘트시험.새글)
        경로 = self.리포트경로()
        self.리포트를만든다(사람코멘트시험.새글.replace("새 표", "더 새 표"), force=True)
        self.assertIn("더 새 표", 경로.read_text(encoding="utf-8"))


class 못받은구간알림시험(unittest.TestCase):
    """수집 직후 한 줄이 상한에 걸린 구간과 막힌 구간을 갈라 적는지 본다.

    둘을 한 줄로 뭉치면 GDELT 가 막은 날을 상한 문제로 알아듣고 max_split_depth
    를 올리게 된다. 할 일이 서로 다르니 문장도 갈라 적는다.
    """

    def 찍힌글(self, 못받은구간):
        class 가짜수집기:
            @staticmethod
            def collect_day(date_str, cfg, fetcher=None):
                return {
                    "date": date_str,
                    "articles": [기사(1, "20260911T013000Z")],
                    "truncated_windows": list(못받은구간),
                }

        찍힌것 = io.StringIO()
        with mock.patch.object(cli, "_module", return_value=가짜수집기):
            with contextlib.redirect_stdout(찍힌것):
                cli._collect_only(날짜, {})
        return 찍힌것.getvalue()

    def test_상한에_걸린_구간만_있으면_상한만_적는다(self):
        글 = self.찍힌글([{"start": "20260910150000", "end": "20260911025959", "count": 250}])
        self.assertIn("250건 상한에 걸린 구간이 1개", 글)
        self.assertNotIn("GDELT 가 막아", 글)

    def test_막힌_구간은_다시_돌리라고_적는다(self):
        글 = self.찍힌글(
            [{"start": "20260910150000", "end": "20260911025959", "blocked": True}]
        )
        self.assertIn("GDELT 가 막아 받지 못한 구간이 1개", 글)
        self.assertNotIn("250건 상한에 걸린", 글)
        self.assertIn("다시 돌리면", 글)

    def test_두_가지가_섞이면_둘_다_적는다(self):
        글 = self.찍힌글(
            [
                {"start": "20260910150000", "end": "20260911025959", "count": 250},
                {"start": "20260911030000", "end": "20260911065959", "blocked": True},
            ]
        )
        self.assertIn("250건 상한에 걸린 구간이 1개", 글)
        self.assertIn("GDELT 가 막아 받지 못한 구간이 1개", 글)

    def test_못_받은_구간이_없으면_아무_말도_붙이지_않는다(self):
        글 = self.찍힌글([])
        self.assertNotIn("상한", 글)
        self.assertNotIn("막아", 글)


class 판단대조시험(뿌리):
    """판단이 다룬 건수와 DB 건수를 맞춰 보는 점검이 run 에서도 도는지 본다."""

    def test_정상_실행에서는_판단_대조가_통과다(self):
        묶음 = [기사(1, "20260911T013000Z"), 기사(2, "20260911T043000Z")]
        코드, 글 = self.돌린다(묶음)
        self.assertEqual(코드, cli.EXIT_OK)
        self.assertIn("판단 대조", 글)
        self.assertIn("DB 에 쌓인 건수와 같다", 글)


if __name__ == "__main__":
    unittest.main()
