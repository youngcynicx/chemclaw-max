SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chemicals (
    chemical_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    formula TEXT,
    location TEXT,
    amount REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL,
    threshold REAL DEFAULT 0,
    hazard_level TEXT NOT NULL DEFAULT 'low',
    max_auto_delta REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    location TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS message_logs (
    message_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    source_message_id TEXT,
    raw_text TEXT NOT NULL,
    parsed_intent TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS event_proposals (
    event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_subtype TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'proposed', 'pending_confirmation', 'approved',
            'rejected', 'applied', 'superseded'
        )
    ),
    policy_version TEXT NOT NULL,
    supersedes_event_id TEXT,
    confirmed_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES message_logs(message_id),
    FOREIGN KEY (supersedes_event_id) REFERENCES event_proposals(event_id)
);

CREATE TABLE IF NOT EXISTS inventory_transactions (
    transaction_id TEXT PRIMARY KEY,
    message_id TEXT,
    event_id TEXT,
    chemical_id TEXT NOT NULL,
    user_id TEXT,
    project_id TEXT,
    transaction_type TEXT NOT NULL,
    amount_delta REAL NOT NULL,
    unit TEXT NOT NULL,
    confidence REAL,
    confirmation_status TEXT NOT NULL DEFAULT 'approved',
    raw_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES message_logs(message_id),
    FOREIGN KEY (event_id) REFERENCES event_proposals(event_id),
    FOREIGN KEY (chemical_id) REFERENCES chemicals(chemical_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS experiment_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT,
    user_id TEXT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT,
    source_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id TEXT PRIMARY KEY,
    message_id TEXT,
    event_id TEXT,
    event_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    required_action TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES message_logs(message_id),
    FOREIGN KEY (event_id) REFERENCES event_proposals(event_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gateway_identities (
    identity_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE (source, external_user_id)
);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# --- S02 (schema version 4): shared contracts, identity, and durable flows ---
#
# Identity: principals/memberships/credentials are the authority for the v2
# capability entry. Legacy users/gateway_identities stay untouched for the
# compatibility API. Session namespaces bind (principal, project, mode) first;
# channel coordinates are attributes of that binding.
S02_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'agent', 'service')),
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memberships (
    principal_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'member', 'owner')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (principal_id, project_id),
    FOREIGN KEY (principal_id) REFERENCES principals(principal_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS auth_credentials (
    credential_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'api_key',
    secret_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (principal_id) REFERENCES principals(principal_id),
    UNIQUE (principal_id, credential_id)
);

CREATE TABLE IF NOT EXISTS session_namespaces (
    session_key TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    project_id TEXT,
    mode TEXT NOT NULL,
    role TEXT,
    channel_source TEXT,
    external_account TEXT,
    peer TEXT,
    thread_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (principal_id) REFERENCES principals(principal_id)
);

-- Binds an authenticated principal to a legacy user row so business
-- attribution (inventory_transactions.user_id FK) is derived from the
-- authenticated subject, never from a self-reported field.
CREATE TABLE IF NOT EXISTS principal_user_links (
    principal_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (principal_id) REFERENCES principals(principal_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS operation_results (
    namespace TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    revision_after INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (namespace, operation_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    title TEXT,
    principal_id TEXT NOT NULL,
    project_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')
    ),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (principal_id) REFERENCES principals(principal_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    worker_id TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    input_version TEXT NOT NULL,
    output_refs_json TEXT NOT NULL DEFAULT '[]',
    operation_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS pending_interactions (
    interaction_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    question TEXT NOT NULL,
    context_refs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('open', 'answered', 'expired')),
    answer_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    answered_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS inbound_events (
    inbound_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    transport_event_id TEXT NOT NULL,
    external_thread TEXT,
    principal_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('received', 'queued', 'processed', 'failed', 'duplicate')),
    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT,
    UNIQUE (source, transport_event_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'delivered', 'failed')),
    transport_event_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    FOREIGN KEY (outbox_id) REFERENCES outbox(outbox_id)
);

CREATE TABLE IF NOT EXISTS typed_messages (
    message_id TEXT PRIMARY KEY,
    message_kind TEXT NOT NULL CHECK (message_kind IN ('command', 'domain_event', 'task_result')),
    message_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    causation_id TEXT,
    correlation_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Experiment domain schema (S02 lays the tables; S04 provides the services).
-- Revision chains carry both event time and knowledge time (Graphiti-inspired
-- temporal semantics adapted to SQLite per plan §3.2 of the research review).
CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    project_id TEXT,
    label TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS experiment_records (
    record_id TEXT PRIMARY KEY,
    project_id TEXT,
    run_id TEXT,
    sample_id TEXT,
    record_kind TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (run_id) REFERENCES experiment_runs(run_id),
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id)
);

CREATE TABLE IF NOT EXISTS evidence_objects (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT,
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime TEXT,
    page_count INTEGER,
    source TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS event_revisions (
    revision_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    field_json TEXT NOT NULL DEFAULT '{}',
    event_time_start TEXT,
    event_time_end TEXT,
    knowledge_as_of TEXT,
    supersedes_revision_id TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_type, entity_id, revision),
    FOREIGN KEY (supersedes_revision_id) REFERENCES event_revisions(revision_id)
);
"""

# --- S03 (schema version 5): evidence ingestion, transcription, positioning ---
#
# The original file is immutable (evidence_objects.sha256 re-checkable); every
# derived version records its transform chain. Transcription runs are
# append-only; the page's active transcript is a pointer, so corrections
# create new revisions and never overwrite raw recognition output.
S03_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evidence_documents (
    document_id TEXT PRIMARY KEY,
    project_id TEXT,
    notebook_code TEXT NOT NULL,
    title TEXT,
    author TEXT,
    source_channel TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, notebook_code),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS evidence_pages (
    page_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    page_order INTEGER NOT NULL,
    paper_page_number TEXT,
    status TEXT NOT NULL DEFAULT 'ingested'
      CHECK (status IN ('ingested', 'transcribing', 'transcribed',
                        'needs_human', 'failed')),
    prev_page_id TEXT,
    next_page_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, page_order),
    FOREIGN KEY (document_id) REFERENCES evidence_documents(document_id),
    FOREIGN KEY (evidence_id) REFERENCES evidence_objects(evidence_id),
    FOREIGN KEY (prev_page_id) REFERENCES evidence_pages(page_id),
    FOREIGN KEY (next_page_id) REFERENCES evidence_pages(page_id)
);

CREATE TABLE IF NOT EXISTS page_relations (
    relation_id TEXT PRIMARY KEY,
    from_page_id TEXT NOT NULL,
    to_page_id TEXT NOT NULL,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('continuation', 'supplement', 'insert')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_page_id) REFERENCES evidence_pages(page_id),
    FOREIGN KEY (to_page_id) REFERENCES evidence_pages(page_id)
);

CREATE TABLE IF NOT EXISTS derived_artifacts (
    artifact_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preview', 'rotated', 'cropped', 'enhanced')),
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    transform_json TEXT NOT NULL DEFAULT '{}',
    materialized INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (page_id) REFERENCES evidence_pages(page_id)
);

CREATE TABLE IF NOT EXISTS transcript_runs (
    run_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    provider_version TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    input_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'succeeded', 'failed', 'needs_human')),
    error_code TEXT,
    origin TEXT NOT NULL DEFAULT 'ocr' CHECK (origin IN ('ocr', 'text', 'human_correction')),
    raw_output_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    FOREIGN KEY (page_id) REFERENCES evidence_pages(page_id)
);

CREATE TABLE IF NOT EXISTS transcript_blocks (
    block_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    block_kind TEXT NOT NULL,
    reading_order INTEGER NOT NULL,
    block_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES transcript_runs(run_id)
);

CREATE TABLE IF NOT EXISTS page_transcripts (
    page_id TEXT PRIMARY KEY,
    active_run_id TEXT,
    active_revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    FOREIGN KEY (page_id) REFERENCES evidence_pages(page_id),
    FOREIGN KEY (active_run_id) REFERENCES transcript_runs(run_id)
);

CREATE TABLE IF NOT EXISTS risk_fragments (
    fragment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sample_id', 'number', 'unit', 'date', 'negation', 'state')),
    value_text TEXT,
    normalized_value TEXT,
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'unknown', 'pending_confirm')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES transcript_runs(run_id),
    FOREIGN KEY (block_id) REFERENCES transcript_blocks(block_id)
);

CREATE TABLE IF NOT EXISTS competing_readings (
    reading_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL,
    candidate_text TEXT NOT NULL,
    confidence REAL,
    chosen INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (block_id) REFERENCES transcript_blocks(block_id)
);
"""

# --- S04 (schema version 6): experiment memory service ---
# Proposals are the ONLY write path for facts: raw recognition and derived
# indices never promote themselves to facts (plan §10.8). Confirmations are
# bound to a specific proposal revision, so correcting the source evidence
# invalidates prior confirmations (plan §10.10). `event_proposals` is taken
# by the inventory flow, hence the memory_* prefix.
S04_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_proposals (
    proposal_id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    page_id TEXT,
    source_transcript_revision INTEGER,
    profile_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed'
      CHECK (status IN ('proposed', 'awaiting_confirmation', 'awaiting_clarification',
                        'partially_applied', 'applied', 'rejected', 'expired')),
    operation_id TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    UNIQUE (project_id, operation_id),
    FOREIGN KEY (page_id) REFERENCES evidence_pages(page_id)
);

CREATE TABLE IF NOT EXISTS clarifications (
    clarification_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL DEFAULT 0,
    interaction_id TEXT,
    questions_json TEXT NOT NULL DEFAULT '[]',
    answers_json TEXT,
    status TEXT NOT NULL DEFAULT 'open'
      CHECK (status IN ('open', 'answered', 'partially_answered', 'expired', 'cancelled')),
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    answered_at TEXT,
    FOREIGN KEY (proposal_id) REFERENCES memory_proposals(proposal_id),
    FOREIGN KEY (interaction_id) REFERENCES pending_interactions(interaction_id)
);

CREATE TABLE IF NOT EXISTS event_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'declined')),
    principal_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid'
      CHECK (status IN ('valid', 'invalidated')),
    invalidated_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    invalidated_at TEXT,
    UNIQUE (proposal_id, proposal_revision, item_key, principal_id),
    FOREIGN KEY (proposal_id) REFERENCES memory_proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS memory_artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    based_on_revisions_json TEXT NOT NULL DEFAULT '[]',
    derived INTEGER NOT NULL DEFAULT 1,
    stale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    rebuilt_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
"""

# --- S05: shared query service -------------------------------------------
# Read-side only: reports persist what was answered and against which source
# revisions; the audit log records query paths and candidate references —
# never private text (plan §11.11).
S05_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS query_reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    knowledge_as_of TEXT,
    snapshot_json TEXT NOT NULL,
    claims_json TEXT NOT NULL DEFAULT '[]',
    rendered_text TEXT NOT NULL DEFAULT '',
    based_on_json TEXT NOT NULL DEFAULT '[]',
    stale INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
      CHECK (status IN ('active', 'restricted')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS query_audit_log (
    log_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    query_path TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    snapshot_version TEXT,
    candidate_refs_json TEXT NOT NULL DEFAULT '[]',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# --- S06: agent host (manifests, run tokens, workflows) ---------------------
# The Pi host executes roles; Core keeps execution authority. Manifests are
# loaded only from the admin config directory; declaring a capability never
# grants authority (every tool call is re-authorized by the capability layer).
# Run tokens are server-signed per run: identity lives in the token, never in
# tool parameters (plan §12.5).
S06_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_manifests (
    manifest_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    loaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (agent_id, version)
);

CREATE TABLE IF NOT EXISTS agent_run_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    project_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_scope_json TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT,
    budget_tool_calls INTEGER NOT NULL,
    used_tool_calls INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
      CHECK (status IN ('active', 'exhausted', 'revoked', 'expired')),
    expires_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    current_step TEXT,
    steps_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'running'
      CHECK (status IN ('running', 'waiting_input', 'succeeded', 'failed', 'cancelled')),
    claim_epoch INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
"""

# --- S07 (schema version 9): cross-domain inventory subtasks ------------------
#
# B-domain memory events that represent ACTUAL chemical usage reach the
# inventory domain as persisted subtasks (plan §13.3), never as LLM chat.
# Each row carries its own dedup key (the same operation reported twice —
# photo + message — must consume stock exactly once, §13.4) and the stock
# revision it was computed against, so a stale worker cannot apply a delta
# silently (§13.3). The subtask is the traceable link between event_id,
# evidence_id and inventory_transaction_id (§13.6); "record saved, inventory
# pending" is a normal, retryable state (§13.7).
S07_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inventory_subtasks (
    subtask_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    project_id TEXT,
    source_event_id TEXT NOT NULL,
    source_event_revision INTEGER NOT NULL DEFAULT 1,
    evidence_id TEXT,
    -- Nullable while the identity is unresolved (§13.2 clarification).
    chemical_id TEXT,
    transaction_type TEXT NOT NULL
      CHECK (transaction_type IN ('use', 'add', 'adjust', 'opening_balance')),
    amount REAL NOT NULL,
    unit TEXT NOT NULL,
    amount_delta REAL NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending', 'clarification', 'submitted', 'applied',
                        'failed', 'duplicate_suspect', 'rejected', 'cancelled')),
    expected_revision INTEGER,
    inventory_transaction_id TEXT,
    event_id TEXT,
    trace_id TEXT,
    error_code TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chemical_id) REFERENCES chemicals(chemical_id)
);
"""

# Adjustment proposals (§13.8): an experiment usage correction never silently
# rewrites an applied inventory transaction; it creates a linked proposal that
# a human confirms, and confirmation applies an `adjust` transaction.
S07_ADJUSTMENTS_SQL = """
CREATE TABLE IF NOT EXISTS inventory_adjustment_proposals (
    adjustment_id TEXT PRIMARY KEY,
    project_id TEXT,
    source_event_id TEXT NOT NULL,
    original_transaction_id TEXT NOT NULL,
    chemical_id TEXT NOT NULL,
    amount_delta REAL NOT NULL,
    unit TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_confirmation'
      CHECK (status IN ('pending_confirmation', 'applied', 'rejected', 'stale')),
    inventory_transaction_id TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# S09 (plan §15): extension domains.
#
# C — resources & bookings: plans, candidate slots and formal bookings are
# DISTINCT return values; a booking is scheduling fact, never an experiment
# result (it mints no experiment events and no inventory messages).
S09_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL
      CHECK (kind IN ('instrument', 'room', 'equipment', 'service')),
    capacity INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active'
      CHECK (status IN ('active', 'maintenance', 'retired')),
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS resource_bookings (
    booking_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    project_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
      CHECK (status IN ('planned', 'confirmed', 'cancelled', 'completed')),
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    purpose_note TEXT,
    plan_summary TEXT,
    candidates_json TEXT,
    created_by TEXT,
    confirmed_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- D — research milestones: acceptance criteria are checked against locatable
-- evidence refs; status transitions are human decisions. There is
-- deliberately NO progress-percentage column (§15 D).
CREATE TABLE IF NOT EXISTS research_milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending', 'in_review', 'accepted', 'rejected')),
    created_by TEXT,
    decided_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS milestone_criteria (
    criterion_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES research_milestones(milestone_id),
    description TEXT NOT NULL,
    evidence_refs TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending', 'satisfied', 'unverified', 'stale')),
    check_note TEXT,
    checked_by TEXT,
    checked_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- External results: computation outputs are NOT wet-lab measurements — they
-- live in their own table and never reach experiment events or the inventory
-- bridge (§15 external-results rule).
CREATE TABLE IF NOT EXISTS external_results (
    result_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_kind TEXT NOT NULL
      CHECK (source_kind IN ('computation', 'literature', 'external_lab')),
    title TEXT NOT NULL,
    summary TEXT,
    tool_name TEXT,
    tool_version TEXT,
    method TEXT,
    inputs_hash TEXT,
    artifact_refs TEXT,
    task_id TEXT,
    submitted_by TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# S10 (plan §16): messaging channels. The channel layer is an ORIGINAL-LEDGER
# plus a mapping layer — it never replaces Core idempotency:
#
# - channel_accounts: admin-registered channel endpoints. Credentials are
#   stored as REFERENCES (env var / secret-store names), never as plaintext.
# - channel_identities: explicit, revocable platform-author -> principal
#   bindings made by a trusted admin. Group/peer relations NEVER grant
#   project access by themselves (§16.3).
# - channel_messages: raw inbound event ledger. The original is persisted
#   BEFORE any processing (§16.6 outage rule); dedupe is
#   UNIQUE(channel_account_id, platform_message_id) so the same external
#   event across two channel accounts stays namespace-separated (§16.7).
#   Recalls/edits append review state — audited business history is never
#   rewritten (§16.4).
S10_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channel_accounts (
    channel_account_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL
      CHECK (platform IN ('webhook_test', 'qq_bot', 'wecom_bot', 'local')),
    display_name TEXT,
    credential_ref TEXT,
    verify_secret_ref TEXT,
    allow_group_threads INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_identities (
    binding_id TEXT PRIMARY KEY,
    channel_account_id TEXT NOT NULL REFERENCES channel_accounts(channel_account_id),
    platform_user_id TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    display_name TEXT,
    bound_by TEXT,
    bound_at TEXT DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT,
    UNIQUE (channel_account_id, platform_user_id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
    channel_message_id TEXT PRIMARY KEY,
    channel_account_id TEXT NOT NULL REFERENCES channel_accounts(channel_account_id),
    platform_message_id TEXT NOT NULL,
    platform_thread_id TEXT,
    platform_user_id TEXT,
    author_principal_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('message', 'edited', 'recalled', 'receipt')),
    text_content TEXT,
    attachments_json TEXT,
    status TEXT NOT NULL
      CHECK (status IN ('received', 'deduplicated', 'unmapped', 'rejected',
                        'processed', 'recalled')),
    raw_json TEXT,
    recall_of_message_id TEXT,
    edits_original_id TEXT,
    inbound_id TEXT,
    error_code TEXT,
    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT,
    UNIQUE (channel_account_id, platform_message_id)
);
"""

# --- S11 repair round (schema version 12) ----------------------------------
#
# This round does NOT rewrite business history. It adds the missing authority,
# delivery, approval, provenance and timezone columns needed to make the
# existing contracts enforceable:
#
# - approval_grants separates "a human approved this exact content" from
#   "an agent invoked the mutation".
# - delivery_attempts records every real network attempt behind one logical
#   delivery; deliveries.state carries the explicit transport state.
# - operation_results gains the initiating authority/project context so a
#   receipt can never be replayed across principals or project scopes.
# - resource_bookings gains UTC instants for overlap checks (raw text kept).
S11_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_grants (
    approval_id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL
      CHECK (proposal_type IN ('inventory_event', 'inventory_adjustment',
                               'memory_proposal')),
    proposal_id TEXT NOT NULL,
    project_id TEXT,
    allowed_action TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval_source TEXT NOT NULL CHECK (approval_source IN ('human', 'policy')),
    policy_version TEXT,
    status TEXT NOT NULL DEFAULT 'active'
      CHECK (status IN ('active', 'consumed', 'expired', 'revoked')),
    expires_at TEXT,
    consumed_by_operation_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    state TEXT NOT NULL
      CHECK (state IN ('sending', 'accepted', 'delivered', 'retryable_failure',
                       'failed_permanent', 'unknown', 'held', 'cancelled')),
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    transport_event_id TEXT,
    error_code TEXT,
    error_message TEXT,
    UNIQUE (delivery_id, attempt_no)
);
"""
