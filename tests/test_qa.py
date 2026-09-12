"""자동 점검 모듈 시험.

정상인 하루를 먼저 통과시켜 두고, 그 하루를 한 군데씩 일부러 깨뜨리면서
점검이 제대로 멈추는지 본다. 멈춤 조건 다섯 가지와 주의 두 가지를 모두 다룬다.
"""

import datetime as dt
import logging
import unittest

from news_brief import qa


def setUpModule():
    """점검이 남기는 경고를 잠시 잠근다. 일부러 깨뜨린 시험이 많아 출력이 어지러워진다."""
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)

# 시험에 쓸 기준 날짜. 한국 날짜다.
날짜 = "2026-09-11"

try:
    from news_brief import store
except Exception as err:  # pragma: no cover - 다른 에이전트가 아직 만드는 중일 때
    store = None
    store_막힌이유 = str(err)
else:
    store_막힌이유 = ""

try:
    from news_brief import judge
except Exception as err:  # pragma: no cover
    judge = None
    judge_막힌이유 = str(err)
else:
    judge_막힌이유 = ""


def 기사(url, seendate, title="오픈AI가 새 모델을 공개했다", domain="zdnet.co.kr"):
    """시험용 기사 한 건. 수집기가 주는 모양을 그대로 흉내 냈다."""
    return {
        "url": url,
        "title": title,
        "title_raw": title,
        "domain": domain,
        "seendate": seendate,
        "language": "Korean",
    }


def 정상수집():
    """아무 데도 깨지지 않은 하루치 수집 결과."""
    return {
        "date": 날짜,
        "articles": [
            기사("https://zdnet.co.kr/view/?no=1", "20260911T013000Z"),
            기사(
                "https://www.etnews.com/2026091100002",
                "20260911T044500Z",
                title="과기정통부가 인공지능 기본법 시행령을 내놨다",
                domain="etnews.com",
            ),
            기사(
                "https://it.chosun.com/news/3",
                "20260910T230000Z",
                title="네이버가 자체 언어모델을 업데이트했다",
                domain="it.chosun.com",
            ),
        ],
        "windows": [("20260910150000", "20260911145959")],
        "truncated_windows": [],
        "raw_path": "/dev/null",
    }


def 상태(결과, 이름):
    """점검 목록에서 이름으로 한 줄을 찾아 상태를 돌려준다."""
    for 줄 in 결과["checks"]:
        if 줄["name"] == 이름:
            return 줄["status"]
    raise AssertionError("%s 라는 점검 항목이 없다" % 이름)


def 내용(결과, 이름):
    """점검 목록에서 이름으로 한 줄을 찾아 내용을 돌려준다."""
    for 줄 in 결과["checks"]:
        if 줄["name"] == 이름:
            return 줄["detail"]
    raise AssertionError("%s 라는 점검 항목이 없다" % 이름)


class 시각변환시험(unittest.TestCase):
    """UTC 를 한국 날짜로 바꾸는 자를 따로 잰다. 하루 경계가 여기서 갈린다."""

    def test_한국시간으로_아홉시간_당긴다(self):
        self.assertEqual(qa.kst_date_from_utc("20260911T013000Z"), "2026-09-11")

    def test_UTC_열다섯시가_다음날_자정이다(self):
        self.assertEqual(qa.kst_date_from_utc("20260910T150000Z"), "2026-09-11")
        self.assertEqual(qa.kst_date_from_utc("20260910T145959Z"), "2026-09-10")

    def test_모양이_달라도_숫자_열넷이면_읽는다(self):
        self.assertEqual(qa.kst_date_from_utc("20260911013000"), "2026-09-11")
        self.assertEqual(qa.kst_date_from_utc("2026-09-11 01:30:00"), "2026-09-11")

    def test_알아볼_수_없으면_None_이다(self):
        self.assertIsNone(qa.kst_date_from_utc(""))
        self.assertIsNone(qa.kst_date_from_utc(None))
        self.assertIsNone(qa.kst_date_from_utc("어제 아침"))
        self.assertIsNone(qa.kst_date_from_utc("20261345T990000Z"))


class 정상시험(unittest.TestCase):
    """깨진 데가 없으면 통과해야 한다."""

    def setUp(self):
        self.결과 = qa.run_checks(None, 날짜, 정상수집(), {})

    def test_종료코드가_0이다(self):
        self.assertEqual(self.결과["exit_code"], 0)

    def test_멈춤이_하나도_없다(self):
        막힌것 = [줄 for 줄 in self.결과["checks"] if 줄["status"] == "fail"]
        self.assertEqual(막힌것, [], "멈춘 항목 %s" % 막힌것)

    def test_멈춤조건_다섯가지가_모두_들어있다(self):
        이름들 = {줄["name"] for 줄 in self.결과["checks"]}
        for 이름 in ("수집 건수", "상한 구간", "날짜 범위", "필수 필드", "링크 중복"):
            self.assertIn(이름, 이름들)

    def test_상태값은_세가지뿐이다(self):
        for 줄 in self.결과["checks"]:
            self.assertIn(줄["status"], ("ok", "warn", "fail"))
            self.assertTrue(줄["detail"].strip(), "내용이 비었다. %s" % 줄["name"])

    def test_수집_건수를_그대로_적는다(self):
        self.assertIn("3건", 내용(self.결과, "수집 건수"))
        self.assertEqual(self.결과["article_count"], 3)


