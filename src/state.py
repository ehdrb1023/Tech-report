"""이미 보도한 항목 기록 — 같은 내용이 며칠 연속 올라가는 걸 막는다.

on-demand 방식에서는 화제의 논문이 3~4일 회자되는 게 흔하다. 24시간 창만으로는
"어제 karpathy 가 언급 → 오늘 다른 사람이 또 언급" 을 걸러내지 못해 같은 논문이
반복 보도된다. 그래서 보도한 arXiv ID 와 글 ID 를 기록해 둔다.

GitHub Actions 에서는 actions/cache 로 state/ 디렉터리를 실행 간에 이어붙인다.
캐시가 없어도(첫 실행, 캐시 만료) 그냥 중복 억제가 안 될 뿐 동작에는 지장이 없다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Item

log = logging.getLogger(__name__)

FILENAME = "seen.json"


class SeenStore:
    def __init__(self, config) -> None:
        cfg = config.section("state")
        self.enabled: bool = cfg.get("enabled", True)
        self.ttl_days: int = int(cfg.get("ttl_days", 7))
        self.path: Path = Path(config.state_dir) / FILENAME
        self._entries: dict[str, str] = {}  # key -> ISO8601 보도 시각
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("[state] 기록 읽기 실패(%s) — 빈 상태로 시작", exc)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.ttl_days)
        for key, stamp in (raw.get("entries") or {}).items():
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when >= cutoff:  # 오래된 기록은 버려서 파일이 무한히 안 커지게
                self._entries[key] = stamp
        log.info("[state] 최근 %d일 보도 기록 %d건 로드", self.ttl_days, len(self._entries))

    # ------------------------------------------------------------------
    @staticmethod
    def _keys(item: Item) -> list[str]:
        """한 항목을 식별하는 키들. 논문은 글 ID 와 별개로 따로 잡는다."""
        keys = [item.uid]
        keys.extend(f"arxiv:{arxiv_id}" for arxiv_id in item.arxiv_ids())
        return keys

    def filter_unseen(self, items: list[Item]) -> tuple[list[Item], int]:
        """이미 보도한 항목을 제외. (남은 항목, 걸러낸 수) 반환."""
        if not self.enabled:
            return items, 0

        kept: list[Item] = []
        skipped = 0
        for item in items:
            if any(key in self._entries for key in self._keys(item)):
                skipped += 1
                log.debug("[state] 중복 보도 제외: %s", item.title[:60])
                continue
            kept.append(item)

        if skipped:
            log.info("[state] 이미 보도한 %d건 제외", skipped)
        return kept, skipped

    # ------------------------------------------------------------------
    def record(self, items: list[Item]) -> None:
        """실제로 보도한 항목을 기록. 전송에 성공한 뒤에만 부를 것."""
        if not self.enabled:
            return
        now = datetime.now(timezone.utc).isoformat()
        for item in items:
            for key in self._keys(item):
                self._entries[key] = now

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"entries": self._entries}, ensure_ascii=False, indent=1
                ),
                encoding="utf-8",
            )
            log.info("[state] 보도 기록 %d건 저장", len(self._entries))
        except OSError as exc:
            log.warning("[state] 기록 저장 실패: %s", exc)
