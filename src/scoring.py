"""키워드 필터링 + 스코어링 + 선별.

최종점수 = 키워드가중치합 + 인기도점수 + 최신성점수 + 소스가중치 + 논문가산점

arXiv 가 소스에서 빠졌으므로 "논문"은 이제 별도 소스가 아니라 Reddit/X 글의
속성이다. 논문 링크가 걸린 글에 가산점을 주고, 같은 논문을 여러 사람이 언급하면
하나로 합치면서 점수를 올린다(여러 명이 얘기 = 더 중요하다는 신호).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from .config import Config
from .models import Item

log = logging.getLogger(__name__)


class Scorer:
    def __init__(self, config: Config) -> None:
        self.config = config
        cfg = config.scoring
        self.source_weight: dict[str, float] = cfg.get("source_weight", {})
        self.engagement_cap = float(cfg.get("engagement_cap", 4.0))
        self.recency_weight = float(cfg.get("recency_weight", 2.0))
        self.paper_bonus = float(cfg.get("paper_bonus", 2.5))
        self.cross_mention_bonus = float(cfg.get("cross_mention_bonus", 1.5))

        self.min_keyword_score = float(
            config.collect.get("min_keyword_score", 0.0)
        )
        self.max_keyword_terms = int(cfg.get("max_keyword_terms", 5))

        keywords = cfg.get("keywords", {})
        # [(컴파일된 정규식, 원본 표현, 정규화형, 가중치), ...]
        # 정규화형이 같은 표기 변형(multi-agent / multi agent)은 한 번만 등록한다.
        self.tiers: list[tuple[re.Pattern[str], str, str, float]] = []
        seen_canon: set[str] = set()
        for tier_name in ("high", "medium", "low"):
            tier = keywords.get(tier_name, {}) or {}
            weight = float(tier.get("weight", 1.0))
            for term in tier.get("terms", []):
                canon = self._canonical(term)
                if canon in seen_canon:
                    log.debug("[scoring] 표기 중복 키워드 무시: %s", term)
                    continue
                seen_canon.add(canon)
                self.tiers.append((self._compile(term), term, canon, weight))

        self.blocked = [
            self._compile(term) for term in keywords.get("blocked", [])
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(term: str) -> str:
        """'multi-agent' 와 'multi agent' 를 같은 것으로 취급하기 위한 정규화."""
        return re.sub(r"[\s\-_]+", " ", term.lower().strip())

    @staticmethod
    def _compile(term: str) -> re.Pattern[str]:
        """단어 경계를 지킨 매칭. 'api'가 'rapid'에 걸리는 것을 막는다."""
        escaped = re.escape(term.lower()).replace(r"\ ", r"[\s\-_]+")
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")

    @staticmethod
    def _drop_subsumed(
        matched: list[tuple[str, str, float]]
    ) -> list[tuple[str, str, float]]:
        """더 구체적인 키워드에 포함되는 일반 키워드는 뺀다.

        'claude code' 가 걸렸으면 'claude' 는 같은 글을 두 번 세는 것이므로 제외.
        """
        kept = []
        for term, canon, weight in matched:
            covered = any(
                other_canon != canon
                and re.search(rf"(?<![a-z0-9]){re.escape(canon)}(?![a-z0-9])", other_canon)
                for _, other_canon, _ in matched
            )
            if not covered:
                kept.append((term, canon, weight))
        return kept

    # ------------------------------------------------------------------
    def score_item(self, item: Item) -> float:
        text = item.searchable_text()

        # 차단 키워드는 즉시 탈락
        for pattern in self.blocked:
            if pattern.search(text):
                item.score = -1.0
                return -1.0

        hits = [
            (term, canon, weight)
            for pattern, term, canon, weight in self.tiers
            if pattern.search(text)
        ]
        # 'claude code' 가 걸렸으면 'claude' 는 중복이므로 뺀다
        hits = self._drop_subsumed(hits)
        # 키워드 스팸(모든 단어 나열) 방지: 가중치 상위 N개만 인정
        hits.sort(key=lambda h: h[2], reverse=True)
        counted = hits[: self.max_keyword_terms]

        keyword_score = sum(weight for _, _, weight in counted)
        matched = [term for term, _, _ in counted]

        # "키워드가 충분히 나왔는가" 자체를 관문으로 쓴다.
        # (총점만으로는 최신성·소스 가중치만으로도 통과해버려서 내용 필터가 안 된다)
        if keyword_score < self.min_keyword_score:
            item.matched_keywords = matched
            item.score = -1.0
            return -1.0

        total = keyword_score
        total += self._engagement_score(item)
        total += self._recency_score(item)
        total += float(self.source_weight.get(item.source, 1.0))
        # 논문을 링크한 글은 대체로 정보 밀도가 높다
        if item.arxiv_ids():
            total += self.paper_bonus
        # 여러 사람이 같은 걸 얘기하면 더 중요하다는 신호
        total += self.cross_mention_bonus * len(item.also_from)

        item.matched_keywords = matched
        item.score = round(total, 3)
        return item.score

    def _engagement_score(self, item: Item) -> float:
        """업보트/댓글을 로그 스케일로 압축. 대형 서브레딧 편향을 줄인다."""
        raw = item.engagement + 2 * item.comments
        if raw <= 0:
            return 0.0
        return min(math.log10(raw + 1) * 1.5, self.engagement_cap)

    def _recency_score(self, item: Item) -> float:
        lookback = max(self.config.lookback_hours, 1)
        freshness = 1.0 - min(item.age_hours / lookback, 1.0)
        return round(self.recency_weight * freshness, 3)

    # ------------------------------------------------------------------
    def select(self, items: list[Item]) -> list[Item]:
        """중복 제거 → 논문 병합 → 스코어링 → 문턱 통과분 전부.

        개수 상한(max_items / per_source_cap)은 기본적으로 없다. 24시간 안에
        나오는 유용한 글은 그리 많지 않고, 산수로 미리 자르는 것보다 문턱을 넘은
        걸 모두 Claude 에게 보여주고 고르게 하는 편이 판단 품질이 낫기 때문이다.
        (0 또는 미설정 = 무제한. 폭주 방지가 필요하면 config 에서 값을 주면 된다.)
        """
        deduped = self._dedupe(items)
        merged = self._merge_by_paper(deduped)

        min_score = float(self.config.collect.get("min_score", 0.0))
        max_items = int(self.config.collect.get("max_items", 0) or 0)
        per_source_cap = int(self.config.collect.get("per_source_cap", 0) or 0)

        scored = [i for i in merged if self.score_item(i) >= min_score]
        scored.sort(key=lambda i: i.score, reverse=True)
        selected = scored

        if per_source_cap:
            counts: Counter[str] = Counter()
            kept: list[Item] = []
            for item in selected:
                if counts[item.source] < per_source_cap:
                    counts[item.source] += 1
                    kept.append(item)
            selected = kept
        if max_items:
            selected = selected[:max_items]

        papers = sum(1 for i in selected if i.arxiv_ids())
        dropped_kw = sum(
            1 for i in merged if i.score < 0 and not self._is_blocked(i)
        )
        log.info(
            "수집 %d건 → 중복제거 %d건 → 논문병합 %d건 → 키워드미달 %d건 제외 "
            "→ 선정 %d건 (논문 링크 포함 %d건)",
            len(items),
            len(deduped),
            len(merged),
            dropped_kw,
            len(selected),
            papers,
        )
        if max_items and len(scored) > max_items:
            log.info(
                "[scoring] 문턱은 넘었지만 max_items 상한에 밀린 %d건이 있습니다",
                len(scored) - max_items,
            )
        return selected

    def _is_blocked(self, item: Item) -> bool:
        text = item.searchable_text()
        return any(pattern.search(text) for pattern in self.blocked)

    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe(items: list[Item]) -> list[Item]:
        """uid 중복 + 제목이 사실상 같은 항목(크로스포스트) 제거."""
        seen_uid: set[str] = set()
        seen_title: set[str] = set()
        out: list[Item] = []
        for item in items:
            if item.uid in seen_uid:
                continue
            norm = re.sub(r"[^a-z0-9]+", "", item.title.lower())[:60]
            if norm and norm in seen_title:
                continue
            seen_uid.add(item.uid)
            if norm:
                seen_title.add(norm)
            out.append(item)
        return out

    @staticmethod
    def _merge_by_paper(items: list[Item]) -> list[Item]:
        """같은 논문을 가리키는 글들을 하나로 합친다.

        가장 인게이지먼트가 높은 글을 대표로 남기고, 나머지 출처는 also_from 에
        모아 "여러 명이 언급했다"는 신호로 쓴다.
        """
        by_paper: dict[str, list[Item]] = {}
        standalone: list[Item] = []

        for item in items:
            ids = item.arxiv_ids()
            if ids:
                by_paper.setdefault(ids[0], []).append(item)
            else:
                standalone.append(item)

        merged: list[Item] = []
        for group in by_paper.values():
            if len(group) == 1:
                merged.append(group[0])
                continue
            group.sort(key=lambda i: i.engagement + 2 * i.comments, reverse=True)
            primary, rest = group[0], group[1:]
            primary.also_from = [i.origin for i in rest]
            # 합쳐진 글들의 인게이지먼트도 대표에 반영
            primary.engagement += sum(i.engagement for i in rest)
            primary.comments += sum(i.comments for i in rest)
            merged.append(primary)

        return merged + standalone
