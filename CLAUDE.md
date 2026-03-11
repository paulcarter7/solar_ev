# Home Energy Optimizer

## What This Is
A personal app for monitoring and optimizing home energy — solar production,
EV charging, and grid consumption. Built as a learning project for AWS
serverless and React.

## Hardware
- **Solar**: 15 × Enphase IQ8AC microinverters, 6.6 kW system
- **Home battery**: 4 × Enphase IQ Battery 5P = 20 kWh, max 14.16 kW charge/discharge
- **EV**: 2026 BMW iX 45 — 76.6 kWh usable, 7.2–11 kW AC charging
- **EV charger**: Enphase IQ EVSE 50R (serial 482535021339, 1.44–9.6 kW range), managed by Enphase app
- **Utility**: PG&E / MCE, rate plan E-TOU-C

## Stack
- **Backend**: AWS Lambda (Python 3.12), DynamoDB, EventBridge (hourly cron)
- **Frontend**: React 18 + Vite + Tailwind 3 + Recharts
- **Infrastructure**: AWS CDK (TypeScript) — no EC2, no always-on servers
- **Enphase API**: Enlighten v4 (`https://api.enphaseenergy.com/api/v4`)
  — credentials in SSM Parameter Store (api-key, access-token, client-id, client-secret)

## Architecture Rules
- Lambda functions stay small and single-purpose — one responsibility per function
- Never hardcode ARNs, table names, or resource IDs — use environment variables
- DynamoDB access goes through a dedicated data layer, not inline in handlers
- EventBridge events must have a defined schema — document any new event types
- Keep Lambda cold-start time in mind: minimize imports, avoid heavy dependencies

## Data & Domain
- Solar data source: Enphase API (inverter + IQ Battery 5P × 4 = 20 kWh storage)
- EV: 2026 BMW iX 45 (76.6 kWh usable, 7.2–11 kW AC charging) — no charger API yet
- Utility: PG&E / MCE, rate: E-TOU-C
- Primary DynamoDB tables: `solar-ev-energy-readings`, `solar-ev-user-config`
- Key access patterns:
  - `solar-ev-energy-readings`: query by `summary_date` (YYYY-MM-DD), hourly buckets in Pacific time
  - `solar-ev-user-config`: single-item user preferences (target SOC, schedule prefs)

## Testing
- Unit tests for all Lambda handler logic (moto for AWS mocking)
- Test each Lambda in isolation — no live AWS calls in unit tests
- Integration tests for DynamoDB access patterns when schema changes
- For React: test behavior not implementation (React Testing Library)

## How to Run Locally
```bash
# Terminal 1 — backend (port 3001)
cd backend && python3 local_server.py

# Terminal 2 — frontend (port 5173, proxies /api → 3001)
cd frontend && npm run dev
```

## How to Deploy
```bash
# Bootstrap once per account/region (first time only)
cd infra && npx cdk bootstrap

# Deploy all resources
cd infra && npx cdk deploy
```
