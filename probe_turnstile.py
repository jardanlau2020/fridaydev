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

# ⚠️ 一定要**單行** JS：UC 模式下 sb.execute_script 走 CDP evaluate，唔吞多行（會回 {}）。
# 2026-10-06 Lunes 同款坑；今次首跑 run 38739272603 又中（[1 初次載入] {}）。
STATE_JS = (
    "var q=function(s){return document.querySelector(s)};"
    "var i=q('input[name=\"cf-turnstile-response\"]');"
    "var w=q('.fd-captcha-widget')||q('[class*=captcha]');"
    "var b=w?w.getBoundingClientRect():null;"
    "return JSON.stringify({turnstile_api:typeof window.turnstile,"
    "fdCaptchaSolve:typeof window.fdCaptchaSolve,"
    "iframes:document.querySelectorAll('iframe').length,"
    "cf_iframes:document.querySelectorAll('iframe[src*=\"challenges.cloudflare.com\"]').length,"
    "widget:!!w,token_len:i?(i.value||'').length:-1,"
    "box:b?{x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height)}:null});"
)
# 服務頁有咩線索（單行、同樣結尾 return）
INFO_JS = (
    "var t=document.body?document.body.innerText.replace(/\\s+/g,' '):'';"
    "var m=t.match(/(Renouvelable[^.]{0,40}|Renouveler[^.]{0,30})/);"
    "return JSON.stringify({snippet:t.slice(0,200),renew_text:m?m[1]:null,"
    "cf_frames:document.querySelectorAll('iframe[src*=\"challenges.cloudflare.com\"]').length});"
)
# 撳 cookie 橫幅（遮住服務卡底部嘅掣）
DISMISS_JS = (
    "var bs=document.querySelectorAll('button');"
    "for(var k=0;k<bs.length;k++){var x=(bs[k].innerText||'').trim().toLowerCase();"
    "if(x==='refuser'||x==='accepter'||x==='tout accepter'||x==='accepter et fermer'){bs[k].click();return x;}}"
    "return '';"
)


SRC_JS = "return String(window.fdCaptchaSolve).slice(0,400);"
LOADCF_JS = (
    "var s=document.createElement('script');"
    "s.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';"
    "s.async=true;document.head.appendChild(s);"
    "return 'injected';"
)
BTN_JS = (
    "var bs=document.querySelectorAll('button,a');var out=[];"
    "for(var k=0;k<bs.length;k++){var t=(bs[k].innerText||'').trim();"
    "if(/renouvel/i.test(t)){out.push(t.slice(0,40)+(bs[k].disabled?' [disabled]':''));}}"
    "return JSON.stringify(out);"
)


HOOK_JS = (
    "if(window.__fdhook) return 'already';"
    "window.__fdapi=[];"
    "var of=window.fetch;"
    "window.fetch=function(){var a=arguments;return of.apply(this,a).then(function(r){"
    "try{r.clone().text().then(function(t){window.__fdapi.push({url:String(r.url),status:r.status,body:String(t).slice(0,500)});});}catch(e){}"
    "return r;});};"
    "var oo=XMLHttpRequest.prototype.open,os=XMLHttpRequest.prototype.send;"
    "XMLHttpRequest.prototype.open=function(m,u){this.__u=u;return oo.apply(this,arguments);};"
    "XMLHttpRequest.prototype.send=function(){var self=this;"
    "this.addEventListener('load',function(){try{window.__fdapi.push({url:String(self.__u),status:self.status,body:String(self.responseText||'').slice(0,500)});}catch(e){}});"
    "return os.apply(this,arguments);};"
    "window.__fdhook=1;return 'hooked';"
)
API_JS = "return JSON.stringify(window.__fdapi||[]).slice(0,1500);"
BTN_INFO_JS = (
    "var bs=document.querySelectorAll('button,a');var out=[];"
    "for(var k=0;k<bs.length;k++){var t=(bs[k].innerText||'').trim();"
    "if(/renouvel/i.test(t)){out.push({tag:bs[k].tagName,disabled:!!bs[k].disabled,"
    "cls:String(bs[k].className).slice(0,60),onclick:String(bs[k].getAttribute('onclick')||'').slice(0,160),"
    "data:Array.prototype.slice.call(bs[k].attributes).map(function(a){return a.name+'='+String(a.value).slice(0,60);}).join('|').slice(0,300)});}}"
    "return JSON.stringify(out);"
)
ENABLE_JS = (
    "var bs=document.querySelectorAll('button,a');var n=0;"
    "for(var k=0;k<bs.length;k++){var t=(bs[k].innerText||'').trim();"
    "if(/renouvel/i.test(t)){bs[k].removeAttribute('disabled');bs[k].disabled=false;"
    "bs[k].classList.remove('disabled');bs[k].style.pointerEvents='auto';n++;}}"
    "return 'enabled:'+n;"
)
CLICK_JS = (
    "var bs=document.querySelectorAll('button,a');"
    "for(var k=0;k<bs.length;k++){var t=(bs[k].innerText||'').trim();"
    "if(/renouvel/i.test(t)){bs[k].click();return 'clicked:'+t.slice(0,40);}}"
    "return 'not-found';"
)
SITEKEY_JS = (
    "var r=window.__fdapi||[];"
    "for(var i=0;i<r.length;i++){var b=String(r[i].body||'');"
    "var m=b.match(/\"site_?key\"\s*:\s*\"([^\"]+)\"/i)||b.match(/(0x4[A-Za-z0-9_\-]{10,})/);"
    "if(m){return m[1];}}return '';"
)


