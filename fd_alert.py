#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FridayDev 告警统一出口：一天最多一条 + 正文必须可执行。

用法：
    from fd_alert import alert
    alert("session", detail="登入成功但 /services/ 被跳到 /404")

规则：
  · 一天（北京时间）内同一项目只发一条 TG。同日再来告警 → editMessageText
    追加到那一条的「同日重复」区，不新增消息。
  · 正文由 STEPS[kind] 生成，必带编号步骤（登入 → 复制 cookie → 写 secret →
    手动跑一次确认）。
  · 状态 fd_alert_state.json，由 workflow actions/cache 持久化（前缀 fdalert-）。
  · 任何异常只打印，不 raise：告警失败不能拖死续期。
"""
import datetime
import json
import os
import urllib.request

PROJECT = "FridayDev"
STATE = os.environ.get("FD_ALERT_STATE") or "fd_alert_state.json"
TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
CHAT = os.environ.get("TG_CHAT_ID") or ""
DRY = os.environ.get("DRY_RUN") == "1"
MAXLEN = 3800          # TG 上限 4096，留余量给「同日重复」尾巴
TZ_HOURS = 8           # 北京时间

PANEL = "https://fridaydev.fr"
LOGIN = PANEL + "/login/"
SERVICES = PANEL + "/services/"

HEAD = {
    "session": "🚨 FridayDev · 登录态失效（今日唯一一条）",
    "login": "🚨 FridayDev · 帐密登录失败（今日唯一一条）",
    "structure": "🚨 FridayDev · 页面结构变了（今日唯一一条）",
    "renew_failed": "🚨 FridayDev · 续期没生效（今日唯一一条）",
    "window_open": "🔔 FridayDev · 续期窗口已开（今日唯一一条）",
    "renew_ok": "✅ FridayDev · 续期成功（今日一条）",
    "renew_pending": "⏳ FridayDev · 已提交，服务端排队中（今日一条）",
    "unknown": "ℹ️ FridayDev · 结果未知（并入今日一条）",
    "probe_cookie": "🧪 FridayDev 探针 · COOKIE 已失效（今日唯一一条）",
    "probe_other": "🧪 FridayDev 探针 · 状态变化（今日唯一一条）",
}

STEPS = {
    "session": (
        "1. 浏览器开 %s ，用 FD_EMAIL 登录，再开 %s ，确认能看到服务卡和「Renouveler」按钮。\n"
        "2. F12 → Application → Storage → Cookies → fridaydev.fr，把**全部** cookie 复制成一串\n"
        "   `a=1; b=2; ...`（至少要有 PHPSESSID / laravel_session / XSRF-TOKEN）。\n"
        "3. GitHub → repo fridaydev → Settings → Secrets and variables → Actions → 更新 secret `COOKIE`，\n"
        "   整串粘贴，不要换行、不要带引号。\n"
        "4. Actions → 「Auto Renew FridayDev Service」→ Run workflow → mode=watchdog、dry_run 不勾 → 跑完看日志：\n"
        "   出现 `✅ 登入成功` + `服务: {...}` 即恢复；仍失败就把日志尾 20 行发给 load藤。\n"
        "兜底：第 1 步登录后仍被弹到 /404，是站方改版不是 cookie 问题 → 转阿华查前端。"
    ) % (LOGIN, SERVICES),
    "login": (
        "1. 无痕窗口手工验证帐密：%s ，用 FD_EMAIL / FD_PASSWORD 登录。\n"
        "2. 能登进去 → 不是密码问题，是出口 IP 被拦或站方加了验证 → 转阿滨查住宅出口（SUB_URL / 节点）。\n"
        "3. 登不进去 → 去邮箱走「忘记密码」重置，再把新密码写进 secret `FD_PASSWORD`\n"
        "   （Settings → Secrets → Actions → FD_PASSWORD，覆盖旧值）。\n"
        "4. Actions → renew.yml → Run workflow（mode=watchdog）→ 日志出现 `✅ 登入成功` 才算完。"
    ) % LOGIN,
    "structure": (
        "1. 先分清是登录态还是真改版：手工登录后开 %s ，看页面上有没有 `Mes services` 和 `.btn-renew`。\n"
        "2. 有 → 只是选择器/文案变了 → 转阿华按当前 DOM 改 `renew_v2.py` 的 `parse_service()`。\n"
        "3. 没有、或被弹到 /404 → 是登录态 → 按「登录态失效」那 4 步走（重贴 COOKIE）。\n"
        "4. 改完手动 dispatch 一次 renew.yml，日志出现 `服务: {` 才算恢复。"
    ) % SERVICES,
    "renew_failed": (
        "1. 手工打开 %s ，看「Renouveler」按钮是否可点、到期日是多少（截图留证）。\n"
        "2. 可点 → 人手点一次续期，页面自己过 Turnstile；成功后回来确认到期日已推进。\n"
        "3. 不可点 → 说明还没进续期窗口（剩 >2 日），不用管，等窗口开。\n"
        "4. 点了没反应 / 弹 428 → 把截图和浏览器 Console 报错发给 load藤，转阿华。"
    ) % SERVICES,
    "window_open": (
        "1. 打开 %s 。\n"
        "2. 点「Renouveler」→ 选免费方案 → 等页面自己过 Turnstile。\n"
        "3. 回到 /services/ 刷新，确认到期日已往后推。\n"
        "4. 若卡在 Turnstile 验证失败：换一个网络（手机热点）再试一次，仍失败转阿滨。"
    ) % SERVICES,
    "probe_cookie": (
        "1. 浏览器开 %s 登录，再开 %s 确认服务卡可见。\n"
        "2. F12 → Application → Cookies → fridaydev.fr，全部 cookie 复制成一串 `a=1; b=2; ...`。\n"
        "3. repo fridaydev → Settings → Secrets → Actions → 更新 secret `COOKIE`（整串，无换行）。\n"
        "4. Actions → 「FD Turnstile Probe (read-only)」→ Run workflow → 日志出现 `[0] URL = %s` 即恢复。"
    ) % (LOGIN, SERVICES, SERVICES),
    "probe_other": (
        "1. 打开该次 run 的日志，找 `[API 回應]` 那一行，看 status 是 428 还是别的。\n"
        "2. 428 + captcha_required → 是 Turnstile 拦截，不是 cookie 问题 → 转阿滨（出口）或阿华（前端）。\n"
        "3. 401/403/跳登录 → 按「COOKIE 已失效」4 步重贴 cookie。\n"
        "4. 探针只读，不需要立刻处理；把 run 链接发给 load藤即可。"
    ),
    "renew_ok": (
        "1. 打开 %s 看一眼服务卡，确认到期日已经往后推（截图留证）。\n"
        "2. 记住新的到期日，续期窗口下一次开在到期前 5 天。\n"
        "3. 不需要做别的动作，脚本明天照常巡检。\n"
        "4. 若到期日没变，把这条消息和 run 链接发给 load藤。"
    ) % SERVICES,
    "renew_pending": (
        "1. 过 20–30 分钟再打开 %s 刷一次，看到期日有没有往后推。\n"
        "2. 推了 → 什么都不用做，今天的续期已经生效。\n"
        "3. 没推 → 到 Actions 手动跑一次 renew.yml，看这次日志的「当前页面日期」。\n"
        "4. 连跑两次都没推 → 把日志尾 20 行发给 load藤。"
    ) % SERVICES,
    "unknown": (
        "1. 打开 %s ，确认能看到服务卡和到期日。\n"
        "2. 能看到 → 说明只是本轮页面没加载出来，不用处理，等下一次巡检。\n"
        "3. 看不到 / 被弹到 /404 → 按「登录态失效」处理，重贴 COOKIE。\n"
        "4. 拿不准就把 run 链接发给 load藤。"
    ) % SERVICES,
}


def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_HOURS)


def _load():
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            return json.loads(fh.read() or "{}")
    except Exception:
        return {}


def _save(st):
    try:
        with open(STATE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(st, ensure_ascii=False))
    except Exception as exc:
        print("[alert] 状态写入失败: %s" % exc, flush=True)


def _api(method, payload):
    """返回 (ok, message_id)。"""
    if DRY or not TOKEN or not CHAT:
        print("[alert][DRY] %s: %s" % (method, json.dumps(payload, ensure_ascii=False)[:1500]), flush=True)
        return True, payload.get("message_id") or 1
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode() or "{}")
        mid = (res.get("result") or {}).get("message_id")
        print("[alert] %s ok mid=%s" % (method, mid), flush=True)
        return True, mid
    except Exception as exc:
        print("[alert] %s 失败: %s" % (method, exc), flush=True)
        return False, None


def build(kind, detail="", extra=""):
    return "%s\n现象：%s\n\n【你要做的】\n%s%s" % (
        HEAD.get(kind, "🚨 FridayDev · %s" % kind), detail or "—",
        STEPS.get(kind, "1. 把这条告警和 run 链接发给 load藤，由他分派。"), extra)


def _api_photo(caption, photo_path):
    """sendPhoto（multipart）。只给 urgent 用：穿透一天一条时带图才有意义。"""
    if DRY or not TOKEN or not CHAT:
        print("[alert][DRY] sendPhoto: %s" % caption[:300], flush=True)
        return True, 1
    import uuid
    try:
        boundary = "----fdalert" + uuid.uuid4().hex
        with open(photo_path, "rb") as fh:
            img = fh.read()
        parts = []

        def _field(name, value):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, name, value)).encode())

        _field("chat_id", CHAT)
        _field("caption", caption[:1024])
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"photo\"; "
                      "filename=\"result.png\"\r\nContent-Type: image/png\r\n\r\n"
                      % boundary).encode())
        parts.append(img)
        parts.append(("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendPhoto" % TOKEN,
            data=b"".join(parts),
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode() or "{}")
        mid = (res.get("result") or {}).get("message_id")
        print("[alert] sendPhoto ok mid=%s" % mid, flush=True)
        return True, mid
    except Exception as exc:
        print("[alert] sendPhoto 失败: %s" % exc, flush=True)
        return False, None


def alert(kind, detail="", extra="", urgent=False, photo=None):
    """发告警：一天最多一条，同日重复合并进同一条。返回是否成功。

    urgent=True：无视「当天已发过」，立即新发一条（失败 / 需人工必须能穿透）。
                 带 photo 时走 sendPhoto（穿透 + 带图才有意义）。
    photo：截图路径，只在 urgent 时使用；非 urgent 一律纯文字，免得一天一条被破。
    """
    today = _now().strftime("%Y-%m-%d")
    st = _load()
    if st.get("date") != today:
        st = {"date": today, "message_id": None, "kinds": {}, "text": ""}
    st["kinds"][kind] = st.get("kinds", {}).get(kind, 0) + 1

    if urgent:
        text = ("🚨【需人工】" + build(kind, detail, extra))[:MAXLEN]
        ok, mid = False, None
        if photo and os.path.isfile(photo):
            ok, mid = _api_photo(text, photo)
        if not ok:
            ok, mid = _api("sendMessage", {"chat_id": CHAT, "text": text,
                                           "disable_web_page_preview": True})
        # 故意不写 st["message_id"]：urgent 那条是独立的【需人工】消息，
        # 让它当「当天那条普通消息」的话，后面的成功/排队中会去 edit 它，
        # 变成「需人工失败」下面挂着「同日重复：续期成功」，直接误导判断。
        if ok and mid:
            st.setdefault("urgent_ids", []).append(mid)
        _save(st)
        return ok

    if st.get("message_id"):
        note = "\n\n＋ 同日重复：%s（今日第 %d 次，最后 %s）" % (
            kind, st["kinds"][kind], _now().strftime("%H:%M"))
        new_text = (st.get("text") or "")[: MAXLEN - len(note)] + note
        ok, _ = _api("editMessageText", {"chat_id": CHAT, "message_id": st["message_id"], "text": new_text})
        if ok:
            st["text"] = new_text
        _save(st)
        return ok

    text = build(kind, detail, extra)[:MAXLEN]
    ok, mid = _api("sendMessage", {"chat_id": CHAT, "text": text, "disable_web_page_preview": True})
    if ok:
        st["message_id"] = mid
        st["text"] = text
    _save(st)
    return ok
