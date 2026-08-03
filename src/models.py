"""파이프라인 전체에서 공유하는 데이터 모델."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# arXiv 링크/ID 추출용. 사람들이 논문을 얘기할 땐 대부분 링크를 붙이므로
# "제목 언급" 추측은 하지 않고 링크만 신뢰한다(오탐이 너무 많음).
ARXIV_PATTERNS = (
    re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})", re.I),
    re.compile(r"huggingface\.co/papers/(\d{4}\.\d{4,5})", re.I),
    re.compile(r"alphaxiv\.org/(?:abs|overview)/(\d{4}\.\d{4,5})", re.I),
    re.compile(r"\barxiv[:\s]\s*(\d{4}\.\d{4,5})", re.I),
)


def extract_arxiv_ids(*texts: str) -> list[str]:
    """주어진 텍스트들에서 arXiv ID를 중복 없이 추출 (버전 접미사 제거)."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for pattern in ARXIV_PATTERNS:
            for match in pattern.findall(text):
                arxiv_id = match.split("v")[0]
                if arxiv_id not in found:
                    found.append(arxiv_id)
    return found


@dataclass
class Paper:
    """arXiv에서 보강해 온 논문 정보."""

    arxiv_id: str
    title: str = ""
    authors: str = ""
    abstract: str = ""
    category: str = ""
    published: datetime | None = None

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def resolved(self) -> bool:
        """arXiv 조회가 성공해 실제 메타데이터가 채워졌는지."""
        return bool(self.title)

    def to_prompt_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract[:1200],
            "category": self.category,
            "url": self.url,
        }


@dataclass
class Item:
    """수집된 게시물 하나. 모든 수집기가 이 형태로 정규화해서 돌려준다."""

    source: str           # "reddit" | "x"
    origin: str           # 세부 출처. 예: "r/LocalLLaMA", "@karpathy"
    uid: str              # 소스 내 고유 ID (중복 제거용)
    title: str
    url: str
    author: str = ""
    body: str = ""        # 본문 (없으면 빈 문자열)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    engagement: int = 0   # 업보트, 좋아요 등
    comments: int = 0     # 댓글, 리트윗 등
    # 글에 걸린 바깥 링크 (Reddit 링크 포스트의 대상, X 의 expanded_url 등).
    # 본문에 안 나타나는 링크가 여기 들어오므로 논문 탐지에 꼭 필요하다.
    outbound_urls: list[str] = field(default_factory=list)

    # 스코어링 단계에서 채워짐
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    # 이 글이 언급한 논문 (보강 단계에서 메타데이터가 채워짐)
    papers: list[Paper] = field(default_factory=list)
    # 같은 논문/주제를 언급한 다른 글의 출처 (중복 병합 시 누적)
    also_from: list[str] = field(default_factory=list)

    @property
    def age_hours(self) -> float:
        delta = datetime.now(timezone.utc) - self.created_at
        return delta.total_seconds() / 3600.0

    def searchable_text(self) -> str:
        """키워드 매칭 대상 텍스트 (소문자)."""
        return f"{self.title}\n{self.body}".lower()

    def arxiv_ids(self) -> list[str]:
        """이 글이 가리키는 arXiv 논문 ID."""
        return extract_arxiv_ids(self.url, self.body, self.title, *self.outbound_urls)

    def to_prompt_dict(self) -> dict:
        """Claude 프롬프트에 넣을 최소 형태."""
        data = {
            "source": self.source,
            "origin": self.origin,
            "title": self.title.strip(),
            "url": self.url,
            "author": self.author,
            "excerpt": self.body.strip()[:800],
            "engagement": self.engagement,
            "comments": self.comments,
            "hours_ago": round(self.age_hours, 1),
            "score": round(self.score, 2),
            "keywords": self.matched_keywords[:8],
        }
        if self.also_from:
            data["also_discussed_by"] = self.also_from[:5]
        resolved = [p.to_prompt_dict() for p in self.papers if p.resolved]
        if resolved:
            data["papers"] = resolved
        return data


@dataclass
class ReportSection:
    """리포트의 한 섹션 (예: 'AI 에이전트', '화제의 논문')."""

    title: str
    items: list[dict] = field(default_factory=list)


@dataclass
class Report:
    """Claude가 생성한 최종 리포트."""

    headline: str
    tldr: list[str] = field(default_factory=list)
    sections: list[ReportSection] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    closing: str = ""

    # 메타데이터
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    total_collected: int = 0
    total_used: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    papers_found: int = 0
    skipped_as_seen: int = 0
