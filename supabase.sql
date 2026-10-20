-- Run once in Supabase: SQL Editor > New query > paste > Run.
create table if not exists runs (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  url text, company text, buyer text, objective text,
  provider text, model text,
  pages int, claims int, verified int, verify_rate real,
  tokens_in int, tokens_out int, cost_usd real, seconds real,
  result jsonb
);
alter table runs enable row level security;  -- no policies: only the server's service key can read or write
