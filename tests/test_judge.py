"""judge 모듈을 확인하는 테스트다.

네트워크를 타지 않는다. 앤스로픽 호출은 함수를 갈아 끼워 흉내만 낸다.
DB 도 메모리에만 만들어 쓴다. 다른 모듈은 가져오지 않는다.
"""

import json
import logging
import os
import sqlite3
import ssl
import unittest
from unittest import mock

from news_brief import judge


def setUpModule():
    """내려앉을 때 남기는 경고가 테스트 화면을 어지럽히지 않게 눌러 둔다."""
    judge.로그.setLevel(logging.CRITICAL)


def 설정(**덮어쓰기):
    """테스트에서 쓸 설정을 만든다. 넘기지 않은 값은 기본값 그대로 둔다."""
    바탕 = {"keywords": {}, "judge": {"similarity_threshold": 0.4}}
    for 열쇠, 값 in 덮어쓰기.items():
        if 열쇠 in ("interest", "exclude", "regulation", "tool", "stopwords"):
            바탕["keywords"][열쇠] = 값
        else:
            바탕["judge"][열쇠] = 값
    return 바탕


def 기사(url, title, domain="example.com"):
    """collect 가 넘겨 주는 모양대로 기사 한 건을 만든다."""
    return {
        "url": url,
        "title": title,
        "title_raw": title,
        "domain": domain,
        "seendate": "20260911T230000Z",
        "language": "Korean",
    }


# 같은 사건을 다룬 기사 셋과 규제 기사 하나, 그리고 걸러야 할 기사 둘이다.
표본기사 = [
    기사("https://a.example.com/1", "오픈AI가 새 추론 모델 GPT-5를 공개했다", "a.example.com"),
    기사("https://b.example.com/2", "오픈AI, 새 추론 모델 GPT-5 공개", "b.example.com"),
    기사("https://c.example.com/3", "오픈AI 새 추론 모델 공개, 업계 반응은", "c.example.com"),
    기사("https://d.example.com/4", "개인정보위, 생성형 AI 가이드라인 발표", "d.example.com"),
    기사("https://e.example.com/5", "AI 관련주 급등, 코스피 강세", "e.example.com"),
    기사("https://f.example.com/6", "서울 아파트 전세값 하락세", "f.example.com"),
]


