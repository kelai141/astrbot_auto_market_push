# astrbot_auto_market_push

> 把「手动点进 AstrBot 插件市场发布页、填表、点提交」这件事自动化。
>
> 实时跟踪你指定的 GitHub 仓库，**`metadata.yaml` 里的 `version` 一变，就自动送审**。

支持三种运行形态，共用同一套引擎：

| 形态 | 适合场景 |
| --- | --- |
| **AstrBot 插件** | 已经有 AstrBot 在跑，想在聊天里 `/<命令>` 查看与管理 |
| **本地 / 服务器 CLI** | 想常驻轮询，或先手动观察一段时间 |
| **Docker** | 丢到服务器上无人值守 |
| **GitHub Actions** | 不想维护任何服务器，跟随仓库事件或定时触发 |

---

## 它到底做了什么

AstrBot 插件市场的送审入口在 `cloud.astrbot.app`，**没有公开 API Token**，只能在浏览器里点。
本工具复现了它的完整 HTTP 流程：

```
GET  /api/v1/market/options                                # 读取开关与账号级限流
GET  /api/v1/auth/github/connection                        # GitHub 是否已连接
GET  /api/v1/market/github/namespaces                      # 取 installation_id
GET  /api/v1/market/github/installations/{id}/repositories # 取 repository_id
POST /api/v1/market/plugins/parse/github                   # 服务端解析 metadata.yaml
POST /api/v1/market/plugins                                # 真正提交送审
```

唯一必须人工做一次的是 **浏览器登录 + 安装 AstrBot Cloud GitHub App**（hCaptcha + OAuth 绕不开）。
这一步由 `ampush login` 引导完成，之后长期走纯 HTTP，不再需要浏览器。

### 判定逻辑

用 `metadata.yaml` 的 `version` 作为唯一事实来源，按顺序判定：

1. 仓库被禁用 / 未开启自动送审 → 跳过
2. `metadata.yaml` 缺少 `version` → 跳过
3. 该版本已经提交过（本地去重库）→ 跳过
4. 云端存在待审核提交 → 跳过（避免占用审核名额）
5. 本地版本 == 线上版本，或本地版本不高于线上版本 → 跳过
6. 以上都不满足 → **送审**

送审前还有一道限流闸门：最小间隔、本地 24 小时上限、以及云端 `/market/options`
返回的账号级上限（默认 3 次/24h、3 个并发审核）。

---

## 快速开始

### 方式一：作为 AstrBot 插件

1. 把本仓库装进 AstrBot：
   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/kelai141/astrbot_auto_market_push
   ```
2. 在 **AstrBot WebUI → 插件 → 插件市场** 页面点右上角刷新/安装依赖（`requirements.txt` 会自动安装）。
3. 先去电脑上拿会话（见下方「获取会话」），然后把 `storage_state.json` 内容粘贴到
   插件配置的 **「会话 JSON」** 字段。
4. 在 **「监控的仓库列表」** 里填 `owner/repo`（一行一个，支持 `owner/repo#分支`），保存。

插件会随 AstrBot 启动自动开始轮询。聊天里可用：

```
/ampush status   查看会话、限流与各仓库状态
/ampush check    只检查将要执行的动作，不提交
/ampush push     立即执行一轮（忽略最小间隔）
/ampush start    启动后台轮询
/ampush stop     停止后台轮询
/ampush login    查看如何获取 / 更新会话
```

### 方式二：本地 / 服务器 CLI

```bash
pip install astrbot-auto-market-push
# 需要浏览器登录引导时：
pip install "astrbot-auto-market-push[browser]"
python -m playwright install chromium

cp config.example.yaml config.yaml   # 编辑 watch.repositories
ampush login                          # 一次性浏览器登录
ampush doctor                         # 体检：会话 / GitHub App / 每个仓库是否可达
ampush check                          # 只检查，不提交
ampush run                            # 常驻轮询
```

