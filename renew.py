import os
import re
import sys
import time
import json
import urllib.request
import urllib.parse
from playwright.sync_api import sync_playwright

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
EXIT_NO_RENEW = 2  # 倒计时未到（不可续）
EXIT_COOKIE_DEAD = 3  # cookie 失效
EXIT_FAIL = 1      # 其他失败

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

            if "Renouvelable dans" in new_page_text:
                msg = "🎉🎉 <b>FridayDev 续期成功！</b>\n按钮已进入下一次续期倒计时状态。"
                print(msg)
                tg_send(msg + f"\n📅 新到期日: {', '.join(new_dates[:3])}")
            elif "Actif" in new_page_text and "Suspendu" not in new_page_text:
                msg = "🎉🎉 <b>FridayDev 续期成功！</b>\n服务状态为 【Actif (正常运行)】"
                print(msg)
                tg_send(msg + f"\n📅 新到期日: {', '.join(new_dates[:3])}")
            else:
                msg = "✅ <b>FridayDev 续期流程已完成</b>，请查看截图确认。"
                print(msg)
                tg_send(msg + f"\n📅 刷新后日期: {', '.join(new_dates[:3])}")

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