class 멈춤시험(unittest.TestCase):
    """일부러 한 군데씩 깨뜨려 놓고 멈추는지 본다."""

    def test_기사가_0건이면_멈춘다(self):
        수집 = 정상수집()
        수집["articles"] = []
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "수집 건수"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_상한_구간이_남으면_멈춘다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [("20260910150000", "20260911025959")]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "상한 구간"), "fail")
        self.assertEqual(결과["exit_code"], 1)
        self.assertIn("20260910150000", 내용(결과, "상한 구간"))

    def test_상한_구간이_딕셔너리로_와도_읽는다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [
            {"start": "20260910150000", "end": "20260911025959", "count": 250}
        ]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "상한 구간"), "fail")
        self.assertIn("20260911025959", 내용(결과, "상한 구간"))

    def test_한국날짜를_벗어나면_멈춘다(self):
        수집 = 정상수집()
        # UTC 14시 59분 59초는 아직 당일이고, 15시부터는 다음날이다. 딱 넘겨 본다.
        수집["articles"].append(
            기사("https://zdnet.co.kr/view/?no=9", "20260911T150000Z")
        )
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")
        self.assertEqual(결과["exit_code"], 1)
        self.assertIn("2026-09-12", 내용(결과, "날짜 범위"))

    def test_경계_안쪽은_멈추지_않는다(self):
        수집 = 정상수집()
        수집["articles"] = [
            기사("https://zdnet.co.kr/view/?no=10", "20260910T150000Z"),
            기사("https://zdnet.co.kr/view/?no=11", "20260911T145959Z"),
        ]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "날짜 범위"), "ok")

    def test_시각을_알아볼_수_없어도_멈춘다(self):
        수집 = 정상수집()
        수집["articles"][0]["seendate"] = "어제 오후"
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_필수_필드가_비면_멈춘다(self):
        for 자리 in ("url", "title", "domain", "seendate"):
            with self.subTest(자리=자리):
                수집 = 정상수집()
                수집["articles"][1][자리] = ""
                결과 = qa.run_checks(None, 날짜, 수집, {})
                self.assertEqual(상태(결과, "필수 필드"), "fail")
                self.assertEqual(결과["exit_code"], 1)
                self.assertIn(자리, 내용(결과, "필수 필드"))

    def test_필수_필드가_아예_없어도_멈춘다(self):
        수집 = 정상수집()
        del 수집["articles"][0]["domain"]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "필수 필드"), "fail")

    def test_링크가_겹치면_멈춘다(self):
        수집 = 정상수집()
        수집["articles"].append(기사("https://zdnet.co.kr/view/?no=1", "20260911T020000Z"))
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "링크 중복"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_여러_군데가_깨져도_종료코드는_1이다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [("20260910150000", "20260911025959")]
        수집["articles"].append(기사("https://zdnet.co.kr/view/?no=1", "20260913T020000Z"))
        결과 = qa.run_checks(None, 날짜, 수집, {})
        막힌것 = [줄["name"] for 줄 in 결과["checks"] if 줄["status"] == "fail"]
        self.assertGreaterEqual(len(막힌것), 3)
        self.assertEqual(결과["exit_code"], 1)

    def test_수집_기록이_없으면_상한을_확인하지_못한다고_적는다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집()["articles"], {})
        self.assertEqual(상태(결과, "상한 구간"), "warn")
        self.assertEqual(결과["exit_code"], 0)