class 규칙거르기테스트(unittest.TestCase):
    """규칙 백엔드가 무엇을 남기고 무엇을 빼는지 본다."""

    def test_ai_소식만_남긴다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        남은링크 = [항목["url"] for 항목 in 결과["kept"]]
        self.assertEqual(남은링크, [
            "https://a.example.com/1",
            "https://b.example.com/2",
            "https://c.example.com/3",
            "https://d.example.com/4",
        ])
        뺀링크 = {항목["url"] for 항목 in 결과["dropped"]}
        self.assertEqual(뺀링크, {"https://e.example.com/5", "https://f.example.com/6"})

    def test_뺀_이유를_한국어로_남긴다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        이유들 = {항목["url"]: 항목["reason"] for 항목 in 결과["dropped"]}
        self.assertIn("코스피", 이유들["https://e.example.com/5"])
        self.assertIn("AI 소식이 아니라고 봤다", 이유들["https://e.example.com/5"])
        self.assertIn("보이지 않아", 이유들["https://f.example.com/6"])
        for 항목 in 결과["kept"] + 결과["dropped"]:
            self.assertTrue(항목["reason"].strip(), "이유가 비어 있으면 안 된다")
            self.assertNotIn("—", 항목["reason"])

    def test_남긴_이유에_걸린_키워드를_적는다(self):
        결과 = judge.judge_articles([기사("https://x/1", "OpenAI 새 모델 공개")], 설정())
        self.assertIn("openai", 결과["kept"][0]["reason"].lower())

    def test_두루뭉술한_키워드보다_또렷한_쪽을_적는다(self):
        # ai 도 걸리고 오픈ai 도 걸리면 긴 쪽을 이유에 적는다
        결과 = judge.judge_articles([기사("https://x/1", "오픈AI 새 모델 공개")], 설정())
        self.assertIn("오픈ai", 결과["kept"][0]["reason"].lower())

    def test_제목이_없거나_링크가_없으면_뺀다(self):
        기사들 = [
            {"url": "https://x/1", "title": "", "domain": "x"},
            {"url": "", "title": "오픈AI 새 모델 공개", "domain": "x"},
        ]
        결과 = judge.judge_articles(기사들, 설정())
        self.assertEqual(결과["kept"], [])
        이유들 = [항목["reason"] for 항목 in 결과["dropped"]]
        self.assertTrue(any("제목이 비어" in 이유 for 이유 in 이유들))
        self.assertTrue(any("링크가 없어" in 이유 for 이유 in 이유들))

    def test_같은_링크는_한_번만_남긴다(self):
        같은것 = 기사("https://x/1", "오픈AI 새 모델 공개")
        결과 = judge.judge_articles([같은것, dict(같은것)], 설정())
        self.assertEqual(len(결과["kept"]), 1)
        self.assertEqual(len(결과["dropped"]), 1)
        self.assertIn("한 번만", 결과["dropped"][0]["reason"])

    def test_기사가_없으면_빈_결과를_돌려준다(self):
        결과 = judge.judge_articles([], 설정())
        self.assertEqual(결과["kept"], [])
        self.assertEqual(결과["dropped"], [])
        self.assertEqual(결과["groups"], [])

    def test_설정이_비어_있어도_돈다(self):
        for 빈설정 in ({}, None):
            결과 = judge.judge_articles(표본기사, 빈설정)
            self.assertEqual(len(결과["kept"]), 4)
            self.assertEqual(len(결과["groups"]), 2)

    def test_설정의_관심어가_기본값을_밀어낸다(self):
        기사들 = [
            기사("https://x/1", "청소 로봇 신제품 출시"),
            기사("https://x/2", "오픈AI 새 모델 공개"),
        ]
        결과 = judge.judge_articles(기사들, 설정(interest=["로봇"]))
        self.assertEqual([항목["url"] for 항목 in 결과["kept"]], ["https://x/1"])

    def test_설정의_제외어가_기본값을_밀어낸다(self):
        기사들 = [기사("https://x/1", "오픈AI 새 모델 공개 행사 포토")]
        self.assertEqual(len(judge.judge_articles(기사들, 설정())["kept"]), 0)
        살아남음 = judge.judge_articles(기사들, 설정(exclude=["부고"]))
        self.assertEqual(len(살아남음["kept"]), 1)

    def test_한_글자_한글_키워드는_낱말로만_잡는다(self):
        # 법 이라는 키워드가 방법이나 법인 안에서 잡히면 안 된다
        self.assertFalse(judge._키워드있나("ai 활용 방법 정리", "법"))
        self.assertTrue(judge._키워드있나("ai 기본 법 국회 통과", "법"))

    def test_짧은_영문_키워드는_낱말로만_잡는다(self):
        # ai 가 email 이나 thailand 안에서 잡히면 안 된다
        self.assertFalse(judge._키워드있나("email 마케팅 요령", "ai"))
        self.assertTrue(judge._키워드있나("ai 반도체 수출", "ai"))
        self.assertTrue(judge._키워드있나("정부가 ai를 도입한다", "ai"))


class 목록설정테스트(unittest.TestCase):
    """설정의 낱말 목록을 어디서 읽고, 빈 목록을 어떻게 보는지 확인한다."""

    수혜주기사 = [기사("https://x/1", "인공지능 수혜주 급등")]

    def test_목록_자리가_없으면_기본값을_쓴다(self):
        # keywords.json 이 아예 없는 환경이다. 기본 제외어가 살아 있어야 한다.
        결과 = judge.judge_articles(self.수혜주기사, {})
        self.assertEqual(len(결과["kept"]), 0)
        self.assertIn("수혜주", 결과["dropped"][0]["reason"])

    def test_빈_목록은_빈_목록으로_받아들인다(self):
        # 목록을 비우는 것이 그 검사를 끄는 가장 단순한 방법이다. 비웠는데
        # 코드에 박힌 기본값으로 되돌아가면, 파일에 없는 낱말을 이유로 댄다.
        설정값 = {"keywords": {"exclude": []}}
        self.assertEqual(judge._규칙설정(설정값)["exclude"], [])

        결과 = judge.judge_articles(self.수혜주기사, 설정값)
        self.assertEqual([항목["url"] for 항목 in 결과["kept"]], ["https://x/1"])

    def test_관심어를_비우면_아무것도_남지_않는다(self):
        설정값 = {"keywords": {"interest": []}}
        결과 = judge.judge_articles(표본기사, 설정값)
        self.assertEqual(결과["kept"], [])
        self.assertEqual(len(결과["dropped"]), len(표본기사))

    def test_불용어를_비워도_돈다(self):
        설정값 = {"keywords": {"stopwords": []}}
        self.assertEqual(judge._규칙설정(설정값)["stopwords"], [])
        self.assertTrue(judge.judge_articles(표본기사, 설정값)["groups"])

    def test_어느_목록을_어디서_읽었는지_로그에_남긴다(self):
        with self.assertLogs(judge.로그, level="INFO") as 기록:
            judge._규칙설정({"keywords": {"exclude": []}})
        찍힌것 = "\n".join(기록.output)
        self.assertIn("제외어 목록은 설정의 keywords.exclude 에서 읽었다", 찍힌것)
        self.assertIn("관심어 목록이 설정에 없어", 찍힌것)


