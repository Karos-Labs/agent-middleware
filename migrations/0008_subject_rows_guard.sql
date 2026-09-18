-- --------------------------------------------------------------------
-- 0008 — subject_rows: what a published post may never stop being (B1)
-- --------------------------------------------------------------------
--
-- SCRUM-462 asks for this in one line: *"Status can only move forward
-- (trigger, same style as `run_feedback_00_guard`). No deletes."* The table
-- landed in 0007 with only `subject_rows_90_touch`, so a `posted` row could be
-- moved back to `drafted` and any row could be deleted outright.
--
-- WHAT "FORWARD" ACTUALLY MEANS HERE, because the obvious reading is wrong.
-- The tempting guard is a rank over the five statuses, refusing any move to a
-- lower one. That guard refuses `approved` → `change_requested`, which is a
-- client approving a draft on Monday and asking for a change on Tuesday —
-- ordinary, frequent, and nobody's mistake. A rank would also refuse `skipped`
-- → `posted`, which is a client passing on a draft and posting it a week later;
-- refusing that leaves the row saying `skipped` about a post that is live.
--
-- Every status but one describes somebody's INTENTION, and an intention may be
-- revised. `posted` describes a FACT about the world: the words are out, under
-- the client's name, where other people can read them. That is the one that
-- cannot be taken back, so that is the one this guard makes terminal.
--
-- Why it matters beyond tidiness: `posted` rows are what B2's `likes` are
-- derived from and what the anti-repetition window treats as "already said".
-- A row that moves off `posted` retroactively unsays a post the audience saw,
-- and the next run is then free to write it again.
--
-- The three rules, and each is a different kind of untruth:
--
--   1. A `posted` row may not change status, and may not have `posted_at`
--      cleared. Both would deny a publication that happened.
--   2. No row may change `client_slug`, `platform`, `run_id` or `subject`.
--      Those four are the row's identity and every join in the loop; a row
--      that starts describing a different run is worse than a deleted one,
--      because nothing looks wrong.
--   3. No deletes, ever — the same rule `client_feedback_log_00_guard` already
--      enforces next door, for the same reason. A subject row is the record
--      that a thing was said; a wrong one is corrected by the row that
--      supersedes it, not by removing the evidence.
--
-- Idempotent: re-running `set_subject_status` with `posted` on an already
-- `posted` row is a no-op and stays allowed, which is what makes the portal's
-- retry-safe publish path safe.

create or replace function config.guard_subject_rows_history()
returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception
            'subject_rows: row % is the record that a subject was drafted and cannot be deleted',
            old.id
            using errcode = 'restrict_violation',
                  hint = 'Supersede it with a newer row. The loop reads this table as history.';
    end if;

    if old.client_slug is distinct from new.client_slug
       or old.platform is distinct from new.platform
       or old.run_id is distinct from new.run_id
       or old.subject is distinct from new.subject
    then
        raise exception
            'subject_rows: row % cannot be repointed at a different client, platform, run or subject',
            old.id
            using errcode = 'restrict_violation',
                  hint = 'Those four are this row''s identity. A different one of any of them is a different row.';
    end if;

    if old.status = 'posted' then
        if new.status is distinct from old.status then
            raise exception
                'subject_rows: row % is posted and cannot become %',
                old.id, new.status
                using errcode = 'restrict_violation',
                      hint = 'A posted draft is out in the world under the client''s name. '
                             'Record what happened next as a new row, not by unsaying this one.';
        end if;
        if new.posted_at is null and old.posted_at is not null then
            raise exception
                'subject_rows: row % is posted and cannot lose its posted_at',
                old.id
                using errcode = 'restrict_violation',
                      hint = 'The timestamp is what the anti-repetition window and the likes derivation read.';
        end if;
    end if;

    return new;
end;
$$;

-- `00` so it runs before `90_touch`: a refused write should never have moved
-- `updated_at` first.
drop trigger if exists subject_rows_00_guard on config.subject_rows;
create trigger subject_rows_00_guard
    before update or delete on config.subject_rows
    for each row execute function config.guard_subject_rows_history();
