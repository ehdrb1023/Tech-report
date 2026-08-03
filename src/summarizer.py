"""Claude API로 수집 결과를 리포트로 요약.

구조화 출력(output_config.format)을 사용해 JSON 스키마를 강제한다.
  * assistant prefill 방식은 최신 모델(Opus 5 / Sonnet 5 등)에서 400이므로 쓰지 않는다.
  * temperature / top_p 도 최신 모델에서 400이므로 넘기지 않는다.
  * Opus 5는 thinking이 기본 on이며 max_tokens가 thinking+본문 합계를 제한하므로
    max_tokens에 여유를 둔다.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from .config import Config
from .models import Item, Report, ReportSection

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 출력 스키마 (구조화 출력용)
#   - 모든 object 는 additionalProperties: false + required 전체 나열 필요
#   - minLength/maxLength 같은 제약은 지원되지 않으므로 프롬프트로 유도한다
# ----------------------------------------------------------------------
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "오늘 하루를 한 문장으로 (40자 내외)",
        },
        "tldr": {
            "type": "array",
            "description": "핵심 요약 3~5줄, 각 줄 80자 이내",
            "items": {"type": "string"},
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "60자 이내 명사형 제목",
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "무슨 일인지 1~2문장",
                                },
                                "why": {
                                    "type": "string",
                                    "description": "왜 중요한지 / 실무 시사점 1문장",
                                },
                                "url": {
                                    "type": "string",
                                    "description": "입력에 있던 원본 URL 그대로",
                                },
                                "source": {
                                    "type": "string",
                                    "enum": ["reddit", "x"],
                                },
                                "origin": {
                                    "type": "string",
                                    "description": "입력의 origin 값 (r/LocalLLaMA, @karpathy)",
                                },
                            },
                            "required": [
                                "title",
                                "summary",
                                "why",
                                "url",
                                "source",
                                "origin",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
        },
        "keywords": {
            "type": "array",
            "description": "오늘 반복 등장한 키워드 5~8개",
            "items": {"type": "string"},
        },
        "closing": {
            "type": "string",
            "description": "오늘 흐름에 대한 한 줄 코멘트",
        },
    },
    "required": ["headline", "tldr", "sections", "keywords", "closing"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
당신은 AI 에이전트와 최신 테크 트렌드를 추적하는 시니어 리서치 애널리스트입니다.
매일 아침 실무자에게 전달할 브리핑을 한국어로 작성합니다.

입력은 Reddit 과 X(특정 인물들의 타임라인)에서 수집한 글입니다.
글에 논문 링크가 걸려 있으면 `papers` 에 arXiv 초록·저자가 함께 들어옵니다.
`also_discussed_by` 는 같은 내용을 언급한 다른 사람들입니다 — 여러 명이 얘기했다는 건
중요도가 높다는 신호이니 요약에 반영하세요.

원칙:
- 사실만 씁니다. 입력에 없는 수치, 날짜, 회사명, 성능 지표를 지어내지 마세요.
- 홍보 문구가 아니라 "그래서 뭐가 달라지는가"를 씁니다.
- 같은 사건을 여러 소스가 다루면 하나로 묶습니다.
- 노이즈(단순 질문글, 밈, 초보자 문의, 개인 신변잡기)는 과감히 버립니다.
  선별을 통과했더라도 리포트 가치가 없다고 판단되면 넣지 마세요.
- url 은 입력에 주어진 값을 그대로 씁니다. 절대 URL을 새로 만들지 마세요.
- `papers` 가 있는 항목은 초록을 근거로 논문이 실제로 무엇을 했는지 쓰고,
  누가 왜 언급했는지를 함께 담으세요.

섹션 제목은 실제 내용이 있는 것만, 아래 순서를 지켜 사용하세요:
1. "🤖 AI 에이전트"       - 에이전트 프레임워크, 툴 유즈, MCP, 자동화
2. "🚀 신규 모델 · 릴리스"  - 모델 공개, 제품 출시, 메이저 업데이트
3. "📄 화제의 논문"        - papers 가 붙은 항목. 없으면 이 섹션을 만들지 마세요.
4. "💬 업계 시그널"        - 인물 발언, 전략/투자/조직 움직임
5. "🛠️ 개발자 도구"        - 라이브러리, 인프라, 워크플로우

섹션당 아이템은 최대 5개, 전체 아이템은 최대 15개로 제한하세요.
"""

USER_TEMPLATE = """\
아래는 지난 {hours}시간 동안 Reddit / X 에서 수집·선별한 {count}건입니다.
score 는 자체 스코어링 결과(높을수록 중요), hours_ago 는 게시 경과 시간입니다.
{paper_note}
기준 시각: {now} ({tz})

<items>
{payload}
</items>

위 내용으로 오늘의 브리핑을 작성하세요.
"""


