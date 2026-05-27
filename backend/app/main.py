import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from pydantic import BaseModel, Field


MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "your_created_token")
INFLUX_ORG = os.getenv("INFLUX_ORG", "factorypulse")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "machine_data")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://factorypulse:yourpassword@localhost:5432/factorypulse")
AUTH_SECRET = os.getenv("AUTH_SECRET", "create_one")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))
RECIPES = {"standard", "high-throughput", "precision"}
DEMO_USERS = {
    "operator": {"password": "yourpassword", "role": "operator", "display_name": "Line Operator"},
    "supervisor": {"password": "yourpassword", "role": "supervisor", "display_name": "Shift Supervisor"},
    "maintenance": {"password": "yourpassword", "role": "maintenance", "display_name": "Maintenance Tech"},
    "admin": {"password": "yourpassword", "role": "admin", "display_name": "System Admin"},
}
ROLE_PERMISSIONS = {
    "operator": {"acknowledge_alarm"},
    "supervisor": {"acknowledge_alarm", "set_target_count", "change_recipe"},
    "maintenance": {"start_machine", "stop_machine", "reset_alarm", "acknowledge_alarm"},
    "admin": {"start_machine", "stop_machine", "reset_alarm", "acknowledge_alarm", "set_target_count", "change_recipe"},
}


# Application wiring and in-memory fallbacks. PostgreSQL and InfluxDB are the
# durable stores, but the API can still demonstrate core flows while they start.
app = FastAPI(title="FactoryPulse API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

latest_by_machine: dict[str, dict[str, Any]] = {}
active_alarms: dict[str, dict[str, Any]] = {}
active_threshold_alerts: dict[str, dict[str, Any]] = {}
audit_log: list[dict[str, Any]] = []
integration_status: dict[str, dict[str, Any]] = {}
notification_targets: list[dict[str, Any]] = []
notification_attempts: list[dict[str, Any]] = []
websockets: set[WebSocket] = set()
event_loop: asyncio.AbstractEventLoop | None = None
mqtt_connected = False

influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx.write_api(write_options=SYNCHRONOUS)
query_api = influx.query_api()
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="factorypulse-api")
postgres_ready = False


# Request/response contracts exposed to the React dashboard.
class CommandRequest(BaseModel):
    command: str = Field(pattern="^(start_machine|stop_machine|reset_alarm|acknowledge_alarm|set_target_count|change_recipe)$")
    value: str | int | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class AlertRuleUpdate(BaseModel):
    enabled: bool


class NotificationTargetRequest(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    target_type: str = Field(pattern="^(simulated|webhook)$")
    endpoint: str = Field(min_length=3, max_length=300)
    enabled: bool = True


class DemoAlarmRequest(BaseModel):
    machine_id: str
    alarm_code: str = Field(pattern="^(E-STOP|TEMP-HIGH|LOW-PRESSURE)$")


DEFAULT_ALERT_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "temperature-high",
        "label": "Temperature high",
        "metric": "temperature",
        "operator": ">",
        "threshold": 75,
        "severity": "warning",
        "enabled": True,
    },
    {
        "rule_id": "pressure-low",
        "label": "Pressure low",
        "metric": "pressure",
        "operator": "<",
        "threshold": 3.2,
        "severity": "warning",
        "enabled": True,
    },
    {
        "rule_id": "cycle-time-high",
        "label": "Cycle time high",
        "metric": "cycle_time_ms",
        "operator": ">",
        "threshold": 1800,
        "severity": "info",
        "enabled": True,
    },
]
ALERT_RULES: list[dict[str, Any]] = [dict(rule) for rule in DEFAULT_ALERT_RULES]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_NOTIFICATION_TARGET = {
    "target_id": "target-simulated-maintenance",
    "name": "Maintenance Console",
    "target_type": "simulated",
    "endpoint": "local-demo",
    "enabled": True,
}
notification_targets.append({**DEFAULT_NOTIFICATION_TARGET, "created_at": now_iso()})


# Minimal signed demo tokens keep auth visible without adding an identity server.
def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_token(header: str, payload: str) -> str:
    message = f"{header}.{payload}".encode()
    return b64encode(hmac.new(AUTH_SECRET.encode(), message, hashlib.sha256).digest())