`ampush login` 会打开浏览器：完成 GitHub 登录后，**记得把要送审的仓库加入 AstrBot Cloud
GitHub App 的可访问列表**，否则送审会报 `github_app_installation_required`。

### 方式三：Docker

```bash
git clone https://github.com/kelai141/astrbot_auto_market_push
cd astrbot_auto_market_push

cp docker/config.docker.yaml data/config.yaml   # 编辑 watch.repositories
cp .state/storage_state.json data/              # 从本机 login 后拷过来

GITHUB_TOKEN=ghp_xxx docker compose up -d
docker compose logs -f
```

也可以完全不用配置文件，仅靠环境变量：

```bash
docker run -d --name ampush --restart unless-stopped \
  -v "$PWD/data:/data" \
  -e AMP_REPOSITORIES="kelai141/astrbot_plugin_foo,kelai141/astrbot_plugin_bar#dev" \
  -e GITHUB_TOKEN="ghp_xxx" \
  -e AMP_CLOUD_STORAGE_STATE="$(cat storage_state.json)" \
  astrbot-auto-market-push:latest run
```

### 方式四：GitHub Actions

**推荐**：直接用本仓库提供的复合 Action，放进你自己的插件仓库。

```yaml
# .github/workflows/astrbot-market.yml
name: AstrBot Market Auto Push

on:
  release:
    types: [published]
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch:

jobs:
  push:
    runs-on: ubuntu-latest
    steps:
      - uses: kelai141/astrbot_auto_market_push@main
        with:
          storage-state: ${{ secrets.AMP_CLOUD_STORAGE_STATE }}
          repositories: kelai141/astrbot_plugin_foo
```

也可以调用可复用工作流：

```yaml
jobs:
  push:
    uses: kelai141/astrbot_auto_market_push/.github/workflows/auto-push.yml@main
    with:
      repositories: kelai141/astrbot_plugin_foo
    secrets:
      AMP_CLOUD_STORAGE_STATE: ${{ secrets.AMP_CLOUD_STORAGE_STATE }}
```

> 把 `ampush login` 生成的 `storage_state.json` 全文存到仓库 Secret
> `AMP_CLOUD_STORAGE_STATE`。会话过期时重新登录并更新 Secret 即可。

---

## 获取会话

```bash
ampush login
```

浏览器里完成：

1. 用 GitHub（或邮箱）登录 `cloud.astrbot.app`；
2. 点击连接 GitHub；
3. 安装 **AstrBot Cloud GitHub App**，并把要送审的仓库加入可访问列表。

完成后会话保存在 `.state/storage_state.json`。三种用法对应的传递方式：

| 形态 | 传递方式 |
| --- | --- |
| AstrBot 插件 | 粘贴到「会话 JSON」配置项 |
| CLI / Docker | 放在 `.state/storage_state.json`（Docker 挂载到 `/data`） |
| GitHub Actions | 放进仓库 Secret `AMP_CLOUD_STORAGE_STATE` |

会话过期（收到 401「请先登录」）时，本工具会明确告警并通知，不会静默失败；重新跑一次
`ampush login` 即可。

---

## 配置

完整示例见 [`config.example.yaml`](config.example.yaml)。要点：

```yaml
watch:
  interval_seconds: 300          # 轮询间隔，最小 30
  repositories:
    - kelai141/astrbot_plugin_foo            # 简写
    - repo: kelai141/astrbot_plugin_bar      # 完整写法
      ref: dev
      overrides:                             # 覆盖送审字段
        tags: [工具, 自动化]
        category: 工具

submission:
  dry_run: false                 # true = 只演练不提交
  min_interval_seconds: 600
  max_submissions_per_24h: 3
  respect_cloud_limits: true

github:
  token: ${GITHUB_TOKEN:-}       # 强烈建议配置，否则限流 60 次/小时
```

