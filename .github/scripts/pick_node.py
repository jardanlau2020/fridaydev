# -*- coding: utf-8 -*-
"""从 Clash YAML 订阅里挑出 sing-box 能用的节点，按 TCP 延迟排序后逐行输出 URI。

⚠️ 延迟快 ≠ 出口干净。本脚本只做 TCP 连通性排序，**不能**当作「可用节点」的
最终判据：2026-11-21 实测，按延迟排出的前 19 条出口全是 Cloudflare WARP
（104.28.x / AS13335 / proxy=true），Turnstile 一条票都不给。
调用方必须对每个候选再查一次出口身份（ip-api 的 hosting / proxy 字段），
hosting=true 或 proxy=true 的直接跳过，见 renew.yml 的「逐节点验出口纯净度」。

用法: python3 pick_node.py <clash_yaml> [最大输出条数]
只输出 vless / trojan（openvpn 等 sing-box 不支持的跳过）。
不联网、不打印凭据到 stderr，URI 逐行写 stdout 供 shell 循环消费。
"""
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote


def strip_val(v):
    v = v.strip()
    if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def load_proxies(path):
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^proxies:\s*$", l):
            start = i
            break
    if start is None:
        return []
    out, cur, base = [], None, None
    for l in lines[start + 1:]:
        if not l.strip() or l.strip().startswith("#"):
            continue
        if re.match(r"^[A-Za-z_-]+:", l):
            break
        m = re.match(r"^(\s*)-\s+(\w[\w-]*):\s*(.*)$", l)
        if m:
            if cur:
                out.append(cur)
            cur, base = {}, len(m.group(1))
            cur[m.group(2)] = strip_val(m.group(3))
            continue
        m2 = re.match(r"^(\s*)([\w-]+):\s*(.*)$", l)
        if m2 and cur is not None:
            depth = len(m2.group(1)) - (base or 0)
            k, v = m2.group(2), strip_val(m2.group(3))
            if depth <= 2:
                cur[k] = v
            else:
                cur.setdefault("_nested", {})[k] = v
    if cur:
        out.append(cur)
    return out


def build_uri(p):
    t = (p.get("type") or "").lower()
    server, port = p.get("server") or "", p.get("port") or ""
    if not server or not port:
        return None
    name = quote(p.get("name") or "node", safe="")
    nested = p.get("_nested") or {}
    net = p.get("network") or "tcp"
    path = nested.get("path") or "/" if net == "ws" else ""
    host = (nested.get("Host") or nested.get("host") or server) if net == "ws" else ""

    if t == "vless":
        sec = "tls" if str(p.get("tls")).lower() == "true" else "none"
        if nested.get("public-key") or p.get("reality-opts"):
            sec = "reality"
        sni = p.get("servername") or p.get("sni") or server
        q = ["encryption=none", f"security={sec}", f"type={net}",
             f"sni={quote(sni, safe='')}",
             f"fp={p.get('client-fingerprint') or 'chrome'}"]
        if path:
            q.append(f"path={quote(path, safe='')}")
        if host:
            q.append(f"host={quote(host, safe='')}")
        if p.get("flow"):
            q.append(f"flow={p['flow']}")
        if sec == "reality":
            pbk = nested.get("public-key") or ""
            sid = nested.get("short-id") or ""
            if pbk:
                q.append(f"pbk={pbk}")
            if sid:
                q.append(f"sid={sid}")
        return f"vless://{p.get('uuid','')}@{server}:{port}?{'&'.join(q)}#{name}"

    if t == "trojan":
        sni = p.get("sni") or p.get("servername") or server
        q = ["security=tls", f"type={net}", f"sni={quote(sni, safe='')}"]
        if path:
            q.append(f"path={quote(path, safe='')}")
        if host:
            q.append(f"host={quote(host, safe='')}")
        return f"trojan://{p.get('password','')}@{server}:{port}?{'&'.join(q)}#{name}"

    return None


def tcp_ok(server, port, timeout=4.0):
    import time
    try:
        port = int(port)
    except Exception:
        return False, 99.0
    t0 = time.time()
    try:
        with socket.create_connection((server, port), timeout=timeout):
            return True, round(time.time() - t0, 3)
    except Exception:
        return False, 99.0


def main():
    path = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    cands = []
    for p in load_proxies(path):
        u = build_uri(p)
        if u:
            cands.append({"uri": u, "server": p.get("server"), "port": p.get("port")})
    if not cands:
        print("pick_node: 没有可用节点", file=sys.stderr)
        return 1
    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = [ex.submit(tcp_ok, c["server"], c["port"]) for c in cands]
        for c, f in zip(cands, futs):
            ok, lat = f.result()
            c["ok"], c["lat"] = ok, lat
    alive = sorted([c for c in cands if c["ok"]], key=lambda x: x["lat"])[:limit]
    print(f"pick_node: 候选 {len(cands)}，TCP 可达 {len(alive)}", file=sys.stderr)
    for c in alive:
        print(c["uri"])
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
