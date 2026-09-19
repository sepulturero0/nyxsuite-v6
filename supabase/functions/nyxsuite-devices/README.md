# NyxSuite Devices Edge Function

Token-gated endpoint for the Developer Settings device fleet dashboard.

Deploy with JWT verification disabled. New NyxSuite installs use the Fleet API
URL to enroll automatically and persist a device-only heartbeat credential.
The `NYXSUITE_FLEET_TOKEN` remains an owner credential for reading the fleet:

```sh
supabase secrets set NYXSUITE_FLEET_TOKEN=<shared-fleet-token>
supabase functions deploy nyxsuite-devices --no-verify-jwt
```

Endpoints:

- `POST /enroll` returns a device-only credential for the submitted device id
- `POST /heartbeat` with a device-only or owner `X-NyxSuite-Fleet-Token`
- `GET /devices` with `X-NyxSuite-Fleet-Token`

The payload is intentionally minimal: device id, device name, OS, app version,
and last-seen timestamp only. Device-only credentials cannot read the fleet.