def create_token(username: str, role: str) -> str:
    header = b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64encode(
        json.dumps(
            {
                "sub": username,
                "role": role,
                "exp": int(time.time()) + TOKEN_TTL_SECONDS,
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{header}.{payload}.{sign_token(header, payload)}"


def verify_token(token: str) -> dict[str, Any]:
    try:
        header, payload, signature = token.split(".")
        expected = sign_token(header, payload)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Bad signature")
        claims = json.loads(b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    if int(claims.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Authentication token expired")
    if claims.get("sub") not in DEMO_USERS:
        raise HTTPException(status_code=401, detail="Unknown user")
    return claims


def current_user(authorization: str | None) -> dict[str, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    claims = verify_token(authorization.removeprefix("Bearer ").strip())
    username = str(claims["sub"])
    user = DEMO_USERS[username]
    return {"username": username, "role": user["role"], "display_name": user["display_name"]}


def require_read_access(authorization: str | None) -> dict[str, str]:
    return current_user(authorization)


# Alarm, event, and notification helpers translate raw machine state into
# operator-facing incidents.
def alarm_severity(code: str | None) -> str:
    if code == "E-STOP":
        return "critical"
    if code in {"TEMP-HIGH", "LOW-PRESSURE"}:
        return "warning"
    return "info"


def update_active_alarm(payload: dict[str, Any]) -> None:
    machine_id = payload["machine_id"]
    alarm_code = payload.get("active_alarm_code")
    if not alarm_code:
        return
    existing = active_alarms.get(machine_id)
    if existing and existing["code"] == alarm_code:
        existing["last_seen"] = payload["timestamp"]
        return
    active_alarms[machine_id] = {
        "alarm_id": f"{machine_id}-{alarm_code}-{int(time.time() * 1000)}",
        "machine_id": machine_id,
        "code": alarm_code,
        "severity": alarm_severity(alarm_code),
        "first_seen": payload["timestamp"],
        "last_seen": payload["timestamp"],
        "acknowledged": False,
        "acknowledged_by": None,
    }


def event_point(payload: dict[str, Any]) -> Point:
    return (
        Point("machine_events")
        .tag("machine_id", payload.get("machine_id", "unknown"))
        .tag("event_type", payload.get("event_type", "event"))
        .field("message", str(payload.get("message", "")))
        .field("severity", str(payload.get("severity", "info")))
        .time(payload.get("timestamp", now_iso()))
    )


def write_event(payload: dict[str, Any]) -> None:
    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=event_point(payload))
    except Exception as exc:
        print(f"InfluxDB event write failed: {exc}", flush=True)


def notify(event: dict[str, Any], category: str) -> None:
    for target in notification_targets:
        if not target["enabled"]:
            continue
        attempt = {
            "notification_id": f"notification-{int(time.time() * 1000)}-{target['target_id']}",
            "target_id": target["target_id"],
            "target_name": target["name"],
            "target_type": target["target_type"],
            "category": category,
            "event_type": event.get("event_type"),
            "machine_id": event.get("machine_id"),
            "severity": event.get("severity", "info"),
            "message": event.get("message", ""),
            "timestamp": now_iso(),
            "status": "sent",
            "detail": "Simulated delivery recorded",
        }
        if target["target_type"] == "webhook":
            request = urllib.request.Request(
                str(target["endpoint"]),
                data=json.dumps({"category": category, "event": event}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    attempt["detail"] = f"Webhook responded {response.status}"
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                attempt["status"] = "failed"
                attempt["detail"] = str(exc)
        notification_attempts.insert(0, attempt)
        notification_attempts[:] = notification_attempts[:50]
        record_audit(attempt, "notification")


def threshold_matches(value: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return value > threshold
    if operator == "<":
        return value < threshold
    return False


def evaluate_threshold_alerts(payload: dict[str, Any]) -> None:
    machine_id = payload["machine_id"]
    for rule in ALERT_RULES:
        alert_key = f"{machine_id}:{rule['rule_id']}"
        if not rule["enabled"]:
            active_threshold_alerts.pop(alert_key, None)
            continue
        raw_value = payload.get(rule["metric"])
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        breached = threshold_matches(value, rule["operator"], float(rule["threshold"]))
        existing = active_threshold_alerts.get(alert_key)
        if breached and not existing:
            alert = {
                "alert_id": f"{alert_key}:{int(time.time() * 1000)}",
                "event_type": "threshold_alert_opened",
                "rule_id": rule["rule_id"],
                "label": rule["label"],
                "machine_id": machine_id,
                "metric": rule["metric"],
                "operator": rule["operator"],
                "threshold": rule["threshold"],
                "value": round(value, 2),
                "severity": rule["severity"],
                "message": f"{rule['label']} on {machine_id}: {rule['metric']} {rule['operator']} {rule['threshold']}",
                "first_seen": payload["timestamp"],
                "last_seen": payload["timestamp"],
                "timestamp": payload["timestamp"],
            }
            active_threshold_alerts[alert_key] = alert
            write_event(alert)
            record_audit(alert, "threshold_alert")
            notify(alert, "threshold")
        elif breached and existing:
            existing["value"] = round(value, 2)
            existing["last_seen"] = payload["timestamp"]
            existing["timestamp"] = payload["timestamp"]
        elif not breached and existing:
            cleared = {
                **existing,
                "event_type": "threshold_alert_cleared",
                "cleared_at": payload["timestamp"],
                "timestamp": payload["timestamp"],
                "duration_minutes": alarm_duration_minutes(existing["first_seen"]),
                "message": f"{existing['label']} cleared on {machine_id}",
            }
            active_threshold_alerts.pop(alert_key, None)
            write_event(cleared)
            record_audit(cleared, "threshold_alert")


def init_postgres() -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    # The demo owns its schema so a fresh Docker volume can boot without a
    # separate migration step. A production system would replace this with
    # migrations and managed credentials.
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_entries (
                id BIGSERIAL PRIMARY KEY,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                entry_type TEXT NOT NULL,
                machine_id TEXT,
                command_id TEXT,
                command TEXT,
                status TEXT,
                requested_by TEXT,
                role TEXT,
                payload JSONB NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_entries_occurred_at ON audit_entries (occurred_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_entries_machine_id ON audit_entries (machine_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_rules (
                rule_id TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_targets (
                target_id TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for rule in DEFAULT_ALERT_RULES:
            conn.execute(
                """
                INSERT INTO alert_rules (rule_id, payload)
                VALUES (%s, %s)
                ON CONFLICT (rule_id) DO NOTHING
                """,
                (rule["rule_id"], Jsonb(rule)),
            )
        conn.execute(
            """
            INSERT INTO notification_targets (target_id, payload)
            VALUES (%s, %s)
            ON CONFLICT (target_id) DO NOTHING
            """,
            (DEFAULT_NOTIFICATION_TARGET["target_id"], Jsonb({**DEFAULT_NOTIFICATION_TARGET, "created_at": now_iso()})),
        )


def load_persistent_config() -> None:
    if not postgres_ready:
        return
    try:
        import psycopg

        with psycopg.connect(POSTGRES_DSN) as conn:
            rules = conn.execute("SELECT payload FROM alert_rules ORDER BY rule_id").fetchall()
            targets = conn.execute("SELECT payload FROM notification_targets ORDER BY target_id").fetchall()
        if rules:
            ALERT_RULES[:] = [row[0] for row in rules]
        if targets:
            notification_targets[:] = [row[0] for row in targets]
    except Exception as exc:
        print(f"PostgreSQL config load failed: {exc}", flush=True)


def persist_alert_rule(rule: dict[str, Any]) -> None:
    if not postgres_ready:
        return
    try:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute(
                """
                INSERT INTO alert_rules (rule_id, payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (rule_id)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (rule["rule_id"], Jsonb(rule)),
            )
    except Exception as exc:
        print(f"PostgreSQL alert rule persist failed: {exc}", flush=True)


def persist_notification_target(target: dict[str, Any]) -> None:
    if not postgres_ready:
        return
    try:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute(
                """
                INSERT INTO notification_targets (target_id, payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (target_id)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (target["target_id"], Jsonb(target)),
            )
    except Exception as exc:
        print(f"PostgreSQL notification target persist failed: {exc}", flush=True)


def audit_timestamp(payload: dict[str, Any]) -> datetime:
    raw_timestamp = payload.get("result_timestamp") or payload.get("timestamp") or now_iso()
    if isinstance(raw_timestamp, str):
        try:
            return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def record_audit(payload: dict[str, Any], entry_type: str) -> None:
    audit_log.insert(0, payload)
    audit_log[:] = audit_log[:100]
    if not postgres_ready:
        return
    try:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute(
                """
                INSERT INTO audit_entries (
                    occurred_at, entry_type, machine_id, command_id, command,
                    status, requested_by, role, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    audit_timestamp(payload),
                    entry_type,
                    payload.get("machine_id"),
                    payload.get("command_id"),
                    payload.get("command"),
                    payload.get("status") or ("accepted" if payload.get("accepted") else None),
                    payload.get("requested_by"),
                    payload.get("role"),
                    Jsonb(payload),
                ),
            )
    except Exception as exc:
        print(f"PostgreSQL audit write failed: {exc}", flush=True)


def read_audit_entries(limit: int = 50) -> list[dict[str, Any]]:
    if not postgres_ready:
        return audit_log[:limit]
    try:
        import psycopg

        with psycopg.connect(POSTGRES_DSN) as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM audit_entries
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [row[0] for row in rows]
    except Exception as exc:
        print(f"PostgreSQL audit read failed: {exc}", flush=True)
        return audit_log[:limit]


def read_machine_events(minutes: int = 480) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc).timestamp() - (minutes * 60)
    if not postgres_ready:
        return [
            entry
            for entry in audit_log
            if entry.get("event_type") in {"alarm_triggered", "alarm_resolved"}
            and parse_timestamp(str(entry.get("timestamp", now_iso()))).timestamp() >= cutoff
        ]
    try:
        import psycopg

        with psycopg.connect(POSTGRES_DSN) as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM audit_entries
                WHERE entry_type = 'machine_event'
                  AND occurred_at >= now() - (%s || ' minutes')::interval
                ORDER BY occurred_at DESC, id DESC
                """,
                (minutes,),
            ).fetchall()
            return [row[0] for row in rows]
    except Exception as exc:
        print(f"PostgreSQL machine event read failed: {exc}", flush=True)
        return [
            entry
            for entry in audit_log
            if entry.get("event_type") in {"alarm_triggered", "alarm_resolved"}
            and parse_timestamp(str(entry.get("timestamp", now_iso()))).timestamp() >= cutoff
        ]


def read_machine_events_range(start: datetime, end: datetime) -> list[dict[str, Any]]:
    if not postgres_ready:
        return [
            entry
            for entry in audit_log
            if entry.get("event_type") in {"alarm_triggered", "alarm_resolved"}
            and start <= parse_timestamp(str(entry.get("timestamp", now_iso()))) <= end
        ]
    try:
        import psycopg

        with psycopg.connect(POSTGRES_DSN) as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM audit_entries
                WHERE entry_type = 'machine_event'
                  AND occurred_at >= %s
                  AND occurred_at <= %s
                ORDER BY occurred_at DESC, id DESC
                """,
                (start, end),
            ).fetchall()
            return [row[0] for row in rows]
    except Exception as exc:
        print(f"PostgreSQL machine event range read failed: {exc}", flush=True)
        return [
            entry
            for entry in audit_log
            if entry.get("event_type") in {"alarm_triggered", "alarm_resolved"}
            and start <= parse_timestamp(str(entry.get("timestamp", now_iso()))) <= end
        ]


def parse_timestamp(timestamp: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def alarm_duration_minutes(started_at: str) -> float:
    elapsed = datetime.now(timezone.utc) - parse_timestamp(started_at)
    return round(max(0, elapsed.total_seconds() / 60), 1)


def service_status() -> dict[str, Any]:
    postgres_status = "ok" if postgres_ready else "degraded"
    try:
        import psycopg

        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute("SELECT 1").fetchone()
        postgres_status = "ok"
    except Exception:
        postgres_status = "down"

    try:
        influx_status = "ok" if influx.health().status == "pass" else "degraded"
    except Exception:
        influx_status = "down"

    now = datetime.now(timezone.utc)
    machines_status = []
    for machine in sorted(latest_by_machine.values(), key=lambda item: item["machine_id"]):
        last_seen = parse_timestamp(machine["timestamp"])
        seconds_since_last_seen = max(0, round((now - last_seen).total_seconds(), 1))
        if seconds_since_last_seen <= 10:
            connection_state = "online"
        elif seconds_since_last_seen <= 30:
            connection_state = "stale"
        else:
            connection_state = "offline"
        machines_status.append(
            {
                "machine_id": machine["machine_id"],
                "connection_state": connection_state,
                "last_seen": machine["timestamp"],
                "seconds_since_last_seen": seconds_since_last_seen,
                "machine_status": machine["status"],
            }
        )

    services = {
        "api": "ok",
        "mqtt": "ok" if mqtt_connected else "down",
        "influxdb": influx_status,
        "postgres": postgres_status,
        "opcua_adapter": integration_status.get("opcua", {}).get("state", "down"),
        "websocket_clients": len(websockets),
    }
    overall = "ok"
    if any(status == "down" for key, status in services.items() if key != "websocket_clients"):
        overall = "down"
    elif any(status == "degraded" for status in services.values()) or any(item["connection_state"] != "online" for item in machines_status):
        overall = "degraded"

    return {
        "timestamp": now_iso(),
        "overall": overall,
        "services": services,
        "machines": machines_status,
    }


def reject_command(command_id: str, machine_id: str, request: CommandRequest, user: dict[str, str], reason: str, status_code: int) -> None:
    payload = {
        "command_id": command_id,
        "machine_id": machine_id,
        "command": request.command,
        "value": request.value,
        "requested_by": user["username"],
        "role": user["role"],
        "timestamp": now_iso(),
        "status": "rejected",
        "message": reason,
    }
    record_audit(payload, "command_rejected")
    raise HTTPException(status_code=status_code, detail=reason)


# Remote machine interaction is deliberately constrained by role, current
# machine state, and command-specific safety rules.
def validate_command(machine_id: str, request: CommandRequest, user: dict[str, str], command_id: str) -> None:
    machine = latest_by_machine.get(machine_id)
    if not machine:
        reject_command(command_id, machine_id, request, user, "Machine has not published telemetry yet", 404)

    allowed = ROLE_PERMISSIONS.get(user["role"], set())
    if request.command not in allowed:
        reject_command(command_id, machine_id, request, user, f"Role '{user['role']}' cannot execute {request.command}", 403)

    active_alarm = active_alarms.get(machine_id) or machine.get("active_alarm_code")
    if request.command == "start_machine" and active_alarm:
        reject_command(command_id, machine_id, request, user, "Cannot start while an alarm is active", 409)
    if request.command in {"reset_alarm", "acknowledge_alarm"} and not active_alarm:
        reject_command(command_id, machine_id, request, user, "No active alarm for this command", 409)
    if request.command == "stop_machine" and machine["status"] == "offline":
        reject_command(command_id, machine_id, request, user, "Cannot stop an offline machine", 409)
    if request.command == "change_recipe":
        if str(request.value) not in RECIPES:
            reject_command(command_id, machine_id, request, user, "Unknown recipe", 422)
        if machine["status"] != "idle":
            reject_command(command_id, machine_id, request, user, "Recipe changes are allowed only while idle", 409)
    if request.command == "set_target_count":
        try:
            target_count = int(request.value or 0)
        except (TypeError, ValueError):
            reject_command(command_id, machine_id, request, user, "Target count must be a number", 422)
        if target_count < 1 or target_count > 10000:
            reject_command(command_id, machine_id, request, user, "Target count must be between 1 and 10000", 422)
        if target_count < int(machine["production_count"]):
            reject_command(command_id, machine_id, request, user, "Target count cannot be below current production", 409)


async def broadcast(message: dict[str, Any]) -> None:
    dead_connections: list[WebSocket] = []
    for socket in websockets:
        try:
            await socket.send_json(message)
        except RuntimeError:
            dead_connections.append(socket)
    for socket in dead_connections:
        websockets.discard(socket)


# MQTT is the bridge between the machine/integration side and the web API.
def telemetry_point(payload: dict[str, Any]) -> Point:
    point = (
        Point("machine_telemetry")
        .tag("machine_id", payload["machine_id"])
        .tag("status", payload["status"])
        .tag("recipe", payload.get("current_recipe", "unknown"))
        .field("production_count", int(payload["production_count"]))
        .field("target_count", int(payload["target_count"]))
        .field("cycle_time_ms", int(payload["cycle_time_ms"]))
        .field("reject_count", int(payload["reject_count"]))
        .field("temperature", float(payload["temperature"]))
        .field("pressure", float(payload["pressure"]))
        .field("speed", float(payload["speed"]))
    )
    if payload.get("active_alarm_code"):
        point.tag("alarm_code", payload["active_alarm_code"])
    return point.time(payload["timestamp"])


def on_connect(client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
    global mqtt_connected
    mqtt_connected = True
    client.subscribe("factory/machines/+/telemetry")
    client.subscribe("factory/machines/+/events")
    client.subscribe("factory/machines/+/command-results")
    client.subscribe("factory/integrations/+/status")


def on_disconnect(client: mqtt.Client, userdata: Any, *args: Any) -> None:
    global mqtt_connected
    mqtt_connected = False


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    global event_loop
    payload = json.loads(msg.payload.decode())
    topic = msg.topic

    if topic.endswith("/telemetry"):
        latest_by_machine[payload["machine_id"]] = payload
        update_active_alarm(payload)
        evaluate_threshold_alerts(payload)
        try:
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=telemetry_point(payload))
        except Exception as exc:
            print(f"InfluxDB write failed: {exc}", flush=True)
        message_type = "telemetry"
    elif topic.startswith("factory/integrations/") and topic.endswith("/status"):
        integration_status[str(payload.get("integration", "unknown"))] = payload
        record_audit(payload, "integration_status")
        message_type = "integration_status"
    elif topic.endswith("/command-results"):
        if payload.get("accepted") and payload.get("command") == "reset_alarm":
            alarm = active_alarms.pop(payload["machine_id"], None)
            if alarm:
                resolution_payload = {
                    "event_type": "alarm_resolved",
                    "alarm_id": alarm["alarm_id"],
                    "machine_id": payload["machine_id"],
                    "alarm_code": alarm["code"],
                    "severity": alarm["severity"],
                    "acknowledged": alarm["acknowledged"],
                    "acknowledged_by": alarm["acknowledged_by"],
                    "resolved_by": payload.get("requested_by"),
                    "duration_minutes": alarm_duration_minutes(alarm["first_seen"]),
                    "message": f"{alarm['code']} resolved on {payload['machine_id']}",
                    "timestamp": now_iso(),
                }
                write_event(resolution_payload)
                record_audit(resolution_payload, "machine_event")
        if payload.get("accepted") and payload.get("command") == "acknowledge_alarm":
            alarm = active_alarms.get(payload["machine_id"])
            if alarm:
                alarm["acknowledged"] = True
                alarm["acknowledged_by"] = payload.get("requested_by")
        record_audit(payload, "command_result")
        message_type = "command_result"
    else:
        if payload.get("event_type") == "alarm_triggered":
            active_alarms[payload["machine_id"]] = {
                "alarm_id": payload.get("alarm_id"),
                "machine_id": payload["machine_id"],
                "code": payload.get("alarm_code"),
                "severity": payload.get("severity", alarm_severity(payload.get("alarm_code"))),
                "first_seen": payload["timestamp"],
                "last_seen": payload["timestamp"],
                "acknowledged": False,
                "acknowledged_by": None,
            }
        write_event(payload)
        record_audit(payload, "machine_event")
        if payload.get("event_type") == "alarm_triggered":
            notify(payload, "machine_alarm")
        message_type = "event"

    if event_loop:
        asyncio.run_coroutine_threadsafe(broadcast({"type": message_type, "payload": payload}), event_loop)


@app.on_event("startup")
async def startup() -> None:
    global event_loop, postgres_ready
    event_loop = asyncio.get_running_loop()
    for attempt in range(1, 31):
        try:
            init_postgres()
            postgres_ready = True
            load_persistent_config()
            break
        except Exception as exc:
            if attempt == 30:
                print(f"PostgreSQL unavailable; audit will use memory fallback: {exc}", flush=True)
                break
            print(f"PostgreSQL unavailable, retrying in 1s: {exc}", flush=True)
            await asyncio.sleep(1)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    for attempt in range(1, 31):
        try:
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            if attempt == 30:
                raise
            print(f"MQTT unavailable, retrying in 1s: {exc}", flush=True)
            await asyncio.sleep(1)
    mqtt_client.loop_start()


@app.on_event("shutdown")
async def shutdown() -> None:
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    influx.close()


# HTTP routes are grouped by operator workflow: access, live operations,
# configuration, analytics, reports, and remote commands.
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/system/status")
def system_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_read_access(authorization)
    return service_status()


@app.post("/auth/login")
def login(request: LoginRequest) -> dict[str, Any]:
    user = DEMO_USERS.get(request.username)
    if not user or not hmac.compare_digest(user["password"], request.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {
        "access_token": create_token(request.username, user["role"]),
        "token_type": "bearer",
        "user": {
            "username": request.username,
            "role": user["role"],
            "display_name": user["display_name"],
        },
    }


@app.get("/auth/me")
def me(authorization: str | None = Header(default=None)) -> dict[str, str]:
    return current_user(authorization)


@app.get("/machines")
def machines(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return sorted(latest_by_machine.values(), key=lambda item: item["machine_id"])


@app.get("/machines/{machine_id}/latest")
def latest(machine_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_read_access(authorization)
    if machine_id not in latest_by_machine:
        raise HTTPException(status_code=404, detail="Machine has not published telemetry yet")
    return latest_by_machine[machine_id]


@app.get("/machines/{machine_id}/history")
def history(machine_id: str, minutes: int = 30, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return machine_history(machine_id, minutes)


def machine_history(machine_id: str, minutes: int = 30) -> list[dict[str, Any]]:
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{minutes}m)
  |> filter(fn: (r) => r._measurement == "machine_telemetry")
  |> filter(fn: (r) => r.machine_id == "{machine_id}")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "production_count", "reject_count", "cycle_time_ms", "temperature", "pressure", "speed"])
'''
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"InfluxDB query failed: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for table in tables:
        for record in table.records:
            values = record.values
            rows.append(
                {
                    "timestamp": values["_time"].isoformat(),
                    "production_count": values.get("production_count", 0),
                    "reject_count": values.get("reject_count", 0),
                    "cycle_time_ms": values.get("cycle_time_ms", 0),
                    "temperature": values.get("temperature", 0),
                    "pressure": values.get("pressure", 0),
                    "speed": values.get("speed", 0),
                }
            )
    return rows


def machine_history_range(machine_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: time(v: "{start.isoformat()}"), stop: time(v: "{end.isoformat()}"))
  |> filter(fn: (r) => r._measurement == "machine_telemetry")
  |> filter(fn: (r) => r.machine_id == "{machine_id}")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "production_count", "reject_count", "cycle_time_ms", "temperature", "pressure", "speed"])
'''
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"InfluxDB report query failed: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for table in tables:
        for record in table.records:
            values = record.values
            rows.append(
                {
                    "timestamp": values["_time"].isoformat(),
                    "production_count": values.get("production_count", 0),
                    "reject_count": values.get("reject_count", 0),
                    "cycle_time_ms": values.get("cycle_time_ms", 0),
                    "temperature": values.get("temperature", 0),
                    "pressure": values.get("pressure", 0),
                    "speed": values.get("speed", 0),
                }
            )
    return rows


def production_summary(machine_id: str, rows: list[dict[str, Any]], minutes: int) -> dict[str, Any]:
    if not rows:
        return {
            "machine_id": machine_id,
            "produced": 0,
            "rejects": 0,
            "availability": 0,
            "performance": 0,
            "quality": 0,
            "oee": 0,
            "downtime_minutes": minutes,
            "average_cycle_time_ms": 0,
            "reject_rate": 0,
        }

    running_rows = [row for row in rows if row.get("cycle_time_ms", 0) > 0 and row.get("speed", 0) > 0]
    cycle_times = [float(row["cycle_time_ms"]) for row in running_rows if row.get("cycle_time_ms", 0) > 0]
    produced_delta = max(0, int(rows[-1]["production_count"]) - int(rows[0]["production_count"]))
    reject_delta = max(0, int(rows[-1]["reject_count"]) - int(rows[0]["reject_count"]))
    total_attempts = produced_delta + reject_delta
    availability = len(running_rows) / len(rows)
    average_cycle_time = sum(cycle_times) / len(cycle_times) if cycle_times else 0
    ideal_cycle_time = 1000
    performance = min(1, ideal_cycle_time / average_cycle_time) if average_cycle_time else 0
    quality = (produced_delta / total_attempts) if total_attempts else 1
    downtime_minutes = round((1 - availability) * minutes, 1)
    reject_rate = round((reject_delta / total_attempts) * 100, 1) if total_attempts else 0

    return {
        "machine_id": machine_id,
        "produced": produced_delta,
        "rejects": reject_delta,
        "availability": round(availability * 100, 1),
        "performance": round(performance * 100, 1),
        "quality": round(quality * 100, 1),
        "oee": round(availability * performance * quality * 100, 1),
        "downtime_minutes": downtime_minutes,
        "average_cycle_time_ms": round(average_cycle_time),
        "reject_rate": reject_rate,
    }


@app.get("/audit")
def audit(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return read_audit_entries(50)


@app.get("/alarms")
def alarms(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return sorted(active_alarms.values(), key=lambda item: (item["severity"], item["machine_id"]))


@app.get("/alerts")
def threshold_alerts(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return sorted(active_threshold_alerts.values(), key=lambda item: (item["severity"], item["machine_id"], item["rule_id"]))


@app.get("/alert-rules")
def alert_rules(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return ALERT_RULES


@app.post("/alert-rules/{rule_id}")
def update_alert_rule(rule_id: str, request: AlertRuleUpdate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = current_user(authorization)
    if user["role"] not in {"supervisor", "admin"}:
        raise HTTPException(status_code=403, detail="Only supervisors and admins can update alert rules")
    for rule in ALERT_RULES:
        if rule["rule_id"] == rule_id:
            rule["enabled"] = request.enabled
            if not request.enabled:
                for alert_key in [key for key in active_threshold_alerts if key.endswith(f":{rule_id}")]:
                    active_threshold_alerts.pop(alert_key, None)
            payload = {
                "event_type": "alert_rule_updated",
                "rule_id": rule_id,
                "enabled": request.enabled,
                "requested_by": user["username"],
                "role": user["role"],
                "timestamp": now_iso(),
            }
            persist_alert_rule(rule)
            record_audit(payload, "threshold_rule")
            return rule
    raise HTTPException(status_code=404, detail="Unknown alert rule")


@app.get("/notification-targets")
def list_notification_targets(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return notification_targets


@app.post("/notification-targets")
def create_notification_target(request: NotificationTargetRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = current_user(authorization)
    if user["role"] not in {"supervisor", "admin"}:
        raise HTTPException(status_code=403, detail="Only supervisors and admins can manage notification targets")
    target = {
        "target_id": f"target-{int(time.time() * 1000)}",
        "name": request.name,
        "target_type": request.target_type,
        "endpoint": request.endpoint,
        "enabled": request.enabled,
        "created_at": now_iso(),
    }
    notification_targets.append(target)
    persist_notification_target(target)
    record_audit({**target, "event_type": "notification_target_created", "requested_by": user["username"], "role": user["role"]}, "notification_target")
    return target


@app.post("/notification-targets/{target_id}")
def update_notification_target(target_id: str, request: NotificationTargetRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = current_user(authorization)
    if user["role"] not in {"supervisor", "admin"}:
        raise HTTPException(status_code=403, detail="Only supervisors and admins can manage notification targets")
    for target in notification_targets:
        if target["target_id"] == target_id:
            target.update(
                {
                    "name": request.name,
                    "target_type": request.target_type,
                    "endpoint": request.endpoint,
                    "enabled": request.enabled,
                }
            )
            persist_notification_target(target)
            record_audit({**target, "event_type": "notification_target_updated", "requested_by": user["username"], "role": user["role"], "timestamp": now_iso()}, "notification_target")
            return target
    raise HTTPException(status_code=404, detail="Unknown notification target")


@app.get("/notifications")
def list_notifications(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_read_access(authorization)
    return notification_attempts[:50]


@app.post("/demo/alarm")
def trigger_demo_alarm(request: DemoAlarmRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = current_user(authorization)
    if user["role"] not in {"supervisor", "maintenance", "admin"}:
        raise HTTPException(status_code=403, detail="Only supervisors, maintenance, and admins can trigger demo alarms")
    machine = latest_by_machine.get(request.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine has not published telemetry yet")

    timestamp = now_iso()
    payload = {
        "event_type": "alarm_triggered",
        "alarm_id": f"demo-{request.machine_id}-{request.alarm_code}-{int(time.time() * 1000)}",
        "machine_id": request.machine_id,
        "alarm_code": request.alarm_code,
        "severity": alarm_severity(request.alarm_code),
        "message": f"{request.alarm_code} demo alarm on {request.machine_id}",
        "triggered_by": user["username"],
        "timestamp": timestamp,
    }
    machine["status"] = "alarm"
    machine["active_alarm_code"] = request.alarm_code
    active_alarms[request.machine_id] = {
        "alarm_id": payload["alarm_id"],
        "machine_id": request.machine_id,
        "code": request.alarm_code,
        "severity": payload["severity"],
        "first_seen": timestamp,
        "last_seen": timestamp,
        "acknowledged": False,
        "acknowledged_by": None,
    }
    write_event(payload)
    record_audit(payload, "machine_event")
    notify(payload, "machine_alarm")
    if event_loop:
        asyncio.run_coroutine_threadsafe(broadcast({"type": "event", "payload": payload}), event_loop)
        asyncio.run_coroutine_threadsafe(broadcast({"type": "telemetry", "payload": machine}), event_loop)
    return payload


@app.get("/machines/{machine_id}/oee")
def oee(machine_id: str, minutes: int = 30, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_read_access(authorization)
    rows = machine_history(machine_id, minutes)
    latest = latest_by_machine.get(machine_id)
    if not rows or not latest:
        return {
            "machine_id": machine_id,
            "window_minutes": minutes,
            "availability": 0,
            "performance": 0,
            "quality": 0,
            "oee": 0,
            "downtime_minutes": 0,
            "average_cycle_time_ms": 0,
            "reject_rate": 0,
        }

    summary = production_summary(machine_id, rows, minutes)

    return {
        "machine_id": machine_id,
        "window_minutes": minutes,
        "availability": summary["availability"],
        "performance": summary["performance"],
        "quality": summary["quality"],
        "oee": summary["oee"],
        "downtime_minutes": summary["downtime_minutes"],
        "average_cycle_time_ms": summary["average_cycle_time_ms"],
        "reject_rate": summary["reject_rate"],
    }


@app.get("/analytics/shift")
def shift_analytics(minutes: int = 480, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_read_access(authorization)
    machine_summaries: list[dict[str, Any]] = []
    for machine_id in sorted(latest_by_machine):
        rows = machine_history(machine_id, minutes)
        machine_summaries.append(production_summary(machine_id, rows, minutes))

    total_produced = sum(item["produced"] for item in machine_summaries)
    total_rejects = sum(item["rejects"] for item in machine_summaries)
    total_downtime = round(sum(item["downtime_minutes"] for item in machine_summaries), 1)
    active_alarm_count = len(active_alarms)
    average_oee = round(sum(item["oee"] for item in machine_summaries) / len(machine_summaries), 1) if machine_summaries else 0
    total_attempts = total_produced + total_rejects
    reject_rate = round((total_rejects / total_attempts) * 100, 1) if total_attempts else 0

    return {
        "window_minutes": minutes,
        "machine_count": len(machine_summaries),
        "total_produced": total_produced,
        "total_rejects": total_rejects,
        "reject_rate": reject_rate,
        "total_downtime_minutes": total_downtime,
        "active_alarm_count": active_alarm_count,
        "average_oee": average_oee,
        "machines": machine_summaries,
    }


@app.get("/analytics/downtime")
def downtime_analytics(minutes: int = 480, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_read_access(authorization)
    minutes = max(1, min(minutes, 10080))
    events = read_machine_events(minutes)
    reasons: dict[str, dict[str, Any]] = {}
    machines: dict[str, dict[str, Any]] = {}
    total_alarm_events = 0

    def reason_bucket(alarm_code: str) -> dict[str, Any]:
        return reasons.setdefault(
            alarm_code,
            {"alarm_code": alarm_code, "count": 0, "duration_minutes": 0.0, "active_count": 0},
        )

    def machine_bucket(machine_id: str) -> dict[str, Any]:
        return machines.setdefault(
            machine_id,
            {"machine_id": machine_id, "count": 0, "duration_minutes": 0.0, "active_count": 0},
        )

    for event in events:
        event_type = event.get("event_type")
        alarm_code = str(event.get("alarm_code") or event.get("code") or "UNKNOWN")
        machine_id = str(event.get("machine_id") or "unknown")
        if event_type == "alarm_triggered":
            total_alarm_events += 1
            reason_bucket(alarm_code)["count"] += 1
            machine_bucket(machine_id)["count"] += 1
        if event_type == "alarm_resolved":
            duration = round(float(event.get("duration_minutes") or 0), 1)
            reason_bucket(alarm_code)["duration_minutes"] += duration
            machine_bucket(machine_id)["duration_minutes"] += duration

    for alarm in active_alarms.values():
        alarm_code = str(alarm.get("code") or "UNKNOWN")
        machine_id = str(alarm.get("machine_id") or "unknown")
        duration = alarm_duration_minutes(str(alarm.get("first_seen", now_iso())))
        reason = reason_bucket(alarm_code)
        machine = machine_bucket(machine_id)
        reason["duration_minutes"] += duration
        reason["active_count"] += 1
        machine["duration_minutes"] += duration
        machine["active_count"] += 1

    reason_rows = sorted(
        ({**item, "duration_minutes": round(item["duration_minutes"], 1)} for item in reasons.values()),
        key=lambda item: (item["duration_minutes"], item["count"]),
        reverse=True,
    )
    machine_rows = sorted(
        ({**item, "duration_minutes": round(item["duration_minutes"], 1)} for item in machines.values()),
        key=lambda item: item["machine_id"],
    )

    return {
        "window_minutes": minutes,
        "total_alarm_events": total_alarm_events,
        "total_downtime_minutes": round(sum(item["duration_minutes"] for item in reason_rows), 1),
        "active_alarm_count": len(active_alarms),
        "reasons": reason_rows,
        "machines": machine_rows,
    }


def downtime_breakdown_for_range(start: datetime, end: datetime, machine_ids: set[str]) -> dict[str, Any]:
    events = [
        event
        for event in read_machine_events_range(start, end)
        if str(event.get("machine_id") or "unknown") in machine_ids
    ]
    reasons: dict[str, dict[str, Any]] = {}
    total_alarm_events = 0

    def reason_bucket(alarm_code: str) -> dict[str, Any]:
        return reasons.setdefault(alarm_code, {"alarm_code": alarm_code, "count": 0, "duration_minutes": 0.0, "active_count": 0})

    for event in events:
        alarm_code = str(event.get("alarm_code") or event.get("code") or "UNKNOWN")
        if event.get("event_type") == "alarm_triggered":
            total_alarm_events += 1
            reason_bucket(alarm_code)["count"] += 1
        if event.get("event_type") == "alarm_resolved":
            reason_bucket(alarm_code)["duration_minutes"] += round(float(event.get("duration_minutes") or 0), 1)

    now = datetime.now(timezone.utc)
    for alarm in active_alarms.values():
        machine_id = str(alarm.get("machine_id") or "unknown")
        first_seen = parse_timestamp(str(alarm.get("first_seen", now_iso())))
        if machine_id not in machine_ids or first_seen > end or now < start:
            continue
        active_start = max(first_seen, start)
        active_end = min(now, end)
        if active_end < active_start:
            continue
        duration = round(max(0, (active_end - active_start).total_seconds() / 60), 1)
        reason = reason_bucket(str(alarm.get("code") or "UNKNOWN"))
        reason["duration_minutes"] += duration
        reason["active_count"] += 1

    reason_rows = sorted(
        ({**item, "duration_minutes": round(item["duration_minutes"], 1)} for item in reasons.values()),
        key=lambda item: (item["duration_minutes"], item["count"]),
        reverse=True,
    )
    return {
        "total_alarm_events": total_alarm_events,
        "total_downtime_minutes": round(sum(item["duration_minutes"] for item in reason_rows), 1),
        "active_alarm_count": sum(item["active_count"] for item in reason_rows),
        "reasons": reason_rows,
    }


@app.get("/reports/production")
def production_report(
    start: str,
    end: str,
    machine_id: str = "all",
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_read_access(authorization)
    start_at = parse_timestamp(start)
    end_at = parse_timestamp(end)
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="End time must be after start time")
    window_minutes = round((end_at - start_at).total_seconds() / 60)
    if window_minutes > 10080:
        raise HTTPException(status_code=422, detail="Reports are limited to a 7 day window")

    known_machine_ids = sorted(latest_by_machine)
    if machine_id == "all":
        report_machine_ids = known_machine_ids
    elif machine_id in latest_by_machine:
        report_machine_ids = [machine_id]
    else:
        raise HTTPException(status_code=404, detail="Unknown machine")

    machine_summaries: list[dict[str, Any]] = []
    for current_machine_id in report_machine_ids:
        rows = machine_history_range(current_machine_id, start_at, end_at)
        machine_summaries.append(production_summary(current_machine_id, rows, window_minutes))

    total_produced = sum(item["produced"] for item in machine_summaries)
    total_rejects = sum(item["rejects"] for item in machine_summaries)
    total_attempts = total_produced + total_rejects
    downtime = downtime_breakdown_for_range(start_at, end_at, set(report_machine_ids))
    average_oee = round(sum(item["oee"] for item in machine_summaries) / len(machine_summaries), 1) if machine_summaries else 0

    return {
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "window_minutes": window_minutes,
        "machine_id": machine_id,
        "machine_count": len(machine_summaries),
        "total_produced": total_produced,
        "total_rejects": total_rejects,
        "reject_rate": round((total_rejects / total_attempts) * 100, 1) if total_attempts else 0,
        "average_oee": average_oee,
        "telemetry_downtime_minutes": round(sum(item["downtime_minutes"] for item in machine_summaries), 1),
        "alarm_downtime_minutes": downtime["total_downtime_minutes"],
        "alarm_events": downtime["total_alarm_events"],
        "active_alarm_count": downtime["active_alarm_count"],
        "top_downtime_reasons": downtime["reasons"][:5],
        "machines": machine_summaries,
    }


@app.post("/machines/{machine_id}/commands")
def command(machine_id: str, request: CommandRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = current_user(authorization)
    command_id = f"{machine_id}-{int(time.time() * 1000)}"
    validate_command(machine_id, request, user, command_id)
    payload = {
        "command_id": command_id,
        "machine_id": machine_id,
        "command": request.command,
        "value": request.value,
        "requested_by": user["username"],
        "role": user["role"],
        "timestamp": now_iso(),
    }
    mqtt_client.publish(f"factory/machines/{machine_id}/commands", json.dumps(payload), qos=1)
    record_audit({**payload, "status": "sent"}, "command_sent")
    return {"status": "sent", "command_id": command_id}


@app.websocket("/ws/machines")
async def machine_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    try:
        verify_token(token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    websockets.add(websocket)
    await websocket.send_json({"type": "snapshot", "payload": list(latest_by_machine.values())})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        websockets.discard(websocket)
