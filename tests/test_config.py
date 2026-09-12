"""설정 모듈 시험.

보는 것은 셋이다.
하나, 한국 날짜를 UTC 구간으로 바꾸는 계산이 자정과 연말, 여름과 겨울에 다 맞는가.
둘, 설정 파일 두 개가 기본값 위에 제대로 겹치는가.
셋, 환경변수가 파일을 이기고 그 과정에서 자료형이 바로 바뀌는가.

환경변수를 건드리는 시험은 os.environ 을 통째로 비우고 시작한다.
돌리는 사람 컴퓨터에 NEWS_BRIEF_ 로 시작하는 값이 남아 있어도 결과가 흔들리지 않게 하려는 것이다.
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_brief import config  # noqa: E402


def setUpModule():
    """시험 중에는 설정 모듈의 경고를 덮는다. 설정 파일을 일부러 비워 두는 시험이 많아서
    없다는 경고가 줄줄이 찍히면 정작 볼 결과가 묻힌다."""
    logging.getLogger("news_brief.config").setLevel(logging.ERROR)


def _write_config(root, settings=None, keywords=None):
    """임시 프로젝트에 설정 파일 두 개를 깔아 둔다."""
    config_dir = Path(root) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (config_dir / "settings.json").write_text(
            json.dumps(settings, ensure_ascii=False), encoding="utf-8"
        )
    if keywords is not None:
        (config_dir / "keywords.json").write_text(
            json.dumps(keywords, ensure_ascii=False), encoding="utf-8"
        )
    return config_dir


class 하루경계시험(unittest.TestCase):
    """kst_day_window 가 한국 하루를 UTC 구간으로 옳게 바꾸는지 본다."""

    def test_하루는_전날_15시부터_그날_14시_59분_59초까지다(self):
        시작, 끝 = config.kst_day_window("2026-09-12")
        self.assertEqual(시작, "20260911150000")
        self.assertEqual(끝, "20260912145959")

    def test_돌려주는_값은_문자열_열네_자리다(self):
        for 값 in config.kst_day_window("2026-09-12"):
            self.assertIsInstance(값, str)
            self.assertEqual(len(값), 14)
            self.assertTrue(값.isdigit())

    def test_서머타임이_없어_여름과_겨울이_같다(self):
        여름시작, 여름끝 = config.kst_day_window("2026-07-15")
        겨울시작, 겨울끝 = config.kst_day_window("2026-01-15")
        self.assertEqual(여름시작, "20260714150000")
        self.assertEqual(여름끝, "20260715145959")
        self.assertEqual(겨울시작, "20260114150000")
        self.assertEqual(겨울끝, "20260115145959")

    def test_해가_바뀌는_자리도_하루_전으로_넘어간다(self):
        시작, 끝 = config.kst_day_window("2026-01-01")
        self.assertEqual(시작, "20251231150000")
        self.assertEqual(끝, "20260101145959")

    def test_윤년_2월_29일도_센다(self):
        시작, 끝 = config.kst_day_window("2028-02-29")
        self.assertEqual(시작, "20280228150000")
        self.assertEqual(끝, "20280229145959")

    def test_구간_길이는_하루에서_1초_모자란다(self):
        시작, 끝 = config.kst_day_window("2026-09-12")
        모양 = config.WINDOW_FORMAT
        간격 = datetime.strptime(끝, 모양) - datetime.strptime(시작, 모양)
        self.assertEqual(간격, timedelta(hours=23, minutes=59, seconds=59))

    def test_이틀치_구간은_서로_겹치지_않고_1초만_비운다(self):
        _, 첫날끝 = config.kst_day_window("2026-09-12")
        이튿날시작, _ = config.kst_day_window("2026-09-13")
        모양 = config.WINDOW_FORMAT
        틈 = datetime.strptime(이튿날시작, 모양) - datetime.strptime(첫날끝, 모양)
        self.assertEqual(틈, timedelta(seconds=1))

    def test_실제로_아홉_시간_차이가_난다(self):
        시작, _ = config.kst_day_window("2026-09-12")
        받은시각 = datetime.strptime(시작, config.WINDOW_FORMAT).replace(tzinfo=timezone.utc)
        한국자정 = datetime(2026, 9, 12, 0, 0, 0, tzinfo=config.seoul_timezone())
        self.assertEqual(받은시각, 한국자정.astimezone(timezone.utc))

    def test_날짜_객체도_받는다(self):
        from datetime import date

        self.assertEqual(
            config.kst_day_window(date(2026, 9, 12)),
            config.kst_day_window("2026-09-12"),
        )

    def test_모양이_틀린_날짜는_멈춘다(self):
        for 나쁜값 in ("2026/09/12", "20260912", "", None, "어제"):
            with self.subTest(값=나쁜값):
                with self.assertRaises(ValueError):
                    config.kst_day_window(나쁜값)

    def test_없는_날짜도_멈춘다(self):
        with self.assertRaises(ValueError):
            config.kst_day_window("2026-02-30")


class 날짜잔손시험(unittest.TestCase):
    """어제와 지난주를 구하는 잔손이 맞는지 본다."""

    def test_날짜를_앞뒤로_옮긴다(self):
        self.assertEqual(config.shift_date("2026-09-12", -1), "2026-09-11")
        self.assertEqual(config.shift_date("2026-01-01", -1), "2025-12-31")
        self.assertEqual(config.shift_date("2026-09-12", 7), "2026-09-19")

    def test_그_주_월요일을_찾는다(self):
        self.assertEqual(config.week_start_of("2026-09-12"), "2026-09-07")
        self.assertEqual(config.week_start_of("2026-09-07"), "2026-09-07")
        self.assertEqual(config.week_start_of("2026-09-13"), "2026-09-07")

    def test_오늘은_한국_날짜로_센다(self):
        오늘 = config.kst_today()
        self.assertEqual(오늘, datetime.now(config.seoul_timezone()).date().isoformat())


class 설정병합시험(unittest.TestCase):
    """파일 두 개가 기본값 위에 겹치는 방식을 본다."""

    def setUp(self):
        self.임시 = tempfile.TemporaryDirectory()
        self.뿌리 = Path(self.임시.name).resolve()
        self.addCleanup(self.임시.cleanup)
        self.빈환경 = mock.patch.dict(os.environ, {}, clear=True)
        self.빈환경.start()
        self.addCleanup(self.빈환경.stop)

    def test_파일이_없어도_기본값으로_돌아간다(self):
        cfg = config.load_config(self.뿌리)
        self.assertEqual(cfg["maxrecords"], 250)
        self.assertEqual(cfg["sleep_seconds"], 5)
        self.assertEqual(cfg["judge"]["backend"], "rules")
        self.assertEqual(cfg["keywords"], {})

    def test_설정_파일이_없어도_점검_임계값은_안전한_쪽이다(self):
        # 파일이 없는 환경에서도 날짜 범위 임계값이 있어야 하고,
        # 링크 대조는 꺼진 쪽이 기본이어야 한다. 켜져 있으면 아무 설정 없이
        # 돌린 사람의 실행이 바깥으로 요청을 내보낸다.
        cfg = config.load_config(self.뿌리)
        self.assertGreater(cfg["qa"]["max_stray_ratio"], 0)
        self.assertIs(cfg["qa"]["link_check"]["enabled"], False)

    def test_링크_대조만_따로_켤_수_있다(self):
        _write_config(self.뿌리, settings={"qa": {"link_check": {"enabled": True}}})
        cfg = config.load_config(self.뿌리)
        self.assertIs(cfg["qa"]["link_check"]["enabled"], True)
        # 나머지 값은 기본값이 그대로 남아야 한다.
        self.assertEqual(cfg["qa"]["link_check"]["sample"], 3)
        self.assertEqual(cfg["qa"]["max_drop_ratio"], 0.7)

    def test_파일_값이_기본값을_이긴다(self):
        _write_config(self.뿌리, settings={"query": "다른 검색어", "maxrecords": 100})
        cfg = config.load_config(self.뿌리)
        self.assertEqual(cfg["query"], "다른 검색어")
        self.assertEqual(cfg["maxrecords"], 100)

    def test_안쪽_묶음은_적은_것만_바뀐다(self):
        _write_config(self.뿌리, settings={"judge": {"backend": "llm"}})
        cfg = config.load_config(self.뿌리)
        self.assertEqual(cfg["judge"]["backend"], "llm")
        # 파일에 적지 않은 값은 기본값 그대로 남아 있어야 한다.
        self.assertEqual(cfg["judge"]["similarity_threshold"], 0.4)
        self.assertEqual(cfg["judge"]["model"], "claude-opus-5")

    def test_목록은_이어붙이지_않고_갈아_끼운다(self):
        _write_config(self.뿌리, settings={"report": {"font_candidates": ["내글꼴"]}})
        cfg = config.load_config(self.뿌리)
        self.assertEqual(cfg["report"]["font_candidates"], ["내글꼴"])

    def test_키워드_파일은_keywords_아래로_들어간다(self):
        _write_config(
            self.뿌리,
            keywords={"keywords": ["엔비디아"], "exclude": ["주가"], "interest": ["인공지능"]},
        )
        cfg = config.load_config(self.뿌리)
        self.assertEqual(cfg["keywords"]["exclude"], ["주가"])
        self.assertEqual(config.watch_keywords(cfg), ["엔비디아"])

    def test_사전_모양_키워드에서도_이름만_뽑는다(self):
        _write_config(
            self.뿌리,
            keywords={"keywords": [{"keyword": "엔비디아", "roman": "Nvidia"}, "구글", "구글"]},
        )
        cfg = config.load_config(self.뿌리)
        self.assertEqual(config.watch_keywords(cfg), ["엔비디아", "구글"])

    def test_경로는_절대경로로_펴진다(self):
        cfg = config.load_config(self.뿌리)
        for 이름 in ("data_dir", "db", "briefs_dir", "reports_dir", "raw_dir"):
            with self.subTest(경로=이름):
                self.assertTrue(Path(cfg["paths"][이름]).is_absolute())
                self.assertTrue(cfg["paths"][이름].startswith(str(self.뿌리)))
        self.assertEqual(cfg["root"], str(self.뿌리))

    def test_산출물_자리를_계산한다(self):
        cfg = config.load_config(self.뿌리)
        self.assertEqual(config.db_path(cfg), self.뿌리 / "data" / "news.db")
        self.assertEqual(
            config.brief_path(cfg, "2026-09-12"),
            self.뿌리 / "data" / "briefs" / "2026-09-12.md",
        )
        self.assertEqual(
            config.report_path(cfg, "2026-09-07"),
            self.뿌리 / "data" / "reports" / "2026-09-07" / "report.md",
        )

    def test_깨진_JSON_은_어느_줄인지_알려주며_멈춘다(self):
        (self.뿌리 / "config").mkdir(parents=True, exist_ok=True)
        (self.뿌리 / "config" / "settings.json").write_text("{\n  \"query\":\n}", encoding="utf-8")
        with self.assertRaises(ValueError) as 잡은것:
            config.load_config(self.뿌리)
        self.assertIn("settings.json", str(잡은것.exception))

    def test_점으로_설정값을_꺼낸다(self):
        cfg = config.load_config(self.뿌리)
        self.assertEqual(config.setting(cfg, "judge.backend"), "rules")
        self.assertEqual(config.setting(cfg, "없는.자리", "기본"), "기본")


class 환경변수시험(unittest.TestCase):
    """환경변수가 파일을 이기는지, 자료형이 제대로 바뀌는지 본다."""

    def setUp(self):
        self.임시 = tempfile.TemporaryDirectory()
        self.뿌리 = Path(self.임시.name).resolve()
        self.addCleanup(self.임시.cleanup)
        _write_config(self.뿌리, settings={"query": "파일 검색어", "maxrecords": 100})

    def _설정(self, **환경):
        with mock.patch.dict(os.environ, 환경, clear=True):
            return config.load_config(self.뿌리)

    def test_검색어를_덮어쓴다(self):
        cfg = self._설정(NEWS_BRIEF_QUERY="환경변수 검색어")
        self.assertEqual(cfg["query"], "환경변수 검색어")

    def test_숫자는_숫자로_바뀐다(self):
        cfg = self._설정(NEWS_BRIEF_MAXRECORDS="50", NEWS_BRIEF_SLEEP_SECONDS="2.5")
        self.assertEqual(cfg["maxrecords"], 50)
        self.assertIsInstance(cfg["maxrecords"], int)
        self.assertEqual(cfg["sleep_seconds"], 2.5)

    def test_참거짓은_여러_표기를_받는다(self):
        for 값, 기대 in (("false", False), ("0", False), ("no", False),
                        ("true", True), ("1", True), ("yes", True)):
            with self.subTest(값=값):
                cfg = self._설정(NEWS_BRIEF_DRY_RUN=값)
                self.assertIs(cfg["notify"]["dry_run"], 기대)

    def test_안쪽_묶음도_덮어쓴다(self):
        cfg = self._설정(NEWS_BRIEF_BACKEND="llm", NEWS_BRIEF_TOP_KEYWORDS="3")
        self.assertEqual(cfg["judge"]["backend"], "llm")
        self.assertEqual(cfg["report"]["top_keywords"], 3)

    def test_빈_값은_못_본_셈_친다(self):
        cfg = self._설정(NEWS_BRIEF_QUERY="", NEWS_BRIEF_MAXRECORDS="  ")
        self.assertEqual(cfg["query"], "파일 검색어")
        self.assertEqual(cfg["maxrecords"], 100)

    def test_숫자가_아닌_값은_조용히_넘기지_않고_멈춘다(self):
        with self.assertRaises(ValueError) as 잡은것:
            self._설정(NEWS_BRIEF_MAXRECORDS="이백오십")
        self.assertIn("NEWS_BRIEF_MAXRECORDS", str(잡은것.exception))

    def test_참거짓이_아닌_값도_멈춘다(self):
        with self.assertRaises(ValueError):
            self._설정(NEWS_BRIEF_DRY_RUN="아마도")

    def test_경로_환경변수는_절대경로가_된다(self):
        cfg = self._설정(NEWS_BRIEF_DB="다른곳/news.db")
        self.assertEqual(cfg["paths"]["db"], str(self.뿌리 / "다른곳" / "news.db"))
        cfg = self._설정(NEWS_BRIEF_DB="/tmp/news.db")
        self.assertEqual(cfg["paths"]["db"], "/tmp/news.db")

    def test_뒷문으로_아무_자리나_바꾼다(self):
        cfg = self._설정(**{"NEWS_BRIEF_CFG__judge__max_tokens": "1234"})
        self.assertEqual(cfg["judge"]["max_tokens"], 1234)
        cfg = self._설정(**{"NEWS_BRIEF_CFG__brief__max_groups": "4"})
        self.assertEqual(cfg["brief"]["max_groups"], 4)

    def test_뒷문은_글자도_받는다(self):
        cfg = self._설정(**{"NEWS_BRIEF_CFG__judge__model": "claude-sonnet-5"})
        self.assertEqual(cfg["judge"]["model"], "claude-sonnet-5")

    def test_비밀값은_있는지만_적고_값은_담지_않는다(self):
        cfg = self._설정(SLACK_WEBHOOK_URL="https://hooks.slack.com/services/비밀")
        self.assertIs(cfg["secrets"]["slack_webhook"], True)
        self.assertIs(cfg["secrets"]["notion_token"], False)
        self.assertNotIn("비밀", json.dumps(cfg, ensure_ascii=False))


class 실제설정파일시험(unittest.TestCase):
    """저장소에 든 config/*.json 이 실제로 읽히고 필요한 것을 다 갖췄는지 본다."""

    @classmethod
    def setUpClass(cls):
        cls.뿌리 = Path(__file__).resolve().parents[1]
        with mock.patch.dict(os.environ, {}, clear=True):
            cls.cfg = config.load_config(cls.뿌리)

    def test_설정_파일이_실제로_있다(self):
        self.assertTrue((self.뿌리 / "config" / "settings.json").exists())
        self.assertTrue((self.뿌리 / "config" / "keywords.json").exists())

    def test_수집에_필요한_값이_다_있다(self):
        self.assertIn("sourcelang:korean", self.cfg["query"])
        self.assertEqual(self.cfg["maxrecords"], 250)
        self.assertEqual(self.cfg["sleep_seconds"], 5)
        self.assertGreaterEqual(self.cfg["max_split_depth"], 1)

    def test_점검_임계값이_다_있다(self):
        qa = self.cfg["qa"]
        self.assertEqual(qa["lookback_days"], 7)
        self.assertEqual(qa["max_drop_ratio"], 0.7)
        # 날짜 범위를 비율로 보는 임계값. 없으면 한 건 때문에 하루를 통째로 잃는다.
        self.assertIn("max_stray_ratio", qa)
        self.assertGreater(qa["max_stray_ratio"], 0)
        self.assertLess(qa["max_stray_ratio"], 1)

    def test_링크_대조_점검은_기본이_꺼짐이다(self):
        # 픽스처 링크는 손으로 지어낸 기사 번호라 켜 두면 늘 어긋남으로 나온다.
        # 게다가 이 점검만 바깥으로 요청을 내보내므로 기본은 꺼져 있어야 한다.
        링크 = self.cfg["qa"]["link_check"]
        self.assertIs(링크["enabled"], False)
        self.assertGreaterEqual(링크["sample"], 1)
        self.assertGreater(링크["min_overlap"], 0)
        self.assertGreater(링크["timeout_seconds"], 0)

    def test_판단과_브리핑_설정이_있다(self):
        self.assertEqual(self.cfg["judge"]["backend"], "rules")
        self.assertGreater(self.cfg["judge"]["similarity_threshold"], 0)
        self.assertEqual(self.cfg["brief"]["min_groups"], 3)
        self.assertEqual(self.cfg["brief"]["max_groups"], 5)
        self.assertEqual(self.cfg["report"]["top_keywords"], 5)

    def test_키워드_목록이_비어_있지_않다(self):
        keywords = self.cfg["keywords"]
        for 이름 in ("keywords", "interest", "regulation", "tool", "exclude", "stopwords"):
            with self.subTest(목록=이름):
                self.assertTrue(keywords.get(이름), "{0} 목록이 비어 있다".format(이름))
        self.assertGreaterEqual(len(config.watch_keywords(self.cfg)), 10)

    def test_관심_키워드는_두_글자_넘고_로마자_표기가_붙어_있다(self):
        for 항목 in self.cfg["keywords"]["keywords"]:
            with self.subTest(키워드=항목):
                self.assertGreaterEqual(len(항목["keyword"]), 2)
                self.assertTrue(항목["roman"].isascii())

    def test_설정_파일에_비밀값이_적혀_있지_않다(self):
        글 = (self.뿌리 / "config" / "settings.json").read_text(encoding="utf-8")
        for 수상한말 in ("hooks.slack.com", "secret_", "sk-ant", "Bearer "):
            with self.subTest(말=수상한말):
                self.assertNotIn(수상한말, 글)

    def test_처음_보는_키워드_개수도_설정에_있다(self):
        # 브리핑이 brief.max_keywords 를 읽는다. 설정에 없으면 코드 상수만 먹어
        # 파일을 고쳐도 아무 일도 일어나지 않는다.
        self.assertGreaterEqual(self.cfg["brief"]["max_keywords"], 1)
        self.assertEqual(
            self.cfg["brief"]["max_keywords"],
            config.DEFAULT_SETTINGS["brief"]["max_keywords"],
        )

    def test_쪼개기_깊이가_수집기_상한_안에_있다(self):
        # 설정에 상한보다 큰 값을 적어 두면 수집기가 조용히 깎아 쓰고 경고만 남는다.
        from news_brief import collect

        self.assertLessEqual(self.cfg["max_split_depth"], collect.MAX_SPLIT_DEPTH_LIMIT)

    def test_코드_기본값과_설정_파일이_같은_자리를_갖췄다(self):
        # config.py 의 기본값은 설정 파일과 같은 값을 적어 둔 것이다. 한쪽에만
        # 있는 자리가 생기면 파일 없이 돌린 결과와 파일로 돌린 결과가 갈린다.
        def 자리들(묶음, 앞=""):
            for 이름, 값 in 묶음.items():
                자리 = "{0}{1}".format(앞, 이름)
                if isinstance(값, dict):
                    yield from 자리들(값, 자리 + ".")
                else:
                    yield 자리

        파일 = json.loads(
            (self.뿌리 / "config" / "settings.json").read_text(encoding="utf-8")
        )
        빠진것 = [
            자리
            for 자리 in 자리들(config.DEFAULT_SETTINGS)
            if config.setting(파일, 자리, "없음") == "없음"
        ]
        self.assertEqual(빠진것, [], "설정 파일에 없는 기본값 자리 %s" % 빠진것)


class 실제키워드파일시험(unittest.TestCase):
    """효준님이 직접 고치는 config/keywords.json 이 제 모양인지 본다.

    이 파일은 사람이 손으로 고치는 자리라 두 가지를 지켜야 한다. 하나, 목록마다
    무엇을 정하는 목록인지 설명이 붙어 있어야 한다. 둘, 세는 목록에 서로를 품는
    낱말이 없어야 한다. 한쪽이 다른 쪽을 품으면 한 기사가 두 번 세어져 표가 부푼다.
    """

    @classmethod
    def setUpClass(cls):
        cls.뿌리 = Path(__file__).resolve().parents[1]
        cls.낱말 = json.loads(
            (cls.뿌리 / "config" / "keywords.json").read_text(encoding="utf-8")
        )

    def test_목록마다_설명이_붙어_있다(self):
        for 이름 in ("keywords", "interest", "regulation", "tool", "exclude", "stopwords"):
            with self.subTest(목록=이름):
                설명 = self.낱말.get(이름 + "_설명", "")
                self.assertTrue(설명.strip(), "%s 목록에 설명이 없다" % 이름)
                self.assertGreater(len(설명), 20, "%s 설명이 너무 짧다" % 이름)

    def test_고치는_법과_주의를_적어_두었다(self):
        머리 = self.낱말.get("설명") or {}
        붙인글 = " ".join(str(값) for 값 in 머리.values())
        self.assertIn("--force", 붙인글)
        self.assertIn("띄어쓰기", 붙인글)

    def test_회사와_모델_이름이_들어_있다(self):
        이름들 = [항목["keyword"] for 항목 in self.낱말["keywords"]]
        for 이름 in (
            "오픈AI", "OpenAI", "챗GPT", "구글", "제미나이", "앤스로픽", "클로드",
            "삼성", "SK하이닉스", "네이버", "카카오", "LG", "엔비디아",
        ):
            with self.subTest(이름=이름):
                self.assertIn(이름, 이름들)

    def test_주제어가_들어_있다(self):
        이름들 = [항목["keyword"] for 항목 in self.낱말["keywords"]]
        for 이름 in (
            "에이전트", "데이터센터", "반도체", "규제", "개인정보",
            "AI 기본법", "채용", "보안",
        ):
            with self.subTest(이름=이름):
                self.assertIn(이름, 이름들)

    def test_세는_목록에_서로를_품는_낱말이_없다(self):
        from news_brief import store

        이름들 = [항목["keyword"] for 항목 in self.낱말["keywords"]]
        겹친것 = [
            (긴것, 짧은것)
            for 긴것 in 이름들
            for 짧은것 in 이름들
            if 긴것 != 짧은것 and store.keyword_hit(긴것, 짧은것)
        ]
        self.assertEqual(겹친것, [], "한 기사가 두 번 세어질 짝 %s" % 겹친것)

    def test_같은_낱말을_두_번_적지_않았다(self):
        for 이름 in ("interest", "regulation", "tool", "exclude", "stopwords"):
            with self.subTest(목록=이름):
                목록 = self.낱말[이름]
                self.assertEqual(len(목록), len(set(목록)))
        키워드 = [항목["keyword"] for 항목 in self.낱말["keywords"]]
        self.assertEqual(len(키워드), len(set(키워드)))

    def test_규제_판정어에_부처와_절차가_들어_있다(self):
        for 이름 in (
            "과기정통부", "개인정보위", "공정위", "시행령", "입법예고",
            "지침", "가이드라인",
        ):
            with self.subTest(이름=이름):
                self.assertIn(이름, self.낱말["regulation"])

    def test_도구_판정어가_활용형을_덮는다(self):
        from news_brief import judge

        목록 = self.낱말["tool"]
        for 이름 in ("출시", "공개", "업데이트"):
            with self.subTest(이름=이름):
                self.assertIn(이름, 목록)
        # 줄기만 적어 두어도 활용형이 걸려야 한다.
        for 제목 in ("네이버가 새 모델을 선보였다", "오픈AI가 새 요금제를 내놨다"):
            with self.subTest(제목=제목):
                self.assertTrue(
                    any(judge._키워드있나(제목.lower(), 말) for 말 in 목록),
                    "도구 소식인데 판정어가 하나도 걸리지 않는다",
                )

    def test_제외어가_증시와_잡_꼭지를_덮는다(self):
        for 이름 in ("주가", "코스피", "수혜주", "특징주", "부고", "운세"):
            with self.subTest(이름=이름):
                self.assertIn(이름, self.낱말["exclude"])


if __name__ == "__main__":
    unittest.main()
