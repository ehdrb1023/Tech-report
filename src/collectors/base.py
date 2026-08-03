"""수집기 공통 베이스 + HTTP 헬퍼."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from ..config import Config
from ..models import Item

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; discord-ai-trend-bot/1.0; "
    "+https://github.com/)"
)


class Collector(ABC):
    """모든 수집기의 공통 인터페이스.

    수집기는 절대 예외를 밖으로 던지지 않는다. 한 소스가 죽어도
    나머지 소스로 리포트는 나가야 하기 때문에, 실패는 로그 + 빈 리스트로 처리한다.
    """

    name: str = "base"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA})

    @abstractmethod
    def _collect(self) -> list[Item]:
        """실제 수집 로직. 서브클래스가 구현."""

    def collect(self) -> list[Item]:
        started = time.monotonic()
        try:
            items = self._collect()
        except Exception as exc:  # noqa: BLE001 - 소스 하나가 전체를 막지 않도록
            log.error("[%s] 수집 실패: %s", self.name, exc, exc_info=True)
            return []
        elapsed = time.monotonic() - started
        log.info("[%s] %d건 수집 (%.1fs)", self.name, len(items), elapsed)
        return items

    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        timeout: int = 20,
        retries: int = 2,
        backoff: float = 1.5,
        **kwargs: Any,
    ) -> requests.Response | None:
        """재시도가 붙은 GET.

        실패하면 None. 단 재시도를 모두 소진한 원인이 429(rate limit)이면
        호출자가 구분해서 더 길게 대기할 수 있도록 그 응답을 그대로 돌려준다.
        """
        last_exc: Exception | None = None
        last_429: requests.Response | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, timeout=timeout, **kwargs)
                if resp.status_code == 429:
                    last_429 = resp
                    if attempt >= retries:
                        break
                    wait = float(resp.headers.get("Retry-After", backoff))
                    log.warning("[%s] 429 rate limit, %.1fs 대기", self.name, wait)
                    time.sleep(min(wait, 10))
                    continue
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
        if last_429 is not None:
            return last_429
        log.warning("[%s] GET 실패 %s (%s)", self.name, url, last_exc)
        return None
