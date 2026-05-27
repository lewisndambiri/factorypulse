import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


API_URL = "http://localhost:8000"


def request_json(path: str, token: str | None = None, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, json.loads(raw) if raw else {}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def login(username: str, password: str) -> str:
    status, payload = request_json("/auth/login", method="POST", body={"username": username, "password": password})
    require(status == 200, f"login failed for {username}: {payload}")
    return payload["access_token"]


def main() -> int:
    maintenance_token = login("maintenance", "maintenance123")
    operator_token = login("operator", "operator123")

    status, _ = request_json("/machines")
    require(status == 401, "machines endpoint should require auth")

    status, machines = request_json("/machines", maintenance_token)
    require(status == 200 and len(machines) >= 1, "machines endpoint did not return telemetry")
    machine_id = machines[0]["machine_id"]

    status, system_status = request_json("/system/status", maintenance_token)
    require(status == 200, "system status failed")
    require(system_status["services"]["mqtt"] == "ok", "MQTT is not healthy")
    require(system_status["services"]["postgres"] == "ok", "PostgreSQL is not healthy")

    status, operator_response = request_json(
        "/demo/alarm",
        operator_token,
        method="POST",
        body={"machine_id": machine_id, "alarm_code": "TEMP-HIGH"},
    )
    require(status == 403, f"operator should not trigger demo alarm: {operator_response}")

    status, demo_alarm = request_json(
        "/demo/alarm",
        maintenance_token,
        method="POST",
        body={"machine_id": machine_id, "alarm_code": "TEMP-HIGH"},
    )
    require(status == 200 and demo_alarm["event_type"] == "alarm_triggered", "maintenance demo alarm failed")

    time.sleep(1)
    status, alarms = request_json("/alarms", maintenance_token)
    require(status == 200 and any(alarm["machine_id"] == machine_id for alarm in alarms), "demo alarm not visible")

    status, notifications = request_json("/notifications", maintenance_token)
    require(status == 200 and len(notifications) >= 1, "notification attempt not recorded")

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=8)
    params = urllib.parse.urlencode({"machine_id": "all", "start": start.isoformat(), "end": end.isoformat()})
    status, report = request_json(f"/reports/production?{params}", maintenance_token)
    require(status == 200 and "machines" in report, "production report failed")

    print("Smoke test passed")
    print(f"Checked machine: {machine_id}")
    print(f"System: {system_status['overall']}")
    print(f"Report machines: {report['machine_count']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
