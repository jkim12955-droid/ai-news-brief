"""깃허브 액션 워크플로 파일을 글로 읽어 보는 시험.

액션을 실제로 돌려 볼 수 없으니 글자로 확인한다. 여기서 보는 것은 세 가지다.
첫째, DB 를 실행 사이에 이어 가는 단계가 있는지. 없으면 매 아침이 빈 DB 로 시작해
'처음 보는 키워드' 가 매일 거짓이 된다.
둘째, 전송 여부를 시크릿 유무로 정하지 않는지. 웹훅 주소를 넣는 일과 실제로
보내기로 정하는 일은 다른 결정이다.
셋째, 커밋 단계가 없는 경로 때문에 통째로 빈손이 되지 않는지. git add 는 경로
지정이 하나라도 맞지 않으면 fatal 로 끝나고 아무것도 스테이지하지 않는다.

YAML 파서를 쓰지 않는다. 이 저장소는 표준 라이브러리와 matplotlib 으로만 돌고
PyYAML 이 없다. 글자와 줄 단위로 보는 것으로 충분한 확인이다.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

뿌리 = Path(__file__).resolve().parents[1]
파일 = 뿌리 / ".github" / "workflows" / "daily.yml"


def 글():
    return 파일.read_text(encoding="utf-8")


def 실행블록들():
    """run: | 아래 셸 블록만 뽑아 돌려준다."""
    묶음 = []
    for 덩어리 in re.findall(r"run: \|\n((?:          .*\n|\n)+)", 글()):
        줄들 = [줄[10:] if 줄.startswith(" " * 10) else 줄 for 줄 in 덩어리.splitlines()]
        묶음.append("\n".join(줄들))
    return 묶음


class 파일있음시험(unittest.TestCase):

    def test_워크플로_파일이_있다(self):
        self.assertTrue(파일.exists(), "%s 가 없다" % 파일)

    def test_셸_블록이_문법에_맞는다(self):
        for i, 블록 in enumerate(실행블록들()):
            with self.subTest(블록=i):
                자리 = Path(tempfile.mkdtemp()) / "s.sh"
                자리.write_text(블록, encoding="utf-8")
                결과 = subprocess.run(
                    ["bash", "-n", str(자리)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(결과.returncode, 0, 결과.stderr)


class DB이어가기시험(unittest.TestCase):
    """매 실행이 빈 DB 로 시작하지 않게 하는 단계가 있는지 본다."""

    def test_DB_를_되살리는_단계가_있다(self):
        본문 = 글()
        self.assertIn("actions/cache/restore", 본문)
        되살리는자리 = 본문.index("actions/cache/restore")
        뒤 = 본문[되살리는자리:되살리는자리 + 400]
        self.assertIn("data/news.db", 뒤)
        self.assertIn("restore-keys", 뒤)

    def test_DB_를_다시_남기는_단계가_있다(self):
        본문 = 글()
        self.assertIn("actions/cache/save", 본문)
        남기는자리 = 본문.index("actions/cache/save")
        뒤 = 본문[남기는자리:남기는자리 + 300]
        self.assertIn("data/news.db", 뒤)

    def test_되살리기가_브리핑보다_먼저_온다(self):
        본문 = 글()
        self.assertLess(
            본문.index("actions/cache/restore"),
            본문.index("python -m news_brief run"),
            "DB 를 되살리기 전에 브리핑을 만들면 빈 DB 로 도는 셈이다",
        )

    def test_남기기가_브리핑_뒤에_온다(self):
        본문 = 글()
        self.assertGreater(
            본문.index("actions/cache/save"), 본문.index("python -m news_brief run")
        )

    def test_중간에_멈춘_실행도_DB_를_남긴다(self):
        # 점검이 실패해 종료 코드 1 로 끝난 날에도 그날 적재한 기사는 DB 에 있다.
        # 남기지 않으면 그 하루가 이력에서 통째로 빠진다.
        본문 = 글()
        앞 = 본문.rindex("- name:", 0, 본문.index("actions/cache/save"))
        self.assertIn("if: always()", 본문[앞:본문.index("actions/cache/save")])

    def test_캐시_경로가_설정의_DB_자리와_같다(self):
        # 설정에서 DB 자리를 옮기면 캐시가 조용히 빈손이 된다. 두 자리를 맞춰 둔다.
        import json

        설정 = json.loads(
            (뿌리 / "config" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertIn(설정["paths"]["db"], 글())


class 전송결정시험(unittest.TestCase):
    """전송 여부를 시크릿 유무로 정하지 않는지 본다."""

    def 전송단계(self):
        본문 = 글()
        시작 = 본문.index("실제로 보낼지 미리보기로 끝낼지 정한다")
        끝 = 본문.index("판단을 규칙으로 할지 모델로 할지 정한다")
        return 본문[시작:끝]

    def test_따로_켜는_변수를_본다(self):
        단계 = self.전송단계()
        self.assertIn("SEND_FOR_REAL", 단계)
        self.assertIn("vars.SEND_FOR_REAL", 글())

    def test_시크릿만으로_실제_전송을_켜지_않는다(self):
        단계 = self.전송단계()
        # dry_run=false 를 내는 자리가 SEND_FOR_REAL 확인 뒤에 있어야 한다.
        거짓자리 = 단계.index("dry_run=false")
        변수자리 = 단계.index("SEND_FOR_REAL")
        self.assertLess(변수자리, 거짓자리)
        # 웹훅 주소가 있다는 것만으로 참을 뒤집는 옛 모양이 남아 있지 않아야 한다.
        self.assertNotRegex(
            단계,
            r'elif \[ -n "\$SLACK_WEBHOOK_URL" \];\s*then\s*\n\s*echo "dry_run=false"',
        )

    def test_예약_실행에도_기본은_미리보기다(self):
        단계 = self.전송단계()
        첫판정 = 단계.index("if [")
        판정글 = 단계[첫판정:단계.index("\n", 단계.index("dry_run=true", 첫판정))]
        self.assertIn("SEND_FOR_REAL", 판정글)
        self.assertIn('!= "true"', 판정글)


class 커밋단계시험(unittest.TestCase):
    """글로브 하나가 빗나가도 커밋이 빈손이 되지 않는지 본다."""

    def 커밋블록(self):
        for 블록 in 실행블록들():
            if "git commit" in 블록:
                return 블록
        raise AssertionError("커밋 단계를 찾지 못했다")

    def test_없는_경로_때문에_통째로_빈손이_되지_않는다(self):
        블록 = self.커밋블록()
        self.assertIn("nullglob", 블록)
        self.assertNotIn(
            "git add --force data/briefs/*.md data/reports/*/*.md",
            블록,
            "세 글로브를 한 번에 넘기면 하나만 빗나가도 아무것도 담기지 않는다",
        )

    def test_실패를_조용히_삼키지_않는다(self):
        # 설명 줄은 뺀다. 무엇을 왜 고쳤는지 적어 둔 주석에 옛 모양이 인용되어 있다.
        명령줄들 = [
            줄 for 줄 in self.커밋블록().splitlines() if not 줄.strip().startswith("#")
        ]
        self.assertNotIn("2>/dev/null || true", "\n".join(명령줄들))

    def test_담을_것이_없으면_그렇게_적는다(self):
        self.assertIn("담을 산출물이 하나도 없다", self.커밋블록())

    def test_실제_산출물_무늬로_글로브가_걸린다(self):
        """지금 저장소 모양에서 무늬 셋 가운데 무엇이 걸리는지 셸로 확인한다."""
        블록 = self.커밋블록()
        무늬들 = re.search(r"found=\((.*?)\)", 블록, re.S).group(1).split()
        self.assertEqual(len(무늬들), 3)
        임시 = Path(tempfile.mkdtemp())
        (임시 / "data" / "briefs").mkdir(parents=True)
        (임시 / "data" / "briefs" / "2026-09-11.md").write_text("브리핑", encoding="utf-8")
        # data/reports 는 첫 실행처럼 아예 없는 상태로 둔다.
        스크립트 = "shopt -s nullglob\nfound=(%s)\nprintf '%%s\\n' \"${found[@]}\"\n" % " ".join(
            무늬들
        )
        결과 = subprocess.run(
            ["bash", "-c", 스크립트], cwd=str(임시), capture_output=True, text=True, timeout=60
        )
        self.assertEqual(결과.returncode, 0, 결과.stderr)
        self.assertEqual(결과.stdout.split(), ["data/briefs/2026-09-11.md"])


class 글쓰기시험(unittest.TestCase):
    """워크플로에 적은 한국어도 프로젝트 규칙을 따른다."""

    def test_줄표와_이모지를_쓰지_않는다(self):
        본문 = 글()
        self.assertNotIn("—", 본문)
        self.assertNotIn("–", 본문)
        for ch in 본문:
            self.assertLess(ord(ch), 0x1F000, "이모지가 섞였다. %r" % ch)


if __name__ == "__main__":
    unittest.main()
