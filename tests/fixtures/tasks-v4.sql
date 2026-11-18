-- Schema copied from the pre-removal v4 task store. Never applied to live state.
CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY CHECK(length(task_id) = 32 AND task_id NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 1),
            goal TEXT NOT NULL,
            project TEXT,
            scope TEXT NOT NULL,
            acceptance TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            source_sender_id TEXT NOT NULL,
            source_input_id TEXT,
            state TEXT NOT NULL CHECK(state IN ('queued', 'ready', 'running', 'blocked', 'awaiting_approval', 'ready_for_review', 'failed', 'cancelled', 'reconciliation_required')),
            verification TEXT NOT NULL CHECK(verification = 'not_run')
        ) STRICT;
CREATE UNIQUE INDEX tasks_source_input ON tasks(source_channel, source_input_id) WHERE source_input_id IS NOT NULL;
CREATE TABLE task_events (
            event_id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('created', 'transitioned', 'cancelled', 'reconciled')),
            from_state TEXT CHECK(from_state IS NULL OR from_state IN ('queued', 'ready', 'running', 'blocked', 'awaiting_approval', 'ready_for_review', 'failed', 'cancelled', 'reconciliation_required')),
            to_state TEXT NOT NULL CHECK(to_state IN ('queued', 'ready', 'running', 'blocked', 'awaiting_approval', 'ready_for_review', 'failed', 'cancelled', 'reconciliation_required')),
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            request_key TEXT NOT NULL,
            note TEXT NOT NULL,
            UNIQUE(task_id, revision),
            CHECK((event_type = 'created' AND revision = 1 AND from_state IS NULL AND to_state = 'queued') OR
                  (event_type IN ('transitioned', 'reconciled') AND revision >= 2 AND from_state IS NOT NULL AND from_state != to_state AND to_state != 'cancelled') OR
                  (event_type = 'cancelled' AND revision >= 2 AND from_state IS NOT NULL AND from_state != 'cancelled' AND to_state = 'cancelled'))
        ) STRICT;
CREATE INDEX task_events_by_task ON task_events(task_id, event_id DESC);
CREATE UNIQUE INDEX task_events_retry ON task_events(task_id, channel, sender_id, request_key);
CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events BEGIN SELECT RAISE(ABORT, 'task events are append-only'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events BEGIN SELECT RAISE(ABORT, 'task events are append-only'); END;
CREATE TABLE action_intents (
            intent_id TEXT PRIMARY KEY CHECK(length(intent_id) = 32 AND intent_id NOT GLOB '*[^0-9a-f]*'),
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            operation_label TEXT NOT NULL,
            target_label TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            payload TEXT NOT NULL,
            request_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('prepared', 'invalidated')),
            CHECK((status = 'prepared' AND revision = 1) OR (status = 'invalidated' AND revision = 2))
        ) STRICT;
CREATE UNIQUE INDEX action_intents_retry ON action_intents(task_id, request_key);
CREATE INDEX action_intents_by_task ON action_intents(task_id, status, updated_at DESC, intent_id ASC);
CREATE TRIGGER action_intents_guard_update BEFORE UPDATE ON action_intents
            WHEN NEW.intent_id != OLD.intent_id OR NEW.task_id != OLD.task_id OR
                 NEW.task_revision != OLD.task_revision OR NEW.created_at != OLD.created_at OR
                 NEW.operation_label != OLD.operation_label OR NEW.target_label != OLD.target_label OR
                 NEW.target_identity != OLD.target_identity OR NEW.payload != OLD.payload OR
                 NEW.request_key != OLD.request_key OR OLD.status != 'prepared' OR
                 NEW.status != 'invalidated' OR NEW.revision != OLD.revision + 1
            BEGIN SELECT RAISE(ABORT, 'action intent request is immutable'); END;
CREATE TABLE action_events (
            event_id INTEGER PRIMARY KEY,
            intent_id TEXT NOT NULL REFERENCES action_intents(intent_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('prepared', 'invalidated')),
            from_status TEXT,
            to_status TEXT NOT NULL CHECK(to_status IN ('prepared', 'invalidated')),
            UNIQUE(intent_id, revision),
            CHECK(
                (event_type = 'prepared' AND revision = 1 AND from_status IS NULL AND to_status = 'prepared') OR
                (event_type = 'invalidated' AND revision = 2 AND from_status = 'prepared' AND to_status = 'invalidated')
            )
        ) STRICT;
CREATE INDEX action_events_by_intent ON action_events(intent_id, event_id DESC);
CREATE TRIGGER action_events_no_update BEFORE UPDATE ON action_events BEGIN SELECT RAISE(ABORT, 'action events are append-only'); END;
CREATE TRIGGER action_events_no_delete BEFORE DELETE ON action_events BEGIN SELECT RAISE(ABORT, 'action events are append-only'); END;
CREATE TABLE dispatches (
            dispatch_id TEXT PRIMARY KEY CHECK(length(dispatch_id) = 32 AND dispatch_id NOT GLOB '*[^0-9a-f]*'),
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
            action_intent_id TEXT NOT NULL REFERENCES action_intents(intent_id) ON DELETE RESTRICT,
            action_revision INTEGER NOT NULL CHECK(action_revision >= 1),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            request_key TEXT NOT NULL,
            target_label TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('recorded', 'invalidated')),
            CHECK((status = 'recorded' AND revision = 1) OR (status = 'invalidated' AND revision = 2))
        ) STRICT;
