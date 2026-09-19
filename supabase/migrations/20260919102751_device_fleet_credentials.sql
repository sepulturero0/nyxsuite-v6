create table if not exists public.nyxsuite_device_credentials (
  device_id text primary key,
  token_hash text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.nyxsuite_device_credentials enable row level security;

revoke all on table public.nyxsuite_device_credentials from anon, authenticated;
grant all on table public.nyxsuite_device_credentials to service_role;
