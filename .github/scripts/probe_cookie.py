#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：拿 COOKIE / COOKIE_VALUE 问站点的 /php/auth.php，看 isLoggedIn。

不点续期、不改任何东西。cookie 解析直接复用 app.py 的 get_account()，
保证和真续期那一轮走的解析路径一模一样。

用法：python probe_cookie.py direct|proxy
退出时在 result.txt 写 alive / dead / inconclusive / nocookie
"""

import os
import sys

import requests

sys.path.insert(0, os.getcwd())
import app  # noqa: E402


def write_result(v: str) -> None:
    with open("result.txt", "w", encoding="utf-8") as f:
        f.write(v)
    print("PROBE_RESULT=" + v, flush=True)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"

    acct = app.get_account()
    if not acct:
        print("❌ COOKIE / COOKIE_VALUE 都没配")
        write_result("nocookie")
        return

    pairs = acct["pairs"]
    names = [n for n, _ in pairs]
    print(f"模式: {mode}")
    print(f"secret 来源: {acct['secret_name']}")
    print(f"cookie 条目数: {len(pairs)}，名称: {names}")
    print(f"cookie 串长度: {len(acct['cookie_raw'])}（值不打印）")
    print(f"含长命凭证 remember_web_*: {any(n.startswith('remember_web_') for n in names)}")

    s = requests.Session()
    for n, v in pairs:
        s.cookies.set(n, v, domain="fridaydev.fr")
        s.cookies.set(n, v, domain=".fridaydev.fr")

    proxies = None
    if mode == "proxy":
        p = "http://127.0.0.1:7890"
        proxies = {"http": p, "https": p}

    url = app.BASE_URL + app.AUTH_CHECK_URL
    print(f"GET {url}")
    try:
        r = s.get(
            url,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/131.0.0.0 Safari/537.36"),
            },
            proxies=proxies,
            timeout=45,
        )
    except Exception as e:
        print(f"❌ 请求异常: {e}")
        write_result("inconclusive" if mode == "direct" else "dead")
        return

    body = r.text or ""
    print(f"HTTP {r.status_code}，长度 {len(body)}")
    print("BODY(前 400):", body[:400].replace("\n", " "))

    low = body.lower()
    cf_hit = ("just a moment" in low or "__cf_chl" in low
              or "cdn-cgi/challenge" in low or "checking your browser" in low)
    print("CF 挑战页特征:", cf_hit)

    try:
        j = r.json()
    except Exception:
        j = None

    if isinstance(j, dict) and "isLoggedIn" in j:
        ok = bool(j["isLoggedIn"])
        print("isLoggedIn =", j["isLoggedIn"])
        print("✅ cookie 有效" if ok else "❌ cookie 已失效")
        write_result("alive" if ok else "dead")
        return

    # 拿不到 JSON：CF 拦 / 站点改版 / 其它
    if cf_hit or r.status_code in (403, 503):
        print("⚠️ 被 Cloudflare 拦，本次判定不成立")
    else:
        print("⚠️ auth.php 没返回 isLoggedIn（可能站点改版）")
    write_result("inconclusive")


if __name__ == "__main__":
    main()
