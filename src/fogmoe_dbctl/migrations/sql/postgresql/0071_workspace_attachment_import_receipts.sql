-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief 切换附件可见性状态机前冻结全部可能携带旧路径的派生物 /
-- Freeze every derivative that can carry an old attachment path before changing attachment visibility semantics.
-- Operators must stop old Bot/worker binaries before this migration and restart only the new
-- binary after commit.  The locks make the migration's own check-and-write sequence atomic; they
-- cannot prevent a stale process from publishing an unreceipted path after it releases its old
-- database connection.
LOCK TABLE conversation.conversation_turns,
           conversation.conversation_messages,
           conversation.inference_activities,
           assistant.tool_agent_steps,
           assistant.tool_effect_receipts,
           context_window.compactions,
           retrieval.source_projections,
           retrieval.passages,
           retrieval.passage_vectors,
           user_profile.evidence_events,
           user_profile.profiles,
           user_profile.profile_revisions,
           user_profile.dreams,
           user_profile.dream_sources
  IN SHARE ROW EXCLUSIVE MODE;

-- @brief 拒绝会在迁移中冻结 pending path 或旧 summary 的非终态工作 /
-- Reject non-terminal work that could freeze a pending path or old summary during this migration.
DO $fogmoe_0071_drain_attachment_derivatives$
DECLARE
  nonterminal_inference_count BIGINT;
  nonterminal_compaction_count BIGINT;
  nonterminal_vector_count BIGINT;
  nonterminal_dream_count BIGINT;
BEGIN
  SELECT count(*) INTO nonterminal_inference_count
  FROM conversation.inference_activities
  WHERE status NOT IN ('completed', 'failed', 'cancelled');
  IF nonterminal_inference_count > 0 THEN
    RAISE EXCEPTION
      '0071 requires a drained inference queue; found % non-terminal conversation.inference_activities row(s)',
      nonterminal_inference_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*) INTO nonterminal_compaction_count
  FROM context_window.compactions
  WHERE status NOT IN ('completed', 'failed_final', 'cancelled');
  IF nonterminal_compaction_count > 0 THEN
    RAISE EXCEPTION
      '0071 requires drained context compactions; found % non-terminal context_window.compactions row(s)',
      nonterminal_compaction_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*) INTO nonterminal_vector_count
  FROM retrieval.passage_vectors
  WHERE status NOT IN ('completed', 'failed_final');
  IF nonterminal_vector_count > 0 THEN
    RAISE EXCEPTION
      '0071 requires a drained retrieval vector queue; found % non-terminal retrieval.passage_vectors row(s)',
      nonterminal_vector_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*) INTO nonterminal_dream_count
  FROM user_profile.dreams
  WHERE status NOT IN ('completed', 'failed_final');
  IF nonterminal_dream_count > 0 THEN
    RAISE EXCEPTION
      '0071 requires drained Profile Dreaming jobs; found % non-terminal user_profile.dreams row(s)',
      nonterminal_dream_count
      USING ERRCODE = '55000';
  END IF;
END;
$fogmoe_0071_drain_attachment_derivatives$;

-- @brief 所有已有 marker、旧 media 或 rollout request 都没有本 revision 定义的 receipt /
-- Every existing marker, legacy media row, or rollout request lacks this revision's receipt.
-- Never infer a native write from a textual placeholder.  The temporary set includes all eight
-- accepted Telegram media kinds, because 0070 predates audio/animation/video-note ingress.
CREATE TEMP TABLE wspctl_0071_unreceipted_attachment_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0071_unreceipted_attachment_turns (turn_id, conversation_id)
SELECT DISTINCT message.turn_id, message.conversation_id
FROM conversation.conversation_messages AS message
LEFT JOIN conversation.inference_activities AS activity
  ON activity.turn_id = message.turn_id
WHERE message.role = 'user'
  AND message.turn_id IS NOT NULL
  AND (
    message.content ? 'workspace_attachment'
    OR COALESCE(
      message.content #>> '{media,kind}',
      message.content ->> 'content_kind'
    ) IN (
      'photo', 'sticker', 'document', 'voice', 'audio', 'video', 'animation', 'video_note'
    )
    OR jsonb_typeof(activity.request -> 'current_turn_upload') = 'object'
  );

