"""arXiv 보강(enrichment).

arXiv 는 더 이상 "발견" 소스가 아니다. Reddit/X 에서 사람들이 실제로 링크한
논문만 골라, 그 논문의 초록·저자·정확한 제목을 arXiv API 로 채워 넣는다.

  발견: 사람 (Reddit/X 에서 링크됨)  →  내용: arXiv API

이렇게 하면 아무도 안 읽은 논문 초록이 리포트를 채우는 일이 없고,
Claude 는 "누가 이 논문을 왜 얘기했는가 + 논문이 실제로 뭔가"를 함께 보게 된다.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import feedparser
import requests

from .models import Item, Paper

log = logging.getLogger(__name__)

API_URL = "http://export.arxiv.org/api/query"
WS_RE = re.compile(r"\s+")
# arXiv API 는 id_list 에 한 번에 여러 개를 받는다. 너무 길면 URL 이 커지므로 쪼갠다.
BATCH_SIZE = 40


class ArxivEnricher:
    """선별된 항목들이 언급한 논문만 arXiv 에서 조회해 붙인다."""

    def __init__(self, config) -> None:
        self.config = config
        cfg = config.section("arxiv")
        self.enabled: bool = cfg.get("enabled", True)
        self.timeout: int = int(cfg.get("request_timeout", 30))
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "discord-ai-trend-bot/1.0 (arxiv enrichment)"}
        )

    # ------------------------------------------------------------------
    def enrich(self, items: list[Item]) -> int:
        """items 에 걸린 arXiv 링크를 조회해 Paper 를 채운다. 채운 논문 수 반환."""
        if not self.enabled:
            return 0

        # 어떤 논문이 어떤 글에서 언급됐는지 모아둔다 (한 논문을 여러 글이 링크 가능)
        wanted: dict[str, list[Item]] = {}
        for item in items:
            for arxiv_id in item.arxiv_ids():
                wanted.setdefault(arxiv_id, []).append(item)

        if not wanted:
            log.info("[arxiv] 링크된 논문 없음 — 보강 건너뜀")
            return 0

        log.info("[arxiv] 논문 %d편 조회 시작", len(wanted))
        papers = self._fetch(list(wanted))

        for arxiv_id, paper in papers.items():
            for item in wanted[arxiv_id]:
                item.papers.append(paper)

        missing = set(wanted) - set(papers)
        if missing:
            log.warning(
                "[arxiv] %d편 조회 실패(취소·비arXiv ID 등): %s",
                len(missing),
                ", ".join(sorted(missing)[:5]),
            )
            # 조회에 실패해도 링크 자체는 살려둔다 (제목 없이 ID만)
            for arxiv_id in missing:
                for item in wanted[arxiv_id]:
                    item.papers.append(Paper(arxiv_id=arxiv_id))

        log.info("[arxiv] %d/%d편 보강 완료", len(papers), len(wanted))
        return len(papers)

    # ------------------------------------------------------------------
    def _fetch(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        papers: dict[str, Paper] = {}
        for start in range(0, len(arxiv_ids), BATCH_SIZE):
            batch = arxiv_ids[start : start + BATCH_SIZE]
            papers.update(self._fetch_batch(batch))
            if start + BATCH_SIZE < len(arxiv_ids):
                time.sleep(3)  # arXiv API 예의상 요청 간격
        return papers

    def _fetch_batch(self, batch: list[str]) -> dict[str, Paper]:
        try:
            resp = self.session.get(
                API_URL,
                params={"id_list": ",".join(batch), "max_results": len(batch)},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - 보강 실패가 리포트를 막지 않게
            log.warning("[arxiv] 조회 실패: %s", exc)
            return {}

        feed = feedparser.parse(resp.content)
        papers: dict[str, Paper] = {}
        for entry in feed.entries:
            arxiv_id = self._entry_id(entry)
            if not arxiv_id:
                continue
            # 삭제·취소된 논문은 title 에 "Error" 가 온다
            title = WS_RE.sub(" ", entry.get("title", "")).strip()
            if not title or title.lower().startswith("error"):
                continue

            authors = [a.get("name", "") for a in entry.get("authors", [])]
            author_str = ", ".join(authors[:3])
            if len(authors) > 3:
                author_str += f" 외 {len(authors) - 3}명"

            primary = entry.get("arxiv_primary_category", {}) or {}
            tags = entry.get("tags") or [{}]
            category = primary.get("term") or tags[0].get("term", "")

            papers[arxiv_id] = Paper(
                arxiv_id=arxiv_id,
                title=title,
                authors=author_str,
                abstract=WS_RE.sub(" ", entry.get("summary", "")).strip(),
                category=category,
                published=self._entry_time(entry),
            )
        return papers

    # ------------------------------------------------------------------
    @staticmethod
    def _entry_id(entry) -> str:
        """entry.id 는 http://arxiv.org/abs/2607.29405v1 형태."""
        raw = entry.get("id", "")
        match = re.search(r"abs/(\d{4}\.\d{4,5})", raw)
        return match.group(1) if match else ""

    @staticmethod
    def _entry_time(entry) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