所有字符串支持 `${ENV}` 与 `${ENV:-默认值}` 展开。

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `AMP_CONFIG` | 配置文件路径（不存在时降级为纯环境变量模式） |
| `AMP_REPOSITORIES` | 仓库列表，逗号或换行分隔，支持 `owner/repo#branch` |
| `AMP_CLOUD_STORAGE_STATE` | 内联的 storage_state JSON |
| `GITHUB_TOKEN` / `AMP_GITHUB_TOKEN` | 读取被跟踪仓库用的 Token |
| `AMP_NOTIFY_WEBHOOK` | 通知 Webhook 地址 |

---

## 命令参考

| 命令 | 作用 |
| --- | --- |
| `ampush login` | 一次性浏览器登录引导，生成会话 |
| `ampush whoami` | 校验会话，打印云端限流配置 |
| `ampush check` | 只检查，列出每个仓库将要执行的动作 |
| `ampush once` | 执行一轮（`--dry-run` 只演练，`--force` 忽略限流） |
| `ampush run` | 常驻轮询（`--dry-run` / `--force`） |
| `ampush doctor` | 诊断配置、会话、GitHub App 与监控目标 |
| `ampush state show` | 查看去重状态与送审历史 |
| `ampush state reset` | 清除去重状态，使下次重新评估 |

---

## 常见问题

**`github_app_installation_required`**
AstrBot Cloud 的 GitHub App 没装到该仓库，或没把仓库加进可访问列表。打开
`cloud.astrbot.app/publish`，在 GitHub 授权设置里补上。

**`github_connection_required`**
账号还没连接 GitHub。重新 `ampush login`，或到发布页点「连接 GitHub」。

**`GitHub 返回 401 Bad credentials`**
环境里存在一个已失效的 `GITHUB_TOKEN`。本工具会优先使用它，于是连公开仓库也读不了。
更新该变量，或清空后用匿名访问（公开仓库可用，限流降到 60 次/小时）。

**一直不动，`check` 显示「线上版本已是 x.x.x」**
说明 `metadata.yaml` 里的 `version` 没改。本工具按版本号判定，改代码但版本没变是不会送审的。

**提示「存在待审核提交」**
云端限制同时只允许少量待审提交。等上一笔审核出结果即可，工具会自动继续。

**送审成功但市场没更新**
送审只是「提交审核」，还需要平台侧审核通过才会在市场上生效。

**会话老是过期**
属正常现象。把 `ampush login` 产出的 `storage_state.json` 更新到配置/Secret 即可。

---

## 开发

```bash
git clone https://github.com/kelai141/astrbot_auto_market_push
cd astrbot_auto_market_push
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -e ".[dev,browser]"
ruff check . && ruff format --check .
pytest -q
```

包结构：

```
astrbot_auto_market_push/   核心引擎（纯 Python，无 LLM 依赖）
├── config.py               配置加载（YAML / 插件 schema / 环境变量）
├── models.py               领域模型与语义化版本比较
├── github_client.py        只读 GitHub REST
├── cloud_client.py         AstrBot Cloud API（cookie 会话）
├── watcher.py              变更检测与动作规划
├── submitter.py            送审执行与限流闸门
├── engine.py               编排与常驻轮询
├── state.py                SQLite 去重与历史
├── session.py              浏览器一次性登录引导
└── cli.py                  命令行入口
main.py                     AstrBot 插件适配层（薄封装）
```

---

## 免责声明

本项目通过公开 HTTP 接口操作 AstrBot Cloud，与 AstrBot 官方无隶属关系。
请遵守插件市场的相关规定，合理设置轮询间隔与送审频率，避免对服务造成压力。
因使用本工具产生的任何后果由使用者自行承担。

## 许可

[AGPL-3.0-or-later](LICENSE)。
本项目在运行时调用 AstrBot / AstrBot Cloud 的公开接口，并以插件形式与 AstrBot 框架协同工作，
故采用与上游一致的 AGPL-3.0 协议。
