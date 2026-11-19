"""Сборка конфигов Xray для каждого сервера и клиентских ссылок.

Модель: на сервере один вход VLESS + Reality (один порт, одна пара ключей). В нём живут
пользователи двух видов: клиенты (если сервер первый в цепочке) и звенья (если сервер идёт
после первого). Маршрут выбирается по email пользователя, поэтому через один сервер
могут проходить любые цепочки одновременно.
"""

from __future__ import annotations

from urllib.parse import quote

from .model import Chain, Client, Node, Topology
from .state import State

FLOW = "xtls-rprx-vision"
INBOUND_TAG = "in"


def client_email(chain: str, client: str) -> str:
    return f"c.{chain}.{client}"


def hop_email(chain: str, index: int) -> str:
    return f"h.{chain}.{index}"


def outbound_tag(chain: str) -> str:
    return f"to.{chain}"


def chains_through(topo: Topology, node: str) -> list[Chain]:
    return [c for c in topo.chains.values() if node in c.path]


def users_at(topo: Topology, clients: list[Client], state: State, node: str) -> list[tuple[str, str, str]]:
    """Список (chain, email, uuid) для входа сервера."""
    users: list[tuple[str, str, str]] = []
    for chain in chains_through(topo, node):
        pos = chain.path.index(node)
        if pos == 0:
            for client in clients:
                if client.enabled and chain.name in client.chains:
                    users.append((chain.name, client_email(chain.name, client.name), state.client_uuid(client.name, chain.name)))
        else:
            users.append((chain.name, hop_email(chain.name, pos - 1), state.hop_uuid(chain.name, pos - 1)))
    return users


def build_node_config(topo: Topology, clients: list[Client], state: State, node_name: str) -> dict:
    node = topo.nodes[node_name]
    keys = state.node(node_name)
    through = chains_through(topo, node_name)
    users = users_at(topo, clients, state, node_name)
    is_exit = any(c.exit == node_name for c in through)

    inbound = {
        "tag": INBOUND_TAG,
        "listen": "0.0.0.0",
        "port": node.port,
        "protocol": "vless",
        "settings": {
            "clients": [{"id": uid, "flow": FLOW, "email": email} for _, email, uid in users],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": f"{node.sni}:443",
                "xver": 0,
                "serverNames": [node.sni],
                "privateKey": keys["private_key"],
                "shortIds": [keys["short_id"]],
            },
        },
    }
    if is_exit:
        inbound["sniffing"] = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}

    outbounds: list[dict] = [{"tag": "block", "protocol": "blackhole"}]
    rules: list[dict] = []
    if is_exit:
        outbounds.append({"tag": "direct", "protocol": "freedom", "streamSettings": {"sockopt": {"domainStrategy": "UseIPv4"}}})
        rules.append({"type": "field", "inboundTag": [INBOUND_TAG], "ip": ["geoip:private"], "outboundTag": "block"})
        if node.block_torrents:
            rules.append({"type": "field", "inboundTag": [INBOUND_TAG], "protocol": ["bittorrent"], "outboundTag": "block"})

    for chain in through:
        pos = chain.path.index(node_name)
        emails = [email for ch, email, _ in users if ch == chain.name]
        if not emails:
            continue
        if pos == len(chain.path) - 1:
            target = "direct"
        else:
            target = outbound_tag(chain.name)
            nxt = topo.nodes[chain.path[pos + 1]]
            nxt_keys = state.node(nxt.name)
            outbounds.append(
                {
                    "tag": target,
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": nxt.host,
                                "port": nxt.port,
                                "users": [{"id": state.hop_uuid(chain.name, pos), "encryption": "none", "flow": FLOW}],
                            }
                        ]
                    },
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {
                            "serverName": nxt.sni,
                            "fingerprint": nxt.fingerprint,
                            "publicKey": nxt_keys["public_key"],
                            "shortId": nxt_keys["short_id"],
                            "spiderX": "/",
                        },
                    },
                }
            )
        rules.append({"type": "field", "inboundTag": [INBOUND_TAG], "user": emails, "outboundTag": target})

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }


def vless_link(topo: Topology, state: State, chain: Chain, client: str) -> str:
    entry = topo.nodes[chain.entry]
    keys = state.node(entry.name)
    host = entry.public_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    remark = quote(f"{chain.title}-{client}", safe="")
    params = (
        f"encryption=none&flow={FLOW}&security=reality&sni={entry.sni}&fp={entry.fingerprint}"
        f"&pbk={keys['public_key']}&sid={keys['short_id']}&type=tcp&headerType=none"
    )
    return f"vless://{state.client_uuid(client, chain.name)}@{host}:{entry.port}?{params}#{remark}"


def client_links(topo: Topology, state: State, client: Client) -> list[str]:
    return [vless_link(topo, state, topo.chains[c], client.name) for c in client.chains if c in topo.chains]


def apply_order(topo: Topology) -> list[str]:
    """Сначала выходы и промежуточные звенья, потом входы: к моменту запуска входа цель уже слушает."""
    depth: dict[str, int] = {}
    for chain in topo.chains.values():
        for i, name in enumerate(chain.path):
            depth[name] = max(depth.get(name, 0), i)
    return sorted(topo.nodes, key=lambda n: (-depth.get(n, 0), n))