class 주의시험(unittest.TestCase):
    """멈추지는 않지만 사람이 봐야 하는 경우."""

    def test_걸러낸_비율이_임계값을_넘으면_주의다(self):
        판단 = {
            "kept": [{"url": "https://a", "reason": "AI 소식이 맞다"}],
            "dropped": [
                {"url": "https://b", "reason": "주가 기사라 뺐다"},
                {"url": "https://c", "reason": "AI 와 상관없다"},
                {"url": "https://d", "reason": "연예 기사라 뺐다"},
                {"url": "https://e", "reason": "부고 기사라 뺐다"},
            ],
        }
        결과 = qa.run_checks(None, 날짜, 정상수집(), {}, judged=판단)
        self.assertEqual(상태(결과, "걸러낸 비율"), "warn")
        self.assertEqual(결과["exit_code"], 0)
        self.assertIn("80%", 내용(결과, "걸러낸 비율"))

    def test_걸러낸_비율이_임계값_아래면_통과다(self):
        판단 = {
            "kept": [{"url": "https://a", "reason": "AI 소식이 맞다"}] * 8,
            "dropped": [{"url": "https://b", "reason": "주가 기사라 뺐다"}] * 2,
        }
        결과 = qa.run_checks(None, 날짜, 정상수집(), {}, judged=판단)
        self.assertEqual(상태(결과, "걸러낸 비율"), "ok")

    def test_임계값은_설정으로_바꾼다(self):
        판단 = {
            "kept": [{"url": "https://a", "reason": "AI 소식이 맞다"}] * 8,
            "dropped": [{"url": "https://b", "reason": "주가 기사라 뺐다"}] * 2,
        }
        설정 = {"qa": {"max_drop_ratio": 0.1}}
        결과 = qa.run_checks(None, 날짜, 정상수집(), 설정, judged=판단)
        self.assertEqual(상태(결과, "걸러낸 비율"), "warn")

    def test_판단_기록이_없으면_건너뛴다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "걸러낸 비율"), "ok")
        self.assertIn("건너뛰었다", 내용(결과, "걸러낸 비율"))


@unittest.skipIf(store is None, "store 모듈을 아직 쓸 수 없다. %s" % store_막힌이유)
class 평균대비시험(unittest.TestCase):
    """최근 이레 평균과 견주는 자리. DB 가 있어야 볼 수 있다."""

    def 채운다(self, 하루건수):
        """대상 날짜 앞 이레를 하루 몇 건씩 채운 DB 를 만든다."""
        conn = store.open_db(":memory:")
        기준 = dt.date.fromisoformat(날짜)
        for 뒤로 in range(1, 8):
            그날 = (기준 - dt.timedelta(days=뒤로)).isoformat()
            묶음 = [
                기사(
                    "https://zdnet.co.kr/view/?no=%s-%d" % (그날, i),
                    "%sT013000Z" % 그날.replace("-", ""),
                )
                for i in range(하루건수)
            ]
            store.upsert_articles(conn, 그날, 묶음)
        self.addCleanup(conn.close)
        return conn

    def test_평균과_비슷하면_통과다(self):
        conn = self.채운다(10)
        수집 = 정상수집()
        수집["articles"] = [
            기사("https://zdnet.co.kr/view/?no=t%d" % i, "20260911T013000Z")
            for i in range(9)
        ]
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "최근 이레 대비"), "ok")
        self.assertEqual(결과["exit_code"], 0)

    def test_절반_아래로_줄면_주의다(self):
        conn = self.채운다(20)
        수집 = 정상수집()
        수집["articles"] = [
            기사("https://zdnet.co.kr/view/?no=t%d" % i, "20260911T013000Z")
            for i in range(5)
        ]
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "최근 이레 대비"), "warn")
        self.assertEqual(결과["exit_code"], 0, "주의만으로는 멈추지 않아야 한다")

    def test_두배_위로_늘면_주의다(self):
        conn = self.채운다(10)
        수집 = 정상수집()
        수집["articles"] = [
            기사("https://zdnet.co.kr/view/?no=t%d" % i, "20260911T013000Z")
            for i in range(40)
        ]
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "최근 이레 대비"), "warn")

    def test_쌓인_기록이_적으면_견주지_않는다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        결과 = qa.run_checks(conn, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "최근 이레 대비"), "ok")
        self.assertIn("이르다", 내용(결과, "최근 이레 대비"))

    def test_DB에_덜_들어갔으면_주의다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        수집 = 정상수집()
        store.upsert_articles(conn, 날짜, 수집["articles"][:1])
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "적재 대조"), "warn")
        self.assertEqual(결과["exit_code"], 0)

    def test_다_들어갔으면_통과다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        수집 = 정상수집()
        store.upsert_articles(conn, 날짜, 수집["articles"])
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "적재 대조"), "ok")

    def test_수집_결과가_없으면_DB로_대신_본다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        store.upsert_articles(conn, 날짜, 정상수집()["articles"])
        결과 = qa.run_checks(conn, 날짜, None, {})
        self.assertEqual(상태(결과, "수집 건수"), "ok")
        self.assertEqual(결과["article_count"], 3)


@unittest.skipIf(
    store is None or judge is None,
    "store 나 judge 모듈을 아직 쓸 수 없다. %s %s" % (store_막힌이유, judge_막힌이유),
)
class 판단기록시험(unittest.TestCase):
    """DB 에 남은 판단 기록을 읽어 비율을 재는지 본다."""

    def test_저장된_판단을_읽어_비율을_잰다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        판단 = {
            "kept": [{"url": "https://a", "reason": "AI 소식이 맞다"}],
            "dropped": [
                {"url": "https://b", "reason": "주가 기사라 뺐다"},
                {"url": "https://c", "reason": "연예 기사라 뺐다"},
                {"url": "https://d", "reason": "부고 기사라 뺐다"},
            ],
            "groups": [],
        }
        judge.save_decisions(conn, 날짜, 판단)
        결과 = qa.run_checks(conn, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "걸러낸 비율"), "warn")
        self.assertIn("75%", 내용(결과, "걸러낸 비율"))


