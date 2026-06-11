# CogniFactory Backend API

**Base URL (production):** `http://20.51.200.142:5000`
**CORS:** open for all origins on `/api/*` — you can call from any frontend without proxy.

This doc covers everything a frontend dev needs to:
1. Pull the list of sensors from the database.
2. Wire each sensor card / icon to a real DB sensor.
3. Add an **"Analyser avec IA"** button per sensor that returns a French diagnostic report.

If you only have 30 seconds, jump to [§4 Quick recipe](#4-quick-recipe-list--per-sensor-ai-button).

---

## 1. Health check

```http
GET /healthz
```

```json
{ "model": "qwen2.5vl", "status": "ok" }
```

Use this to verify the backend is reachable before debugging anything else.

---

## 2. Sensors

### 2.1 List all sensors

```http
GET /api/sensors
```

Returns the sensors the dashboard is currently scoped to (configurable via the `DASHBOARD_FACTORY_ID` env var on the backend; default = `13` = HA_Factory).

**Response — array of sensor objects:**

```json
[
  {
    "id": 88,
    "device_id": 66,
    "attribute_id": 31,
    "name": "Ora_Humidity (Humidity)",
    "type": "Humidity",
    "unit": "%",
    "location": "Souse",
    "factory_id": "13",
    "status": "Actif",
    "last_value": 65.73,
    "last_value_raw": "65.73",
    "last_update": "2026-03-11T02:38:34.124000+00:00",
    "lat": null,
    "lon": null,
    "battery_level": null,
    "min_threshold": null,
    "max_threshold": null
  },
  ...
]
```

**Use this**:
- `id` → pass this to **every other sensor-specific endpoint**. This is the canonical sensor identifier.
- `name`, `type`, `unit`, `location` → display on the card.
- `status` → `"Actif"` or `"Inactif"` (derived: have we seen events in the last 365 days; tunable via `DASHBOARD_ACTIVE_WINDOW_HOURS`).
- `last_value` → already converted to a number when possible. Falls back to `last_value_raw` (string) for non-numeric events like `"fire_detected"`.

**Fields you can ignore for now**: `lat`, `lon`, `battery_level`, `min_threshold`, `max_threshold` — always `null` because the new Postgres schema doesn't carry them.

### 2.2 Sensor history (for charts)

```http
GET /api/sensor-history/:id
```

Returns the most recent 50 events for the given sensor, **oldest first** so you can plot directly.

```json
[
  { "timestamp": "2026-03-11T02:31:05.806000+00:00", "value": "65.71" },
  { "timestamp": "2026-03-11T02:31:14.978000+00:00", "value": "65.71" },
  ...
]
```

`value` is a **string** (events table is polymorphic). `parseFloat()` on the frontend if you need a number.

### 2.3 Dashboard stats / aggregates

```http
GET /api/stats
```

```json
{
  "total_sensors": 4,
  "active_sensors": 4,
  "alerts": 0,
  "by_type":   [ { "name": "Temperature", "value": 1 }, ... ],
  "by_status": [ { "name": "Actif", "value": 4 } ],
  "sensor_trends": [
    {
      "id": 87,
      "name": "Ora_Temperature (Temperature)",
      "latest_value": 20.59,
      "data_points_count": 30,
      "data": [
        { "time": "00:33", "timestamp": "...", "value": 20.6 },
        ...
      ]
    },
    ...
  ]
}
```

Use `total_sensors / active_sensors / alerts` for KPI cards, `by_type` / `by_status` for pie charts, `sensor_trends[*].data` for the per-sensor mini-charts (last 30 points, chronological).

### 2.4 Activity log

```http
GET /api/activity-log
```

Last 10 alerts merged from `alert_historic` + `abnormal_behavior`. Returns `[]` when empty.

---

## 3. AI diagnostic for a sensor

This is the endpoint that powers the **"Analyser avec IA"** button.

```http
POST /api/diagnose-sensor
Content-Type: application/json

{ "sensor_id": 88 }
```

### Identifier flexibility — you don't have to know our `sensor_id`

If your frontend stores sensors by `device_id`, `attribute_id`, or a name, the endpoint accepts any of these (first non-empty wins):

| Payload | Resolution | Notes |
|---|---|---|
| `{ "sensor_id": 88 }` | direct | canonical = `device_attributes.id` from `/api/sensors` |
| `{ "device_id": 66, "attribute_id": 31 }` | exact | unambiguous |
| `{ "iddevice": 66, "idattribute": 31 }` | exact | snake_case alias (Postgres column names) |
| `{ "device_id": 66 }` | unique only | resolves only if device 66 has exactly one attribute |
| `{ "attribute_id": 31 }` | unique only | resolves only if exactly one device produces attribute 31 |
| `{ "name": "Ora_Humidity" }` | unique only | matches `device.name` via ILIKE; single match required |

Anything else returns a **404 with a hint**:

```json
{
  "error": "Capteur non trouvé.",
  "received": { "idattribute": 34 },
  "hint": "Send any of: {sensor_id} (= device_attributes.id from GET /api/sensors), {device_id, attribute_id}, or {name} ..."
}
```

If you get a 404 with `received: {idattribute: <some_id>}` it means your frontend's `idattribute` doesn't map to any `(device, attribute)` pair in the Postgres DB. Use `GET /api/sensors/lookup` (see §3.1) to translate.

### 3.1 `GET /api/sensors/lookup` — translate any ID to the canonical `sensor_id`

Use this once when your frontend mounts, to map each of your sensor cards to the backend's `sensor_id`. Then send that `sensor_id` to `/api/diagnose-sensor` from then on.

```bash
# By device name
curl 'http://20.51.200.142:5000/api/sensors/lookup?name=Ora_Carbon-Dioxide'

# By device_id + attribute_id pair
curl 'http://20.51.200.142:5000/api/sensors/lookup?device_id=79&attribute_id=29'

# By attribute_id alone (only works if one device produces that attribute)
curl 'http://20.51.200.142:5000/api/sensors/lookup?attribute_id=31'
```

```json
{
  "sensor_id": 90,
  "device_id": 79,
  "attribute_id": 29,
  "device_name": "Ora_Carbon-Dioxide",
  "attribute": "co2",
  "unit": "%PPm",
  "location": "front",
  "factory_id": "13"
}
```

Returns **404 + hint** if no match.

### 3.2 What the endpoint returns on success

The backend looks up the sensor, pulls the last 20 events, computes avg/min/max, and asks Ollama (`qwen2.5vl`) for a structured French report.

**Response:**

```json
{ "diagnosis": "### Diagnostic détaillé du capteur Ora_Humidity\n\n#### 1. ..." }
```

The text is **Markdown** — render with `react-markdown` / `marked` / `markdown-it` etc., or just `<pre style={{whiteSpace:"pre-wrap"}}>{diagnosis}</pre>`.

**Errors:**

| Code | Body | When |
|---|---|---|
| 400 | `{"error":"Erreur: sensor_id manquant."}` | Body missing `sensor_id` |
| 400 | `{"error":"Aucune donnée disponible..."}` | No events for that sensor |
| 404 | `{"error":"Capteur non trouvé."}` | `sensor_id` doesn't exist |
| 500 | `{"error":"..."}` | Ollama down / DB error |

**Timing:**

- First call after the VM is idle: **30–60 s** (model loads into RAM).
- Subsequent calls: **5–15 s**.
- **Always set a client timeout ≥ 120 s.**

```js
// axios
axios.post(url, body, { timeout: 120_000 })

// fetch (use AbortController)
const ctrl = new AbortController();
const t = setTimeout(() => ctrl.abort(), 120_000);
fetch(url, { method: "POST", body, signal: ctrl.signal })
  .finally(() => clearTimeout(t));
```

---

## 3.3 If your frontend already has its own sensor list

You don't need to abandon your data model — just translate once, when your component mounts, and cache the mapping.

```ts
type MySensor = { id: number; name: string; device_id?: number; attribute_id?: number };

// Once per mount: build a map from "my sensor id" → "backend sensor_id"
async function buildBackendIdMap(mySensors: MySensor[]) {
  const map = new Map<number, number>();
  for (const s of mySensors) {
    const params = new URLSearchParams();
    if (s.device_id && s.attribute_id) {
      params.set("device_id", String(s.device_id));
      params.set("attribute_id", String(s.attribute_id));
    } else {
      params.set("name", s.name); // ILIKE match
    }
    const r = await fetch(`http://20.51.200.142:5000/api/sensors/lookup?${params}`);
    if (r.ok) {
      const { sensor_id } = await r.json();
      map.set(s.id, sensor_id);
    } else {
      console.warn("[lookup] no backend match for", s, await r.json());
    }
  }
  return map;
}

