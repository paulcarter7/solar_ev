# Home Energy Optimizer

A personal web app that decides **when to charge your EV** by combining home
solar production data, time-of-use electricity rates, and weather forecasts.

**Stack:** React + Vite + Tailwind · AWS Lambda (Python 3.12) · API Gateway ·
DynamoDB · EventBridge · CDK (TypeScript)

**Hardware:** Enphase inverter · 2026 BMW iX 45 · PG&E / MCE E-TOU-C

---

## Project structure

```
solar_ev/
├── frontend/          # React app (Vite + Tailwind + Recharts)
├── backend/
│   ├── functions/
│   │   ├── solar_data/     # GET /solar/today
│   │   ├── recommendation/ # GET /recommendation
│   │   └── ingest/         # Hourly EventBridge cron (Enphase + weather)
│   └── shared/             # Shared Python utilities
└── infra/             # AWS CDK stack (TypeScript)
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | 20+ | `brew install node` |
| Python | 3.12+ | `brew install python@3.12` |
| AWS CLI | 2.x | `brew install awscli` |
| AWS CDK | 2.x | `npm install -g aws-cdk` |
| (optional) AWS SAM | 1.x | `brew install aws-sam-cli` |

---

## 1. Clone & install dependencies

```bash
git clone <your-repo>
cd solar_ev

# CDK infrastructure
cd infra && npm install && cd ..

# React frontend
cd frontend && npm install && cd ..
```

---

## 2. AWS setup (one-time)

### Configure credentials
```bash
aws configure
# Enter your Access Key ID, Secret, region (us-west-2), output format (json)
```

### CDK bootstrap (one-time per account/region)
```bash
cd infra
npx cdk bootstrap
```

---

## 3. Run locally (no AWS needed)

The quickest way to see the full stack running is a tiny local Express server
that wraps the Lambda handlers directly.

```bash
# Terminal 1 — local API server (port 3001)
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install boto3   # only needed for DynamoDB calls; mock mode skips it
python3 local_server.py

# Terminal 2 — React dev server (port 5173)
cd frontend
npm run dev
```

Open http://localhost:5173 — Vite proxies `/api/*` to `localhost:3001`.

> **Tip:** If you already have the API Gateway URL from a deployed stack,
> create `frontend/.env.local` with:
> ```
> VITE_API_URL=https://xxxx.execute-api.us-west-2.amazonaws.com/prod
> ```
> and skip the local backend entirely.

---

## 4. Deploy to AWS

```bash
cd infra

# Preview what will be created
npx cdk diff

# Deploy everything (Lambda + API Gateway + DynamoDB + EventBridge)
npx cdk deploy

# The API Gateway URL is printed in Outputs:
#   SolarEvStack.ApiUrl = https://xxxx.execute-api.us-west-2.amazonaws.com/prod
```

After deploying, add the URL to `frontend/.env.local` (see step 3), then run
the React dev server or `npm run build && npm run preview` for production.

---

## 5. Configure real API keys (optional)

Right now all data is **mock**. To switch to live data:

### Enphase Enlighten API
1. Sign up at https://developer.enphase.com/
2. Create an app, note your **API key** and **system ID** (from Enlighten web app)
3. Set environment variables before deploying:
   ```bash
   export ENPHASE_API_KEY=your_key
   export ENPHASE_SYSTEM_ID=your_system_id
   cd infra && npx cdk deploy
   ```

### OpenWeatherMap
1. Sign up at https://openweathermap.org/api (free tier is fine)
2. Get your API key, then:
   ```bash
   export OPENWEATHER_API_KEY=your_key
   export LOCATION_LAT=37.7749   # your home latitude
   export LOCATION_LON=-122.4194 # your home longitude
   cd infra && npx cdk deploy
   ```

> **Production best practice:** store secrets in AWS Secrets Manager and have
> CDK/Lambda read from there instead of environment variables.

---

## 6. Useful commands

```bash
# CDK
cd infra
npx cdk synth          # synthesize CloudFormation template
npx cdk diff           # compare deployed vs local
npx cdk deploy         # deploy / update
npx cdk destroy        # tear everything down

# Frontend
cd frontend
npm run dev            # local dev server
npm run build          # production build → dist/
npm run typecheck      # TypeScript check without building

# Test a Lambda locally (no SAM needed)
cd backend
python3 -c "
import json, sys
sys.path.insert(0,'functions/solar_data')
from handler import lambda_handler
print(json.dumps(lambda_handler({}, None), indent=2))
"
```

---

## Architecture

```
                    ┌─────────────────┐
                    │  React Frontend  │
                    │  (Vite / S3 /   │
                    │   CloudFront)   │
                    └────────┬────────┘
                             │ HTTPS
                    ┌────────▼────────┐
                    │  API Gateway    │
                    │  (REST API)     │
                    └──┬─────────┬───┘
               GET /solar/today  GET /recommendation
                    │             │
          ┌─────────▼─┐     ┌────▼──────────┐
          │ solar_data │     │ recommendation │
          │  Lambda    │     │   Lambda       │
          └─────────┬─┘     └────┬──────────┘
                    │             │
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │  DynamoDB   │
                    │  Tables     │
                    │ • energy-   │
                    │   readings  │
                    │ • user-     │
                    │   config    │
                    └─────────────┘
                         ▲
              ┌──────────┴──────────┐
              │   ingest Lambda      │
              │ (hourly EventBridge) │
              │  • Enphase API       │
              │  • OpenWeatherMap    │
              └──────────────────────┘
```

---

## Iterating from here

| Feature | Where to start |
|---------|---------------|
| Live Enphase data | `backend/functions/ingest/handler.py` |
| Weather-adjusted solar forecast | `backend/functions/ingest/handler.py` |
| Historical trend charts | Add DynamoDB query to `solar_data`, new chart component |
| User config (battery %, charge rate) | `backend/functions/recommendation/handler.py` |
| Push notifications (charge now!) | New Lambda + SNS/SES topic |
| Host frontend on S3 + CloudFront | Add `S3Bucket` + `CloudFrontDistribution` to CDK stack |
| Secrets Manager for API keys | Replace env vars in CDK stack |
