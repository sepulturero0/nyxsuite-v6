# NyxSuite Devices Edge Function

Token-gated endpoint for the Developer Settings device fleet dashboard.

Deploy with JWT verification disabled so NyxSuite installs only need the Fleet
API URL and `NYXSUITE_FLEET_TOKEN`:

```sh
supabase secrets set NYXSUITE_FLEET_TOKEN=<shared-fleet-token>
supabase functions deploy nyxsuite-devices --no-verify-jwt
```

Endpoints:

- `POST /heartbeat` with `X-NyxSuite-Fleet-Token`
- `GET /devices` with `X-NyxSuite-Fleet-Token`

The payload is intentionally minimal: device id, device name, OS, app version,
and last-seen timestamp only.