class 표시험(unittest.TestCase):
    """점검 결과를 표로 뽑는 자리."""

    def test_표에_항목과_상태가_다_들어간다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        표 = 결과["table"]
        self.assertIn("점검", 표)
        for 줄 in 결과["checks"]:
            self.assertIn(줄["name"], 표)
            self.assertIn(줄["detail"], 표)

    def test_줄_수가_머리와_항목_수에_맞는다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        줄들 = 결과["table"].splitlines()
        self.assertEqual(len(줄들), len(결과["checks"]) + 2)

    def test_한글이_섞여도_열이_어긋나지_않는다(self):
        # 이름 길이가 서로 다르고 한글이 섞여 있어도 내용 열이 같은 자리에서 시작해야 한다.
        점검들 = [
            {"name": "수집 건수", "status": "ok", "detail": "기사 3건을 모았다"},
            {"name": "링크 중복", "status": "fail", "detail": "같은 링크가 1번 더 들어왔다"},
            {"name": "최근 이레 대비", "status": "warn", "detail": "평균의 0.2배다"},
        ]
        줄들 = qa.format_checks(점검들).splitlines()
        시작자리 = [
            qa._width(줄) - qa._width(점검["detail"])
            for 줄, 점검 in zip(줄들[2:], 점검들)
        ]
        self.assertEqual(
            len(set(시작자리)), 1, "내용 열이 줄마다 다른 자리에서 시작한다. %s" % 시작자리
        )
        # 줄 끝에 공백을 남기지 않는다. 마크다운이나 로그에 그대로 붙여도 지저분하지 않게.
        for 줄 in 줄들:
            self.assertEqual(줄, 줄.rstrip())

    def test_표에_줄표나_이모지를_쓰지_않는다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        글 = 결과["table"] + 결과["summary"]
        self.assertNotIn("—", 글)
        self.assertNotIn("–", 글)
        for ch in 글:
            self.assertLess(ord(ch), 0x1F000, "이모지가 섞였다. %r" % ch)

    def test_한줄_요약이_개수를_맞게_센다(self):
        수집 = 정상수집()
        수집["articles"] = []
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertIn("멈춤 1개", 결과["summary"])
        self.assertIn("여기서 멈춘다", 결과["summary"])

    def test_점검이_없으면_그렇게_적는다(self):
        self.assertIn("없다", qa.format_checks([]))


class 입력시험(unittest.TestCase):
    """들어오는 값이 어긋났을 때의 태도."""

    def test_날짜_모양이_어긋나면_바로_알린다(self):
        with self.assertRaises(ValueError):
            qa.run_checks(None, "2026/09/11", 정상수집(), {})

    def test_설정이_없어도_돌아간다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), None)
        self.assertEqual(결과["exit_code"], 0)

    def test_설정값이_숫자가_아니면_기본값으로_돌아간다(self):
        판단 = {
            "kept": [{"url": "https://a", "reason": "맞다"}],
            "dropped": [{"url": "https://b", "reason": "뺐다"}] * 9,
        }
        결과 = qa.run_checks(None, 날짜, 정상수집(), {"qa": {"max_drop_ratio": "많이"}}, judged=판단)
        self.assertEqual(상태(결과, "걸러낸 비율"), "warn")

    def test_돌려주는_모양이_계약대로다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        self.assertIn("checks", 결과)
        self.assertIn("exit_code", 결과)
        self.assertIsInstance(결과["checks"], list)
        self.assertIsInstance(결과["exit_code"], int)
        for 줄 in 결과["checks"]:
            self.assertEqual(set(줄), {"name", "status", "detail"})


def 여러건(개수, 시작번호=100):
    """한국 날짜 안에 드는 기사를 여러 건 만든다. 비율 시험에 쓴다."""
    return [
        기사("https://zdnet.co.kr/view/?no=%d" % (시작번호 + i), "20260911T0%d3000Z" % (i % 10))
        for i in range(개수)
    ]