CREATE UNIQUE INDEX dispatches_retry ON dispatches(channel, sender_id, request_key);
CREATE INDEX dispatches_by_task ON dispatches(task_id, status, updated_at DESC, dispatch_id ASC);
CREATE TABLE dispatch_events (
            event_id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('recorded', 'invalidated')),
            from_status TEXT,
            to_status TEXT NOT NULL CHECK(to_status IN ('recorded', 'invalidated')),
            UNIQUE(dispatch_id, revision),
            CHECK((event_type = 'recorded' AND revision = 1 AND from_status IS NULL AND to_status = 'recorded') OR
                  (event_type = 'invalidated' AND revision = 2 AND from_status = 'recorded' AND to_status = 'invalidated'))
        ) STRICT;
CREATE INDEX dispatch_events_by_dispatch ON dispatch_events(dispatch_id, event_id DESC);
CREATE TRIGGER dispatches_guard_update BEFORE UPDATE ON dispatches
            WHEN NEW.dispatch_id != OLD.dispatch_id OR NEW.task_id != OLD.task_id OR
                 NEW.task_revision != OLD.task_revision OR NEW.action_intent_id != OLD.action_intent_id OR
                 NEW.action_revision != OLD.action_revision OR NEW.created_at != OLD.created_at OR
                 NEW.channel != OLD.channel OR NEW.sender_id != OLD.sender_id OR NEW.request_key != OLD.request_key OR
                 NEW.target_label != OLD.target_label OR NEW.target_identity != OLD.target_identity OR
                 OLD.status != 'recorded' OR NEW.status != 'invalidated' OR NEW.revision != OLD.revision + 1
            BEGIN SELECT RAISE(ABORT, 'dispatch binding is immutable'); END;
