import json
from app.core.database import SessionLocal
from app.models.storage import StorageNode
from app.models.private_network import PrivateNetwork, NetworkKind
from app.services.storage_service import build_runner_config
from app.services import private_network_dhcp as d
db = SessionLocal()
for node in db.query(StorageNode).all():
    cfg = build_runner_config(db, node)
    print("node", node.id, node.name, node.runner_version, "enabled", node.enabled, "vlan_mode", node.vlan_mode, "location", d.storage_location.node_location_id(node))
    print("  networks:", json.dumps(cfg.get("networks")))
    for t in cfg.get("tenants", []):
        print("  tenant", t.get("id"), "network_id", t.get("network_id"), "dhcp", json.dumps(t.get("dhcp")))
for n in db.query(PrivateNetwork).filter(PrivateNetwork.kind == NetworkKind.STANDARD).all():
    v = d.owner_view(db, n, staff=True)
    print("network", n.id, n.name, n.cidr, "vlan", n.segment_id, "loc", n.location_id, "available", v["available"], "served", v["served"], "node", v["serving_node"], "error", v["error"], "warnings", v["warnings"])
    print("   automatic", [(a["ip"], a["macs"]) for a in v["automatic"]], "leases", v["leases"])
