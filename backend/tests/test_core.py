import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


def auth_header(username: str, role: str) -> str:
    return f"Bearer {main.create_token(username, role)}"


class FactoryPulseCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        main.latest_by_machine.clear()
        main.active_alarms.clear()
        main.active_threshold_alerts.clear()
        main.notification_attempts.clear()
        main.latest_by_machine["CUTTER-01"] = {
            "machine_id": "CUTTER-01",
            "timestamp": main.now_iso(),
            "status": "running",
            "production_count": 120,
            "target_count": 500,
            "cycle_time_ms": 980,
            "reject_count": 1,
            "temperature": 48,
            "pressure": 4.6,
            "speed": 86,
            "current_recipe": "standard",
            "active_alarm_code": None,
        }

    def test_token_round_trip_and_missing_header(self) -> None:
        token = main.create_token("maintenance", "maintenance")
        claims = main.verify_token(token)

        self.assertEqual(claims["sub"], "maintenance")
        self.assertEqual(claims["role"], "maintenance")
        with self.assertRaises(HTTPException) as raised:
            main.current_user(None)
        self.assertEqual(raised.exception.status_code, 401)

    def test_validate_command_rejects_unauthorized_role(self) -> None:
        request = main.CommandRequest(command="stop_machine")
        user = {"username": "operator", "role": "operator", "display_name": "Line Operator"}

        with patch.object(main, "record_audit"), self.assertRaises(HTTPException) as raised:
            main.validate_command("CUTTER-01", request, user, "cmd-1")

        self.assertEqual(raised.exception.status_code, 403)

    def test_validate_recipe_change_requires_idle(self) -> None:
        request = main.CommandRequest(command="change_recipe", value="precision")
        user = {"username": "supervisor", "role": "supervisor", "display_name": "Shift Supervisor"}

        with patch.object(main, "record_audit"), self.assertRaises(HTTPException) as raised:
            main.validate_command("CUTTER-01", request, user, "cmd-2")

        self.assertEqual(raised.exception.status_code, 409)

    def test_validate_target_cannot_be_below_current_output(self) -> None:
        request = main.CommandRequest(command="set_target_count", value=100)
        user = {"username": "supervisor", "role": "supervisor", "display_name": "Shift Supervisor"}

        with patch.object(main, "record_audit"), self.assertRaises(HTTPException) as raised:
            main.validate_command("CUTTER-01", request, user, "cmd-3")

        self.assertEqual(raised.exception.status_code, 409)

    def test_demo_alarm_is_role_gated_and_creates_alarm(self) -> None:
        request = main.DemoAlarmRequest(machine_id="CUTTER-01", alarm_code="TEMP-HIGH")

        with self.assertRaises(HTTPException) as raised:
            main.trigger_demo_alarm(request, auth_header("operator", "operator"))
        self.assertEqual(raised.exception.status_code, 403)

        with (
            patch.object(main, "write_event"),
            patch.object(main, "record_audit"),
            patch.object(main, "notify") as notify,
        ):
            payload = main.trigger_demo_alarm(request, auth_header("maintenance", "maintenance"))

        self.assertEqual(payload["event_type"], "alarm_triggered")
        self.assertEqual(payload["machine_id"], "CUTTER-01")
        self.assertEqual(main.active_alarms["CUTTER-01"]["code"], "TEMP-HIGH")
        notify.assert_called_once()

    def test_alert_rule_update_is_role_gated_and_persisted(self) -> None:
        request = main.AlertRuleUpdate(enabled=False)

        with self.assertRaises(HTTPException) as raised:
            main.update_alert_rule("cycle-time-high", request, auth_header("operator", "operator"))
        self.assertEqual(raised.exception.status_code, 403)

        with patch.object(main, "persist_alert_rule") as persist, patch.object(main, "record_audit"):
            rule = main.update_alert_rule("cycle-time-high", request, auth_header("supervisor", "supervisor"))
        self.assertFalse(rule["enabled"])
        persist.assert_called_once()

        with patch.object(main, "persist_alert_rule"), patch.object(main, "record_audit"):
            main.update_alert_rule("cycle-time-high", main.AlertRuleUpdate(enabled=True), auth_header("supervisor", "supervisor"))


if __name__ == "__main__":
    unittest.main()