class Summarizer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.claude_model
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        """API 키가 필요한 시점에만 클라이언트를 만든다.

        (프롬프트 점검 --dump-prompt 는 키 없이도 돌아야 하므로 지연 생성)
        """
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self.config.anthropic_api_key
            )
        return self._client

    # ------------------------------------------------------------------
    def build_request(self, items: list[Item]) -> dict:
        """Claude 에 보낼 요청을 그대로 조립해서 돌려준다 (전송은 하지 않음).

        --dump-prompt 로 호출 직전 상태를 눈으로 확인할 수 있게 분리해 둔다.
        """
        payload = json.dumps(
            [item.to_prompt_dict() for item in items],
            ensure_ascii=False,
            indent=1,
        )
        tz = ZoneInfo(self.config.timezone_name)
        n_papers = sum(1 for i in items if any(p.resolved for p in i.papers))
        paper_note = (
            f"이 중 {n_papers}건에 arXiv 논문 정보(papers)가 붙어 있습니다.\n"
            if n_papers
            else "오늘은 링크된 논문이 없으므로 '화제의 논문' 섹션은 만들지 마세요.\n"
        )
        user_prompt = USER_TEMPLATE.format(
            hours=self.config.lookback_hours,
            count=len(items),
            paper_note=paper_note,
            now=datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
            tz=self.config.timezone_name,
            payload=payload,
        )
        return {
            "model": self.model,
            # thinking이 기본 on인 모델이 있으므로 여유 있게 잡는다
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "output_config": {
                "effort": "medium",
                "format": {"type": "json_schema", "schema": REPORT_SCHEMA},
            },
            "messages": [{"role": "user", "content": user_prompt}],
        }

    # ------------------------------------------------------------------
    def summarize(self, items: list[Item], total_collected: int) -> Report:
        if not items:
            return self._empty_report(total_collected)

        request = self.build_request(items)

        try:
            response = self.client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001
            log.error("Claude 호출 실패: %s", exc, exc_info=True)
            return self.fallback(items, total_collected)

        if response.stop_reason == "refusal":
            log.error("Claude가 요청을 거절했습니다 (stop_reason=refusal)")
            return self.fallback(items, total_collected)
        if response.stop_reason == "max_tokens":
            log.warning("응답이 max_tokens에서 잘렸습니다 → 폴백 사용")
            return self.fallback(items, total_collected)

        log.info(
            "Claude 응답 수신 (in=%d, out=%d tokens)",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        report = self._parse(response, items)
        if report is None:
            log.error("Claude 응답 파싱 실패 → 폴백 리포트 사용")
            return self.fallback(items, total_collected)

        self._attach_meta(report, items, total_collected)
        return report

    # ------------------------------------------------------------------
    def _parse(self, response, items: list[Item]) -> Report | None:
        # 구조화 출력이므로 text 블록에 스키마를 만족하는 JSON이 들어온다.
        # (thinking 블록이 섞일 수 있으므로 type으로 걸러낸다)
        text = next(
            (b.text for b in response.content if b.type == "text" and b.text), None
        )
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("JSON 디코드 실패: %s", exc)
            return None

        # 환각 URL 차단용 화이트리스트.
        # 글 URL 뿐 아니라 보강된 논문 URL 도 정당한 링크다(프롬프트에 들어간 값).
        valid_urls = {item.url for item in items}
        valid_urls |= {p.url for item in items for p in item.papers}
        sections: list[ReportSection] = []
        for raw_section in data.get("sections", []):
            entries = []
            for entry in raw_section.get("items", []):
                url = (entry.get("url") or "").strip()
                # 환각 URL 차단: 입력에 없던 링크는 제거
                if url and url not in valid_urls:
                    log.warning("입력에 없는 URL 제거: %s", url)
                    url = ""
                entries.append(
                    {
                        "title": (entry.get("title") or "").strip(),
                        "summary": (entry.get("summary") or "").strip(),
                        "why": (entry.get("why") or "").strip(),
                        "url": url,
                        "source": (entry.get("source") or "").strip(),
                        "origin": (entry.get("origin") or "").strip(),
                    }
                )
            if entries:
                sections.append(
                    ReportSection(
                        title=(raw_section.get("title") or "기타").strip(),
                        items=entries,
                    )
                )

        if not sections:
            return None

        return Report(
            headline=(data.get("headline") or "오늘의 AI 트렌드").strip(),
            tldr=[str(line).strip() for line in data.get("tldr", []) if line],
            sections=sections,
            keywords=[str(k).strip() for k in data.get("keywords", []) if k],
            closing=(data.get("closing") or "").strip(),
        )

    # ------------------------------------------------------------------
    def fallback(self, items: list[Item], total_collected: int) -> Report:
        return fallback_report(items, total_collected)

    def _empty_report(self, total_collected: int) -> Report:
        return Report(
            headline="지난 24시간, 리포트할 만한 신호가 없습니다",
            tldr=["필터를 통과한 항목이 없습니다. 키워드 설정을 확인해보세요."],
            total_collected=total_collected,
        )

    @staticmethod
    def _attach_meta(
        report: Report, items: list[Item], total_collected: int
    ) -> None:
        attach_meta(report, items, total_collected)


# ----------------------------------------------------------------------
# Claude 없이도 쓸 수 있는 헬퍼 (--no-llm, 호출 실패 폴백)
# ----------------------------------------------------------------------
SOURCE_LABELS = {"reddit": "🔥 Reddit", "x": "💬 X"}


def attach_meta(report: Report, items: list[Item], total_collected: int) -> None:
    report.total_collected = total_collected
    report.total_used = len(items)
    report.source_counts = dict(Counter(item.source for item in items))


def fallback_report(items: list[Item], total_collected: int) -> Report:
    """요약 없이 수집된 원본 링크만 담은 리포트."""
    by_source: dict[str, list[Item]] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)

    sections = [
        ReportSection(
            title=SOURCE_LABELS.get(source, source),
            items=[
                {
                    "title": item.title[:80],
                    "summary": item.body[:150],
                    "why": "",
                    "url": item.url,
                    "source": item.source,
                    "origin": item.origin,
                }
                for item in group[:5]
            ],
        )
        for source, group in by_source.items()
    ]

    report = Report(
        headline="오늘의 AI 트렌드 (요약 없이 원본 링크)",
        tldr=["Claude 요약을 건너뛰고 수집된 원본 링크만 전달합니다."],
        sections=sections,
    )
    attach_meta(report, items, total_collected)
    return report