CREATE TRIGGER dispatch_events_no_update BEFORE UPDATE ON dispatch_events BEGIN SELECT RAISE(ABORT, 'dispatch events are append-only'); END;
CREATE TRIGGER dispatch_events_no_delete BEFORE DELETE ON dispatch_events BEGIN SELECT RAISE(ABORT, 'dispatch events are append-only'); END;
CREATE TABLE approvals (
            approval_id TEXT PRIMARY KEY CHECK(length(approval_id) = 32 AND approval_id NOT GLOB '*[^0-9a-f]*'),
            dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT,
            dispatch_revision INTEGER NOT NULL CHECK(dispatch_revision >= 1),
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
            action_intent_id TEXT NOT NULL REFERENCES action_intents(intent_id) ON DELETE RESTRICT,
            action_revision INTEGER NOT NULL CHECK(action_revision >= 1),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ttl_seconds INTEGER NOT NULL CHECK(ttl_seconds BETWEEN 1 AND 3600),
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            request_key TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            decision TEXT CHECK(decision IS NULL OR decision IN ('accepted', 'rejected')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected', 'expired', 'invalidated')),
            CHECK((status = 'pending' AND revision = 1 AND decision IS NULL) OR
                  (status = 'accepted' AND revision = 2 AND decision = 'accepted') OR
                  (status = 'rejected' AND revision = 2 AND decision = 'rejected') OR
                  (status IN ('expired', 'invalidated') AND revision = 2 AND decision IS NULL))
        ) STRICT;
CREATE UNIQUE INDEX approvals_retry ON approvals(channel, sender_id, request_key);
CREATE INDEX approvals_by_task ON approvals(task_id, status, expires_at, approval_id);
CREATE TABLE approval_events (
            event_id INTEGER PRIMARY KEY,
            approval_id TEXT NOT NULL REFERENCES approvals(approval_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('requested', 'accepted', 'rejected', 'expired', 'invalidated')),
            from_status TEXT,
            to_status TEXT NOT NULL CHECK(to_status IN ('pending', 'accepted', 'rejected', 'expired', 'invalidated')),
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            UNIQUE(approval_id, revision),
            CHECK((event_type = 'requested' AND revision = 1 AND from_status IS NULL AND to_status = 'pending') OR
                  (event_type != 'requested' AND revision = 2 AND from_status = 'pending' AND to_status = event_type))
        ) STRICT;
CREATE INDEX approval_events_by_approval ON approval_events(approval_id, event_id DESC);
CREATE TRIGGER approvals_guard_update BEFORE UPDATE ON approvals
            WHEN NEW.approval_id != OLD.approval_id OR NEW.dispatch_id != OLD.dispatch_id OR
                 NEW.dispatch_revision != OLD.dispatch_revision OR
                 NEW.task_id != OLD.task_id OR NEW.task_revision != OLD.task_revision OR
                 NEW.action_intent_id != OLD.action_intent_id OR NEW.action_revision != OLD.action_revision OR
                 NEW.created_at != OLD.created_at OR NEW.expires_at != OLD.expires_at OR NEW.ttl_seconds != OLD.ttl_seconds OR
                 NEW.channel != OLD.channel OR NEW.sender_id != OLD.sender_id OR NEW.request_key != OLD.request_key OR
                 NEW.target_identity != OLD.target_identity OR OLD.status != 'pending' OR NEW.revision != OLD.revision + 1 OR
                 NEW.status NOT IN ('accepted', 'rejected', 'expired', 'invalidated') OR
                 (NEW.status IN ('accepted', 'rejected') AND NEW.decision != NEW.status) OR
                 (NEW.status IN ('expired', 'invalidated') AND NEW.decision IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'approval request is immutable or already used'); END;
CREATE TRIGGER approval_events_no_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT, 'approval events are append-only'); END;
CREATE TRIGGER approval_events_no_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT, 'approval events are append-only'); END;
CREATE TABLE runs (
            run_id TEXT PRIMARY KEY CHECK(length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*'),
            dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT,
            dispatch_revision INTEGER NOT NULL CHECK(dispatch_revision >= 1),
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
            action_intent_id TEXT NOT NULL REFERENCES action_intents(intent_id) ON DELETE RESTRICT,
            action_revision INTEGER NOT NULL CHECK(action_revision >= 1),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            request_key TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open', 'succeeded', 'failed', 'uncertain', 'not_started')),
            outcome_note TEXT NOT NULL,
            CHECK((status = 'open' AND revision = 1) OR
                  (status IN ('succeeded', 'failed', 'uncertain', 'not_started') AND revision IN (2, 3)))
        ) STRICT;
CREATE UNIQUE INDEX runs_retry ON runs(dispatch_id, request_key);
CREATE INDEX runs_by_dispatch ON runs(dispatch_id, status, updated_at DESC, run_id ASC);
CREATE UNIQUE INDEX runs_one_open_per_task ON runs(task_id) WHERE status = 'open';
CREATE TABLE run_events (
            event_id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'started', 'succeeded', 'failed', 'interrupted',
                'confirmed_succeeded', 'confirmed_failed', 'confirmed_not_started'
            )),
            from_status TEXT,
            to_status TEXT NOT NULL CHECK(to_status IN ('open', 'succeeded', 'failed', 'uncertain', 'not_started')),
            channel TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            note TEXT NOT NULL,
            UNIQUE(run_id, revision),
            CHECK((event_type = 'started' AND revision = 1 AND from_status IS NULL AND to_status = 'open') OR
                  (event_type IN ('succeeded', 'failed', 'interrupted') AND revision = 2 AND from_status = 'open' AND
                   to_status = CASE event_type WHEN 'interrupted' THEN 'uncertain' ELSE event_type END) OR
                  (event_type LIKE 'confirmed_%' AND revision = 3 AND from_status = 'uncertain' AND
                   to_status = CASE event_type WHEN 'confirmed_succeeded' THEN 'succeeded' WHEN 'confirmed_failed' THEN 'failed' ELSE 'not_started' END))
        ) STRICT;
CREATE INDEX run_events_by_run ON run_events(run_id, event_id DESC);
CREATE TRIGGER runs_guard_update BEFORE UPDATE ON runs
            WHEN NEW.run_id != OLD.run_id OR NEW.dispatch_id != OLD.dispatch_id OR
                 NEW.dispatch_revision != OLD.dispatch_revision OR NEW.task_id != OLD.task_id OR
                 NEW.task_revision != OLD.task_revision OR NEW.action_intent_id != OLD.action_intent_id OR
                 NEW.action_revision != OLD.action_revision OR NEW.created_at != OLD.created_at OR
                 NEW.channel != OLD.channel OR NEW.sender_id != OLD.sender_id OR NEW.request_key != OLD.request_key OR
                 NEW.target_identity != OLD.target_identity OR
                 NOT ((OLD.status = 'open' AND NEW.status IN ('succeeded', 'failed', 'uncertain')) OR
                      (OLD.status = 'uncertain' AND NEW.status IN ('succeeded', 'failed', 'not_started'))) OR
                 NEW.revision != OLD.revision + 1
            BEGIN SELECT RAISE(ABORT, 'run binding or terminal state is immutable'); END;
CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events BEGIN SELECT RAISE(ABORT, 'run events are append-only'); END;
CREATE TRIGGER run_events_no_delete BEFORE DELETE ON run_events BEGIN SELECT RAISE(ABORT, 'run events are append-only'); END;
CREATE TABLE reminders (
            reminder_id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            due_at INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','delivering','spoken','cancelled','unknown')),
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            spoken_at INTEGER
        );
CREATE INDEX reminders_state_due ON reminders(state, due_at);
CREATE TABLE inbox (
            item_id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
PRAGMA application_id=1247171667;
PRAGMA user_version=4;
