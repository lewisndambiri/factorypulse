import json
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt


MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MACHINE_IDS = [item.strip() for item in os.getenv("MACHINE_IDS", "CUTTER-01").split(",")]
RANDOM_SEED = os.getenv("SIMULATOR_SEED")
if RANDOM_SEED:
    random.seed(RANDOM_SEED)

RECIPES = ["standard", "high-throughput", "precision"]
RECIPE_PROFILES = {
    "standard": {"cycle_factor": 1.0, "reject_factor": 1.0, "temp_offset": 0.0, "pressure_offset": 0.0},
    "high-throughput": {"cycle_factor": 0.82, "reject_factor": 1.45, "temp_offset": 5.0, "pressure_offset": 0.2},
    "precision": {"cycle_factor": 1.2, "reject_factor": 0.55, "temp_offset": -2.0, "pressure_offset": -0.1},
}
MACHINE_PROFILES = {
    "CUTTER": {
        "target_count": 750,
        "base_cycle_ms": 980,
        "units_per_cycle": 2,
        "reject_probability": 0.012,
        "alarm_probability": 0.0018,
        "temperature": 48.0,
        "pressure": 4.6,
        "speed": 86.0,
        "alarms": ["TEMP-HIGH", "LOW-PRESSURE"],
    },
    "PACKER": {
        "target_count": 620,
        "base_cycle_ms": 1260,
        "units_per_cycle": 3,
        "reject_probability": 0.008,
        "alarm_probability": 0.0012,
        "temperature": 39.0,
        "pressure": 3.8,
        "speed": 74.0,
        "alarms": ["E-STOP", "LOW-PRESSURE"],
    },
    "ROBOT": {
        "target_count": 540,
        "base_cycle_ms": 1450,
        "units_per_cycle": 1,
        "reject_probability": 0.006,
        "alarm_probability": 0.001,
        "temperature": 44.0,
        "pressure": 4.1,
        "speed": 68.0,
        "alarms": ["E-STOP", "TEMP-HIGH"],
    },
}
DEFAULT_PROFILE = {
    "target_count": 500,
    "base_cycle_ms": 1200,
    "units_per_cycle": 2,
    "reject_probability": 0.01,
    "alarm_probability": 0.0015,
    "temperature": 42.0,
    "pressure": 4.2,
    "speed": 72.0,
    "alarms": ["E-STOP", "TEMP-HIGH", "LOW-PRESSURE"],
}
ALARM_SEVERITY = {
    "E-STOP": "critical",
    "TEMP-HIGH": "warning",
    "LOW-PRESSURE": "warning",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def machine_profile(machine_id: str) -> dict[str, Any]:
    prefix = machine_id.split("-")[0]
    return MACHINE_PROFILES.get(prefix, DEFAULT_PROFILE)


def drift(current: float, target: float, noise: float, lower: float, upper: float) -> float:
    next_value = current + ((target - current) * 0.12) + random.uniform(-noise, noise)
    return round(max(lower, min(upper, next_value)), 2)


@dataclass
class Machine:
    machine_id: str
    status: str = "idle"
    production_count: int = 0
    target_count: int = 500
    cycle_time_ms: int = 1200
    reject_count: int = 0
    temperature: float = 42.0
    pressure: float = 4.2
    speed: float = 72.0
    current_recipe: str = "standard"
    active_alarm_code: str | None = None
    active_alarm_id: str | None = None
    ticks_in_state: int = 0
    ticks_until_next_cycle: int = 1

    def __post_init__(self) -> None:
        profile = machine_profile(self.machine_id)
        self.target_count = int(profile["target_count"])
        self.cycle_time_ms = int(profile["base_cycle_ms"])
        self.temperature = float(profile["temperature"])
        self.pressure = float(profile["pressure"])
        self.speed = float(profile["speed"])
        self.status = "running"

    def recipe_profile(self) -> dict[str, float]:
        return RECIPE_PROFILES[self.current_recipe]

    def operating_targets(self) -> dict[str, float]:
        profile = machine_profile(self.machine_id)
        recipe = self.recipe_profile()
        return {
            "cycle_time_ms": float(profile["base_cycle_ms"]) * recipe["cycle_factor"],
            "temperature": float(profile["temperature"]) + recipe["temp_offset"],
            "pressure": float(profile["pressure"]) + recipe["pressure_offset"],
            "speed": float(profile["speed"]) / recipe["cycle_factor"],
            "reject_probability": float(profile["reject_probability"]) * recipe["reject_factor"],
            "alarm_probability": float(profile["alarm_probability"]),
        }

    def create_alarm(self) -> dict[str, Any]:
        profile = machine_profile(self.machine_id)
        if self.temperature > 82:
            alarm_code = "TEMP-HIGH"
        elif self.pressure < 3.0:
            alarm_code = "LOW-PRESSURE"
        else:
            alarm_code = random.choice(profile["alarms"])
        self.status = "alarm"
        self.active_alarm_code = alarm_code
        self.active_alarm_id = f"{self.machine_id}-{alarm_code}-{int(time.time() * 1000)}"
        return {
            "event_type": "alarm_triggered",
            "alarm_id": self.active_alarm_id,
            "machine_id": self.machine_id,
            "alarm_code": alarm_code,
            "severity": ALARM_SEVERITY[alarm_code],
            "message": f"{alarm_code} triggered on {self.machine_id}",
            "timestamp": now_iso(),
        }

    def tick_running(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        targets = self.operating_targets()
        self.ticks_until_next_cycle -= 1
        self.cycle_time_ms = max(650, int(random.gauss(targets["cycle_time_ms"], targets["cycle_time_ms"] * 0.045)))
        self.speed = round(drift(self.speed, targets["speed"], 1.2, 0, 120), 1)
        self.temperature = drift(self.temperature, targets["temperature"], 0.25, 25, 88)
        self.pressure = drift(self.pressure, targets["pressure"], 0.04, 2.4, 6.2)

        if self.ticks_until_next_cycle <= 0:
            self.production_count += int(machine_profile(self.machine_id)["units_per_cycle"])
            self.ticks_until_next_cycle = max(1, round(self.cycle_time_ms / 1000))
            if random.random() < targets["reject_probability"]:
                self.reject_count += 1

        if self.production_count >= self.target_count:
            self.status = "idle"
            self.speed = 0
            self.ticks_in_state = 0
        elif self.ticks_in_state > 20 and random.random() < 0.002:
            self.status = "idle"
            self.speed = 0
            self.ticks_in_state = 0
        elif random.random() < targets["alarm_probability"] or self.temperature >= 86 or self.pressure <= 2.7:
            events.append(self.create_alarm())
            self.ticks_in_state = 0
        return events

    def tick_idle(self) -> None:
        targets = self.operating_targets()
        self.speed = 0
        self.cycle_time_ms = int(targets["cycle_time_ms"])
        self.temperature = drift(self.temperature, float(machine_profile(self.machine_id)["temperature"]) - 3, 0.15, 25, 88)
        self.pressure = drift(self.pressure, float(machine_profile(self.machine_id)["pressure"]), 0.03, 2.4, 6.2)
        if self.production_count < self.target_count and self.ticks_in_state > 6 and random.random() < 0.18:
            self.status = "running"
            self.ticks_in_state = 0
        if self.production_count >= self.target_count and self.ticks_in_state > 20:
            self.production_count = 0
            self.reject_count = 0
            self.status = "running"
            self.ticks_in_state = 0

    def tick_alarm(self) -> None:
        self.speed = 0
        self.cycle_time_ms = 0
        self.temperature = drift(self.temperature, float(machine_profile(self.machine_id)["temperature"]) - 1, 0.12, 25, 88)
        self.pressure = drift(self.pressure, float(machine_profile(self.machine_id)["pressure"]), 0.03, 2.4, 6.2)

    def tick(self) -> list[dict[str, Any]]:
        self.ticks_in_state += 1
        if self.status == "running":
            return self.tick_running()
        if self.status == "idle":
            self.tick_idle()
        elif self.status == "alarm":
            self.tick_alarm()
        return []

    def telemetry(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "timestamp": now_iso(),
            "status": self.status,
            "production_count": self.production_count,
            "target_count": self.target_count,
            "cycle_time_ms": self.cycle_time_ms,
            "reject_count": self.reject_count,
            "temperature": self.temperature,
            "pressure": self.pressure,
            "speed": self.speed,
            "current_recipe": self.current_recipe,
            "active_alarm_code": self.active_alarm_code,
        }

    def apply_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = payload["command"]
        value = payload.get("value")
        accepted = True
        message = "Command accepted"

        if command == "start_machine":
            if self.status != "alarm":
                self.status = "running"
                self.ticks_in_state = 0
            else:
                accepted = False
                message = "Cannot start while alarm is active"
        elif command == "stop_machine":
            self.status = "idle"
            self.speed = 0
            self.ticks_in_state = 0
        elif command == "reset_alarm":
            previous_alarm = self.active_alarm_code
            self.active_alarm_code = None
            self.active_alarm_id = None
            self.status = "idle"
            self.ticks_in_state = 0
            if previous_alarm:
                message = f"{previous_alarm} reset"
        elif command == "acknowledge_alarm":
            if not self.active_alarm_code:
                accepted = False
                message = "No active alarm to acknowledge"
            else:
                message = f"{self.active_alarm_code} acknowledged"
        elif command == "set_target_count":
            target_count = int(value or self.target_count)
            if target_count < self.production_count:
                accepted = False
                message = "Target cannot be below current production"
            else:
                self.target_count = target_count
        elif command == "change_recipe":
            if str(value) in RECIPES:
                if self.status == "idle":
                    self.current_recipe = str(value)
                    self.ticks_until_next_cycle = 1
                else:
                    accepted = False
                    message = "Recipe changes require idle state"
            else:
                accepted = False
                message = "Unknown recipe"

        return {
            **payload,
            "accepted": accepted,
            "message": message,
            "result_timestamp": now_iso(),
        }


machines = {machine_id: Machine(machine_id=machine_id) for machine_id in MACHINE_IDS}
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="factorypulse-simulator")


def on_connect(mqtt_client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
    mqtt_client.subscribe("factory/machines/+/commands")


def on_message(mqtt_client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    payload = json.loads(msg.payload.decode())
    machine = machines.get(payload["machine_id"])
    if not machine:
        return
    result = machine.apply_command(payload)
    mqtt_client.publish(
        f"factory/machines/{machine.machine_id}/command-results",
        json.dumps(result),
        qos=1,
    )


def publish_loop() -> None:
    while True:
        for machine in machines.values():
            events = machine.tick()
            client.publish(
                f"factory/machines/{machine.machine_id}/telemetry",
                json.dumps(machine.telemetry()),
                qos=0,
            )
            for event in events:
                client.publish(
                    f"factory/machines/{machine.machine_id}/events",
                    json.dumps(event),
                    qos=1,
                )
        time.sleep(1)


client.on_connect = on_connect
client.on_message = on_message
for attempt in range(1, 31):
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        break
    except OSError as exc:
        if attempt == 30:
            raise
        print(f"MQTT unavailable, retrying in 1s: {exc}", flush=True)
        time.sleep(1)
threading.Thread(target=publish_loop, daemon=True).start()
client.loop_forever()
