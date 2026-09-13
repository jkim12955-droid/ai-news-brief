"""전송 모듈 시험이다.

네트워크는 한 번도 타지 않는다. 실제 전송 자리에는 가짜 전송기를 끼워 넣어
어떤 주소로 무엇을 보내려 했는지만 들여다본다.
"""

import json
import logging
import os
import ssl
import unittest
from unittest import mock

from news_brief import notify


def setUpModule():
    """실패 상황을 일부러 만드는 시험이 많아서, 도는 동안은 경고 로그를 잠시 끈다."""
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# 시험에만 쓰는 가짜 비밀값이다. 진짜 웹훅이나 토큰이 아니다.
가짜_웹훅 = "https://hooks.slack.example/services/T0000/B0000/abcdefghijklmnop"
가짜_토큰 = "ntn_시험용토큰1234567890"
가짜_부모 = "11112222333344445555666677778888"


class 가짜전송기:
    """실제 전송 대신 부름을 기록하고 미리 정한 응답을 돌려준다."""

    def __init__(self, 응답들=None):
        self.calls = []
        self.응답들 = list(응답들 or [(200, "ok")])

    def __call__(self, url, payload, headers=None, method="POST"):
        self.calls.append({"url": url, "payload": payload, "headers": headers or {}, "method": method})
        if len(self.응답들) > 1:
            return self.응답들.pop(0)
        return self.응답들[0]


class 터지는전송기:
    """연결 자체가 무너지는 상황을 흉내 낸다."""

    def __init__(self, message):
        self.message = message
        self.calls = []

    def __call__(self, url, payload, headers=None, method="POST"):
        self.calls.append(url)
        raise OSError(self.message)


def 환경비우기():
    """전송 관련 환경변수를 모두 지운 채로 시험한다."""
    return mock.patch.dict(os.environ, {}, clear=True)


def 문자열전부(결과):
    """결과 딕셔너리를 통째로 문자열로 펴서 비밀값이 섞였는지 훑어본다."""
    return json.dumps(결과, ensure_ascii=False)


class 마스킹시험(unittest.TestCase):

    def test_비어_있으면_없음이라고_적는다(self):
        self.assertEqual(notify.mask(None), "(없음)")
        self.assertEqual(notify.mask(""), "(없음)")
        self.assertEqual(notify.mask("   "), "(없음)")

    def test_짧은_값은_통째로_가린다(self):
        self.assertEqual(notify.mask("abc123"), "****")

    def test_긴_값은_앞_두_글자만_남는다(self):
        덮은값 = notify.mask(가짜_웹훅)
        self.assertEqual(덮은값, "ht****")
        self.assertNotIn("abcdefghijklmnop", 덮은값)
        self.assertNotIn("hooks.slack.example", 덮은값)

    def test_길이가_드러나지_않는다(self):
        짧은쪽 = notify.mask("a" * 20)
        긴쪽 = notify.mask("a" * 200)
        self.assertEqual(짧은쪽, 긴쪽)

    def test_문장_속_비밀값도_덮는다(self):
        문장 = "응답이 왔다. 주소는 %s 였다." % 가짜_웹훅
        덮은문장 = notify._scrub(문장, 가짜_웹훅)
        self.assertNotIn(가짜_웹훅, 덮은문장)
        self.assertIn("ht****", 덮은문장)


