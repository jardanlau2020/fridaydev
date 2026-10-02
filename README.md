# fridaydev

FridayDev（fridaydev.fr）免费服务自动续期 —— **watchdog 模式**。

每日跑一次（`cron: '5 7 * * *'`，即 UTC 07:05 / 北京 15:05）：只读面板上的续期
倒计时，剩 ≤5 天开始用 Telegram 提醒人去手点。**不自动点击**。

> ⚠️ 上面那个钟点**只是 cron 里写的**。GitHub 的 `schedule` 触发器是 best-effort，
> 免费 runner 上会排队 —— 实测（run #21–#31）落地时间全在 UTC 12:40–15:19
> （北京 20:40–23:19），迟 5.5–8 小时，偶尔更晚。watchdog 只提醒不续期，晚到
> 无害；但如果你要它落在某个具体钟点，得把 cron 往前挪几小时补偿。

## 为什么默认不自动续期

站点对免费续期强制 Cloudflare **Turnstile 互动验证**（`/php/renew_free_service.php`
返回 HTTP 428 + `captcha_required`，前端用 `window.fdCaptchaSolve()` render widget）。
无 token 时前端弹 `alert("Renouvellement impossible.")` 然后按钮复位 ——
旧版脚本把这种「点完没反应」当成续期成功，报了一堆假绿。

在 GHA runner IP 上实测过不去，累计试过：

- 5 种引擎：playwright / patchright / camoufox / seleniumbase UC / nodriver
- 真人鼠标序列（`human_click()`：分段移动 + 0.12s 按住）
- 20 组出口 × UA 组合

结论（2026-09-24）：继续每日自动点只会制造红灯和假信号，降级为 watchdog。
想再试就手动 dispatch 并选 `mode=renew`。

> 引擎首选 **patchright**（undetected Playwright）。证据：weirdhost 探针
> run 35501369694 —— 同一段点击代码，普通 playwright 点完 71 秒无反应，
> patchright 一点即有 `cf_clearance`。指纹差异就是 CF 认不认你。

## 续期矩阵

| 模式 | 触发 | 行为 | 退出码 |
|---|---|---|---|
| `watchdog`（默认） | schedule / dispatch | 只读倒计时，剩 ≤5 天发 TG | 未到窗口 0；**进入窗口 1**（提醒你去点） |
| `renew` | 仅手动 dispatch | 真点续期按钮（预期失败） | 成功 0；失败 1 |

`watchdog` 的静默约定：倒计时 **>5 天**一条消息都不发。天天收「还早」就是噪音，
看久了就会开始忽略 —— 那提醒就废了。剩 ≤3 天时第二行会从「未到續期窗口」
升级成「續期窗口即將開啟」。

### skip 文案的三处出口（已修复）

迁移时这里原本有个**已知文案缺陷**，被 `B3d / B5c / B14c` 三条断言钉住原样保留。
迁移验收绿了之后单独修掉了，现在三处出口分开建模、不再靠正则拆散文：

| 出口 | 判据 | 渲染出来 |
|---|---|---|
| watchdog 进窗口 | `open_now=True` | `🔔 … · 續期窗口已開` / `ℹ️ 面板 MM-DD 到期 · 請人手撳「Renouveler gratuitement」` |
| watchdog 剩 ≤3 天 | `human=True` | `🟢 … · 狀態良好（剩 N 日）` / `ℹ️ 面板 MM-DD 到期 · 續期窗口即將開啟` |
| renew 剩 ≤2 天 | `note="下次自動續"` | `🟢 … · 狀態良好（剩 N 日）` / `ℹ️ 下次自動續` |

**原缺陷**（值得记一笔，因为它不是排版洁癖而是真会丢服务器）：`build_tg()` 处理
`skip` 时只从 `detail` 里正则抽「仲有 X 日」，括号里逗号之后那句被整个丢掉。于是
「窗口已经开了、去手点」那条——**全系统唯一一条要人行动的通知**——渲染成：

```
🟢 FridayDev · 狀態良好
ℹ️ 面板 10-07 到期 · 續期窗口即將開啟
```

窗口都开了还说「即將開啟」，抬头还说「狀態良好」。看通知的人划走，第二天收到同一条，
再划走 —— 直到服务器到期被删。真正起作用的提醒只剩 **job 标红**本身。

修法：给「窗口已开」单开 `open_now=True` 出口（🔔 抬头 + 明写要手点）；「窗口没开、
只是 ≤3 天预备提醒」保持 🟢 且**只**升到「即將開啟」（这会儿页面上没有按钮，
叫人去撳是错的——原来 `run()` 传的 `要人手撳` 那句本身就是错的，现在连传都不传）；
renew 模式的「下次自動續」用 `note=` 带出来。`A12c` / `A12g` 两条断言保证
🔔 那条不会同时出现「狀態良好」或「即將開啟」、`open_now` 优先级高于 `human`。