-- @brief 旧/rollout 行显式终结为 unavailable，不伪造 path 或 receipt /
-- Terminalize old/rollout rows as unavailable without fabricating a path or receipt.
-- This happens before the transition guard is installed, because an old terminal activity may
-- legitimately have no current worker status capable of authorizing an unavailable transition.
UPDATE conversation.conversation_messages AS message
SET content = jsonb_set(
  message.content,
  '{workspace_attachment}',
  jsonb_build_object('version', 1, 'state', 'unavailable'),
  true
)
FROM wspctl_0071_unreceipted_attachment_turns AS affected
WHERE message.turn_id = affected.turn_id
  AND message.conversation_id = affected.conversation_id
  AND message.role = 'user';

-- @brief 为遗漏在 0070 前的新增媒体种类计算私聊/群聊的前向污染闭包 /
-- Compute private/group forward taint closures for media kinds added after 0070.
CREATE TEMP TABLE wspctl_0071_private_attachment_descendant_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0071_private_attachment_descendant_turns (turn_id, conversation_id)
SELECT DISTINCT activity.turn_id, activity.conversation_id
FROM conversation.inference_activities AS activity
JOIN wspctl_0071_unreceipted_attachment_turns AS direct_attachment
  ON direct_attachment.conversation_id = activity.conversation_id
WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'
  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'false'
  AND EXISTS (
    SELECT 1
    FROM conversation.conversation_messages AS activity_message
    JOIN conversation.conversation_messages AS attachment_message
      ON attachment_message.turn_id = direct_attachment.turn_id
     AND attachment_message.conversation_id = direct_attachment.conversation_id
     AND attachment_message.role = 'user'
    WHERE activity_message.turn_id = activity.turn_id
      AND activity_message.conversation_id = activity.conversation_id
      AND activity_message.role = 'user'
      AND activity_message.sequence >= attachment_message.sequence
  );

CREATE TEMP TABLE wspctl_0071_group_attachment_descendant_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0071_group_attachment_descendant_turns (turn_id, conversation_id)
SELECT DISTINCT activity.turn_id, activity.conversation_id
FROM conversation.inference_activities AS activity
JOIN wspctl_0071_unreceipted_attachment_turns AS direct_attachment
  ON direct_attachment.conversation_id = activity.conversation_id
WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'
  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'true'
  AND EXISTS (
    SELECT 1
    FROM conversation.conversation_messages AS activity_message
    JOIN conversation.conversation_messages AS attachment_message
      ON attachment_message.turn_id = direct_attachment.turn_id
     AND attachment_message.conversation_id = direct_attachment.conversation_id
     AND attachment_message.role = 'user'
    WHERE activity_message.turn_id = activity.turn_id
      AND activity_message.conversation_id = activity.conversation_id
      AND activity_message.role = 'user'
      AND activity_message.sequence >= attachment_message.sequence
  );

CREATE TEMP TABLE wspctl_0071_tainted_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0071_tainted_turns (turn_id, conversation_id)
SELECT turn_id, conversation_id FROM wspctl_0071_unreceipted_attachment_turns
UNION
SELECT turn_id, conversation_id FROM wspctl_0071_private_attachment_descendant_turns
UNION
SELECT turn_id, conversation_id FROM wspctl_0071_group_attachment_descendant_turns;

-- @brief 审计行保留，所有可能看过 raw/pending 内容的同 Turn 派生面一律排除 /
-- Preserve audit rows while excluding every same-turn surface that could have observed raw/pending content.
UPDATE conversation.conversation_messages AS message
SET content = jsonb_set(
  message.content,
  '{exclude_from_assistant}',
  'true'::JSONB,
  true
)
FROM wspctl_0071_tainted_turns AS affected
WHERE message.turn_id = affected.turn_id
  AND NOT (message.content @> jsonb_build_object('exclude_from_assistant', TRUE));

UPDATE context_window.compactions AS compaction
SET predecessor_compaction_id = NULL
WHERE compaction.conversation_id IN (
  SELECT DISTINCT tainted.conversation_id FROM wspctl_0071_tainted_turns AS tainted
)
  AND compaction.predecessor_compaction_id IS NOT NULL;

DELETE FROM context_window.compactions AS compaction
WHERE compaction.conversation_id IN (
  SELECT DISTINCT tainted.conversation_id FROM wspctl_0071_tainted_turns AS tainted
);

