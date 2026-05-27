# FactoryPulse Technical Notes

These notes explain the engineering theory behind FactoryPulse: why the system is shaped this way, how data moves, how analytics are computed, and what would change in a real deployment.

## 1. System Intent

FactoryPulse models a common industrial modernization problem: a machine already has local control and an onboard HMI, but customers want remote visibility into status, output, alarms, and productivity.

The design goal is not to replace PLC control logic. The design goal is to create a web-service layer around machine data:

- ingest telemetry from machines or gateways
- store high-frequency process measurements
- expose live dashboard updates
- calculate productivity metrics
- authorize remote interaction
- persist audit records for traceability
- leave room for production protocols such as OPC UA

## 2. Separation Of Responsibilities

The system is split into services because industrial monitoring has clear ownership boundaries.

| Component | Responsibility |
| --- | --- |
| Machine simulator | Emits realistic machine telemetry, events, and command results |
| OPC UA adapter | Represents the PLC integration boundary |
| Mosquitto MQTT | Brokered transport for telemetry, events, commands, and integration status |
| FastAPI backend | Auth, validation, REST APIs, WebSockets, analytics, persistence orchestration |
| InfluxDB | Time-series storage for high-frequency measurements |
| PostgreSQL | Durable audit records and persistent configuration |
| React dashboard | Browser-based HMI, analytics, reporting, and remote interaction |

This keeps protocol-specific machine communication out of the dashboard and keeps analytics storage separate from operational audit storage.

## 3. Telemetry And Event Flow

Machine telemetry is published to MQTT topics such as:

```text
factory/machines/{machine_id}/telemetry
factory/machines/{machine_id}/events
factory/machines/{machine_id}/command-results
factory/machines/{machine_id}/commands
factory/integrations/opcua/status
```

The backend subscribes to telemetry, events, command results, and integration status.

On each telemetry message, the backend:

1. Updates the latest in-memory machine snapshot.
2. Evaluates active machine alarms.
3. Evaluates threshold alert rules.
4. Writes time-series fields to InfluxDB.
5. Broadcasts live updates through WebSockets.

Machine events, command results, notification attempts, and configuration changes are written to PostgreSQL through the audit table.

## 4. Why MQTT

MQTT fits the project because machine telemetry is naturally event-oriented:

- machines publish state periodically
- alarms are events
- commands can be issued asynchronously
- command results may arrive later
- integrations can publish health heartbeats

Brokered messaging also decouples the backend from the simulator or future machine gateways. The backend only needs a topic contract and payload schema.

## 5. Why OPC UA

OPC UA is a common industrial protocol for structured PLC and equipment data. In this project, the OPC UA adapter is intentionally a boundary service rather than part of the dashboard or backend.

In local development it publishes a simulated health heartbeat. In a production deployment it would:

1. Connect to an OPC UA endpoint such as `opc.tcp://plc.example.local:4840`.
2. Read configured node IDs.
3. Normalize those node values into machine telemetry fields.
4. Publish the normalized data to MQTT.

This keeps PLC-specific details isolated and allows the rest of the system to keep using the same MQTT ingestion model.

## 6. Time-Series vs Relational Storage

FactoryPulse uses two databases because telemetry and audit records behave differently.

InfluxDB stores telemetry:

- production count
- reject count
- target count
- cycle time
- temperature
- pressure
- speed
- status and recipe tags

This data is high-frequency and queried over time windows for charts, OEE, reports, and process trends.

PostgreSQL stores operational records:

- command sent/rejected/result events
- machine alarm events
- threshold alert open/clear events
- notification attempts
- alert rules
- notification targets

These records are lower frequency, business-critical, and need durable traceability.

## 7. OEE And Productivity Metrics

FactoryPulse uses OEE-style calculations as a practical production-monitoring model.

Availability:

```text
running_samples / total_samples
```

Performance:

```text
ideal_cycle_time / average_cycle_time
```

Quality:

```text
good_units / (good_units + rejected_units)
```

OEE:

```text
availability * performance * quality
```

