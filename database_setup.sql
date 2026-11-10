-- ============================================================================
-- SmartStock AI - Estrutura de banco (Supabase / PostgreSQL)
-- Execute este arquivo no SQL Editor do Supabase:
-- Dashboard > SQL Editor > New query > cole tudo > Run
-- ============================================================================

-- Perfil de cada cliente, vinculado ao respectivo login (auth.users)
create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    name text,
    email text,
    created_at timestamptz not null default now(),
    last_login_at timestamptz
);

alter table public.profiles enable row level security;

drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own"
    on public.profiles for select
    using (auth.uid() = id);

drop policy if exists "profiles_insert_own" on public.profiles;
create policy "profiles_insert_own"
    on public.profiles for insert
    with check (
        auth.uid() = id
        and lower(email) = lower(auth.jwt() ->> 'email')
    );

drop policy if exists "profiles_update_own" on public.profiles;
create policy "profiles_update_own"
    on public.profiles for update
    using (auth.uid() = id)
    with check (
        auth.uid() = id
        and lower(email) = lower(auth.jwt() ->> 'email')
    );

-- Histórico de acessos (um registro por login)
create table if not exists public.login_events (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    email text,
    logged_in_at timestamptz not null default now()
);

create index if not exists login_events_user_idx
    on public.login_events (user_id, logged_in_at desc);

alter table public.login_events enable row level security;

drop policy if exists "login_events_select_own" on public.login_events;
create policy "login_events_select_own"
    on public.login_events for select
    using (auth.uid() = user_id);

drop policy if exists "login_events_insert_own" on public.login_events;
create policy "login_events_insert_own"
    on public.login_events for insert
    with check (
        auth.uid() = user_id
        and lower(email) = lower(auth.jwt() ->> 'email')
    );

-- Cria o perfil automaticamente quando um novo usuário se cadastra
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (id, name, email)
    values (
        new.id,
        left(coalesce(new.raw_user_meta_data->>'name', new.email), 120),
        new.email
    )
    on conflict (id) do update
        set name = left(excluded.name, 120),
            email = excluded.email;
    return new;
end;
$$;

revoke all on function public.handle_new_user() from public, anon, authenticated;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- ============================================================================
-- Conversas do assistente (histórico por usuário)
-- ============================================================================

create table if not exists public.chat_sessions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    title text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists chat_sessions_user_idx
    on public.chat_sessions (user_id, updated_at desc);

alter table public.chat_sessions enable row level security;

drop policy if exists "chat_sessions_select_own" on public.chat_sessions;
create policy "chat_sessions_select_own"
    on public.chat_sessions for select
    using (auth.uid() = user_id);

drop policy if exists "chat_sessions_insert_own" on public.chat_sessions;
create policy "chat_sessions_insert_own"
    on public.chat_sessions for insert
    with check (auth.uid() = user_id);

drop policy if exists "chat_sessions_update_own" on public.chat_sessions;
create policy "chat_sessions_update_own"
    on public.chat_sessions for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "chat_sessions_delete_own" on public.chat_sessions;
create policy "chat_sessions_delete_own"
    on public.chat_sessions for delete
    using (auth.uid() = user_id);

create table if not exists public.chat_messages (
    id bigint generated always as identity primary key,
    session_id uuid not null references public.chat_sessions(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null check (role in ('user', 'assistant')),
    content text not null,
    created_at timestamptz not null default now()
);

create index if not exists chat_messages_session_idx
    on public.chat_messages (session_id, created_at);

alter table public.chat_messages enable row level security;

drop policy if exists "chat_messages_select_own" on public.chat_messages;
create policy "chat_messages_select_own"
    on public.chat_messages for select
    using (auth.uid() = user_id);

drop policy if exists "chat_messages_insert_own" on public.chat_messages;
create policy "chat_messages_insert_own"
    on public.chat_messages for insert
    with check (
        auth.uid() = user_id
        and exists (
            select 1
            from public.chat_sessions s
            where s.id = session_id
              and s.user_id = auth.uid()
        )
    );

drop policy if exists "chat_messages_delete_own" on public.chat_messages;
create policy "chat_messages_delete_own"
    on public.chat_messages for delete
    using (auth.uid() = user_id);

-- ============================================================================
-- Limites de tamanho (defesa em profundidade contra abuso de armazenamento)
-- ============================================================================
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'profiles_name_len') then
        alter table public.profiles
            add constraint profiles_name_len
            check (name is null or char_length(name) <= 120);
    end if;

    if not exists (select 1 from pg_constraint where conname = 'profiles_email_len') then
        alter table public.profiles
            add constraint profiles_email_len
            check (email is null or char_length(email) <= 254);
    end if;

    if not exists (select 1 from pg_constraint where conname = 'login_events_email_len') then
        alter table public.login_events
            add constraint login_events_email_len
            check (email is null or char_length(email) <= 254);
    end if;

    if not exists (select 1 from pg_constraint where conname = 'chat_sessions_title_len') then
        alter table public.chat_sessions
            add constraint chat_sessions_title_len
            check (title is null or char_length(title) <= 80);
    end if;

    if not exists (select 1 from pg_constraint where conname = 'chat_messages_content_len') then
        alter table public.chat_messages
            add constraint chat_messages_content_len
            check (char_length(content) between 1 and 8000);
    end if;
end;
$$;