// On "Analyser avec IA" click:
async function onAnalyseClick(mySensor: MySensor, map: Map<number, number>) {
  const sensorId = map.get(mySensor.id);
  if (!sensorId) throw new Error("Pas de capteur correspondant dans le backend");
  const r = await fetch("http://20.51.200.142:5000/api/diagnose-sensor", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sensor_id: sensorId }),
  });
  if (!r.ok) throw new Error((await r.json()).error);
  return (await r.json()).diagnosis as string;
}
```

If you'd rather skip the prebuild step, you can also send `name` / `device_id+attribute_id` directly to `/api/diagnose-sensor` and let the backend resolve each time — costs one extra Postgres lookup per click (~50 ms).

---

## 4. Quick recipe — list + per-sensor AI button

Plain JS (paste anywhere — React, Vue, Svelte, Angular, vanilla):

```js
const API = "http://20.51.200.142:5000";

export async function fetchSensors() {
  const r = await fetch(`${API}/api/sensors`);
  if (!r.ok) throw new Error("fetchSensors failed");
  return r.json();          // array of sensor objects (see §2.1)
}

export async function diagnoseSensor(sensorId) {
  const r = await fetch(`${API}/api/diagnose-sensor`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sensor_id: sensorId }),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "diagnose failed");
  return data.diagnosis;    // markdown string
}
```

### Minimal React component

```jsx
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown"; // optional
import { fetchSensors, diagnoseSensor } from "./api";

