from pathlib import Path
import tempfile

from lerk_vpn.model import Client, parse_topology
from lerk_vpn.state import State

RAW = {
    "project": "demo",
    "defaults": {"swap_gb": 2},
    "nodes": {
        "fi1": {"host": "203.0.113.10", "country": "fi"},
        "de1": {"host": "203.0.113.11", "sni": "www.cloudflare.com"},
        "fr1": {"host": "203.0.113.12", "port": 8443},
    },
    "chains": {
        "main": {"path": ["fi1", "fr1"], "title": "FI-FR"},
        "triple": {"path": ["fi1", "de1", "fr1"]},
        "solo": {"path": ["de1"]},
    },
}


def make():
    topo = parse_topology(RAW)
    clients = [Client("ann", ["main", "triple"]), Client("bob", ["solo"]), Client("off", ["main"], enabled=False)]
    tmp = Path(tempfile.mkdtemp())
    state = State(tmp / "state.json")
    state.ensure(topo, clients)
    return topo, clients, state
