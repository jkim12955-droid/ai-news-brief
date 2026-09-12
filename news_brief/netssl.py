"""https 호출에 쓸 SSL 컨텍스트를 한 자리에서 만든다.

이 파일이 따로 있는 까닭이 있다. python.org 에서 받은 맥용 파이썬은 시스템
키체인을 보지 않고 자기 폴더 안의 etc/openssl/cert.pem 만 본다. 설치 직후
그 파일이 없으면 CA 묶음이 통째로 비고, 그러면 이 프로젝트에서 바깥으로
나가는 모든 호출이 요청을 보내기도 전에 TLS 손잡기에서 끊긴다. GDELT 수집,
앤스로픽 판단, 슬랙과 노션 전송이 모두 같은 자리에서 막힌다.

그래서 컨텍스트를 만드는 일을 세 모듈에 흩어 두지 않고 여기 모았다.
검증은 어느 경로에서도 끄지 않는다. 받은 제목을 그대로 브리핑에 올리는
파이프라인이라, 검증을 끄면 중간에서 바꿔친 응답을 알아볼 길이 없다.
"""

from __future__ import annotations

import logging
import os
import ssl

logger = logging.getLogger(__name__)

# 파이썬이 들고 있는 CA 묶음이 비어 있을 때 대신 찾아볼 자리들.
# 운영체제가 들고 있는 묶음을 찾아 쓰되, 검증 자체는 그대로 켜 둔다.
CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/usr/local/etc/openssl/cert.pem",
)

# 컨텍스트를 만드는 데 값이 좀 든다. 한 번 만든 것을 다시 쓴다.
_ssl_context = None


def _ca_bundle_path():
    """인증서 검증에 쓸 CA 묶음 파일을 찾는다. 못 찾으면 None 이다."""
    named = os.environ.get("SSL_CERT_FILE")
    if named and os.path.isfile(named):
        return named
    try:
        import certifi
    except ImportError:
        pass
    else:
        path = certifi.where()
        if os.path.isfile(path):
            return path
    for path in CA_BUNDLE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def ssl_context():
    """인증서를 검증하는 SSL 컨텍스트를 돌려준다.

    기본 컨텍스트에 CA 가 하나도 없으면 운영체제 묶음을 찾아 얹는다.
    찾지 못하면 기본 컨텍스트를 그대로 돌려준다. 검증을 끄지는 않는다.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    context = ssl.create_default_context()
    if not context.get_ca_certs():
        path = _ca_bundle_path()
        if path:
            logger.warning(
                "파이썬이 들고 있는 CA 묶음이 비어 있어 %s 를 대신 쓴다. "
                "맥에서 python.org 파이썬을 쓰면 'Install Certificates.command' 를 "
                "한 번 돌려 두는 편이 깔끔하다.",
                path,
            )
            context = ssl.create_default_context(cafile=path)
        else:
            logger.error(
                "인증서를 검증할 CA 묶음을 찾지 못했다. https 호출이 막힐 수 있다. "
                "SSL_CERT_FILE 에 묶음 파일 자리를 적어 주면 된다."
            )
    _ssl_context = context
    return _ssl_context
