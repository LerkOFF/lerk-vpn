"""Подготовка сервера: swap, BBR, Xray, файрвол, применение конфига, проверка цепочки."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from . import __version__
from .model import Node, Topology
from .remote import Remote, RemoteError

XRAY_DIR = "/usr/local/etc/xray"
XRAY_CONFIG = f"{XRAY_DIR}/config.json"
MARKER = "/etc/lerk-vpn.json"

PREPARE = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 20; }
command -v apt-get >/dev/null || { echo "поддерживаются Debian и Ubuntu (нужен apt)"; exit 21; }
need=""
for b in curl unzip; do command -v $b >/dev/null || need="$need $b"; done
if [ -n "$need" ]; then apt-get update -qq >/dev/null; apt-get install -y -qq $need ca-certificates >/dev/null; fi
echo ok
"""


def swap_script(gb: int) -> str:
    if gb <= 0:
        return "echo пропуск\n"
    return f"""
set -e
want=$(({gb}*1024*1024*1024))
cur=$(swapon --noheadings --bytes --show=NAME,SIZE 2>/dev/null | awk '$1=="/swapfile"{{print $2}}')
if [ "$cur" != "$want" ]; then
  free_gb=$(df --output=avail -B1G / | tail -1 | tr -d ' ')
  if [ "$free_gb" -lt $(({gb}+2)) ]; then echo "мало места на диске для swap {gb} ГБ (свободно ${{free_gb}} ГБ)"; exit 22; fi
  swapoff /swapfile 2>/dev/null || true
  rm -f /swapfile
  fallocate -l {gb}G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$(({gb}*1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo swap создан
else
  echo swap уже {gb} ГБ
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/98-lerk-vpn-swap.conf
sysctl -q -p /etc/sysctl.d/98-lerk-vpn-swap.conf >/dev/null
"""


BBR = r"""
set -e
printf 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' > /etc/sysctl.d/99-lerk-vpn-bbr.conf
sysctl -q -p /etc/sysctl.d/99-lerk-vpn-bbr.conf >/dev/null || true
sysctl -n net.ipv4.tcp_congestion_control
"""


def xray_script(version: str | None) -> str:
    ver = f"--version v{version}" if version else ""
    want = version or ""
    return f"""
set -e
want="{want}"
have=""
command -v xray >/dev/null && have=$(xray version | awk 'NR==1{{print $2}}')
if [ -z "$have" ] || {{ [ -n "$want" ] && [ "$have" != "$want" ]; }}; then
  curl -fsSL --retry 3 -m 120 https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/xray-install.sh
  bash /tmp/xray-install.sh install {ver} >/tmp/xray-install.log 2>&1 || {{ tail -20 /tmp/xray-install.log; exit 23; }}
  rm -f /tmp/xray-install.sh
fi
xray version | awk 'NR==1{{print $2}}'
"""


def firewall_script(ssh_port: int, xray_port: int) -> str:
    return f"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v ufw >/dev/null || {{ apt-get update -qq >/dev/null; apt-get install -y -qq ufw >/dev/null; }}
ufw allow {ssh_port}/tcp >/dev/null
ufw allow {xray_port}/tcp >/dev/null
ufw --force enable >/dev/null
echo ufw включён
"""


def config_script(port: int) -> str:
    return f"""
set -e
d={XRAY_DIR}
cfg=$d/config.json
new=$d/config.lerk-new.json
test_out=$(xray run -test -config $new 2>&1) || {{ echo "$test_out" | tail -15; rm -f $new; exit 12; }}
owner=$(stat -c %G $d)
changed=1
if [ -f $cfg ] && cmp -s $cfg $new; then changed=0; rm -f $new; else
  mkdir -p $d/backup; chmod 700 $d/backup
  [ -f $cfg ] && cp -p $cfg $d/backup/config.$(date +%Y%m%d%H%M%S).json
  ls -1t $d/backup/config.*.json 2>/dev/null | tail -n +11 | xargs -r rm -f
  mv $new $cfg
fi
chown root:$owner $cfg; chmod 640 $cfg
systemctl enable xray >/dev/null 2>&1
if [ $changed = 1 ] || ! systemctl is-active --quiet xray; then systemctl restart xray; fi
sleep 1
systemctl is-active --quiet xray || {{ journalctl -u xray -n 20 --no-pager; exit 13; }}
ss -tln | grep -q ":{port} " || {{ echo "порт {port} не слушается"; exit 14; }}
echo changed=$changed
"""


@dataclass
class ApplyResult:
    node: str
    changed: bool
    xray: str
    steps: list[str]


def config_digest(config_json: str) -> str:
    return hashlib.sha256(config_json.encode()).hexdigest()[:16]


def render(config: dict) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def apply_node(topo: Topology, node: Node, config: dict, log=print) -> ApplyResult:
    rem = Remote(node)
    steps: list[str] = []

    def step(title: str, script: str, timeout: int = 600) -> str:
        log(f"  [{node.name}] {title}")
        out = rem.run(script, timeout=timeout).out.strip()
        steps.append(title)
        return out

    step("проверка системы", PREPARE)
    step(f"swap {node.swap_gb} ГБ", swap_script(node.swap_gb))
    if node.bbr:
        step("BBR", BBR)
    version = step("Xray", xray_script(topo.xray_version), timeout=900).splitlines()[-1]
    if node.firewall:
        step("файрвол ufw", firewall_script(node.ssh_port, node.port))
    text = render(config)
    log(f"  [{node.name}] конфиг ({config_digest(text)})")
    rem.put(text, f"{XRAY_DIR}/config.lerk-new.json", "640")
    out = step("применение конфига", config_script(node.port))
    changed = "changed=1" in out
    marker = {"project": topo.project, "version": __version__, "applied_at": int(time.time()), "config": config_digest(text), "xray": version}
    rem.put(json.dumps(marker) + "\n", MARKER, "644")
    return ApplyResult(node.name, changed, version, steps)


def status_script(port: int) -> str:
    return f"""
