# 해제된 힙을 OS에 돌려주는 도우미 — 큰 이미지를 다룬 뒤 RSS가 안 내려가는 문제 대응
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_malloc_trim_probe: list[object] = []


def release_free_heap() -> bool:
    """큰 이미지를 펼쳤다 줄인 뒤 해제된 힙을 OS에 돌려준다(2026-08-06 실측 대응).

    파이썬이 free해도 glibc가 arena에 쥐고 있어 RSS가 안 내려간다. 썸네일 1건당
    약 1MB가 남아 새 기사가 들어오는 낮 동안 4시간에 +71MB씩 올랐고, 이 호출을
    넣은 뒤 1건당 0.19MB로 줄었다.

    glibc가 아니면(윈도우·musl) 조용히 넘어간다.
    """
    if not _malloc_trim_probe:
        trim = None
        if sys.platform.startswith("linux"):
            try:
                trim = ctypes.CDLL("libc.so.6").malloc_trim
                trim.argtypes = [ctypes.c_size_t]
                trim.restype = ctypes.c_int
            except (OSError, AttributeError) as exc:  # noqa: BLE001 - 없으면 안 쓰면 그만이다.
                logger.info("malloc_trim unavailable error=%s", exc)
                trim = None
        _malloc_trim_probe.append(trim)
    trim = _malloc_trim_probe[0]
    if trim is None:
        return False
    try:
        trim(0)
    except Exception as exc:  # noqa: BLE001 - 메모리 반납 실패가 응답을 막으면 안 된다.
        logger.warning("malloc_trim failed error=%s", exc)
        return False
    return True
