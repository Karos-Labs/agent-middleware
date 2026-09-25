-- 0009 — lessons a person retired from the derived voice notes (2026-09-25)
--
-- `client_preferences.voice_notes` is DERIVED: every feedback event rebuilds
-- it from the log and the collected run records (`derive_preferences`). Since
-- SCRUM-508 every revision note and change request becomes a standing lesson,
-- including a note that was only true of one post. Deleting it from
-- `voice_notes` would not last -- the next event derives it straight back from
-- the append-only log. So a retired lesson is recorded here, in the words it
-- was shown in, and the derivation leaves it out (matched on the sentence,
-- case and spacing ignored). Restoring it is removing it from this list.
--
-- Set by a person through POST /clients/{slug}/learning/preferences/lessons/
-- {retire,restore}. Never read by the engine: what the engine reads is the
-- derived list with these already taken out.
--
-- Idempotent: safe to re-run.

begin;

alter table config.client_preferences
    add column if not exists retired_lessons text[] not null default '{}';

comment on column config.client_preferences.retired_lessons is
    'Derived voice lessons a person retired, in the words shown. derive_preferences leaves any '
    'lesson matching one of these (case and spacing ignored) out of voice_notes.';

insert into config.schema_migrations (filename) values ('0009_retired_lessons.sql')
on conflict (filename) do nothing;

commit;