class 글다듬기테스트(unittest.TestCase):
    """판단이 만든 한 줄 문장에 줄표와 이모지가 남지 않는지 본다.

    브리핑만 사후에 정리하면 리포트와 슬랙, 노션에는 그대로 새어 나간다.
    """

    지저분한기사 = [기사("https://x/1", "오픈AI — 새 모델 🚀 공개, 무료 배포")]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_줄표는_쉼표로_이모지는_떼어_낸다(self):
        self.assertEqual(
            judge._글다듬기("오픈AI — 새 모델 🚀 공개, 무료 배포"),
            "오픈AI, 새 모델 공개, 무료 배포",
        )

    def test_한_줄_문장에_줄표와_이모지가_없다(self):
        묶음 = judge.judge_articles(self.지저분한기사, 설정())["groups"][0]
        for 줄표 in ("—", "–", "―", "‒"):
            self.assertNotIn(줄표, 묶음["headline"])
        self.assertNotIn("🚀", 묶음["headline"])
        self.assertIn("오픈AI, 새 모델 공개, 무료 배포", 묶음["headline"])

    def test_DB_에_남는_문장도_깨끗하다(self):
        judge.save_decisions(self.conn, "2026-09-11", {
            "kept": [{"url": "https://x/1", "reason": "AI 소식이다"}],
            "dropped": [],
            "groups": [{
                "headline": "오픈AI — 새 모델 🚀 공개",
                "urls": ["https://x/1"],
                "kind": "tool",
            }],
            "backend": "rules",
        })
        읽은것 = judge.load_decisions(self.conn, "2026-09-11")
        self.assertEqual(읽은것["groups"][0]["headline"], "오픈AI, 새 모델 공개")

    def test_모델이_준_문장의_줄표도_걷어_낸다(self):
        속내용 = {
            "판단": [{"번호": 1, "채택": True, "이유": "AI 소식이다"}],
            "묶음": [{"제목": "오픈AI — 새 모델 🚀 공개", "번호들": [1], "종류": "tool"}],
        }

        def 흉내(payload, api_key, timeout=60):
            return {"content": [{"type": "text", "text": json.dumps(속내용, ensure_ascii=False)}]}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", side_effect=흉내):
                결과 = judge.judge_articles(self.지저분한기사, 설정(), backend="llm")
        self.assertEqual(결과["backend"], "llm")
        self.assertEqual(결과["groups"][0]["headline"], "오픈AI, 새 모델 공개")