class 날짜범위비율시험(unittest.TestCase):
    """날짜가 어긋난 기사 한 건 때문에 하루를 통째로 잃지 않는지 본다.

    GDELT 는 seendate 를 15분 단위로 끊어 주기 때문에 하루 경계에 놓인 기사가
    반올림만으로도 옆날로 보인다. 그 한 건은 주의로 남기고, 비율이 임계값을 넘을
    때만 멈춰야 한다.
    """

    def 수집(self, 안쪽개수, 벗어난개수):
        묶음 = 여러건(안쪽개수)
        for i in range(벗어난개수):
            묶음.append(기사("https://zdnet.co.kr/view/?no=9%d" % i, "20260911T150000Z"))
        수집 = 정상수집()
        수집["articles"] = 묶음
        return 수집

    def test_한_건만_벗어나면_주의로_남기고_멈추지_않는다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(19, 1), {})
        self.assertEqual(상태(결과, "날짜 범위"), "warn")
        self.assertEqual(결과["exit_code"], 0)
        self.assertIn("5%", 내용(결과, "날짜 범위"))

    def test_벗어난_비율이_임계값을_넘으면_멈춘다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(9, 3), {})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_임계값을_0으로_두면_한_건도_넘기지_않는다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(19, 1), {"qa": {"max_stray_ratio": 0}})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")

    def test_임계값을_올리면_더_넉넉해진다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(9, 3), {"qa": {"max_stray_ratio": 0.5}})
        self.assertEqual(상태(결과, "날짜 범위"), "warn")
        self.assertEqual(결과["exit_code"], 0)

    def test_시각을_알아볼_수_없으면_비율과_무관하게_멈춘다(self):
        수집 = self.수집(19, 0)
        수집["articles"][0]["seendate"] = "어제 오후"
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")


class 구간밖버린기사시험(unittest.TestCase):
    """수집기가 구간 밖 기사를 버렸을 때 점검이 그 숫자를 본다.

    지금 경로에서는 구간 밖 기사가 DB 에 들어가지 않으므로 기사 목록만 훑으면
    점검이 늘 통과로 나온다. 그러면 하루 경계 계산이 크게 틀어진 날도 조용히
    지나간다. 수집기가 들고 나온 버린 건수를 비율로 보아야 그 신호가 살아난다.
    """

    def 수집(self, 안쪽개수, 버린개수):
        수집 = 정상수집()
        수집["articles"] = 여러건(안쪽개수)
        수집["stray_dropped"] = 버린개수
        수집["stray_samples"] = [
            "https://zdnet.co.kr/view/?no=9%d(20260911T160000Z)" % i
            for i in range(버린개수)
        ]
        return 수집

    def test_버린_것이_임계값_아래면_주의로_남긴다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(19, 1), {})
        self.assertEqual(상태(결과, "날짜 범위"), "warn")
        self.assertEqual(결과["exit_code"], 0)
        자세히 = 내용(결과, "날짜 범위")
        self.assertIn("넣기 전에 버렸다", 자세히)
        self.assertIn("5%", 자세히)

    def test_버린_비율이_임계값을_넘으면_멈춘다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(9, 3), {})
        self.assertEqual(상태(결과, "날짜 범위"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_버린_것이_없으면_그대로_통과다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(19, 0), {})
        self.assertEqual(상태(결과, "날짜 범위"), "ok")

    def test_DB_에서_읽은_하루는_기사_목록으로_본다(self):
        # qa 명령은 수집기를 거치지 않아 들고 나올 숫자가 없다. 예전에 잘못
        # 들어간 기사를 잡는 그물은 그대로 남아 있어야 한다.
        수집 = 정상수집()
        수집.pop("stray_dropped", None)
        수집["articles"] = 여러건(19) + [
            기사("https://zdnet.co.kr/view/?no=99", "20260911T150000Z")
        ]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "날짜 범위"), "warn")
        self.assertIn("수집 단계에서 버려야 할 기사다", 내용(결과, "날짜 범위"))


