-- 0008 — per-platform post-type preferences on client_preferences (2026-09-23)
--
-- The owner: a client's post-type preferences (Geektime posts news flashes,
-- Deel's feed is photo-led) are facts about the client and belong with what
-- the feedback loop knows about it, not in hand-set engine config. Set by a
-- person (PUT /clients/{slug}/learning/preferences, `formats`), projected into
-- the C7 §2.4 preferences doc as `formats`, read by the Instagram engine's
-- 01-open-run below the run input and the client config.
--
-- Shape: {"instagram": {"format": "single", "postModes": ["news_flash"],
--          "pictureDensity": "photo-first", "series": "the_list"}}
-- Validated by the API (`FormatPreference`); the table only insists on an
-- object, so a new platform or field needs no migration.
--
-- Idempotent: safe to re-run.

begin;

alter table config.client_preferences
    add column if not exists format_preferences jsonb not null default '{}'::jsonb;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'client_preferences_formats_is_object'
    ) then
        alter table config.client_preferences
            add constraint client_preferences_formats_is_object
            check (jsonb_typeof(format_preferences) = 'object');
    end if;
end $$;

comment on column config.client_preferences.format_preferences is
    'Per-platform post-type preferences a person set (format, postModes, pictureDensity, series). '
    'Projected into the C7 preferences doc as `formats`; a run input and the engine config override it.';

insert into config.schema_migrations (filename) values ('0008_format_preferences.sql')
on conflict (filename) do nothing;

commit;
