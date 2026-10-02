#!/usr/bin/env python3
"""fridaydev/renew.py 的验收 harness。

四段：
    [A] 纯逻辑     —— 状态字映射、报告退出码、esc_html、fmt_date、build_tg、
                      parse_cookies、current_mode
    [B] 场景矩阵   —— watchdog / renew 两条路的每个出口，浏览器整层换替身
    [C] 静态与接线 —— 「迁移掉的东西不得回归」+ workflow 与代码对得上
    [D] 真子进程   —— py_compile + 真的 import 一次 + 真的跑一次 main()

为什么要有 [C]：迁移这类重构最容易的退步不是逻辑错，而是**旧实现偷偷回来**
（比如有人把 `_clip` 又抄一遍），或者 workflow 与脚本的约定漂移
（脚本要 COOKIE，workflow 忘了传）。这些静态就能钉死，比跑一遍便宜得多。

两层源码视图（沿用 1ifecycle 那边的约定）：
    SRC  —— 原始文本，用来断言「某个字面量在不在」
    CODE —— 注释与字符串字面量被挖空（保留行列），用来断言「某个标识符/结构在不在」
没有这层区分的话，「本文件不许出现 _clip(」会被 docstring 里
「_clip() -> renewkit.shorten」这句说明自己撞红。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import time as _real_time
import tokenize
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/fridaydev
SCRIPT = ROOT / "renew.py"
WORKFLOW = ROOT / ".github" / "workflows" / "renew.yml"
README = ROOT / "README.md"

#: 迁移前的状态字 → 退出码，用来证明新映射没把语义改掉
OLD_EXIT_CODES = {
    "ok": 0, "skip": 0, "manual": 2, "cookie": 3, "fail": 1, "error": 1,
}
NEW_EXIT_CODES = {
    "ok": 0, "skip": 0, "manual": 1, "cookie": 1, "fail": 1, "error": 1,
}

RENEWKIT_REF_RE = re.compile(r"jardanlau2020/renew-kit/\.github/actions/renew@v\d")


# ----------------------------------------------------------------- 基础设施

class Checks:
    def __init__(self) -> None:
        self.ok = 0
        self.fails: list[str] = []
        self.skips: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, name: str, cond: bool, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  \u2705 {name}")
        else:
            tag = f"  [{extra}]" if extra else ""
            self.fails.append(name + tag)
            print(f"  \u274c {name}{tag}")
        return bool(cond)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def skip(self, name: str, why: str) -> None:
        self.skips.append(name)
        print(f"  \u26aa SKIP {name} — {why}")

    def report(self) -> int:
        print("\n" + "=" * 62)
        total = self.ok + len(self.fails)
        if self.fails:
            print(f"\u274c {len(self.fails)}/{total} 项失败")
            for f in self.fails:
                print(f"   - {f}")
        else:
            print(f"\u2705 全部通过（{self.ok} 项）"
                  + (f"，{len(self.skips)} 项跳过" if self.skips else ""))
        return 1 if self.fails else 0


#: 要挖空的 token：注释、普通字符串，以及 3.12+ 的 f-string 三段
_BLANK_TOKENS = {tokenize.COMMENT, tokenize.STRING}
for _extra in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _t = getattr(tokenize, _extra, None)
    if _t is not None:
        _BLANK_TOKENS.add(_t)


def code_only(src: str) -> str:
    """把注释与字符串字面量挖空（**保留行列位置**），只留代码结构。

    tokenize 是逐 token 定位的，挖空后行号列号都不变，
    跨行正则（`[\\s\\S]{0,600}?`）仍然有效。
    """
    lines = src.splitlines()
    buf = [list(line) for line in lines]
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src
    for tok in toks:
        if tok.type not in _BLANK_TOKENS:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            c0 = scol if row == srow else 0
            c1 = ecol if row == erow else len(lines[row - 1])
            for i in range(c0, min(c1, len(buf[row - 1]))):
                buf[row - 1][i] = " "
    return "\n".join("".join(b) for b in buf)


def find_renewkit() -> Path | None:
    """优先用本地源码（开发时与 renew-kit 同工作区），找不到就退回已安装的包。"""
    override = os.environ.get("RENEWKIT_PATH")
    if override:
        return Path(override)
    for cand in (ROOT.parents[1] / "renew-kit",
                 ROOT.parent / "renew-kit",
                 Path.home() / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").is_file():
            return cand
    return None


# ------------------------------------------------------------ 浏览器整层替身

class FakeLocator:
    """够用就行的 Playwright locator 替身。

    只实现脚本真正调到的成员。`filter()` 一律返回自身 —— 脚本里
    `.filter(...).filter(...).filter(...)` 连着三次，语义上就是「还是那个按钮」。
    """

    def __init__(self, page, n: int, *, sel: str = "", text: str = "") -> None:
        self.page = page
        self._n = n
        self.sel = sel
        self._text = text

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return self._n

    def is_visible(self) -> bool:
        return self._n > 0

    def inner_text(self) -> str:
        return self._text

    def filter(self, **_kw) -> "FakeLocator":
        return self

    def bounding_box(self):
        return None

    def click(self, timeout=None) -> None:
        # CGU 三个按钮（accept / later / close）点下去都算「弹窗已处理」。
        # cgu_stubborn=True 时例外 —— 模拟站点把遮罩留在那儿，逼出「强制移除节点」兜底。
        if "fd-cgu" in self.sel and not self.page.state.get("cgu_stubborn"):
            self.page.cgu_dismissed = True
            return
        if self.page.state.get("click_fails"):
            raise RuntimeError(
                "Element is not visible / intercepts pointer events (timeout=%s)"
                % timeout)

    def evaluate(self, _js):
        if "fd-cgu" in self.sel and not self.page.state.get("cgu_stubborn"):
            self.page.cgu_dismissed = True
            return None
        if self.page.state.get("js_click_fails"):
            raise RuntimeError("evaluate failed: detached from frame")
        return None


class FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple] = []
        self.clicks = 0

    def move(self, x, y, steps=None) -> None:
        self.moves.append((x, y, steps))

    def down(self) -> None:
        pass

    def up(self) -> None:
        self.clicks += 1


class FakeResponse:
    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self._body = body
        self.url = url

    def text(self) -> str:
        return self._body


class FakePage:
    """假面板。

    `state` 支持的键：
        initial_text / after_text   inner_text("body") 的前后两个版本
                                    （面板日期就直接写在这里面 —— 脚本是用
                                     `re.findall(r"\\d{2}/\\d{2}/\\d{4}")` 从 body
                                     文本里捞日期的，没有别的通道）
        renewable                   有「Renouveler gratuitement」按钮
        renew_text                  那个按钮的文案
        days                        倒计时天数；None = 页面上没有倒计时
        cgu                         有 CGU 全屏遮罩
        cgu_stubborn                遮罩点了也不消失（逼出「强制移除节点」兜底）
        cookie_banner               有底部 Cookie 提示条
        click_fails                 常规 click 抛异常（模拟被遮罩拦截）
        js_click_fails              JS 兜底 click 也抛异常
        turnstile_token             Turnstile token 是否已到手（默认 True）
        api                         列表 [(status, body)]，reload 时当作续期 API 回包
        goto_api                    goto 时也要回包（用来测 API 已经先回了一次）
    """

    def __init__(self, state: dict) -> None:
        self.state = state
        self.browser = None            # FakeBrowser 建好 page 后会回填，B17 用它查 closed
        self.handlers: dict[str, list] = {}
        self.reloaded = False
        self.cgu_dismissed = False
        self.screenshots: list[str] = []
        self.mouse = FakeMouse()

    # ---- 事件
    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def _fire(self, key: str) -> None:
        for status, body in (self.state.get(key) or []):
            for fn in self.handlers.get("response", []):
                fn(FakeResponse(status, body,
                                "https://fridaydev.fr/php/renew_free_service.php"))

    # ---- 导航
    def goto(self, url, **_kw) -> None:
        self.goto_url = url
        self._fire("goto_api")

    def reload(self, **_kw) -> None:
        self.reloaded = True
        self._fire("api")

    # ---- 内容
    def inner_text(self, _sel="body") -> str:
        if self.reloaded:
            return self.state.get("after_text", self.state.get("initial_text", ""))
        return self.state.get("initial_text", "")

    def evaluate(self, js):
        """按脚本内容回不同答案 —— 之前一律回 True，把两条完全不同的路混成一条了。"""
        s = str(js)
        # wait_turnstile_token 的第一句就是查 token。默认直接回 True（验证已通过），
        # 这样整条 renew 路径不用真跑 75 秒轮询；要测「拿不到 token」就设
        # state["turnstile_token"] = False。
        if "cf-turnstile-response" in s:
            return self.state.get("turnstile_token", True)
        # dismiss_cgu_modal 的兜底：`document.getElementById('fd-cgu-modal').remove()`
        if "fd-cgu-modal" in s:
            self.cgu_dismissed = True
            return None
        # 其余（[CAPTCHA] 状态读数、widget 容器坐标）只被 print 出去，回 None 就行。
        # widget 坐标回 None 意味着「没有可点的容器」，human_click 那条路自然跳过。
        return None

    def screenshot(self, path=None, **_kw) -> None:
        self.screenshots.append(str(path))

    def locator(self, sel) -> FakeLocator:
        s = str(sel)
        if "fd-cgu" in s:
            if not self.state.get("cgu") or self.cgu_dismissed:
                return FakeLocator(self, 0, sel=s)
            if s in ("#fd-cgu-modal", "#fd-cgu-accept"):
                return FakeLocator(self, 1, sel=s)
            if s == "#fd-cgu-later":
                return FakeLocator(self, 0, sel=s)      # 站点没有这个按钮
            return FakeLocator(self, 1 if "fd-cgu-close" in s else 0, sel=s)
        if "Accepter" in s:
            return FakeLocator(self, 1 if self.state.get("cookie_banner") else 0, sel=s)
        if s.startswith("button, a"):
            return FakeLocator(
                self, 1 if self.state.get("renewable") else 0, sel=s,
                text=self.state.get("renew_text", "Renouveler gratuitement"))
        if s.startswith("text=/") and "Renouvelable dans" in s:
            days = self.state.get("days")
            return FakeLocator(
                self, 0 if days is None else 1, sel=s,
                text=f"Renouvelable dans {days} jour")
        return FakeLocator(self, 0, sel=s)


class FakeBrowser:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.page = FakePage(state)
        self.page.browser = self          # 回填，B17 靠它查 close() 有没有被调到
        self.closed = False
        self.context_kwargs: dict = {}

    def new_context(self, **kw):
        self.context_kwargs = kw
        return self

    def add_cookies(self, cookies) -> None:
        self.cookies = cookies

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    """`sync_playwright()` 的替身 —— 同时是工厂和上下文管理器。"""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.browser = None
        self.launch_kwargs: dict = {}

    def __call__(self, *_a, **_k) -> "FakePlaywright":
        return self

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    @property
    def chromium(self) -> "FakePlaywright":
        return self

    def launch(self, **kw) -> FakeBrowser:
        self.launch_kwargs = kw
        self.browser = FakeBrowser(self.state)
        return self.browser


def install_stubs() -> list[str]:
    """塞最小替身：playwright / patchright。

    模块级 `from patchright.sync_api import sync_playwright` 必须能 import 成功，
    否则连模块都载不进来。真浏览器留给 CI。
    """
    stubbed: list[str] = []

    for name in ("playwright", "patchright"):
        if importlib.util.find_spec(name) is not None:
            continue
        pkg = types.ModuleType(name)
        api = types.ModuleType(f"{name}.sync_api")

        def _factory(*_a, **_k):
            raise RuntimeError("stub sync_playwright：harness 里请直接 patch mod.sync_playwright")

        api.sync_playwright = _factory
        pkg.sync_api = api
        sys.modules[name] = pkg
        sys.modules[f"{name}.sync_api"] = api
        stubbed.append(name)

    return stubbed


def load_script(renewkit: Path | None):
    if renewkit is not None:
        sys.path.insert(0, str(renewkit))
    else:
        try:
            import renewkit  # noqa: F401
        except ImportError:
            raise SystemExit(
                "找不到 renewkit：先 `pip install renewkit`，"
                "或用 RENEWKIT_PATH 指向 renew-kit 源码目录")
    spec = importlib.util.spec_from_file_location("fridaydev_renew", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fridaydev_renew"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- 场景矩阵驱动

class FakeTime:
    """假 time 模块（只换给被测模块，不动 harness 自己）。

    两个作用，缺一个 [B] 段就跑不完：

      · sleep() 是空操作 —— 脚本里 `time.sleep(5)` / `(4)` / `(2)` 加上
        「等 API 回包」那个 30 次轮询，真睡的话每个 renew 场景 40 秒起，
        整个 [B] 段要跑好几分钟（第一版就是这样，被外层 timeout 砍在半路）。
      · time() 每次调用往前跳 step 秒 —— `wait_turnstile_token` 是
        `while time.time() < deadline` 的轮询，光把 sleep 变空操作的话
        它会**空转烧 CPU** 直到真过 75 秒。跳时间让它在十几次迭代内自己到期。
        （token 到手的情况第一次迭代就 return，根本走不到这里。）
    """

    def __init__(self, step: float = 5.0) -> None:
        self._step = step
        self._t = _real_time.time()

    def time(self) -> float:
        self._t += self._step
        return self._t

    def monotonic(self) -> float:
        return self.time()

    def sleep(self, _secs) -> None:
        pass


@contextlib.contextmanager
def notify_spy(mod):
    """拦 renewkit.notify.send，记下每条消息（顺带把 DRY_RUN 闸门绕开）。"""
    sent: list[str] = []
    saved = mod.notify.send
    mod.notify.send = lambda text, **k: (sent.append(text), True)[1]
    try:
        yield sent
    finally:
        mod.notify.send = saved


def run_main(mod, *, cookie="a=1; b=2", state=None, dry_run=False,
             patch_notify=True, mode=None):
    """跑一次 main()，浏览器整层换替身。

    返回 (exit_code, stdout, sent_messages, page)。
    """
    state = dict(state or {})
    fake = FakePlaywright(state)

    saved = {
        "sync_playwright": mod.sync_playwright,
        "time": mod.time,
    }
    saved_env = {k: os.environ.get(k) for k in ("COOKIE", "FD_MODE", "DRY_RUN")}

    mod.sync_playwright = fake
    mod.time = FakeTime()
    if cookie is None:
        os.environ.pop("COOKIE", None)
    else:
        os.environ["COOKIE"] = cookie
    if mode is None:
        os.environ.pop("FD_MODE", None)
    else:
        os.environ["FD_MODE"] = mode
    # dry_run 参数以前声明了却没人用（B18 于是自己在外面设 os.environ，
    # 又被这里无条件 pop 掉 —— 演练那条断言从来没真跑过）。现在由这里统一管。
    if dry_run:
        os.environ["DRY_RUN"] = "1"
    else:
        os.environ.pop("DRY_RUN", None)

    ctx = notify_spy(mod) if patch_notify else contextlib.nullcontext([])
    buf = io.StringIO()
    try:
        with ctx as sent, contextlib.redirect_stdout(buf):
            code = mod.main()
    finally:
        mod.sync_playwright = saved["sync_playwright"]
        mod.time = saved["time"]
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    page = fake.browser.page if fake.browser else None
    return code, buf.getvalue(), sent, page


# ------------------------------------------------------------- [A] 纯逻辑

def section_a(c: Checks, mod) -> None:
    c.section("[A] 纯逻辑")

    from renewkit import Outcome, shorten as kit_shorten

    # --- 状态字 → Outcome
    want = {
        "ok": Outcome.RENEWED,
        "skip": Outcome.SKIPPED,
        "manual": Outcome.FAILED,
        "cookie": Outcome.FAILED,
        "fail": Outcome.FAILED,
        "error": Outcome.FAILED,
    }
    for status, outcome in want.items():
        c.eq(f"A1  {status} -> {outcome.value}", mod._outcome_of(status), outcome)
    c.eq("A1b 没见过的 status -> UNKNOWN", mod._outcome_of("who_knows"), Outcome.UNKNOWN)
    c.eq("A1c 空字符串 -> UNKNOWN", mod._outcome_of(""), Outcome.UNKNOWN)

    # --- 映射表的键就是脚本会产出的那 6 个状态字（多一个少一个都要发现）
    c.eq("A2  _STATUS_OUTCOME 的键集合",
         sorted(mod._STATUS_OUTCOME), sorted(want))

    # --- 退出码语义：只有 FAILED 才是 1
    for status in want:
        c.eq(f"A3  {status} 的退出码", mod._outcome_of(status).exit_code,
             NEW_EXIT_CODES[status])
    c.check("A3b  ok / skip 不标红",
            not mod._outcome_of("ok").is_error and not mod._outcome_of("skip").is_error)
    for status in ("manual", "cookie", "fail", "error"):
        c.check(f"A3c  {status} 必须标红（否则守门白装）",
                mod._outcome_of(status).is_error)

    # --- 迁移前后的退出码差异是**有意**的，逐项钉死
    for status, old in OLD_EXIT_CODES.items():
        new = NEW_EXIT_CODES[status]
        if old == new:
            c.eq(f"A4  {status} 退出码迁移前后一致", new, old)
        else:
            c.check(f"A4  {status} 退出码 {old} -> {new}（有意收敛）", new == 1)

    # --- _report
    c.eq("A5  单条 ok 的报告退出码",
         mod._report([("x", "ok", "續期成功")]).exit_code, 0)
    c.eq("A5b 单条 manual 的报告退出码",
         mod._report([("x", "manual", "要人手撳")]).exit_code, 1)
    c.eq("A5c 混装时取最坏（有 manual 就 1）",
         mod._report([("x", "ok", "好"), ("y", "manual", "差")]).exit_code, 1)
    c.eq("A5d 空报告的退出码", mod._report([]).exit_code, 0)
    c.eq("A5e 报告带 expire 时不炸",
         mod._report([("x", "ok", "好", "10-31")]).results[0].expire, "10-31")
    _r = mod._report([("x", "fail", "x" * 400)])
    c.check("A5f 报告 detail 被 shorten 压到 120 以内",
            len(_r.results[0].detail) <= 120, str(len(_r.results[0].detail)))

    # --- esc_html
    c.eq("A6  esc_html 转义 &", mod.esc_html("a&b"), "a&amp;b")
    c.eq("A6b esc_html 转义 <>", mod.esc_html("<b>x</b>"),
         "&lt;b&gt;x&lt;/b&gt;")
    c.eq("A6c esc_html 先转 & 再转 <（顺序不能反）",
         mod.esc_html("&lt;"), "&amp;lt;")
    c.eq("A6d esc_html 吃 None", mod.esc_html(None), "None")

    # --- fmt_date：面板 DD/MM/YYYY -> MM-DD
    c.eq("A7  fmt_date 正常", mod.fmt_date("31/10/2026"), "10-31")
    c.eq("A7b fmt_date 从长文本里捞", mod.fmt_date("Expire le 05/01/2027 !"), "01-05")
    c.eq("A7c fmt_date 解析不到回空串", mod.fmt_date("沒有日期"), "")
    c.eq("A7d fmt_date 吃 None", mod.fmt_date(None), "")

    # --- parse_cookies
    c.eq("A8  parse_cookies 拆两项",
         [x["name"] for x in mod.parse_cookies("a=1; b=2")], ["a", "b"])
    c.eq("A8b parse_cookies 的值带 = 也不切坏",
         mod.parse_cookies("t=abc=def")[0]["value"], "abc=def")
    c.eq("A8c parse_cookies 丢掉没有 = 的碎片",
         len(mod.parse_cookies("junk; a=1; ; b=2")), 2)
    c.eq("A8d parse_cookies 空串 -> 空表", mod.parse_cookies(""), [])
    c.eq("A8e cookie 的 domain 钉在 fridaydev.fr",
         mod.parse_cookies("a=1")[0]["domain"], "fridaydev.fr")
    c.eq("A8f cookie 的 path 是 /",
         mod.parse_cookies("a=1")[0]["path"], "/")

    # --- current_mode
    saved = os.environ.get("FD_MODE")
    try:
        os.environ.pop("FD_MODE", None)
        c.eq("A9  未设 FD_MODE -> watchdog", mod.current_mode(), "watchdog")
        os.environ["FD_MODE"] = "renew"
        c.eq("A9b FD_MODE=renew", mod.current_mode(), "renew")
        os.environ["FD_MODE"] = "  RENEW  "
        c.eq("A9c FD_MODE 大小写与空白都吃掉", mod.current_mode(), "renew")
        os.environ["FD_MODE"] = ""
        c.eq("A9d FD_MODE 是空串 -> 落回 watchdog（不是空串）",
             mod.current_mode(), "watchdog")
    finally:
        if saved is None:
            os.environ.pop("FD_MODE", None)
        else:
            os.environ["FD_MODE"] = saved

    # --- build_tg（方案 B 兩行制，文案逐字保留）
    c.eq("A10 ok 带到期日",
         mod.build_tg("ok", expire="10-31"),
         "✅ FridayDev · 成功續期至 10-31\nℹ️ 服務已自動展期")
    c.eq("A10b ok 不带到期日",
         mod.build_tg("ok"), "✅ FridayDev · 成功續期\nℹ️ 服務已自動展期")

    _skip = mod.build_tg("skip", "未可續（仲有 4 日）")
    c.eq("A11 skip 从 detail 里提天数", _skip.splitlines()[0],
         "🟢 FridayDev · 狀態良好（剩 4 日）")
    c.eq("A11b skip 的第二行默认说未到窗口", _skip.splitlines()[1],
         "ℹ️ 未到續期窗口")

    _skip_key = mod.build_tg("skip", "未可續（要人手撳）", key="面板 10-31", human=True)
    c.eq("A12 skip + key + human 的第一行", _skip_key.splitlines()[0],
         "🟢 FridayDev · 狀態良好")
    c.eq("A12b skip + key + human 的第二行",
         _skip_key.splitlines()[1], "ℹ️ 面板 10-31 到期 · 續期窗口即將開啟")

    c.eq("A13 key 里已经带「到期」就不再加",
         mod.build_tg("skip", "", key="面板 10-31 到期").splitlines()[1],
         "ℹ️ 面板 10-31 到期 · 未到續期窗口")

    c.eq("A13b skip 有 expire 没 key 时用它",
         mod.build_tg("skip", "", expire="10-31").splitlines()[1],
         "ℹ️ 10-31 到期 · 未到續期窗口")

    c.eq("A14 fail 的第一行", mod.build_tg("fail", "任意").splitlines()[0],
         "🚨 FridayDev · 續期未完成")
    c.eq("A14b fail 的第二行带「請登入面板手動處理」",
         mod.build_tg("fail", "API 未回 success").splitlines()[1],
         "⚠️ API 未回 success · 請登入面板手動處理")
    c.eq("A14c fail 的 detail 缺省是「執行失敗」",
         mod.build_tg("fail").splitlines()[1],
         "⚠️ 執行失敗 · 請登入面板手動處理")

    _long = mod.build_tg("fail", "x" * 200)
    _reason = _long.splitlines()[1].replace("⚠️ ", "").replace(" · 請登入面板手動處理", "")
    c.check("A14d fail 的 detail 截到 60 字以内", len(_reason) <= 60, str(len(_reason)))

    c.check("A15 动态内容里的 < 被转义（否则整条 TG 会被 400 丢掉）",
            "&lt;" in mod.build_tg("fail", "面板返回 <html>"), "")

    c.check("A16 shorten 就是 renewkit 的（没有本地副本）",
            mod.shorten is kit_shorten)

    # --- 状态字集合与 run() 的出口一致（防止加了出口忘了加映射）
    c.eq("A17 run() 会返回的状态字都在映射表里",
         {s for s in ("ok", "skip", "manual", "cookie", "fail") if s not in mod._STATUS_OUTCOME},
         set())


# --------------------------------------------------------- [B] 场景矩阵

def section_b(c: Checks, mod) -> None:
    c.section("[B] 场景矩阵（浏览器整层替身）")

    GOOD_BODY = "Mes services fridaydev 面板"
    # 面板日期是从 body 文本里正则捞的（extract_dates），没有单独的接口 ——
    # 所以要让脚本「看得见日期」，就得把日期写进 initial_text。
    PANEL_DATE = "31/10/2026"

    # B1 没有 COOKIE
    code, out, sent, _ = run_main(mod, cookie=None)
    c.eq("B1  缺 COOKIE -> exit 1", code, 1)
    c.check("B1b 缺 COOKIE 会提示设 secret", "COOKIE" in out, out[-200:])
    c.eq("B1c 缺 COOKIE 也发一条 TG", len(sent), 1)
    c.check("B1d 那条 TG 说「未設置 COOKIE」", "COOKIE" in sent[0], sent[0][:120])

    # B2 Cookie 失效（页面认不出登录态）
    code, out, sent, page = run_main(mod, state={"initial_text": "Accueil"})
    c.eq("B2  Cookie 失效 -> exit 1", code, 1)
    c.check("B2b 日志说 Cookie 已失效", "Cookie 已失效" in out, out[-300:])
    c.eq("B2c 失效时发一条 TG", len(sent), 1)
    c.check("B2d TG 提醒更新 Secrets", "Secrets" in sent[0], sent[0][:160])
    c.check("B2e 失效时也截了图", page is not None and page.screenshots, "")

    # B3 watchdog + 已经在续期窗口 → manual，exit 1（这就是 run #28 的设计内红）
    code, out, sent, _ = run_main(
        mod, state={"initial_text": GOOD_BODY + " " + PANEL_DATE,
                    "renewable": True, "days": None})
    c.eq("B3  watchdog 撞上续期窗口 -> exit 1（预期红）", code, 1)
    c.check("B3b 日志说「可續期窗口已開」且「唔會自動撳」",
            "可續期窗口已開" in out and "唔會自動撳" in out, out[-400:])
    c.eq("B3c 发一条 TG", len(sent), 1)
    # ⚠️ 已知文案缺陷，本次迁移**刻意原样保留**（要改就是另一次单独的改动）：
    #   窗口明明已经开了，第一行却说「狀態良好」、第二行说「即將開啟」；
    #   而且 run() 传进来的 detail「未可續（要人手撳）」被 build_tg 整个丢掉
    #   （它只从 detail 里正则抽「仲有 X 日」，抽不到就什么都不写）。
    #   看通知的人很容易当成「一切正常」直接划走 —— 真正起作用的提醒其实是
    #   job 标红本身。历史行为如此，这里只钉住它、不趁机改文案。
    c.eq("B3d 进窗口的完整文案（原样保留，含上述缺陷）", sent[0],
         "🟢 FridayDev · 狀態良好\nℹ️ 面板 10-31 到期 · 續期窗口即將開啟")
    c.check("B3e TG 里带面板日期（从 body 文本捞出来的）",
            "10-31" in sent[0], sent[0][:160])
    c.check("B3f 进入窗口才报红 —— 没有别的错误", code == 1 and len(sent) == 1)

    # B4 watchdog + 剩 4 天 → 提醒，但不标红
    code, out, sent, _ = run_main(
        mod, state={"initial_text": GOOD_BODY, "renewable": False, "days": 4})
    c.eq("B4  watchdog 剩 4 天 -> exit 0", code, 0)
    c.eq("B4b 剩 4 天发一条 TG", len(sent), 1)
    c.check("B4c TG 第一行带「（剩 4 日）」", "（剩 4 日）" in sent[0], sent[0][:160])
    c.check("B4d 4 天不算 urgent —— 第二行说「未到续期窗口」",
            "未到續期窗口" in sent[0], sent[0][:200])

    # B5 watchdog + 剩 2 天 → urgent（human=True）
    code, out, sent, _ = run_main(
        mod, state={"initial_text": GOOD_BODY, "renewable": False, "days": 2})
    c.eq("B5  剩 2 天 -> exit 0", code, 0)
    c.check("B5b 剩 2 天升级成「續期窗口即將開啟」",
            "續期窗口即將開啟" in sent[0], sent[0][:200])
    # 同 B3d 的缺陷：`run()` 传的是 detail="未可續（仲有 2 日，要人手撳）"，
    # build_tg 只抽走了「仲有 2 日」，后半句「要人手撳」不见了。原样保留。
    c.eq("B5c 剩 2 天的完整文案（原样保留）", sent[0],
         "🟢 FridayDev · 狀態良好（剩 2 日）\nℹ️ 續期窗口即將開啟")

    # B6 watchdog + 剩 10 天 → 完全静默（一条都不发）
    code, out, sent, _ = run_main(
        mod, state={"initial_text": GOOD_BODY, "renewable": False, "days": 10})
    c.eq("B6  剩 10 天 -> exit 0", code, 0)
    c.eq("B6b 剩 10 天**一条 TG 都不发**（约定静默）", len(sent), 0)
    c.check("B6c 静默这件事在日志里写明了", "靜默" in out, out[-300:])

    # B7 watchdog + 页面上没倒计时也没按钮 → skip（读数未知，不标红）
    code, out, sent, _ = run_main(
        mod, state={"initial_text": GOOD_BODY, "renewable": False, "days": None})
    c.eq("B7  watchdog 无按钮无倒计时 -> exit 0", code, 0)
    c.eq("B7b 这种情况也静默", len(sent), 0)

    # B8 CGU 全屏遮罩被点掉
    code, out, sent, page = run_main(
        mod, state={"initial_text": GOOD_BODY, "cgu": True,
                    "renewable": False, "days": 7})
    c.check("B8  检测到 CGU 弹窗", "检测到 CGU 弹窗" in out, out[:400])
    c.check("B8b 点掉了 #fd-cgu-accept", "已点击 CGU 弹窗按钮: #fd-cgu-accept" in out,
            out[:600])
    c.check("B8c 弹窗消失后不再强制移除节点",
            "已强制移除遮罩节点" not in out, out[:600])
    c.check("B8c2 日志确认弹窗已消失", "CGU 弹窗已消失" in out, out[:600])

    # B8d 遮罩点了也不消失（站点把 close 按钮去掉了 / 点击被吞）→ 走强制移除节点兜底。
    #     这条兜底是 2026-09-20 加进来的：遮罩不关就会 intercept pointer events，
    #     续期按钮永远点不到。迁移时最容易被顺手删掉的就是它。
    code, out, sent, page = run_main(
        mod, state={"initial_text": GOOD_BODY, "cgu": True, "cgu_stubborn": True,
                    "renewable": False, "days": 7})
    c.check("B8d 顽固遮罩走强制移除节点兜底",
            "已强制移除遮罩节点" in out, out[:800])
    c.check("B8e 兜底之后照常出结果（没被遮罩卡死）", code == 0, str(code))

    # B9 底部 Cookie 提示条
    code, out, _, _ = run_main(
        mod, state={"initial_text": GOOD_BODY, "cookie_banner": True,
                    "renewable": False, "days": 7})
    c.check("B9  关掉了底部 Cookie 提示条", "已关闭底部 Cookie 提示条" in out, out[:500])

    # B10 renew 模式：点不到按钮 -> fail，exit 1
    code, out, sent, page = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": True,
               "click_fails": True, "js_click_fails": True})
    c.eq("B10 renew 模式点不到按钮 -> exit 1", code, 1)
    c.check("B10b 常规点击失败后有 JS 兜底提示",
            "改用 JS 点击兜底" in out, out[-800:])
    c.check("B10c 日志说续期按钮点击失败", "续期按钮点击失败" in out, out[-500:])
    c.eq("B10d 发一条 TG", len(sent), 1)
    c.check("B10e TG 说按钮点唔到", "按鈕點唔到" in sent[0], sent[0][:200])

    # B11 renew 模式：常规 click 失败但 JS 兜底成功 -> 继续往下走
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": True,
               "click_fails": True, "js_click_fails": False,
               "after_text": GOOD_BODY + " Renouvelable dans 30 jour"})
    c.eq("B11 JS 兜底成功 -> exit 0", code, 0)
    c.check("B11b 走了 JS 兜底那条路", "改用 JS 点击兜底" in out, "")
    c.check("B11c 报成续期成功", "续期成功" in out, out[-500:])
    c.check("B11d TG 是 ok 文案", "成功續期" in (sent[0] if sent else ""), sent[:1])

    # B12 renew 模式：API 没回 success -> fail，exit 1
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": True,
               "after_text": GOOD_BODY + " Renouveler gratuitement",
               "api": [(428, '{"error":"captcha_required"}')]})
    c.eq("B12 API 未回 success -> exit 1", code, 1)
    c.check("B12b 日志老实报「未成功」而不是假成功",
            "续期未成功" in out, out[-600:])
    c.check("B12c TG 说明大概率是 Turnstile",
            "Turnstile" in (sent[0] if sent else ""), sent[:1])
    c.check("B12d 没有把 428 报成成功",
            "成功續期" not in (sent[0] if sent else ""), sent[:1])

    # B12e renew 模式：Turnstile token 始终不到手 —— 这才是 GHA runner 上的常态
    #      （run 35496183082 那批旧脚本就是在这里骗自己说「续期成功」的）。
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": True,
               "turnstile_token": False,
               "after_text": GOOD_BODY + " Renouveler gratuitement"})
    c.eq("B12e Turnstile 拿不到 token -> exit 1", code, 1)
    c.check("B12f 日志说等 Turnstile 逾时", "逾時" in out, out[-700:])
    c.check("B12g 没把「验证没过」报成成功",
            "成功續期" not in (sent[0] if sent else ""), sent[:1])

    # B13 renew 模式：API 回 200 success -> ok，exit 0
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": True,
               "after_text": GOOD_BODY + " Renouvelable dans 30 jour",
               "api": [(200, '{"success":true}')]})
    c.eq("B13 API 回 200 success -> exit 0", code, 0)
    c.check("B13b TG 是 ok 文案", "成功續期" in (sent[0] if sent else ""), sent[:1])

    # B14 renew 模式 + 页面只剩倒计时（按钮不在）
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": False, "days": 1})
    c.eq("B14 renew + 只剩 1 天倒计时 -> exit 0", code, 0)
    c.eq("B14b 剩 1 天（≤2）会提醒", len(sent), 1)
    # 又是同一个缺陷（见 B3d）：`run()` 传的是「未可續（仲有 1 日，下次自動續）」，
    # build_tg 只留下「仲有 1 日」，「下次自動續」这句安抚的话没进通知。
    c.eq("B14c 剩 1 天的完整文案（原样保留）", sent[0],
         "🟢 FridayDev · 狀態良好（剩 1 日）\nℹ️ 未到續期窗口")

    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": False, "days": 5})
    c.eq("B14d renew + 剩 5 天（>2）-> 静默", len(sent), 0)

    # B15 renew + 没有按钮也没有倒计时 -> skip（可能已续期）
    code, out, sent, _ = run_main(
        mod, mode="renew",
        state={"initial_text": GOOD_BODY, "renewable": False, "days": None})
    c.eq("B15 renew + 无按钮无倒计时 -> exit 0", code, 0)
    c.check("B15b 日志说「可能已成功续期」",
            "可能已成功续期" in out, out[-300:])

    # B16 未捕获异常 -> error，exit 1，且异常信息被转义后发出
    code, out, sent, _ = run_main(mod, state={"initial_text": GOOD_BODY},)
    c.eq("B16 正常路径先确认基线 exit 0", code, 0)

    saved = mod.run
    mod.run = lambda _c: (_ for _ in ()).throw(RuntimeError("boom <&>"))
    try:
        code, out, sent, _ = run_main(mod)
        c.eq("B16b 脚本异常 -> exit 1", code, 1)
        c.eq("B16c 异常时发一条 TG", len(sent), 1)
        c.check("B16d 异常信息里的 < 被转义（否则整条通知丢失）",
                "&lt;" in sent[0] and "<&>" not in sent[0], sent[0][:200])
        c.check("B16e 异常类型名出现在通知里", "RuntimeError" in sent[0], sent[0][:200])
    finally:
        mod.run = saved

    # B17 浏览器关闭不漏（每条出口都得关）
    #     原来是 `page is not None` —— 那是句废话（拿到 page 就说明开过浏览器），
    #     真正要验的是 close() 被调到没有。FakeBrowser 建 page 时会把自己回填进
    #     page.browser，所以这里能直接查 closed。
    for label, kw in (
        ("cookie 失效（浏览器已开）", dict(state={"initial_text": "Accueil"})),
        ("watchdog 静默", dict(state={"initial_text": GOOD_BODY, "days": 9})),
        ("watchdog 进窗口", dict(state={"initial_text": GOOD_BODY, "renewable": True})),
        ("renew 点唔到按钮", dict(mode="renew",
                                  state={"initial_text": GOOD_BODY, "renewable": True,
                                         "click_fails": True, "js_click_fails": True})),
    ):
        code, out, sent, page = run_main(mod, **kw)
        if page is None:
            c.check(f"B17 {label}：没开浏览器就不用关", True)
        else:
            c.check(f"B17 {label}：浏览器已关闭（browser.close() 走到了）",
                    page.browser is not None and page.browser.closed)

    # B18 DRY_RUN 下 notify.send 只打印不发，且正文只出现一次
    #     （1ifecycle 那边暴露过的坑：脚本自己 print + kit 回显 = 两遍）
    #     这里同时钉住「闸门在 token 检查**之前**」：config() 故意返回空，
    #     如果闸门排在后面就会走「未配置」分支把消息吞掉（got=0）。
    saved_cfg = mod.notify.config
    mod.notify.config = lambda: ("", "")
    try:
        code, out, _, _ = run_main(
            mod, patch_notify=False, dry_run=True,
            state={"initial_text": GOOD_BODY, "renewable": True})
        c.eq("B18 DRY_RUN 下 exit 码不变（演练不改结论）", code, 1)
        c.check("B18b DRY_RUN 下正文只出现一次",
                out.count("🟢 FridayDev · 狀態良好") == 1, out[-500:])
        c.check("B18c DRY_RUN 下带演练抬头", "DRY_RUN 演练" in out, out[-400:])
        c.check("B18d 没 token 也照样演练（闸门排在 token 检查前面）",
                "未配置" in out, out[-400:])
    finally:
        mod.notify.config = saved_cfg


# ------------------------------------------------------ [C] 静态与接线

def section_c(c: Checks, src: str, code: str) -> None:
    c.section("[C] 静态与接线")

    # --- renew-kit 接线
    for name, pat in (
        ("C1  import Outcome/RenewReport/TargetResult/shorten",
         r"from renewkit import [^\n]*Outcome[^\n]*RenewReport[^\n]*TargetResult[^\n]*shorten"),
        ("C1b import env / notify", r"from renewkit import env, notify"),
    ):
        c.check(name, re.search(pat, code) is not None)

    c.check("C1c 用到 Outcome.RENEWED", "Outcome.RENEWED" in code)
    c.check("C1d 用到 Outcome.SKIPPED", "Outcome.SKIPPED" in code)
    c.check("C1e 用到 Outcome.FAILED", "Outcome.FAILED" in code)
    c.check("C1f 用到 Outcome.UNKNOWN（没见过的状态字兜底）",
            "Outcome.UNKNOWN" in code)

    # --- 迁移掉的东西不得回归
    for name, pat in (
        ("C2  不再自己定义 now_local()", r"^def now_local\("),
        ("C2b 不再自己定义 _clip()", r"^def _clip\("),
        ("C2c 不再自己定义 _esc_html()", r"^def _esc_html\("),
        ("C2d 不再自己读 TG_BOT_TOKEN", r"TG_BOT_TOKEN\s*=\s*"),
        ("C2e 不再自己读 TG_CHAT_ID", r"TG_CHAT_ID\s*=\s*"),
        ("C2f 不再自己拼 sendMessage 的 URL", r"api\.telegram\.org/bot"),
    ):
        c.check(name, re.search(pat, code, re.M) is None,
                (re.search(pat, code, re.M) or [""])[0] if re.search(pat, code, re.M) else "")

    # --- 已删掉的 import
    for name, pat in (
        ("C3  不再 import json", r"^import json"),
        ("C3b 不再 import os", r"^import os"),
        ("C3c 不再 import urllib.request", r"^import urllib\.request"),
        ("C3d 不再 import urllib.parse", r"^import urllib\.parse"),
    ):
        c.check(name, re.search(pat, code, re.M) is None)

    # --- sys.exit 只允许一处，且必须是 sys.exit(main())
    exits = re.findall(r"sys\.exit\(([^\n]*)\)", code)
    c.eq("C4  sys.exit 只出现一次", len(exits), 1)
    c.eq("C4b 那一处是 sys.exit(main())", exits, ["main()"])

    # --- 浏览器关闭在 finally 里
    c.check("C5  browser.close() 在 finally 块里",
            re.search(r"finally:\s*\n(?:[^\n]*\n){0,6}?[^\n]*browser\.close\(\)", code)
            is not None)

    # --- notify 走 kit
    c.check("C6  tg_send 走 notify.send", "notify.send(text" in code)
    # parse_mode 的值是字符串字面量 —— CODE 视图里被挖空了，所以两头都要看：
    # CODE 证明「调用点确实传了这个参数」，SRC 证明「传的值是 HTML」。
    c.check("C6b tg_send 带 parse_mode=HTML",
            re.search(r"notify\.send\([^\n]*parse_mode=", code) is not None
            and 'parse_mode="HTML"' in src)
    c.check("C6c tg_send 用 env/notify 而不是裸 os.environ",
            "os.environ" not in code)

    # --- 新函数在位
    c.check("C7  parse_cookies 是独立函数",
            re.search(r"^def parse_cookies\(", code, re.M) is not None)
    c.check("C7b current_mode 是独立函数",
            re.search(r"^def current_mode\(", code, re.M) is not None)
    c.check("C7c _STATUS_OUTCOME 映射表在",
            re.search(r"^_STATUS_OUTCOME\s*=", code, re.M) is not None)
    c.check("C7d _outcome_of 查表函数在",
            re.search(r"^def _outcome_of\(", code, re.M) is not None)
    c.check("C7e _report 在",
            re.search(r"^def _report\(", code, re.M) is not None)
    c.check("C7f main() 返回 int 并交给 sys.exit",
            re.search(r"^def main\(\) -> int:", code, re.M) is not None)

    # --- 静默约定必须留着（迁移最容易顺手改坏的地方）
    c.check("C8  剩 >5 天的静默分支还在（watchdog）",
            re.search(r"days\.isdigit\(\) and int\(days\) <= 5", code) is not None)
    c.check("C8b 剩 >2 天的静默分支还在（renew 倒计时）",
            re.search(r"days\.isdigit\(\) and int\(days\) <= 2", code) is not None)
    # 'renew' 是字符串字面量，CODE 视图里挖空了 —— 同样两头看。
    # 注意 CODE 里连那个引号都没了（`mode != "renew"` -> `mode !=       `），
    # 所以 CODE 侧只能匹配到 `mode !=` 为止。
    c.check('C8c watchdog 判据用的是 mode != "renew"',
            re.search(r"mode\s*!=", code) is not None
            and 'mode != "renew"' in src)

    # --- 业务资产不得被顺手删掉
    for name, needle in (
        ("C9  Turnstile 等待逻辑还在", "def wait_turnstile_token("),
        ("C9b CGU 遮罩处理还在", "def dismiss_cgu_modal("),
        ("C9c 真人点击序列还在", "def human_click("),
        ("C9d JS 点击兜底还在", "def safe_click("),
        ("C9e 日期提取还在", "def extract_dates("),
        ("C9f 方案 B 文案还在", "def build_tg("),
    ):
        c.check(name, needle in code)

    # --- patchright 首选引擎
    c.check("C10 首选 patchright 引擎", "patchright" in code)
    c.check("C10b 退回 playwright 兜底", "playwright" in code)

    # --- workflow
    wf = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""
    if not WORKFLOW.is_file():
        c.check("C11 renew.yml 存在", False)
    else:
        c.check("C11 renew.yml 存在", True)
        c.check("C11a 用 renew-kit composite action",
                RENEWKIT_REF_RE.search(wf) is not None, wf[:200])
        c.check("C11b renewkit-ref 与 uses 的 tag 一致",
                re.search(r"renewkit-ref:\s*v0\.5\.3", wf) is not None
                and "@v0.5.3" in wf)
        c.check("C11c 指定了脚本 renew.py",
                re.search(r"script:\s*renew\.py", wf) is not None)
        c.check("C11d python-version 仍是 3.10（迁移前就是）",
                re.search(r'python-version:\s*["\']3\.10', wf) is not None)
        c.check("C11e 装 playwright + patchright",
                re.search(r"pip-packages:.*playwright.*patchright", wf) is not None)
        c.check("C11f setup-command 里装 chromium",
                re.search(r"playwright install chromium", wf) is not None)
        c.check("C11g setup-command 里装系统依赖",
                re.search(r"playwright install-deps chromium", wf) is not None)
        c.check("C11h setup-continue-on-error 设 false（安装失败就是硬失败）",
                re.search(r'setup-continue-on-error:\s*["\']?false', wf) is not None)
        c.check("C11i 关掉 action 的兜底 TG（脚本自己已发，避免双推）",
                re.search(r'notify-on-failure:\s*["\']?false', wf) is not None)
        c.check("C11j 传 COOKIE", "COOKIE: ${{ secrets.COOKIE }}" in wf)
        c.check("C11k 传 TG_BOT_TOKEN", "TG_BOT_TOKEN:" in wf)
        c.check("C11l 传 TG_CHAT_ID", "TG_CHAT_ID:" in wf)
        c.check("C11m FD_MODE 默认 watchdog",
                re.search(r"FD_MODE:\s*\$\{\{\s*inputs\.mode \|\| 'watchdog'\s*\}\}", wf)
                is not None)
        c.check("C11n 有 dry_run 输入接到 DRY_RUN",
                "DRY_RUN: ${{ inputs.dry_run" in wf)
        c.check("C11o 保留了每日排程（cron 5 7 * * *）",
                re.search(r"cron:\s*'5 7 \* \* \*'", wf) is not None)
        c.check("C11p 保留 workflow_dispatch", "workflow_dispatch:" in wf)
        c.check("C11q 保留 mode 二选一", "watchdog" in wf and "renew" in wf
                and re.search(r"type:\s*choice", wf) is not None)
        c.check("C11r 截图产物是 always() 上传（迁移前就是这样）",
                re.search(r"if:\s*always\(\)", wf) is not None)
        c.check("C11s 产物路径是 result.png",
                re.search(r"path:\s*result\.png", wf) is not None)
        c.check("C11t 删掉了没人用的 actions: write 授权",
                re.search(r"^\s*actions:\s*write", wf, re.M) is None)
        c.check("C11u 保留了 Turnstile 过不去的说明",
                "Turnstile" in wf)

    # --- README
    rd = README.read_text(encoding="utf-8") if README.is_file() else ""
    if not README.is_file():
        c.check("C12 README.md 存在", False)
    else:
        c.check("C12 README.md 存在", True)
        c.check("C12a README 提到 renew-kit", "renew-kit" in rd)
        c.check("C12b README 写明 watchdog 是默认", "watchdog" in rd)
        c.check("C12c README 不残留旧 tag",
                not re.search(r"renew-kit@v0\.(?!5\.3)", rd))
        c.check("C12d README 记下 Turnstile 过不去的原因", "Turnstile" in rd)
        c.check("C12e README 记下静默约定（>5 天不发）", "5 天" in rd)
        c.check("C12f README 记下退出码收敛 0/1/2/3 -> 0/1", "0/1/2/3" in rd)
        c.check("C12g README 记下 manual -> FAILED 的理由", "永久绿灯" in rd)
        c.check("C12h README 提到验收 harness", "verify_fridaydev.py" in rd)
        c.check("C12i README 提到 dry_run", "dry_run" in rd)

    # --- CRLF 防线（GitHub Actions 的 run 块被 CRLF 污染过）
    for path in (SCRIPT, WORKFLOW, README):
        if path.is_file():
            raw = path.read_bytes()
            c.check(f"C13 {path.name} 不含 CR", raw.count(b"\r") == 0,
                    f"CR x{raw.count(b'\r')}")
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".yml", ".yaml", ".md"):
            if path.name == "verify_fridaydev.py":
                continue
            raw = path.read_bytes()
            if b"\r" in raw:
                c.check(f"C13b {path.relative_to(ROOT)} 不含 CR", False)
                break
    else:
        c.check("C13b 仓库内无任何 CRLF 的脚本/配置", True)


# ------------------------------------------------------------ [D] 真子进程

BOOT = r'''
import sys, types

# 先把 playwright / patchright 换成替身，否则模块级 import 就炸
for _n in ("playwright", "patchright"):
    _pkg = types.ModuleType(_n)
    _api = types.ModuleType(_n + ".sync_api")
    _api.sync_playwright = lambda *a, **k: None
    _pkg.sync_api = _api
    sys.modules[_n] = _pkg
    sys.modules[_n + ".sync_api"] = _api

import renew as w

# 带 key 一起打：下面是把 stdout 拆成 `key value` 的 dict，光打一个
# "FridayDev" 没有空格，那行会被整个丢掉（D3 第一版就是这么红的）。
print("SERVICE", w.SERVICE)
print("OUTCOMES", ",".join(sorted(o.value for o in w._STATUS_OUTCOME.values())))
print("EXIT_OK", w._outcome_of("ok").exit_code)
print("EXIT_SKIP", w._outcome_of("skip").exit_code)
print("EXIT_MANUAL", w._outcome_of("manual").exit_code)
print("EXIT_COOKIE", w._outcome_of("cookie").exit_code)
print("FMT", w.fmt_date("31/10/2026"))
print("ESC", w.esc_html("<a>"))
print("TG", repr(w.build_tg("ok", expire="10-31")))
print("ENGINE", w.ENGINE)
import os
os.environ.pop("COOKIE", None)
print("MAIN_NOCOOKIE", w.main())
'''


def section_d(c: Checks) -> None:
    c.section("[D] 真子进程")

    env = dict(os.environ)
    # scripts/ 不用加 —— 本仓库脚本就在根目录
    parts = [str(ROOT)]
    rk = find_renewkit()
    if rk is not None:
        parts.append(str(rk))
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(parts + ([prev] if prev else []))
    env.pop("COOKIE", None)
    env.pop("FD_MODE", None)
    env.pop("DRY_RUN", None)

    r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)],
                       capture_output=True, text=True, cwd=str(ROOT))
    c.check("D1  py_compile 通过", r.returncode == 0, r.stderr[-400:])

    r = subprocess.run([sys.executable, "-c", BOOT], capture_output=True,
                       text=True, cwd=str(ROOT), env=env, timeout=120)
    c.check("D2  真子进程能 import 模块并跑 main()", r.returncode == 0,
            r.stderr[-600:])
    out = r.stdout
    lines = dict(l.split(" ", 1) for l in out.splitlines() if " " in l)

    c.eq("D3  SERVICE 名", lines.get("SERVICE"), "FridayDev")
    c.eq("D4  映射表覆盖 6 种 Outcome",
         len(lines.get("OUTCOMES", "").split(",")), 6)
    c.eq("D5  ok -> exit 0", lines.get("EXIT_OK"), "0")
    c.eq("D6  skip -> exit 0", lines.get("EXIT_SKIP"), "0")
    c.eq("D7  manual -> exit 1", lines.get("EXIT_MANUAL"), "1")
    c.eq("D8  cookie -> exit 1", lines.get("EXIT_COOKIE"), "1")
    c.eq("D9  fmt_date 走通", lines.get("FMT"), "10-31")
    c.eq("D10 esc_html 走通", lines.get("ESC"), "&lt;a&gt;")
    c.eq("D11 build_tg 走通", lines.get("TG"),
         repr("✅ FridayDev · 成功續期至 10-31\nℹ️ 服務已自動展期"))
    c.check("D12 ENGINE 是 patchright 或 playwright 之一",
            lines.get("ENGINE") in ("patchright", "playwright"), out[:300])
    c.eq("D13 缺 COOKIE 时 main() 返回 1", lines.get("MAIN_NOCOOKIE"), "1")


# ------------------------------------------------------------------- main

def main() -> int:
    c = Checks()
    stubbed = install_stubs()
    renewkit = find_renewkit()
    if renewkit is None:
        print("⚠️ 没找到 renew-kit 源码目录，改用已安装的包")

    src = SCRIPT.read_text(encoding="utf-8")
    code = code_only(src)

    mod = load_script(renewkit)

    print(f"（替身模块：{', '.join(stubbed) or '无'}；"
          f"renewkit 来自 {renewkit or '已安装包'}）")

    section_a(c, mod)
    section_b(c, mod)
    section_c(c, src, code)
    section_d(c)
    return c.report()


if __name__ == "__main__":
    sys.exit(main())
