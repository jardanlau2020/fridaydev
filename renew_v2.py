#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FridayDev 續期 v2 — 2026-11-18

背景：站方 2026-11 改版（Laravel + JS）
  · 舊版 `/services/` 續期流程（cookie + Renouveler 按鈕 + Turnstile）已作廢
  · 新版：`POST /php/login.php` 登入 → `/services/` 服務卡 →
          `POST /php/renew_options.php` / `POST /php/renew_server.php`
  · 續期掣只有「剩 ≤2 日」才啟用（tooltip: Possible à partir de 2 jours restants）
  · 免費方案落單有機會回 428 captcha_required → 由頁面自己叫 Turnstile（captcha-gate.js）

因此本版改為：**帳密即場登入**（唔再靠人手抄 cookie），
再由頁面自己撳掣（CSRF / Turnstile 由站方 JS 處理，我哋唔硬解）。

環境變數
  FD_EMAIL / FD_PASSWORD   必填
  FD_MODE                  watchdog（默認，只讀）| renew（窗口開就撳）
  TG_BOT_TOKEN / TG_CHAT_ID 可選
  DRY_RUN=1                只印唔發 TG
  PROXY_SERVER             可選，socks5://127.0.0.1:1080

退出碼
  0 = 正常（含「未到窗口」跳過）
  1 = 失敗（登入失敗 / 讀唔到服務 / 撳完冇變化）
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

EMAIL = (os.environ.get("FD_EMAIL") or "").strip()
PASSWORD = os.environ.get("FD_PASSWORD") or ""
MODE = (os.environ.get("FD_MODE") or "watchdog").strip().lower()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT = os.environ.get("TG_CHAT_ID") or ""
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PROXY = os.environ.get("PROXY_SERVER") or ""

BASE = "https://fridaydev.fr"
SERVICES = BASE + "/services/"
SHOT = os.path.join(os.getcwd(), "result.png")


def log(msg):
    print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%SZ"), msg), flush=True)


def notify(text):
    if DRY_RUN or not TG_TOKEN or not TG_CHAT:
        log("TG(skip): " + text.replace("\n", " | ")[:300])
        return
    try:
        import requests
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
                          json={"chat_id": TG_CHAT, "text": text}, timeout=15)
        log("TG: %s" % r.status_code)
    except Exception as e:
        log("TG 失敗: %s" % e)


def parse_service(page):
    """由 /services/ 頁面讀出服務卡狀態。"""
    body = page.inner_text("body")
    info = {"raw_has_services": "Mes services" in body}

    m = re.search(r"\b(srv-[a-z0-9]{6,})\b", body)
    info["service_id"] = m.group(1) if m else None

    m = re.search(r"RENOUVELLEMENT\s*\n?\s*(\d{2}/\d{2}/\d{4})", body)
    if not m:
        m = re.search(r"(\d{2}/\d{2}/\d{4})", body)
    info["expiry"] = m.group(1) if m else None

    m = re.search(r"Renouvelable dans\s+(\d+)\s*jour", body, re.I)
    if m:
        info["days_left"] = int(m.group(1))
    else:
        info["days_left"] = None
        if info["expiry"]:
            try:
                d = datetime.strptime(info["expiry"], "%d/%m/%Y").date()
                info["days_left"] = (d - datetime.now(timezone.utc).date()).days
            except Exception:
                pass

    m = re.search(r"(Suspendu|Suspendue|Actif|Expiré)", body)
    info["status"] = m.group(1) if m else None

    btn = page.query_selector(".btn-renew")
    if btn:
        info["btn_text"] = (btn.inner_text() or "").strip()
        info["btn_disabled"] = btn.get_attribute("disabled") is not None or "is-disabled" in (btn.get_attribute("class") or "")
    else:
        info["btn_text"] = None
        info["btn_disabled"] = None
    return info