class 묶기테스트(unittest.TestCase):
    """제목이 닮은 기사끼리 묶이는지 본다."""

    def test_같은_사건은_한_묶음이_된다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        self.assertEqual(len(결과["groups"]), 2)
        첫묶음 = 결과["groups"][0]
        self.assertEqual(sorted(첫묶음["urls"]), [
            "https://a.example.com/1",
            "https://b.example.com/2",
            "https://c.example.com/3",
        ])
        self.assertEqual(결과["groups"][1]["urls"], ["https://d.example.com/4"])

    def test_한_기사는_한_묶음에만_들어간다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        모든링크 = [링크 for 묶음 in 결과["groups"] for 링크 in 묶음["urls"]]
        self.assertEqual(len(모든링크), len(set(모든링크)))
        self.assertEqual(set(모든링크), {항목["url"] for 항목 in 결과["kept"]})

    def test_임계값을_올리면_덜_묶인다(self):
        느슨할때 = judge.judge_articles(표본기사, 설정(similarity_threshold=0.4))
        빡빡할때 = judge.judge_articles(표본기사, 설정(similarity_threshold=0.9))
        self.assertEqual(len(느슨할때["groups"]), 2)
        self.assertEqual(len(빡빡할때["groups"]), 3)

    def test_묶음이_많이_다룬_순서로_온다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        매체수들 = [len({링크.split("/")[2] for 링크 in 묶음["urls"]}) for 묶음 in 결과["groups"]]
        self.assertEqual(매체수들, sorted(매체수들, reverse=True))

    def test_한줄문장이_사람이_쓴_것처럼_온다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        for 묶음 in 결과["groups"]:
            한줄 = 묶음["headline"]
            self.assertTrue(한줄.endswith("다"), 한줄)
            self.assertNotIn("\n", 한줄)
            self.assertNotIn("—", 한줄)
            self.assertNotIn("[", 한줄)
            self.assertLess(len(한줄), 120)
        self.assertIn("3개 매체", 결과["groups"][0]["headline"])

    def test_끊긴_제목은_따옴표로_묶어_말이_어그러지지_않게_한다(self):
        서술형 = judge._한줄문장("오픈AI가 새 모델을 공개했다", 3)
        self.assertEqual(서술형, "오픈AI가 새 모델을 공개했다고 3개 매체가 전했다")
        끊긴것 = judge._한줄문장("네이버, 온디바이스 AI 탑재 신제품 선보여", 1)
        self.assertEqual(끊긴것, "'네이버, 온디바이스 AI 탑재 신제품 선보여' 소식이 한 곳에서 나왔다")

    def test_제목의_머리표와_꼬리매체를_걷어낸다(self):
        다듬은것 = judge._제목다듬기("[단독] 오픈AI 새 모델 공개 | 아무개신문")
        self.assertEqual(다듬은것, "오픈AI 새 모델 공개")


class 종류가리기테스트(unittest.TestCase):
    """규제 소식과 도구 소식을 가려내는지 본다."""

    def test_규제_소식은_policy_로_본다(self):
        기사들 = [기사("https://x/1", "개인정보위, 생성형 AI 가이드라인 발표")]
        결과 = judge.judge_articles(기사들, 설정())
        self.assertEqual(결과["groups"][0]["kind"], "policy")

    def test_출시_소식은_tool_로_본다(self):
        기사들 = [기사("https://x/1", "앤스로픽, 새 코딩 도구 출시")]
        결과 = judge.judge_articles(기사들, 설정())
        self.assertEqual(결과["groups"][0]["kind"], "tool")

    def test_어느_쪽도_아니면_other_로_둔다(self):
        기사들 = [기사("https://x/1", "인공지능 학회에 몰린 연구자들 이야기")]
        결과 = judge.judge_articles(기사들, 설정())
        self.assertEqual(결과["groups"][0]["kind"], "other")

    def test_규제어와_출시어가_같이_있으면_policy_가_이긴다(self):
        기사들 = [기사("https://x/1", "과기정통부, AI 규제 지침 공개")]
        결과 = judge.judge_articles(기사들, 설정())
        self.assertEqual(결과["groups"][0]["kind"], "policy")

    def test_종류는_세_가지_가운데_하나다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        for 묶음 in 결과["groups"]:
            self.assertIn(묶음["kind"], ("tool", "policy", "other"))


