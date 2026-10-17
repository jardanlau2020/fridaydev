#!/usr/bin/env bash
# setup_residential.sh — 由 cfnew 家宽订阅（SUB_URL）起 mihomo，逐條測試揀一條通嘅住宅出口。
#
# 背景（2026-10-17 run 39018711788）：舊 renew.yml 用 setup_proxy.sh + 單一 NODE_LINK
# （42.200.173.5，香港 HKT），節點死咗 → i/o timeout → 回退直連機房 IP → CF 腳本載唔到
# （turnstile_api=undefined、iframes=[]）→ 續期失敗。而同一時間探針 workflow 用訂閱
# 逐條揀，揀到 🏠 KR-家宽-01 就正常（CF 腳本載到、428+sitekey+widget 齊）。
# 所以呢度照抄探針嘅揀節點邏輯，令續期都有得用住宅出口。
#
# 成功：寫 PROXY_SERVER=socks5://127.0.0.1:1080 同 PROXY_NODE=<節點名> 落 GITHUB_ENV
# 失敗：exit 1（caller 用 `|| echo '[WARN] …'` 兜，唔會殺 job）
set -uo pipefail
set +e   # GitHub 用 bash -e 跑 run；明確關掉，唔好俾單條 curl 失敗殺死

[ -z "${SUB_URL:-}" ] && { echo "[res] 冇 SUB_URL，跳過（直連）"; exit 1; }

MIHOMO_VER=v1.19.26
if [ ! -x /tmp/mihomo ]; then
  curl -sL --max-time 120 -o /tmp/mihomo.gz \
    "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VER}/mihomo-linux-amd64-${MIHOMO_VER}.gz" \
    && gzip -dc /tmp/mihomo.gz > /tmp/mihomo && chmod +x /tmp/mihomo \
    || { echo "[res] ✗ mihomo 下載失敗"; exit 1; }
fi

SUB_OK=0
for _try in 1 2 3; do
  if curl -s --max-time 60 -H "user-agent: v2rayNG/1.8.5" "$SUB_URL" -o /tmp/sub.yaml \
     && [ -s /tmp/sub.yaml ] \
     && grep -qE '^(proxies|proxy-groups|mixed-port|mode):' /tmp/sub.yaml; then
    SUB_OK=1; break
  fi
  echo "[res] ⚠️ 訂閱下載第 ${_try}/3 次失敗，5 秒後重試"
  sleep 5
done
[ "$SUB_OK" = "1" ] || { echo "[res] ✗ 訂閱下載失敗（試 3 次）"; exit 1; }

sed -i 's/^mixed-port: .*/mixed-port: 1080/; s/^external-controller: .*/external-controller: 127.0.0.1:9091/; s/^mode: .*/mode: global/' /tmp/sub.yaml
mkdir -p /tmp/mihomo_data
pkill -f '/tmp/mihomo -f' 2>/dev/null || true
nohup /tmp/mihomo -f /tmp/sub.yaml -d /tmp/mihomo_data > /tmp/mihomo.log 2>&1 &
sleep 18
curl -s --max-time 10 http://127.0.0.1:9091/version >/dev/null || { echo "[res] ✗ mihomo 起唔到"; tail -3 /tmp/mihomo.log; exit 1; }
echo "[res] ✅ mihomo 起動"

python3 - <<'PY' > /tmp/nodes.txt
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:9091/proxies", timeout=20))["proxies"]
home = [k for k in d if "家宽" in k]
PRIO = ("KR", "TH", "VN", "TW", "HK", "SG")
def rank(n):
    for i, c in enumerate(PRIO):
        if c + "-家宽" in n: return i
    return len(PRIO)
for k in sorted(home, key=rank): print(k)
PY

N=0
while IFS= read -r NODE; do
  [ -z "$NODE" ] && continue
  N=$((N+1)); [ "$N" -gt 25 ] && break
  python3 - "$NODE" <<'PY' 2>/dev/null || continue
import json, urllib.request, sys
req = urllib.request.Request("http://127.0.0.1:9091/proxies/GLOBAL", method="PUT",
      data=json.dumps({"name": sys.argv[1]}).encode(),
      headers={"content-type": "application/json"})
urllib.request.urlopen(req, timeout=15)
PY
  sleep 4
  A=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 --socks5-hostname 127.0.0.1:1080 https://fridaydev.fr/ 2>/dev/null || true)
  B=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 --socks5-hostname 127.0.0.1:1080 https://challenges.cloudflare.com/turnstile/v0/api.js 2>/dev/null || true)
  case "$A:$B" in
    200:200|200:301|200:302|301:200|301:301|301:302|302:200|302:301|302:302)
      echo "[res] ✅ 揀到節點：${NODE}（fridaydev=${A}, turnstile_api.js=${B}）"
      { echo "PROXY_SERVER=socks5://127.0.0.1:1080"; echo "PROXY_NODE=${NODE}"; } >> "${GITHUB_ENV:-/dev/null}"
      exit 0 ;;
    *) echo "[res] ✗ ${NODE} → fridaydev=${A:-000} cf=${B:-000}" ;;
  esac
done < /tmp/nodes.txt
echo "[res] ✗ 試完 $N 條都唔通"
exit 1