class 슬랙시험(unittest.TestCase):

    def test_기본값은_미리보기라_보내지_않는다(self):
        전송기 = 가짜전송기()
        with 환경비우기():
            결과 = notify.send_slack({"markdown": "어제 기사는 모두 120건이었다."}, poster=전송기)
        self.assertEqual(결과["status"], "dry_run")
        self.assertTrue(결과["dry_run"])
        self.assertEqual(전송기.calls, [])
        self.assertEqual(결과["payload"]["text"], "어제 기사는 모두 120건이었다.")

    def test_브리핑_결과를_슬랙_모양으로_바꾼다(self):
        브리핑 = {
            "markdown": "# 어제의 AI 뉴스",
            "slack_blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "묶음 셋"}}],
            "summary_line": "어제는 오픈AI 모델 공개가 가장 크게 다뤄졌다.",
        }
        with 환경비우기():
            결과 = notify.send_slack(브리핑)
        self.assertEqual(결과["payload"]["blocks"], 브리핑["slack_blocks"])
        self.assertEqual(결과["payload"]["text"], 브리핑["summary_line"])

    def test_문자열도_그대로_받는다(self):
        with 환경비우기():
            결과 = notify.send_slack("한 줄만 보낸다.")
        self.assertEqual(결과["payload"], {"text": "한 줄만 보낸다."})

    def test_웹훅이_없으면_보내지_않고_건너뛴다(self):
        전송기 = 가짜전송기()
        with 환경비우기():
            결과 = notify.send_slack({"text": "보낼 내용"}, dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "skipped")
        self.assertIn("SLACK_WEBHOOK_URL", 결과["reason"])
        self.assertEqual(전송기.calls, [])

    def test_웹훅을_환경변수에서_읽는다(self):
        전송기 = 가짜전송기([(200, "ok")])
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_slack({"text": "보낼 내용"}, dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "sent")
        self.assertEqual(결과["status_code"], 200)
        self.assertEqual(len(전송기.calls), 1)
        self.assertEqual(전송기.calls[0]["url"], 가짜_웹훅)
        self.assertEqual(전송기.calls[0]["method"], "POST")

    def test_인자로_준_웹훅이_환경변수보다_앞선다(self):
        전송기 = 가짜전송기()
        직접준주소 = "https://hooks.slack.example/services/직접/준/주소값입니다"
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            notify.send_slack({"text": "내용"}, webhook_url=직접준주소, dry_run=False, poster=전송기)
        self.assertEqual(전송기.calls[0]["url"], 직접준주소)

    def test_실패하면_상태코드와_응답_앞부분을_남긴다(self):
        전송기 = 가짜전송기([(500, "invalid_payload 라고 서버가 답했다. " + "x" * 500)])
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_slack({"text": "내용"}, dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "failed")
        self.assertEqual(결과["status_code"], 500)
        self.assertIn("invalid_payload", 결과["response_preview"])
        self.assertLessEqual(len(결과["response_preview"]), notify.RESPONSE_PREVIEW_LENGTH)

    def test_실패해도_주소는_어디에도_남지_않는다(self):
        전송기 = 가짜전송기([(403, "거절당했다. 문제의 주소는 %s 였다." % 가짜_웹훅)])
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_slack({"text": "내용"}, dry_run=False, poster=전송기)
        전부 = 문자열전부(결과)
        self.assertNotIn(가짜_웹훅, 전부)
        self.assertNotIn("abcdefghijklmnop", 전부)
        self.assertEqual(결과["webhook"], "ht****")

    def test_연결이_무너져도_결과를_돌려준다(self):
        전송기 = 터지는전송기("%s 로 가는 길이 막혔다." % 가짜_웹훅)
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_slack({"text": "내용"}, dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "failed")
        self.assertNotIn(가짜_웹훅, 문자열전부(결과))

    def test_미리보기에서도_주소는_찍지_않는다(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_slack({"text": "내용"})
        self.assertNotIn(가짜_웹훅, 문자열전부(결과))


class 노션블록변환시험(unittest.TestCase):

    def test_제목과_문단을_알아본다(self):
        블록들 = notify.markdown_to_blocks("# 큰 제목\n## 중간 제목\n### 작은 제목\n그냥 문단이다.")
        종류들 = [블록["type"] for 블록 in 블록들]
        self.assertEqual(종류들, ["heading_1", "heading_2", "heading_3", "paragraph"])
        self.assertEqual(블록들[0]["heading_1"]["rich_text"][0]["text"]["content"], "큰 제목")

    def test_네_단계_제목은_세_단계로_눌러_담는다(self):
        블록들 = notify.markdown_to_blocks("#### 아주 작은 제목")
        self.assertEqual(블록들[0]["type"], "heading_3")

    def test_목록과_인용과_구분선을_알아본다(self):
        본문 = "- 첫째 항목\n* 둘째 항목\n1. 번호 항목\n> 인용한 문장\n---"
        종류들 = [블록["type"] for 블록 in notify.markdown_to_blocks(본문)]
        self.assertEqual(종류들, [
            "bulleted_list_item", "bulleted_list_item", "numbered_list_item", "quote", "divider",
        ])

    def test_빈_줄은_블록을_만들지_않는다(self):
        self.assertEqual(notify.markdown_to_blocks("한 줄\n\n\n두 줄"), notify.markdown_to_blocks("한 줄\n두 줄"))
        self.assertEqual(notify.markdown_to_blocks(""), [])
        self.assertEqual(notify.markdown_to_blocks(None), [])

    def test_링크를_살려_옮긴다(self):
        블록들 = notify.markdown_to_blocks("대표 기사는 [연합뉴스 기사](https://example.com/a) 다.")
        조각들 = 블록들[0]["paragraph"]["rich_text"]
        self.assertEqual(조각들[0]["text"]["content"], "대표 기사는 ")
        self.assertEqual(조각들[1]["text"]["content"], "연합뉴스 기사")
        self.assertEqual(조각들[1]["text"]["link"], {"url": "https://example.com/a"})
        self.assertEqual(조각들[2]["text"]["content"], " 다.")

    def test_굵은_글씨를_살려_옮긴다(self):
        조각들 = notify.markdown_to_blocks("어제 기사는 **120건** 이었다.")[0]["paragraph"]["rich_text"]
        self.assertEqual(조각들[1]["text"]["content"], "120건")
        self.assertTrue(조각들[1]["annotations"]["bold"])
        self.assertFalse(조각들[0]["annotations"]["bold"])

    def test_링크와_굵은_글씨가_섞여도_순서를_지킨다(self):
        본문 = "**규제 소식** 은 [기사](https://example.com/b) 에 실렸다."
        조각들 = notify.markdown_to_blocks(본문)[0]["paragraph"]["rich_text"]
        내용들 = [조각["text"]["content"] for 조각 in 조각들]
        self.assertEqual(내용들, ["규제 소식", " 은 ", "기사", " 에 실렸다."])
        self.assertTrue(조각들[0]["annotations"]["bold"])
        self.assertEqual(조각들[2]["text"]["link"], {"url": "https://example.com/b"})

    def test_긴_문단은_이천자씩_나눈다(self):
        조각들 = notify.markdown_to_blocks("가" * 4500)[0]["paragraph"]["rich_text"]
        self.assertEqual([len(조각["text"]["content"]) for 조각 in 조각들], [2000, 2000, 500])

    def test_같은_마크다운은_늘_같은_블록을_만든다(self):
        본문 = "# 제목\n- 항목 [링크](https://example.com/c)\n> 인용"
        self.assertEqual(notify.markdown_to_blocks(본문), notify.markdown_to_blocks(본문))


class 노션전송시험(unittest.TestCase):

    def test_기본값은_미리보기라_블록만_돌려준다(self):
        전송기 = 가짜전송기()
        with 환경비우기():
            결과 = notify.send_notion("9월 12일 브리핑", "# 제목\n문단이다.", poster=전송기)
        self.assertEqual(결과["status"], "dry_run")
        self.assertEqual(결과["block_count"], 2)
        self.assertEqual(결과["blocks"][0]["type"], "heading_1")
        self.assertEqual(전송기.calls, [])

    def test_미리보기_이유에_빠진_환경변수를_적어_준다(self):
        with 환경비우기():
            결과 = notify.send_notion("제목", "문단")
        self.assertIn("NOTION_TOKEN", 결과["reason"])
        self.assertIn("NOTION_PARENT_PAGE_ID", 결과["reason"])

    def test_토큰이_없으면_조용히_건너뛴다(self):
        전송기 = 가짜전송기()
        with mock.patch.dict(os.environ, {"NOTION_PARENT_PAGE_ID": 가짜_부모}, clear=True):
            결과 = notify.send_notion("제목", "문단", dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "skipped")
        self.assertIn("NOTION_TOKEN", 결과["reason"])
        self.assertEqual(전송기.calls, [])

    def test_부모_페이지가_없으면_건너뛴다(self):
        전송기 = 가짜전송기()
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": 가짜_토큰}, clear=True):
            결과 = notify.send_notion("제목", "문단", dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "skipped")
        self.assertIn("NOTION_PARENT_PAGE_ID", 결과["reason"])
        self.assertEqual(전송기.calls, [])

    def test_환경변수가_있으면_페이지를_만든다(self):
        전송기 = 가짜전송기([(200, json.dumps({"id": "페이지1234"}))])
        환경 = {"NOTION_TOKEN": 가짜_토큰, "NOTION_PARENT_PAGE_ID": 가짜_부모}
        with mock.patch.dict(os.environ, 환경, clear=True):
            결과 = notify.send_notion("9월 12일 브리핑", "# 제목\n문단", dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "sent")
        self.assertEqual(결과["sent_blocks"], 2)
        보낸것 = 전송기.calls[0]
        self.assertEqual(보낸것["url"], notify.NOTION_PAGES_URL)
        self.assertEqual(보낸것["headers"]["Notion-Version"], notify.NOTION_API_VERSION)
        self.assertEqual(보낸것["payload"]["parent"], {"page_id": 가짜_부모})
        제목조각 = 보낸것["payload"]["properties"]["title"]["title"][0]["text"]["content"]
        self.assertEqual(제목조각, "9월 12일 브리핑")

    def test_토큰은_결과와_로그에_남지_않는다(self):
        전송기 = 가짜전송기([(401, "토큰 %s 가 거절당했다." % 가짜_토큰)])
        환경 = {"NOTION_TOKEN": 가짜_토큰, "NOTION_PARENT_PAGE_ID": 가짜_부모}
        with mock.patch.dict(os.environ, 환경, clear=True):
            결과 = notify.send_notion("제목", "문단", dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "failed")
        전부 = 문자열전부(결과)
        self.assertNotIn(가짜_토큰, 전부)
        self.assertNotIn(가짜_부모, 전부)
        self.assertEqual(결과["token"], notify.mask(가짜_토큰))

    def test_블록이_백개를_넘으면_나눠_올린다(self):
        본문 = "\n".join("- 항목 %d" % 번호 for 번호 in range(1, 151))
        전송기 = 가짜전송기([(200, json.dumps({"id": "페이지1234"})), (200, "{}")])
        환경 = {"NOTION_TOKEN": 가짜_토큰, "NOTION_PARENT_PAGE_ID": 가짜_부모}
        with mock.patch.dict(os.environ, 환경, clear=True):
            결과 = notify.send_notion("긴 리포트", 본문, dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "sent")
        self.assertEqual(결과["block_count"], 150)
        self.assertEqual(결과["sent_blocks"], 150)
        self.assertEqual(len(전송기.calls), 2)
        self.assertEqual(len(전송기.calls[0]["payload"]["children"]), 100)
        self.assertEqual(전송기.calls[1]["method"], "PATCH")
        self.assertEqual(len(전송기.calls[1]["payload"]["children"]), 50)
        self.assertIn("페이지1234", 전송기.calls[1]["url"])


class 네트워크차단시험(unittest.TestCase):
    """미리보기와 건너뛰기 길에서는 바깥으로 나가는 통로를 아예 건드리지 않아야 한다."""

    def test_미리보기는_망으로_나가지_않는다(self):
        환경 = {
            "SLACK_WEBHOOK_URL": 가짜_웹훅,
            "NOTION_TOKEN": 가짜_토큰,
            "NOTION_PARENT_PAGE_ID": 가짜_부모,
        }
        with mock.patch.dict(os.environ, 환경, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("망을 탔다.")):
            self.assertEqual(notify.send_slack({"text": "내용"})["status"], "dry_run")
            self.assertEqual(notify.send_notion("제목", "문단")["status"], "dry_run")
            self.assertEqual(notify.send_failure("멈췄다.")["status"], "dry_run")

    def test_비밀값이_없으면_망으로_나가지_않는다(self):
        with 환경비우기(), mock.patch("urllib.request.urlopen", side_effect=AssertionError("망을 탔다.")):
            self.assertEqual(notify.send_slack({"text": "내용"}, dry_run=False)["status"], "skipped")
            self.assertEqual(notify.send_notion("제목", "문단", dry_run=False)["status"], "skipped")


class 실패알림시험(unittest.TestCase):

    def test_기본값은_미리보기다(self):
        전송기 = 가짜전송기()
        with 환경비우기():
            결과 = notify.send_failure("기사가 0건이라 멈췄다.", poster=전송기)
        self.assertEqual(결과["status"], "dry_run")
        self.assertEqual(결과["kind"], "failure")
        self.assertEqual(전송기.calls, [])
        self.assertIn("기사가 0건이라 멈췄다.", 문자열전부(결과))

    def test_점검_결과를_문장으로_편다(self):
        점검 = {
            "exit_code": 1,
            "checks": [
                {"name": "기사 건수", "status": "fail", "detail": "어제 기사가 0건이다."},
                {"name": "날짜 범위", "status": "ok", "detail": "한국 시간 경계와 맞다."},
                {"name": "상한 도달", "status": "warn", "detail": "구간 하나가 250건에 닿았다."},
            ],
        }
        with 환경비우기():
            결과 = notify.send_failure(점검)
        문장 = 결과["summary_text"]
        self.assertIn("기사 건수", 문장)
        self.assertIn("상한 도달", 문장)
        self.assertNotIn("날짜 범위", 문장)

    def test_예외도_그대로_받는다(self):
        with 환경비우기():
            결과 = notify.send_failure(ValueError("날짜 형식이 어긋났다."))
        self.assertIn("날짜 형식이 어긋났다.", 결과["summary_text"])
        self.assertIn("ValueError", 결과["summary_text"])

    def test_웹훅이_있으면_실제로_보낸다(self):
        전송기 = 가짜전송기()
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": 가짜_웹훅}, clear=True):
            결과 = notify.send_failure("점검이 실패했다.", dry_run=False, poster=전송기)
        self.assertEqual(결과["status"], "sent")
        self.assertEqual(len(전송기.calls), 1)
        self.assertIn("멈췄다", 전송기.calls[0]["payload"]["text"])


class 인증서테스트(unittest.TestCase):
    """실제 전송이 인증서 검증을 켠 채 나가는지 본다.

    파이썬이 들고 있는 CA 묶음이 비어 있으면, 컨텍스트를 넘기지 않은 호출은
    요청이 슬랙이나 노션에 닿기도 전에 끊긴다. 반대로 검증을 끄면 웹훅 주소와
    토큰을 실어 보내는 호출이 어디에 닿았는지 확인할 수 없게 된다.
    """

    class 가짜응답:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *예외):
            return False

        def read(self):
            return b"ok"

    def test_검증_컨텍스트를_넘겨_보낸다(self):
        받은것 = {}

        def 가짜열기(request, timeout=None, context=None):
            받은것["timeout"] = timeout
            받은것["context"] = context
            return self.가짜응답()

        with mock.patch("urllib.request.urlopen", 가짜열기):
            상태, 본문 = notify._request_json(
                "https://hooks.slack.example/services/T0000/B0000/x",
                {"text": "확인"},
                timeout=9,
            )

        self.assertEqual((상태, 본문), (200, "ok"))
        컨텍스트 = 받은것["context"]
        self.assertIsInstance(컨텍스트, ssl.SSLContext)
        self.assertEqual(컨텍스트.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(컨텍스트.check_hostname)
        self.assertEqual(받은것["timeout"], 9)


if __name__ == "__main__":
    unittest.main()


class 노션페이지ID뽑기Test(unittest.TestCase):
    """시크릿에 주소를 통째로 넣어도 페이지 ID 만 뽑아 쓰는지 본다."""

    ID = "1a2b3c4d5e6f40718293a4b5c6d7e8f9"

    def test_ID_만_넣으면_그대로다(self):
        from news_brief.notify import normalize_page_id
        self.assertEqual(normalize_page_id(self.ID), self.ID)

    def test_붙임표_UUID_도_받는다(self):
        from news_brief.notify import normalize_page_id
        self.assertEqual(normalize_page_id("1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"), self.ID)

    def test_제목이_붙은_주소에서_뽑는다(self):
        from news_brief.notify import normalize_page_id
        url = "https://www.notion.so/AI-Deface-" + self.ID + "?pvs=4"
        self.assertEqual(normalize_page_id(url), self.ID)

    def test_작업공간_이름이_든_주소에서_뽑는다(self):
        from news_brief.notify import normalize_page_id
        url = "https://www.notion.so/kimhyojun/AI-" + self.ID.upper() + "#abc"
        self.assertEqual(normalize_page_id(url), self.ID)

    def test_못_찾으면_받은_값을_돌려준다(self):
        from news_brief.notify import normalize_page_id
        self.assertEqual(normalize_page_id("페이지주소아님"), "페이지주소아님")
        self.assertEqual(normalize_page_id(""), "")