class LLM강등테스트(unittest.TestCase):
    """열쇠가 없거나 응답이 어긋날 때 규칙으로 내려앉는지 본다."""

    def test_열쇠가_없으면_규칙으로_내려앉는다(self):
        빈환경 = {열쇠: 값 for 열쇠, 값 in os.environ.items() if 열쇠 != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, 빈환경, clear=True):
            with mock.patch.object(judge, "_call_anthropic") as 부른것:
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        부른것.assert_not_called()
        self.assertEqual(결과["backend"], "rules")
        self.assertEqual(결과["requested_backend"], "llm")
        self.assertIn("ANTHROPIC_API_KEY", 결과["fallback_reason"])

    def test_내려앉아도_결과_구조는_같다(self):
        빈환경 = {열쇠: 값 for 열쇠, 값 in os.environ.items() if 열쇠 != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, 빈환경, clear=True):
            내려앉은것 = judge.judge_articles(표본기사, 설정(), backend="llm")
        규칙것 = judge.judge_articles(표본기사, 설정())
        for 열쇠 in ("kept", "dropped", "groups"):
            self.assertEqual(내려앉은것[열쇠], 규칙것[열쇠])

    def test_응답_형식이_어긋나면_규칙으로_내려앉는다(self):
        어긋난응답 = {"content": [{"type": "text", "text": "미안하지만 표로 정리해 줄게"}]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", return_value=어긋난응답) as 부른것:
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        self.assertEqual(부른것.call_count, 1)
        self.assertEqual(결과["backend"], "rules")
        self.assertIn("형식", 결과["fallback_reason"])

    def test_호출이_실패해도_규칙으로_내려앉는다(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", side_effect=OSError("연결이 끊겼다")):
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        self.assertEqual(결과["backend"], "rules")
        self.assertIn("실패", 결과["fallback_reason"])
        self.assertEqual(len(결과["kept"]), 4)

    def test_모르는_백엔드도_규칙으로_돈다(self):
        결과 = judge.judge_articles(표본기사, 설정(), backend="점쟁이")
        self.assertEqual(결과["backend"], "rules")
        self.assertIn("백엔드는 없어", 결과["fallback_reason"])

    def test_모델_이름은_설정에서_가져온다(self):
        받아본것 = {}

        def 흉내(payload, api_key, timeout=60):
            받아본것.update(payload)
            return {"content": [{"type": "text", "text": "{}"}]}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", side_effect=흉내):
                judge.judge_articles(표본기사, 설정(model="claude-opus-5"), backend="llm")
        self.assertEqual(받아본것["model"], "claude-opus-5")

    def test_기본_모델_이름은_설정이_없을_때_쓴다(self):
        받아본것 = {}

        def 흉내(payload, api_key, timeout=60):
            받아본것.update(payload)
            return {"content": [{"type": "text", "text": "{}"}]}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", side_effect=흉내):
                judge.judge_articles(표본기사, {}, backend="llm")
        self.assertEqual(받아본것["model"], judge.DEFAULT_MODEL)

    def test_기본_모델_이름이_설정_기본값과_같다(self):
        # 두 곳이 어긋나면 설정을 거쳐 올 때와 judge 를 직접 부를 때 다른 모델로
        # 나간다. config 의 기본값 하나만 보고 맞춘다.
        from news_brief import config

        self.assertEqual(judge.DEFAULT_MODEL, config.DEFAULT_SETTINGS["judge"]["model"])


class LLM응답읽기테스트(unittest.TestCase):
    """모델이 제대로 답했을 때 같은 구조로 옮기는지 본다. 네트워크는 타지 않는다."""

    def 모델답(self):
        return {
            "판단": [
                {"번호": 1, "채택": True, "이유": "오픈AI 모델 소식이라 남겼다"},
                {"번호": 2, "채택": True, "이유": "같은 사건을 다룬 기사다"},
                {"번호": 3, "채택": True, "이유": "같은 사건의 반응 기사다"},
                {"번호": 4, "채택": True, "이유": "규제 소식이라 남겼다"},
                {"번호": 5, "채택": False, "이유": "주식 시세 이야기라 뺐다"},
                {"번호": 6, "채택": False, "이유": "부동산 기사라 뺐다"},
            ],
            "묶음": [
                {"제목": "오픈AI가 새 추론 모델을 내놨다고 세 곳이 전했다",
                 "번호들": [1, 2, 3], "종류": "tool"},
                {"제목": "개인정보위가 생성형 AI 지침을 내놨다",
                 "번호들": [4], "종류": "policy"},
            ],
        }

    def 흉내응답(self, 속내용):
        return {"content": [{"type": "text", "text": json.dumps(속내용, ensure_ascii=False)}]}

    def test_모델이_준_판단과_묶음을_그대로_옮긴다(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic",
                                   return_value=self.흉내응답(self.모델답())):
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        self.assertEqual(결과["backend"], "llm")
        self.assertIsNone(결과["fallback_reason"])
        self.assertEqual(len(결과["kept"]), 4)
        self.assertEqual(결과["dropped"][0]["reason"], "주식 시세 이야기라 뺐다")
        self.assertEqual(결과["groups"][0]["kind"], "tool")
        self.assertEqual(결과["groups"][1]["kind"], "policy")
        self.assertEqual(결과["groups"][0]["urls"], [
            "https://a.example.com/1",
            "https://b.example.com/2",
            "https://c.example.com/3",
        ])

    def test_코드울타리가_붙어_있어도_읽는다(self):
        본문 = "```json\n" + json.dumps(self.모델답(), ensure_ascii=False) + "\n```"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic",
                                   return_value={"content": [{"type": "text", "text": 본문}]}):
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        self.assertEqual(결과["backend"], "llm")
        self.assertEqual(len(결과["groups"]), 2)

    def test_모델이_흘린_기사도_묶어_붙인다(self):
        답 = self.모델답()
        답["묶음"] = [{"제목": "오픈AI 소식이다", "번호들": [1], "종류": "tool"}]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", return_value=self.흉내응답(답)):
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        묶인링크 = [링크 for 묶음 in 결과["groups"] for 링크 in 묶음["urls"]]
        self.assertEqual(sorted(묶인링크), sorted(항목["url"] for 항목 in 결과["kept"]))

    def test_모르는_종류는_규칙으로_다시_가린다(self):
        답 = self.모델답()
        답["묶음"][1]["종류"] = "규제같은것"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "테스트열쇠"}):
            with mock.patch.object(judge, "_call_anthropic", return_value=self.흉내응답(답)):
                결과 = judge.judge_articles(표본기사, 설정(), backend="llm")
        self.assertEqual(결과["groups"][1]["kind"], "policy")