DELETE FROM retrieval.source_projections AS projection
WHERE projection.source_kind = 'conversation.turn'
  AND projection.source_id IN (SELECT turn_id FROM wspctl_0071_tainted_turns);

DELETE FROM retrieval.passages AS passage
WHERE passage.source_kind = 'conversation.turn'
  AND passage.source_id IN (SELECT turn_id FROM wspctl_0071_tainted_turns);

CREATE TEMP TABLE wspctl_0071_affected_profile_users (
  user_id BIGINT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO wspctl_0071_affected_profile_users (user_id)
SELECT DISTINCT evidence.owner_user_id
FROM user_profile.evidence_events AS evidence
JOIN wspctl_0071_tainted_turns AS affected
  ON affected.turn_id = evidence.source_turn_id;

DELETE FROM user_profile.dream_sources AS source
USING user_profile.dreams AS dream,
      wspctl_0071_affected_profile_users AS affected
WHERE source.dream_id = dream.dream_id
  AND dream.user_id = affected.user_id;

DELETE FROM user_profile.dreams AS dream
USING wspctl_0071_affected_profile_users AS affected
WHERE dream.user_id = affected.user_id;

UPDATE user_profile.profiles AS profile
SET current_revision = NULL,
    observed_through_event_id = 0,
    next_eligible_at = CURRENT_TIMESTAMP,
    updated_at = GREATEST(profile.updated_at, CURRENT_TIMESTAMP)
FROM wspctl_0071_affected_profile_users AS affected
WHERE profile.user_id = affected.user_id;

DELETE FROM user_profile.profile_revisions AS revision
USING wspctl_0071_affected_profile_users AS affected
WHERE revision.user_id = affected.user_id;

DELETE FROM user_profile.evidence_events AS evidence
USING wspctl_0071_tainted_turns AS affected
WHERE evidence.source_turn_id = affected.turn_id;

-- @brief durable receipt 仅描述当前附件 native publish，不存 provider capability 或 bytes /
-- A durable receipt describes only current-attachment native publication; it stores neither provider capabilities nor bytes.
CREATE TABLE workspace.attachment_import_receipts (
  turn_id UUID PRIMARY KEY
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE RESTRICT,
  conversation_id TEXT NOT NULL CHECK (char_length(conversation_id) BETWEEN 1 AND 512),
  source_message_id UUID NOT NULL UNIQUE
    REFERENCES conversation.conversation_messages(message_id) ON DELETE RESTRICT,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal', 'group')),
  scope_id BIGINT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:attachment-import$'
  ),
  request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  runtime_path TEXT NOT NULL CHECK (
    runtime_path ~ '^/workspace/uploads/attachment-[0-9a-f]{64}/payload$'
  ),
  byte_size BIGINT NOT NULL CHECK (byte_size >= 0 AND byte_size <= 8388608),
  sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT workspace_attachment_import_receipts_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0)
    OR (scope_kind = 'group' AND scope_id <> 0)
  ),
  CONSTRAINT workspace_attachment_import_receipts_request_id_ck CHECK (
    request_id = turn_id::TEXT || ':attachment-import'
  )
);

COMMENT ON TABLE workspace.attachment_import_receipts IS
  'Immutable witness that a current-turn payload was published by RuntimeProcess.add_file before the path became model-visible.';
COMMENT ON COLUMN workspace.attachment_import_receipts.runtime_path IS
  'Runtime-internal fixed upload path, never a host path or user-controlled filename.';

-- @brief receipt 插入时再次绑定 source user 行、activity request、scope 与 pending placeholder /
-- On receipt insert, bind the source user row, activity request, scope, and pending placeholder again.
CREATE FUNCTION workspace.validate_attachment_import_receipt()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_receipt$
DECLARE
  source_row RECORD;
  activity_request JSONB;
  expected_scope_kind TEXT;
  expected_scope_id BIGINT;