echo "active=$(systemctl is-active xray 2>/dev/null)"
echo "listen=$(ss -tln | grep -c ':{port} ')"
echo "xray=$(xray version 2>/dev/null | awk 'NR==1{{print $2}}')"
echo "swap_mb=$(free -m | awk '/^Swap/{{print $2}}')"
echo "mem_mb=$(free -m | awk '/^Mem/{{print $7}}')"
echo "load=$(cut -d' ' -f1 /proc/loadavg)"
echo "cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)"
echo "marker=$(cat {MARKER} 2>/dev/null | tr -d '\\n')"
echo "disk=$(df --output=pcent / | tail -1 | tr -d ' ')"
"""


def node_status(node: Node) -> dict:
    res = Remote(node).run(status_script(node.port), timeout=60)
    data = {}
    for line in res.out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    if data.get("marker"):
        try:
            data["marker"] = json.loads(data["marker"])
        except json.JSONDecodeError:
            data["marker"] = {}
    else:
        data["marker"] = {}
    return data


def verify_script(client_cfg: str, port: int, expect_ip: str) -> str:
    return f"""
set -e
f=$(mktemp /tmp/lerk-vpn-test.XXXXXX.json)
cat > $f <<'LERK_EOF'
{client_cfg}
LERK_EOF
xray run -config $f >/tmp/lerk-vpn-test.log 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null; rm -f $f' EXIT
sleep 3
ip=""
for url in https://api.ipify.org https://ipinfo.io/ip https://icanhazip.com; do
  ip=$(curl -s -m 12 -x socks5h://127.0.0.1:{port} $url | tr -d '[:space:]') && [ -n "$ip" ] && break || true
done
echo "ip=$ip"
[ "$ip" = "{expect_ip}" ] && echo verdict=ok || echo verdict=fail
"""


def _resolve(host: str) -> str:
    import socket

    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def verify_chain(topo: Topology, state, chain, client_name: str, log=print) -> tuple[bool, str]:
    """Поднимает на последнем сервере цепочки временного клиента и смотрит, с каким IP он выходит в сеть."""

    entry = topo.nodes[chain.entry]
    last = topo.nodes[chain.exit]
    entry_keys = state.node(entry.name)
    port = 10899
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {"vnext": [{"address": entry.host, "port": entry.port, "users": [{"id": state.client_uuid(client_name, chain.name), "encryption": "none", "flow": entry.flow}]}]},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {"serverName": entry.sni, "fingerprint": entry.fingerprint, "publicKey": entry_keys["public_key"], "shortId": entry_keys["short_id"], "spiderX": "/"},
                },
            }
        ],
    }
    expect = _resolve(last.host)
    res = Remote(last).run(verify_script(json.dumps(cfg), port, expect), timeout=120, check=False)
    got = ""
    for line in res.out.splitlines():
        if line.startswith("ip="):
            got = line[3:]
    ok = "verdict=ok" in res.out
    return ok, got


STATS_MARK = "---STATS---"


def collect_script(port: int) -> str:
    from .xraycfg import API_PORT

    return status_script(port) + f"""
echo {STATS_MARK}
xray api statsquery --server=127.0.0.1:{API_PORT} -reset 2>/dev/null || true
"""


def parse_stats(text: str) -> dict[str, dict[str, int]]:
    """{email: {"up": байт, "down": байт}} из вывода xray api statsquery."""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, int]] = {}
    for item in data.get("stat", []) or []:
        parts = str(item.get("name", "")).split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            slot = result.setdefault(parts[1], {"up": 0, "down": 0})
            slot["up" if parts[3] == "uplink" else "down"] += int(item.get("value", 0) or 0)
    return result


def node_collect(node: Node) -> tuple[dict, dict[str, dict[str, int]]]:
    res = Remote(node).run(collect_script(node.port), timeout=90)
    head, _, tail = res.out.partition(STATS_MARK)
    data = {}
    for line in head.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    try:
        data["marker"] = json.loads(data["marker"]) if data.get("marker") else {}
    except json.JSONDecodeError:
        data["marker"] = {}
    return data, parse_stats(tail)
