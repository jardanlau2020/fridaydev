#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Friday(fridaydev.fr) 自动续期（单账号 Cookie 版）
# Secrets：
#   COOKIE_VALUE（首选；可填整串 "remember_web_<hash>=<值>"，也可只填值）
#   COOKIE（兼容旧 Secrets；同上）
#   EMAIL（可选，通知用备注名）
#   全局共用：GH_TOKEN / TG_BOT_TOKEN / TG_CHAT_ID / NODE_LINK(代理，可选)

import os, re, sys, time, random, requests, subprocess
from datetime import datetime

try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

GH_TOKEN      = os.environ.get("GH_TOKEN") or ""
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""

BASE_URL      = "https://fridaydev.fr"
SERVICES_URL   = f"{BASE_URL}/services/"
DOMAIN        = "fridaydev.fr"
REMEMBER_WEB_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
AUTH_CHECK_URL = "/php/auth.php"

# 登录凭证：长命的是 remember_web_<hash>（后缀随站点变，只认前缀），后两个为改版前的旧格式
AUTH_COOKIE_NAMES = ("user_id", "PHPSESSID")
AUTH_COOKIE_PREFIXES = ("remember_web_",)

IS_PROXY      = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER  = os.environ.get("PROXY_SERVER") or "http://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None
HEADLESS      = os.environ.get("HEADLESS", "false").lower() == "true"
VERBOSE       = (os.environ.get("VERBOSE") or "false").lower() == "true"

# Cloudflare 整页挑战 / Turnstile 组件共用的 iframe 选择器
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

# Cookie 剩余有效期低于此天数则 TG 预警
EXPIRY_WARN_DAYS = 3


if sys.platform == 'win32':
    DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
else:
    DEFAULT_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'


STEALTH_JS = """
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def vlog(message):
    if VERBOSE:
        log(message)


def is_auth_cookie(name) -> bool:
    name = name or ""
    return name in AUTH_COOKIE_NAMES or name.startswith(AUTH_COOKIE_PREFIXES)


def cookie_label(name: str) -> str:
    if not name.startswith(AUTH_COOKIE_PREFIXES) or len(name) <= 18:
        return name
    return name[:18] + "…"


def parse_cookie_str(raw: str):
    """把 "a=b; c=d" 解析成 [(name, value)]，值按第一个 = 切分，值内含 = 也安全。"""
    pairs = []
    for item in (raw or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append((name, value))
    return pairs


def get_account():
    """单账号：首选 COOKIE_VALUE，其次兼容旧 COOKIE。"""
    raw = (os.environ.get("COOKIE_VALUE") or os.environ.get("COOKIE") or "").strip()
    if not raw:
        return None
    source = "COOKIE_VALUE" if (os.environ.get("COOKIE_VALUE") or "").strip() else "COOKIE"
    pairs = parse_cookie_str(raw)
    if not pairs and "=" not in raw:
        # eooce 原脚手架的 COOKIE_VALUE 只填值；Friday 登录仍需要带上完整名称。
        pairs = [(REMEMBER_WEB_NAME, raw)]
        raw = f"{REMEMBER_WEB_NAME}={raw}"
    return {"cookie_raw": raw, "email": (os.environ.get("EMAIL") or "").strip(),
            "secret_name": source, "pairs": pairs}


def mask_email(email: str) -> str:
    if "@" in email:
        name, domain = email.split("@", 1)
        return f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    return (email[:2] + "****") if email else "（未填）"


def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 文字通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


def send_telegram_photo(message: str, image_path: str):
    """TG 附截图通知，截图不存在或未配置则退化为纯文字。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        if image_path and os.path.isfile(image_path):
            with open(image_path, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": TG_CHAT_ID, "caption": message[:1000]},
                    files={"photo": (os.path.basename(image_path), f, "image/png")},
                    timeout=20)
            if r.status_code == 200:
                print("✅ Telegram 截图通知已发送")
                return
            print(f"⚠️ sendPhoto 失败({r.status_code})，降级为文字通知")
    except Exception as e:
        print(f"⚠️ 截图通知异常，降级为文字通知: {e}")
    send_telegram_message(message)