BEGIN
  SELECT message.turn_id, message.conversation_id, message.role, message.content
  INTO source_row
  FROM conversation.conversation_messages AS message
  WHERE message.message_id = NEW.source_message_id;

  IF NOT FOUND
     OR source_row.turn_id IS DISTINCT FROM NEW.turn_id
     OR source_row.conversation_id IS DISTINCT FROM NEW.conversation_id
     OR source_row.role IS DISTINCT FROM 'user'
     OR EXISTS (
       SELECT 1
       FROM conversation.conversation_messages AS other_user
       WHERE other_user.turn_id = NEW.turn_id
         AND other_user.role = 'user'
         AND other_user.message_id <> NEW.source_message_id
     ) THEN
    RAISE EXCEPTION
      'workspace attachment receipt source message does not own the stated turn/conversation'
      USING ERRCODE = '23514';
  END IF;

  IF jsonb_typeof(source_row.content -> 'workspace_attachment') IS DISTINCT FROM 'object'
     OR jsonb_typeof(source_row.content #> '{workspace_attachment,version}') IS DISTINCT FROM 'number'
     OR source_row.content #>> '{workspace_attachment,version}' IS DISTINCT FROM '1'
     OR jsonb_typeof(source_row.content #> '{workspace_attachment,state}') IS DISTINCT FROM 'string'
     OR source_row.content #>> '{workspace_attachment,state}' IS DISTINCT FROM 'pending'
     OR source_row.content ->> 'text' IS DISTINCT FROM format('<workspace_file path="%s" />', NEW.runtime_path)
     OR source_row.content -> 'model_message' IS DISTINCT FROM jsonb_build_object(
       'schema_version', 2,
       'role', 'user',
       'parts', jsonb_build_array(jsonb_build_object(
         'type', 'text',
         'text', format('<workspace_file path="%s" />', NEW.runtime_path)
       )),
       'policy', jsonb_build_object('include_in_context', TRUE),
       'meta', '{}'::JSONB
     ) THEN
    RAISE EXCEPTION
      'workspace attachment receipt requires the exact pending source placeholder and canonical model message'
      USING ERRCODE = '23514';
  END IF;

  SELECT activity.request INTO activity_request
  FROM conversation.inference_activities AS activity
  WHERE activity.turn_id = NEW.turn_id
    AND activity.conversation_id = NEW.conversation_id;
  IF NOT FOUND
     OR jsonb_typeof(activity_request -> 'current_turn_upload') IS DISTINCT FROM 'object'
     OR jsonb_typeof(activity_request #> '{scope,is_group}') IS DISTINCT FROM 'boolean'
     OR jsonb_typeof(activity_request #> '{scope,message_id}') IS DISTINCT FROM 'number'
     OR jsonb_typeof(activity_request #> '{current_turn_upload,source_message_id}') IS DISTINCT FROM 'number'
     OR (activity_request #>> '{scope,message_id}' ~ '^[1-9][0-9]*$') IS NOT TRUE
     OR (activity_request #>> '{current_turn_upload,source_message_id}' ~ '^[1-9][0-9]*$') IS NOT TRUE
     OR activity_request #>> '{scope,message_id}'
        IS DISTINCT FROM activity_request #>> '{current_turn_upload,source_message_id}' THEN
    RAISE EXCEPTION
      'workspace attachment receipt requires a matching durable current_turn_upload request'
      USING ERRCODE = '23514';
  END IF;

  IF activity_request #>> '{scope,is_group}' = 'true' THEN
    IF jsonb_typeof(activity_request #> '{scope,group_id}') IS DISTINCT FROM 'number'
       OR (activity_request #>> '{scope,group_id}' ~ '^-?[1-9][0-9]*$') IS NOT TRUE THEN
      RAISE EXCEPTION
        'workspace attachment receipt group scope is malformed'
        USING ERRCODE = '23514';
    END IF;
    expected_scope_kind := 'group';
    expected_scope_id := CAST(activity_request #>> '{scope,group_id}' AS BIGINT);
  ELSE
    IF jsonb_typeof(activity_request #> '{user,user_id}') IS DISTINCT FROM 'number'
       OR (activity_request #>> '{user,user_id}' ~ '^[1-9][0-9]*$') IS NOT TRUE THEN
      RAISE EXCEPTION
        'workspace attachment receipt personal scope is malformed'
        USING ERRCODE = '23514';
    END IF;
    expected_scope_kind := 'personal';
    expected_scope_id := CAST(activity_request #>> '{user,user_id}' AS BIGINT);
  END IF;

  IF NEW.scope_kind IS DISTINCT FROM expected_scope_kind
     OR NEW.scope_id IS DISTINCT FROM expected_scope_id THEN
    RAISE EXCEPTION
      'workspace attachment receipt scope does not match its durable command'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$fogmoe_workspace_attachment_receipt$;

CREATE TRIGGER workspace_attachment_import_receipts_validate_tr
BEFORE INSERT ON workspace.attachment_import_receipts
FOR EACH ROW EXECUTE FUNCTION workspace.validate_attachment_import_receipt();

-- @brief receipt 是 native publish 的不可变审计事实，禁止更新和删除 /
-- A receipt is an immutable native-publication audit fact; prohibit updates and deletes.
CREATE FUNCTION workspace.forbid_attachment_import_receipt_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_receipt_immutable$
BEGIN
  RAISE EXCEPTION
    'workspace.attachment_import_receipts is immutable'
    USING ERRCODE = '55000';
END;
$fogmoe_workspace_attachment_receipt_immutable$;

CREATE TRIGGER workspace_attachment_import_receipts_immutable_tr
BEFORE UPDATE OR DELETE ON workspace.attachment_import_receipts
FOR EACH ROW EXECUTE FUNCTION workspace.forbid_attachment_import_receipt_mutation();

-- @brief 同一事务提交前必须已把 receipt 的 source marker 发布为 imported /
-- Before the same transaction commits, a receipt's source marker must have been published as imported.
CREATE FUNCTION workspace.require_imported_attachment_receipt_source()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_receipt_commit$
DECLARE
  source_content JSONB;
BEGIN
  SELECT message.content INTO source_content
  FROM conversation.conversation_messages AS message
  WHERE message.message_id = NEW.source_message_id
    AND message.turn_id = NEW.turn_id
    AND message.conversation_id = NEW.conversation_id;

  IF NOT FOUND
     OR jsonb_typeof(source_content -> 'workspace_attachment') IS DISTINCT FROM 'object'
     OR jsonb_typeof(source_content #> '{workspace_attachment,version}') IS DISTINCT FROM 'number'
     OR source_content #>> '{workspace_attachment,version}' IS DISTINCT FROM '1'
     OR jsonb_typeof(source_content #> '{workspace_attachment,state}') IS DISTINCT FROM 'string'
     OR source_content #>> '{workspace_attachment,state}' IS DISTINCT FROM 'imported'
     OR source_content ->> 'text' IS DISTINCT FROM format('<workspace_file path="%s" />', NEW.runtime_path)
     OR source_content -> 'model_message' IS DISTINCT FROM jsonb_build_object(
       'schema_version', 2,
       'role', 'user',
       'parts', jsonb_build_array(jsonb_build_object(
         'type', 'text',
         'text', format('<workspace_file path="%s" />', NEW.runtime_path)
       )),
       'policy', jsonb_build_object('include_in_context', TRUE),
       'meta', '{}'::JSONB
     ) THEN
    RAISE EXCEPTION
      'workspace attachment receipt must commit with its source marker imported and canonical model message'
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$fogmoe_workspace_attachment_receipt_commit$;

CREATE CONSTRAINT TRIGGER workspace_attachment_import_receipts_commit_tr
AFTER INSERT ON workspace.attachment_import_receipts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION workspace.require_imported_attachment_receipt_source();

-- @brief 附件 marker 只有 pending→imported 或 pending→unavailable 两条合法边 /
-- The attachment marker has only two legal edges: pending→imported or pending→unavailable.
CREATE FUNCTION workspace.guard_attachment_visibility_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_visibility$
BEGIN
  IF OLD.content = NEW.content THEN
    RETURN NEW;
  END IF;

  IF OLD.role IS DISTINCT FROM 'user'
     OR NEW.role IS DISTINCT FROM 'user'
     OR OLD.turn_id IS NULL
     OR NEW.turn_id IS DISTINCT FROM OLD.turn_id
     OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
     OR EXISTS (
       SELECT 1
       FROM conversation.conversation_messages AS other_user
       WHERE other_user.turn_id = NEW.turn_id
         AND other_user.role = 'user'
         AND other_user.message_id <> NEW.message_id
     ) THEN
    RAISE EXCEPTION
      'workspace attachment marker requires the sole user source message of its turn'
      USING ERRCODE = '23514';
  END IF;

  IF jsonb_typeof(OLD.content -> 'workspace_attachment') IS DISTINCT FROM 'object'
     OR jsonb_typeof(OLD.content #> '{workspace_attachment,version}') IS DISTINCT FROM 'number'
     OR OLD.content #>> '{workspace_attachment,version}' IS DISTINCT FROM '1'
     OR jsonb_typeof(OLD.content #> '{workspace_attachment,state}') IS DISTINCT FROM 'string'
     OR OLD.content #>> '{workspace_attachment,state}' IS DISTINCT FROM 'pending' THEN
    RAISE EXCEPTION
      'workspace attachment marker is immutable outside a valid pending transition'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.content = jsonb_set(
       OLD.content,
       '{workspace_attachment,state}',
       to_jsonb('imported'::TEXT),
       false
     ) THEN
    PERFORM 1
    FROM workspace.attachment_import_receipts AS receipt
    WHERE receipt.turn_id = NEW.turn_id
      AND receipt.conversation_id = NEW.conversation_id
      AND receipt.source_message_id = NEW.message_id
      AND NEW.content ->> 'text' = format('<workspace_file path="%s" />', receipt.runtime_path);
    IF NOT FOUND THEN
      RAISE EXCEPTION
        'workspace attachment imported marker requires its matching durable receipt'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.content = jsonb_set(
       OLD.content,
       '{workspace_attachment,state}',
       to_jsonb('unavailable'::TEXT),
       false
     ) THEN
    PERFORM 1
    FROM conversation.inference_activities AS activity
    WHERE activity.turn_id = NEW.turn_id
      AND activity.conversation_id = NEW.conversation_id
      AND activity.status = 'failed'
      AND jsonb_typeof(activity.request -> 'current_turn_upload') IS NOT DISTINCT FROM 'object'
      AND jsonb_typeof(activity.request #> '{scope,is_group}') IS NOT DISTINCT FROM 'boolean'
      AND jsonb_typeof(activity.request #> '{scope,message_id}') IS NOT DISTINCT FROM 'number'
      AND jsonb_typeof(activity.request #> '{current_turn_upload,source_message_id}')
          IS NOT DISTINCT FROM 'number'
      AND (activity.request #>> '{scope,message_id}' ~ '^[1-9][0-9]*$') IS TRUE
      AND (activity.request #>> '{current_turn_upload,source_message_id}' ~ '^[1-9][0-9]*$') IS TRUE
      AND activity.request #>> '{scope,message_id}'
          IS NOT DISTINCT FROM activity.request #>> '{current_turn_upload,source_message_id}'
      -- Keep the same personal/group scope grammar as receipt publication.  A structurally
      -- malformed failed request cannot terminalize a pending attachment that it cannot prove
      -- it owns.
      AND (
        (
          activity.request #>> '{scope,is_group}' = 'true'
          AND jsonb_typeof(activity.request #> '{scope,group_id}') = 'number'
          AND (activity.request #>> '{scope,group_id}' ~ '^-?[1-9][0-9]*$') IS TRUE
        )
        OR (
          activity.request #>> '{scope,is_group}' = 'false'
          AND jsonb_typeof(activity.request #> '{user,user_id}') = 'number'
          AND (activity.request #>> '{user,user_id}' ~ '^[1-9][0-9]*$') IS TRUE
        )
      );
    IF NOT FOUND OR EXISTS (
      SELECT 1
      FROM workspace.attachment_import_receipts AS receipt
      WHERE receipt.turn_id = NEW.turn_id
    ) THEN
      RAISE EXCEPTION
        'workspace attachment unavailable marker requires final attachment failure without a receipt'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'workspace attachment marker permits only pending-to-imported or pending-to-unavailable'
    USING ERRCODE = '23514';
END;
$fogmoe_workspace_attachment_visibility$;

CREATE TRIGGER conversation_messages_workspace_attachment_visibility_tr
BEFORE UPDATE OF content ON conversation.conversation_messages
FOR EACH ROW
WHEN (OLD.content ? 'workspace_attachment' OR NEW.content ? 'workspace_attachment')
EXECUTE FUNCTION workspace.guard_attachment_visibility_transition();

-- No direct application-role GRANT appears here.  fogmoe-dbctl's controlled access-policy
-- convergence owns the minimum workspace-schema DML grant after this revision.

-- migrate:down

DO $fogmoe_0071_irreversible$
BEGIN
  RAISE EXCEPTION
    '0071 is irreversible: deleting attachment receipts or reopening unavailable markers would fabricate native publication semantics'
    USING ERRCODE = '0A000';
END;
$fogmoe_0071_irreversible$;
