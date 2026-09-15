"""GDELT 번역 수집 원본 파일에서 기사를 뽑는 모듈 시험. 망을 타지 않는다."""

import io
import unittest
import urllib.error
import zipfile
from unittest import mock

from news_brief import brief, collect, gdelt, gkg


def _row(date, source, url, lang, title):
    cols = [""] * 27
    cols[0] = "%s-1" % date
    cols[1] = date
    cols[3] = source
    cols[4] = url
    cols[25] = "srclc:%s;eng:GT-%s 1.0" % (lang, lang.upper())
    cols[26] = "<PAGE_TITLE>%s</PAGE_TITLE>" % title
    return "\t".join(cols)


def _zip(rows):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("slice.translation.gkg.csv", "\n".join(rows) + "\n")
    return buffer.getvalue()


class 파일시각시험(unittest.TestCase):
    def test_한국_하루는_파일_96개다(self):
        start, end = "20260913150000", "20260914145959"
        stamps = gkg.slice_stamps(start, end)
        self.assertEqual(len(stamps), 96)
        self.assertEqual(stamps[0], "20260913150000")
        self.assertEqual(stamps[-1], "20260914144500")


class 파일읽기시험(unittest.TestCase):
    def test_한국어이고_제목에_AI_관련어가_든_기사만_남긴다(self):
        body = _zip(
            [
                _row("20260913151500", "zdnet.co.kr", "https://zdnet.co.kr/1", "kor", "오픈AI, 새 모델 공개"),
                _row("20260913151500", "yna.co.kr", "https://yna.co.kr/2", "kor", "추석 귀성길 정체"),
                _row("20260913151500", "sina.com", "https://sina.com/3", "zho", "AI 新模型"),
                _row("20260913151500", "edaily.co.kr", "https://edaily.co.kr/4", "kor", "AIR 부산 노선 확대"),
                _row("20260913151500", "newspim.com", "https://newspim.com/5", "kor", "&#xC0BC;&#xC131;, AI &#xBC18;&#xB3C4;&#xCCB4;"),
            ]
        )
        picked, korean = gkg.parse_file(body, gkg.DEFAULT_TITLE_PATTERN)

        self.assertEqual(korean, 4)
        self.assertEqual([p["url"] for p in picked], ["https://zdnet.co.kr/1", "https://newspim.com/5"])
        self.assertEqual(picked[1]["title"], "삼성, AI 반도체")
        self.assertEqual(picked[0]["seendate"], "20260913T151500Z")
        self.assertEqual(picked[0]["domain"], "zdnet.co.kr")


class 하루치부르개시험(unittest.TestCase):
    def setUp(self):
        self.window = mock.patch.object(
            gkg.config, "kst_day_window", return_value=("20260913150000", "20260914145959")
        )
        self.window.start()
        self.addCleanup(self.window.stop)

    def _opener(self, missing_count):
        호출 = []

        def opener(url):
            호출.append(url)
            순번 = len(호출)
            if 순번 <= missing_count:
                raise urllib.error.HTTPError(url, 404, "없음", None, None)
            stamp = url.rsplit("/", 1)[1][:14]
            return _zip([_row(stamp, "zdnet.co.kr", "https://zdnet.co.kr/%s" % stamp, "kor", "AI 소식 %s" % stamp)])

        return opener, 호출

    def test_구간에_드는_기사만_돌려준다(self):
        opener, 호출 = self._opener(0)
        fetch = gkg.day_fetcher("2026-09-14", {"gkg": {"workers": 1}}, opener=opener)

        전부 = fetch("q", "20260913150000", "20260914145959", maxrecords=1000)["articles"]
        앞 = fetch("q", "20260913150000", "20260913154500", maxrecords=1000)["articles"]

        self.assertEqual(len(전부), 96)
        self.assertEqual(len(앞), 4)
        self.assertEqual(len(호출), 96, "하루치는 한 번만 받아야 한다")

    def test_못_받은_파일이_허용을_넘으면_막힌_것으로_올린다(self):
        opener, _ = self._opener(9)
        fetch = gkg.day_fetcher("2026-09-14", {"gkg": {"workers": 1, "max_missing_files": 8}}, opener=opener)
        with self.assertRaises(gdelt.GdeltRateLimited):
            fetch("q", "20260913150000", "20260914145959")

    def test_허용_안이면_받은_만큼_돌려준다(self):
        opener, _ = self._opener(3)
        fetch = gkg.day_fetcher("2026-09-14", {"gkg": {"workers": 1, "max_missing_files": 8}}, opener=opener)
        self.assertEqual(len(fetch("q", "20260913150000", "20260914145959", maxrecords=1000)["articles"]), 93)
        self.assertEqual(len(fetch.missing()), 3)


class 출처설정시험(unittest.TestCase):
    def test_출처가_gkg_면_수집기가_원본_파일_부르개를_쓴다(self):
        가짜 = mock.Mock(return_value={"articles": []})
        with mock.patch.object(gkg, "day_fetcher", return_value=가짜) as 만들기, mock.patch.object(
            collect.config, "kst_day_window", return_value=("20260913150000", "20260914145959")
        ), mock.patch.object(collect, "_raw_dir", return_value=None), mock.patch.object(
            collect, "_start_partial", return_value=None
        ), mock.patch.object(collect, "_save_raw", return_value=""):
            collect.collect_day("2026-09-14", {"source": "gdelt_gkg", "empty_retries": 0})
        만들기.assert_called_once()
        가짜.assert_called()

    def test_브리핑_출처_문장이_원본_파일을_말한다(self):
        문장 = brief._source_sentence({"source": "gdelt_gkg"})
        self.assertIn("원본 파일", 문장)
        self.assertNotIn("DOC 2.0", 문장)
        self.assertNotIn("—", 문장)