class 링크중복열쇠시험(unittest.TestCase):
    """중복 점검이 원본 url 이 아니라 자기 열쇠로 견주는지 본다.

    원본 글자를 그대로 견주면 수집 단계가 이미 지운 뒤라 어떤 경우에도 걸리지 않는다.
    """

    def 두건(self, 첫링크, 둘째링크):
        수집 = 정상수집()
        수집["articles"] = [
            기사(첫링크, "20260911T013000Z"),
            기사(둘째링크, "20260911T023000Z"),
        ]
        return qa.run_checks(None, 날짜, 수집, {})

    def test_추적_파라미터만_붙은_링크는_같은_기사로_본다(self):
        결과 = self.두건(
            "https://zdnet.co.kr/view/?no=1",
            "https://zdnet.co.kr/view/?no=1&utm_source=slack",
        )
        self.assertEqual(상태(결과, "링크 중복"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_http_와_https_만_다른_링크도_같은_기사로_본다(self):
        결과 = self.두건(
            "http://zdnet.co.kr/view/?no=1",
            "https://www.zdnet.co.kr/view/?no=1",
        )
        self.assertEqual(상태(결과, "링크 중복"), "fail")

    def test_끝에_빗금만_다른_링크도_같은_기사로_본다(self):
        결과 = self.두건(
            "https://it.chosun.com/news/3",
            "https://it.chosun.com/news/3/",
        )
        self.assertEqual(상태(결과, "링크 중복"), "fail")

    def test_기사_번호가_다르면_다른_기사다(self):
        결과 = self.두건(
            "https://zdnet.co.kr/view/?no=1",
            "https://zdnet.co.kr/view/?no=2",
        )
        self.assertEqual(상태(결과, "링크 중복"), "ok")
        self.assertEqual(결과["exit_code"], 0)

    def test_수집이_합친_건수를_함께_적는다(self):
        수집 = 정상수집()
        수집["dedupe"] = {"duplicated": 3, "merged_distinct_urls": 1, "kept": 3}
        결과 = qa.run_checks(None, 날짜, 수집, {})
        내용글 = 내용(결과, "링크 중복")
        self.assertIn("3건이 합쳐졌다", 내용글)
        self.assertIn("1건", 내용글)


class 적재전점검시험(unittest.TestCase):
    """적재 전 자리에서 부르면 멈춤 조건만 보는지 본다."""

    def test_멈춤_조건_다섯_가지만_본다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {}, stage=qa.STAGE_COLLECTED)
        이름들 = [줄["name"] for 줄 in 결과["checks"]]
        self.assertEqual(
            이름들, ["수집 건수", "상한 구간", "날짜 범위", "필수 필드", "링크 중복"]
        )
        self.assertEqual(결과["stage"], qa.STAGE_COLLECTED)

    def test_적재_전에도_날짜가_어긋나면_멈춘다(self):
        수집 = 정상수집()
        수집["articles"].append(기사("https://zdnet.co.kr/view/?no=9", "20260911T150000Z"))
        결과 = qa.run_checks(None, 날짜, 수집, {}, stage=qa.STAGE_COLLECTED)
        self.assertEqual(상태(결과, "날짜 범위"), "fail")
        self.assertEqual(결과["exit_code"], 1)


class 링크대조시험(unittest.TestCase):
    """링크가 가리키는 곳이 기사 제목과 맞는지 보는 점검.

    상태 코드만 보면 없는 기사 번호에도 200 과 빈 페이지를 돌려주는 매체를 놓친다.
    기본은 꺼 두고, 켤 때만 응답 제목과 낱말이 겹치는지까지 본다.
    """

    켠설정 = {"qa": {"link_check": {"enabled": True, "sample": 2, "min_overlap": 0.2}}}

    def 수집(self):
        수집 = 정상수집()
        수집["articles"] = 수집["articles"][:2]
        return 수집

    def test_기본값은_꺼져_있어_아무것도_부르지_않는다(self):
        부른것 = []

        def 부르개(url):
            부른것.append(url)
            raise AssertionError("꺼 둔 점검이 링크를 불렀다")

        결과 = qa.run_checks(None, 날짜, self.수집(), {}, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "ok")
        self.assertIn("꺼 두어", 내용(결과, "기사 링크 대조"))
        self.assertEqual(부른것, [])

    def test_켜면_제목이_겹치는지_본다(self):
        def 부르개(url):
            return {"status": 200, "title": "오픈AI가 새 모델을 공개했다 | 지디넷코리아"}

        수집 = self.수집()
        수집["articles"] = 수집["articles"][:1]
        결과 = qa.run_checks(None, 날짜, 수집, self.켠설정, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "ok")
        self.assertIn("맞은 것 1건", 내용(결과, "기사 링크 대조"))

    def test_상태는_200_인데_제목이_비면_어긋남으로_센다(self):
        def 부르개(url):
            return {"status": 200, "title": ""}

        결과 = qa.run_checks(None, 날짜, self.수집(), self.켠설정, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "warn")
        self.assertIn("제목이 없다", 내용(결과, "기사 링크 대조"))
        self.assertEqual(결과["exit_code"], 0)

    def test_엉뚱한_기사에_닿으면_어긋남으로_센다(self):
        def 부르개(url):
            return {"status": 200, "title": "코스피 청약 경쟁률 마감"}

        결과 = qa.run_checks(None, 날짜, self.수집(), self.켠설정, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "warn")
        self.assertIn("어긋난 것 2건", 내용(결과, "기사 링크 대조"))

    def test_상태_코드가_200_이_아니면_어긋남이다(self):
        def 부르개(url):
            return {"status": 404, "title": "찾을 수 없습니다"}

        결과 = qa.run_checks(None, 날짜, self.수집(), self.켠설정, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "warn")
        self.assertIn("상태 404", 내용(결과, "기사 링크 대조"))

    def test_받지_못한_링크는_따로_센다(self):
        def 부르개(url):
            raise OSError("연결이 끊겼다")

        결과 = qa.run_checks(None, 날짜, self.수집(), self.켠설정, link_fetcher=부르개)
        self.assertEqual(상태(결과, "기사 링크 대조"), "warn")
        self.assertIn("못 받은 것 2건", 내용(결과, "기사 링크 대조"))

    def test_표본_수만큼만_부른다(self):
        부른것 = []

        def 부르개(url):
            부른것.append(url)
            return {"status": 200, "title": "오픈AI가 새 모델을 공개했다"}

        설정 = {"qa": {"link_check": {"enabled": True, "sample": 1}}}
        qa.run_checks(None, 날짜, 정상수집(), 설정, link_fetcher=부르개)
        self.assertEqual(len(부른것), 1)

    def test_응답_HTML_에서_제목을_꺼낸다(self):
        html글 = (
            "<html><head><title>오픈AI가 새 모델을 공개했다 &lt;단독&gt;</title>"
            "</head><body>본문</body></html>"
        )
        self.assertEqual(qa._page_title(html글), "오픈AI가 새 모델을 공개했다 <단독>")
        self.assertEqual(qa._page_title("<html><body>제목이 없다</body></html>"), "")


@unittest.skipIf(store is None, "store 모듈을 아직 쓸 수 없다. %s" % store_막힌이유)
class 기록이어짐시험(unittest.TestCase):
    """앞선 기록이 이어졌는지 가리는 자리.

    빈 DB 로 시작한 실행에서는 관심 키워드 전부가 처음 보는 말이 되어 버린다.
    그 판단을 점검이 만들어 주어야 브리핑이 그 절을 비울 수 있다.
    """

    def test_앞선_기록이_없으면_주의를_남긴다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        결과 = qa.run_checks(conn, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "기록 이어짐"), "warn")
        self.assertFalse(결과["history"]["ready"])
        self.assertIn("처음 보는 키워드", 내용(결과, "기록 이어짐"))
        self.assertEqual(결과["exit_code"], 0)

    def test_앞선_기록이_있으면_통과다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        어제 = (dt.date.fromisoformat(날짜) - dt.timedelta(days=1)).isoformat()
        store.upsert_articles(
            conn,
            어제,
            [기사("https://zdnet.co.kr/view/?no=어제-1", "%sT013000Z" % 어제.replace("-", ""))],
        )
        결과 = qa.run_checks(conn, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "기록 이어짐"), "ok")
        self.assertTrue(결과["history"]["ready"])
        self.assertEqual(결과["history"]["article_count"], 1)

    def test_DB_가_더_많으면_어느_숫자로_세는지_적는다(self):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        수집 = 정상수집()
        store.upsert_articles(conn, 날짜, 수집["articles"])
        store.upsert_articles(
            conn, 날짜, [기사("https://zdnet.co.kr/view/?no=더많음", "20260911T033000Z")]
        )
        결과 = qa.run_checks(conn, 날짜, 수집, {})
        self.assertEqual(상태(결과, "적재 대조"), "warn")
        self.assertIn("DB 쪽 4건", 내용(결과, "적재 대조"))
        self.assertEqual(결과["stored_count"], 4)
        self.assertEqual(결과["exit_code"], 0)


