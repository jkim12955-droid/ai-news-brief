"""브리핑과 리포트를 슬랙, 노션으로 내보내는 자리다.

기본값은 전부 미리보기라, 이 모듈을 그냥 부르면 바깥으로는 아무것도 나가지 않는다.
실제 전송은 부르는 쪽이 dry_run=False 를 분명히 넘길 때만 일어난다.
웹훅 주소와 토큰은 코드나 설정 파일에 적지 않고 환경변수에서만 읽는다.
로그에 남길 때는 mask 로 덮고, 응답 본문에 섞여 들어온 비밀값도 한 번 더 훑어서 지운다.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

from . import netssl

logger = logging.getLogger("news_brief.notify")

# 노션 API 규격. 버전 헤더가 없으면 요청이 거절된다.
NOTION_API_VERSION = "2022-06-28"
NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_CHILDREN_URL = "https://api.notion.com/v1/blocks/{block_id}/children"

# 노션은 한 번에 자식 블록 100개, 텍스트 한 덩어리 2000자가 상한이다.
MAX_BLOCKS_PER_REQUEST = 100
MAX_TEXT_LENGTH = 2000

# 실패 응답은 앞부분만 남긴다. 통째로 남기면 로그가 길어지고 비밀값이 섞일 위험도 커진다.
RESPONSE_PREVIEW_LENGTH = 200

# 전송에 쓰는 시간 제한. 아침 9시 실행이 여기서 오래 붙들리면 안 된다.
REQUEST_TIMEOUT_SECONDS = 15

_MASK = "****"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_DIVIDERS = ("---", "***", "___")

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def mask(secret):
    """비밀값을 로그에 적어도 되는 모양으로 덮는다.

    앞 두 글자만 남기고 나머지는 별표로 가린다. 별표 개수를 고정해 두어서 길이도 드러나지 않는다.
    슬랙 웹훅 주소처럼 통째로 비밀인 값은 앞 두 글자가 https 의 일부라 아무것도 알려주지 않는다.
    """
    if secret is None:
        return "(없음)"
    text = str(secret).strip()
    if not text:
        return "(없음)"
    if len(text) < 8:
        return _MASK
    return text[:2] + _MASK


def _scrub(text, *secrets):
    """로그나 응답 미리보기로 나갈 문장에서 비밀값을 찾아 덮는다."""
    out = "" if text is None else str(text)
    for secret in secrets:
        if not secret:
            continue
        value = str(secret).strip()
        if len(value) < 4:
            continue
        out = out.replace(value, mask(value))
    return out


def _log(logs, message, *secrets, level=logging.INFO):
    """한 줄을 로그로 남기고 결과 딕셔너리에도 같이 담는다. 비밀값은 나가기 전에 덮는다."""
    line = _scrub(message, *secrets)
    logs.append(line)
    logger.log(level, line)
    return line


def _request_json(url, payload, headers=None, method="POST", timeout=REQUEST_TIMEOUT_SECONDS):
    """JSON 한 덩어리를 보내고 (상태코드, 응답본문) 을 돌려준다.

    실패해도 주소는 밖으로 내보내지 않는다. 연결 자체가 안 되면 상태코드 자리는 None 이 된다.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        # 컨텍스트를 꼭 넘긴다. 파이썬이 들고 있는 CA 묶음이 비어 있으면 기본
        # 컨텍스트로는 슬랙과 노션 호출이 요청을 보내기도 전에 끊긴다.
        with urllib.request.urlopen(
            request, timeout=timeout, context=netssl.ssl_context()
        ) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")
        except Exception:
            detail = error.reason if isinstance(error.reason, str) else "응답 본문을 읽지 못했다."
        return error.code, detail
    except urllib.error.URLError as error:
        return None, "연결하지 못했다. 사유는 %s 다." % (error.reason,)
    except OSError as error:
        return None, "연결하지 못했다. 사유는 %s 다." % (error,)