def do_login(page):
    page.goto(BASE + "/login/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    if page.query_selector("#email") is None:
        return False, "登入表單唔見（站方可能再改版）"
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.click("form#loginForm .btn-primary")
    for _ in range(20):
        page.wait_for_timeout(700)
        body = page.inner_text("body")
        if "Espace client" in body or "Déconnecter" in body or "Deconnecter" in body:
            return True, page.url
        if "2fa" in page.url:
            return False, "帳號要求 2FA（需人手）"
    return False, "登入後仍未見『Espace client』：%s" % page.url


def main():
    if not EMAIL or not PASSWORD:
        log("缺少 FD_EMAIL / FD_PASSWORD")
        notify("🚨 FridayDev 續期：未設定 FD_EMAIL / FD_PASSWORD secret")
        return 1

    log("模式 MODE=%s | 出口=%s" % (MODE, PROXY or "直連"))
    with sync_playwright() as p:
        launch = {"headless": not bool(os.environ.get("DISPLAY"))}
        ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        browser = None
        for label, kw in (("真 Chrome", {"channel": "chrome"}), ("內建 chromium", {}),
                          ("系統 chromium", {"executable_path": "/usr/bin/chromium"})):
            try:
                browser = p.chromium.launch(args=ARGS, **kw, **launch)
                log("引擎: %s" % label)
                break
            except Exception as e:
                log("引擎 %s 不可用: %s" % (label, str(e)[:80]))
        if browser is None:
            raise SystemExit("冇可用瀏覽器")

        ctx_args = {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "locale": "fr-FR", "viewport": {"width": 1366, "height": 1000}}
        if PROXY:
            ctx_args["proxy"] = {"server": PROXY}
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()

        try:
            ok, detail = do_login(page)
            if not ok:
                log("❌ 登入失敗: %s" % detail)
                try:
                    page.screenshot(path=SHOT, full_page=True)
                except Exception:
                    pass
                notify("🚨 FridayDev · 登入失敗\n%s\n請檢查 FD_EMAIL / FD_PASSWORD" % detail)
                return 1
            log("✅ 登入成功: %s" % detail)

            page.goto(SERVICES, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            info = parse_service(page)
            log("服務: %s" % json.dumps(info, ensure_ascii=False))

            if not info.get("service_id"):
                page.screenshot(path=SHOT, full_page=True)
                notify("🚨 FridayDev · 讀唔到服務卡（/services/ 結構可能又改）")
                return 1

            days = info.get("days_left")
            if days is None:
                log("⚠️ 讀唔到剩餘日數，保守當未到窗口")
                notify("⚠️ FridayDev · 讀唔到剩餘日數（%s）" % info.get("expiry"))
                return 0

            if days > 2:
                log("⏳ 未到窗口（剩 %s 日，需 ≤2 日）：靜默結束" % days)
                if info.get("btn_disabled") is False:
                    log("（注意：掣已啟用但日數 >2，照樣唔撳）")
                return 0

            if MODE != "renew":
                if info.get("btn_disabled") is False:
                    log("🔔 watchdog：剩 %s 日、掣已開 → 提醒人手撳" % days)
                    notify("🔔 FridayDev 續期窗口已開（剩 %s 日，到期 %s）\n請上面板撳「Renouveler」" % (days, info.get("expiry")))
                elif days <= 1:
                    log("🔔 watchdog：剩 %s 日但掣仍停用 → 提醒" % days)
                    notify("⚠️ FridayDev 只剩 %s 日（到期 %s），但續期掣仍停用（%s）\n請人手上面板確認" % (days, info.get("expiry"), info.get("btn_text")))
                else:
                    log("⏳ 剩 %s 日但掣仍停用（%s）：靜默等下一輪" % (days, info.get("btn_text")))
                return 0

            # ── renew ──
            if info.get("btn_disabled"):
                log("⚠️ renew 模式但掣仍 disabled（%s），等下一輪" % info.get("btn_text"))
                notify("⚠️ FridayDev · 窗口內但掣停用（%s）" % info.get("btn_text"))
                return 0

            before = info.get("expiry")
            log("🖱️ 撳續期掣（%s）…" % info.get("btn_text"))
            page.click(".btn-renew")

            # 可能出續期選項彈窗（renew-engagement.js）→ 揀第一個選項再確認
            confirmed = False
            for _ in range(30):
                page.wait_for_timeout(1000)
                if page.query_selector("#fd-renew-ov .fd-rn-btn--go"):
                    log("彈窗出現，撳確認…")
                    page.click("#fd-renew-ov .fd-rn-btn--go")
                    confirmed = True
                    break
                if page.query_selector("#fd-renew-ov .fd-rn-opt"):
                    log("彈窗出現，揀第一個方案…")
                    page.click("#fd-renew-ov .fd-rn-opt")
                    page.wait_for_timeout(800)
                    if page.query_selector("#fd-renew-ov .fd-rn-btn--go"):
                        page.click("#fd-renew-ov .fd-rn-btn--go")
                        confirmed = True
                        break
                body = page.inner_text("body")
                if "renouvel" in body.lower() and "confirm" in body.lower():
                    continue
            log("確認步驟: %s" % ("有" if confirmed else "無（可能直接完成）"))

            # 驗證：重新載入睇到期日有冇推進
            page.wait_for_timeout(4000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            after_info = parse_service(page)
            after = after_info.get("expiry")
            page.screenshot(path=SHOT, full_page=True)

            if after and before and after != before:
                log("🎉 續期成功：%s → %s" % (before, after))
                notify("🎉 FridayDev 續期成功\n%s → %s\n服務 %s" % (before, after, info.get("service_id")))
                return 0
            if after_info.get("days_left") is not None and after_info["days_left"] > 2:
                log("🎉 續期成功（剩餘日數 %s）" % after_info["days_left"])
                notify("🎉 FridayDev 續期成功（剩 %s 日，到期 %s）" % (after_info["days_left"], after))
                return 0

            log("❌ 撳完到期日冇變（%s）" % after)
            notify("🚨 FridayDev · 撳完續期但到期日冇變（%s → %s）\n請人手上面板" % (before, after))
            return 1
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
