"""Command line interface.

Subcommands map one-to-one onto operator intents:

``login``    one-time browser bootstrap, produces ``storage_state.json``
``whoami``   verify the stored session and print cloud capabilities
``check``    show what *would* happen, without submitting anything
``once``     run exactly one poll/submit cycle
``run``      poll forever (the "实时跟踪" mode)
``doctor``   diagnose config / session / GitHub App wiring
``state``    inspect and reset the local dedup database
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import AppConfig, load_config
from .engine import Engine
from .errors import AmpError, ConfigError, SessionExpiredError
from .github_client import GitHubClient
from .logging_setup import setup_logging
from .models import ActionKind
from .session import bootstrap_login
from .state import StateStore, summarize_status
from .watcher import Watcher

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="自动跟踪 GitHub 仓库并把新版本送审 AstrBot 插件市场。",
)
state_app = typer.Typer(help="查看与重置本地去重状态。", no_args_is_help=True)
app.add_typer(state_app, name="state")

console = Console()
err_console = Console(stderr=True)

ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="YAML 配置文件路径。")]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="输出调试日志。")]


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"astrbot-auto-market-push {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="显示版本并退出。",
        ),
    ] = False,
) -> None:
    """AstrBot 插件市场自动送审工具。"""


def _prepare(config_path: Path | None, verbose: bool) -> AppConfig:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err_console.print(f"[red]配置错误：[/red]{exc}")
        raise typer.Exit(code=2) from exc
    setup_logging("DEBUG" if verbose else config.logging.level, log_file=config.logging.file)
    return config


def _fail(message: str, code: int = 1) -> None:
    err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=code)


# --------------------------------------------------------------------------- login


@app.command()
def login(
    config: ConfigOption = None,
    headless: Annotated[
        bool, typer.Option("--headless", help="不显示浏览器窗口（需要已有可用的登录态）。")
    ] = False,
    timeout: Annotated[int, typer.Option("--timeout", help="等待登录完成的秒数。")] = 600,
    skip_github: Annotated[
        bool, typer.Option("--skip-github", help="只等登录，不等待 GitHub App 安装。")
    ] = False,
) -> None:
    """打开浏览器完成一次性登录，并保存会话到 storage_state.json。"""
    config = _prepare(config, verbose=False)
    state_path = config.cloud.storage_state
    if state_path is None:
        _fail("未配置 cloud.storage_state，无法确定会话保存位置。")

    try:
        asyncio.run(
            bootstrap_login(
                state_path=state_path,
                headless=headless,
                login_timeout=timeout,
                wait_for_github=not skip_github,
            )
        )
    except ConfigError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        _fail("登录引导被中断。")

    console.print(f"[green]会话已保存：[/green]{state_path}")
    console.print("后续无需浏览器，直接运行 `ampush check` 或 `ampush run` 即可。")


# -------------------------------------------------------------------------- whoami


@app.command()
def whoami(config: ConfigOption = None) -> None:
    """检查当前会话，并打印云端能力与限流配置。"""
    config = _prepare(config, verbose=False)

    async def _run() -> None:
        async with Engine(config) as engine:
            account = await engine.cloud.get_me()
            options = engine.options or {}
            connection = await engine.cloud.get_github_connection()

            table = Table(title="AstrBot Cloud 账号", show_header=False)
            table.add_row("账号", str((account or {}).get("username", "未知")))
            table.add_row("角色", str((account or {}).get("role", "user")))
            table.add_row(
                "GitHub 连接",
                "[green]已连接[/green]" if connection.get("connected") else "[red]未连接[/red]",
            )
            console.print(table)

            limits = Table(title="云端限制", show_header=True, header_style="bold")
            limits.add_column("项目")
            limits.add_column("值")
            limits.add_row("允许送审", str(options.get("publish_enabled")))
            limits.add_row("支持 zip 上传", str(options.get("zip_upload_enabled")))
            limits.add_row("GitHub App 启用", str(options.get("github_app_enabled")))
            limits.add_row(
                "同时审核上限", str(options.get("max_active_reviews_per_account", "未知"))
            )
            limits.add_row(
                "24h 送审上限",
                str(options.get("max_review_submissions_per_account_24h", "未知")),
            )
            console.print(limits)

    try:
        asyncio.run(_run())
    except SessionExpiredError as exc:
        _fail(str(exc))


# --------------------------------------------------------------------------- check


@app.command()
def check(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """只做检查，不实际提交：列出每个仓库将要执行的动作。"""
    config = _prepare(config, verbose)

    async def _run() -> None:
        try:
            async with Engine(config) as engine:
                watcher = Watcher(
                    config=config,
                    github=engine.github,
                    cloud=engine.cloud,
                    state=engine.state,
                )
                plans = await watcher.evaluate_all()
                _print_plans(plans)
        except SessionExpiredError as exc:
            err_console.print(f"[yellow]会话不可用，降级为仅检查 GitHub：[/yellow]{exc}")
            await _check_github_only(config)

    asyncio.run(_run())


async def _check_github_only(config: AppConfig) -> None:
    async with GitHubClient(
        token=config.github.token,
        api_url=config.github.api_url,
        timeout=config.github.timeout_seconds,
    ) as github:
        table = Table(title="仅本地检查（无云端会话）", header_style="bold")
        table.add_column("仓库")
        table.add_column("分支")
        table.add_column("metadata 版本")
        table.add_column("commit")
        for target in config.watch.repositories:
            try:
                metadata, commit = await github.fetch_metadata(target)
                table.add_row(
                    target.ref_name.full_name,
                    target.ref or "(默认分支)",
                    metadata.normalized_version,
                    (commit or "")[:12],
                )
            except Exception as exc:  # noqa: BLE001
                table.add_row(target.repo, target.ref or "-", f"[red]{exc}[/red]", "-")
        console.print(table)


def _print_plans(plans: list) -> None:
    table = Table(title="跟踪结果", header_style="bold")
    table.add_column("仓库")
    table.add_column("线上版本")
    table.add_column("本地版本")
    table.add_column("动作")
    table.add_column("说明", overflow="fold")

    for plan in plans:
        color = {
            ActionKind.SUBMIT: "green",
            ActionKind.SKIP: "yellow",
            ActionKind.ERROR: "red",
        }[plan.kind]
        table.add_row(
            plan.target.key,
            plan.online_version or "-",
            plan.local_version or "-",
            f"[{color}]{plan.kind.value}[/{color}]",
            plan.reason,
        )
    console.print(table)


# --------------------------------------------------------------------------- once


@app.command()
def once(
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="忽略限流与最小间隔，立即提交。")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只构建提交内容，不真正发送。")
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """执行一轮：检查所有仓库并对需要更新的仓库送审。"""
    config = _prepare(config, verbose)
    if dry_run:
        config.submission.dry_run = True

    async def _run() -> None:
        async with Engine(config, force=force) as engine:
            report = await engine.cycle()
            _print_plans(report.plans)
            _print_outcomes(report)
            if report.session_expired:
                _fail("会话已失效，请重新执行 `ampush login`。", code=3)

    asyncio.run(_run())


# ---------------------------------------------------------------------------- run


@app.command()
def run(
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="忽略限流与最小间隔。")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="长期运行但不真正提交。")] = False,
    verbose: VerboseOption = False,
) -> None:
    """常驻轮询：实时跟踪并自动送审（Ctrl+C 退出）。"""
    config = _prepare(config, verbose)
    if dry_run:
        config.submission.dry_run = True

    async def _run() -> None:
        async with Engine(config, force=force) as engine:
            await engine.run_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")


# ------------------------------------------------------------------------- doctor


@app.command()
def doctor(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """诊断配置、会话、GitHub App 与监控目标是否就绪。"""
    config = _prepare(config, verbose)
    console.print(f"[bold]配置文件[/bold]：{config.state.path.parent}")

    if not config.watch.repositories:
        _fail("没有任何监控目标，请在配置的 watch.repositories 中至少填写一个仓库。")

    async def _run() -> None:
        try:
            engine_cm = Engine(config)
        except SessionExpiredError as exc:
            err_console.print(f"[red]✗[/red] AstrBot Cloud 会话不可用：{exc}")
            err_console.print(
                "[yellow]提示[/yellow]：请先执行 `ampush login`。下面仅检查 GitHub 侧的配置。"
            )
            await _check_github_only(config)
            return

        async with engine_cm as engine:
            report = await engine.health()

            session = report["session"]
            if session["ok"]:
                console.print("[green]✓[/green] AstrBot Cloud 会话有效")
            else:
                console.print(f"[red]✗[/red] AstrBot Cloud 会话无效：{session['error']}")
                return

            console.print(f"[green]✓[/green] GitHub 连接：{report.get('github_connection', {})}")
            token_state = "已配置" if report["github_token"] else "未配置（限流 60 次/小时）"
            console.print(f"[green]✓[/green] GitHub token：{token_state}")

            table = Table(title="监控目标", header_style="bold")
            table.add_column("仓库")
            table.add_column("GitHub App")
            table.add_column("installation_id")
            table.add_column("repository_id")
            for target in report["targets"]:
                ok = target.get("github_app_ok")
                app_state = (
                    "[green]可访问[/green]"
                    if ok
                    else f"[red]{target.get('github_app_error')}[/red]"
                )
                table.add_row(
                    target["repo"],
                    app_state,
                    str(target.get("installation_id", "-")),
                    str(target.get("repository_id", "-")),
                )
            console.print(table)

    try:
        asyncio.run(_run())
    except SessionExpiredError as exc:
        _fail(str(exc))


# --------------------------------------------------------------------- outcomes


def _print_outcomes(report) -> None:
    if not report.outcomes:
        console.print("[dim]本轮没有任何送审动作。[/dim]")
        return

    table = Table(title="送审结果", header_style="bold")
    table.add_column("仓库")
    table.add_column("版本")
    table.add_column("结果")
    table.add_column("状态")
    table.add_column("说明", overflow="fold")
    for outcome in report.outcomes:
        if outcome.dry_run:
            result = "[cyan]模拟[/cyan]"
        elif outcome.submitted:
            result = "[green]已提交[/green]"
        else:
            result = "[red]失败[/red]"
        table.add_row(
            outcome.repo,
            outcome.version,
            result,
            summarize_status(outcome.status),
            outcome.message,
        )
    console.print(table)

    summary = (
        f"送审 {report.submitted} · 模拟 {report.simulated} · 失败 {report.failed}"
        f" · 跳过 {report.skipped} · 错误 {report.errors}"
    )
    console.print(f"[bold]{summary}[/bold]")


# ------------------------------------------------------------------------ state


@state_app.command("show")
def state_show(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="显示的历史条数。")] = 20,
) -> None:
    """显示本地状态与最近的送审历史。"""
    config = _prepare(config, verbose=False)
    with StateStore(config.state.path) as store:
        rows = store.export_repo_states()
        table = Table(title="仓库状态", header_style="bold")
        table.add_column("仓库")
        table.add_column("已见到版本")
        table.add_column("已送审版本")
        table.add_column("最近状态")
        table.add_column("连续失败")
        for row in rows:
            table.add_row(
                row["repo"],
                row["last_seen_version"] or "-",
                row["last_submitted_version"] or "-",
                row["last_status"] or "-",
                str(row["consecutive_failures"]),
            )
        console.print(table)

        history = store.history(limit=limit)
        if history:
            hist_table = Table(title="送审历史", header_style="bold")
            hist_table.add_column("时间")
            hist_table.add_column("仓库")
            hist_table.add_column("版本")
            hist_table.add_column("结果")
            hist_table.add_column("信息", overflow="fold")
            for item in history:
                hist_table.add_row(
                    item["submitted_at"],
                    item["repo"],
                    item["version"],
                    "模拟" if item["dry_run"] else item["status"],
                    item["message"] or "",
                )
            console.print(hist_table)
        console.print(f"状态数据库：{config.state.path}")


@state_app.command("reset")
def state_reset(
    config: ConfigOption = None,
    repo: Annotated[
        str | None, typer.Option("--repo", help="只重置指定仓库（owner/name）。")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认。")] = False,
) -> None:
    """清除本地去重状态，使下次检查重新评估（不会删除送审历史）。"""
    config = _prepare(config, verbose=False)
    if not yes:
        typer.confirm(f"确认重置{'仓库 ' + repo if repo else '全部'}的本地状态？", abort=True)
    with StateStore(config.state.path) as store:
        count = store.reset(repo)
    console.print(f"[green]已重置 {count} 条状态记录。[/green]")


# ----------------------------------------------------------------------- entry


def main() -> None:
    """Console script entry point."""
    try:
        app()
    except AmpError as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