def _normalize_slack_payload(payload):
    """브리핑이 넘겨준 값을 슬랙 웹훅이 받는 모양으로 맞춘다.

    brief.build_brief 가 주는 {markdown, slack_blocks, summary_line} 도 그대로 받아서 처리한다.
    """
    if payload is None:
        return {"text": "보낼 내용이 비어 있다."}
    if isinstance(payload, str):
        return {"text": payload}
    if not isinstance(payload, dict):
        return {"text": str(payload)}

    summary = str(payload.get("summary_line") or "").strip()
    if "slack_blocks" in payload:
        # 블록만 보내면 알림 목록에 빈 줄로 뜨기 때문에 text 자리에 한 줄 요약을 같이 넣는다.
        return {"blocks": payload["slack_blocks"], "text": summary or "AI 뉴스 브리핑"}
    if "blocks" in payload or "text" in payload:
        body = dict(payload)
        if body.get("blocks") is not None and not str(body.get("text") or "").strip():
            body["text"] = summary or "AI 뉴스 브리핑"
        body.pop("summary_line", None)
        body.pop("markdown", None)
        return body
    if "markdown" in payload:
        return {"text": str(payload["markdown"])}
    return {"text": json.dumps(payload, ensure_ascii=False)}


def _chunk_text(content):
    """긴 문장을 노션이 받는 길이로 자른다."""
    text = "" if content is None else str(content)
    if not text:
        return []
    return [text[i:i + MAX_TEXT_LENGTH] for i in range(0, len(text), MAX_TEXT_LENGTH)]


def _plain_rich_text(content, link=None, bold=False):
    """글자 한 토막을 노션 rich_text 항목으로 만든다."""
    items = []
    for chunk in _chunk_text(content):
        items.append({
            "type": "text",
            "text": {"content": chunk, "link": {"url": link} if link else None},
            "annotations": {"bold": bool(bold)},
        })
    return items


def _inline_rich_text(text):
    """한 줄짜리 마크다운을 rich_text 배열로 바꾼다. 링크와 굵은 글씨만 알아본다."""
    items = []
    rest = "" if text is None else str(text)
    while rest:
        link = _LINK_RE.search(rest)
        bold = _BOLD_RE.search(rest)
        if link and bold:
            first = link if link.start() <= bold.start() else bold
        else:
            first = link or bold
        if first is None:
            items.extend(_plain_rich_text(rest))
            break
        if first.start() > 0:
            items.extend(_plain_rich_text(rest[:first.start()]))
        if first.re is _LINK_RE:
            주소 = first.group(2)
            글 = first.group(1) or 주소
            그림 = first.start() > 0 and rest[first.start() - 1] == "!"
            if 그림 and items and items[-1]["text"]["content"].endswith("!"):
                items[-1]["text"]["content"] = items[-1]["text"]["content"][:-1]
                if not items[-1]["text"]["content"]:
                    items.pop()
            if 주소.lower().startswith(("http://", "https://")):
                items.extend(_plain_rich_text(글, link=주소))
            else:
                # 노션은 상대 경로 링크를 받지 않고 페이지 전체를 거절한다(400 Invalid URL for link).
                꼬리 = " (이미지는 노션에 올리지 않았다)" if 그림 else ""
                items.extend(_plain_rich_text(글 + 꼬리))
        else:
            items.extend(_plain_rich_text(first.group(1), bold=True))
        rest = rest[first.end():]
    if not items:
        items = _plain_rich_text(" ")
    return items


def _block(kind, text):
    """본문 한 줄을 노션 블록 하나로 감싼다."""
    return {
        "object": "block",
        "type": kind,
        kind: {"rich_text": _inline_rich_text(text)},
    }


def markdown_to_blocks(markdown):
    """마크다운을 노션 블록 배열로 바꾸는 최소 변환기다.

    제목 세 단계, 문단, 글머리 목록, 번호 목록, 인용, 구분선을 알아본다.
    줄 안에서는 링크와 굵은 글씨만 살린다. 표나 코드 블록은 그냥 문단으로 흘려보낸다.
    """
    blocks = []
    for raw_line in ("" if markdown is None else str(markdown)).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in _DIVIDERS:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            # 노션 제목은 세 단계까지라 그보다 깊은 제목은 heading_3 으로 눌러 담는다.
            level = min(len(heading.group(1)), 3)
            blocks.append(_block("heading_%d" % level, heading.group(2)))
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            blocks.append(_block("quote", quote.group(1)))
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            blocks.append(_block("bulleted_list_item", bullet.group(1)))
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered:
            blocks.append(_block("numbered_list_item", numbered.group(1)))
            continue
        blocks.append(_block("paragraph", line))
    return blocks


