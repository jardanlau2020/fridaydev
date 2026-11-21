# fd_alert.py 用法（给接 PR #5 的人）

`fd_alert.py` 是**独立模块**，不依赖 renew_v2.py / probe_turnstile.py 任何东西。
上游 `ldsceoceo/Friday-renew` 的 app.py 只有裸 `sendMessage`，没有一天一条、没有
`editMessageText` 合并，所以告警层没有可整份采用的上游，这个模块要保留。

## 接口

```python
from fd_alert import alert
alert(kind, detail="")     # 返回 bool；任何异常只打印不抛，不阻塞主流程
```

7 个 kind：`session` / `login` / `structure` / `renew_failed` / `window_open` /
`probe_cookie` / `probe_other`。正文由 `STEPS[kind]` 生成，每个都带 4 条编号步骤，
`session` 和 `probe_cookie` 明确写 secret 名 `COOKIE`。

## 行为

- 按北京时间一天一条：当天第一条走 `sendMessage`，同日后续走 `editMessageText`
  追加「同日重复」区，不新增消息。
- 状态 `fd_alert_state.json`：`{date, message_id, kinds:{kind:count}, text}`。
- workflow 里用 `actions/cache/restore|save`（前缀 `fdalert-`，key `fdalert-${{ github.run_id }}`）
  持久化；多个 workflow 共用同一前缀，合起来一天也只发一条。
- 环境变量：`TG_BOT_TOKEN` / `TG_CHAT_ID`，`DRY_RUN=1` 只打印不发送，
  `FD_ALERT_STATE` 可覆盖状态文件路径。

## 接到 app.py 上的改法（PR #5 合完之后）

把 app.py 里所有 `send_telegram_message(...)` / `send_telegram_photo(...)` 的调用点
换成 `alert(kind, detail=...)`，映射建议：

| 原调用点 | 改成 |
|---|---|
| cookie / auth.php 未登录 | `alert("session", detail=...)` |
| 帐密登录失败 | `alert("login", detail=...)` |
| 读不到服务卡 / 选择器变 | `alert("structure", detail=...)` |
| 按钮已可点（watchdog） | `alert("window_open", detail=...)` |
| 点了续期但到期日没变 | `alert("renew_failed", detail=...)` |
| 续期成功 | **不发 TG**，只 log（成功不是告警） |

`send_telegram_photo` 那种带截图的：截图仍上传 artifact，TG 走 `alert` 纯文本，
别在告警层再开一条 sendPhoto 通道，否则一天一条又破。

## 验收（已验证过，复现用）

1. `DRY_RUN=1` 跑一次 → 正文含 `【你要做的】` + 4 条编号步骤 + secret 名 `COOKIE`
2. 同日再跑 → 日志是 `editMessageText`，无第二次 `sendMessage`，state `kinds` 计数 2
3. 换日期 / 删 state → 重新 `sendMessage`