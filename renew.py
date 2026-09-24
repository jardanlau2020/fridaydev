import os
import re
import sys
import time
import json
import urllib.request
import urllib.parse
# 2026-09-20：改用 patchright（undetected Playwright）做首選引擎。
# 證據：weirdhost 探針 run 35501369694 —— 同一段點擊代碼，普通 playwright 撳完
# 71 秒乜都冇，patchright 一撳即有 cf_clearance。指紋差別就係 CF 認唔認你。
try:
    from patchright.sync_api import sync_playwright
    ENGINE = "patchright"
except Exception:
    from playwright.sync_api import sync_playwright
    ENGINE = "playwright"
print(f"[INFO] 浏览器引擎 = {ENGINE}", flush=True)

COOKIE_STR = os.environ.get("COOKIE")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

def tg_send(text):
    """TG 通知（有配置先發；失敗唔影響主流程）"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"⚠️ TG 发送失败: {e}")

EXIT_OK = 0        # 续期成功或无需续期
EXIT_MANUAL_REQUIRED = 2  # 已进入可续期窗口，但要人手撳（红叉係预期訊號，唔係故障）
EXIT_COOKIE_DEAD = 3  # cookie 失效
EXIT_FAIL = 1      # 其他失败

# ── 2026-09-24：由「自動續期」降級做「到期提醒守門」(watchdog) ──────────────
# 硬證據：Cloudflare Turnstile 喺 GHA runner IP 過唔到（playwright / patchright /
# camoufox / seleniumbase UC / nodriver 五種引擎 + 真鼠標 OS 事件 + 20 組出口×UA
# 全部失敗，run 35510050597 起連續紅）。繼續每日自動撳只會製造紅燈同假訊號。
# 默認 MODE=watchdog：只讀狀態 + 到期前 TG 提醒（剩 ≤5 日開始提，≤3 日每日提）。
# 想再試自動續期就手動 dispatch 並揀 mode=renew。
MODE = (os.environ.get("FD_MODE") or "watchdog").strip().lower()
print(f"[INFO] 运行模式 MODE = {MODE}", flush=True)

if not COOKIE_STR:
    print("❌ 错误: 未在 GitHub Secrets 中设置 COOKIE")
    sys.exit(EXIT_FAIL)

cookies = []
for item in COOKIE_STR.split(";"):
    if "=" in item:
        name, value = item.strip().split("=", 1)
        cookies.append({
            "name": name,
            "value": value,
            "domain": "fridaydev.fr",
            "path": "/"
        })


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
        print(f"⚠️ 常规点击{(' [' + label + ']') if label else ''}失败({repr(e)[:90]})，改用 JS 点击兜底...")
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
        page.evaluate("() => { const m = document.getElementById('fd-cgu-modal'); if (m) m.remove(); }")
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
        dates = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", text)
        return dates
    except Exception:
        return []


def run():
    with sync_playwright() as p:
        print("🚀 启动无头浏览器...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )

        context.add_cookies(cookies)
        page = context.new_page()

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
        page.goto("https://fridaydev.fr/services/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        page_text = page.inner_text("body")
        if "Mes services" not in page_text and "fridaydev" not in page_text.lower():
            print("❌ Cookie 已失效，请更新 Secrets 中的 COOKIE")
            tg_send("❌ <b>FridayDev 续期失败</b>\nCookie 已失效，请重新抓取并更新 GitHub Secrets")
            page.screenshot(path="result.png", full_page=True)
            browser.close()
            sys.exit(EXIT_COOKIE_DEAD)

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
        if MODE != "renew":
            if renew_btn.count() > 0 and renew_btn.first.is_visible():
                print("🔔【可續期窗口已開】watchdog 模式：唔會自動撳（GHA 過唔到 Turnstile）")
                tg_send("🔔 <b>FridayDev 可以續期喇</b>\n"
                        "請人手去面板撳「Renouveler gratuitement」。\n"
                        f"📅 面板日期: {', '.join(old_dates[:3]) or '?'}\n"
                        "（自動續期喺 GHA runner 過唔到 Cloudflare Turnstile，已停用；"
                        "要再試就手動 dispatch 並揀 mode=renew）")
                page.screenshot(path="result.png", full_page=True)
                browser.close()
                return EXIT_MANUAL_REQUIRED
            status_text = not_yet_btn.first.inner_text().strip() if not_yet_btn.count() else ""
            cd = re.search(r"Renouvelable dans (\d+) jour", status_text)
            days = cd.group(1) if cd else "?"
            print(f"🔒【watchdog 讀數】{status_text or '未見倒計時（可能已可續）'} | 面板日期: {old_dates[:3]}")
            if days.isdigit() and int(days) <= 5:
                urge = "⚠️ 就到期，記得去撳！" if int(days) <= 3 else ""
                tg_send(f"🔒 <b>FridayDev 續期倒計時</b>\n仲有 {days} 日可以續期。{urge}\n"
                        "到期前請人手到面板撳「Renouveler gratuitement」。")
            page.screenshot(path="result.png", full_page=True)
            browser.close()
            return EXIT_OK

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
                    tg_send("❌ <b>FridayDev 续期失败</b>\n续期按钮点唔到（可能又出咗新遮罩/改版），已截图")
                    browser.close()
                    sys.exit(EXIT_FAIL)
            time.sleep(4)

            # 428 + captcha_required → 前端會 render Cloudflare Turnstile，等佢過
            print("🔐 檢查反機械人驗證（Turnstile）…")
            wait_turnstile_token(page, timeout=75)
            # 等續期 API 真正回覆（成功/失敗）
            for _ in range(30):
                if api_results and (api_results[-1][0] == 200 or api_results[-1][0] == "dialog"):
                    break
                time.sleep(1)
            time.sleep(2)

            # 确认弹窗处理（如果有）
            try:
                modal_confirm = page.locator(".modal.show button, .modal.active button, .swal2-confirm, button:has-text('Confirmer'), button:has-text('Valider')").filter(has_not_text="suppression").filter(has_not_text="Résilier")
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
            print(f"🧾 續期 API 記錄: {api_results[-3:] if api_results else '（完全冇呼叫過續期 API）'}")

            ok_api = any(
                isinstance(s, int) and s == 200 and '"success":true' in (b or "").replace(" ", "").lower()
                for s, b in api_results
            )
            still_renewable = "Renouveler gratuitement" in new_page_text

            if "Renouvelable dans" in new_page_text:
                msg = "🎉🎉 <b>FridayDev 续期成功！</b>\n按钮已进入下一次续期倒计时状态。"
                print(msg)
                tg_send(msg + f"\n📅 新到期日: {', '.join(new_dates[:3])}")
            elif ok_api and not still_renewable:
                msg = "🎉🎉 <b>FridayDev 续期成功！</b>\n续期 API 返回 success，按钮已复位。"
                print(msg)
                tg_send(msg + f"\n📅 新到期日: {', '.join(new_dates[:3])}")
            elif ok_api:
                msg = "✅ <b>FridayDev 续期已完成（API 确认）</b>，页面按钮状态稍后刷新。"
                print(msg)
                tg_send(msg + f"\n📅 新到期日: {', '.join(new_dates[:3])}")
            else:
                # 按鈕仲喺度／API 冇成功 —— 老實報紅，唔好再報假成功
                trace = "; ".join(f"{s}:{str(b)[:80]}" for s, b in api_results[-3:]) or "無 API 呼叫"
                msg = ("❌ <b>FridayDev 续期未成功</b>\n"
                       "续期 API 未回 success（大概率係反機械人測試 Turnstile 未過）。\n"
                       f"API 記錄: {trace[:300]}")
                print(msg)
                tg_send(msg)
                page.screenshot(path="result.png", full_page=True)
                browser.close()
                sys.exit(EXIT_FAIL)

        elif not_yet_btn.count() > 0:
            status_text = not_yet_btn.first.inner_text().strip()
            countdown = re.search(r"Renouvelable dans (\d+) jour", status_text)
            days = countdown.group(1) if countdown else "?"
            print(f"🔒【暂不可续期】倒计时状态: 【{status_text}】")
            # 静默：倒计时 > 2 天不发 TG；剩 0-2 天先提醒（就快到期要手动留意）
            if days.isdigit() and int(days) <= 2:
                tg_send(f"🔒 <b>FridayDev 续期倒计时</b>\n还有 {days} 天可续期，下次运行将自动续")
        else:
            print("ℹ️ 未发现续期按钮，当前可能已成功续期。")

        page.screenshot(path="result.png", full_page=True)
        print("📸 最终截图已保存至 result.png")
        browser.close()
        return EXIT_OK

if __name__ == "__main__":
    code = run() or EXIT_OK
    sys.exit(code)