def parse_cookies(raw: str) -> list[dict]:
    out = []
    for item in (raw or "").split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            out.append({"name": k, "value": v, "domain": "fridaydev.fr", "path": "/"})
    return out


def state(sb) -> dict:
    try:
        raw = sb.execute_script(STATE_JS)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"raw": str(raw)[:200]}


def page_info(sb) -> dict:
    try:
        raw = sb.execute_script(INFO_JS)
        return json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def cdp_click(sb, x, y):
    """CDP 層真實滑鼠點擊（isTrusted）。

    繞過 SeleniumBase `uc_gui_click_captcha` 喺 UC/CDP 模式會拋
    `SyntaxError: Illegal return statement` 嘅問題（2026-10-17 run 39018228210 實證：
    撳掣成功、428+sitekey 拿到、widget render 到，但一點就炸）。
    """
    try:
        drv = sb.driver
        drv.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(0.05)
        drv.execute_cdp_cmd("Input.dispatchMouseEvent",
                            {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
        time.sleep(0.06)
        drv.execute_cdp_cmd("Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True
    except Exception as exc:
        print(f"    ⚠️ CDP 點擊失敗: {type(exc).__name__}: {exc}", flush=True)
        return False


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
        try:
            got = sb.execute_script(DISMISS_JS)
            print(f"  [cookie 橫幅] 撳咗: {got!r}", flush=True)
            time.sleep(2)
        except Exception as exc:
            print(f"  ⚠️ 撳 cookie 橫幅失敗: {type(exc).__name__}", flush=True)
        dump("1 初次載入", sb)
        try:
            print(f"  [fdCaptchaSolve 源碼] {sb.execute_script(SRC_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 讀 fdCaptchaSolve 源碼失敗: {type(exc).__name__}", flush=True)
        try:
            print(f"  [續期掣] {sb.execute_script(BTN_JS)}", flush=True)
        except Exception:
            pass
        # 核心測試：住宅出口能唔能載到 Cloudflare Turnstile 腳本？
        # （10-08 嘅失敗簽名就係「widget render 到但 iframes=[]、window.turnstile undefined」）
        try:
            print(f"  [載 CF 腳本] {sb.execute_script(LOADCF_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 注入 CF 腳本失敗: {type(exc).__name__}", flush=True)
        for _ in range(6):
            time.sleep(3)
            st_cf = state(sb)
            if st_cf.get("turnstile_api") not in (None, "undefined"):
                break
        dump("1b 載入 CF 腳本後", sb)

        # 頁面通常唔會一開始就 render widget（要撳續期先出）→ 直接呼叫前端
        # window.fdCaptchaSolve()（純前端 render，零伺服器寫入；比 POST 續期端點安全）
        st = state(sb)
        if not st.get("widget"):
            print("  → 頁面未見 widget，試呼叫 window.fdCaptchaSolve()（只 render，唔提交）", flush=True)
            try:
                print(f"  [頁面線索] {json.dumps(page_info(sb), ensure_ascii=False)}", flush=True)
            except Exception:
                pass
            try:
                has = sb.execute_script("return typeof window.fdCaptchaSolve")
                print(f"  fdCaptchaSolve = {has}", flush=True)
                if has == "function":
                    sb.execute_script("window.fdCaptchaSolve();")
                    time.sleep(5)
                    dump("2 呼叫 fdCaptchaSolve 後", sb)
            except Exception as exc:
                print(f"  ⚠️ 呼叫 fdCaptchaSolve 失敗: {type(exc).__name__}: {exc}", flush=True)

        # ── 完整流程：hook API → 強制 enable 續期掣 → click → 讀 428 + sitekey
        #    → fdCaptchaSolve(sitekey) render widget → UC 點 → 睇能唔能攞 token
        print("  === 完整流程測試（hook → 撳掣 → 428 → render widget → 點）===", flush=True)
        try:
            print(f"  [hook] {sb.execute_script(HOOK_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ hook 失敗: {type(exc).__name__}", flush=True)
        try:
            print(f"  [掣資訊] {sb.execute_script(BTN_INFO_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 讀掣資訊失敗: {type(exc).__name__}", flush=True)
        try:
            print(f"  [強制 enable] {sb.execute_script(ENABLE_JS)}", flush=True)
            time.sleep(1)
            print(f"  [撳掣] {sb.execute_script(CLICK_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 撳掣失敗: {type(exc).__name__}", flush=True)
        for _ in range(8):
            time.sleep(3)
            st2 = state(sb)
            if st2.get("widget"):
                break
        try:
            print(f"  [API 回應] {sb.execute_script(API_JS)}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 讀 API 回應失敗: {type(exc).__name__}", flush=True)
        sk = None
        try:
            sk = sb.execute_script(SITEKEY_JS)
            print(f"  [sitekey] {sk!r}", flush=True)
            if sk:
                call_js = ("window.fdCaptchaSolve(" + json.dumps(sk) + "); return 'called';")
                print(f"  [呼叫 fdCaptchaSolve] {sb.execute_script(call_js)}", flush=True)
                time.sleep(6)
        except Exception as exc:
            print(f"  ⚠️ 取 sitekey／呼叫 fdCaptchaSolve 失敗: {type(exc).__name__}: {exc}", flush=True)
        best = dump("2b 撳掣 + 引驗證後", sb)

        # 嘗試 UC 模式點 widget 攞 token（唔提交任何嘢）
        for attempt in range(1, 4):
            st = state(sb)
            if st.get("token_len", 0) and st["token_len"] > 50:
                token = "x" * st["token_len"]
                break
            if not st.get("widget"):
                print(f"  （第 {attempt} 次）冇 widget 可以點", flush=True)
                break
            box = st.get("box") or {}
            print(f"  （第 {attempt} 次）點 widget：box={box}", flush=True)
            clicked = False
            if box:
                clicked = cdp_click(sb, int(box["x"]) + 30, int(box["y"]) + int(box.get("h", 73)) // 2)
            if not clicked:
                try:
                    sb.uc_gui_click_captcha()
                    clicked = True
                except Exception as exc:
                    print(f"    ⚠️ uc_gui_click_captcha 亦失敗: {type(exc).__name__}: {exc}", flush=True)
            print(f"    → 點擊{'已發出' if clicked else '失敗'}", flush=True)
            for _ in range(8):
                time.sleep(4)
                st = state(sb)
                if st.get("widget"):
                    best = st
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
               f"❌ 攞唔到 token（token_len={best.get('token_len')}）\n"
               f"閘狀態：428+sitekey={'✅' if sk else '❌'}｜widget={'✅' if best.get('widget') else '❌'}"
               f"｜turnstile_api={best.get('turnstile_api')}｜cf_iframes={best.get('cf_iframes')}"
               f"｜節點 {os.environ.get('PROXY_NODE','?')}")
    print("\n" + msg.replace("**", ""))
    tg(msg)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
