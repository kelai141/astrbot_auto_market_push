"""AstrBot 插件入口：把「插件市场自动送审」跑在 AstrBot 框架里。

设计要点：

* 本文件是**薄适配层**，真正的逻辑都在仓库根目录下的 ``astrbot_auto_market_push``
  纯 Python 包中，CLI 与本插件共用同一套引擎；
* 全程不依赖任何 LLM，只是一个定时轮询 + HTTP 调用的自动化机器人；
* 插件配置来自 ``_conf_schema.json``，运行状态与去重数据落在
  ``data/plugin_data/astrbot_auto_market_push``，升级/重装插件不会丢数据。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

# AstrBot 以 `data.plugins.<目录名>.main` 的形式 __import__ 本模块，
# 插件目录并不在 sys.path 上。这里手动挂载，使内置的 astrbot_auto_market_push
# 包可以被正常导入（CLI 安装方式下 __init__.py 已存在，此处会直接命中）。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from astrbot.api import logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # noqa: E402
from astrbot.api.star import Context, Star  # noqa: E402

from astrbot_auto_market_push.config import (  # noqa: E402
    AppConfig,
    build_targets_from_entries,
    load_config_dict,
    parse_overrides_yaml,
)
from astrbot_auto_market_push.engine import Engine  # noqa: E402
from astrbot_auto_market_push.errors import (  # noqa: E402
    AmpError,
    ConfigError,
    SessionExpiredError,
)
from astrbot_auto_market_push.models import ActionKind  # noqa: E402
from astrbot_auto_market_push.state import summarize_status  # noqa: E402

PLUGIN_NAME = "astrbot_auto_market_push"

HELP_TEXT = """插件市场自动送审 · 可用命令

/ampush status        查看会话、限流与各仓库状态
/ampush check         只检查将要执行的动作，不提交
/ampush push          立即执行一轮（忽略最小间隔）
/ampush start         启动后台轮询
/ampush stop          停止后台轮询
/ampush login         查看如何获取 / 更新会话
/ampush help          显示本帮助

