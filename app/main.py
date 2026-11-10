"""CLI 入口。各サブコマンドは最小限のアダプタのみ。本体ロジックは各モジュールに委譲。"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from app import config as cfg_module
from app import db as db_module
from app.config import Config, load_config, missing_env_keys

logger = logging.getLogger("ai_cat_reels")


def _configure_logging() -> None:
    cfg_module.ensure_directories()
    log_path = cfg_module.LOGS_DIR / "app.log"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_path)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
    )


def _check_env(cfg: Config, command: str) -> int:
    missing = missing_env_keys(cfg, command)
    if missing:
        for key in missing:
            logger.error("Missing required env: %s", key)
        return 2
    return 0


def _apply_dry_run(cfg: Config, dry_run_flag: bool) -> Config:
    return replace(cfg, dry_run=cfg.dry_run or dry_run_flag)


def cmd_init(_args: argparse.Namespace, _cfg: Config) -> int:
    cfg_module.ensure_directories()
    db_module.init_db()
    logger.info("Initialized DB at %s", cfg_module.DB_PATH)

    # .env 全体の必須キー検証
    cfg = load_config()
    all_keys = cfg_module.all_required_keys()
    mapping: dict[str, str] = {
        "IG_ACCESS_TOKEN": cfg.ig_access_token,
        "IG_USER_ID": cfg.ig_user_id,
        "KLING_ACCESS_KEY": cfg.kling_access_key,
        "KLING_SECRET_KEY": cfg.kling_secret_key,
        "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
    }
    missing = [k for k in all_keys if not mapping.get(k, "")]
    if missing:
        logger.warning("Missing .env keys: %s", ", ".join(missing))
        logger.warning(".env.example を参考に設定してください (ファイル: %s)", cfg_module.ENV_PATH)
        return 1
    logger.info("All required .env keys are set.")
    return 0


def cmd_collect(_args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "collect")) != 0:
        return rc
    from app.collector import collect

    stats = collect(cfg)
    logger.info("collect done: %s", stats)
    return 0


def cmd_analyze(args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "analyze")) != 0:
        return rc
    from app.analyzer import analyze_pending

    result = analyze_pending(cfg, limit=args.limit)
    logger.info("analyze done: %s", result)
    return 0


def cmd_plan(_args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "plan")) != 0:
        return rc
    from app.planner import plan_today

    gen_id = plan_today(cfg)
    logger.info("plan done: generation_id=%s", gen_id)
    return 0


def cmd_generate(args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "generate")) != 0:
        return rc
    from app.kling_client import generate_for

    gen_id = generate_for(cfg, generation_id=args.generation_id)
    logger.info("generate done: generation_id=%s", gen_id)
    return 0


def cmd_publish(args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "publish")) != 0:
        return rc
    from app.publisher import publish_for

    result = publish_for(cfg, generation_id=args.generation_id)
    logger.info("publish done: %s", result)
    return 0


def cmd_metrics(_args: argparse.Namespace, cfg: Config) -> int:
    if (rc := _check_env(cfg, "metrics")) != 0:
        return rc
    from app.publisher import refresh_metrics

    n = refresh_metrics(cfg)
    logger.info("metrics done: updated=%d", n)
    return 0


def _already_posted_today() -> bool:
    today = date.today().isoformat()
    with db_module.open_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE substr(posted_at, 1, 10) = ? LIMIT 1", (today,)
        ).fetchone()
        return row is not None


def cmd_daily(args: argparse.Namespace, cfg: Config) -> int:
    if _already_posted_today():
        logger.info("daily: already posted today. skip.")
        return 0

    # metrics
    if (rc := cmd_metrics(args, cfg)) != 0:
        logger.warning("daily: metrics failed rc=%d (continue)", rc)

    # collect + analyze (任意)
    if args.with_collect:
        if (rc := cmd_collect(args, cfg)) != 0:
            logger.warning("daily: collect failed rc=%d (continue)", rc)
        else:
            analyze_args = argparse.Namespace(limit=10)
            if (rc := cmd_analyze(analyze_args, cfg)) != 0:
                logger.warning("daily: analyze failed rc=%d (continue)", rc)

    # plan → generate → publish
    if (rc := cmd_plan(args, cfg)) != 0:
        return rc
    gen_args = argparse.Namespace(generation_id=None)
    if (rc := cmd_generate(gen_args, cfg)) != 0:
        return rc
    pub_args = argparse.Namespace(generation_id=None)
    return cmd_publish(pub_args, cfg)


def _add_dry_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Instagram への投稿を行わない")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-cat-reels")
    sub = parser.add_subparsers(dest="command", required=True)

    # --dry-run はサブコマンド側にだけ付ける (グローバル位置と両立させると
    # argparse 側で defaults=False に上書きされる罠があるため)。
    subparsers: list[argparse.ArgumentParser] = []

    p = sub.add_parser("init", help="DB・フォルダ作成、.env チェック")
    p.set_defaults(func=cmd_init)
    subparsers.append(p)

    p = sub.add_parser("collect", help="人気動画を収集")
    p.set_defaults(func=cmd_collect)
    subparsers.append(p)

    p = sub.add_parser("analyze", help="未分析の参考動画を分析")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_analyze)
    subparsers.append(p)

    p = sub.add_parser("plan", help="今日のネタと Kling プロンプトを生成")
    p.set_defaults(func=cmd_plan)
    subparsers.append(p)

    p = sub.add_parser("generate", help="Kling で動画生成・ダウンロード")
    p.add_argument("--generation-id", type=int, default=None)
    p.set_defaults(func=cmd_generate)
    subparsers.append(p)

    p = sub.add_parser("publish", help="生成済み動画をリール投稿")
    p.add_argument("--generation-id", type=int, default=None)
    p.set_defaults(func=cmd_publish)
    subparsers.append(p)

    p = sub.add_parser("metrics", help="自分の投稿の反応を更新")
    p.set_defaults(func=cmd_metrics)
    subparsers.append(p)

    p = sub.add_parser("daily", help="毎日の一連の流れをまとめて実行")
    p.add_argument("--with-collect", action="store_true")
    p.set_defaults(func=cmd_daily)
    subparsers.append(p)

    for sp in subparsers:
        _add_dry_run(sp)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = _apply_dry_run(load_config(), getattr(args, "dry_run", False))
    try:
        return args.func(args, cfg)
    except Exception:
        logger.exception("command failed: %s", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
