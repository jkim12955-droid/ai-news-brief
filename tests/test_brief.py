"""브리핑 만들기 시험.

묶음이 있는 날, 조용한 날, 최근 7일 평균이 0 인 날, 처음 보는 키워드가 있는 날을 각각 본다.
글쓰기 규칙(줄표 금지, 이모지 금지, 빈 섹션 제목 금지)도 같이 확인한다.
brief 모듈은 다른 프로젝트 모듈을 가져오지 않으므로 이 시험은 혼자 돌아간다.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_brief import brief


def group(headline, urls, kind="other"):
    return {"headline": headline, "urls": urls, "kind": kind}


def busy_judged():
    """매체 수가 서로 다른 묶음 네 개. 정책 묶음이 하나 섞여 있다."""
    return {
        "kept": [],
        "dropped": [],
        "groups": [
            group(
                "구글이 제미나이 새 버전을 내놨다.",
                [
                    "https://www.etnews.com/20260911a",
                    "https://zdnet.co.kr/20260911b",
                ],
                "tool",
            ),
            group(
                "오픈AI가 새 추론 모델을 공개했다.",
                [
                    "https://www.hani.co.kr/arti/1.html",
                    "https://m.hani.co.kr/arti/1.html",
                    "https://news.mt.co.kr/2.html",
                    "https://biz.chosun.com/3.html",
                    "https://www.yna.co.kr/4.html",
                ],
                "tool",
            ),
            group(
                "과기정통부가 AI 기본법 시행령을 손봤다.",
                [
                    "https://www.yna.co.kr/policy1.html",
                    "https://www.khan.co.kr/policy2.html",
                    "https://www.seoul.co.kr/policy3.html",
                ],
                "policy",
            ),
            group(
                "국내 반도체 업체가 AI 서버용 메모리 공급을 늘린다.",
                ["https://www.mk.co.kr/mem1.html", "https://www.hankyung.com/mem2.html"],
                "other",
            ),
        ],
    }


def busy_stats(**extra):
    stats = {"article_count": 42, "avg_7d": 30.5, "first_seen_keywords": []}
    stats.update(extra)
    return stats


CFG = {"query": '("artificial intelligence" OR OpenAI) sourcelang:korean'}


def bullet_lines(markdown, heading):
    """지정한 소제목 아래에 붙은 묶음 줄만 뽑아 온다."""
    lines = markdown.splitlines()
    try:
        start = lines.index("### " + heading)
    except ValueError:
        return []
    picked = []
    for line in lines[start + 1:]:
        if line.startswith("### "):
            break
        if line.startswith("- "):
            picked.append(line)
    return picked


class BuildBriefTest(unittest.TestCase):
    def test_묶음이_있는_날(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        markdown = built["markdown"]

        # 제일 크게 다뤄진 묶음이 한 줄 요약이 된다. 매체 다섯 곳 가운데 hani 는 m 과 www 를 하나로 센다.
        self.assertEqual(built["summary_line"], "오픈AI가 새 추론 모델을 공개했다.")
        self.assertIn("어제 이 소식을 다룬 매체가 4곳으로 가장 많았다.", markdown)
        self.assertIn("링크: https://www.hani.co.kr/arti/1.html", markdown)

        # 날짜와 요일이 맨 위에 온다.
        self.assertTrue(markdown.startswith("## 2026-09-11 금요일 AI 뉴스 브리핑"))

        # 기사 수와 평균 대비.
        self.assertIn("어제 모인 기사는 42건이다. 최근 7일 평균 30.5건보다 11.5건 많다.", markdown)

        # 대표 묶음을 뺀 나머지가 목록으로 붙고, 정책 묶음은 거기서 빠진다.
        rest = bullet_lines(markdown, "그 밖의 묶음")
        self.assertEqual(len(rest), 2)
        self.assertIn("- 구글이 제미나이 새 버전을 내놨다.", rest)
        self.assertNotIn("과기정통부", "\n".join(rest))

        # 정책 묶음은 따로 모인다.
        policy = bullet_lines(markdown, "규제와 정책")
        self.assertEqual(policy, ["- 과기정통부가 AI 기본법 시행령을 손봤다."])
        self.assertIn("매체 3곳 · https://www.yna.co.kr/policy1.html", markdown)

        # 새 도구 묶음에는 표시가 붙는다.
        self.assertIn("매체 2곳 · 새로 나온 도구 · https://www.etnews.com/20260911a", markdown)

        # 출처 한 줄이 맨 아래에 온다.
        self.assertIn("GDELT DOC 2.0", markdown.strip().splitlines()[-1])
        self.assertIn('("artificial intelligence" OR OpenAI) sourcelang:korean', markdown)

        # 처음 보는 키워드가 없으니 그 제목은 나오지 않는다.
        self.assertNotIn("처음 보는 키워드", markdown)

    def test_대표_묶음은_아래_목록에_다시_나오지_않는다(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        markdown = built["markdown"]
        self.assertEqual(markdown.count("오픈AI가 새 추론 모델을 공개했다."), 1)

    def test_묶음은_다섯_개까지만_올린다(self):
        judged = {
            "groups": [
                group("소식 {0} 번이다.".format(i), ["https://site{0}.co.kr/a".format(i)] * (9 - i))
                for i in range(9)
            ]
        }
        markdown = brief.build_brief("2026-09-11", judged, busy_stats(), CFG)["markdown"]
        # 맨 위 대표 묶음 하나에 목록 네 개를 더해 다섯이다.
        self.assertEqual(len(bullet_lines(markdown, "그 밖의 묶음")), 4)

    def test_조용한_날(self):
        judged = {"kept": [], "dropped": [], "groups": []}
        built = brief.build_brief("2026-09-12", judged, busy_stats(article_count=4), CFG)
        markdown = built["markdown"]

        self.assertTrue(built["summary_line"].startswith("어제는 조용했다"))
        self.assertIn("어제는 조용했다", markdown)
        # 빈 섹션 제목이 남으면 안 된다.
        for heading in ("### 그 밖의 묶음", "### 묶어 본 소식", "### 규제와 정책", "### 처음 보는 키워드"):
            self.assertNotIn(heading, markdown)
        # 짧게 끝내되 건수와 출처는 남긴다.
        self.assertIn("어제 모인 기사는 4건이다.", markdown)
        self.assertIn("GDELT DOC 2.0", markdown)
        self.assertLess(len(markdown.splitlines()), 12)

    def test_기사가_한_건도_없는_날(self):
        built = brief.build_brief("2026-09-12", {"groups": []}, {"article_count": 0, "avg_7d": 30.5}, CFG)
        markdown = built["markdown"]
        self.assertTrue(built["summary_line"].startswith("어제는 조용했다"))
        self.assertIn("기사가 한 건도 모이지 않았다", markdown)
        self.assertIn("수집이 제대로 돌았는지", markdown)

    def test_평균이_0_이면_비교_문장을_뺀다(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(avg_7d=0), CFG)
        markdown = built["markdown"]
        self.assertIn("어제 모인 기사는 42건이다.", markdown)
        self.assertNotIn("평균", markdown)

    def test_평균과_비슷한_날(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(article_count=31, avg_7d=30.5), CFG)
        self.assertIn("최근 7일 평균 30.5건과 비슷하다.", built["markdown"])

    def test_평균보다_적은_날(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(article_count=12, avg_7d=30.5), CFG)
        self.assertIn("최근 7일 평균 30.5건보다 18.5건 적다.", built["markdown"])

    def test_처음_보는_키워드가_있는_날(self):
        stats = busy_stats(first_seen_keywords=["소버린 AI", {"keyword": "온디바이스"}, "소버린 AI"])
        built = brief.build_brief("2026-09-11", busy_judged(), stats, CFG)
        markdown = built["markdown"]
        self.assertIn("### 처음 보는 키워드", markdown)
        self.assertIn("소버린 AI · 온디바이스", markdown)
        self.assertIn("최근 7일 기사에는 없다가 어제 처음 올라온 말이다.", markdown)
        # 같은 말이 두 번 들어가지 않는다.
        self.assertEqual(markdown.count("소버린 AI"), 1)
        # 슬랙 블록에도 같은 내용이 올라간다.
        slack_text = json.dumps(built["slack_blocks"], ensure_ascii=False)
        self.assertIn("처음 보는 키워드", slack_text)

    def test_정책_묶음만_있는_날(self):
        judged = {
            "groups": [
                group("국회가 AI 기본법을 통과시켰다.", ["https://a.co.kr/1", "https://b.co.kr/2"], "policy"),
                group("개인정보위가 학습 데이터 지침을 냈다.", ["https://c.co.kr/3"], "policy"),
            ]
        }
        markdown = brief.build_brief("2026-09-11", judged, busy_stats(), CFG)["markdown"]
        # 가장 큰 정책 소식이 한 줄 요약으로 올라가고, 나머지만 규제 항목에 남는다.
        self.assertIn("국회가 AI 기본법을 통과시켰다.", markdown)
        self.assertEqual(bullet_lines(markdown, "규제와 정책"), ["- 개인정보위가 학습 데이터 지침을 냈다."])
        self.assertNotIn("### 그 밖의 묶음", markdown)
        self.assertEqual(markdown.count("국회가 AI 기본법을 통과시켰다."), 1)

    def test_따로_써_준_요약이_있으면_그걸_쓴다(self):
        stats = busy_stats(summary_line="어제는 모델 공개와 규제 소식이 함께 몰렸다.")
        built = brief.build_brief("2026-09-11", busy_judged(), stats, CFG)
        self.assertEqual(built["summary_line"], "어제는 모델 공개와 규제 소식이 함께 몰렸다.")
        # 이때는 대표 묶음도 목록에 그대로 남는다.
        rest = bullet_lines(built["markdown"], "묶어 본 소식")
        self.assertIn("- 오픈AI가 새 추론 모델을 공개했다.", rest)

    def test_집계_이름이_달라도_알아본다(self):
        stats = {"count": 42, "recent_avg": 30.5, "new_keywords": ["온디바이스"]}
        markdown = brief.build_brief("2026-09-11", busy_judged(), stats, CFG)["markdown"]
        self.assertIn("어제 모인 기사는 42건이다.", markdown)
        self.assertIn("온디바이스", markdown)

    def test_수집이_준_매체_이름을_먼저_쓴다(self):
        judged = {
            "groups": [
                group("한 매체가 두 번 썼다.", ["https://one.example.com/a", "https://two.example.com/b"])
            ],
            "articles": [
                {"url": "https://one.example.com/a", "domain": "sisa.co.kr"},
                {"url": "https://two.example.com/b", "domain": "sisa.co.kr"},
            ],
        }
        markdown = brief.build_brief("2026-09-11", judged, busy_stats(), CFG)["markdown"]
        self.assertIn("다룬 매체는 한 곳뿐이지만", markdown)


class GroupLimitTest(unittest.TestCase):
    """settings.json 의 brief.min_groups 와 brief.max_groups 가 실제로 먹는지 본다."""

    def many(self, general=6, policy=7):
        """일반 묶음과 정책 묶음을 넉넉히 만든다. 매체 수를 달리해 순서를 굳힌다."""
        묶음들 = [
            group(
                "일반 소식 {0}이다.".format(i),
                ["https://gen{0}-{1}.co.kr/a".format(i, j) for j in range(general - i)],
            )
            for i in range(general)
        ]
        묶음들 += [
            group(
                "정책 소식 {0}이다.".format(i),
                ["https://pol{0}-{1}.co.kr/a".format(i, j) for j in range(policy - i)],
                "policy",
            )
            for i in range(policy)
        ]
        return {"groups": 묶음들}

    def shown(self, markdown):
        return (
            len(bullet_lines(markdown, "그 밖의 묶음"))
            + len(bullet_lines(markdown, "묶어 본 소식"))
            + len(bullet_lines(markdown, "규제와 정책"))
        )

    def test_정책_묶음까지_합쳐_상한을_넘지_않는다(self):
        # 일반을 먼저 끊고 정책을 따로 더 붙이면 화면에 오르는 묶음이 열 개까지 늘어난다.
        cfg = dict(CFG, brief={"min_groups": 3, "max_groups": 5})
        built = brief.build_brief("2026-09-11", self.many(), busy_stats(), cfg)
        self.assertEqual(built["group_shown"], 5)
        # 맨 위 대표 묶음 하나를 뺀 나머지가 목록에 올라간다.
        self.assertEqual(self.shown(built["markdown"]), 4)

    def test_설정의_max_groups_를_읽는다(self):
        cfg = dict(CFG, brief={"min_groups": 2, "max_groups": 3})
        built = brief.build_brief("2026-09-11", self.many(), busy_stats(), cfg)
        self.assertEqual(built["group_shown"], 3)
        self.assertEqual(self.shown(built["markdown"]), 2)

    def test_잘라낸_묶음_개수를_적는다(self):
        cfg = dict(CFG, brief={"min_groups": 3, "max_groups": 5})
        markdown = brief.build_brief("2026-09-11", self.many(), busy_stats(), cfg)["markdown"]
        self.assertIn(
            "어제 묶인 소식은 13개인데 브리핑에는 5개까지만 올리기로 해서 8개는 실지 않았다.",
            markdown,
        )

    def test_묶음이_min_groups_보다_적으면_그_사실을_적는다(self):
        cfg = dict(CFG, brief={"min_groups": 3, "max_groups": 5})
        judged = {"groups": [group("혼자 남은 소식이다.", ["https://a.co.kr/1"])]}
        built = brief.build_brief("2026-09-11", judged, busy_stats(), cfg)
        self.assertEqual(built["group_shown"], 1)
        self.assertIn("올린 묶음이 1개다.", built["markdown"])
        self.assertIn("3개는 되어야 한다고", built["markdown"])

    def test_묶음이_넉넉하면_아무_말도_붙이지_않는다(self):
        cfg = dict(CFG, brief={"min_groups": 3, "max_groups": 5})
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), cfg)
        self.assertEqual(built["group_total"], built["group_shown"])
        self.assertNotIn("실지 않았다", built["markdown"])
        self.assertNotIn("올린 묶음이", built["markdown"])

    def test_설정이_없으면_기본값으로_돈다(self):
        built = brief.build_brief("2026-09-11", self.many(), busy_stats(), CFG)
        self.assertEqual(built["group_shown"], brief.MAX_GROUPS)

    def test_어긋난_설정에서는_위_한계를_믿는다(self):
        cfg = dict(CFG, brief={"min_groups": 9, "max_groups": 2})
        built = brief.build_brief("2026-09-11", self.many(), busy_stats(), cfg)
        self.assertEqual(built["group_shown"], 2)
        self.assertNotIn("올린 묶음이", built["markdown"])


class KeywordLimitTest(unittest.TestCase):
    """처음 보는 키워드를 끊었다는 사실을 남기는지 본다."""

    열두개 = [
        "앤스로픽", "구글", "엔비디아", "네이버", "카카오", "삼성",
        "클로드", "제미나이", "인공지능", "반도체", "데이터센터", "온디바이스",
    ]

    def test_끊은_개수를_키워드_줄에_적는다(self):
        stats = busy_stats(first_seen_keywords=list(self.열두개))
        built = brief.build_brief("2026-09-11", busy_judged(), stats, CFG)
        markdown = built["markdown"]
        self.assertIn("클로드 · 제미나이 외 4개", markdown)
        self.assertIn("처음 보는 말은 모두 12개인데 여기에는 8개만 적었다.", markdown)
        # 슬랙에도 같은 꼬리가 붙는다.
        self.assertIn("외 4개", json.dumps(built["slack_blocks"], ensure_ascii=False))

    def test_끊을_것이_없으면_꼬리를_붙이지_않는다(self):
        stats = busy_stats(first_seen_keywords=["온디바이스", "소버린 AI"])
        markdown = brief.build_brief("2026-09-11", busy_judged(), stats, CFG)["markdown"]
        self.assertIn("온디바이스 · 소버린 AI", markdown)
        self.assertNotIn("외 ", markdown)
        self.assertNotIn("만 적었다", markdown)

    def test_설정으로_키워드_개수를_바꾼다(self):
        cfg = dict(CFG, brief={"max_keywords": 3})
        stats = busy_stats(first_seen_keywords=list(self.열두개))
        markdown = brief.build_brief("2026-09-11", busy_judged(), stats, cfg)["markdown"]
        self.assertIn("앤스로픽 · 구글 · 엔비디아 외 9개", markdown)
        self.assertIn("여기에는 3개만 적었다.", markdown)


class WritingRuleTest(unittest.TestCase):
    def test_줄표와_이모지가_없다(self):
        judged = busy_judged()
        judged["groups"][0]["headline"] = "구글이 제미나이를 내놨다 — 성능을 올렸다 🚀"
        built = brief.build_brief("2026-09-11", judged, busy_stats(first_seen_keywords=["온디바이스"]), CFG)
        blob = built["markdown"] + json.dumps(built["slack_blocks"], ensure_ascii=False)
        for dash in ("—", "–", "―", "‒"):
            self.assertNotIn(dash, blob)
        self.assertNotIn("🚀", blob)
        # 줄표 자리는 쉼표로 바뀐다.
        self.assertIn("구글이 제미나이를 내놨다, 성능을 올렸다.", built["markdown"])

    def test_대괄호_제목을_쓰지_않는다(self):
        markdown = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)["markdown"]
        self.assertNotIn("[", markdown)

    def test_같은_입력이면_같은_글이_나온다(self):
        first = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        second = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        self.assertEqual(first["markdown"], second["markdown"])
        self.assertEqual(first["slack_blocks"], second["slack_blocks"])

    def test_빈_줄이_세_줄_넘게_이어지지_않는다(self):
        markdown = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)["markdown"]
        self.assertNotIn("\n\n\n", markdown)


class SlackBlockTest(unittest.TestCase):
    def test_블록_모양(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(first_seen_keywords=["온디바이스"]), CFG)
        blocks = built["slack_blocks"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["text"]["type"], "plain_text")
        self.assertFalse(blocks[0]["text"]["emoji"])
        for block in blocks:
            self.assertIn(block["type"], ("header", "section", "context", "divider"))
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)
        # 대표 링크는 슬랙 문법으로 들어간다.
        blob = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("<https://www.hani.co.kr/arti/1.html|hani.co.kr>", blob)
        self.assertLess(len(blocks), 50)

    def test_조용한_날_블록은_짧다(self):
        built = brief.build_brief("2026-09-12", {"groups": []}, {"article_count": 3, "avg_7d": 20}, CFG)
        types = [block["type"] for block in built["slack_blocks"]]
        self.assertEqual(types, ["header", "section", "context", "context"])

    def test_보낼_모양으로_바꾼다(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        payload = brief.brief_payload(built)
        self.assertEqual(payload["text"], built["summary_line"])
        self.assertEqual(payload["blocks"], built["slack_blocks"])


class SaveBriefTest(unittest.TestCase):
    def test_파일로_남긴다(self):
        built = brief.build_brief("2026-09-11", busy_judged(), busy_stats(), CFG)
        with tempfile.TemporaryDirectory() as tmp:
            path = brief.default_brief_path(tmp, "2026-09-11")
            self.assertTrue(path.endswith(os.path.join("data", "briefs", "2026-09-11.md")))
            saved = brief.save_brief(built["markdown"], path)
            self.assertTrue(os.path.exists(saved))
            with open(saved, encoding="utf-8") as handle:
                text = handle.read()
            self.assertEqual(text, built["markdown"])
            self.assertTrue(text.endswith("\n"))

    def test_같은_자리에_다시_써도_된다(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "brief", "2026-09-11.md")
            brief.save_brief("첫 번째 글이다.\n", path)
            brief.save_brief("두 번째 글이다.\n", path)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "두 번째 글이다.\n")


class 기록없어비운절시험(unittest.TestCase):
    """처음 보는 키워드를 비운 까닭이 브리핑 본문까지 이어지는지 본다.

    집계 쪽(cli._stats_for)은 앞선 기간 기사가 DB 에 없으면 처음 보는 키워드를
    넘기지 않고 first_seen_skipped 에 그 사실을 담는다. 그 신호가 브리핑까지
    닿지 않으면 절만 조용히 사라져서, 읽는 사람은 어제 새 말이 하나도 없었다고 읽는다.
    """

    def 만들기(self, stats):
        return brief.build_brief("2026-09-11", busy_judged(), stats, {})

    def test_비운_까닭을_본문에_적는다(self):
        built = self.만들기({
            "article_count": 12,
            "first_seen_keywords": [],
            "first_seen_skipped": True,
            "first_seen_skipped_reason": "앞선 7일 기사 기록이 DB 에 없어 견줄 수가 없다",
            "first_seen_lookback_days": 7,
        })
        self.assertIn("앞선 7일 기사 기록이 DB 에 없어 견줄 수가 없다.", built["markdown"])
        self.assertNotIn("### 처음 보는 키워드", built["markdown"])

    def test_까닭이_없어도_비웠다는_사실은_적는다(self):
        built = self.만들기({
            "article_count": 12,
            "first_seen_keywords": [],
            "first_seen_skipped": True,
            "first_seen_lookback_days": 7,
        })
        self.assertIn("앞선 7일 기사 기록이 없어 처음 보는 키워드는 비워 두었다.", built["markdown"])

    def test_새_말이_없어_빈_날에는_아무_말도_붙이지_않는다(self):
        built = self.만들기({"article_count": 12, "first_seen_keywords": []})
        self.assertNotIn("비워 두었다", built["markdown"])
        self.assertNotIn("### 처음 보는 키워드", built["markdown"])

    def test_조용한_날에도_비운_까닭을_적는다(self):
        built = brief.build_brief(
            "2026-09-11",
            {"groups": []},
            {
                "article_count": 0,
                "first_seen_skipped": True,
                "first_seen_skipped_reason": "앞선 7일 기사 기록이 DB 에 없어 견줄 수가 없다",
            },
            {},
        )
        self.assertIn("견줄 수가 없다.", built["markdown"])

    def test_슬랙_블록에도_같은_줄이_들어간다(self):
        built = self.만들기({
            "article_count": 12,
            "first_seen_keywords": [],
            "first_seen_skipped": True,
            "first_seen_skipped_reason": "앞선 7일 기사 기록이 DB 에 없어 견줄 수가 없다",
        })
        글자 = json.dumps(built["slack_blocks"], ensure_ascii=False)
        self.assertIn("견줄 수가 없다.", 글자)


class 글다듬기한잣대시험(unittest.TestCase):
    """판단과 브리핑과 리포트가 글을 같은 잣대로 다듬는지 본다.

    같은 일을 세 모듈이 각자 하고 있어서 한쪽만 고치면 조용히 갈라진다.
    갈라지면 브리핑은 깨끗한데 리포트에는 줄표가 남는 식으로 새어 나간다.
    """

    def 세잣대(self, 글):
        from news_brief import judge, report

        return brief._text(글), report._clean_text(글), judge._글다듬기(글)

    def test_세_모듈이_같은_값을_낸다(self):
        보기 = [
            "오픈AI — 새 모델 🚀 공개, 무료 배포",
            "GPT‑5 공개",
            "AI ℹ 소식",
            "삼성‐LG 협력",
            "AI™ 브랜드",
        ]
        for 글 in 보기:
            a, b, c = self.세잣대(글)
            self.assertEqual(a, b, 글)
            self.assertEqual(a, c, 글)

    def test_붙임표는_쉼표가_아니라_아스키_붙임표로_내린다(self):
        for 값 in self.세잣대("GPT‑5 공개"):
            self.assertEqual(값, "GPT-5 공개")
        for 값 in self.세잣대("삼성‐LG 협력"):
            self.assertEqual(값, "삼성-LG 협력")

    def test_범위_밖_기호도_떼어_낸다(self):
        for 값 in self.세잣대("AI ℹ 소식"):
            self.assertEqual(값, "AI 소식")
        for 값 in self.세잣대("AI™ 브랜드"):
            self.assertEqual(값, "AI 브랜드")

    def test_뜻을_지고_오는_기호는_건드리지_않는다(self):
        # 화살표와 ▶ 는 한국어 제목에서 뜻을 지고 온다. 이모지로 보고 지우면
        # 'A→B 전환' 이 'AB 전환' 이 되어 읽는 사람이 뜻을 잃는다.
        for 값 in self.세잣대("A→B 전환"):
            self.assertEqual(값, "A→B 전환")
        for 값 in self.세잣대("▶ 행사 안내"):
            self.assertEqual(값, "▶ 행사 안내")


class BadInputTest(unittest.TestCase):
    def test_빈_입력에도_깨지지_않는다(self):
        built = brief.build_brief("2026-09-11", {}, {}, {})
        self.assertIn("어제는 조용했다", built["summary_line"])
        self.assertIn("GDELT DOC 2.0", built["markdown"])

    def test_모양이_어긋난_묶음은_건너뛴다(self):
        judged = {"groups": [None, {"kind": "tool"}, {"headline": "링크 없는 소식이다.", "urls": []}]}
        built = brief.build_brief("2026-09-11", judged, {"article_count": 2}, {})
        self.assertEqual(built["summary_line"], "링크 없는 소식이다.")
        self.assertNotIn("링크: ", built["markdown"])

    def test_날짜_모양이_틀려도_돈다(self):
        built = brief.build_brief("어제", {"groups": []}, {}, {})
        self.assertTrue(built["markdown"].startswith("## 어제 AI 뉴스 브리핑"))


if __name__ == "__main__":
    unittest.main()


class 보이는링크다듬기Test(unittest.TestCase):
    """GDELT 가 붙여 주는 기본 포트를 브리핑 링크에서 떼는지 본다."""

    def test_기본_포트를_뗀_링크가_실린다(self):
        from news_brief.collect import tidy_url

        self.assertEqual(
            tidy_url("https://www.ddaily.co.kr:443/page/view/2026090819144801128"),
            "https://www.ddaily.co.kr/page/view/2026090819144801128",
        )
        self.assertEqual(
            tidy_url("http://www.koreatimes.com:80/article/20260911/1629413"),
            "http://www.koreatimes.com/article/20260911/1629413",
        )

    def test_기본_포트가_아니면_그대로_둔다(self):
        from news_brief.collect import tidy_url

        self.assertEqual(tidy_url("https://example.com:8443/a"), "https://example.com:8443/a")
        self.assertEqual(tidy_url("https://example.com/a?b=1#c"), "https://example.com/a?b=1#c")
        self.assertEqual(tidy_url(""), "")