## renew-kit

脚本与 workflow 都走 [renew-kit](https://github.com/jardanlau2020/renew-kit) `v0.5.3`：

```yaml
uses: jardanlau2020/renew-kit/.github/actions/renew@v0.5.3
```

依赖安装 / renewkit 注入 / 失败兜底统一由公共骨架处理，本仓库只管业务逻辑。
`notify-on-failure` 设为 `false` —— 脚本对每种结局都已经自己发过 TG，兜底会重复。

### 迁到 renew-kit 改了什么

| 原来 | 现在 |
|---|---|
| 自己读 `TG_BOT_TOKEN` / `TG_CHAT_ID` 拼 urlencode | `renewkit.notify.send(text, parse_mode="HTML")` |
| 各仓库各写一份 DRY_RUN 闸门 | 闸门只在 `notify.send()` 一处（`DRY_RUN=1` 只打印不发） |
| `_clip(t, limit)` | `renewkit.shorten` |
| `now_local()` | **删掉** —— 原文件定义了但从没调用过，纯死代码 |
| 模块级 `if not COOKIE: sys.exit(1)` | 挪进 `main()`。原来那是 import 期副作用，没 COOKIE 时 `import renew` 直接退进程，验收 harness 连模块都载不进来 |
| `run()` 里 3 处裸 `sys.exit(...)` | 改成 `return` 状态字，退出码统一由 `main()` 算 |
| 退出码 `0/1/2/3` | 收敛成 `0/1`（只有 `FAILED` 才 1） |
| 5 条出口里 3 条靠显式 `browser.close()` | 收敛到 `finally`，任何出口都不漏关 |
| `import json / os / urllib.*` | 删掉（迁移后已无人使用） |

退出码语义变化（有意为之）：

- `mode=watchdog` 撞上续期窗口：原来 `exit 2`，现在 `exit 1`。「红」这个信号保留
  —— job 照样标红提醒你去点，只是收敛到统一语义。
- cookie 失效：原来 `exit 3`，现在 `1`。

状态字到 renew-kit 结果语义的映射见 `renew.py` 的 `_STATUS_OUTCOME`：

| status | Outcome | 说明 |
|---|---|---|
| `ok` | `RENEWED` | 本次确实续上了 |
| `skip` | `SKIPPED` | 未到窗口 / watchdog 读数「还早」 |
| `manual` | `FAILED` | **进窗口了，必须人手点**。不是「读不到结果」，也不是「重试就好」的上游抖动 —— 是每次都会撞上的硬闸门。映射成非 `FAILED` 就是永久绿灯，watchdog 白装 |
| `cookie` | `FAILED` | Cookie 失效，要换 secret |
| `fail` | `FAILED` | 点不到按钮 / API 未回 success |
| `error` | `FAILED` | 未捕获异常 |

## 手动触发

```
Actions → Auto Renew FridayDev Service → Run workflow
  mode:    watchdog（默认，只读） / renew（真点，预期红）
  dry_run: true  → 只检查、不发 TG（用来验证流程与文案）
```

`dry_run` 下 `renewkit.notify.send()` 会把「本轮本应发送」的原文整条打出来，
但一个字节都不发出去 —— 所以演练时看日志就能确认通知内容对不对。

## 目录结构

```
renew.py                            续期 / 守门主脚本
.github/workflows/renew.yml         每日守门 + 手动 dispatch
.verify/verify_fridaydev.py         验收 harness（真子进程 + 场景矩阵）
```

## 验收测试

```bash
pip install pytest          # 可选，harness 自己也能跑
export RENEWKIT_PATH=/path/to/renew-kit     # 不在同工作区时才需要
python .verify/verify_fridaydev.py
```

覆盖四段：纯逻辑（`build_tg` / `parse_cookies` / 状态字映射）、watchdog 与 renew
的场景矩阵、workflow 与代码的接线一致性、以及**真子进程**跑一次 `main()`。
当前 **229 项全绿**（`✅ 全部通过（229 项）`）。

> harness 会给自己换一个假 `time` 模块（`sleep()` 空操作、`time()` 每次调用往前
> 跳 5 秒），否则脚本里的 `time.sleep(4)` 和那个 30 次「等 API 回包」轮询会让
> 每个 renew 场景真跑 40 秒起。跑的时候把依赖目录挂上 `PYTHONPATH`（绝对路径，
> `[D]` 段会开真子进程、相对路径会失效）：
>
> ```bash
> PYTHONPATH=/abs/path/to/_deps python .verify/verify_fridaydev.py
> ```

## Secrets

| 名称 | 必需 | 说明 |
|---|---|---|
| `COOKIE` | ✅ | fridaydev.fr 的 Cookie 串（`a=1; b=2`） |
| `TG_BOT_TOKEN` | 建议 | 不配就静默跳过通知 |
| `TG_CHAT_ID` | 建议 | 同上 |
