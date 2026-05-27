import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt


MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
OPCUA_ENDPOINT = os.getenv("OPCUA_ENDPOINT", "opc.tcp://plc.example.local:4840")
OPCUA_MODE = os.getenv("OPCUA_MODE", "simulated")
NODE_MAP = os.getenv(
    "OPCUA_NODE_MAP",
    "CUTTER-01.production_count=ns=2;s=Factory.Cutter.ProductionCount,"
    "CUTTER-01.status=ns=2;s=Factory.Cutter.Status,"
    "PACKER-02.production_count=ns=2;s=Factory.Packer.ProductionCount",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_node_map(raw_value: str) -> dict[str, str]:
    nodes: dict[str, str] = {}
    for item in raw_value.split(","):
        if "=" not in item:
            continue
        key, node_id = item.split("=", 1)
        nodes[key.strip()] = node_id.strip()
    return nodes


def status_payload(state: str, message: str, nodes: dict[str, str]) -> dict[str, Any]:
    return {
        "integration": "opcua",
        "state": state,
        "mode": OPCUA_MODE,
        "endpoint": OPCUA_ENDPOINT,
        "node_count": len(nodes),
        "message": message,
        "timestamp": now_iso(),
    }


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="factorypulse-opcua-adapter")


def publish_status(state: str, message: str, nodes: dict[str, str]) -> None:
    client.publish(
        "factory/integrations/opcua/status",
        json.dumps(status_payload(state, message, nodes)),
        qos=1,
        retain=True,
    )


def on_connect(mqtt_client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
    nodes = parse_node_map(NODE_MAP)
    publish_status("ok", "OPC UA adapter connected to MQTT", nodes)


client.on_connect = on_connect
for attempt in range(1, 31):
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        break
    except OSError as exc:
        if attempt == 30:
            raise
        print(f"MQTT unavailable, retrying in 1s: {exc}", flush=True)
        time.sleep(1)

nodes = parse_node_map(NODE_MAP)
client.loop_start()

while True:
    if OPCUA_MODE == "simulated":
        publish_status("ok", "Simulated OPC UA mapping ready", nodes)
    else:
        publish_status(
            "degraded",
            "Client mode configured; install an OPC UA client library and map these nodes for production",
            nodes,
        )
    time.sleep(5)
