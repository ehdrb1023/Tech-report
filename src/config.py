"""config.yaml + .env 로딩."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


class Config:
    """YAML 설정 + 환경변수를 한 곳에서 제공."""

    def __init__(self, path: Path | str | None = None) -> None:
        load_dotenv(ROOT / ".env")

        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as f:
            self.raw: dict[str, Any] = yaml.safe_load(f) or {}

        # --- 환경변수로 덮어쓸 수 있는 값들 ---
        collect = self.raw.setdefault("collect", {})
        collect["lookback_hours"] = int(
            os.getenv("LOOKBACK_HOURS", collect.get("lookback_hours", 24))
        )
        # 0 = 무제한. 문턱(min_keyword_score)을 넘은 건 전부 Claude 에게 보낸다.
        collect["max_items"] = int(
            os.getenv("MAX_ITEMS") or collect.get("max_items", 0) or 0
        )

    # ------------------------------------------------------------------
    # 섹션 접근자
    # ------------------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}

    @property
    def collect(self) -> dict[str, Any]:
        return self.section("collect")

    @property
    def reddit(self) -> dict[str, Any]:
        return self.section("reddit")

    @property
    def arxiv(self) -> dict[str, Any]:
        return self.section("arxiv")

    @property
    def x(self) -> dict[str, Any]:
        return self.section("x")

    @property
    def scoring(self) -> dict[str, Any]:
        return self.section("scoring")

    @property
    def lookback_hours(self) -> int:
        return int(self.collect.get("lookback_hours", 24))

    @property
    def max_items(self) -> int:
        return int(self.collect.get("max_items", 25))

    # ------------------------------------------------------------------
    # 환경변수 (비밀값)
    # ------------------------------------------------------------------
    @staticmethod
    def env(key: str, default: str = "") -> str:
        return (os.getenv(key) or default).strip()

    @property
    def discord_webhook_url(self) -> str:
        return self.env("DISCORD_WEBHOOK_URL")

    @property
    def anthropic_api_key(self) -> str:
        return self.env("ANTHROPIC_API_KEY")

    @property
    def claude_model(self) -> str:
        return self.env("CLAUDE_MODEL", "claude-opus-5")

    @property
    def reddit_client_id(self) -> str:
        return self.env("REDDIT_CLIENT_ID")

    @property
    def reddit_client_secret(self) -> str:
        return self.env("REDDIT_CLIENT_SECRET")

    @property
    def reddit_user_agent(self) -> str:
        """Reddit 규약 형식: <platform>:<app id>:<version> (by /u/<username>)

        식별 가능한 UA 를 쓰는 것이 Reddit 이용 규칙이며, 익명 UA 보다
        차단당할 위험도 낮다. .env 에서 본인 계정명으로 바꾸는 것을 권장.
        """
        return self.env(
            "REDDIT_USER_AGENT",
            "python:discord-ai-trend-bot:v1.0 (by /u/unknown)",
        )

    @property
    def x_bearer_token(self) -> str:
        """있으면 공식 X API, 없으면 Nitter 미러 폴백."""
        return self.env("X_BEARER_TOKEN")

    @property
    def state_dir(self) -> Path:
        """중복 보도 방지 기록·계정 ID 캐시 저장 위치."""
        path = Path(self.env("STATE_DIR") or (ROOT / "state"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def language(self) -> str:
        return self.env("REPORT_LANGUAGE", "ko")

    @property
    def timezone_name(self) -> str:
        return self.env("TIMEZONE", "Asia/Seoul")

    # ------------------------------------------------------------------
    def validate_for_send(self) -> None:
        """실제 전송 전에 필수 비밀값을 확인."""
        missing = []
        if not self.discord_webhook_url:
            missing.append("DISCORD_WEBHOOK_URL")
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise ConfigError(
                "필수 환경변수가 없습니다: "
                + ", ".join(missing)
                + "\n.env 파일 또는 GitHub Secrets 설정을 확인하세요."
            )