export default function SensorList() {
  const [sensors, setSensors] = useState([]);
  const [busyId, setBusyId] = useState(null);
  const [reports, setReports] = useState({});       // { [sensorId]: markdown }
  const [errors, setErrors]   = useState({});

  useEffect(() => { fetchSensors().then(setSensors); }, []);

  const runDiagnose = async (sensorId) => {
    setBusyId(sensorId);
    setErrors(e => ({ ...e, [sensorId]: null }));
    try {
      const md = await diagnoseSensor(sensorId);
      setReports(r => ({ ...r, [sensorId]: md }));
    } catch (err) {
      setErrors(e => ({ ...e, [sensorId]: err.message }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <ul>
      {sensors.map(s => (
        <li key={s.id}>
          <strong>{s.name}</strong> — {s.last_value ?? "—"} {s.unit ?? ""} ({s.status})

          <button
            disabled={busyId === s.id}
            onClick={() => runDiagnose(s.id)}
          >
            {busyId === s.id ? "🤖 Analyse…" : "🤖 Rapport IA"}
          </button>

          {errors[s.id] && <p style={{ color: "crimson" }}>{errors[s.id]}</p>}
          {reports[s.id] && <ReactMarkdown>{reports[s.id]}</ReactMarkdown>}
        </li>
      ))}
    </ul>
  );
}
```

That's the complete loop: list → bind button → call AI → render markdown.

---

## 4½. Production agent — NL→SQL chat against your own Postgres DB

Lets a user ask a question in plain French about production data; the backend introspects the live schema of a Postgres DB they supply, asks the LLM to write a SQL `SELECT`, executes it read-only, then asks the LLM to summarise the rows.

### `POST /api/test-db-connection`

Quick credential check before opening the chat.

```http
POST /api/test-db-connection
Content-Type: application/json

{
  "db_config": {
    "host": "4.251.192.31",
    "port": 5432,
    "user": "postgres",
    "password": "...",
    "database": "oranextdb",
    "sslmode": "prefer"      // optional
  }
}
```

**Success → 200:**

```json
{
  "ok": true,
  "database": "oranextdb",
  "user": "postgres",
  "version": "PostgreSQL 16.10 (Ubuntu 16.10-...)",
  "table_count": 49
}
```

**Failure → 400:** `{ "ok": false, "error": "Connexion échouée: ..." }`

### `POST /api/ask-production`

```http
POST /api/ask-production
Content-Type: application/json

{
  "question": "Combien de commandes sont en attente ?",
  "db_config": { ...same shape as above }
}
```

**Pipeline (per request):**
1. Open a **read-only** Postgres connection with `statement_timeout = 15s`.
2. Introspect tables/columns/FKs from `information_schema` (cached 5 min per host).
3. Send schema + question to Ollama (`qwen2.5vl`) → expect a `SELECT`.
4. Safety filter: only `SELECT`/`WITH`; no `INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|…`; one statement only; auto-add `LIMIT 200`.
5. Execute. **On Postgres error → one automatic retry** with the error fed back to the LLM.
6. Send result rows (top 40, ≤ 6 KB) to Ollama → French answer.

**Success → 200:**

```json
{
  "answer": "Il y a 5 commandes en attente, créées entre le 15 et le 17 février 2026.",
  "sql": "SELECT order_no, status, created_at FROM orders WHERE status='pending' ORDER BY created_at DESC LIMIT 200",
  "columns": ["order_no", "status", "created_at"],
  "rows": [ { "order_no": "...", "status": "pending", "created_at": "2026-02-17T..." }, ... ],
  "row_count": 5,
  "model": "qwen2.5vl"
}
```

**Failure → 400:**

```json
{
  "error": "Erreur SQL: column \"status\" does not exist",
  "sql": "SELECT ... WHERE status = ...",
  "attempts": [ "...first try...", "...retry..." ]
}
```

### ⚠ Model-quality caveat — read this before scaling usage

The current Ollama model on the VM is **`qwen2.5vl`** — a **vision-language** model, not a SQL specialist. It works for simple/factual questions ("how many rows where X = Y?") but hallucinates column names on schemas that have unusual naming.

For real production usage, pull a code-tuned model on the VM and switch:

```bash
# On the VM (saves ~5 GB on disk — check 'df -h /' first)
ollama pull qwen2.5-coder:7b
# Then update /etc/systemd/system/cognifactory.service:
#   Environment=OLLAMA_MODEL=qwen2.5-coder:7b
sudo systemctl daemon-reload && sudo systemctl restart cognifactory
```

Other good options if you have disk: `sqlcoder:7b`, `codellama:13b-instruct`, `mistral:7b-instruct`.

### Security model

- Connection is opened with `SET TRANSACTION READ ONLY` + `statement_timeout=15s` — even if the LLM produced something destructive, Postgres would refuse it.
- The Python-side filter rejects anything that isn't a pure `SELECT`/`WITH` (no `;`, no DDL/DML keywords).
- The DB password travels **client → backend → Postgres** as plaintext (HTTP, no HTTPS yet). Use a **dedicated, read-only Postgres role** for this — don't give it the `postgres` superuser password in real deployments. Example:

  ```sql
  CREATE ROLE chat_reader LOGIN PASSWORD '...';
  GRANT CONNECT ON DATABASE oranextdb TO chat_reader;
  GRANT USAGE ON SCHEMA public TO chat_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO chat_reader;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chat_reader;
  ```

- The frontend stores **everything except the password** in `localStorage` (key `cogni.productionDb.v1`). Password is required every session.

### Frontend integration (already done in the React app)

The Assistant page at the CogniFactory frontend now has a **"Mode Production"** toggle. When ON, a DB config form appears (host / port / user / password / database) with a **"Tester la connexion"** button. Each AI reply also shows a collapsible **"Voir le SQL"** revealing the generated query — useful for transparency and debugging.

To wire this into another frontend (e.g. your other app), call `/api/test-db-connection` to validate on form-submit, then `/api/ask-production` with the same `db_config` plus the user question.

---

## 5. Other useful endpoints

These exist on the same backend; use only if relevant:

| Endpoint | What it does |
|---|---|
| `GET  /api/videos` | List demo videos on the server (returns `[{ name, path, size }]`) |
| `GET  /videos/<filename>` | Stream a video file directly (works in `<video src=…>`) |
| `POST /api/analyze-video-screenshot` | Send a JPEG frame as multipart `frame_image` → Ollama vision (qwen2.5vl) returns a French scene description. 10–20 s. Used by the camera-icon button on video cards. |
| `POST /api/generate-summary` | LLM summary for a *batch* of sensors. Send `{ "sensors": [<sensor objects from /api/sensors>] }`. Returns `{ "summary": "..." }`. |
| `POST /api/ask` | Chat with the LLM. `{ "contents": [{ "parts": [{ "text": "..." }] }], "use_rag": false }` |

---

## 6. Gotchas — read once, save yourself an hour

1. **First AI request is slow (30–60 s).** Model loads into VM RAM. Show a friendly loading state ("L'IA analyse les 20 dernières mesures…"). Set timeout ≥ 120 000 ms.
2. **Don't fire diagnose-sensor on every render.** The VM Flask dev server is single-threaded — concurrent diagnoses serialize. Cache the result per sensor for a minute, or only call on explicit click.
3. **`value` from `/api/sensor-history/:id` is a string.** Cast with `parseFloat()` before plotting.
4. **`last_update` is ISO-8601 UTC.** `new Date(s.last_update)` works directly in JS.
5. **`active_sensors` counts `status === "Actif"`.** A sensor is "active" if it has events within `DASHBOARD_ACTIVE_WINDOW_HOURS` (currently 365 days because the seed data is months old). Tighten this once your IoT pipeline ingests live data.
6. **Polling.** The current dashboard polls `/api/stats` every 5 s. That's fine — Postgres queries are pooled and run < 200 ms. If you poll more aggressively, switch the backend to gunicorn first.
7. **Writes are disabled.** `POST /api/add-sensor`, `update-sensor`, `delete-sensor` all return **501**. Devices/attributes must be managed upstream in Postgres directly.
8. **Sensor identity is stable across calls.** `id` from `/api/sensors` ⇄ `sensor_id` in `/api/diagnose-sensor` and `:id` in `/api/sensor-history/:id`. They're all the same `device_attributes.id` Postgres key.

---

## 7. Frontend-dev checklist for the "sensor + AI button" feature

- [ ] `GET /api/sensors` on mount → store the array in state.
- [ ] Render one card per sensor, keyed by `sensor.id`.
- [ ] Beside each card, an **"Analyser avec IA"** button.
- [ ] On click: `POST /api/diagnose-sensor` with that sensor's `id`.
- [ ] Show a spinner while waiting (timeout 120 000 ms).
- [ ] Render the returned `diagnosis` as Markdown in a modal / expandable panel.
- [ ] Handle 400 / 404 / 500 with a user-visible error message.
- [ ] Disable the button while the request is in flight, re-enable on completion.

That's all. Ping if anything in the response shape doesn't match the UI you're building — I can tighten the endpoint without breaking the dashboard.