class 막힌구간시험(unittest.TestCase):
    """GDELT 가 막아 못 받은 구간과 상한에 걸린 구간을 갈라 적는지 본다.

    둘 다 기사를 놓친 것이라 멈춤은 같지만 할 일이 다르다. 한 줄에 뭉쳐 적으면
    막혀서 못 받은 날을 상한 문제로 잘못 알아듣는다.
    """

    def test_막힌_구간도_멈춤이다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [
            {
                "start": "20260910150000",
                "end": "20260911025959",
                "count": 0,
                "blocked": True,
                "reason": "GDELT 가 막아 이 구간을 받지 못했다",
            }
        ]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "상한 구간"), "fail")
        self.assertEqual(결과["exit_code"], 1)

    def test_막힌_것을_상한에_걸린_것으로_적지_않는다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [
            {"start": "20260910150000", "end": "20260911025959", "blocked": True}
        ]
        결과 = qa.run_checks(None, 날짜, 수집, {})
        적은것 = 내용(결과, "상한 구간")
        self.assertIn("GDELT 가 막아", 적은것)
        self.assertNotIn("250건 상한에 걸린", 적은것)
        self.assertIn("다시 돌리면", 적은것)
        self.assertEqual(결과["exit_code"], 1)

    def test_두_가지가_섞여_오면_둘_다_적는다(self):
        수집 = 정상수집()
        수집["truncated_windows"] = [
            {"start": "20260910150000", "end": "20260911025959", "count": 250},
            {"start": "20260911030000", "end": "20260911065959", "blocked": True},
        ]
        적은것 = 내용(qa.run_checks(None, 날짜, 수집, {}), "상한 구간")
        self.assertIn("250건 상한에 걸린 구간이 1개", 적은것)
        self.assertIn("막아 받지 못한 구간이 1개", 적은것)

    def test_아무_구간도_남지_않으면_통과다(self):
        결과 = qa.run_checks(None, 날짜, 정상수집(), {})
        self.assertEqual(상태(결과, "상한 구간"), "ok")


