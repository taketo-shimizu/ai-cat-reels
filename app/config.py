"""設定値の読み込み。必須項目の検証はコマンド側が `required_for_runtime` で明示的に呼ぶ。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"
VIDEOS_DIR = DATA_DIR / "videos"
TMP_DIR = DATA_DIR / "tmp"
LOGS_DIR = REPO_ROOT / "logs"
DB_PATH = DATA_DIR / "app.db"
CONFIG_DIR = REPO_ROOT / "config"
HASHTAGS_FILE = CONFIG_DIR / "hashtags.txt"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.txt"


@dataclass(frozen=True)
class Config:
    ig_access_token: str
    ig_user_id: str
    ig_graph_version: str
    kling_api_key: str
    kling_access_key: str
    kling_secret_key: str
    kling_base_url: str
    kling_model: str
    kling_mode: str
    kling_duration: str
    kling_sound: str
    anthropic_api_key: str
    claude_model: str
    dry_run: bool

    def has_kling_credentials(self) -> bool:
        if self.kling_api_key:
            return True
        return bool(self.kling_access_key and self.kling_secret_key)

    def graph_base(self) -> str:
        return f"https://graph.facebook.com/{self.ig_graph_version}"

    def rupload_base(self) -> str:
        return f"https://rupload.facebook.com/ig-api-upload/{self.ig_graph_version}"


_REQUIRED_KEYS_BY_COMMAND: dict[str, tuple[str, ...]] = {
    "collect": ("IG_ACCESS_TOKEN", "IG_USER_ID"),
    "analyze": ("ANTHROPIC_API_KEY",),
    "plan": ("ANTHROPIC_API_KEY",),
    # generate は KLING_API_KEY or (KLING_ACCESS_KEY + KLING_SECRET_KEY) のどちらか。
    # missing_env_keys() で特別扱いする。
    "generate": (),
    "publish": ("IG_ACCESS_TOKEN", "IG_USER_ID"),
    "metrics": ("IG_ACCESS_TOKEN", "IG_USER_ID"),
}


def load_config() -> Config:
    """.env を読み込み Config を返す。未設定項目は空文字のまま。"""
    load_dotenv(ENV_PATH, override=False)
    return Config(
        ig_access_token=os.getenv("IG_ACCESS_TOKEN", ""),
        ig_user_id=os.getenv("IG_USER_ID", ""),
        ig_graph_version=os.getenv("IG_GRAPH_VERSION", "v23.0"),
        kling_api_key=os.getenv("KLING_API_KEY", ""),
        kling_access_key=os.getenv("KLING_ACCESS_KEY", ""),
        kling_secret_key=os.getenv("KLING_SECRET_KEY", ""),
        kling_base_url=os.getenv("KLING_BASE_URL", "https://api-singapore.klingai.com").rstrip("/"),
        kling_model=os.getenv("KLING_MODEL", "kling-v2-6"),
        kling_mode=os.getenv("KLING_MODE", "std"),
        kling_duration=os.getenv("KLING_DURATION", "5"),
        kling_sound=os.getenv("KLING_SOUND", "on"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        dry_run=os.getenv("DRY_RUN", "false").lower() in {"true", "1", "yes"},
    )


def missing_env_keys(cfg: Config, command: str) -> list[str]:
    """指定コマンドの実行に必要な .env キーのうち、未設定のものを返す。"""
    keys = _REQUIRED_KEYS_BY_COMMAND.get(command, ())
    mapping: dict[str, str] = {
        "IG_ACCESS_TOKEN": cfg.ig_access_token,
        "IG_USER_ID": cfg.ig_user_id,
        "KLING_API_KEY": cfg.kling_api_key,
        "KLING_ACCESS_KEY": cfg.kling_access_key,
        "KLING_SECRET_KEY": cfg.kling_secret_key,
        "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
    }
    missing = [k for k in keys if not mapping.get(k, "")]
    # generate は API Key / (AK + SK) のどちらかが必要
    if command == "generate" and not cfg.has_kling_credentials():
        missing.append("KLING_API_KEY or (KLING_ACCESS_KEY + KLING_SECRET_KEY)")
    return missing


def all_required_keys() -> tuple[str, ...]:
    seen: list[str] = []
    for keys in _REQUIRED_KEYS_BY_COMMAND.values():
        for k in keys:
            if k not in seen:
                seen.append(k)
    return tuple(seen)


def ensure_directories() -> None:
    for d in (DATA_DIR, VIDEOS_DIR, TMP_DIR, LOGS_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_lines(path: Path) -> list[str]:
    """# で始まる行と空行を無視して読む。"""
    if not path.exists():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return lines
