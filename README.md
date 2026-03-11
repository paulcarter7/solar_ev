# Home Energy Optimizer

A personal web app that decides **when to charge your EV** by combining real-time home solar production, home battery state, and time-of-use electricity rates.

Data flows hourly from an Enphase inverter into DynamoDB. The frontend visualises today's solar production and recommends the cheapest charging window — factoring in whether you can run primarily on solar, stored battery energy, or grid power.

**Hardware:** Enphase inverter · 4 × Enphase IQ Battery 5P (20 kWh) · 2026 BMW iX 45 (76.6 kWh, 7.2–11 kW AC)
**Utility:** PG&E / MCE, rate E-TOU-C (see [Rate schedule](#rate-schedule) below)

---

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18 + Vite + Tailwind CSS + Recharts |
| Backend | Python 3.12 AWS Lambda functions |
| API | AWS API Gateway (Lambda proxy) |
| Database | DynamoDB |
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
│       └── api/solar.ts        # Typed API client
├── backend/
│   ├── functions/
│   │   ├── solar_data/         # GET /solar/today
│   │   ├── recommendation/     # GET /recommendation
│   │   └── ingest/             # Hourly cron — fetches Enphase + writes DynamoDB
│   ├── shared/                 # Shared Python utilities (api_response, etc.)
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

# Optional: point at a real DynamoDB table (requires AWS credentials)
ENERGY_TABLE=solar-ev-energy-readings
```

The local server loads this file automatically at startup. When `ENPHASE_SYSTEM_ID` and `ENERGY_TABLE` are set and the table has data, the API returns real readings instead of mock data.

---

## 3. Run the tests

```bash
cd backend
source .venv/bin/activate   # if not already active
pytest tests/ -v
```

Tests use `moto` to mock DynamoDB — no AWS credentials or live calls needed.

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
  │ 4. Write snapshot to DynamoDB    │
  │    with 90-day TTL               │
  │ 5. If battery ≥ 95% + solar      │
  │    curtailed → POST ntfy.sh alert│
  └──────────────────────────────────┘
        │
        ▼
  DynamoDB: solar-ev-energy-readings
  PK: enphase-{system_id}  SK: timestamp (UTC ISO-8601)
  Fields: energy_wh, power_w, summary_date (Pacific), battery_soc_pct
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
                   │
                   ▼
           React frontend
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

# Backend tests
cd backend
pytest tests/ -v                     # all tests
pytest tests/test_recommendation.py  # single file
pytest tests/ --cov=functions        # with coverage

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
| Weather-adjusted solar forecast | `backend/functions/ingest/handler.py` — add OpenWeatherMap fetch |
| Historical trend charts | Add date-range DynamoDB query to `solar_data`; new Recharts component |
| User config (custom charge rate, schedule prefs) | `backend/functions/recommendation/handler.py` + `solar-ev-user-config` table |
| EV charger scheduling | Enphase EVSE API requires a higher API tier; alternative: Home Assistant integration |
| Host frontend on S3 + CloudFront | Add `S3Bucket` + `CloudFrontDistribution` to CDK stack |