The implementation is intentionally lightweight. It gives realistic production-monitoring behavior without pretending to be a certified MES or historian.

## 8. Downtime Reasoning

There are two useful ways to think about downtime:

1. Telemetry-derived downtime: periods where the machine is not running.
2. Alarm-derived downtime: time associated with active or resolved alarms.

FactoryPulse uses both:

- Shift analytics estimate downtime from telemetry availability.
- Downtime reason analytics group persisted alarm trigger/resolution events by alarm code and machine.

This distinction matters because knowing that downtime happened is less valuable than knowing why it happened.

## 9. Remote Commands And Safety Validation

Remote interaction is intentionally role-gated and state-aware.

Examples:

- Operators can acknowledge alarms but cannot stop machines.
- Maintenance can start, stop, acknowledge, and reset alarms.
- Supervisors can change targets and recipes.
- Admins can do all remote commands.

The backend validates command context before publishing to MQTT:

- cannot start while an alarm is active
- cannot reset an alarm if none exists
- cannot change recipe while running
- cannot set target below current production

This models the difference between a web button and an industrially acceptable remote action. The UI asks for confirmation for higher-impact commands, but the backend remains the authority.

## 10. Authentication And Auditability

The project uses signed bearer tokens with demo users. This is enough to demonstrate:

- protected REST APIs
- protected WebSocket telemetry
- role-based command authorization
- configuration permissions

In production, this would be replaced with an identity provider, short-lived tokens, refresh workflows, and stronger secret management.

Every meaningful operational action is auditable:

- command sent
- command rejected
- command result
- alarm triggered/resolved
- threshold alert opened/cleared
- notification sent/failed
- alert rule changed
- notification target changed

The audit trail is a core part of the industrial story because remote actions must be explainable after the fact.

## 11. Alert Rules And Notifications

Threshold rules run against telemetry as it arrives. The current default rules are:

- `temperature > 75`
- `pressure < 3.2`
- `cycle_time_ms > 1800`

When a rule opens, it creates:

1. an active threshold alert
2. an InfluxDB event
3. a PostgreSQL audit record
4. a notification delivery attempt

Notification targets are persisted in PostgreSQL. The default target is simulated so demos do not require external infrastructure. Webhook targets are supported for real callbacks.

## 12. Reporting

The production report endpoint accepts:

- `machine_id`
- `start`
- `end`

It combines:

- InfluxDB telemetry for production deltas, rejects, cycle time, and availability
- PostgreSQL alarm events for alarm downtime reasons

The dashboard can export the report as CSV. This supports the original long-term productivity trend objective without building a full MES reporting module.

## 13. Simulator Design

The simulator is intentionally not purely random. Each machine has:

- target count
- base cycle time
- units per cycle
- reject probability
- alarm probability
- temperature baseline
- pressure baseline
- speed baseline
- possible alarm codes

Recipes modify process behavior:

- `standard`: balanced output and quality
- `high-throughput`: faster, warmer, higher reject risk
- `precision`: slower, cooler, lower reject risk

This makes the demo feel more like machine behavior than arbitrary random numbers.

## 14. Current Test Strategy

The backend unit tests focus on high-risk logic:

- token validation
- role-gated command validation
- unsafe command rejection
- demo alarm authorization
- alert-rule update authorization

The smoke test checks the running stack:

- login
- protected endpoint behavior
- system health
- demo alarm authorization
- notification recording
- production report response

This gives confidence in the core operational paths without overbuilding test infrastructure.

## 15. Production Changes

To move this closer to a real deployment, the next technical changes would be:

- replace demo auth with OIDC or another identity provider
- use TLS for API, WebSocket, MQTT, and database connections
- store secrets outside Compose files
- implement a real OPC UA polling client
- add database migrations
- persist users and roles in PostgreSQL
- add frontend workflow tests
- add monitoring for backend worker failures
- add retention policies for InfluxDB and audit tables

FactoryPulse is not a finished industrial platform but a coherent, explainable prototype showing how such a platform can be structured.
