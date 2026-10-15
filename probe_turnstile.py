#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FridayDev Turnstile 只讀探針 —— 測「新組合」能唔能過站方嘅反機械人閘。

為咩要再做一次
--------------
2026-10-08/09 嘅結論係「Turnstile 過唔到（站方判自動化環境）」，但當時有兩個未解：
  1. 決定性測試（窗口＋**住宅出口**）**從未真正跑過** —— GHA 連唔到住宅節點
     （`vless[proxy] context deadline exceeded` / `代理连接失败`），每次都回退直連機房 IP。
  2. 當時嘅症狀係 widget 容器 render 到（378x73）但 `document.querySelectorAll('iframe')=[]`、
     `window.turnstile === undefined`、token 恆 0 —— 呢個簽名**似係 CF 腳本被出口 IP 擋**
     （而唔係「行為指紋被判死」）。
到 2026-10-15，我哋已經有實證可用嘅新組合：
**SeleniumBase UC 模式（真·有頭 Chrome + OS 級滑鼠點擊，xvfb 之下）＋ cfnew 家宽住宅出口**
（同日喺 OptiLink 連續幾轉過咗數學題 + Turnstile，令牌 794 字）。
所以用同一套組合重測 FridayDev 呢道閘。

本探針**只讀**：注入 cookie → 開服務頁 → dump Turnstile 狀態 → 嘗試點 widget 攞 token。
**絕對唔撳「Renouveler gratuitement」、唔提交續期**（就算撳到 token 都唔提交）。
若果頁面未 render widget，會用**冇 token** POST `/php/renew_free_service.php` 引站方回
428 `captcha_required`（冇 token 必被拒，唔會續到期），令 widget 出現再測。

環境變數
--------
  COOKIE        必填。同 watchdog 用嘅一樣（`a=1; b=2` 字串）
  PROXY_SERVER  選填。socks5://127.0.0.1:1080（住宅出口；機房 IP 會被 CF 擋）
  FD_HEADLESS   選填，預設 false（一定要有頭，Turnstile 對 headless 拒絕渲染）
  PROBE_POST    選填，預設 "1"：頁面冇 widget 時，用冇 token POST 引挑戰
  TG_BOT_TOKEN / TG_CHAT_ID  選填，只發一條結果

退出碼：0 = 攞到 Turnstile token（＝新組合過得閘）；2 = 攞唔到；3 = 環境問題
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

BASE = "https://fridaydev.fr"
SERVICES = f"{BASE}/services/"
RENEW_EP = f"{BASE}/php/renew_free_service.php"

COOKIE = (os.environ.get("COOKIE") or "").strip()
PROXY = (os.environ.get("PROXY_SERVER") or "").strip()
HEADLESS = (os.environ.get("FD_HEADLESS") or "false").strip().lower() in ("1", "true", "yes")
DO_POST = (os.environ.get("PROBE_POST") or "1").strip() not in ("0", "false", "False")

STATE_JS = """
(function(){
  var q = function(s){ return document.querySelector(s); };
  var inp = q('input[name="cf-turnstile-response"]');
  var wid = q('.fd-captcha-widget') || q('[class*=captcha]');
  var box = wid ? wid.getBoundingClientRect() : null;
  return {
    turnstile_api: (typeof window.turnstile),
    fdCaptchaSolve: (typeof window.fdCaptchaSolve),
    iframes: document.querySelectorAll('iframe').length,
    cf_iframes: document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]').length,
    widget: !!wid,
    widget_box: box ? {x: Math.round(box.x), y: Math.round(box.y),
                       w: Math.round(box.width), h: Math.round(box.height)} : null,
    token_len: inp ? (inp.value || '').length : -1,
    body_len: (document.body ? document.body.innerText.length : 0)
  };
})()
"""


def parse_cookies(raw: str) -> list[dict]:
    out = []
    for item in (raw or "").split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            out.append({"name": k, "value": v, "domain": "fridaydev.fr", "path": "/"})
    return out


def state(sb) -> dict:
    try:
        return sb.execute_script(STATE_JS) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def dump(tag: str, sb) -> dict:
    st = state(sb)
    print(f"  [{tag}] {json.dumps(st, ensure_ascii=False)}", flush=True)
    return st


def tg(text: str) -> None:
    tok = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        return
    try:
        import urllib.request
        data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        print("  TG 已發")
    except Exception as exc:
        print(f"  TG 失敗: {exc}")


