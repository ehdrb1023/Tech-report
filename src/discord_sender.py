"""Discord Webhook 전송.

Discord 제한을 지키기 위해 임베드를 분할·묶어서 여러 메시지로 보낸다.
  - 임베드 description: 4096자
  - 메시지당 임베드: 10개, 전체 합계 6000자
  - 웹훅 rate limit: 429 응답 시 Retry-After 만큼 대기
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .models import Report, ReportSection

log = logging.getLogger(__name__)

MAX_DESC = 4000           # 실제 한도는 4096, 여유를 둔다
MAX_EMBEDS_PER_MSG = 6
MAX_CHARS_PER_MSG = 5000  # 실제 한도는 6000, 여유를 둔다

COLORS = {
    "header": 0x5865F2,   # Discord Blurple
    "🤖": 0x00B894,
    "🚀": 0xE17055,
    "📄": 0x0984E3,
    "💬": 0xFDCB6E,
    "🛠️": 0x636E72,
    "default": 0x74B9FF,
}

SOURCE_BADGE = {
    "reddit": "🔥",
    "arxiv": "📄",
    "x": "𝕏",
}


class DiscordSender:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.webhook_urls = config.discord_webhook_urls
        self.tz = ZoneInfo(config.timezone_name)

    # ------------------------------------------------------------------
    def send(self, report: Report) -> bool:
        messages = self.build_messages(report)
        ok = True
        # 한 채널이 실패해도 나머지 채널에는 계속 보낸다
        for channel, url in enumerate(self.webhook_urls, start=1):
            if not self._send_to(url, messages, channel):
                ok = False
        return ok

    def _send_to(self, url: str, messages: list[dict], channel: int) -> bool:
        ok = True
        for index, payload in enumerate(messages, start=1):
            if not self._post(url, payload):
                ok = False
                log.error("채널 %d: 메시지 %d/%d 전송 실패", channel, index, len(messages))
            if index < len(messages):
                time.sleep(1.0)  # 웹훅 rate limit 여유
        if ok:
            log.info("채널 %d: Discord 전송 완료 (%d개 메시지)", channel, len(messages))
        return ok

    # ------------------------------------------------------------------
    def build_messages(self, report: Report) -> list[dict]:
        """리포트를 Discord 제한에 맞춘 payload 리스트로 변환."""
        embeds = [self._header_embed(report)]
        for section in report.sections:
            embeds.extend(self._section_embeds(section))
        if report.closing:
            embeds.append(
                {
                    "description": f"💡 **오늘의 한 줄** — {report.closing}"[
                        :MAX_DESC
                    ],
                    "color": COLORS["default"],
                }
            )

        # 임베드를 메시지 단위로 묶기
        messages: list[dict] = []
        bucket: list[dict] = []
        bucket_chars = 0
        for embed in embeds:
            size = self._embed_size(embed)
            too_many = len(bucket) >= MAX_EMBEDS_PER_MSG
            too_big = bucket_chars + size > MAX_CHARS_PER_MSG
            if bucket and (too_many or too_big):
                messages.append(self._payload(bucket))
                bucket, bucket_chars = [], 0
            bucket.append(embed)
            bucket_chars += size
        if bucket:
            messages.append(self._payload(bucket))
        return messages

    # ------------------------------------------------------------------
    def _header_embed(self, report: Report) -> dict:
        now = report.generated_at.astimezone(self.tz)
        date_str = now.strftime("%Y년 %m월 %d일")

        lines = [f"**{report.headline}**", ""]
        lines.extend(f"• {line}" for line in report.tldr)
        if report.keywords:
            lines.append("")
            lines.append(
                "🏷️ " + " · ".join(f"`{k}`" for k in report.keywords[:8])
            )

        stat = " / ".join(
            f"{SOURCE_BADGE.get(src, '🔗')} {src} {n}건"
            for src, n in sorted(report.source_counts.items())
        )
        footer = (
            f"수집 {report.total_collected}건 → 선별 {report.total_used}건"
            + (f"  |  {stat}" if stat else "")
        )

        return {
            "title": f"📰 AI · 테크 트렌드 브리핑 — {date_str}",
            "description": "\n".join(lines)[:MAX_DESC],
            "color": COLORS["header"],
            "footer": {"text": footer[:2048]},
            "timestamp": report.generated_at.isoformat(),
        }

    def _section_embeds(self, section: ReportSection) -> list[dict]:
        color = COLORS.get(section.title[:2].strip(), COLORS["default"])

        blocks: list[str] = []
        for entry in section.items:
            badge = SOURCE_BADGE.get(entry.get("source", ""), "🔗")
            title = entry.get("title", "").strip() or "(제목 없음)"
            url = entry.get("url", "")

            head = f"**[{title}]({url})**" if url else f"**{title}**"
            block = [f"{badge} {head}"]
            if entry.get("summary"):
                block.append(entry["summary"])
            if entry.get("why"):
                block.append(f"➡️ {entry['why']}")
            if entry.get("origin"):
                block.append(f"-# {entry['origin']}")
            blocks.append("\n".join(block))

        # description 한도를 넘으면 임베드를 나눈다
        embeds: list[dict] = []
        current: list[str] = []
        length = 0
        for block in blocks:
            if current and length + len(block) + 2 > MAX_DESC:
                embeds.append(
                    self._make_section_embed(section.title, current, color, embeds)
                )
                current, length = [], 0
            current.append(block)
            length += len(block) + 2
        if current:
            embeds.append(
                self._make_section_embed(section.title, current, color, embeds)
            )
        return embeds

    @staticmethod
    def _make_section_embed(
        title: str, blocks: list[str], color: int, existing: list[dict]
    ) -> dict:
        suffix = "" if not existing else f" (계속 {len(existing) + 1})"
        return {
            "title": f"{title}{suffix}"[:256],
            "description": "\n\n".join(blocks)[:MAX_DESC],
            "color": color,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _payload(embeds: list[dict]) -> dict:
        return {
            "username": "AI Trend Bot",
            "embeds": embeds,
            # @everyone / @here 오작동 방지
            "allowed_mentions": {"parse": []},
        }

    @staticmethod
    def _embed_size(embed: dict) -> int:
        return (
            len(embed.get("title", ""))
            + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
        )

    # ------------------------------------------------------------------
    def _post(self, url: str, payload: dict, retries: int = 3) -> bool:
        for attempt in range(retries):
            try:
                resp = requests.post(url, json=payload, timeout=30)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After") or 2)
                    log.warning("Discord rate limit, %.1fs 대기", wait)
                    time.sleep(min(wait, 15))
                    continue
                if resp.status_code >= 400:
                    log.error("Discord %d: %s", resp.status_code, resp.text[:500])
                    return False
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Discord 전송 오류(%d회차): %s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        return False

    # ------------------------------------------------------------------
    def send_error(self, message: str) -> None:
        """파이프라인이 죽었을 때 알림."""
        if not self.webhook_urls:
            return
        now = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")
        payload = {
            "username": "AI Trend Bot",
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": "⚠️ 트렌드 리포트 생성 실패",
                    "description": f"```\n{message[:1500]}\n```",
                    "color": 0xD63031,
                    "footer": {"text": now},
                }
            ],
        }
        for url in self.webhook_urls:
            self._post(url, payload)
