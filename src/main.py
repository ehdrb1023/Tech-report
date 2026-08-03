"""파이프라인 진입점.

  수집(Reddit·X) → 중복보도 제외 → 필터링/스코어링 → arXiv 보강 → Claude 요약 → Discord

arXiv 는 수집 소스가 아니라 보강 단계다. 선별을 통과한 글이 링크한 논문만
조회하므로, 아무도 안 읽은 논문 초록이 리포트를 채우지 않는다.

사용:
  python -m src.main                  # 전체 실행 후 Discord 전송
  python -m src.main --dry-run        # 전송 없이 콘솔에 리포트 출력
  python -m src.main --no-llm         # Claude 없이 링크 목록만 (수집기 점검용)
  python -m src.main --hours 48       # 기간 변경
  python -m src.main --source reddit  # 특정 소스만
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .collectors import RedditCollector, XCollector
from .config import Config, ConfigError
from .discord_sender import DiscordSender
from .enrichment import ArxivEnricher
from .models import Item, Report
from .scoring import Scorer
from .state import SeenStore
from .summarizer import Summarizer, fallback_report

log = logging.getLogger("aitrend")

COLLECTORS = {
    "reddit": RedditCollector,
    "x": XCollector,
}


def setup_logging(verbose: bool = False) -> None:
    # Windows 기본 콘솔(cp949)에서 이모지/한글 출력이 UnicodeEncodeError 로 죽는 것을 방지
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI 트렌드 리포트를 만들어 Discord로 보냅니다."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Discord로 보내지 않고 콘솔에 출력"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Claude 요약 없이 원본 링크 목록만 생성"
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="중복 보도 기록을 무시하고 읽지도 쓰지도 않음 (테스트용)",
    )
    parser.add_argument(
        "--dump-prompt",
        nargs="?",
        const="-",
        metavar="PATH",
        help="Claude 에 보낼 요청을 출력만 하고 호출하지 않음 "
        "(API 키 불필요. 경로를 주면 그 파일로 저장)",
    )
    parser.add_argument("--hours", type=int, help="수집 기간(시간). 기본 24")
    parser.add_argument("--max-items", type=int, help="리포트 최대 아이템 수")
    parser.add_argument(
        "--source",
        action="append",
        choices=list(COLLECTORS),
        help="사용할 소스 (반복 지정 가능). 기본: 전체",
    )
    parser.add_argument("--config", help="config.yaml 경로")
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")
    return parser.parse_args()


# ----------------------------------------------------------------------
def collect_all(config: Config, sources: list[str]) -> list[Item]:
    """활성화된 수집기를 병렬로 실행."""
    collectors = [COLLECTORS[name](config) for name in sources]
    items: list[Item] = []
    with ThreadPoolExecutor(max_workers=len(collectors)) as pool:
        for result in pool.map(lambda c: c.collect(), collectors):
            items.extend(result)
    return items


def build_report(
    config: Config, items: list[Item], total_collected: int, use_llm: bool
) -> Report:
    if not use_llm:
        return fallback_report(items, total_collected)
    return Summarizer(config).summarize(items, total_collected)


def print_report(report: Report) -> None:
    """--dry-run 용 콘솔 출력."""
    print("\n" + "=" * 70)
    print(f"  {report.headline}")
    print("=" * 70)
    for line in report.tldr:
        print(f"  • {line}")
    if report.keywords:
        print("\n  🏷️  " + " · ".join(report.keywords))
    for section in report.sections:
        print(f"\n── {section.title} " + "─" * max(0, 50 - len(section.title)))
        for entry in section.items:
            print(f"\n  [{entry.get('origin', '')}] {entry.get('title', '')}")
            if entry.get("summary"):
                print(f"    {entry['summary']}")
            if entry.get("why"):
                print(f"    ➡️  {entry['why']}")
            if entry.get("url"):
                print(f"    {entry['url']}")
    if report.closing:
        print(f"\n💡 {report.closing}")
    print(
        f"\n[수집 {report.total_collected}건 → 선별 {report.total_used}건 | "
        f"논문 {report.papers_found}편 | 중복제외 {report.skipped_as_seen}건 | "
        f"{json.dumps(report.source_counts, ensure_ascii=False)}]\n"
    )


def dump_prompt(config: Config, items: list[Item], dest: str) -> int:
    """Claude 에 보낼 요청을 조립만 해서 보여준다 (전송·과금 없음)."""
    if not items:
        log.warning("선별된 항목이 없어 Claude 를 부를 일이 없습니다")
        return 0

    request = Summarizer(config).build_request(items)
    system = request["system"]
    user = request["messages"][0]["content"]
    schema = request["output_config"]["format"]["schema"]

    body = (
        f"=== 요청 설정 ===\n"
        f"model        : {request['model']}\n"
        f"max_tokens   : {request['max_tokens']}\n"
        f"effort       : {request['output_config']['effort']}\n"
        f"output_format: json_schema (필수 필드 {len(schema['required'])}개)\n"
        f"항목 수      : {len(items)}건\n"
        f"논문 붙은 항목: {sum(1 for i in items if any(p.resolved for p in i.papers))}건\n"
        f"\n=== SYSTEM ===\n{system}\n"
        f"\n=== USER ===\n{user}\n"
    )

    if dest == "-":
        print(body)
    else:
        Path(dest).write_text(body, encoding="utf-8")
        log.info("프롬프트를 %s 에 저장했습니다 (%d자)", dest, len(body))

    # 대략적인 규모만 — 정확한 토큰 수는 API 키가 있어야 셀 수 있다
    log.info(
        "프롬프트 크기: system %d자 + user %d자 = 약 %.1fK자 (대략 %.1fK 토큰 추정)",
        len(system),
        len(user),
        (len(system) + len(user)) / 1000,
        (len(system) + len(user)) / 2500,
    )
    log.info("Claude 는 호출하지 않았습니다 — 과금 없음")
    return 0


# ----------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    config = Config(args.config)

    if args.hours:
        config.collect["lookback_hours"] = args.hours
    if args.max_items:
        config.collect["max_items"] = args.max_items
    if args.no_state:
        config.raw.setdefault("state", {})["enabled"] = False

    # 전송하지 않는 모드(--dry-run / --dump-prompt)는 비밀값 없이도 돌아야 한다
    sends = not (args.dry_run or args.dump_prompt)
    use_llm = not args.no_llm
    if sends:
        config.validate_for_send()
    elif use_llm and not args.dump_prompt and not config.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY 가 없어 --no-llm 으로 진행합니다")
        use_llm = False

    sources = args.source or [
        name for name in COLLECTORS if config.section(name).get("enabled", True)
    ]
    log.info(
        "수집 시작 — 소스: %s / 최근 %d시간",
        ", ".join(sources),
        config.lookback_hours,
    )

    # 1) 수집
    items = collect_all(config, sources)
    if not items:
        log.warning("수집된 항목이 없습니다")
    total_collected = len(items)

    # 2) 이미 보도한 항목 제외 (같은 논문이 며칠 연속 올라가는 것 방지)
    store = SeenStore(config)
    items, skipped = store.filter_unseen(items)

    # 3) 필터링 · 스코어링 · 선별
    selected = Scorer(config).select(items)

    # 4) 선별된 것만 arXiv 보강 — 여기서 처음 arXiv API 를 부른다
    papers_found = ArxivEnricher(config).enrich(selected)

    # 4.5) Claude 호출 직전 상태 점검 (API 키 없이도 동작)
    if args.dump_prompt:
        return dump_prompt(config, selected, args.dump_prompt)

    # 5) 요약
    report = build_report(config, selected, total_collected, use_llm)
    report.papers_found = papers_found
    report.skipped_as_seen = skipped

    if args.dry_run:
        print_report(report)
        return 0

    # 6) 전송 — 성공했을 때만 보도 기록을 남긴다
    if not DiscordSender(config).send(report):
        return 1
    store.record(selected)
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    try:
        return run(args)
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.critical("파이프라인 실패: %s", exc, exc_info=True)
        # 실패 사실만이라도 Discord로 알린다
        if not args.dry_run:
            try:
                DiscordSender(Config(args.config)).send_error(
                    f"{type(exc).__name__}: {exc}\n\n"
                    + "".join(traceback.format_tb(exc.__traceback__)[-2:])
                )
            except Exception:  # noqa: BLE001
                log.error("실패 알림 전송도 실패했습니다")
        return 1


if __name__ == "__main__":
    sys.exit(main())