class 저장테스트(unittest.TestCase):
    """판단을 DB 에 남기고 다시 읽는 자리를 본다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_남긴_판단을_그대로_읽어_온다(self):
        결과 = judge.judge_articles(표본기사, 설정())
        센것 = judge.save_decisions(self.conn, "2026-09-11", 결과)
        self.assertEqual(센것["kept"], 4)
        self.assertEqual(센것["dropped"], 2)
        읽은것 = judge.load_decisions(self.conn, "2026-09-11")
        self.assertEqual(읽은것, 결과)

    def test_판단이_없는_날짜는_None_이다(self):
        self.assertIsNone(judge.load_decisions(self.conn, "2026-09-10"))

    def test_같은_날짜를_다시_남기면_갈아_끼운다(self):
        judge.save_decisions(self.conn, "2026-09-11", judge.judge_articles(표본기사, 설정()))
        작은것 = judge.judge_articles([기사("https://x/1", "오픈AI 새 모델 공개")], 설정())
        judge.save_decisions(self.conn, "2026-09-11", 작은것)
        self.assertEqual(judge.load_decisions(self.conn, "2026-09-11"), 작은것)

    def test_내려앉은_까닭도_같이_남는다(self):
        결과 = judge.judge_articles(표본기사, 설정(), backend="점쟁이")
        judge.save_decisions(self.conn, "2026-09-11", 결과)
        읽은것 = judge.load_decisions(self.conn, "2026-09-11")
        self.assertEqual(읽은것["fallback_reason"], 결과["fallback_reason"])
        self.assertEqual(읽은것["requested_backend"], "점쟁이")

    def test_날짜끼리_섞이지_않는다(self):
        judge.save_decisions(self.conn, "2026-09-10", judge.judge_articles(표본기사, 설정()))
        작은것 = judge.judge_articles([기사("https://x/1", "오픈AI 새 모델 공개")], 설정())
        judge.save_decisions(self.conn, "2026-09-11", 작은것)
        self.assertEqual(len(judge.load_decisions(self.conn, "2026-09-10")["kept"]), 4)
        self.assertEqual(len(judge.load_decisions(self.conn, "2026-09-11")["kept"]), 1)


class 재현성테스트(unittest.TestCase):
    """같은 날을 다시 돌려도 숫자가 흔들리지 않는지 본다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_같은_입력은_같은_결과를_낸다(self):
        첫번째 = judge.judge_articles(표본기사, 설정())
        두번째 = judge.judge_articles(표본기사, 설정())
        self.assertEqual(첫번째, 두번째)

    def test_같은_기사로_다시_돌리면_저장된_판단을_쓴다(self):
        첫번째 = judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정())
        self.assertFalse(첫번째["reused"])

        두번째 = judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정())
        self.assertTrue(두번째["reused"])
        self.assertEqual(두번째["kept"], 첫번째["kept"])
        self.assertEqual(두번째["groups"], 첫번째["groups"])

    def test_기사가_늘어난_날은_다시_판단한다(self):
        # 같은 날짜를 다시 수집해 기사가 늘어난 경우다. 저장된 판단을 그대로
        # 쓰면 새로 들어온 기사가 브리핑에 한 건도 오르지 않는다.
        judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정())

        늘어난기사 = list(표본기사) + [기사("https://z/9", "앤스로픽 클로드 새 모델 공개")]
        다시 = judge.judge_day(self.conn, "2026-09-11", 늘어난기사, 설정())

        self.assertFalse(다시["reused"])
        self.assertIn("건으로 달라졌다", 다시["rejudged_reason"])
        self.assertIn("https://z/9", [항목["url"] for 항목 in 다시["kept"]])
        # 새로 판단한 것이 DB 에도 남아야 다음 실행이 같은 값을 읽는다.
        self.assertEqual(
            judge.load_decisions(self.conn, "2026-09-11")["kept"], 다시["kept"]
        )

    def test_건수가_같아도_링크가_바뀌면_다시_판단한다(self):
        judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정())

        바뀐기사 = list(표본기사[:-1]) + [기사("https://z/9", "오픈AI 새 모델 공개")]
        다시 = judge.judge_day(self.conn, "2026-09-11", 바뀐기사, 설정())

        self.assertFalse(다시["reused"])
        self.assertIn("링크 묶음", 다시["rejudged_reason"])

    def test_다른_백엔드를_달라고_하면_다시_판단한다(self):
        # rules 로 판단해 둔 날에 --backend llm 을 주면, 저장된 rules 판단을
        # 조용히 되쓰는 것이 아니라 다시 판단해야 한다.
        judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정(), backend="rules")
        # 열쇠를 비워 두어 망을 타지 않는다. llm 을 달라고 해도 규칙으로 내려앉는다.
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            다시 = judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정(), backend="llm")

        self.assertFalse(다시["reused"])
        self.assertIn("llm", 다시["rejudged_reason"])
        self.assertEqual(다시["requested_backend"], "llm")
        # 열쇠가 없어 규칙으로 내려앉았다는 사실도 남는다.
        self.assertEqual(다시["backend"], "rules")

    def test_같은_백엔드로_다시_돌리면_되쓴다(self):
        # llm 을 달라고 했지만 열쇠가 없어 규칙으로 내려앉은 날이다. 다음 실행이
        # 또 llm 을 달라고 하면 요청이 같으니 저장된 판단을 그대로 쓴다.
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정(), backend="llm")
            두번째 = judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정(), backend="llm")
        self.assertTrue(두번째["reused"])

    def test_force_를_주면_다시_판단한다(self):
        judge.judge_day(self.conn, "2026-09-11", 표본기사, 설정())
        다른기사 = [기사("https://z/9", "전혀 다른 인공지능 기사 하나")]
        다시 = judge.judge_day(self.conn, "2026-09-11", 다른기사, 설정(), force=True)
        self.assertFalse(다시["reused"])
        self.assertEqual([항목["url"] for 항목 in 다시["kept"]], ["https://z/9"])
        self.assertEqual(len(judge.load_decisions(self.conn, "2026-09-11")["kept"]), 1)


