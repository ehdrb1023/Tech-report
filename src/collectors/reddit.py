"""Reddit 수집기 — 경로가 두 개다.

1) 공개 RSS  (기본값, 인증 불필요)
   https://www.reddit.com/r/{sub}/new/.rss
   Reddit 은 공개 `.json` 엔드포인트를 어떤 User-Agent 로도 403 으로 막지만,
   RSS/Atom 피드는 열려 있다. 대신 제약이 있다:
     - 업보트·댓글 수가 없다 (인기도 스코어링 불가)
     - after 커서 페이지네이션이 안 된다 (403) → 서브레딧당 최신 ~25건이 한계
     - 연속 요청에 민감해서 429 가 잘 뜬다 → 요청 사이 딜레이 필수

2) OAuth + JSON  (REDDIT_CLIENT_ID/SECRET 가 있을 때만)
   oauth.reddit.com 은 업보트·댓글 수를 주고 페이지네이션도 된다.
   앱 등록이 가능해지면 이 경로가 훨씬 낫다. 자격증명이 없으면 자동으로 1)로 간다.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from ..models import Item
from .base import Collector

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"
RSS_URL = "https://www.reddit.com/r/{sub}/new/.rss"
# /new 는 최신 25건뿐이라 활발한 서브레딧은 창의 앞부분이 잘린다.
# 그런 곳만 골라 "오늘의 인기글"을 한 번 더 받아 놓친 글을 건진다.
TOP_URL = "https://www.reddit.com/r/{sub}/top/.rss?t=day"
PAGE_LIMIT = 100  # OAuth 경로에서만 의미 있음

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
HREF_RE = re.compile(r'href="(https?://[^"]+)"')


class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, config) -> None:
        super().__init__(config)
        self.session.headers["User-Agent"] = config.reddit_user_agent
        self._base = OAUTH_BASE
        self._authed = self._authenticate()
        self._requests_made = 0
        cfg = config.reddit
        # 차단 위험을 줄이기 위한 요청 간 딜레이 (RSS 경로)
        self.delay = float(cfg.get("request_delay_seconds", 2.0))
        self.rate_limit_wait = float(cfg.get("rate_limit_wait_seconds", 60.0))
        # /new 25건으로 창을 못 덮은 서브레딧만 "오늘의 인기글"을 추가로 받을지
        self.include_top = bool(cfg.get("supplement_with_top_daily", True))

    # ------------------------------------------------------------------
    def _authenticate(self) -> bool:
        """자격증명이 있을 때만 시도. 없으면 조용히 RSS 경로로 간다."""
        cid = self.config.reddit_client_id
        secret = self.config.reddit_client_secret
        if not cid or not secret:
            log.info("[reddit] OAuth 자격증명 없음 → 공개 RSS 사용")
            return False
        try:
            resp = requests.post(
                TOKEN_URL,
                auth=(cid, secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": self.config.reddit_user_agent},
                timeout=20,
            )
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if not token:
                raise ValueError("access_token 없음")
            self.session.headers["Authorization"] = f"bearer {token}"
            log.info("[reddit] OAuth 인증 성공 — JSON API 사용 (업보트·페이지네이션 가능)")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[reddit] OAuth 실패(%s) → 공개 RSS 폴백", exc)
            return False

    # ------------------------------------------------------------------
    def _collect(self) -> list[Item]:
        cfg = self.config.reddit
        if not cfg.get("enabled", True):
            return []

        subs: list[str] = cfg.get("subreddits", [])
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.lookback_hours
        )

        if self._authed:
            items, failed = self._collect_via_oauth(subs, cutoff)
        else:
            items, failed = self._collect_via_rss(subs, cutoff)

        if failed:
            log.warning(
                "[reddit] %d/%d 서브레딧 수집 실패 (429/차단). "
                "config.yaml 의 reddit.request_delay_seconds 를 늘려보세요.",
                failed,
                len(subs),
            )
        log.info("[reddit] 요청 %d회로 %d건 수집", self._requests_made, len(items))
        return items

    # ==================================================================
    # 1) 공개 RSS — 인증 불필요
    # ==================================================================
    def _collect_via_rss(
        self, subs: list[str], cutoff: datetime
    ) -> tuple[list[Item], int]:
        items: list[Item] = []
        failed = 0
        truncated: list[str] = []

        for index, sub in enumerate(subs):
            if index:
                time.sleep(self.delay)  # 요청 사이 간격 — 차단 위험 완화

            entries = self._fetch_feed(sub)
            if entries is None:
                failed += 1
                continue

            kept = 0
            oldest: datetime | None = None
            for entry in entries:
                published = self._entry_time(entry)
                if published and (oldest is None or published < oldest):
                    oldest = published
                item = self._entry_to_item(entry, sub, cutoff)
                if item is not None:
                    items.append(item)
                    kept += 1

            # RSS 는 ~25건이 한계라, 가장 오래된 글이 아직 창 안이면 그 앞은 못 본 것
            if oldest is not None and oldest > cutoff and kept == len(entries):
                truncated.append(sub)
            log.debug("[reddit] r/%s → %d/%d건", sub, kept, len(entries))

        # 잘린 서브레딧만 "오늘의 인기글"을 추가로 받아 놓친 글을 보충한다.
        # (전체에 적용하면 요청이 두 배가 되어 429·실행시간이 늘어난다)
        if truncated and self.include_top:
            seen = {i.uid for i in items}
            recovered = 0
            for sub in truncated:
                time.sleep(self.delay)
                entries = self._fetch_feed(sub, TOP_URL)
                if not entries:
                    continue
                for entry in entries:
                    item = self._entry_to_item(entry, sub, cutoff)
                    if item is not None and item.uid not in seen:
                        seen.add(item.uid)
                        items.append(item)
                        recovered += 1
            log.info(
                "[reddit] 잘린 %d개 서브레딧에서 인기글 %d건 추가 확보",
                len(truncated),
                recovered,
            )

        if truncated:
            log.warning(
                "[reddit] %s 는 /new 상한(25건)으로 %d시간을 다 못 덮었습니다"
                "%s. 완전히 덮으려면 OAuth 자격증명이 필요합니다.",
                ", ".join(f"r/{s}" for s in truncated),
                self.config.lookback_hours,
                " (인기글로 일부 보충함)" if self.include_top else "",
            )
        return items, failed

    def _fetch_feed(self, sub: str, url_template: str = RSS_URL):
        """429 를 만나면 한 번 더 기다렸다 재시도한다. 실패 시 None."""
        url = url_template.format(sub=sub)
        for attempt in range(2):
            resp = self.get(url, timeout=25, retries=0)
            self._requests_made += 1

            if resp is not None and resp.status_code == 429:
                if attempt == 0:
                    log.info(
                        "[reddit] r/%s 429 — %.0f초 대기 후 재시도",
                        sub,
                        self.rate_limit_wait,
                    )
                    time.sleep(self.rate_limit_wait)
                    continue
                log.warning("[reddit] r/%s 재시도 후에도 429 — 건너뜀", sub)
                return None
            if resp is None:
                return None

            feed = feedparser.parse(resp.content)
            if feed.entries:
                return feed.entries
            log.debug("[reddit] r/%s 빈 피드", sub)
            return None
        return None

    # ------------------------------------------------------------------
    def _entry_to_item(self, entry, sub: str, cutoff: datetime) -> Item | None:
        published = self._entry_time(entry)
        if published is None or published < cutoff:
            return None

        raw_html = ""
        content = entry.get("content") or []
        if content:
            raw_html = content[0].get("value", "") or ""
        raw_html = raw_html or entry.get("summary", "") or ""

        # 링크 포스트의 대상 URL(논문 링크 등)은 본문 HTML 안에만 있다
        outbound = [
            url
            for url in HREF_RE.findall(raw_html)
            if "reddit.com" not in url and "redd.it" not in url
        ]

        body = WS_RE.sub(" ", TAG_RE.sub(" ", raw_html)).strip()
        author = entry.get("author", "") or ""

        return Item(
            source="reddit",
            origin=f"r/{sub}",
            uid=f"reddit:{entry.get('id') or entry.get('link', '')}",
            title=WS_RE.sub(" ", entry.get("title", "")).strip(),
            url=entry.get("link", ""),
            author=author if author.startswith("/u/") else f"/u/{author}",
            body=body[:2000],
            created_at=published,
            # RSS 에는 업보트·댓글 수가 없다. OAuth 경로에서만 채워진다.
            engagement=0,
            comments=0,
            outbound_urls=outbound,
        )

    @staticmethod
    def _entry_time(entry) -> datetime | None:
        parsed = entry.get("updated_parsed") or entry.get("published_parsed")
        if not parsed:
            return None
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)

    # ==================================================================
    # 2) OAuth + JSON — 자격증명이 있을 때만
    # ==================================================================
    def _collect_via_oauth(
        self, subs: list[str], cutoff: datetime
    ) -> tuple[list[Item], int]:
        cfg = self.config.reddit
        min_upvotes = int(cfg.get("min_upvotes", 0))
        max_pages = int(cfg.get("max_pages_per_sub", 5))

        items: list[Item] = []
        failed = 0
        truncated: list[str] = []

        for sub in subs:
            sub_items, ok, hit_cap = self._oauth_subreddit(
                sub, cutoff, min_upvotes, max_pages
            )
            if not ok:
                failed += 1
                continue
            if hit_cap:
                truncated.append(sub)
            items.extend(sub_items)

        if truncated:
            log.warning(
                "[reddit] %s 는 %d페이지로도 24시간을 못 덮었습니다 — "
                "reddit.max_pages_per_sub 를 늘리세요.",
                ", ".join(f"r/{s}" for s in truncated),
                max_pages,
            )
        return items, failed

    def _oauth_subreddit(
        self, sub: str, cutoff: datetime, min_upvotes: int, max_pages: int
    ) -> tuple[list[Item], bool, bool]:
        items: list[Item] = []
        after: str | None = None
        reached_cutoff = False
        any_success = False

        for _ in range(max_pages):
            params = {"limit": PAGE_LIMIT, "raw_json": 1}
            if after:
                params["after"] = after

            resp = self.get(
                f"{self._base}/r/{sub}/new", params=params, timeout=20, retries=1
            )
            self._requests_made += 1
            if resp is None or "json" not in resp.headers.get("content-type", ""):
                break
            try:
                data = resp.json().get("data", {})
            except ValueError:
                break
            any_success = True

            for child in data.get("children", []):
                post = child.get("data", {})
                if post.get("stickied") or post.get("over_18"):
                    continue
                created = datetime.fromtimestamp(
                    post.get("created_utc", 0), tz=timezone.utc
                )
                if created < cutoff:
                    reached_cutoff = True
                    break
                if int(post.get("score", 0)) < min_upvotes:
                    continue
                items.append(self._post_to_item(post, sub, created))

            after = data.get("after")
            if reached_cutoff or not after:
                break

        hit_cap = any_success and not reached_cutoff and after is not None
        return items, any_success, hit_cap

    @staticmethod
    def _post_to_item(post: dict, sub: str, created: datetime) -> Item:
        permalink = post.get("permalink", "")
        outbound: list[str] = []
        raw_url = post.get("url_overridden_by_dest") or post.get("url", "")
        if raw_url and "reddit.com" not in raw_url:
            outbound.append(raw_url)

        return Item(
            source="reddit",
            origin=f"r/{sub}",
            uid=f"reddit:{post.get('id')}",
            title=post.get("title", "").strip(),
            url=f"https://www.reddit.com{permalink}" if permalink else raw_url,
            author=f"u/{post.get('author', 'unknown')}",
            body=(post.get("selftext") or "")[:2000],
            created_at=created,
            engagement=int(post.get("score", 0)),
            comments=int(post.get("num_comments", 0)),
            outbound_urls=outbound,
        )
