"""FridayDev 自動續期（watchdog 模式為主）—— 已迁移到 renew-kit。

业务逻辑、页面选择器、点击序列、watchdog 判据、TG 文案全部原样保留；只把
「各仓库都在重复实现」的那几块换成 renew-kit：

    · now_local()          -> 删掉。原文件定义了但**从头到尾没调用过**，纯死代码。
    · _clip()              -> renewkit.shorten（会顺手把换行压平；本文件用它的
                              地方 detail 都是单行，无影响）
    · tg_send()            -> renewkit.notify.send（DRY_RUN 闸门、4000 字截断、
                              「发送失败不影响结论」全在 kit 里）。本文件不再自己
                              读 TG_BOT_TOKEN / TG_CHAT_ID，也不再自己拼
                              urlencode + parse_mode。
    · 模块级 `if not COOKIE: sys.exit(1)`
                           -> 挪进 main()。原来那个是 **import 期副作用**：没有
                              COOKIE 时 `import renew` 直接退出进程，验收 harness
                              连模块都载不进来。
    · run() 里 3 处裸 sys.exit(...)
                           -> 改成 return 状态字，由 main() 统一算退出码。
    · 退出码 0/1/2/3       -> 收敛成 renewkit 的 0/1（只有 FAILED 才 1）。
    · 顺手清掉 4 个已失效的 import（json / os / urllib.request / urllib.parse）。

刻意没走 kit 的两处：
    · build_tg()。本仓库的 TG 文案是「方案 B 兩行制」，而且要区分 key / human
      两种上下文；renewkit 的 TargetResult 只建模 name/outcome/expire/detail，
      硬套 renderer= 就得往 detail 里塞已经排好版的文本 —— 比留着更难懂。
    · RenewReport.finish()。本仓库是「一判定出状态就立刻发一条」，没有 run 级
      总览；套 finish() 会把同一段文案再发一遍。所以只取 report.exit_code。

退出码语义变化（有意为之）：
    · 原来 `mode=watchdog` 撞上续期窗口是 exit 2，现在是 exit 1。
      「红」这个信号保留 —— job 照样标红提醒你去撳，只是收敛到统一语义。
    · 原来 cookie 失效是 exit 3，现在是 1。同理。
    · 原来「未发现续期按钮」走 exit 0 且文案说「可能已成功续期」，现在映射成
      SKIPPED（exit 0 不变），文案照旧。

文案修复（迁移验收通过后单独做的一次改动，见 README「skip 文案的三处出口」）：
    迁移时把原版 skip 文案的缺陷**原样保留**了（B3d / B5c / B14c 钉住），
    验收绿了之后才动手。原缺陷是：build_tg() 处理 skip 时只从 detail 里正则抽
    「仲有 X 日」，括号里逗号之后那句被整个丢掉；而且「窗口已经开了」那条的第一行
    还说「狀態良好」、第二行说「即將開啟」—— 那是全系统唯一一条**要人行动**的通知，
    却说「一切正常」，看的人划走，然后服务器到期被删。
    改法：给「窗口已开」单开一个 `open_now=True` 出口（🔔 抬头 + 明写要手点），
    ≤3 天的「预备提醒」保持 🟢 但只升到「即將開啟」（窗口没开就不该叫人去撳），
    renew 模式的「下次自動續」用 `note=` 带出来。三处出口都不再靠正则拆散文。

引擎选择（2026-09-20 的结论，原样保留）：
    首选 patchright（undetected Playwright）。证据：weirdhost 探针 run 35501369694
    —— 同一段点击代码，普通 playwright 撳完 71 秒乜都冇，patchright 一撳即有
    cf_clearance。指紋差別就係 CF 認唔認你。
"""
from __future__ import annotations

import re
import sys
import time
import traceback

from renewkit import Outcome, RenewReport, TargetResult, shorten
from renewkit import env, notify

try:
    from patchright.sync_api import sync_playwright

    ENGINE = "patchright"
except Exception:
    from playwright.sync_api import sync_playwright

    ENGINE = "playwright"

SERVICE = "FridayDev"
PANEL_URL = "https://fridaydev.fr/services/"
PANEL_TARGET = "FridayDev 服務"

