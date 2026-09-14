"""250건 상한에 실제로 걸리는 하루를 흉내 내는 시험.

tests/test_collect.py 는 상한을 4건으로 낮춰 쪼개기 얼개만 본다. 숫자가
작으니 빠르지만, 실제 하루가 상한에 걸렸을 때 무슨 일이 벌어지는지는
그 시험으로 알 수 없다. 여기서는 상한을 실제 값인 250 으로 두고 하루에
수백 건이 있는 날을 만들어, 쪼갠 구간이 몇 개가 되는지, 쪼갠 경계에서
기사가 새지 않는지, 그 결과가 점검을 통과하는지까지 이어서 본다.

망은 타지 않는다. 하루치 응답을 임시 폴더에 파일로 써 두고
collect.fixture_fetcher 로 읽어 들인다. 파일에서 읽어 오게 한 것은
실제 API 를 부를 때와 같은 길, 곧 재귀로 구간을 쪼개며 여러 번 부르는
길을 그대로 지나가게 하려는 것이다.

응답 파일을 tests/fixtures 에 박아 두지 않고 시험 안에서 만드는 까닭이 있다.
이 표본은 지어낸 값이라 실호출로 받아 둔 응답이 아니다. 실호출 응답처럼
보이는 파일을 이름만 그럴싸하게 붙여 저장소에 남기면, 나중에 그것을 실측
자료로 착각하기 쉽다. 지어낸 표본은 지어낸 자리에서만 살게 두었다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from news_brief import collect, config, gdelt, netssl, qa, store

WINDOW_FORMAT = "%Y%m%d%H%M%S"
대상날짜 = "2026-09-11"

# 하루에 이만큼 있는 날을 흉내 낸다. 250 의 두 배를 넘겨 두었기 때문에
# 하루 통째로도 상한에 걸리고, 절반으로 쪼개도 한 번 더 걸린다.
하루건수 = 620


def setUpModule():
    # 상한에 걸렸다는 경고가 시험 결과에 섞이지 않게 잠시 막아 둔다.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _시각(stamp):
    """datetime 을 GDELT 가 쓰는 'YYYYMMDDTHHMMSSZ' 모양으로 바꾼다."""
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def 하루치_응답(건수=하루건수, date_str=대상날짜):
    """한국 날짜 하루를 고르게 채운 artlist 응답을 만든다.

    시각을 구간 안에 고르게 흩어 둔 것은, 절반으로 쪼갰을 때 양쪽에 비슷한
    수가 들어가게 하려는 것이다. 한쪽에 몰아 두면 쪼개도 한쪽만 상한에
    걸려서 경계에서 기사가 새는지를 보기 어렵다.
    """
    시작, 끝 = config.kst_day_window(date_str)
    처음 = dt.datetime.strptime(시작, WINDOW_FORMAT)
    마지막 = dt.datetime.strptime(끝, WINDOW_FORMAT)
    간격 = (마지막 - 처음) / max(건수 - 1, 1)

    매체 = ("yna.co.kr", "mk.co.kr", "etnews.com", "zdnet.co.kr", "hani.co.kr")
    기사들 = []
    for 번호 in range(건수):
        찍힌때 = 처음 + 간격 * 번호
        도메인 = 매체[번호 % len(매체)]
        기사들.append(
            {
                "url": "https://www.%s/view/?no=2026091%05d" % (도메인, 번호),
                "title": "AI 소식 %d번" % 번호,
                "seendate": _시각(찍힌때),
                "domain": 도메인,
                "language": "Korean",
                "sourcecountry": "South Korea",
            }
        )
    return {"articles": 기사들}


def 빈껍데기():
    """CA 를 하나도 들고 있지 않은 컨텍스트.

    맥에 python.org 파이썬을 깔고 'Install Certificates.command' 를 돌리지
    않은 직후의 모습이다. 검증은 켜져 있는데 검증할 CA 가 없는 상태다.
    """
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def 꽉찬_부르개(maxrecords=250):
    """어떤 구간을 불러도 상한이 꽉 차는 부르개를 만든다.

    쪼개고 쪼개도 상한 아래로 내려가지 않는 날을 흉내 낸다. 구간마다 서로
    다른 링크를 주기 때문에 겹친 링크 때문에 건수가 줄어드는 일은 없다.
    """

    def fetch(query, start_utc, end_utc, maxrecords=maxrecords, sleep_seconds=0):
        처음 = dt.datetime.strptime(start_utc, WINDOW_FORMAT)
        마지막 = dt.datetime.strptime(end_utc, WINDOW_FORMAT)
        간격 = (마지막 - 처음) / max(maxrecords, 1)
        기사들 = [
            {
                "url": "https://www.zdnet.co.kr/view/?no=%s-%d" % (start_utc, 번호),
                "title": "AI 소식 %s-%d" % (start_utc, 번호),
                "seendate": _시각(처음 + 간격 * 번호),
                "domain": "zdnet.co.kr",
                "language": "Korean",
            }
            for 번호 in range(maxrecords)
        ]
        return {"articles": 기사들}

    return fetch


class 상한에_걸린_하루Test(unittest.TestCase):
    """하루 620건을 상한 250건으로 받아 오는 길을 끝까지 따라간다."""

    def setUp(self):
        self.작업방 = tempfile.TemporaryDirectory()
        self.뿌리 = Path(self.작업방.name)
        self.응답방 = self.뿌리 / "응답"
        self.응답방.mkdir()
        self.응답 = 하루치_응답()
        with (self.응답방 / ("gdelt_%s.json" % 대상날짜)).open("w", encoding="utf-8") as 파일:
            json.dump(self.응답, 파일, ensure_ascii=False)

        self.설정 = {
            "query": '("artificial intelligence" OR OpenAI) sourcelang:korean',
            "maxrecords": 250,
            "max_split_depth": 4,
            "sleep_seconds": 0,
            "paths": {"data_dir": str(self.뿌리 / "data")},
        }
        self.결과 = collect.collect_day(
            대상날짜,
            self.설정,
            fetcher=collect.fixture_fetcher(str(self.응답방)),
        )

    def tearDown(self):
        self.작업방.cleanup()

    def test_하루_통째로는_상한에_걸린다(self):
        첫구간 = self.결과["windows"][0]
        self.assertEqual(첫구간["count"], 250)
        self.assertTrue(첫구간["hit_cap"])
        self.assertTrue(첫구간["split"])

    def test_쪼갠_구간이_일곱개가_된다(self):
        # 620건이면 하루 통째로 한 번, 절반씩 두 번, 네 토막으로 네 번 부른다.
        구간들 = self.결과["windows"]
        self.assertEqual(len(구간들), 7)
        self.assertEqual([구간["depth"] for 구간 in 구간들].count(0), 1)
        self.assertEqual([구간["depth"] for 구간 in 구간들].count(1), 2)
        self.assertEqual([구간["depth"] for 구간 in 구간들].count(2), 4)
        self.assertEqual(max(구간["depth"] for 구간 in 구간들), 2)

    def test_네_토막까지_내려가면_상한_아래로_떨어진다(self):
        잎 = [구간 for 구간 in self.결과["windows"] if 구간["depth"] == 2]
        self.assertEqual(len(잎), 4)
        for 구간 in 잎:
            self.assertLess(구간["count"], 250)
            self.assertFalse(구간["hit_cap"])
            self.assertFalse(구간["split"])

    def test_쪼갠_뒤에는_상한에_걸린_구간이_남지_않는다(self):
        self.assertEqual(self.결과["truncated_windows"], [])

    def test_쪼개도_기사가_새지_않는다(self):
        # 경계 시각을 양쪽 구간이 함께 쓰기 때문에 같은 기사를 두 번 받을 수는
        # 있어도 빠뜨릴 수는 없다. 620건이 그대로 다 돌아와야 한다.
        self.assertEqual(len(self.결과["articles"]), 하루건수)
        받은링크 = {기사["url"] for 기사 in self.결과["articles"]}
        보낸링크 = {기사["url"] for 기사 in self.응답["articles"]}
        self.assertEqual(받은링크, 보낸링크)

    def test_겹쳐_받은_기사는_한_건으로_줄인다(self):
        # 부른 건수를 다 더하면 620보다 많다. 겹친 몫을 걸러내 620이 된 것이다.
        부른건수 = sum(구간["count"] for 구간 in self.결과["windows"])
        self.assertGreater(부른건수, 하루건수)
        링크들 = [기사["url"] for 기사 in self.결과["articles"]]
        self.assertEqual(len(링크들), len(set(링크들)))

    def test_원본과_구간_기록을_파일로_남긴다(self):
        남긴파일 = Path(self.결과["raw_path"])
        self.assertTrue(남긴파일.exists())
        문서 = json.loads(남긴파일.read_text(encoding="utf-8"))
        self.assertEqual(문서["date"], 대상날짜)
        self.assertEqual(len(문서["windows"]), 7)
        self.assertEqual(문서["truncated_windows"], [])
        self.assertEqual(len(문서["responses"]), 7)

    def test_쪼갠_하루도_점검을_통과한다(self):
        연결 = store.open_db(":memory:")
        self.addCleanup(연결.close)
        store.upsert_articles(연결, 대상날짜, self.결과["articles"])
        점검 = qa.run_checks(연결, 대상날짜, self.결과, self.설정)

        self.assertEqual(점검["exit_code"], 0)
        상태 = {줄["name"]: 줄["status"] for 줄 in 점검["checks"]}
        self.assertEqual(상태["수집 건수"], qa.OK)
        self.assertEqual(상태["상한 구간"], qa.OK)
        self.assertEqual(상태["날짜 범위"], qa.OK)
        self.assertEqual(상태["필수 필드"], qa.OK)
        self.assertEqual(상태["링크 중복"], qa.OK)
        self.assertEqual(상태["적재 대조"], qa.OK)
        self.assertNotIn(qa.FAIL, 상태.values())


class 쪼개도_상한이_남는_하루Test(unittest.TestCase):
    """깊이 한계까지 쪼개도 상한이 차는 날은 브리핑을 만들지 않고 멈춘다."""

    def setUp(self):
        self.작업방 = tempfile.TemporaryDirectory()
        self.설정 = {
            "maxrecords": 250,
            "max_split_depth": 2,
            "sleep_seconds": 0,
            "paths": {"data_dir": str(Path(self.작업방.name) / "data")},
        }
        self.결과 = collect.collect_day(대상날짜, self.설정, fetcher=꽉찬_부르개())

    def tearDown(self):
        self.작업방.cleanup()

    def test_깊이_한계에_닿으면_자국을_남긴다(self):
        self.assertEqual(len(self.결과["windows"]), 7)
        남은것 = self.결과["truncated_windows"]
        self.assertEqual(len(남은것), 4)
        for 자국 in 남은것:
            self.assertEqual(자국["count"], 250)
            self.assertEqual(자국["depth"], 2)
            self.assertIn("깊이", 자국["reason"])

    def test_점검이_상한_구간에서_멈춘다(self):
        연결 = store.open_db(":memory:")
        self.addCleanup(연결.close)
        store.upsert_articles(연결, 대상날짜, self.결과["articles"])
        점검 = qa.run_checks(연결, 대상날짜, self.결과, self.설정)

        self.assertEqual(점검["exit_code"], 1)
        상한줄 = next(줄 for 줄 in 점검["checks"] if 줄["name"] == "상한 구간")
        self.assertEqual(상한줄["status"], qa.FAIL)
        self.assertIn("250건 상한", 상한줄["detail"])
        self.assertIn("4개", 상한줄["detail"])


class 막혔을_때_다시_부르기Test(unittest.TestCase):
    """GDELT 가 429 로 막았을 때 하루치를 통째로 버리지 않는지 본다.

    5초 간격을 지켜도 429 가 돌아오는 날이 있었다. 그때 한 구간이 막히면
    하루 전체가 멈추던 자리에 다시 부르기를 넣었다. 실제로 쉬면 시험이
    느려지므로 쉬는 함수를 대신 넣어 쉰 시간만 받아 적는다.
    """

    def setUp(self):
        self.쉰시간 = []

    def test_두_번_막힌_뒤_통하면_받아_온다(self):
        본문들 = [
            gdelt.GdeltRateLimited("429 다"),
            gdelt.GdeltRateLimited("429 다"),
            '{"articles": [{"url": "https://www.zdnet.co.kr/view/?no=1"}]}',
        ]

        def 가짜읽기(url):
            다음 = 본문들.pop(0)
            if isinstance(다음, Exception):
                raise 다음
            return 다음

        with mock.patch.object(gdelt, "_read_body", 가짜읽기):
            결과 = gdelt.fetch_artlist(
                "AI",
                "20260910150000",
                "20260911145959",
                sleep_seconds=5,
                sleeper=self.쉰시간.append,
            )

        self.assertEqual(len(결과["articles"]), 1)
        # 첫 5초는 5초에 한 번 약속을 지키려고 부르기 전에 쉬는 시간이다.
        self.assertEqual(self.쉰시간[0], 5)
        self.assertEqual(self.쉰시간[1:], [float(초) for 초 in gdelt.RETRY_WAITS[:2]])

    def test_끝까지_막히면_그대로_올려_보낸다(self):
        def 늘_막힘(url):
            raise gdelt.GdeltRateLimited("429 다")

        with mock.patch.object(gdelt, "_read_body", 늘_막힘):
            with self.assertRaises(gdelt.GdeltRateLimited):
                gdelt.fetch_artlist(
                    "AI",
                    "20260910150000",
                    "20260911145959",
                    sleep_seconds=0,
                    sleeper=self.쉰시간.append,
                )

        # 정해 둔 만큼만 다시 부르고 그만둔다. 무한정 매달리지 않는다.
        self.assertEqual(self.쉰시간, [float(초) for 초 in gdelt.RETRY_WAITS])

    def test_다시_부르기를_끄면_한_번만_부른다(self):
        부른횟수 = []

        def 늘_막힘(url):
            부른횟수.append(url)
            raise gdelt.GdeltRateLimited("429 다")

        with mock.patch.object(gdelt, "_read_body", 늘_막힘):
            with self.assertRaises(gdelt.GdeltRateLimited):
                gdelt.fetch_artlist(
                    "AI",
                    "20260910150000",
                    "20260911145959",
                    sleep_seconds=0,
                    retry_waits=(),
                    sleeper=self.쉰시간.append,
                )

        self.assertEqual(len(부른횟수), 1)
        self.assertEqual(self.쉰시간, [])

    def test_수집이_막히면_cli_가_알아듣는_예외로_올라온다(self):
        # GdeltError 가 RuntimeError 를 물려받아야 cli.main 이 잡아서 사람이 읽을
        # 한 줄로 바꿔 준다. Exception 으로 되돌리면 역추적만 찍고 끝난다.
        self.assertTrue(issubclass(gdelt.GdeltError, RuntimeError))
        self.assertTrue(issubclass(gdelt.GdeltRateLimited, gdelt.GdeltError))


class 인증서_검증Test(unittest.TestCase):
    """인증서 검증을 켠 채로 부르는지 본다.

    맥에 python.org 파이썬을 깔면 CA 묶음이 비어 있어 https 호출이 다 막힌다.
    급할 때 검증을 끄는 것으로 막힌 것을 뚫고 싶어지는데, 그러면 받은 제목을
    그대로 브리핑에 올리는 이 파이프라인이 중간에서 바꿔친 응답을 알아볼 수
    없게 된다. 검증이 꺼지는 변경을 막으려고 이 줄을 남긴다.
    """

    def test_검증을_켠_컨텍스트를_쓴다(self):
        컨텍스트 = gdelt.ssl_context()
        self.assertEqual(컨텍스트.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(컨텍스트.check_hostname)

    def test_찾은_CA_묶음은_실제로_있는_파일이다(self):
        자리 = gdelt._ca_bundle_path()
        if 자리 is not None:
            self.assertTrue(Path(자리).is_file())

    def test_파이썬이_들고_있는_묶음이_비면_운영체제_것을_얹는다(self):
        # 맥에 python.org 파이썬을 깔면 실제로 이 상태가 된다. 기본 컨텍스트가
        # CA 를 하나도 들고 있지 않아, 요청이 GDELT 에 닿기도 전에 TLS
        # 손잡기에서 끊겼다. 우분투에서는 드러나지 않는 문제다.
        if netssl._ca_bundle_path() is None:
            self.skipTest("이 기계에 CA 묶음 파일이 없어 확인할 수 없다")

        진짜만들기 = ssl.create_default_context
        만든것 = []

        def 가짜만들기(cafile=None):
            if cafile:
                만든것.append(cafile)
                return 진짜만들기(cafile=cafile)
            return 빈껍데기()

        with mock.patch.object(netssl, "_ssl_context", None):
            with mock.patch.object(netssl.ssl, "create_default_context", 가짜만들기):
                컨텍스트 = netssl.ssl_context()

        self.assertEqual(len(만든것), 1, "찾은 묶음으로 컨텍스트를 다시 만들어야 한다")
        self.assertEqual(컨텍스트.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(컨텍스트.check_hostname)
        self.assertTrue(컨텍스트.get_ca_certs(), "CA 를 얹지 않으면 모든 https 호출이 막힌다")

    def test_묶음을_못_찾아도_검증을_끄지_않는다(self):
        # 급할 때 검증을 끄는 것으로 뚫고 싶어지는 자리다. 그러면 받은 제목을
        # 그대로 브리핑에 올리는 이 파이프라인이 바꿔친 응답을 알아볼 수 없다.
        with mock.patch.object(netssl, "_ssl_context", None):
            with mock.patch.object(netssl, "_ca_bundle_path", return_value=None):
                with mock.patch.object(
                    netssl.ssl, "create_default_context", lambda cafile=None: 빈껍데기()
                ):
                    컨텍스트 = netssl.ssl_context()

        self.assertEqual(컨텍스트.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(컨텍스트.check_hostname)

    def test_SSL_CERT_FILE_에_적어_둔_자리를_먼저_본다(self):
        # 환경 변수로 묶음 자리를 알려 주는 길을 README 에 적어 두었다.
        묶음 = netssl._ca_bundle_path()
        if 묶음 is None:
            self.skipTest("이 기계에 CA 묶음 파일이 없어 확인할 수 없다")
        with mock.patch.dict("os.environ", {"SSL_CERT_FILE": 묶음}):
            self.assertEqual(netssl._ca_bundle_path(), 묶음)


if __name__ == "__main__":
    unittest.main()