class 링크없는항목시험(unittest.TestCase):
    """링크가 없어 수집기가 버린 항목의 건수를 표에 적는지 본다.

    그 항목은 기사 목록에 들어오지 않는다. 적어 두지 않으면 GDELT 응답 건수와
    우리가 센 건수가 갈린 까닭을 나중에 되짚을 수 없다.
    """

    def test_버린_건수를_수집_건수_줄에_적는다(self):
        수집 = 정상수집()
        수집["dropped_no_url"] = 2
        self.assertIn("버린 항목이 2건", 내용(qa.run_checks(None, 날짜, 수집, {}), "수집 건수"))

    def test_버린_것이_없으면_아무_말도_붙이지_않는다(self):
        적은것 = 내용(qa.run_checks(None, 날짜, 정상수집(), {}), "수집 건수")
        self.assertNotIn("버린 항목", 적은것)

    def test_기사가_0건이어도_버린_건수를_적는다(self):
        수집 = 정상수집()
        수집["articles"] = []
        수집["dropped_no_url"] = 5
        결과 = qa.run_checks(None, 날짜, 수집, {})
        self.assertEqual(상태(결과, "수집 건수"), "fail")
        self.assertIn("버린 항목이 5건", 내용(결과, "수집 건수"))


@unittest.skipIf(
    store is None or judge is None,
    "store 나 judge 모듈을 아직 쓸 수 없다. %s %s" % (store_막힌이유, judge_막힌이유),
)
class 판단대조시험(unittest.TestCase):
    """판단이 다룬 건수와 DB 에 쌓인 건수가 갈리면 멈추는지 본다.

    판단은 DB 에 쌓인 하루치로 돌고 브리핑의 기사 수도 DB 를 센다. 두 숫자가
    어긋난 채 브리핑이 나가면 본문과 건수가 서로 다른 하루를 말하게 된다.
    """

    def 채운다(self, 건수):
        conn = store.open_db(":memory:")
        self.addCleanup(conn.close)
        store.upsert_articles(
            conn,
            날짜,
            [
                기사("https://zdnet.co.kr/view/?no=%d" % i, "20260911T013000Z")
                for i in range(건수)
            ],
        )
        return conn

    def 판단(self, 남긴수, 버린수):
        return {
            "kept": [
                {"url": "https://zdnet.co.kr/view/?no=%d" % i, "reason": "AI 소식이 맞다"}
                for i in range(남긴수)
            ],
            "dropped": [
                {"url": "https://drop/%d" % i, "reason": "주가 기사라 뺐다"}
                for i in range(버린수)
            ],
            "groups": [],
        }

    def 수집(self, 건수):
        수집 = 정상수집()
        수집["articles"] = [
            기사("https://zdnet.co.kr/view/?no=%d" % i, "20260911T013000Z")
            for i in range(건수)
        ]
        return 수집

    def test_건수가_맞으면_통과다(self):
        conn = self.채운다(5)
        결과 = qa.run_checks(conn, 날짜, self.수집(5), {}, judged=self.판단(3, 2))
        self.assertEqual(상태(결과, "판단 대조"), "ok")
        self.assertEqual(결과["exit_code"], 0)

    def test_기사가_늘었는데_옛_판단을_쓰면_멈춘다(self):
        conn = self.채운다(5)
        결과 = qa.run_checks(conn, 날짜, self.수집(5), {}, judged=self.판단(2, 1))
        self.assertEqual(상태(결과, "판단 대조"), "fail")
        self.assertEqual(결과["exit_code"], 1)
        self.assertIn("판단은 3건", 내용(결과, "판단 대조"))
        self.assertIn("DB 에는 5건", 내용(결과, "판단 대조"))
        self.assertIn("--force", 내용(결과, "판단 대조"))

    def test_DB_에_남은_판단_기록으로도_견준다(self):
        conn = self.채운다(5)
        judge.save_decisions(conn, 날짜, self.판단(2, 1))
        결과 = qa.run_checks(conn, 날짜, self.수집(5), {})
        self.assertEqual(상태(결과, "판단 대조"), "fail")

    def test_판단을_아직_돌리지_않았으면_건너뛴다(self):
        conn = self.채운다(5)
        결과 = qa.run_checks(conn, 날짜, self.수집(5), {})
        self.assertEqual(상태(결과, "판단 대조"), "ok")
        self.assertIn("건너뛰었다", 내용(결과, "판단 대조"))
        self.assertEqual(결과["exit_code"], 0)

    def test_적재_전_자리에서는_보지_않는다(self):
        이름들 = [
            줄["name"]
            for 줄 in qa.run_checks(
                None, 날짜, self.수집(5), {}, judged=self.판단(2, 1),
                stage=qa.STAGE_COLLECTED,
            )["checks"]
        ]
        self.assertNotIn("판단 대조", 이름들)

    def test_DB_를_읽지_않았으면_견주지_않는다(self):
        결과 = qa.run_checks(None, 날짜, self.수집(5), {}, judged=self.판단(2, 1))
        self.assertEqual(상태(결과, "판단 대조"), "ok")
        self.assertEqual(결과["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
