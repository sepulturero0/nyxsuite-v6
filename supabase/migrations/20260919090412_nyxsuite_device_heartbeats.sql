create table if not exists public.nyxsuite_device_heartbeats (
  device_id text primary key,
  device_name text not null,
  os text not null default 'unknown'
    check (os in ('windows', 'macos', 'linux', 'unknown')),
  app_version text not null default '',
  last_seen timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.nyxsuite_device_heartbeats enable row level security;

revoke all on table public.nyxsuite_device_heartbeats from anon, authenticated;
grant all on table public.nyxsuite_device_heartbeats to service_role;

create index if not exists nyxsuite_device_heartbeats_last_seen_idx
  on public.nyxsuite_device_heartbeats (last_seen desc);