def send_slack(payload, webhook_url=None, dry_run=True, poster=None):
    """브리핑을 슬랙 웹훅으로 보낸다. 기본값은 보내지 않고 내용만 돌려주는 미리보기다.

    웹훅 주소는 인자로 받거나 SLACK_WEBHOOK_URL 환경변수에서 읽는다.
    실패하면 상태코드와 응답 앞부분을 남기지만 주소는 어느 로그에도 찍지 않는다.
    poster 는 테스트에서 실제 전송 대신 끼워 넣는 자리다.
    """
    logs = []
    url = str(webhook_url or os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    body = _normalize_slack_payload(payload)
    result = {
        "channel": "slack",
        "status": "",
        "dry_run": bool(dry_run),
        "reason": "",
        "payload": body,
        "webhook": mask(url),
        "status_code": None,
        "response_preview": "",
        "logs": logs,
    }

    if dry_run:
        result["status"] = "dry_run"
        if url:
            result["reason"] = "미리보기라 보내지 않고 내용만 돌려준다."
        else:
            result["reason"] = "미리보기라 보내지 않고 내용만 돌려준다. 웹훅 주소는 아직 없다."
        _log(logs, "슬랙은 미리보기로 끝냈다. 실제로 보내지 않았다.", url)
        return result

    if not url:
        result["status"] = "skipped"
        result["reason"] = "SLACK_WEBHOOK_URL 이 비어 있어서 보내지 않고 건너뛰었다."
        _log(logs, result["reason"], level=logging.WARNING)
        return result

    send = poster or _request_json
    try:
        status_code, response = send(url, body, {"Accept": "application/json"}, "POST")
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = _scrub("전송 도중 예외가 났다. 사유는 %s 다." % (error,), url)
        _log(logs, result["reason"], url, level=logging.ERROR)
        return result

    preview = _scrub(str(response or "")[:RESPONSE_PREVIEW_LENGTH], url)
    result["status_code"] = status_code
    result["response_preview"] = preview

    if isinstance(status_code, int) and 200 <= status_code < 300:
        result["status"] = "sent"
        result["reason"] = "슬랙으로 보냈다."
        _log(logs, "슬랙 전송에 성공했다. 응답 코드는 %s 다." % (status_code,), url)
    else:
        result["status"] = "failed"
        result["reason"] = "슬랙 전송에 실패했다. 응답 코드는 %s, 응답 앞부분은 %s 다." % (
            status_code if status_code is not None else "없음", preview or "비어 있다",
        )
        _log(logs, result["reason"], url, level=logging.ERROR)
    return result



_UUID_DASHED = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
_HEX32_TAIL = re.compile(r"([0-9a-fA-F]{32})$")
_HEX32_ALONE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")


def normalize_page_id(value):
    """노션 페이지 ID 를 32자리로 뽑는다.

    시크릿에 ID 만 넣지 않고 페이지 주소를 통째로 붙여 넣는 경우가 많다.
    주소라면 질의와 조각을 떼고 마지막 경로 조각에서 찾는다. 제목 뒤에 붙은
    32자리, 붙임표가 든 UUID, 홀로 선 32자리 순서로 본다. 못 찾으면 받은 값을
    그대로 돌려줘서 노션이 내는 오류로 원인을 알 수 있게 한다.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    segment = head.rsplit("/", 1)[-1]
    for pattern in (_UUID_DASHED, _HEX32_TAIL, _HEX32_ALONE):
        found = pattern.search(segment) or pattern.search(head)
        if found:
            return found.group(1).replace("-", "").lower()
    return text


def send_notion(title, markdown, token=None, parent_page_id=None, dry_run=True, poster=None):
    """브리핑이나 리포트를 노션 페이지로 쌓는다. 기본값은 블록만 만들어 보여주는 미리보기다.

    토큰은 NOTION_TOKEN, 부모 페이지는 NOTION_PARENT_PAGE_ID 환경변수에서 읽는다.
    둘 중 하나라도 없으면 실제 전송은 조용히 건너뛰고 건너뛴 이유를 돌려준다.
    노션 연동은 첫 완성 뒤에 붙일 계획이라 지금은 이 자리만 만들어 둔다.
    """
    logs = []
    resolved_token = str(token or os.environ.get("NOTION_TOKEN") or "").strip()
    resolved_parent = normalize_page_id(parent_page_id or os.environ.get("NOTION_PARENT_PAGE_ID") or "")
    page_title = str(title or "제목 없음")[:MAX_TEXT_LENGTH]
    blocks = markdown_to_blocks(markdown)

    result = {
        "channel": "notion",
        "status": "",
        "dry_run": bool(dry_run),
        "reason": "",
        "title": page_title,
        "blocks": blocks,
        "block_count": len(blocks),
        "sent_blocks": 0,
        "token": mask(resolved_token),
        "parent_page_id": mask(resolved_parent),
        "status_code": None,
        "response_preview": "",
        "logs": logs,
    }

    missing = []
    if not resolved_token:
        missing.append("NOTION_TOKEN")
    if not resolved_parent:
        missing.append("NOTION_PARENT_PAGE_ID")

    if dry_run:
        result["status"] = "dry_run"
        reason = "미리보기라 보내지 않고 블록 %d개만 만들어 돌려준다." % len(blocks)
        if missing:
            reason += " 실제로 보내려면 %s 가 있어야 한다." % ", ".join(missing)
        result["reason"] = reason
        _log(logs, "노션은 미리보기로 끝냈다. 블록 %d개를 만들었다." % len(blocks),
             resolved_token, resolved_parent)
        return result

    if missing:
        result["status"] = "skipped"
        result["reason"] = "%s 가 없어서 노션 전송을 건너뛰었다." % ", ".join(missing)
        _log(logs, result["reason"], level=logging.INFO)
        return result

    headers = {
        "Authorization": "Bearer %s" % resolved_token,
        "Notion-Version": NOTION_API_VERSION,
        "Accept": "application/json",
    }
    payload = {
        "parent": {"page_id": resolved_parent},
        "properties": {"title": {"title": [{"text": {"content": page_title}}]}},
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    }

    send = poster or _request_json
    try:
        status_code, response = send(NOTION_PAGES_URL, payload, headers, "POST")
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = _scrub("노션 전송 도중 예외가 났다. 사유는 %s 다." % (error,),
                                  resolved_token, resolved_parent)
        _log(logs, result["reason"], resolved_token, resolved_parent, level=logging.ERROR)
        return result

    preview = _scrub(str(response or "")[:RESPONSE_PREVIEW_LENGTH], resolved_token, resolved_parent)
    result["status_code"] = status_code
    result["response_preview"] = preview

    if not (isinstance(status_code, int) and 200 <= status_code < 300):
        result["status"] = "failed"
        result["reason"] = "노션 전송에 실패했다. 응답 코드는 %s, 응답 앞부분은 %s 다." % (
            status_code if status_code is not None else "없음", preview or "비어 있다",
        )
        _log(logs, result["reason"], resolved_token, resolved_parent, level=logging.ERROR)
        return result

    result["sent_blocks"] = len(payload["children"])
    page_id = _page_id_from(response)
    if page_id:
        result["page_id"] = mask(page_id)
    page_url = _page_url_from(response)
    if page_url:
        result["url"] = page_url
    _log(logs, "노션 페이지를 만들었다. 블록 %d개를 함께 올렸다." % result["sent_blocks"],
         resolved_token, resolved_parent)

    rest = blocks[MAX_BLOCKS_PER_REQUEST:]
    if rest and not page_id:
        result["status"] = "partial"
        result["reason"] = ("페이지는 만들었지만 응답에서 페이지 번호를 찾지 못해 남은 블록 %d개를 "
                            "올리지 못했다." % len(rest))
        _log(logs, result["reason"], resolved_token, resolved_parent, level=logging.WARNING)
        return result

    while rest:
        piece = rest[:MAX_BLOCKS_PER_REQUEST]
        rest = rest[MAX_BLOCKS_PER_REQUEST:]
        url = NOTION_CHILDREN_URL.format(block_id=page_id)
        try:
            code, body = send(url, {"children": piece}, headers, "PATCH")
        except Exception as error:
            result["status"] = "partial"
            result["reason"] = _scrub("남은 블록을 올리다 예외가 났다. 사유는 %s 다." % (error,),
                                      resolved_token, resolved_parent)
            _log(logs, result["reason"], resolved_token, resolved_parent, level=logging.ERROR)
            return result
        if not (isinstance(code, int) and 200 <= code < 300):
            result["status"] = "partial"
            result["status_code"] = code
            result["response_preview"] = _scrub(
                str(body or "")[:RESPONSE_PREVIEW_LENGTH], resolved_token, resolved_parent)
            result["reason"] = "남은 블록을 올리지 못했다. 응답 코드는 %s 다." % (
                code if code is not None else "없음",)
            _log(logs, result["reason"], resolved_token, resolved_parent, level=logging.ERROR)
            return result
        result["sent_blocks"] += len(piece)
        _log(logs, "이어서 블록 %d개를 더 올렸다." % len(piece), resolved_token, resolved_parent)

    result["status"] = "sent"
    result["reason"] = "노션에 블록 %d개를 올렸다." % result["sent_blocks"]
    return result


def _page_url_from(response):
    """노션 응답에서 새 페이지 주소를 꺼낸다. 비밀값이 아니라 그대로 남긴다."""
    data = response
    if not isinstance(data, dict):
        try:
            data = json.loads(response or "")
        except (TypeError, ValueError):
            return ""
    if isinstance(data, dict):
        return str(data.get("url") or "")
    return ""


def _page_id_from(response):
    """노션 응답에서 새로 만들어진 페이지 번호를 꺼낸다. 못 찾으면 빈 문자열이다."""
    if isinstance(response, dict):
        return str(response.get("id") or "")
    try:
        parsed = json.loads(response or "")
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("id") or "")
    return ""


def _failure_text(summary):
    """점검 결과나 예외 내용을 사람이 읽을 문장으로 편다."""
    if isinstance(summary, str):
        body = summary.strip()
    elif isinstance(summary, BaseException):
        body = "%s: %s" % (type(summary).__name__, summary)
    elif isinstance(summary, dict):
        lines = []
        checks = summary.get("checks")
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                if check.get("status") in ("fail", "warn"):
                    lines.append("%s 점검이 %s 로 끝났다. %s" % (
                        check.get("name", "이름 없는 점검"),
                        check.get("status"),
                        check.get("detail", "자세한 내용이 없다."),
                    ))
        if not lines:
            lines = ["%s: %s" % (key, value) for key, value in summary.items() if key != "checks"]
        body = "\n".join(lines)
    elif isinstance(summary, (list, tuple)):
        body = "\n".join(str(item) for item in summary)
    else:
        body = str(summary or "")
    return body or "자세한 내용이 넘어오지 않았다."


def send_failure(summary, dry_run=True, webhook_url=None, poster=None):
    """점검이 어긋나거나 예외가 났을 때 슬랙으로 알린다. 기본값은 미리보기다.

    summary 로는 문장, 예외, qa.run_checks 가 돌려준 딕셔너리를 다 받는다.
    """
    body = _failure_text(summary)
    head = "AI 뉴스 브리핑 실행이 도중에 멈췄다."
    payload = {
        "text": "%s %s" % (head, body.splitlines()[0] if body else ""),
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*%s*" % head}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
        ],
    }
    result = send_slack(payload, webhook_url=webhook_url, dry_run=dry_run, poster=poster)
    result["kind"] = "failure"
    result["summary_text"] = body
    return result