def main() -> int:
    print("#" * 46)
    print("   FridayDev Turnstile 只讀探針")
    print("#" * 46)
    if not COOKIE:
        print("❌ 冇 COOKIE")
        return 3
    try:
        from seleniumbase import SB
    except Exception as exc:
        print(f"❌ 缺 seleniumbase（{exc}）")
        return 3

    kwargs = {"uc": True, "test": True, "locale": "en", "raise_test_failure": True,
              "headless": HEADLESS}
    if PROXY:
        kwargs["proxy"] = PROXY.replace("socks5h://", "socks5://")
    print(f"  代理={'有（' + kwargs.get('proxy', '') + '）' if PROXY else '冇（直連）'}"
          f" ｜ headless={HEADLESS} ｜ PROBE_POST={DO_POST}")

    cookies = parse_cookies(COOKIE)
    print(f"  cookie: {len(cookies)} 個 key")

    token = ""
    with SB(**kwargs) as sb:
        sb.uc_open_with_reconnect(SERVICES, reconnect_time=4.0)
        time.sleep(2)
        # 注入 cookie（先開同域頁再落 cookie，避免 domain 問題）
        for c in cookies:
            try:
                sb.driver.add_cookie(c)
            except Exception as exc:
                print(f"  ⚠️ cookie {c['name']} 注入失敗: {type(exc).__name__}")
        sb.open(SERVICES)
        sb.wait_for_ready_state_complete()
        time.sleep(3)
        print(f"  [0] URL = {sb.get_current_url()}", flush=True)
        dump("1 初次載入", sb)

        # 若果冇 widget：用「冇 token」POST 續期端點引站方回 428 captcha_required
        st = state(sb)
        if DO_POST and not st.get("widget"):
            print("  → 頁面未見 widget，用冇 token POST 引挑戰（必被拒，唔會續期）", flush=True)
            try:
                r = sb.execute_async_script("""
                    var cb = arguments[arguments.length - 1];
                    fetch('/php/renew_free_service.php', {method:'POST',
                          headers:{'X-Requested-With':'XMLHttpRequest'}})
                      .then(r => r.text().then(t => cb({status:r.status, text:t.slice(0,400)})))
                      .catch(e => cb({error: String(e)}));
                """)
                print(f"  [POST] {json.dumps(r, ensure_ascii=False)[:500]}", flush=True)
            except Exception as exc:
                print(f"  ⚠️ POST 引挑戰失敗: {type(exc).__name__}", flush=True)
            time.sleep(4)
            if sb.execute_script("return typeof window.fdCaptchaSolve") == "function":
                try:
                    sb.execute_script("window.fdCaptchaSolve();")
                    print("  → 已呼叫 window.fdCaptchaSolve()", flush=True)
                except Exception as exc:
                    print(f"  ⚠️ fdCaptchaSolve 失敗: {type(exc).__name__}")
            time.sleep(4)
            dump("2 引挑戰後", sb)

        # 嘗試 UC 模式點 widget 攞 token（唔提交任何嘢）
        for attempt in range(1, 4):
            st = state(sb)
            if st.get("token_len", 0) and st["token_len"] > 50:
                token = "x" * st["token_len"]
                break
            if not st.get("widget"):
                print(f"  （第 {attempt} 次）冇 widget 可以點", flush=True)
                break
            print(f"  （第 {attempt} 次）uc_gui_click_captcha…", flush=True)
            try:
                sb.uc_gui_click_captcha()
            except Exception as exc:
                print(f"    ⚠️ 點擊異常: {type(exc).__name__}: {exc}", flush=True)
            for _ in range(8):
                time.sleep(4)
                st = state(sb)
                if st.get("token_len", 0) and st["token_len"] > 50:
                    token = "x" * st["token_len"]
                    break
            dump(f"3 點擊後（第 {attempt} 次）", sb)
            if token:
                break

        try:
            sb.save_screenshot("probe_turnstile.png")
        except Exception:
            pass
        final = state(sb)

    ok = bool(token)
    if ok:
        msg = ("🧪 FridayDev Turnstile 探針（只讀）\n"
               f"✅ **過得** —— token 長度 {len(token)}｜"
               f"widget={'有' if final.get('widget') else '冇'}｜"
               f"cf_iframes={final.get('cf_iframes')}｜turnstile_api={final.get('turnstile_api')}\n"
               "💡 即係「有頭 UC 瀏覽器 + 住宅出口」過得站方反機械人閘 → 可以考慮試全自動續期（要你拍板）")
    else:
        msg = ("🧪 FridayDev Turnstile 探針（只讀）\n"
               f"❌ 攞唔到 token｜widget={'有' if final.get('widget') else '冇'}｜"
               f"cf_iframes={final.get('cf_iframes')}｜turnstile_api={final.get('turnstile_api')}｜"
               f"token_len={final.get('token_len')}")
    print("\n" + msg.replace("**", ""))
    tg(msg)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
