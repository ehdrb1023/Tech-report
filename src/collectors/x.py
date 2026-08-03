"""X(Twitter) 수집기 — 지정한 인물들의 타임라인만 읽는다.

경로가 두 개다.

1) 공식 API (X_BEARER_TOKEN 이 있을 때)  ← 권장
   GET /2/users/:id/tweets 로 계정별 타임라인을 읽는다. **검색이 아니다.**
   키워드는 서버로 넘기지 않는다 — 넘길 수 있는 건 계정·시간창·제외옵션뿐이고,
   비용은 "계정 수 × 그 사람의 포스팅 량"으로 결정된다.
   종량제(읽기 1건당 과금)이므로 받는 양 자체를 줄이는 게 곧 비용 절감이다:
     - start_time 으로 24시간 창을 서버에 넘김
     - exclude=retweets,replies
     - max_results 로 계정당 상한
   좋아요·리트윗 수(public_metrics)를 받아오므로 스코어링 정확도도 올라간다.

2) Nitter/RSSHub 미러 RSS (토큰이 없을 때)
   무료지만 서드파티 미러라 가용성이 보장되지 않고, 인게이지먼트 수치가 없다.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser

from ..models import Item
from .base import Collector

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
RT_RE = re.compile(r"^RT by @[\w]+:", re.IGNORECASE)
STATUS_RE = re.compile(r"/status/(\d+)")

# 일부 미러는 200 OK 로 "안내문 한 줄"짜리 가짜 피드를 돌려준다.
# (예: xcancel 의 "RSS reader not yet whitelisted!", 발행일 1971-01-01)
# 이걸 성공으로 착각하면 멀쩡한 다음 미러를 시도하지 않게 되므로 걸러낸다.
PLACEHOLDER_RE = re.compile(
    r"not yet whitelisted|rate limited|instance has been|error|blocked",
    re.IGNORECASE,
)
MIN_VALID_YEAR = 2006  # 트위터 서비스 개시 연도

API_BASE = "https://api.x.com/2"
MIN_TEXT_LEN = 20  # 단답/이모지 트윗은 리포트 가치가 낮음


class XCollector(Collector):
    name = "x"

    def __init__(self, config) -> None:
        super().__init__(config)
        # --- Nitter 폴백 상태 ---
        self._preferred_template: str | None = None
        self._dead_templates: set[str] = set()
        self._working_mirror_hits = 0
        # --- 공식 API 상태 ---
        self.bearer_token = config.x_bearer_token
        self._posts_read = 0  # 종량제 비용 추적용

    # ------------------------------------------------------------------
    def _collect(self) -> list[Item]:
        cfg = self.config.x
        if not cfg.get("enabled", True):
            return []

        accounts: list[str] = cfg.get("accounts", [])
        if not accounts:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.lookback_hours
        )

        if self.bearer_token:
            return self._collect_official(accounts, cutoff)
        log.info("[x] X_BEARER_TOKEN 없음 → Nitter 미러 사용 (무료, 가용성 보장 안 됨)")
        return self._collect_nitter(accounts, cutoff)

    # ==================================================================
    # 1) 공식 API
    # ==================================================================
    def _collect_official(self, accounts: list[str], cutoff: datetime) -> list[Item]:
        cfg = self.config.x
        self.session.headers["Authorization"] = f"Bearer {self.bearer_token}"

        user_ids = self._resolve_user_ids(accounts)
        if not user_ids:
            log.error("[x] 계정 ID 조회 실패 — 토큰/권한을 확인하세요")
            return []

        max_per_account = max(5, min(int(cfg.get("max_results_per_account", 20)), 100))
        items: list[Item] = []

        for username, user_id in user_ids.items():
            items.extend(
                self._fetch_timeline(username, user_id, cutoff, max_per_account)
            )

        cost = self._posts_read * 0.005
        log.info(
            "[x] 공식 API — 계정 %d개에서 읽기 %d건 (예상 비용 약 $%.2f), 필터 후 %d건",
            len(user_ids),
            self._posts_read,
            cost,
            len(items),
        )
        return items

    def _resolve_user_ids(self, accounts: list[str]) -> dict[str, str]:
        """username → user_id. 조회 결과는 캐시해서 매일 다시 부르지 않는다."""
        cache_path = Path(self.config.state_dir) / "x_user_ids.json"
        cache: dict[str, str] = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                cache = {}

        missing = [a for a in accounts if a not in cache]
        for start in range(0, len(missing), 100):  # 엔드포인트 상한 100
            batch = missing[start : start + 100]
            resp = self.get(
                f"{API_BASE}/users/by",
                params={"usernames": ",".join(batch)},
                timeout=20,
            )
            if resp is None:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            for user in payload.get("data", []):
                cache[user["username"]] = user["id"]
            for err in payload.get("errors", []):
                log.warning(
                    "[x] 계정 조회 실패: %s (%s)",
                    err.get("value", "?"),
                    err.get("title", ""),
                )

        if cache:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except OSError as exc:
                log.debug("[x] 계정 ID 캐시 저장 실패: %s", exc)

        return {a: cache[a] for a in accounts if a in cache}

    def _fetch_timeline(
        self, username: str, user_id: str, cutoff: datetime, max_results: int
    ) -> list[Item]:
        resp = self.get(
            f"{API_BASE}/users/{user_id}/tweets",
            params={
                # 시간창을 서버로 넘겨 받는 양(=비용)을 줄인다
                "start_time": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "exclude": "retweets,replies",
                "max_results": max_results,
                "tweet.fields": "created_at,public_metrics,entities",
            },
            timeout=25,
        )
        if resp is None:
            return []
        try:
            payload = resp.json()
        except ValueError:
            log.warning("[x] @%s 응답 파싱 실패", username)
            return []

        tweets = payload.get("data", []) or []
        self._posts_read += len(tweets)

        items: list[Item] = []
        for tweet in tweets:
            text = WS_RE.sub(" ", tweet.get("text", "")).strip()
            if len(text) < MIN_TEXT_LEN:
                continue

            created = self._parse_iso(tweet.get("created_at"))
            if created is None or created < cutoff:
                continue

            metrics = tweet.get("public_metrics", {}) or {}
            # 본문의 t.co 단축 링크로는 arXiv 를 못 잡는다 — 펼친 URL 을 써야 한다
            outbound = [
                url.get("expanded_url", "")
                for url in (tweet.get("entities", {}) or {}).get("urls", [])
                if url.get("expanded_url")
            ]

            items.append(
                Item(
                    source="x",
                    origin=f"@{username}",
                    uid=f"x:{tweet['id']}",
                    title=text[:180],
                    url=f"https://x.com/{username}/status/{tweet['id']}",
                    author=f"@{username}",
                    body=text[:2000],
                    created_at=created,
                    engagement=int(metrics.get("like_count", 0)),
                    comments=int(metrics.get("retweet_count", 0))
                    + int(metrics.get("reply_count", 0)),
                    outbound_urls=outbound,
                )
            )
        return items

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    # ==================================================================
    # 2) Nitter/RSSHub 폴백
    # ==================================================================
    def _collect_nitter(self, accounts: list[str], cutoff: datetime) -> list[Item]:
        items: list[Item] = []
        # 미러에 과부하를 주지 않도록 동시성은 낮게 유지
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._fetch_account, acct, cutoff): acct
                for acct in accounts
            }
            for future in as_completed(futures):
                acct = futures[future]
                try:
                    items.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    log.warning("[x] @%s 처리 실패: %s", acct, exc)

        with_items = len({i.origin for i in items})
        log.info(
            "[x] 미러 응답 %d/%d 계정, 최근 글 있는 계정 %d개 (미러: %s)",
            self._working_mirror_hits,
            len(accounts),
            with_items,
            self._preferred_template or "없음",
        )
        if self._working_mirror_hits == 0 and accounts:
            log.warning(
                "[x] 모든 미러 접근 실패. config.yaml 의 x.rss_templates 를 "
                "살아 있는 인스턴스로 갱신하세요 (https://status.d420.de/ 참고)."
            )
        return items

    # ------------------------------------------------------------------
    def _templates(self) -> list[str]:
        templates: list[str] = list(self.config.x.get("rss_templates", []))
        live = [t for t in templates if t not in self._dead_templates]
        if self._preferred_template and self._preferred_template in live:
            live.remove(self._preferred_template)
            live.insert(0, self._preferred_template)
        return live or templates

    def _fetch_account(self, username: str, cutoff: datetime) -> list[Item]:
        cfg = self.config.x
        timeout = int(cfg.get("request_timeout", 20))
        max_tries = int(cfg.get("max_mirror_tries", 3))

        for template in self._templates()[:max_tries]:
            url = template.format(username=username)
            resp = self.get(url, timeout=timeout, retries=0)
            if resp is None:
                self._dead_templates.add(template)
                continue

            feed = feedparser.parse(resp.content)
            if not feed.entries:
                continue
            if self._is_placeholder(feed.entries):
                log.debug("[x] %s 는 안내문 피드를 반환 → 미러 제외", template)
                self._dead_templates.add(template)
                continue

            self._preferred_template = template
            self._dead_templates.discard(template)
            self._working_mirror_hits += 1
            # 미러는 살아 있다. 최근 글이 없어 빈 리스트가 나올 수도 있다(정상).
            return self._parse_entries(feed.entries, username, cutoff)

        log.debug("[x] @%s: 사용 가능한 미러 없음", username)
        return []

    # ------------------------------------------------------------------
    @staticmethod
    def _is_placeholder(entries) -> bool:
        """미러가 돌려준 게 실제 타임라인이 아니라 안내문인지 판별."""
        for entry in entries:
            if PLACEHOLDER_RE.search(entry.get("title", "") or ""):
                return True
        years = [
            parsed.tm_year
            for parsed in (
                entry.get("published_parsed") or entry.get("updated_parsed")
                for entry in entries
            )
            if parsed
        ]
        return bool(years) and all(year < MIN_VALID_YEAR for year in years)

    def _parse_entries(self, entries, username: str, cutoff: datetime) -> list[Item]:
        items: list[Item] = []
        for entry in entries:
            published = self._parse_time(entry)
            if published is None or published < cutoff:
                continue

            raw_title = entry.get("title", "") or ""
            if RT_RE.match(raw_title):  # 리트윗은 본인 발화가 아님
                continue

            raw_summary = entry.get("summary") or entry.get("description") or raw_title
            text = self._clean(raw_summary)
            if len(text) < MIN_TEXT_LEN:
                continue

            link = self._to_x_url(entry.get("link", ""), username)
            match = STATUS_RE.search(link)
            uid = f"x:{match.group(1)}" if match else f"x:{username}:{text[:40]}"

            items.append(
                Item(
                    source="x",
                    origin=f"@{username}",
                    uid=uid,
                    title=WS_RE.sub(" ", text)[:180],
                    url=link,
                    author=f"@{username}",
                    body=text[:2000],
                    created_at=published,
                    engagement=0,  # RSS 에는 좋아요/리트윗 수가 없음
                    # Nitter 는 URL 을 펼쳐서 주므로 본문 HTML 에서 링크를 뽑는다
                    outbound_urls=self._extract_links(raw_summary),
                )
            )
        return items

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_links(raw_html: str) -> list[str]:
        return re.findall(r'href="(https?://[^"]+)"', raw_html or "")

    @staticmethod
    def _clean(raw: str) -> str:
        text = TAG_RE.sub(" ", raw or "")
        text = html.unescape(text)
        return WS_RE.sub(" ", text).strip()

    @staticmethod
    def _to_x_url(link: str, username: str) -> str:
        """미러 URL 을 원본 x.com 링크로 되돌린다."""
        if not link:
            return f"https://x.com/{username}"
        match = STATUS_RE.search(link)
        if match:
            return f"https://x.com/{username}/status/{match.group(1)}"
        host = urlparse(link).netloc
        if host and "x.com" not in host and "twitter.com" not in host:
            return f"https://x.com/{username}"
        return link

    @staticmethod
    def _parse_time(entry) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