class 인증서테스트(unittest.TestCase):
    """모델을 부를 때 인증서 검증을 켠 채 나가는지 본다.

    맥에 python.org 파이썬을 깔면 CA 묶음이 비어 있어서, 컨텍스트를 넘기지
    않은 호출은 요청이 앤스로픽에 닿기도 전에 TLS 손잡기에서 끊긴다.
    급할 때 검증을 끄는 쪽으로 뚫고 싶어지는데, 그러면 모델이 준 판단을
    중간에서 바꿔치기해도 알아볼 수 없다. 컨텍스트를 넘기는지와 그 컨텍스트가
    검증을 켠 것인지 둘 다 본다.
    """

    class 가짜응답:
        def __init__(self, 본문):
            self.본문 = 본문

        def __enter__(self):
            return self

        def __exit__(self, *예외):
            return False

        def read(self):
            return self.본문.encode("utf-8")

    def test_검증_컨텍스트를_넘겨_부른다(self):
        받은것 = {}
        본문 = json.dumps({"content": [{"type": "text", "text": "{}"}]})

        def 가짜열기(요청, timeout=None, context=None):
            받은것["timeout"] = timeout
            받은것["context"] = context
            return self.가짜응답(본문)

        with mock.patch("urllib.request.urlopen", 가짜열기):
            judge._call_anthropic({"model": "claude-opus-5"}, "열쇠", timeout=7)

        컨텍스트 = 받은것["context"]
        self.assertIsInstance(컨텍스트, ssl.SSLContext)
        self.assertEqual(컨텍스트.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(컨텍스트.check_hostname)
        self.assertEqual(받은것["timeout"], 7)


if __name__ == "__main__":
    unittest.main()
