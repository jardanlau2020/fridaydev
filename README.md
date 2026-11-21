## 🚀 Friday 自动续期（GitHub Actions，单账号 Cookie 版）

定时登录 [Friday](https://fridaydev.fr/services/) 自动续期免费服务（5 天手动续一次，脚本每天巡检）。

### 🔐 Secrets 配置说明

| Secret 名称   | 是否必填 | 说明                                                        |
|---------------|----------|-------------------------------------------------------------|
| COOKIE_VALUE  | ✅ 必填（旧 COOKIE 兼容） | Friday(fridaydev.fr) 登录 Cookie；首选填 `remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d=xxx`，可再附 `; user_id=yyy`；也可只填值 |
| EMAIL         | ❌ 可选  | 通知用备注名，可随意填写                                    |
| GH_TOKEN      | ❌ 可选  | GitHub(classic) token，用于 Cookie 刷新后自动回写，以 `ghp_` 开头 |
| NODE_LINK     | ❌ 可选  | 代理链接（vless/vmess/trojan/hysteria2/tuic/anytls/socks5 等） |
| TG_BOT_TOKEN  | ❌ 可选  | Telegram Bot Token（用于发送通知，含截图）                  |
| TG_CHAT_ID    | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）                 |

> * 长凭证是 `remember_web_` 打头的那条（后缀随站点配置变，脚本按前缀认，不用你改代码）；剩余不足 3 天会 TG 预警，彻底失效时 TG 告警"Cookie 已失效"，需手动重拷。
> * 站点若还在种 `PHPSESSID` / `user_id`，一起贴进来也照样兼容，脚本每次跑通会自动回写最新串。
> * 账密备用登录为二期扩展位，MVP 仅 Cookie 登录。

━━━━━━━━━━━━━━━━━━━━━━

## 部署步骤

1. 新建仓库（建议名 `Friday-Renew`），把 `app.py`、`.github/workflows/renew.yml`、`README.md` 推上去，在 Actions 菜单允许工作流。
2. 在 `Settings` ➡ `Secrets and variables` ➡ `Actions` 里添加上方 Secrets。
3. 去 Actions 手动试运行一次；cron 为每天 UTC 7 点（北京时间 15 点），可按需调整。

### COOKIE 获取

1. 浏览器登录 https://fridaydev.fr/ （登录时勾上"记住我"之类的选项，否则只有两小时会话 Cookie）。
2. 按 F12 ➡ 应用程序（Application）➡ 左侧 Cookie ➡ `https://fridaydev.fr`。
3. 找到 `remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d` 那条（名称以 `remember_web_` 开头，过期时间在未来几个月的那条就是它），值原样复制、不要解码，拼成：
   `remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d=xxx`，填入 `COOKIE_VALUE`；若还看得到 `user_id`，一并附上写成 `remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d=xxx; user_id=yyy`。

> * 别拷成 `control.fridaydev.fr`（Pterodactyl 面板）下的同名 Cookie 了，面板那条只在面板有效，脚本会在日志里提示"auth.php 返回未登录"。

### 获取 `GH_TOKEN`

参考 Bothosting-Renew：GitHub 头像 ➡ Settings ➡ Developer settings ➡ Personal access tokens(classic) ➡ Generate new token(classic)，权限勾 repo 相关即可。

## 注意事项

* Friday 免费档续期按钮双态：未到时间显示倒计时 `Renouvelable dans N jour(s)`，只通知不点；到期后同一位置文案会变化（如 `Renouveler gratuitement (N jours)`），那才是入口——点后弹 CF 验证组件（Turnstile），节点纯净自动过盾即默认续期完成。浏览器 headful 跑在 xvfb 上（Turnstile 需真渲染）。真点了但日期没变才报"可能未成功"人工检查。
* cron 运行时间不一定准时，以 Actions 实际触发为准。

## ⚠️ 免责声明

* 本程序仅供学习了解，非盈利目的。
* 遵守部署服务器所在地、所在国家和用户所在国家的法律法规，作者不对使用者任何不当行为负责。
