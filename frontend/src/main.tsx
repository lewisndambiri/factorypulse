import React, { useEffect, useMemo, useState } from "react";
import ReactDOM from "react-dom/client";
import Plot from "react-plotly.js";
import type { Layout } from "plotly.js";
import { Activity, AlertTriangle, CheckCircle2, Download, Gauge, History, LogIn, LogOut, Play, RotateCcw, Settings2, ShieldCheck, Square, Target } from "lucide-react";
import "./styles.css";

type Machine = {
  machine_id: string;
  timestamp: string;
  status: "running" | "idle" | "alarm" | "maintenance" | "offline";
  production_count: number;
  target_count: number;
  cycle_time_ms: number;
  reject_count: number;
  temperature: number;
  pressure: number;
  speed: number;
  current_recipe: string;
  active_alarm_code: string | null;
};

type HistoryRow = {
  timestamp: string;
  production_count: number;
  reject_count: number;
  cycle_time_ms: number;
  temperature: number;
  pressure: number;
  speed: number;
};

type OeeMetrics = {
  machine_id: string;
  window_minutes: number;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  downtime_minutes: number;
  average_cycle_time_ms: number;
  reject_rate: number;
};

type Alarm = {
  alarm_id: string;
  machine_id: string;
  code: string;
  severity: "critical" | "warning" | "info";
  first_seen: string;
  last_seen: string;
  acknowledged: boolean;
  acknowledged_by: string | null;
};

type ThresholdAlert = {
  alert_id: string;
  rule_id: string;
  label: string;
  machine_id: string;
  metric: string;
  operator: string;
  threshold: number;
  value: number;
  severity: "critical" | "warning" | "info";
  first_seen: string;
  last_seen: string;
};

type AlertRule = {
  rule_id: string;
  label: string;
  metric: string;
  operator: string;
  threshold: number;
  severity: "critical" | "warning" | "info";
  enabled: boolean;
};

type ShiftMachine = {
  machine_id: string;
  produced: number;
  rejects: number;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  downtime_minutes: number;
  reject_rate: number;
};

type ShiftAnalytics = {
  window_minutes: number;
  machine_count: number;
  total_produced: number;
  total_rejects: number;
  reject_rate: number;
  total_downtime_minutes: number;
  active_alarm_count: number;
  average_oee: number;
  machines: ShiftMachine[];
};

type DowntimeReason = {
  alarm_code: string;
  count: number;
  duration_minutes: number;
  active_count: number;
};

type DowntimeMachine = {
  machine_id: string;
  count: number;
  duration_minutes: number;
  active_count: number;
};

type DowntimeAnalytics = {
  window_minutes: number;
  total_alarm_events: number;
  total_downtime_minutes: number;
  active_alarm_count: number;
  reasons: DowntimeReason[];
  machines: DowntimeMachine[];
};

type ProductionReport = {
  start: string;
  end: string;
  window_minutes: number;
  machine_id: string;
  machine_count: number;
  total_produced: number;
  total_rejects: number;
  reject_rate: number;
  average_oee: number;
  telemetry_downtime_minutes: number;
  alarm_downtime_minutes: number;
  alarm_events: number;
  active_alarm_count: number;
  top_downtime_reasons: DowntimeReason[];
  machines: ShiftMachine[];
};

type SystemStatus = {
  timestamp: string;
  overall: "ok" | "degraded" | "down";
  services: {
    api: string;
    mqtt: string;
    influxdb: string;
    postgres: string;
    opcua_adapter: string;
    websocket_clients: number;
  };
  machines: Array<{
    machine_id: string;
    connection_state: "online" | "stale" | "offline";
    last_seen: string;
    seconds_since_last_seen: number;
    machine_status: string;
  }>;
};

type NotificationTarget = {
  target_id: string;
  name: string;
  target_type: "simulated" | "webhook";
  endpoint: string;
  enabled: boolean;
  created_at: string;
};

type NotificationAttempt = {
  notification_id: string;
  target_name: string;
  target_type: string;
  category: string;
  event_type: string;
  machine_id: string;
  severity: string;
  message: string;
  timestamp: string;
  status: "sent" | "failed";
  detail: string;
};

type User = {
  username: string;
  role: "operator" | "supervisor" | "maintenance" | "admin";
  display_name: string;
};

type DashboardView = "operations" | "analytics" | "configuration" | "system";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const WS_URL = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws/machines";
const RECIPES = ["standard", "high-throughput", "precision"];
const DASHBOARD_VIEWS: Array<{ id: DashboardView; label: string }> = [
  { id: "operations", label: "Operations" },
  { id: "analytics", label: "Analytics" },
  { id: "configuration", label: "Configuration" },
  { id: "system", label: "System" },
];

function statusClass(status: Machine["status"]) {
  if (status === "running") return "bg-signal text-white";
  if (status === "alarm") return "bg-fault text-white";
  if (status === "idle") return "bg-warning text-white";
  return "bg-steel text-white";
}

function confirmAndSend(message: string, action: () => void) {
  if (window.confirm(message)) {
    action();
  }
}

function datetimeLocalValue(date: Date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60_000).toISOString().slice(0, 16);
}