跟踪逻辑：读取每个仓库默认分支上的 metadata.yaml，version 变化即自动送审。"""


def _plugin_data_dir() -> Path:
    """Resolve the per-plugin data directory, portable across AstrBot layouts."""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:  # noqa: BLE001 - defensive: AstrBot API may move
        base = Path.cwd() / "data" / "plugin_data"
    return base / PLUGIN_NAME


class Main(Star):
    """AstrBot 的插件类。注册命令并管理后台轮询任务。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config: dict = dict(config or {})
        self.data_dir = _plugin_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_summary = "尚未运行"

    # ------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        logger.info("%s 已加载，数据目录：%s", PLUGIN_NAME, self.data_dir)
        self._log_config_warnings()
        if self._section("watch").get("auto_start", True):
            started = self.start_polling()
            if started:
                logger.info("%s 后台轮询已启动。", PLUGIN_NAME)

    async def terminate(self) -> None:
        self.stop_polling()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        logger.info("%s 已卸载。", PLUGIN_NAME)

    def _log_config_warnings(self) -> None:
        watch = self._section("watch")
        if not watch.get("repositories"):
            logger.warning("%s 尚未配置任何监控仓库。", PLUGIN_NAME)
        cloud = self._section("cloud")
        if not cloud.get("storage_state_json") and not cloud.get("storage_state"):
            logger.warning(
                "%s 尚未配置会话，请先在电脑上执行 `ampush login` 并把 "
                "storage_state.json 粘贴到插件配置。",
                PLUGIN_NAME,
            )

    # -------------------------------------------------------------- polling

    def start_polling(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name=PLUGIN_NAME)
        return True

    def stop_polling(self) -> bool:
        if self._task is None or self._task.done():
            return False
        self._stop.set()
        return True

    @property
    def polling(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _poll_loop(self) -> None:
        interval = max(30, int(self._section("watch").get("interval_seconds", 300)))
        while not self._stop.is_set():
            try:
                report = await self._run_cycle(force=False)
                self._last_summary = self._render_summary(report)
                await self._push_result(report)
            except SessionExpiredError as exc:
                logger.error("%s 会话失效，轮询停止：%s", PLUGIN_NAME, exc)
                self._last_summary = f"会话已失效：{exc}"
                return
            except AmpError as exc:
                logger.error("%s 轮询出错：%s", PLUGIN_NAME, exc)
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.exception("%s 未预期的错误：%s", PLUGIN_NAME, exc)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _run_cycle(self, *, force: bool):
        config = self.build_app_config()
        async with self._lock, Engine(config, force=force) as engine:
            return await engine.cycle()

    async def _push_result(self, report) -> None:
        """Optionally push the cycle result to a configured UMO."""
        target = str(self._section("watch").get("notify_target") or "").strip()
        if not target or not report.outcomes:
            return
        try:
            chain = MessageChain().message(self._render_summary(report))
            await self.context.send_message(target, chain)
        except Exception as exc:  # noqa: BLE001 - push is best effort
            logger.warning("%s 主动推送失败：%s", PLUGIN_NAME, exc)

    # -------------------------------------------------------------- commands

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ampush")
    async def ampush(self, event: AstrMessageEvent):
        """插件市场自动送审：状态 / 检查 / 送审 / 启停。"""
        action = self._extract_action(event.message_str)
        if action in {"", "status"}:
            yield event.plain_result(await self._cmd_status())
        elif action == "check":
            yield event.plain_result(await self._cmd_check())
        elif action == "push":
            yield event.plain_result(await self._cmd_push())
        elif action == "start":
            yield event.plain_result(self._cmd_start())
        elif action == "stop":
            yield event.plain_result(self._cmd_stop())
        elif action == "login":
            yield event.plain_result(self._cmd_login())
        else:
            yield event.plain_result(HELP_TEXT)

    @staticmethod
    def _extract_action(message: str) -> str:
        tokens = (message or "").strip().split()
        if len(tokens) <= 1:
            return ""
        return tokens[1].strip().lower()

    async def _cmd_status(self) -> str:
        lines = ["插件市场自动送审 · 状态", f"轮询：{'运行中' if self.polling else '已停止'}"]
        try:
            config = self.build_app_config()
            async with Engine(config) as engine:
                account = await engine.cloud.get_me()
                options = engine.options or {}
                lines.append(f"账号：{(account or {}).get('username', '未知')}")
                lines.append(
                    "云端限流：同时 "
                    f"{options.get('max_active_reviews_per_account', '?')} / 24h "
                    f"{options.get('max_review_submissions_per_account_24h', '?')}"
                )
                table_lines = []
                for target in config.watch.repositories:
                    state = engine.state.get_repo_state(target.ref_name.full_name)
                    online = "-"
                    try:
                        market = await engine.cloud.find_plugin_by_repo(
                            f"https://github.com/{target.ref_name.full_name}"
                        )
                        if market is not None:
                            online = market.current_online_version or "-"
                    except Exception:  # noqa: BLE001
                        pass
                    table_lines.append(
                        f"  · {target.ref_name.full_name}"
                        f"（本地 {(state or {}).get('last_seen_version') or '-'}"
                        f" / 线上 {online}）"
                    )
                lines.append("监控仓库：")
                lines.extend(table_lines or ["  （未配置）"])
        except SessionExpiredError as exc:
            lines.append(f"会话不可用：{exc}")
        except AmpError as exc:
            lines.append(f"读取状态失败：{exc}")

        lines.append(f"上次结果：{self._last_summary}")
        return "\n".join(lines)

    async def _cmd_check(self) -> str:
        try:
            report = await self._run_cycle(force=False)
        except SessionExpiredError as exc:
            return f"会话不可用：{exc}"
        except AmpError as exc:
            return f"检查失败：{exc}"

        lines = ["插件市场自动送审 · 检查结果"]
        for plan in report.plans:
            mark = {"submit": "🚀", "skip": "⏭️", "error": "❌"}[plan.kind.value]
            lines.append(
                f"{mark} {plan.target.key}｜线上 {plan.online_version or '未上架'}"
                f" → 本地 {plan.local_version or '未知'}\n    {plan.reason}"
            )
        return "\n".join(lines)

    async def _cmd_push(self) -> str:
        try:
            report = await self._run_cycle(force=True)
        except SessionExpiredError as exc:
            return f"会话不可用：{exc}"
        except AmpError as exc:
            return f"送审失败：{exc}"
        self._last_summary = self._render_summary(report)
        return self._render_summary(report, detailed=True)

    def _cmd_start(self) -> str:
        if self.polling:
            return "轮询已在运行中。"
        self.start_polling()
        return "已启动后台轮询。"

    def _cmd_stop(self) -> str:
        if not self.polling:
            return "轮询当前未运行。"
        self.stop_polling()
        return "已请求停止后台轮询。"

    def _cmd_login(self) -> str:
        return (
            "获取 / 更新会话的步骤：\n"
            "1. 在电脑上安装命令行工具：pip install astrbot-auto-market-push\n"
            "2. 执行：ampush login\n"
            "3. 在弹出的浏览器里完成 GitHub 登录，并安装 AstrBot Cloud GitHub App\n"
            "   （务必把要送审的仓库加入可访问列表）\n"
            "4. 把生成的 .state/storage_state.json 内容整段粘贴到本插件的\n"
            "   「会话 JSON」配置项中并保存。\n"
            "会话过期后重复步骤 2-4 即可。"
        )

    async def _cmd_status_placeholder(self) -> str:  # pragma: no cover
        return ""

    # --------------------------------------------------------------- helpers

    def _section(self, name: str) -> dict:
        value = self.config.get(name)
        return value if isinstance(value, dict) else {}

    def build_app_config(self) -> AppConfig:
        """Translate the AstrBot plugin config into the engine's config model."""
        watch_raw = dict(self._section("watch"))
        cloud_raw = dict(self._section("cloud"))
        github_raw = dict(self._section("github"))
        submission_raw = dict(self._section("submission"))

        overrides_text = str(watch_raw.get("overrides_yaml") or "").strip()
        try:
            overrides_map = parse_overrides_yaml(overrides_text)
        except ConfigError as exc:
            logger.warning("%s overrides_yaml 解析失败，已忽略：%s", PLUGIN_NAME, exc)
            overrides_map = {}

        targets = build_targets_from_entries(watch_raw.get("repositories"), overrides_map)
        # `submission.enabled = false` maps onto per-target auto_submit so the
        # engine keeps tracking (and reporting) without ever submitting.
        if not submission_raw.get("enabled", True):
            for target in targets:
                target.auto_submit = False
        watch_raw["repositories"] = [target.model_dump() for target in targets]
        watch_raw.pop("overrides_yaml", None)

        if not str(cloud_raw.get("storage_state_json") or "").strip():
            cloud_raw["storage_state_json"] = None
        if not str(cloud_raw.get("storage_state") or "").strip():
            cloud_raw["storage_state"] = None
        if not str(github_raw.get("token") or "").strip():
            github_raw["token"] = None

        if not cloud_raw.get("storage_state"):
            cloud_raw["storage_state"] = str(self.data_dir / "storage_state.json")

        raw = {
            "cloud": cloud_raw,
            "github": github_raw,
            "watch": watch_raw,
            "submission": submission_raw,
            "notify": dict(self._section("notify")),
            "logging": dict(self._section("logging")),
            "state": {"path": str(self.data_dir / "state.sqlite3")},
        }
        return load_config_dict(raw, base=self.data_dir)

    @staticmethod
    def _render_summary(report, *, detailed: bool = False) -> str:
        lines = [
            "插件市场自动送审 · 本轮结果",
            f"送审 {report.submitted} · 模拟 {report.simulated} · "
            f"失败 {report.failed} · 跳过 {report.skipped} · 错误 {report.errors}",
        ]
        for outcome in report.outcomes:
            if outcome.dry_run:
                state = "模拟"
            elif outcome.submitted:
                state = "已提交"
            else:
                state = "失败"
            lines.append(
                f"  · {outcome.repo} {outcome.version} → {state}"
                f"（{summarize_status(outcome.status)}）{outcome.message}"
            )
        if detailed and not report.outcomes:
            for plan in report.plans:
                lines.append(f"  · {plan.target.key}：{plan.reason}")
        return "\n".join(lines)


__all__ = ["ActionKind", "Main"]