def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    print(f"🔄 更新 Secret: {secret_name}（长度 {len(new_value)}）")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(["gh", "secret", "set", secret_name, "--body", new_value],
                              capture_output=True, text=True, timeout=30, check=False, env=env)
        if proc.returncode == 0:
            return True
        print(f"❌ 更新失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False


def format_notification(status: str, email: str = "", extra: str = "",
                        error: str = "", old_due: str = "", new_due: str = "",
                        current_ip: str = "未知") -> str:
    """eooce 同款：标题 + 状态 + 脱敏账号 + 前后到期双行 + 出口 IP + 北京时间。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    lines = ["🎉 Friday 续期通知", "", f"{status}", f"👤 账号: {mask_email(email)}"]
    lines.append(f"📅 续期前到期：{old_due or '（未获取到）'}")
    lines.append(f"📅 续期后到期：{new_due or '（未获取到）'}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"📝 {error}")
    lines.append(f"🌐 续期使用IP: {current_ip}")
    lines.append(f"🕒 续期时间：{now}")
    return "\n".join(lines)


def get_current_ip(proxy_server=None):
    """获取当前出口 IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def extract_dates(page_text: str):
    """提取页面内 DD/MM/YYYY 日期（法语站格式），返回 datetime 列表。"""
    out = []
    for m in re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", page_text or ""):
        try:
            out.append(datetime(int(m[2]), int(m[1]), int(m[0])))
        except ValueError:
            pass
    return out


def extract_renewal_date(page):
    """优先从 .service-info 精确提取到期日，找不到回落整页正则。"""
    try:
        infos = page.locator(".service-info").all_inner_texts()
        for t in infos:
            if "renouvellement" in (t or "").lower():
                m = re.search(r"(\d{2})/(\d{2})/(\d{4})", t)
                if m:
                    d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                    vlog(f"🎯 精确到期日 (.service-info): {d.strftime('%d/%m/%Y')}")
                    return d
    except Exception:
        pass
    return None


def cookie_expiry_warning(context) -> str:
    """检查长凭证 Cookie 的剩余有效期，取最宽松的那个作判据。"""
    try:
        candidates = [c for c in context.cookies()
                      if is_auth_cookie(c.get("name")) and (c.get("expires") or 0) > 0]
    except Exception as e:
        print(f"⚠️ Cookie 有效期检查异常: {e}")
        return ""
    if not candidates:
        vlog("ℹ️ 浏览器里没读到带过期时间的登录 Cookie，跳过有效期预警")
        return ""
    c = max(candidates, key=lambda x: x.get("expires") or 0)
    name = cookie_label(c.get("name") or "")
    remain_days = (datetime.fromtimestamp(c["expires"]) - datetime.now()).total_seconds() / 86400
    exp_str = datetime.fromtimestamp(c["expires"]).strftime("%Y-%m-%d")
    if remain_days < EXPIRY_WARN_DAYS:
        return (f"🔔 Cookie 告警：{name} 仅剩 {remain_days:.1f} 天"
                f"（约 {exp_str} 过期），请尽快从浏览器重拷 Secrets 更新")
    print(f"🔑 {name} 剩余有效期约 {remain_days:.1f} 天（{exp_str} 过期）")
    return ""


def rebuild_cookie_str(context, old_raw: str) -> str:
    """从浏览器最新 Cookie 重建 Secrets 串；浏览器换了名的旧 remember_web_* 一并清掉。"""
    try:
        cookies = context.cookies()
    except Exception:
        cookies = []
    browser = {c.get("name"): c.get("value") for c in cookies
               if c.get("value") and is_auth_cookie(c.get("name"))}
    old = dict(parse_cookie_str(old_raw))
    merged = dict(old)
    changed = False
    for name, value in browser.items():
        if value != old.get(name):
            merged[name] = value
            changed = True
    stale = [n for n in merged
             if n.startswith(AUTH_COOKIE_PREFIXES) and n not in browser]
    if stale and browser:
        for name in stale:
            del merged[name]
        changed = True
    if not changed:
        return ""
    return "; ".join(f"{k}={v}" for k, v in merged.items() if v)


def _get_turnstile_token(page):
    """读取 Turnstile 验证通过后生成的响应 token，未通过时返回 None。"""
    try:
        return page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
                            if (t && t.length > 30) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[id$="_response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 30) return el.value;
                }
                return null;
            }
            """
        )
    except Exception:
        return None


def _find_visible_cf_frames(page):
    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = handle.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return result


def _cf_checkbox_visible(page):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False


def _click_cf_checkbox(page, frame_el=None):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False


SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required',
                        'checking your browser', 'please wait', 'one more step')


def _is_security_check_page(page):
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate(
            """
            () => {
                const t = document.body ? document.body.innerText.slice(0, 5000).toLowerCase() : '';
                return t.includes('verify you are human') || t.includes('checking your browser')
                    || t.includes('security verification');
            }
            """
        )
    except Exception:
        return True


def _has_interstitial_iframe(page):
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (handle.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False


def _click_security_submit(page):
    for text in ('Verify', 'Continue', 'Submit', 'Proceed'):
        try:
            btn = page.locator(f'button:has-text("{text}"):visible, input[type="submit"]:visible').first
            if btn.count() > 0:
                btn.click(timeout=3000)
                log(f"🖱️ 已点击安全验证页的确认按钮（{text}）...")
                return True
        except Exception:
            continue
    return False


def _dump_security_page(page):
    try:
        page.screenshot(path="security_page.png")
    except Exception as e:
        log(f"🔍 验证页截图采集失败: {e}")


def handle_cloudflare(page, timeout=240):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)

    if not challenge_active():
        for _ in range(3):
            time.sleep(1)
            if challenge_active():
                break
        else:
            return True

    log("🔒 检测到 Cloudflare 安全验证...")
    time.sleep(5)
    _dump_security_page(page)

    effective_timeout = timeout + (180 if timeout > 60 else 0)
    manual_hinted = False
    start_time = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start_time < effective_timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2:
                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if not manual_hinted and timeout > 60 and time.time() - start_time > timeout - 30:
            log("🤝 自动点击未能通过验证，脚本会继续等待...")
            manual_hinted = True
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Turnstile 验证...")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(3, 5))
                    continue
            elif _get_turnstile_token(page) and _is_security_check_page(page):
                if _click_security_submit(page):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("❌ 验证超时。")
    try:
        page.screenshot(path="security_timeout.png")
    except Exception:
        pass
    return False


def solve_modal_turnstile(page, timeout=90):
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    no_frame_seconds = 0
    while time.time() - start < timeout:
        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            no_frame_seconds += 1
            if no_frame_seconds >= 20 and clicks == 0:
                log("ℹ️ 未检测到 Turnstile 验证框，无需验证。")
                return True
            time.sleep(1)
            continue
        no_frame_seconds = 0
        if clicks > 0 and time.time() - last_click > 4 and not _cf_checkbox_visible(page):
            time.sleep(2)
            if not _cf_checkbox_visible(page):
                log("✅ Turnstile 复选框已消失，视为验证通过！")
                return True
        if time.time() - last_click > 6 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        if clicks == 0 and time.time() - start > 20 and time.time() - last_click > 6:
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        time.sleep(1)
    if _get_turnstile_token(page):
        log("✅ Turnstile 验证通过！")
        return True
    log("❌ Turnstile 验证超时。")
    return False


def _click_fd_captcha_widget(page):
    try:
        widget = page.locator(".fd-captcha-widget").first
        if widget.count():
            box = widget.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
    except Exception:
        pass
    try:
        card = page.locator(".fd-captcha-card").first
        if card.count():
            box = card.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
    except Exception:
        pass
    return False


def solve_free_captcha(page, tag) -> bool:
    """Friday 免费续期 428 时的 fdCaptcha 组件：静等自解，必要时点验证框。"""
    backdrop = page.locator(".fd-captcha-backdrop")
    try:
        has_backdrop = backdrop.count() > 0 and backdrop.first.is_visible(timeout=8000)
    except Exception:
        has_backdrop = False
    if not has_backdrop:
        return True

    log(f"{tag} 🔒 弹出反机器人验证，先静等自动通过...")
    for _ in range(15):
        page.wait_for_timeout(2000)
        try:
            gone = backdrop.count() == 0 or not backdrop.first.is_visible(timeout=1000)
        except Exception:
            gone = True
        if gone:
            log(f"{tag} ✅ 验证自动通过")
            return True

    for attempt in range(1, 4):
        try:
            _click_fd_captcha_widget(page)
        except Exception as e:
            vlog(f"{tag} ⚠️ 点击验证出错: {e}")
        for _ in range(6):
            page.wait_for_timeout(2000)
            try:
                gone = backdrop.count() == 0 or not backdrop.first.is_visible(timeout=1000)
            except Exception:
                gone = True
            if gone:
                log(f"{tag} ✅ 验证层已消失")
                return True
            token = _get_turnstile_token(page)
            if token:
                log(f"{tag} ✅ 已拿到验证票据（长度 {len(token)}）")
                return True
        log(f"{tag} ⏳ 第 {attempt} 次验证未通过，重试...")
    log(f"{tag} ❌ 验证最终未通过")
    return False


def open_browser(p):
    """启动反检测内核浏览器，返回 (browser, context, page)。"""
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    if USING_PATCHRIGHT:
        browser = p.chromium.launch(channel="chrome", headless=HEADLESS,
                                    args=['--disable-infobars'], proxy=proxy_arg)
        context = browser.new_context(viewport=None, proxy=proxy_arg)
        page = context.new_page()
        return browser, context, page

    log("⚠️ 未安装 patchright（建议 pip install patchright），退回原生 playwright")
    browser = p.chromium.launch(
        channel="chrome", headless=HEADLESS,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars'],
        proxy=proxy_arg,
    )
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=DEFAULT_UA,
        proxy=proxy_arg,
    )
    page = context.new_page()
    page.add_init_script(STEALTH_JS)
    return browser, context, page


def dismiss_cookie_banner(page):
    for text in ("Accepter", "Accept", "Tout accepter"):
        try:
            btn = page.locator("button", has_text=text).first
            if btn.count() and btn.is_visible(timeout=1000):
                btn.click(timeout=1000)
                log("🍪 已关闭 Cookie 横幅")
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def _is_logged_in(page):
    """是否真正处于登录后状态：优先问站点自己的 /php/auth.php。"""
    try:
        if DOMAIN not in (page.url or ''):
            return False
        if '/login' in (page.url or ''):
            return False
        result = page.evaluate(
            """
            async () => {
                try {
                    const r = await fetch('/php/auth.php', {
                        headers: { 'X-Requested-With': 'XMLHttpRequest' },
                        credentials: 'include',
                        cache: 'no-store'
                    });
                    const text = await r.text();
                    let logged = null;
                    try { logged = JSON.parse(text).isLoggedIn; } catch (e) {}
                    return { status: r.status, logged };
                } catch (e) {
                    return { status: 0, logged: null, error: String(e) };
                }
            }
            """
        )
        status = result.get("status") if isinstance(result, dict) else 0
        logged = result.get("logged") if isinstance(result, dict) else None
        if logged is True:
            return True
        if logged is False or status == 401:
            return False
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    return "Mes services" in body and "/services" in (page.url or '')


def login_with_cookie(page, context, pairs) -> bool:
    if not pairs:
        log("❌ COOKIE 为空或格式错误（应为 a=b; c=d，或 COOKIE_VALUE 只填值）")
        return False
    names = [n for n, _ in pairs]
    if not any(is_auth_cookie(n) for n in names):
        log(f"⚠️ COOKIE 里没有登录凭证（期望 remember_web_* / user_id / PHPSESSID），当前: {names}")

    page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
    handle_cloudflare(page, timeout=240)
    try:
        context.clear_cookies()
    except Exception:
        pass
    cookies = []
    for name, value in pairs:
        cookies.append({
            'name': name,
            'value': value,
            'domain': f'.{DOMAIN}',
            'path': '/',
            'httpOnly': True,
            'secure': True,
            'sameSite': 'Lax',
        })
    try:
        context.add_cookies(cookies)
    except Exception as e:
        log(f"⚠️ 注入 Cookie 失败: {e}")
        return False

    page.goto(SERVICES_URL, wait_until="domcontentloaded", timeout=60000)
    handle_cloudflare(page, timeout=240)
    page.wait_for_timeout(5000)
    dismiss_cookie_banner(page)

    if _is_logged_in(page):
        log("✅ Cookie 登录成功，已到达服务页")
        return True
    log(f"❌ Cookie 登录失败（auth.php 返回未登录；需 fridaydev.fr 域下的 Cookie，"
        f"非 control.fridaydev.fr），URL={page.url}")
    return False


def login_with_password(page) -> bool:
    """账密备用通道（MVP 仅 Cookie 登录）。"""
    log("ℹ️ 账密备用通道尚未实现（MVP 仅 Cookie 登录）")
    return False


def login(page, context, cookie_pairs) -> bool:
    if login_with_cookie(page, context, cookie_pairs):
        return True
    log("🔄 Cookie 登录失败，尝试账密备用通道...")
    return login_with_password(page)


def _element_text(el):
    try:
        return (el.inner_text(timeout=500) or '').strip()
    except Exception:
        return ''


def find_renew_button(page):
    """找可点的续期按钮；返回 (locator, text)。倒计时态和禁用按钮视为不可续期。"""
    for selector in (".js-free-renew", ".btn-renew", "button[data-uuid]"):
        try:
            el = page.locator(selector).first
            if el.count():
                try:
                    if el.is_disabled(timeout=500):
                        continue
                except Exception:
                    pass
                if el.is_visible(timeout=1500):
                    t = _element_text(el)
                    if t and not re.search(r"dans\s*\d+\s*jour", t, re.IGNORECASE):
                        uuid = el.get_attribute("data-uuid") or ""
                        vlog(f"🎯 命中功能选择器 ({selector}, data-uuid={uuid}): '{t}'")
                        return el, t
        except Exception:
            continue

    seen = []
    try:
        buttons = page.locator("button, a")
        count = buttons.count()
    except Exception:
        return None, ""
    for i in range(count):
        el = buttons.nth(i)
        try:
            if not el.is_visible(timeout=200):
                continue
            try:
                if el.is_disabled(timeout=200):
                    continue
            except Exception:
                pass
            t = _element_text(el)
            if not t:
                continue
            seen.append(t[:60])
            if (re.search(r"Renouvel", t, re.IGNORECASE)
                    and not re.search(r"dans\s*\d+\s*jour", t, re.IGNORECASE)):
                return el, t
        except Exception:
            continue
    if seen:
        log(f"🔍 未命中续期按钮，可见按钮文案: {seen[:15]}")
    return None, ""


def find_not_yet_text(page_text: str) -> str:
    m = re.search(r"Renouvelable dans\s*(\d+)\s*jours?", page_text or "", re.IGNORECASE)
    return f"{m.group(1)} 天" if m else ""


def click_modal_confirm(page, tag="") -> bool:
    """fdui 确认弹窗：验证在前、弹窗在后；成功 POST 由前端自动发出，ghost 点击只是收尾。"""
    if not solve_free_captcha(page, tag):
        try:
            page.screenshot(path="captcha_fail.png")
        except Exception:
            pass
        return False
    try:
        page.locator(".fdui-overlay.fdui-open .fdui-modal").wait_for(state="visible", timeout=15000)
    except Exception:
        log("⚠️ 未探测到 fdui 确认弹窗，走旧选择器兜底")
    try:
        buttons = page.locator(".fdui-overlay.fdui-open .fdui-actions button")
        cands = []
        for i in range(buttons.count()):
            b = buttons.nth(i)
            try:
                if b.is_visible(timeout=500):
                    cands.append((b, _element_text(b)))
            except Exception:
                continue
        vlog(f"📝 确认区按钮: {[t for _, t in cands] or '（空）'}")
        order = []
        for b, t in cands:
            tl = t.lower()
            if "annuler" in tl or "cancel" in tl:
                continue
            if re.search(r"garder|conserver|gratuit|offre|confirmer|valider|renew|^ok$", tl):
                order.insert(0, (b, t))
            else:
                order.append((b, t))
        if not order and cands:
            order = [cands[-1]]
        for b, t in order:
            try:
                b.click(timeout=5000)
                log(f"✅ 已点击确认按钮: '{t[:60]}'")
                return True
            except Exception:
                continue
    except Exception as e:
        log(f"⚠️ 枚举确认按钮异常: {e}")
    for sel in (".modal.show button.btn-primary", ".modal.show button",
                ".swal2-confirm", 'button:has-text("Confirmer")',
                'button:has-text("Valider")', 'button:has-text("Confirm")'):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible(timeout=1000):
                btn.click(timeout=3000)
                log(f"✅ 已点击确认弹窗 ({sel})")
                return True
        except Exception:
            pass
    log("⚠️ 未找到确认弹窗按钮（可能无需确认，直接检查结果）")
    return False


def close_success_notice(page, tag=""):
    """关闭成功 toast/弹窗；绝不碰 .fd-captcha-cancel（取消订单）。"""
    try:
        btn = page.locator(".fdui-toast-close").first
        if btn.count() and btn.is_visible(timeout=3000):
            txt = ""
            try:
                txt = page.locator(".fdui-toast").first.inner_text(timeout=1000).strip().replace("\n", " ")[:120]
            except Exception:
                pass
            btn.click(timeout=3000)
            log(f"{tag} 🔕 已关闭成功通知: '{txt}'")
            return True
    except Exception:
        pass
    try:
        body = page.locator(".fdui-overlay.fdui-open .fdui-modal").inner_text(timeout=1000) or ""
    except Exception:
        body = ""
    if body and re.search(r"renouvel|success|succès|5 jours", body, re.IGNORECASE):
        vlog(f"{tag} 🎉 成功弹窗: '{body.strip().replace(chr(10), ' ')[:120]}'")
        for sel in ('.fdui-overlay.fdui-open [data-act="ok"]',
                    '.fdui-overlay.fdui-open .fdui-actions button:last-child'):
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=3000):
                    btn.click(timeout=3000)
                    log(f"{tag} 🔕 已关闭成功弹窗 ({sel})")
                    return True
            except Exception:
                continue
    return False


def renew(page, context, cookie_pairs, cookie_raw, email, current_ip="未知") -> dict:
    result = {"ok": False, "summary": "未知"}
    shot = "result.png"

    if not login(page, context, cookie_pairs):
        msg = format_notification("❌ 登录失败", email=email, current_ip=current_ip,
                                  error="Cookie 已失效，请从浏览器重拷 Secrets 更新")
        send_telegram_message(msg)
        result["summary"] = "❌ 登录失败（Cookie 失效）"
        return result

    try:
        page_text = page.locator("body").inner_text(timeout=10000)
    except Exception:
        page_text = ""
    old_dates = extract_dates(page_text)
    old_max = extract_renewal_date(page) or (max(old_dates) if old_dates else None)
    log(f"📅 当前页面日期: {[d.strftime('%d/%m/%Y') for d in old_dates] or '（未提取到）'}")

    not_yet = find_not_yet_text(page_text)
    btn, btn_text = find_renew_button(page)

    if btn is None and not_yet:
        log(f"⏳ 未到续期时间：{not_yet} 后可续")
        send_telegram_message(format_notification(
            "⏳ 未到续期时间", email=email, current_ip=current_ip,
            extra=f"⏱️ {not_yet}后可续",
            old_due=old_max.strftime("%d/%m/%Y") if old_max else "（未获取到）"))
        result.update(ok=True, summary=f"⏳ 未到时间（{not_yet}后）")

    elif btn is not None:
        tag = f"[{email}]" if email else ""
        log(f"✅ 发现续期按钮: '{btn_text}'，点击...")
        try:
            btn.click(timeout=10000)
        except Exception as e:
            err = f"点击续期按钮失败: {e}"
            log(f"❌ {err}")
            send_telegram_message(format_notification("❌ 续期失败", email=email,
                                                      current_ip=current_ip, error=err))
            result["summary"] = "❌ 续期失败（点击按钮出错）"
            return result
        page.wait_for_timeout(4000)
        handle_cloudflare(page, timeout=120)

        # 若续期动作触发了 CF/Turnstile 组件框，走基座的弹窗验证；Friday 的 fdCaptcha 在后续确认流程处理。
        cf_frame = page.locator(CF_IFRAME_SELECTOR)
        try:
            has_cf_frame = cf_frame.count() > 0 and cf_frame.first.is_visible(timeout=2000)
        except Exception:
            has_cf_frame = False
        if has_cf_frame and not solve_modal_turnstile(page, timeout=90):
            try:
                page.screenshot(path="cf_fail.png")
            except Exception:
                pass
            err = "CF 验证未通过（Turnstile 90s 未出票据），请检查节点纯净度后手动重试"
            log(f"❌ {err}")
            send_telegram_message(format_notification("❌ 续期失败", email=email,
                                                      current_ip=current_ip, error=err))
            result["summary"] = "❌ 续期失败（CF 验证未通过）"
            return result

        click_modal_confirm(page, tag)
        page.wait_for_timeout(3000)
        close_success_notice(page, tag)
        page.wait_for_timeout(5000)
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            page.goto(SERVICES_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=120)
        page.wait_for_timeout(5000)
        try:
            new_text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            new_text = ""
        new_dates = extract_dates(new_text)
        new_max = extract_renewal_date(page) or (max(new_dates) if new_dates else None)
        ok = False
        if old_max and new_max and new_max > old_max:
            ok = True
        elif "Actif" in new_text and "Suspendu" not in new_text and not find_not_yet_text(new_text):
            ok = True  # 日期未变但状态活跃（可能续期即时生效无日期变化）
        try:
            page.screenshot(path=shot)
        except Exception:
            shot = ""
        if ok:
            log(f"✅ 续期成功，新日期: {[d.strftime('%d/%m/%Y') for d in new_dates] or '（状态活跃）'}")
            send_telegram_photo(format_notification(
                "✅ 续期成功", email=email, current_ip=current_ip,
                old_due=old_max.strftime("%d/%m/%Y") if old_max else "",
                new_due=new_max.strftime("%d/%m/%Y") if new_max else "（状态活跃）"), shot)
            result.update(ok=True,
                          summary=f"✅ 续期成功（到期 {new_max.strftime('%d/%m/%Y') if new_max else '活跃'}）")
        else:
            log("⚠️ 续期结果未知，请手动检查")
            send_telegram_photo(format_notification(
                "⚠️ 续期可能未成功", email=email, current_ip=current_ip, extra="请登录后台检查",
                old_due=old_max.strftime("%d/%m/%Y") if old_max else "（未获取到）"), shot)
            result["summary"] = "⚠️ 结果未知（请手动检查）"
    else:
        log("ℹ️ 未找到续期按钮/倒计时，状态未知")
        send_telegram_message(format_notification(
            "ℹ️ 状态未知", email=email, current_ip=current_ip, extra="未找到续期按钮，请手动检查",
            old_due=old_max.strftime("%d/%m/%Y") if old_max else "（未获取到）"))
        result["summary"] = "ℹ️ 状态未知"

    warn = cookie_expiry_warning(context)
    if warn:
        log(warn)
        send_telegram_message(format_notification("🔔 Cookie 有效期预警", email=email,
                                                  current_ip=current_ip, extra=warn))
    new_raw = rebuild_cookie_str(context, cookie_raw)
    if new_raw:
        vlog("🔄 浏览器 Cookie 有更新，回写 Secrets...")
        if GH_TOKEN:
            secret_name = (os.environ.get("COOKIE_VALUE") or "").strip() and "COOKIE_VALUE" or "COOKIE"
            log('✅ 回写成功' if update_github_secret(secret_name, new_raw)
                else '⚠️ 回写失败，请检查 GH_TOKEN')
        else:
            log("⚠️ 未设置 GH_TOKEN，无法自动回写")
    else:
        vlog("✅ Cookie 无需更新")
    return result


def main():
    log(f"🔍 凭证检测: COOKIE_VALUE={'已配置' if os.environ.get('COOKIE_VALUE') else '未配置'}, "
        f"COOKIE={'已配置' if os.environ.get('COOKIE') else '未配置'}, EMAIL={'已配置' if os.environ.get('EMAIL') else '未配置'}")
    acct = get_account()
    if not acct:
        log("❌ 缺少登录凭证")
        sys.exit(1)

    if IS_PROXY:
        log(f"⚙️ 代理已启用: {PROXY_SERVER}")
    else:
        log("🌐 直连模式（未使用代理）")

    current_ip = get_current_ip(PROXY_SERVER if IS_PROXY else None)
    log(f"🎯 当前出口IP: {current_ip}")

    with sync_playwright() as p:
        browser = None
        try:
            browser, context, page = open_browser(p)
            result = renew(page, context, acct["pairs"], acct["cookie_raw"], acct["email"], current_ip)
        except Exception as e:
            log(f"❌ 执行异常: {e}")
            import traceback
            traceback.print_exc()
            result = {"ok": False, "summary": f"❌ 执行异常：{e}"}
            try:
                send_telegram_message(format_notification(
                    "❌ 续期失败", email=acct["email"], current_ip=current_ip, error=f"执行异常：{e}"))
            except Exception:
                pass
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    log(f"🏁 执行完毕：{result['summary']}")
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