# ── 2026-10-06：headless → 有頭 + 真 Chrome（HidenCloud 10-05 嗰單同款配方）──────
# 硬證據 run 35510050597：428 已攞到 captcha_site_key，但頁內 window.turnstile
# 全程 undefined、iframes=[] —— Turnstile 喺 headless Chromium 拒絕渲染
# （error-callback「Le test a échoué」，71 秒後 token 未到手）。
# HidenCloud 用 xvfb + channel="chrome" + headless=False 喺同一個 GHA runner IP 過到；
# 我哋 09-24「5 引擎全敗」嘅結論係喺 headless=True 下得出，從未試過有頭真 Chrome。
# 預設有頭（workflow 用 xvfb-run 提供 DISPLAY）；要無頭先設 FD_HEADLESS=true。
# 讀環境變量走 renewkit.env.get（規程：別直接摸 os.environ，os 已被清走）。
HEADLESS = (env.get("FD_HEADLESS") or "false").strip().lower() in ("1", "true", "yes")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
window.chrome.csi = window.chrome.csi || function () { return {}; };
if (!window.chrome.app) {
  window.chrome.app = { isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANT_RUN: 'cannot_run', UNINSTALLED: 'uninstalled', RUNNING: 'running' } };
}
try {
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({ state: (window.Notification && Notification.permission) || 'prompt' })
        : origQuery(p);
  }
} catch (e) {}
"""

# ── 状态字 → renewkit 结果语义 ───────────────────────────────────────────
#   · ok      -> RENEWED  本次确实续上了
#   · skip    -> SKIPPED  未到窗口 / watchdog 正常读数为「还早」
#   · manual  -> FAILED   **进窗口了，必须人手撳**。
#                 为什么不用 UNKNOWN/TRANSIENT：这不是「读不到结果」，也不是
#                 「重试就好」的上游抖动 —— 它是每次都会撞上的硬闸门
#                 （Turnstile 喺 GHA runner IP 实测过唔到，五種引擎 + 20 組出口
#                 全失败）。映射成非 FAILED 就等于永久绿灯，watchdog 白装。
#                 这也是 run #28 那个「设计内红」的来源。
#   · cookie  -> FAILED  Cookie 失效，必须换 secret
#   · fail    -> FAILED  点击不到 / API 未回 success
#   · error   -> FAILED  未捕获异常
_STATUS_OUTCOME = {
    "ok": Outcome.RENEWED,
    "skip": Outcome.SKIPPED,
    "manual": Outcome.FAILED,
    "cookie": Outcome.FAILED,
    "fail": Outcome.FAILED,
    "error": Outcome.FAILED,
}


def _outcome_of(status: str) -> Outcome:
    """查表；没见过的 status 一律 UNKNOWN（不标红，但报告里会说「未确认」）。"""
    return _STATUS_OUTCOME.get(status, Outcome.UNKNOWN)


def _report(items) -> RenewReport:
    """把 ``(name, status, detail[, expire])`` 列表包成 RenewReport（只取 exit_code）。"""
    report = RenewReport(SERVICE)
    for item in items:
        name, status, detail = item[0], item[1], item[2]
        expire = item[3] if len(item) > 3 else None
        report.add_result(TargetResult(
            name=name,
            outcome=_outcome_of(status),
            expire=expire,
            detail=shorten(detail or "", 120),
        ))
    return report


def esc_html(t) -> str:
    """逃逸 Telegram HTML parse_mode 的保留字符。

    原文件叫 _esc_html，逻辑一字未改。动态内容（面板日期、API 记录、异常信息）
    里只要有一个 `<`，整条通知会被 Telegram 400 丢掉 —— 静默失败，比不转义更糟。

    TODO(收进 kit)：weirdhost_renew.py 里有一份一模一样的 esc_html。
    两处重复就够格进 renewkit 了（`shorten` 当初也是这么进去的），
    但那是要发 v0.5.4 + 动 8 个仓库的 pin，跟本次迁移拆开做。
    """
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text: str) -> bool:
    """TG 通知（有配置先發；失敗唔影響主流程）。

    闸门、截断、异常兜底全在 renewkit.notify.send 里：
      · DRY_RUN=1 时只打印不发（唯一一处闸门）
      · 超 4000 字自动截断
      · 缺 token / 网络失败只回 False，不抛
    """
    return notify.send(text, parse_mode="HTML")


def fmt_date(v) -> str:
    """面板日期 DD/MM/YYYY → MM-DD（解析唔到就回空字串）。"""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(v or ""))
    return f"{m.group(2)}-{m.group(1)}" if m else ""


def build_tg(action, detail="", expire="", key="", human=False,
             open_now=False, note="") -> str:
    """方案 B (極致精簡人話版): 每台精準兩行，徹底消滅頂部計數器。

    三个 skip 语境分开建模，别再靠正则拆散文：

    · ``open_now=True`` —— 续期窗口**已经开了**，页面上就有按钮，只差人去撳。
      全系统唯一一条要人行动的通知，所以单独用 🔔 抬头 + 明写「請人手撳」。
    · ``human=True``    —— 窗口还没开，但倒计时 ≤3 天，属于「预备提醒」。
      抬头仍是 🟢，第二行升到「續期窗口即將開啟」。
    · ``note="..."``    —— 其余情况想在第二行补一句（如 renew 模式的「下次自動續」），
      顶替默认的「未到續期窗口」。
    """
    name = SERVICE
    if action == "ok":
        l1 = f"✅ {name} · 成功續期" + (f"至 {expire}" if expire else "")
        l2 = "ℹ️ 服務已自動展期"
        return esc_html(f"{l1}\n{l2}")
    elif action == "skip":
        # 第一行的「（剩 N 日）」仍然从 detail 里捞 —— 这个位置适合正则，
        # 因为天数本来就是从页面读数来的，不是文案。
        rem = ""
        m = re.search(r"仲有\s*([^，）]+)", detail or "")
        if m:
            rem = f"（剩 {m.group(1)}）"
        info_parts = []
        if key:
            info_parts.append(f"{key} 到期" if "到期" not in key else key)
        elif expire:
            info_parts.append(f"{expire} 到期")
        if open_now:
            # 窗口已开：抬头换 🔔、第二行点名要手点。
            # 修复前的样子是「🟢 狀態良好 / 續期窗口即將開啟」—— 窗口都开了还说
            # 「即將開啟」，用户划走，服务器到期被删。harness B3d 钉住新文案。
            info_parts.append("請人手撳「Renouveler gratuitement」")
            return esc_html(f"🔔 {name} · 續期窗口已開{rem}\n"
                            f"ℹ️ " + " · ".join(info_parts))
        l1 = f"🟢 {name} · 狀態良好{rem}"
        if human:
            info_parts.append("續期窗口即將開啟")
        elif note:
            info_parts.append(note)
        else:
            info_parts.append("未到續期窗口")
        return esc_html(f"{l1}\nℹ️ " + " · ".join(info_parts))
    else:
        l1 = f"🚨 {name} · 續期未完成"
        reason = shorten(detail or "執行失敗", 60)
        l2 = f"⚠️ {reason} · 請登入面板手動處理"
        return esc_html(f"{l1}\n{l2}")


def parse_cookies(raw: str) -> list[dict]:
    """`a=1; b=2` -> Playwright 的 cookie 列表（domain 钉在 fridaydev.fr）。

    原来这段直接摊在模块级（依赖 import 时就存在的 COOKIE），挪成纯函数后
    harness 可以直接喂字符串测。
    """
    out = []
    for item in (raw or "").split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            out.append({"name": k, "value": v,
                        "domain": "fridaydev.fr", "path": "/"})
    return out


def current_mode() -> str:
    """FD_MODE。workflow **默认会传 renew**（2026-10-06 起全自動）；
    只有裸跑 `python renew.py` 乜都唔設先落回 watchdog —— 呢個係本地安全網，
    保證手動試跑唔會一嚟就撳續期。

    读环境变量走 renewkit.env.get（会自动 strip）；别直接摸 os.environ ——
    workflow 里 `${{ inputs.mode || 'renew' }}` 求值成空串时，裸
    os.environ.get 拿到的是 "" 而不是默认值。
    """
    return (env.get("FD_MODE", "watchdog") or "watchdog").strip().lower()


def human_click(page, x, y):
    """2026-09-20：似真人嘅點擊序列。

    證據：run 35502154511（已換 patchright）用 `page.mouse.click()` 撳 Turnstile，
    widget 即刻回 `Le test a échoué. Rechargez la page et réessayez.`；
    mouse.click() 係 down→up 零延遲、無中途移動 —— 典型機器人特徵。
    """
    page.mouse.move(max(0, x - 60), max(0, y - 25), steps=4)
    time.sleep(0.25)
    page.mouse.move(x - 8, y - 3, steps=6)
    time.sleep(0.35)
    page.mouse.move(x, y, steps=3)
    time.sleep(0.45)
    page.mouse.down()
    time.sleep(0.12)
    page.mouse.up()


def safe_click(locator, label="", timeout=15000):
    """点击；被遮罩层拦截时退回 JS 原生 click（绕过 hit-testing）。

    2026-09-20 定案：站点会插入全屏 CGU 遮罩（#fd-cgu-modal, z-index 30000），
    Playwright 的常规 click 会因为 "intercepts pointer events" 直接 timeout
    （run 35441369037）。JS 原生 click 不受 hit-testing 限制，做兜底。
    """
    try:
        locator.first.click(timeout=timeout)
        return True
    except Exception as e:
        print(f"⚠️ 常规点击{(' [' + label + ']') if label else ''}失败"
              f"({repr(e)[:90]})，改用 JS 点击兜底...")
        try:
            locator.first.evaluate("el => el.click()")
            return True
        except Exception as e2:
            print(f"❌ JS 点击也失败: {repr(e2)[:90]}")
            return False


def dismiss_cgu_modal(page):
    """关掉 fridaydev 的 CGU 接受弹窗（#fd-cgu-modal）。

    站点新增「Nos conditions d'utilisation ont été modifiées」全屏遮罩，
    会 intercept pointer events，令续期按钮点唔到。优先点接受（服务器端
    会记低 consent），其次点关闭叉，最后强制移除遮罩节点。
    """
    try:
        if page.locator("#fd-cgu-modal").count() == 0:
            return False
        print("📜 检测到 CGU 弹窗，尝试关闭/接受...")
        for sel in ["#fd-cgu-accept", "#fd-cgu-later", "#fd-cgu-modal .fd-cgu-close"]:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                if safe_click(btn, sel, timeout=5000):
                    print(f"✅ 已点击 CGU 弹窗按钮: {sel}")
                    time.sleep(2)
                    if page.locator("#fd-cgu-modal").count() == 0:
                        print("✅ CGU 弹窗已消失")
                        return True
        page.evaluate("() => { const m = document.getElementById('fd-cgu-modal');"
                      " if (m) m.remove(); }")
        print("🧹 CGU 弹窗仍在，已强制移除遮罩节点（避免拦截点击）")
        time.sleep(1)
        return True
    except Exception as e:
        print(f"⚠️ 处理 CGU 弹窗失败(忽略): {repr(e)[:120]}")
        return False


def wait_turnstile_token(page, timeout=75):
    """等 Turnstile 互動驗證通過。

    2026-09-20 定案：`/php/renew_free_service.php` 對免費續期每次都要過反機械人測試
    （HTTP 428 + `captcha_required`，前端用 `window.fdCaptchaSolve()` render
    Cloudflare Turnstile）。無 token 就 alert("Renouvellement impossible.")
    然後按鈕復位 —— 舊版腳本照當「續期成功」（run 35496183082/35496473395）。
    呢度只係點個 widget 嘅 checkbox（managed 模式多數自動過），唔係 solver。
    """
    deadline = time.time() + timeout
    clicked = False
    last_log = 0.0
    last_click = 0.0
    start = time.time()
    while time.time() < deadline:
        try:
            got = page.evaluate(
                """() => { const e = document.querySelector('input[name="cf-turnstile-response"]'); """
                """return !!(e && e.value && e.value.length > 20); }"""
            )
            if got:
                print("✅ Turnstile 驗證已通過（token 到手）")
                return True
        except Exception:
            pass
        # 每 5 秒打印一次驗證部件狀態（站方用 .fd-captcha-backdrop 彈窗 render Turnstile）
        if time.time() - last_log > 5:
            last_log = time.time()
            try:
                st = page.evaluate("""() => {
                    const q = (s) => document.querySelector(s);
                    const wid = q('.fd-captcha-widget');
                    const box = wid ? (() => { const r = wid.getBoundingClientRect();
                        return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })() : null;
                    const inp = q('input[name="cf-turnstile-response"]');
                    return {
                        t: %d,
                        backdrop: !!q('.fd-captcha-backdrop'),
                        widget_box: box,
                        msg: (q('.fd-captcha-msg') || {}).textContent || null,
                        turnstile_api: typeof window.turnstile,
                        token_len: inp ? (inp.value || '').length : -1,
                        iframes: Array.from(document.querySelectorAll('iframe')).map(f => (f.src || '').slice(0, 60)).slice(0, 5)
                    };
                }""" % int(time.time() - start))
                print(f"   [CAPTCHA] {st}")
            except Exception as e:
                print(f"   [CAPTCHA] 狀態讀取失敗: {repr(e)[:100]}")
        # 2026-09-20（run 35502154511 之後）：站方 widget 係 Cloudflare「managed」模式，
        # 好多時唔使撳都會自己過；我哋一撳就即刻換嚟 "Le test a échoué"。所以改成
        # 先靜觀 25 秒（完全唔撳），唔得先人手式撳一次。
        quiet = 25
        if time.time() - start < quiet:
            time.sleep(1)
            continue
        try:
            fr = page.locator("iframe[src*='challenges.cloudflare.com']")
            if fr.count() > 0:
                box = fr.first.bounding_box()
                if box and box.get("width", 0) > 0 and not clicked:
                    cx = box["x"] + 30
                    cy = box["y"] + box["height"] / 2
                    human_click(page, cx, cy)
                    clicked = True
                    print("🖱️ 已點 Turnstile widget，等驗證通過…")
        except Exception:
            pass
        # 2026-09-20：實測 .fd-captcha-widget 有 render（378x73）但頁面 **完全冇 iframe**
        # （Turnstile 收喺 closed shadow DOM）→ 退返用容器座標直接點 widget 左邊 checkbox 位。
        if not clicked and time.time() - last_click > 10:
            try:
                wbox = page.evaluate("""() => { const w = document.querySelector('.fd-captcha-widget');
                    if (!w) return null; const r = w.getBoundingClientRect();
                    return (r.width > 50) ? [r.x, r.y, r.width, r.height] : null; }""")
                if wbox:
                    cx = wbox[0] + 30
                    cy = wbox[1] + wbox[3] / 2
                    human_click(page, cx, cy)
                    last_click = time.time()
                    print(f"🖱️ 已點驗證 widget 容器（{int(cx)},{int(cy)}），等驗證通過…")
            except Exception:
                pass
        time.sleep(1)
    print("⚠️ 等 Turnstile 逾時（token 未到手）")
    return False


def extract_dates(page):
    """提取页面上的所有日期 (DD/MM/YYYY)"""
    try:
        text = page.inner_text("body")
        return re.findall(r"\b\d{2}/\d{2}/\d{4}\b", text)
    except Exception:
        return []


def run(raw_cookie: str):
    """跑一轮。返回 ``(status, detail, expire)``。

    status ∈ {ok, skip, manual, cookie, fail}，语义见 _STATUS_OUTCOME。
    本函数**自己发 TG**（每条状态文案都依赖当时的上下文：key / human / 天数），
    但**不决定退出码** —— 那是 main() 的活。这样每个出口只写一次通知，
    也不会出现「两个地方都能 sys.exit」的老问题。

    原来的三处裸 sys.exit（cookie 失效 / 点不到按钮 / API 未回 success）
    全部改成 return；浏览器关闭收敛到 finally，任何出口都不会漏关。
    """
    print(f"[INFO] 浏览器引擎 = {ENGINE}", flush=True)
    mode = current_mode()
    print(f"[INFO] 运行模式 MODE = {mode}", flush=True)
    cookies = parse_cookies(raw_cookie)

    with sync_playwright() as p:
        print(f"🚀 启动浏览器：{'headless' if HEADLESS else '有頭（xvfb）+ 真 Chrome'} …", flush=True)
        args = ["--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--window-size=1920,1080"]
        # 真 Chrome 優先（Turnstile 對 chromium headless 拒絕渲染）；
        # 起唔到就退回 chromium，但**保持有頭**，唔好靜默變返 headless。
        browser = None
        if not HEADLESS:
            try:
                browser = p.chromium.launch(channel="chrome", headless=False, args=args)
                print("[INFO] 引擎 = 真 Chrome（channel=chrome, headless=False）", flush=True)
            except Exception as e:
                print(f"[WARN] 真 Chrome 起唔到（{str(e)[:160]}），退回 chromium", flush=True)
        if browser is None:
            browser = p.chromium.launch(headless=HEADLESS, args=args)
            print(f"[INFO] 引擎 = chromium headless={HEADLESS}", flush=True)
        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/143.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080}
            )

            context.add_cookies(cookies)
            page = context.new_page()
            page.add_init_script(STEALTH_JS)

            # ── 監聽續期 API 同 alert（2026-09-20）：之前冇監聽，428/alert 全部走漏，
            #    所以「按鈕點完冇反應」被誤報成「續期成功」。──
            api_results = []

            def _on_response(resp):
                try:
                    u = resp.url
                    if "renew_free_service.php" in u or "renew_server.php" in u:
                        try:
                            body = resp.text()[:300]
                        except Exception:
                            body = ""
                        api_results.append((resp.status, body))
                        print(f"   [API] {resp.status} {u.split('/')[-1]} {body[:200]}")
                except Exception:
                    pass

            def _on_dialog(d):
                print(f"   [ALERT:{d.type}] {d.message[:200]}")
                api_results.append(("dialog", d.message[:200]))
                d.accept()

            def _on_console(msg):
                try:
                    if msg.type in ("error", "warning"):
                        print(f"   [CONSOLE:{msg.type}] {msg.text[:180]}")
                except Exception:
                    pass

            page.on("response", _on_response)
            page.on("dialog", _on_dialog)
            page.on("console", _on_console)

            print("1. 正在访问服务页面...")
            page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)

            page_text = page.inner_text("body")
            if "Mes services" not in page_text and "fridaydev" not in page_text.lower():
                print("❌ Cookie 已失效，请更新 Secrets 中的 COOKIE")
                page.screenshot(path="result.png", full_page=True)
                tg_send(build_tg("fail",
                                 "Cookie 已失效，請重新抓取並更新 Secrets 嘅 COOKIE"))
                return "cookie", "Cookie 已失效（請更新 Secrets 嘅 COOKIE）", ""

            print("✅ Cookie 有效，进入服务列表！")

            # 自动点击底部 Cookie 授权按钮（如果有）
            try:
                accept_cookie_btn = page.locator("button:has-text('Accepter')")
                if accept_cookie_btn.count() > 0 and accept_cookie_btn.first.is_visible():
                    safe_click(accept_cookie_btn, "cookie 提示条", timeout=5000)
                    print("🍪 已关闭底部 Cookie 提示条")
                    time.sleep(1)
            except Exception:
                pass

            # 关掉/接受 CGU 全屏弹窗（唔关会 intercept pointer events 令续期按钮点唔到）
            dismiss_cgu_modal(page)

            # ── 等服務卡真係渲染出嚟先再读 ────────────────────────────────
            # 2026-10-06：下面每一條都係**即時查詢**（inner_text("body") 同
            # .count() 都唔會自動等元素）。卡片未出齊就查會攞到空 —— run
            # 37408676915 與 37350294906 相隔幾分鐘、面板狀態完全一樣，卻一個
            # 讀到「Renouvelable dans 3 jour(s)」、另一個日期 [] 兩個按鈕都 0
            # 個 → 跌落「未發現按鈕＝可能已續期」嘅 skip。全自動模式下咁樣會
            # **靜靜錯過可續窗口**（run 37348520733 起 schedule 已改 renew），
            # 所以讀之前一定要等。
            # 探針結論（run 37409359699）：等待逾時嘅當刻，
            #   text=Renouvelable dans → 1、text=RENOUVELLEMENT → 2
            # 即係元素**一直喺度**，問題唔係 render 慢，而係我最初寫嘅
            #   page.locator("text=/…|…/i").first.wait_for(state="visible")
            # —— regex union 嗰個 selector 嘅 .first 揞中咗個隱藏元素，
            # state="visible" 於是永遠等唔到。避開成套 selector 引擎彎角，
            # 直接對 innerText 做 regex：慢嗰陣等到、渲染咗就即刻過。
            try:
                page.wait_for_function(
                    "() => { try {"
                    "  return /RENOUVELLEMENT|Renouvelable dans|Accéder/i"
                    "         .test(document.body.innerText);"
                    "} catch (e) { return false; } }",
                    timeout=40000)
                print("✅ 服務卡已渲染")
            except Exception:
                # 逾時必須講清楚**此刻**有冇嘢 —— 否則日誌分唔開「只係 render
                # 慢」同「selector 根本冇人認」兩種情況，而補救方法完全唔同。
                print("ℹ️ 40s 內未見服務卡文字 —— 即時探測："
                      f"Renouvelable={page.locator('text=Renouvelable dans').count()} "
                      f"RENOUVELLEMENT={page.locator('text=RENOUVELLEMENT').count()} "
                      "（有數＝只係慢；兩個都 0＝真係未出，"
                      "後面按冇嘢處理、唔當成功）")

            old_dates = extract_dates(page)
            print(f"📅 当前页面检测到日期: {old_dates}")

            print("2. 正在精确定位卡片中的续期按钮...")

            # 1. 优先匹配 "Renouveler gratuitement"（免费续期）
            # 2. 其次匹配纯 "Renouveler" 按钮
            # 3. 坚决排除 "À renouveler" (顶部标签) 和 "Renouvelable dans" (倒计时)
            renew_btn = page.locator("button, a").filter(
                has_text=re.compile(r"Renouveler\s+gratuitement|^Renouveler$", re.I)
            ).filter(
                has_not_text="À renouveler"
            ).filter(
                has_not_text="Renouvelable dans"
            )

            not_yet_btn = page.locator("text=/Renouvelable dans \\d+ jour/i")

            # ── watchdog 模式：只讀唔寫 ──────────────────────────────────────
            # 唔撳任何嘢、唔碰 Turnstile，只報狀態；進入可續期窗口就叫人手續。
            if mode != "renew":
                key = ("面板 " + fmt_date(old_dates[0])) if old_dates else ""
                if renew_btn.count() > 0 and renew_btn.first.is_visible():
                    print("🔔【可續期窗口已開】watchdog 模式：唔會自動撳"
                          "（GHA 過唔到 Turnstile）")
                    # open_now=True：窗口真开了，通知必须明说「去手点」。
                    tg_send(build_tg("skip", "", key=key, open_now=True))
                    page.screenshot(path="result.png", full_page=True)
                    return "manual", "已進入可續期窗口，需人手撳（watchdog 預期訊號）", ""
                status_text = (not_yet_btn.first.inner_text().strip()
                               if not_yet_btn.count() else "")
                cd = re.search(r"Renouvelable dans (\d+) jour", status_text)
                days = cd.group(1) if cd else "?"
                print(f"🔒【watchdog 讀數】{status_text or '未見倒計時（可能已可續）'}"
                      f" | 面板日期: {old_dates[:3]}")
                if days.isdigit() and int(days) <= 5:
                    urgent = int(days) <= 3
                    # 窗口**还没开**，页面上没有可点的按钮 —— 别写「要人手撳」，
                    # 只升到「續期窗口即將開啟」当预备提醒（human=urgent）。
                    tg_send(build_tg("skip", f"未可續（仲有 {days} 日）",
                                     key=key, human=urgent))
                else:
                    # 剩 >5 天**故意静默**：每天一条「还早」就是噪音。
                    # 注意这条分支不能漏 —— 迁移时如果统一在出口发通知，
                    # 静默日就会破功。
                    print(f"ℹ️ 剩 {days} 天（>5），按約定靜默，不發 TG")
                page.screenshot(path="result.png", full_page=True)
                return "skip", f"未到續期窗口（仲有 {days} 日）", ""

            if renew_btn.count() > 0 and renew_btn.first.is_visible():
                target_text = renew_btn.first.inner_text().strip()
                print(f"🎉【成功锁定续期按钮】: 【{target_text}】，正在执行点击！")

                # 点击续费按钮（被遮罩拦截时自动退回 JS 点击）
                if not safe_click(renew_btn, "续期按钮"):
                    # 再试一次：先再清一次遮罩
                    dismiss_cgu_modal(page)
                    if not safe_click(renew_btn, "续期按钮(重试)"):
                        print("❌ 续期按钮点击失败")
                        page.screenshot(path="result.png", full_page=True)
                        tg_send(build_tg("fail",
                                         "續期按鈕點唔到（可能又出新遮罩／改版），已截圖"))
                        return "fail", "續期按鈕點唔到（可能又出新遮罩／改版）", ""
                time.sleep(4)

                # 428 + captcha_required → 前端會 render Cloudflare Turnstile，等佢過
                print("🔐 檢查反機械人驗證（Turnstile）…")
                wait_turnstile_token(page, timeout=75)
                # 等續期 API 真正回覆（成功/失敗）
                for _ in range(30):
                    if api_results and (api_results[-1][0] == 200
                                        or api_results[-1][0] == "dialog"):
                        break
                    time.sleep(1)
                time.sleep(2)

                # 确认弹窗处理（如果有）
                try:
                    modal_confirm = page.locator(
                        ".modal.show button, .modal.active button, .swal2-confirm, "
                        "button:has-text('Confirmer'), button:has-text('Valider')"
                    ).filter(has_not_text="suppression").filter(has_not_text="Résilier")
                    if modal_confirm.count() > 0 and modal_confirm.first.is_visible():
                        if safe_click(modal_confirm, "确认弹窗", timeout=5000):
                            print("✅ 已点击确认弹窗")
                        time.sleep(3)
                except Exception:
                    pass

                # 刷新页面验证结果
                print("3. 正在刷新页面验证结果...")
                page.reload(wait_until="domcontentloaded")
                time.sleep(5)

                new_dates = extract_dates(page)
                new_page_text = page.inner_text("body")

                print(f"📅 刷新后页面日期: {new_dates}")
                print(f"🧾 續期 API 記錄: "
                      f"{api_results[-3:] if api_results else '（完全冇呼叫過續期 API）'}")

                ok_api = any(
                    isinstance(s, int) and s == 200
                    and '"success":true' in (b or "").replace(" ", "").lower()
                    for s, b in api_results
                )
                still_renewable = "Renouveler gratuitement" in new_page_text

                exp_new = fmt_date(new_dates[0]) if new_dates else ""

                if "Renouvelable dans" in new_page_text:
                    print("🎉🎉 FridayDev 续期成功！按钮已进入下一次续期倒计时状态。")
                    tg_send(build_tg("ok", expire=exp_new))
                    return "ok", "續期成功（按鈕已進入下一輪倒計時）", exp_new
                elif ok_api and not still_renewable:
                    print("🎉🎉 FridayDev 续期成功！续期 API 返回 success，按钮已复位。")
                    tg_send(build_tg("ok", expire=exp_new))
                    return "ok", "續期成功（API 回 success，按鈕已復位）", exp_new
                elif ok_api:
                    print("✅ FridayDev 续期已完成（API 确认），页面按钮状态稍后刷新。")
                    tg_send(build_tg("ok", expire=exp_new))
                    return "ok", "續期成功（API 確認，頁面稍後刷新）", exp_new

                # 按鈕仲喺度／API 冇成功 —— 老實報紅，唔好再報假成功
                trace = "; ".join(f"{s}:{str(b)[:80]}"
                                  for s, b in api_results[-3:]) or "無 API 呼叫"
                msg = ("❌ <b>FridayDev 续期未成功</b>\n"
                       "续期 API 未回 success（大概率係反機械人測試 Turnstile 未過）。\n"
                       f"API 記錄: {trace[:300]}")
                print(msg)
                page.screenshot(path="result.png", full_page=True)
                tg_send(build_tg("fail",
                                 f"API 未回 success（大概率 Turnstile 未過）· {trace}"))
                return "fail", f"API 未回 success（大概率 Turnstile 未過）· {trace}", ""

            elif not_yet_btn.count() > 0:
                status_text = not_yet_btn.first.inner_text().strip()
                countdown = re.search(r"Renouvelable dans (\d+) jour", status_text)
                days = countdown.group(1) if countdown else "?"
                print(f"🔒【暂不可续期】倒计时状态: 【{status_text}】")
                # 静默：倒计时 > 2 天不发 TG；剩 0-2 天先提醒（就快到期要手动留意）
                if days.isdigit() and int(days) <= 2:
                    # note= 把那句安抚带进第二行 —— 修复前它被正则吞掉，
                    # 用户只看到「未到續期窗口」+「剩 1 日」，比不说更慌。
                    tg_send(build_tg("skip", f"未可續（仲有 {days} 日）",
                                     note="下次自動續"))
                else:
                    print(f"ℹ️ 剩 {days} 天（>2），按約定靜默，不發 TG")
                # 2026-10-06：renew 模式呢個出口原本**漏咗截圖**（watchdog 出口
                # renew.py:571 同 fail 出口 :655 都有），結果 run 37348520733
                # 冇 artifact 可上傳，而 finally 嗰行仲報「已保存」。
                # Turnstile 排障全靠呢張圖，補返。
                page.screenshot(path="result.png", full_page=True)
                return "skip", f"未到續期窗口（仲有 {days} 日）", ""

            else:
                print("ℹ️ 未发现续期按钮，当前可能已成功续期。")
                page.screenshot(path="result.png", full_page=True)
                return "skip", "未發現續期按鈕（可能已續期）", ""

        # ── 收尾截圖（2026-10-06 修）────────────────────────────────────────
        # 原本 finally 第一句係**無條件 print「截图已保存至 result.png」**，但
        # renew 模式嗰兩個 skip 出口根本冇截圖，於 run 37348520733 出現「日誌話
        # 存咗 / artifact upload 話搵唔到」嘅自相矛盾。改成收尾真補截一次 +
        # 老實報結果（唔準 os —— harness C3b 釘死「不再 import os」）。
        # ⚠️ harness C5 要求 finally: 之後**最多 6 行**就到 browser.close()
        #    （防止關唔到瀏覽器嘅安全網），所以下面嗰段只能咁緊密；
        #    註釋必須放喺 finally 之前 —— 註釋行一樣計數。
        finally:
            try:
                page.screenshot(path="result.png", full_page=True)
                print("📸 截图已存 result.png")
            except Exception:
                print("⚠️ 收尾截图失败，本輪冇 artifact 可上傳")
            try:
                browser.close()
            except Exception:
                pass


def main() -> int:
    raw_cookie = env.get("COOKIE")
    if not raw_cookie:
        print("❌ 错误: 未在 GitHub Secrets 中设置 COOKIE")
        tg_send(build_tg("fail", "未設置 COOKIE secret，無法登入面板"))
        return _report([(PANEL_TARGET, "cookie",
                         "未設置 COOKIE secret")]).exit_code

    try:
        status, detail, expire = run(raw_cookie)
    except Exception as e:
        traceback.print_exc()
        detail = f"{type(e).__name__}: {shorten(str(e), 140)}"
        tg_send(build_tg("fail", f"腳本異常 · {detail}"))
        return _report([(PANEL_TARGET, "error", detail)]).exit_code

    # 退出码交给 kit 按 Outcome 决定 —— 只有 FAILED 才是 1。
    # 这里不调 report.finish()：本仓库是「一判定出状态就立刻发一条」，
    # run 级别再来一条总览就是重复。所以只取 exit_code。
    report = _report([(PANEL_TARGET, status, detail, expire)])
    print(f"\n[結果] {SERVICE} · {status} · {detail}"
          + (f" · 到期 {expire}" if expire else ""))
    if report.exit_code:
        print("[ERROR] 有 1 個目標未完成，exit 1")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