function App() {
  const [machines, setMachines] = useState<Machine[]>([]);
  const [selectedId, setSelectedId] = useState("CUTTER-01");
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [audit, setAudit] = useState<Record<string, unknown>[]>([]);
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [thresholdAlerts, setThresholdAlerts] = useState<ThresholdAlert[]>([]);
  const [alertRules, setAlertRules] = useState<AlertRule[]>([]);
  const [notificationTargets, setNotificationTargets] = useState<NotificationTarget[]>([]);
  const [notifications, setNotifications] = useState<NotificationAttempt[]>([]);
  const [oee, setOee] = useState<OeeMetrics | null>(null);
  const [shift, setShift] = useState<ShiftAnalytics | null>(null);
  const [downtime, setDowntime] = useState<DowntimeAnalytics | null>(null);
  const [report, setReport] = useState<ProductionReport | null>(null);
  const [reportMachine, setReportMachine] = useState("all");
  const [reportStart, setReportStart] = useState(() => datetimeLocalValue(new Date(Date.now() - 8 * 60 * 60 * 1000)));
  const [reportEnd, setReportEnd] = useState(() => datetimeLocalValue(new Date()));
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [targetInput, setTargetInput] = useState("500");
  const [recipeInput, setRecipeInput] = useState("standard");
  const [notificationName, setNotificationName] = useState("Shift Webhook");
  const [notificationType, setNotificationType] = useState<"simulated" | "webhook">("simulated");
  const [notificationEndpoint, setNotificationEndpoint] = useState("local-demo");
  const [commandNotice, setCommandNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [token, setToken] = useState(() => localStorage.getItem("factorypulse_token") ?? "");
  const [user, setUser] = useState<User | null>(null);
  const [loginUsername, setLoginUsername] = useState("maintenance");
  const [loginPassword, setLoginPassword] = useState("maintenance123");
  const [connected, setConnected] = useState(false);
  const [activeView, setActiveView] = useState<DashboardView>("operations");
  const authHeaders = useMemo(() => ({ Authorization: `Bearer ${token}` }), [token]);

  const selected = machines.find((machine) => machine.machine_id === selectedId) ?? machines[0];
  const totalProduced = machines.reduce((sum, machine) => sum + machine.production_count, 0);
  const runningCount = machines.filter((machine) => machine.status === "running").length;
  const alarmCount = machines.filter((machine) => machine.status === "alarm").length;
  const canManageAlertRules = user?.role === "supervisor" || user?.role === "admin";

  useEffect(() => {
    if (!token) {
      setMachines([]);
      setHistory([]);
      setAudit([]);
      setAlarms([]);
      setThresholdAlerts([]);
      setAlertRules([]);
      setNotificationTargets([]);
      setNotifications([]);
      setOee(null);
      setShift(null);
      setDowntime(null);
      setReport(null);
      setSystemStatus(null);
      setConnected(false);
      return;
    }
    fetch(`${API_URL}/auth/me`, { headers: { Authorization: `Bearer ${token}` } })
      .then((response) => {
        if (!response.ok) throw new Error("Session expired");
        return response.json();
      })
      .then(setUser)
      .catch(() => {
        localStorage.removeItem("factorypulse_token");
        setToken("");
        setUser(null);
      });
  }, [token]);

  useEffect(() => {
    if (!user || !token) return;
    fetch(`${API_URL}/machines`, { headers: authHeaders })
      .then((response) => response.json())
      .then((data: Machine[]) => {
        setMachines(data);
        if (data[0]) setSelectedId(data[0].machine_id);
      })
      .catch(() => undefined);

    const socketUrl = `${WS_URL}?token=${encodeURIComponent(token)}`;
    const socket = new WebSocket(socketUrl);
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "snapshot") {
        setMachines(message.payload);
      }
      if (message.type === "telemetry") {
        const incoming: Machine = message.payload;
        setMachines((current) => {
          const others = current.filter((machine) => machine.machine_id !== incoming.machine_id);
          return [...others, incoming].sort((a, b) => a.machine_id.localeCompare(b.machine_id));
        });
      }
      if (message.type === "command_result" || message.type === "event") {
        setAudit((current) => [message.payload, ...current].slice(0, 8));
        fetch(`${API_URL}/alarms`, { headers: authHeaders })
          .then((response) => response.json())
          .then(setAlarms)
          .catch(() => undefined);
      }
    };
    return () => socket.close();
  }, [user, token, authHeaders]);

  useEffect(() => {
    if (!selectedId || !user || !token) return;
    const load = () => {
      fetch(`${API_URL}/machines/${selectedId}/history?minutes=30`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setHistory)
        .catch(() => undefined);
      fetch(`${API_URL}/audit`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setAudit)
        .catch(() => undefined);
      fetch(`${API_URL}/alarms`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setAlarms)
        .catch(() => undefined);
      fetch(`${API_URL}/alerts`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setThresholdAlerts)
        .catch(() => undefined);
      fetch(`${API_URL}/alert-rules`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setAlertRules)
        .catch(() => undefined);
      fetch(`${API_URL}/notification-targets`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setNotificationTargets)
        .catch(() => undefined);
      fetch(`${API_URL}/notifications`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setNotifications)
        .catch(() => undefined);
      fetch(`${API_URL}/machines/${selectedId}/oee?minutes=30`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setOee)
        .catch(() => undefined);
      fetch(`${API_URL}/analytics/shift?minutes=480`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setShift)
        .catch(() => undefined);
      fetch(`${API_URL}/analytics/downtime?minutes=480`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setDowntime)
        .catch(() => undefined);
      fetch(`${API_URL}/system/status`, { headers: authHeaders })
        .then((response) => response.json())
        .then(setSystemStatus)
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [selectedId, user, token, authHeaders]);

  useEffect(() => {
    if (!selected) return;
    setTargetInput(String(selected.target_count));
    setRecipeInput(selected.current_recipe);
  }, [selected?.machine_id, selected?.target_count, selected?.current_recipe]);

  const progress = selected ? Math.min(100, Math.round((selected.production_count / selected.target_count) * 100)) : 0;
  const timestamps = useMemo(() => history.map((row) => new Date(row.timestamp)), [history]);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    const response = await fetch(`${API_URL}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: loginUsername, password: loginPassword }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Login failed" });
      return;
    }
    localStorage.setItem("factorypulse_token", body.access_token);
    setToken(body.access_token);
    setUser(body.user);
    setCommandNotice({ tone: "ok", text: `Signed in as ${body.user.role}` });
  }

  function logout() {
    localStorage.removeItem("factorypulse_token");
    setToken("");
    setUser(null);
    setCommandNotice({ tone: "ok", text: "Signed out" });
  }

  async function sendCommand(command: string, value?: string | number) {
    if (!selected) return;
    await sendCommandFor(selected.machine_id, command, value);
  }

  async function sendCommandFor(machineId: string, command: string, value?: string | number) {
    if (!token) {
      setCommandNotice({ tone: "error", text: "Sign in before sending machine commands" });
      return;
    }
    const response = await fetch(`${API_URL}/machines/${machineId}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ command, value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Command rejected" });
      return;
    }
    setCommandNotice({ tone: "ok", text: `${command.replace(/_/g, " ")} sent` });
  }

  async function toggleAlertRule(rule: AlertRule) {
    const response = await fetch(`${API_URL}/alert-rules/${rule.rule_id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ enabled: !rule.enabled }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Alert rule update rejected" });
      return;
    }
    setAlertRules((current) => current.map((item) => (item.rule_id === body.rule_id ? body : item)));
    setCommandNotice({ tone: "ok", text: `${body.label} ${body.enabled ? "enabled" : "disabled"}` });
  }

  async function createNotificationTarget() {
    const response = await fetch(`${API_URL}/notification-targets`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        name: notificationName,
        target_type: notificationType,
        endpoint: notificationEndpoint,
        enabled: true,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Notification target rejected" });
      return;
    }
    setNotificationTargets((current) => [...current, body]);
    setCommandNotice({ tone: "ok", text: `${body.name} target added` });
  }

  async function toggleNotificationTarget(target: NotificationTarget) {
    const response = await fetch(`${API_URL}/notification-targets/${target.target_id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ...target, enabled: !target.enabled }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Notification target update rejected" });
      return;
    }
    setNotificationTargets((current) => current.map((item) => (item.target_id === body.target_id ? body : item)));
    setCommandNotice({ tone: "ok", text: `${body.name} ${body.enabled ? "enabled" : "disabled"}` });
  }

  async function triggerDemoAlarm() {
    if (!selected) return;
    const response = await fetch(`${API_URL}/demo/alarm`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ machine_id: selected.machine_id, alarm_code: "TEMP-HIGH" }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Demo alarm rejected" });
      return;
    }
    setCommandNotice({ tone: "ok", text: `${body.alarm_code} demo alarm triggered` });
  }

  async function loadReport() {
    if (!token) return;
    const params = new URLSearchParams({
      machine_id: reportMachine,
      start: new Date(reportStart).toISOString(),
      end: new Date(reportEnd).toISOString(),
    });
    const response = await fetch(`${API_URL}/reports/production?${params.toString()}`, { headers: authHeaders });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setCommandNotice({ tone: "error", text: body.detail ?? "Report request failed" });
      return;
    }
    setReport(body);
    setCommandNotice({ tone: "ok", text: "Production report updated" });
  }

  function exportReportCsv() {
    if (!report) return;
    const rows = [
      ["machine_id", "produced", "rejects", "reject_rate", "oee", "availability", "performance", "quality", "downtime_minutes"],
      ...report.machines.map((machine) => [
        machine.machine_id,
        machine.produced,
        machine.rejects,
        machine.reject_rate,
        machine.oee,
        machine.availability,
        machine.performance,
        machine.quality,
        machine.downtime_minutes,
      ]),
    ];
    const csv = rows.map((row) => row.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `factorypulse-report-${report.machine_id}-${report.start.slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="min-h-screen bg-panel text-ink">
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
          <div>
            <p className="text-sm font-semibold uppercase tracking-wide text-signal">FactoryPulse</p>
            <h1 className="text-2xl font-semibold">Industrial Production Monitor</h1>
          </div>
          <div className="flex items-center gap-2 text-sm font-medium">
            <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-signal" : "bg-fault"}`} />
            {connected ? "Live link active" : "Reconnecting"}
          </div>
        </div>
      </header>

      <section className="mx-auto max-w-7xl px-5 pt-5">
        <div className="rounded-md border border-line bg-white p-4 shadow-sm">
          {user ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-3">
                <span className="inline-flex rounded bg-emerald-50 p-2 text-signal">
                  <ShieldCheck size={18} />
                </span>
                <div>
                  <p className="font-semibold">{user.display_name}</p>
                  <p className="text-sm text-steel">{user.username} | {user.role}</p>
                </div>
              </div>
              <CommandButton icon={<LogOut size={16} />} label="Sign Out" onClick={logout} />
            </div>
          ) : (
            <form className="grid gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end" onSubmit={login}>
              <label className="grid gap-2 text-sm font-medium text-steel">
                User
                <select
                  className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                  value={loginUsername}
                  onChange={(event) => {
                    setLoginUsername(event.target.value);
                    setLoginPassword(`${event.target.value}123`);
                  }}
                >
                  <option value="operator">operator</option>
                  <option value="supervisor">supervisor</option>
                  <option value="maintenance">maintenance</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <label className="grid gap-2 text-sm font-medium text-steel">
                Password
                <input
                  className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                  type="password"
                  value={loginPassword}
                  onChange={(event) => setLoginPassword(event.target.value)}
                />
              </label>
              <button className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-signal bg-signal px-3 text-sm font-semibold text-white" type="submit">
                <LogIn size={16} />
                Sign In
              </button>
            </form>
          )}
        </div>
      </section>

      {!user && (
        <section className="mx-auto max-w-7xl px-5 py-6">
          <div className="rounded-md border border-line bg-white p-6 shadow-sm">
            <div className="flex items-start gap-3">
              <span className="inline-flex rounded bg-emerald-50 p-2 text-signal">
                <ShieldCheck size={20} />
              </span>
              <div>
                <h2 className="text-lg font-semibold">Authentication Required</h2>
                <p className="mt-1 text-sm text-steel">Sign in to access live telemetry, analytics, alarms, and remote machine controls.</p>
              </div>
            </div>
            {commandNotice && (
              <div className={`mt-4 rounded-md border px-4 py-3 text-sm font-medium ${commandNotice.tone === "ok" ? "border-signal bg-emerald-50 text-signal" : "border-fault bg-red-50 text-fault"}`}>
                {commandNotice.text}
              </div>
            )}
          </div>
        </section>
      )}

      {user && (
        <>

      <section className="mx-auto grid max-w-7xl gap-4 px-5 py-5 md:grid-cols-4">
        <Kpi icon={<Activity size={20} />} label="Running Machines" value={`${runningCount}/${machines.length || 0}`} />
        <Kpi icon={<Target size={20} />} label="Produced Units" value={totalProduced.toLocaleString()} />
        <Kpi icon={<AlertTriangle size={20} />} label="Active Alarms" value={alarmCount.toString()} tone={alarmCount ? "fault" : "normal"} />
        <Kpi icon={<Gauge size={20} />} label="Selected OEE" value={`${oee?.oee ?? 0}%`} />
      </section>

      <section className="mx-auto max-w-7xl px-5 pb-5">
        <div className="flex flex-wrap gap-2 rounded-md border border-line bg-white p-2 shadow-sm">
          {DASHBOARD_VIEWS.map((view) => (
            <button
              className={`h-10 rounded-md px-3 text-sm font-semibold ${activeView === view.id ? "bg-signal text-white" : "bg-white text-steel hover:bg-panel"}`}
              key={view.id}
              onClick={() => setActiveView(view.id)}
            >
              {view.label}
            </button>
          ))}
        </div>
      </section>

      <section className="mx-auto grid max-w-7xl gap-5 px-5 pb-6 lg:grid-cols-[360px_1fr]">
        <aside className="space-y-3">
          {machines.map((machine) => (
            <button
              className={`w-full rounded-md border bg-white p-4 text-left shadow-sm transition hover:border-signal ${
                selected?.machine_id === machine.machine_id ? "border-signal" : "border-line"
              }`}
              key={machine.machine_id}
              onClick={() => setSelectedId(machine.machine_id)}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="font-semibold">{machine.machine_id}</h2>
                  <p className="mt-1 text-sm text-steel">{machine.current_recipe}</p>
                </div>
                <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${statusClass(machine.status)}`}>
                  {machine.status}
                </span>
              </div>
              <div className="mt-4 h-2 rounded bg-line">
                <div className="h-2 rounded bg-signal" style={{ width: `${Math.min(100, (machine.production_count / machine.target_count) * 100)}%` }} />
              </div>
              <p className="mt-2 text-sm text-steel">
                {machine.production_count} / {machine.target_count} units
              </p>
            </button>
          ))}
        </aside>

        {selected && (
          <div className="space-y-5">
            {activeView === "operations" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-steel">Selected machine</p>
                  <h2 className="mt-1 text-2xl font-semibold">{selected.machine_id}</h2>
                </div>
                <div className="flex flex-wrap gap-2">
                  <CommandButton icon={<Play size={16} />} label="Start" onClick={() => sendCommand("start_machine")} />
                  <CommandButton icon={<Square size={16} />} label="Stop" onClick={() => confirmAndSend("Stop this machine?", () => sendCommand("stop_machine"))} />
                  <CommandButton icon={<CheckCircle2 size={16} />} label="Ack" onClick={() => sendCommand("acknowledge_alarm")} />
                  <CommandButton icon={<RotateCcw size={16} />} label="Reset" onClick={() => confirmAndSend("Reset active alarm?", () => sendCommand("reset_alarm"))} />
                  <CommandButton icon={<AlertTriangle size={16} />} label="Demo Alarm" onClick={triggerDemoAlarm} />
                </div>
              </div>

              <div className="mt-5 grid gap-4 md:grid-cols-4">
                <Metric label="Status" value={selected.status} />
                <Metric label="Progress" value={`${progress}%`} />
                <Metric label="Cycle Time" value={`${selected.cycle_time_ms} ms`} />
                <Metric label="Temperature" value={`${selected.temperature} C`} />
              </div>

              <div className="mt-4 grid gap-4 md:grid-cols-5">
                <Metric label="Availability" value={`${oee?.availability ?? 0}%`} />
                <Metric label="Performance" value={`${oee?.performance ?? 0}%`} />
                <Metric label="Quality" value={`${oee?.quality ?? 0}%`} />
                <Metric label="Downtime" value={`${oee?.downtime_minutes ?? 0} min`} />
                <Metric label="Reject Rate" value={`${oee?.reject_rate ?? 0}%`} />
              </div>

              {selected.active_alarm_code && (
                <div className="mt-4 rounded-md border border-fault bg-red-50 px-4 py-3 text-sm font-medium text-fault">
                  Active alarm: {selected.active_alarm_code}
                </div>
              )}

              {commandNotice && (
                <div className={`mt-4 rounded-md border px-4 py-3 text-sm font-medium ${commandNotice.tone === "ok" ? "border-signal bg-emerald-50 text-signal" : "border-fault bg-red-50 text-fault"}`}>
                  {commandNotice.text}
                </div>
              )}
            </section>
            )}

            {activeView === "analytics" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={18} />
                  <h2 className="text-lg font-semibold">Downtime Reasons</h2>
                </div>
                <span className="text-sm font-medium text-steel">{downtime ? `${Math.round(downtime.window_minutes / 60)}h window` : "Loading"}</span>
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <Metric label="Downtime" value={`${downtime?.total_downtime_minutes ?? 0} min`} />
                <Metric label="Alarm Events" value={`${downtime?.total_alarm_events ?? 0}`} />
                <Metric label="Active Alarms" value={`${downtime?.active_alarm_count ?? 0}`} />
              </div>
              <div className="mt-4 grid gap-4 xl:grid-cols-2">
                <div className="grid gap-2">
                  {(downtime?.reasons ?? []).length === 0 && <p className="text-sm text-steel">No downtime reasons in this window.</p>}
                  {(downtime?.reasons ?? []).map((reason) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_80px_90px_80px] md:items-center" key={reason.alarm_code}>
                      <span className="font-semibold">{reason.alarm_code}</span>
                      <span className="text-steel">{reason.count} events</span>
                      <span className="text-steel">{reason.duration_minutes} min</span>
                      <span className="text-steel">{reason.active_count} active</span>
                    </div>
                  ))}
                </div>
                <div className="grid gap-2">
                  {(downtime?.machines ?? []).map((machine) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_80px_90px_80px] md:items-center" key={machine.machine_id}>
                      <span className="font-semibold">{machine.machine_id}</span>
                      <span className="text-steel">{machine.count} events</span>
                      <span className="text-steel">{machine.duration_minutes} min</span>
                      <span className="text-steel">{machine.active_count} active</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
            )}

            {activeView === "system" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Activity size={18} />
                  <h2 className="text-lg font-semibold">System Health</h2>
                </div>
                <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${healthBadgeClass(systemStatus?.overall ?? "degraded")}`}>
                  {systemStatus?.overall ?? "loading"}
                </span>
              </div>
              <div className="grid gap-4 md:grid-cols-6">
                <Metric label="MQTT" value={systemStatus?.services.mqtt ?? "loading"} />
                <Metric label="InfluxDB" value={systemStatus?.services.influxdb ?? "loading"} />
                <Metric label="PostgreSQL" value={systemStatus?.services.postgres ?? "loading"} />
                <Metric label="OPC UA" value={systemStatus?.services.opcua_adapter ?? "loading"} />
                <Metric label="WS Clients" value={`${systemStatus?.services.websocket_clients ?? 0}`} />
                <Metric label="Machines" value={`${systemStatus?.machines.filter((machine) => machine.connection_state === "online").length ?? 0}/${systemStatus?.machines.length ?? 0}`} />
              </div>
              <div className="mt-4 grid gap-2">
                {(systemStatus?.machines ?? []).map((machine) => (
                  <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_110px_120px] md:items-center" key={machine.machine_id}>
                    <span className="font-semibold">{machine.machine_id}</span>
                    <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${healthBadgeClass(machine.connection_state === "online" ? "ok" : machine.connection_state === "stale" ? "degraded" : "down")}`}>
                      {machine.connection_state}
                    </span>
                    <span className="text-steel">{machine.seconds_since_last_seen}s ago</span>
                    <span className="text-steel">{machine.machine_status}</span>
                  </div>
                ))}
              </div>
            </section>
            )}

            {activeView === "operations" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <Settings2 size={18} />
                <h2 className="text-lg font-semibold">Production Setup</h2>
              </div>
              <div className="grid gap-4 md:grid-cols-[1fr_1fr_auto_auto] md:items-end">
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Target count
                  <input
                    className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                    min="1"
                    max="10000"
                    type="number"
                    value={targetInput}
                    onChange={(event) => setTargetInput(event.target.value)}
                  />
                </label>
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Recipe
                  <select
                    className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                    value={recipeInput}
                    onChange={(event) => setRecipeInput(event.target.value)}
                  >
                    {RECIPES.map((recipe) => (
                      <option key={recipe} value={recipe}>
                        {recipe}
                      </option>
                    ))}
                  </select>
                </label>
                <CommandButton icon={<Target size={16} />} label="Set Target" onClick={() => sendCommand("set_target_count", Number(targetInput))} />
                <CommandButton icon={<Settings2 size={16} />} label="Set Recipe" onClick={() => confirmAndSend("Change recipe on this machine?", () => sendCommand("change_recipe", recipeInput))} />
              </div>
            </section>
            )}

            {activeView === "operations" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center justify-between gap-4">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={18} />
                  <h2 className="text-lg font-semibold">Active Alarm Management</h2>
                </div>
                <span className="text-sm font-medium text-steel">{alarms.length} active</span>
              </div>
              <div className="grid gap-3">
                {alarms.length === 0 && <p className="text-sm text-steel">No active machine alarms.</p>}
                {alarms.map((alarm) => (
                  <div className="grid gap-3 rounded-md border border-line px-4 py-3 md:grid-cols-[1fr_auto] md:items-center" key={alarm.alarm_id}>
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${alarmBadgeClass(alarm.severity)}`}>{alarm.severity}</span>
                        <span className="font-semibold">{alarm.code}</span>
                        <span className="text-sm text-steel">{alarm.machine_id}</span>
                        {alarm.acknowledged && <span className="rounded bg-emerald-50 px-2 py-1 text-xs font-semibold text-signal">acknowledged</span>}
                      </div>
                      <p className="mt-1 text-sm text-steel">First seen {new Date(alarm.first_seen).toLocaleTimeString()}</p>
                    </div>
                    <div className="flex gap-2">
                      <CommandButton icon={<CheckCircle2 size={16} />} label="Ack" onClick={() => sendCommandFor(alarm.machine_id, "acknowledge_alarm")} />
                      <CommandButton icon={<RotateCcw size={16} />} label="Reset" onClick={() => confirmAndSend("Reset active alarm?", () => sendCommandFor(alarm.machine_id, "reset_alarm"))} />
                    </div>
                  </div>
                ))}
              </div>
            </section>
            )}

            {activeView === "configuration" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Gauge size={18} />
                  <h2 className="text-lg font-semibold">Threshold Monitoring</h2>
                </div>
                <span className="text-sm font-medium text-steel">{thresholdAlerts.length} active</span>
              </div>
              <div className="grid gap-4 xl:grid-cols-2">
                <div className="grid gap-2">
                  {thresholdAlerts.length === 0 && <p className="text-sm text-steel">No active threshold alerts.</p>}
                  {thresholdAlerts.map((alert) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_100px_90px] md:items-center" key={alert.alert_id}>
                      <span className="font-semibold">{alert.label}</span>
                      <span className="text-steel">{alert.machine_id}</span>
                      <span className="text-steel">{alert.value} / {alert.threshold}</span>
                      <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${alarmBadgeClass(alert.severity)}`}>{alert.severity}</span>
                    </div>
                  ))}
                </div>
                <div className="grid gap-2">
                  {alertRules.map((rule) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_110px_auto] md:items-center" key={rule.rule_id}>
                      <div>
                        <p className="font-semibold">{rule.label}</p>
                        <p className="text-steel">{rule.metric} {rule.operator} {rule.threshold}</p>
                      </div>
                      <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${rule.enabled ? "bg-signal text-white" : "bg-steel text-white"}`}>
                        {rule.enabled ? "enabled" : "disabled"}
                      </span>
                      <CommandButton icon={<Settings2 size={16} />} label={rule.enabled ? "Disable" : "Enable"} onClick={() => toggleAlertRule(rule)} disabled={!canManageAlertRules} />
                    </div>
                  ))}
                </div>
              </div>
            </section>
            )}

            {activeView === "configuration" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={18} />
                  <h2 className="text-lg font-semibold">Notification Delivery</h2>
                </div>
                <span className="text-sm font-medium text-steel">{notifications.length} recent attempts</span>
              </div>
              <div className="grid gap-4 md:grid-cols-[1fr_150px_1fr_auto] md:items-end">
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Target name
                  <input className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal" value={notificationName} onChange={(event) => setNotificationName(event.target.value)} />
                </label>
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Type
                  <select className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal" value={notificationType} onChange={(event) => setNotificationType(event.target.value as "simulated" | "webhook")}>
                    <option value="simulated">simulated</option>
                    <option value="webhook">webhook</option>
                  </select>
                </label>
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Endpoint
                  <input className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal" value={notificationEndpoint} onChange={(event) => setNotificationEndpoint(event.target.value)} />
                </label>
                <CommandButton icon={<Settings2 size={16} />} label="Add Target" onClick={createNotificationTarget} disabled={!canManageAlertRules} />
              </div>
              <div className="mt-4 grid gap-4 xl:grid-cols-2">
                <div className="grid gap-2">
                  {notificationTargets.map((target) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_90px_auto] md:items-center" key={target.target_id}>
                      <span className="font-semibold">{target.name}</span>
                      <span className="text-steel">{target.target_type}</span>
                      <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${target.enabled ? "bg-signal text-white" : "bg-steel text-white"}`}>{target.enabled ? "enabled" : "disabled"}</span>
                      <CommandButton icon={<Settings2 size={16} />} label={target.enabled ? "Disable" : "Enable"} onClick={() => toggleNotificationTarget(target)} disabled={!canManageAlertRules} />
                    </div>
                  ))}
                </div>
                <div className="grid gap-2">
                  {notifications.length === 0 && <p className="text-sm text-steel">No notification attempts yet.</p>}
                  {notifications.slice(0, 6).map((notification) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_80px] md:items-center" key={notification.notification_id}>
                      <div>
                        <p className="font-semibold">{notification.message || notification.event_type}</p>
                        <p className="text-steel">{notification.target_name} | {notification.machine_id}</p>
                      </div>
                      <span className={`rounded px-2 py-1 text-xs font-semibold uppercase ${notification.status === "sent" ? "bg-signal text-white" : "bg-fault text-white"}`}>{notification.status}</span>
                      <span className="text-steel">{new Date(notification.timestamp).toLocaleTimeString()}</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
            )}

            {activeView === "analytics" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Gauge size={18} />
                  <h2 className="text-lg font-semibold">Current Shift Analytics</h2>
                </div>
                <span className="text-sm font-medium text-steel">{shift ? `${Math.round(shift.window_minutes / 60)}h window` : "Loading"}</span>
              </div>
              <div className="grid gap-4 md:grid-cols-5">
                <Metric label="Shift Output" value={(shift?.total_produced ?? 0).toLocaleString()} />
                <Metric label="Avg OEE" value={`${shift?.average_oee ?? 0}%`} />
                <Metric label="Reject Rate" value={`${shift?.reject_rate ?? 0}%`} />
                <Metric label="Downtime" value={`${shift?.total_downtime_minutes ?? 0} min`} />
                <Metric label="Shift Alarms" value={`${shift?.active_alarm_count ?? 0}`} />
              </div>
              <div className="mt-4 grid gap-2">
                {(shift?.machines ?? []).map((machine) => (
                  <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_90px_90px_90px] md:items-center" key={machine.machine_id}>
                    <span className="font-semibold">{machine.machine_id}</span>
                    <span className="text-steel">{machine.produced} units</span>
                    <span className="text-steel">{machine.oee}% OEE</span>
                    <span className="text-steel">{machine.reject_rate}% reject</span>
                    <span className="text-steel">{machine.downtime_minutes} min down</span>
                  </div>
                ))}
              </div>
            </section>
            )}

            {activeView === "analytics" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <History size={18} />
                  <h2 className="text-lg font-semibold">Production Report</h2>
                </div>
                <div className="flex flex-wrap gap-2">
                  <CommandButton icon={<Activity size={16} />} label="Run Report" onClick={loadReport} />
                  <CommandButton icon={<Download size={16} />} label="Export CSV" onClick={exportReportCsv} disabled={!report} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-[1fr_1fr_1fr]">
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Machine
                  <select
                    className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                    value={reportMachine}
                    onChange={(event) => setReportMachine(event.target.value)}
                  >
                    <option value="all">All machines</option>
                    {machines.map((machine) => (
                      <option key={machine.machine_id} value={machine.machine_id}>
                        {machine.machine_id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-2 text-sm font-medium text-steel">
                  Start
                  <input
                    className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                    type="datetime-local"
                    value={reportStart}
                    onChange={(event) => setReportStart(event.target.value)}
                  />
                </label>
                <label className="grid gap-2 text-sm font-medium text-steel">
                  End
                  <input
                    className="h-10 rounded-md border border-line bg-white px-3 text-ink outline-none focus:border-signal"
                    type="datetime-local"
                    value={reportEnd}
                    onChange={(event) => setReportEnd(event.target.value)}
                  />
                </label>
              </div>

              <div className="mt-4 grid gap-4 md:grid-cols-5">
                <Metric label="Output" value={(report?.total_produced ?? 0).toLocaleString()} />
                <Metric label="Avg OEE" value={`${report?.average_oee ?? 0}%`} />
                <Metric label="Reject Rate" value={`${report?.reject_rate ?? 0}%`} />
                <Metric label="Telemetry Down" value={`${report?.telemetry_downtime_minutes ?? 0} min`} />
                <Metric label="Alarm Down" value={`${report?.alarm_downtime_minutes ?? 0} min`} />
              </div>

              <div className="mt-4 grid gap-4 xl:grid-cols-2">
                <div className="grid gap-2">
                  {(report?.machines ?? []).length === 0 && <p className="text-sm text-steel">Run a report to compare production over a selected range.</p>}
                  {(report?.machines ?? []).map((machine) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_90px_90px_90px_90px] md:items-center" key={machine.machine_id}>
                      <span className="font-semibold">{machine.machine_id}</span>
                      <span className="text-steel">{machine.produced} units</span>
                      <span className="text-steel">{machine.oee}% OEE</span>
                      <span className="text-steel">{machine.reject_rate}% reject</span>
                      <span className="text-steel">{machine.downtime_minutes} min down</span>
                    </div>
                  ))}
                </div>
                <div className="grid gap-2">
                  {(report?.top_downtime_reasons ?? []).length === 0 && report && <p className="text-sm text-steel">No alarm downtime reasons in this report.</p>}
                  {(report?.top_downtime_reasons ?? []).map((reason) => (
                    <div className="grid gap-3 rounded border border-line px-3 py-2 text-sm md:grid-cols-[1fr_80px_90px_80px] md:items-center" key={reason.alarm_code}>
                      <span className="font-semibold">{reason.alarm_code}</span>
                      <span className="text-steel">{reason.count} events</span>
                      <span className="text-steel">{reason.duration_minutes} min</span>
                      <span className="text-steel">{reason.active_count} active</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
            )}

            {activeView === "analytics" && (
            <section className="grid gap-5 xl:grid-cols-2">
              <ChartPanel title="Production Trend">
                <Plot
                  data={[
                    { x: timestamps, y: history.map((row) => row.production_count), type: "scatter", mode: "lines", name: "Produced", line: { color: "#00897b" } },
                    { x: timestamps, y: history.map((row) => row.reject_count), type: "scatter", mode: "lines", name: "Rejects", line: { color: "#c62828" } },
                  ]}
                  layout={plotLayout("Units")}
                  config={{ displayModeBar: false, responsive: true }}
                  className="h-full w-full"
                />
              </ChartPanel>
              <ChartPanel title="Process Conditions">
                <Plot
                  data={[
                    { x: timestamps, y: history.map((row) => row.temperature), type: "scatter", mode: "lines", name: "Temp", line: { color: "#d9822b" } },
                    { x: timestamps, y: history.map((row) => row.pressure), type: "scatter", mode: "lines", name: "Pressure", yaxis: "y2", line: { color: "#516171" } },
                  ]}
                  layout={{ ...plotLayout("Temperature"), yaxis2: { overlaying: "y", side: "right", title: { text: "Pressure" } } }}
                  config={{ displayModeBar: false, responsive: true }}
                  className="h-full w-full"
                />
              </ChartPanel>
            </section>
            )}

            {activeView === "system" && (
            <section className="rounded-md border border-line bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <History size={18} />
                <h2 className="text-lg font-semibold">Recent Command And Event Log</h2>
              </div>
              <div className="grid gap-2">
                {audit.length === 0 && <p className="text-sm text-steel">Waiting for commands or events.</p>}
                {audit.map((entry, index) => (
                  <div className="rounded border border-line px-3 py-2 text-sm" key={index}>
                    <span className="font-medium">{String(entry.command ?? entry.message ?? entry.event_type ?? "event")}</span>
                    <span className="ml-2 text-steel">{String(entry.machine_id ?? "")}</span>
                  </div>
                ))}
              </div>
            </section>
            )}
          </div>
        )}
      </section>
        </>
      )}
    </main>
  );
}

function Kpi({ icon, label, value, tone = "normal" }: { icon: React.ReactNode; label: string; value: string; tone?: "normal" | "fault" }) {
  return (
    <div className="rounded-md border border-line bg-white p-4 shadow-sm">
      <div className={`mb-3 inline-flex rounded p-2 ${tone === "fault" ? "bg-red-50 text-fault" : "bg-emerald-50 text-signal"}`}>{icon}</div>
      <p className="text-sm text-steel">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-line bg-panel p-3">
      <p className="text-xs font-semibold uppercase text-steel">{label}</p>
      <p className="mt-1 text-lg font-semibold capitalize">{value}</p>
    </div>
  );
}

function CommandButton({ icon, label, onClick, disabled = false }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button className="inline-flex h-10 items-center gap-2 rounded-md border border-line bg-white px-3 text-sm font-semibold hover:border-signal disabled:cursor-not-allowed disabled:opacity-50" disabled={disabled} onClick={onClick}>
      {icon}
      {label}
    </button>
  );
}

function alarmBadgeClass(severity: Alarm["severity"]) {
  if (severity === "critical") return "bg-fault text-white";
  if (severity === "warning") return "bg-warning text-white";
  return "bg-steel text-white";
}

function healthBadgeClass(status: "ok" | "degraded" | "down") {
  if (status === "ok") return "bg-signal text-white";
  if (status === "degraded") return "bg-warning text-white";
  return "bg-fault text-white";
}

function ChartPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="h-[360px] rounded-md border border-line bg-white p-4 shadow-sm">
      <h2 className="mb-2 text-lg font-semibold">{title}</h2>
      <div className="h-[300px]">{children}</div>
    </section>
  );
}

function plotLayout(yTitle: string): Partial<Layout> {
  return {
    autosize: true,
    margin: { t: 10, r: 20, b: 35, l: 45 },
    paper_bgcolor: "white",
    plot_bgcolor: "white",
    font: { family: "Inter, system-ui, sans-serif", color: "#17202a" },
    yaxis: { title: { text: yTitle }, gridcolor: "#edf1f4" },
    xaxis: { gridcolor: "#edf1f4" },
    legend: { orientation: "h" },
  };
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
