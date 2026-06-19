# Home Energy Optimizer

A personal web app that decides **when to charge your EV** by combining real-time home solar production, home battery state, and time-of-use electricity rates. A built-in AI chat panel lets you ask natural-language questions about your energy system — covering uploaded documents, historical DynamoDB readings, and automatically detected anomalies.

Data flows hourly from an Enphase inverter into DynamoDB. The frontend visualises today's solar production and recommends the cheapest charging window — factoring in whether you can run primarily on solar, stored battery energy, or grid power. A chat router (Amazon Nova Lite) classifies each question and delegates to one of three specialised Lambda handlers: document RAG (pgvector + Bedrock), data queries (structured DynamoDB), or anomaly summaries (rule-based detection + LLM narrative).

**Hardware:** Enphase inverter · 4 × Enphase IQ Battery 5P (20 kWh) · 2026 BMW iX 45 (76.6 kWh, 7.2–11 kW AC)
**Utility:** PG&E / MCE, rate E-TOU-C (see [Rate schedule](#rate-schedule) below)

---

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18 + Vite + Tailwind CSS + Recharts |
| Backend | Python 3.12 AWS Lambda functions |
| API | AWS API Gateway (Lambda proxy) |
| Database | DynamoDB (time-series readings), Neon serverless Postgres + pgvector (document embeddings) |
| AI / embeddings | AWS Bedrock — Amazon Titan Text Embeddings V2 + Amazon Nova Lite |
| Document storage | S3 (PDF upload triggers auto-ingest) |
| Scheduler | EventBridge (hourly cron → ingest Lambda) |
| Secrets | AWS SSM Parameter Store (SecureString) |
| Infrastructure | AWS CDK (TypeScript) |

---

## Project structure

```
solar_ev/
├── frontend/                   # React app (Vite + Tailwind + Recharts)
│   └── src/
│       ├── App.tsx             # Main dashboard component
│       ├── api/
│       │   ├── solar.ts        # Typed API client (solar + recommendation)
│       │   └── chat.ts         # POST /chat API client
│       └── components/
│           └── ChatPanel.tsx   # AI chat UI with source citations
├── backend/
│   ├── functions/
│   │   ├── solar_data/         # GET /solar/today
│   │   ├── recommendation/     # GET /recommendation
│   │   ├── ingest/             # Hourly cron — fetches Enphase + writes DynamoDB + anomaly detection
│   │   ├── doc_ingest/         # S3 trigger — PDF → chunks → Bedrock → pgvector
│   │   ├── rag_query/          # POST /chat route: documents — pgvector retrieval → Nova Lite
│   │   ├── data_query/         # POST /chat route: data — NL → intent → DynamoDB → response
│   │   ├── anomaly_query/      # POST /chat route: anomalies — fetch + Nova Lite narrative
│   │   └── chat/               # POST /chat — Nova Lite classifier, routes to above 3
│   ├── shared/                 # Shared Python utilities (api_response, neon, etc.)
│   ├── tests/                  # Unit tests (pytest + moto)
│   └── local_server.py         # Zero-dependency local dev server (port 3001)
└── infra/                      # AWS CDK stack — all resources in one file
    └── lib/solar-ev-stack.ts
```

---

## Rate schedule

The app uses the **PG&E E-TOU-C** time-of-use rate (Pacific time). All hours in a day fall into one of three tiers:

| Period | Hours | Rate | Colour |
|--------|-------|------|--------|
| Super Off-Peak | 09:00 – 14:00 | ~$0.17/kWh | Green |
| Off-Peak | 00:00 – 09:00, 14:00 – 16:00, 21:00 – 24:00 | ~$0.28/kWh | Amber |
| Peak | 16:00 – 21:00 | ~$0.48/kWh | Red |

> Rates are simplified averages and do not include fixed charges or seasonal variation. Update `HOURLY_RATES` and `TOU_SCHEDULE` in the handler files to match your exact tariff.

### How the recommendation engine uses rates

1. **Energy needed** is calculated from your EV's current and target state of charge (SOC): `76.6 kWh × (target − current)`.
2. **Charging duration** is derived from the energy needed and the assumed charge rate (7.2 kW by default).
3. Every valid start hour is **scored**: base cost = sum of hourly rates × charge rate. Solar coverage reduces the effective cost — a window with 100% solar coverage costs 80% less than a pure-grid window at the same rate.
4. The **lowest-scoring window** is recommended.
5. A **charging source** is determined from the home battery SOC and solar coverage:
   - **Direct Solar** — window is ≥ 70% solar-covered and battery < 60%
   - **Solar + Home Battery** — ≥ 70% solar-covered and battery ≥ 60%, *or* battery ≥ 80% with ≥ 30% solar
   - **Home Battery (avoid peak)** — battery ≥ 60% and the best window falls in peak hours
   - **Grid** — all other cases, labelled with the rate period

---

## AI chat layer

The dashboard chat panel routes natural-language questions to one of three specialised backends, chosen by a lightweight Nova Lite classifier:

| Route | Trigger words / intent | Handler |
|-------|----------------------|---------|
| **documents** | How something works, specs, rate rules, equipment policies | `rag_query` |
| **data** | Specific numbers, history, averages from your system | `data_query` |
| **anomalies** | Problems, alerts, unusual behaviour, issues | `anomaly_query` |

Off-topic questions are blocked before generation: if the best-matching document chunk has a cosine distance > 0.7, the `rag_query` handler returns a guardrail message instead of calling Nova Lite.

### Route 1 — document RAG (`rag_query`)

1. Upload a PDF to the S3 documents bucket (printed as `DocumentsBucketName` in CDK deploy output).
2. An S3 event triggers `doc_ingest`, which splits the PDF into overlapping 500-word chunks, embeds each with Bedrock Titan Text Embeddings V2, and stores them in Neon pgvector with an HNSW cosine index.
3. At query time, `rag_query` embeds the question, retrieves the 5 nearest chunks, and generates an answer with Nova Lite. Each response includes source citations (document name + page number).

```bash
# Upload a document
aws s3 cp your-rate-schedule.pdf s3://<DocumentsBucketName>/your-rate-schedule.pdf
```

Re-uploading the same key re-ingests (old chunks deleted first).

### Route 2 — data queries (`data_query`)

Handles questions about historical energy readings stored in DynamoDB:

- "How much did I produce yesterday?" → total daily `energy_wh`
- "What was my average battery SOC last week?" → averaged `battery_soc_pct`
- "Which day had the most solar this month?" → maximum daily production

Nova Lite extracts a structured intent (`metric`, `aggregation`, `start_date`, `end_date`) from the question. Python then executes the query against DynamoDB and formats a plain-English response. The LLM never touches DynamoDB directly — only the extracted intent drives the query.

### Route 3 — anomaly detection (`anomaly_query`)

The hourly `ingest` Lambda runs rule-based anomaly detection after each Enphase snapshot and writes flagged events to the `solar-ev-anomalies` DynamoDB table (30-day TTL):

| Anomaly | Condition |
|---------|-----------|
| `no_production` (high severity) | Zero power output during solar peak (10:00–14:00) with < 50% cloud cover |
| `low_production` (medium severity) | < 1 kW during solar peak with < 20% cloud cover |
| `battery_critically_low` (medium severity) | Battery SOC < 10% at any time |

When you ask about problems or anomalies, `anomaly_query` fetches recent events from DynamoDB and has Nova Lite write a natural-language summary. If no anomalies exist, the system confirms everything looks healthy.

**Local dev:** Set `NEON_CONNECTION_STRING` in `backend/.env` to skip SSM lookup. All three sub-handlers load automatically in the local server; the `/chat` route uses `_classify` directly (no boto3 Lambda.invoke needed). Note: pg8000 must be installed (`pip install pg8000 pypdf`) for `rag_query` to load.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | 20+ | `brew install node` |
| Python | 3.12+ | `brew install python@3.12` |
| AWS CLI | 2.x | `brew install awscli` |
| AWS CDK | 2.x | `npm install -g aws-cdk` |

---

## 1. Clone & install dependencies

```bash
git clone <your-repo>
cd solar_ev

# CDK infrastructure
cd infra && npm install && cd ..

# React frontend
cd frontend && npm install && cd ..

# Python test/dev dependencies (optional, for running tests)
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cd ..
```

---

## 2. Run locally (no AWS needed)

Without any configuration, both APIs fall back to **mock data** — useful for developing the frontend without a live Enphase system.

```bash
# Terminal 1 — local API server (port 3001)
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install boto3
python3 local_server.py

# Terminal 2 — React dev server (port 5173)
cd frontend
npm run dev
```

Open http://localhost:5173. Vite proxies `/api/*` to `localhost:3001`.

### Using real Enphase data locally

Create `backend/.env` (never commit this file):

```bash
ENPHASE_SYSTEM_ID=your_system_id
ENPHASE_API_KEY=your_api_key
ENPHASE_ACCESS_TOKEN=your_oauth_access_token
ENPHASE_REFRESH_TOKEN=your_oauth_refresh_token
ENPHASE_CLIENT_ID=your_oauth_client_id
ENPHASE_CLIENT_SECRET=your_oauth_client_secret

# Optional: OpenWeatherMap current conditions (free tier API key)
OPENWEATHER_API_KEY=your_owm_api_key
LOCATION_LAT=37.8216
LOCATION_LON=-121.9999

# Optional: point at a real DynamoDB table (requires AWS credentials)
ENERGY_TABLE=solar-ev-energy-readings
```

The local server loads this file automatically at startup. When `ENPHASE_SYSTEM_ID` and `ENERGY_TABLE` are set and the table has data, the API returns real readings instead of mock data.

---

## 3. Run the tests

### Backend (Python — pytest + moto)

```bash
cd backend
source .venv/bin/activate   # if not already active
pytest tests/ -v
```

All AWS calls are mocked with `moto` — no credentials or live infrastructure needed.

Covered: `solar_data`, `recommendation`, `ingest`, `doc_ingest`, `rag_query`, `data_query`, `chat`, and `anomaly_query` Lambda handlers — including SSM helpers, OAuth token refresh, Enphase API calls, battery SOC, OpenWeatherMap fetch, curtailment alert logic, PDF chunking with page tracking, pgvector retrieval, Bedrock generation, intent extraction and DynamoDB aggregation logic, chat routing (all three routes + fallback), anomaly detection rules (boundary conditions, peak/off-peak, weather data absent), and anomaly query summarisation.

### Frontend (TypeScript — Vitest + Testing Library)

```bash
cd frontend
npm test              # run all tests once
npm run test:watch    # watch mode
npm run test:coverage # with coverage report
```

Covered: API client (`fetchSolarToday`, `fetchRecommendation`), pure chart/timeline functions (`timeToHour`, `rateAtHour`, `hourToPercent`), `RecommendationCard` component, and `App` integration (loading, success, error states).

### Infrastructure (TypeScript — Jest + CDK Assertions)

```bash
cd infra
npm test
```

Covered: DynamoDB table schema and TTL, Lambda function names/runtimes/env vars/timeouts, IAM policies (SSM GetParameter/PutParameter scopes), API Gateway routes, and EventBridge schedule.

---

## 4. AWS setup (one-time)

### Configure credentials
```bash
aws configure
# Access Key ID, Secret, region (e.g. us-west-2), output format (json)
```

### Store Enphase credentials in SSM Parameter Store

The ingest Lambda reads credentials from SSM at runtime (SecureString, encrypted with the default KMS key). Create each parameter before deploying:

```bash
REGION=us-west-2   # change to your region

aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/enphase-api-key --value "YOUR_API_KEY"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/enphase-access-token --value "YOUR_ACCESS_TOKEN"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/enphase-refresh-token --value "YOUR_REFRESH_TOKEN"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/enphase-client-id --value "YOUR_CLIENT_ID"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/enphase-client-secret --value "YOUR_CLIENT_SECRET"
```

The ingest Lambda automatically refreshes the access token when it expires and writes the new token back to SSM — no manual rotation needed.

### Store OpenWeatherMap API key in SSM (optional)

The ingest Lambda fetches current weather conditions (cloud cover, temperature, condition) each hour and stores them alongside the solar snapshot. To enable it, get a free API key at [openweathermap.org](https://openweathermap.org/api) and store it:

```bash
aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/openweather-api-key --value "YOUR_OWM_API_KEY"
```

If the parameter is absent, weather fetching is silently skipped — ingest continues normally.

### Store Neon connection string in SSM (required for AI chat)

Create a free Postgres database at [neon.tech](https://neon.tech) (pgvector is pre-installed). Store the connection string:

```bash
aws ssm put-parameter --region $REGION --type SecureString \
  --name /solar-ev/neon-connection-string \
  --value 'postgresql://user:password@host/dbname?sslmode=require'
```

> Use **single quotes** around the value to prevent the shell from interpreting special characters in the password.

The `doc_ingest` Lambda creates the `document_chunks` table and HNSW index on first run — no manual schema migration needed.

### Curtailment alerts via ntfy.sh (optional)

The ingest Lambda sends a push notification when the home battery is full and solar is being curtailed. To enable it:

1. Pick a secret topic name (treat it like a password — anyone who knows it can subscribe):
   ```bash
   aws ssm put-parameter --region $REGION --type SecureString \
     --name /solar-ev/ntfy-topic --value "your-secret-topic-name"
   ```
2. Install the [ntfy app](https://ntfy.sh/) on iOS or Android and subscribe to the same topic name.

Alerts fire when: battery ≥ 95% **and** solar > 200 W **and** time is 09:00–17:00 Pacific. A 6-hour cooldown prevents repeated notifications. De-duplication state is stored in the `solar-ev-user-config` DynamoDB table.

### CDK bootstrap (one-time per account/region)
```bash
cd infra
npx cdk bootstrap
```

---

## 5. Deploy to AWS

```bash
cd infra

# Preview what will be created/changed
npx cdk diff

# Deploy Lambda + API Gateway + DynamoDB + EventBridge
npx cdk deploy

# The API Gateway URL is printed in Outputs:
#   SolarEvStack.ApiUrl = https://xxxx.execute-api.us-west-2.amazonaws.com/prod
```

After deploying, point the frontend at the live API by creating `frontend/.env.local`:

```
VITE_API_URL=https://xxxx.execute-api.us-west-2.amazonaws.com/prod
```

Then run `npm run dev` (dev mode) or `npm run build` (production build).

---

## 6. How data flows

```
EventBridge (hourly)
        │
        ▼
  ingest Lambda
  ┌──────────────────────────────────┐
  │ 1. Read credentials from SSM     │
  │ 2. GET /api/v4/systems/          │
  │    {id}/summary  (Enphase)       │
  │ 3. GET .../telemetry/battery     │
  │ 4. GET openweathermap.org/       │
  │    data/2.5/weather  (OWM)       │
  │ 5. Write snapshot to DynamoDB    │
  │    with 90-day TTL               │
  │ 6. Rule-based anomaly detection  │
  │    → write to anomalies table    │
  │ 7. If battery ≥ 95% + solar      │
  │    curtailed → POST ntfy.sh alert│
  └──────────────────────────────────┘
        │                    │
        ▼                    ▼
  DynamoDB:           DynamoDB:
  solar-ev-energy-    solar-ev-anomalies
  readings            PK: system_id
  PK: enphase-{id}    SK: timestamp
  SK: timestamp       30-day TTL
  90-day TTL
        │
        ├──────────────────────┐
        ▼                      ▼
  solar_data Lambda      recommendation Lambda
  GET /solar/today        GET /recommendation
  Returns 24 hourly       Scores every start hour,
  production slots        returns best charging window
  (Pacific time) +        + charging source decision
  TOU schedule            (solar / battery / grid)
        │                      │
        └──────────┬───────────┘
                   ▼
            API Gateway
                   ▼
           React frontend
                   ▼
           POST /chat
                   │
            chat Lambda (Nova Lite classifier)
           ┌───────┼───────────┐
           ▼       ▼           ▼
      rag_query  data_query  anomaly_query
      (documents) (data)     (anomalies)
      pgvector   DynamoDB    anomalies
      + Bedrock  intent→SQL  table + Bedrock
```

### UTC vs. Pacific time

Enphase reports `energy_today` as a cumulative daily total that **resets at Pacific midnight**. A single Pacific calendar day spans two UTC dates (e.g. Pacific 2026-03-10 runs from UTC 08:00 on Mar 10 through UTC 07:59 on Mar 11).

The ingest Lambda stores Enphase's `summary_date` field (local date) alongside the UTC timestamp. Query handlers fetch rows spanning both UTC dates and filter by `summary_date` to get the correct Pacific-day slice. Hourly buckets in all API responses use Pacific local hours so they align with the TOU schedule.

---

## 7. Useful commands

```bash
# CDK
cd infra
npx cdk synth          # generate CloudFormation template (no deploy)
npx cdk diff           # compare deployed vs local
npx cdk deploy         # deploy / update
npx cdk destroy        # tear everything down

# Frontend
cd frontend
npm run dev            # local dev server (http://localhost:5173)
npm run build          # production build → dist/
npm run typecheck      # TypeScript check without building

# Backend tests (pytest + moto)
cd backend
pytest tests/ -v                     # all tests
pytest tests/test_recommendation.py  # single file
pytest tests/ --cov=functions        # with coverage

# Frontend tests (Vitest + Testing Library)
cd frontend
npm test                             # run once
npm run test:watch                   # watch mode
npm run test:coverage                # with coverage

# CDK infrastructure tests (Jest + CDK Assertions)
cd infra
npm test

# Quick Lambda smoke test (no server needed)
cd backend
python3 -c "
import json, sys
sys.path.insert(0, 'functions/solar_data')
import handler
print(json.dumps(handler.lambda_handler({}, None), indent=2))
"
```

---

## What's next

| Feature | Where to start |
|---------|---------------|
| Weather-adjusted solar forecast | Use `cloud_cover_pct` already stored per hour — adjust solar estimate in `recommendation` handler |
| Fetch battery SOC from Enphase API | Update `ingest` handler to call `/api/v4/systems/{id}/telemetry/battery`; feed real SOC into recommendation |
| User config (custom charge rate, schedule prefs) | `backend/functions/recommendation/handler.py` + `solar-ev-user-config` table |
| EV charger scheduling | Enphase EVSE API requires a higher API tier; alternative: Home Assistant integration |
| Host frontend on S3 + CloudFront | Add `S3Bucket` + `CloudFrontDistribution` to CDK stack |
| Richer anomaly rules | Extend `_detect_anomalies` in `ingest/handler.py` — e.g. grid export when EV could be charging, unexpected overnight battery drain |
