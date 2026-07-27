-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief 旧 worker 不知道 intent gate，必须在升级前停止；锁使 receipt backfill 与新 gate 原子可见 /
-- Stop old workers before this upgrade because they do not know the intent gate; locks make receipt backfill and the new gate atomically visible.
LOCK TABLE workspace.attachment_import_receipts,
           conversation.conversation_messages,
           conversation.inference_activities
  IN SHARE ROW EXCLUSIVE MODE;

-- @brief intent 是 native add_file 之前的不可变恢复聚合；不保存 provider capability 或 payload bytes /
-- An intent is the immutable recovery aggregate before native add_file; it stores neither a provider capability nor payload bytes.
CREATE TABLE workspace.attachment_import_intents (
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
  prepared_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT workspace_attachment_import_intents_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0)
    OR (scope_kind = 'group' AND scope_id <> 0)
  ),
  CONSTRAINT workspace_attachment_import_intents_request_id_ck CHECK (
    request_id = turn_id::TEXT || ':attachment-import'
  )
);

COMMENT ON TABLE workspace.attachment_import_intents IS
  'Immutable AttachmentImportIntent aggregate committed before RuntimeProcess.add_file; bridges durable source semantics to native payload-journal recovery.';
COMMENT ON COLUMN workspace.attachment_import_intents.runtime_path IS
  'Runtime-internal fixed upload path, never a host path or user-controlled filename.';
COMMENT ON COLUMN workspace.attachment_import_intents.prepared_at IS
  'Intent preparation time; legacy receipt-derived rows use the already durable receipt timestamp during 0072 backfill.';

-- @brief 已部署 0071 的 receipt 已是 native publication 的 durable witness；用完全相同字段回填 intent /
-- Existing deployed-0071 receipts are already durable native-publication witnesses; backfill intents with exactly the same fields.
-- This is a migration provenance bridge, not a claim that a pre-0072 worker had an intent table
-- before it called native add_file.
INSERT INTO workspace.attachment_import_intents (
  turn_id, conversation_id, source_message_id, scope_kind, scope_id,
  request_id, request_hash, runtime_path, byte_size, sha256, prepared_at
)
SELECT
  receipt.turn_id,
  receipt.conversation_id,
  receipt.source_message_id,
  receipt.scope_kind,
  receipt.scope_id,
  receipt.request_id,
  receipt.request_hash,
  receipt.runtime_path,
  receipt.byte_size,
  receipt.sha256,
  receipt.imported_at
FROM workspace.attachment_import_receipts AS receipt;

-- @brief intent 与 receipt 共用的 source/placeholder/activity/scope 绑定；新 intent 只能在 pending 时插入 /
-- Source/placeholder/activity/scope binding shared by intent and receipt; a new intent may be inserted only while pending.
CREATE FUNCTION workspace.validate_attachment_import_binding(
  import_turn_id UUID,
  import_conversation_id TEXT,
  import_source_message_id UUID,
  import_scope_kind TEXT,
  import_scope_id BIGINT,
  import_runtime_path TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_binding$
DECLARE
  source_row RECORD;
  activity_request JSONB;
  expected_scope_kind TEXT;
  expected_scope_id BIGINT;
BEGIN
  SELECT message.turn_id, message.conversation_id, message.role, message.content
  INTO source_row
  FROM conversation.conversation_messages AS message
  WHERE message.message_id = import_source_message_id;

  IF NOT FOUND
     OR source_row.turn_id IS DISTINCT FROM import_turn_id
     OR source_row.conversation_id IS DISTINCT FROM import_conversation_id
     OR source_row.role IS DISTINCT FROM 'user'
     OR EXISTS (
       SELECT 1
       FROM conversation.conversation_messages AS other_user
       WHERE other_user.turn_id = import_turn_id
         AND other_user.role = 'user'
         AND other_user.message_id <> import_source_message_id
     ) THEN
    RAISE EXCEPTION
      'workspace attachment import source message does not own the stated turn/conversation'
      USING ERRCODE = '23514';
  END IF;

  IF jsonb_typeof(source_row.content -> 'workspace_attachment') IS DISTINCT FROM 'object'
     OR jsonb_typeof(source_row.content #> '{workspace_attachment,version}') IS DISTINCT FROM 'number'
     OR source_row.content #>> '{workspace_attachment,version}' IS DISTINCT FROM '1'
     OR jsonb_typeof(source_row.content #> '{workspace_attachment,state}') IS DISTINCT FROM 'string'
     OR source_row.content #>> '{workspace_attachment,state}' IS DISTINCT FROM 'pending'
     OR source_row.content ->> 'text' IS DISTINCT FROM format('<workspace_file path="%s" />', import_runtime_path)
     OR source_row.content -> 'model_message' IS DISTINCT FROM jsonb_build_object(
       'schema_version', 2,
       'role', 'user',
       'parts', jsonb_build_array(jsonb_build_object(
         'type', 'text',
         'text', format('<workspace_file path="%s" />', import_runtime_path)
       )),
       'policy', jsonb_build_object('include_in_context', TRUE),
       'meta', '{}'::JSONB
     ) THEN
    RAISE EXCEPTION
      'workspace attachment import requires the exact pending source placeholder and canonical model message'
      USING ERRCODE = '23514';
  END IF;

  SELECT activity.request INTO activity_request
  FROM conversation.inference_activities AS activity
  WHERE activity.turn_id = import_turn_id
    AND activity.conversation_id = import_conversation_id;
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
      'workspace attachment import requires a matching durable current_turn_upload request'
      USING ERRCODE = '23514';
  END IF;

  IF activity_request #>> '{scope,is_group}' = 'true' THEN
    IF jsonb_typeof(activity_request #> '{scope,group_id}') IS DISTINCT FROM 'number'
       OR (activity_request #>> '{scope,group_id}' ~ '^-?[1-9][0-9]*$') IS NOT TRUE THEN
      RAISE EXCEPTION
        'workspace attachment import group scope is malformed'
        USING ERRCODE = '23514';
    END IF;
    expected_scope_kind := 'group';
    expected_scope_id := CAST(activity_request #>> '{scope,group_id}' AS BIGINT);
  ELSE
    IF jsonb_typeof(activity_request #> '{user,user_id}') IS DISTINCT FROM 'number'
       OR (activity_request #>> '{user,user_id}' ~ '^[1-9][0-9]*$') IS NOT TRUE THEN
      RAISE EXCEPTION
        'workspace attachment import personal scope is malformed'
        USING ERRCODE = '23514';
    END IF;
    expected_scope_kind := 'personal';
    expected_scope_id := CAST(activity_request #>> '{user,user_id}' AS BIGINT);
  END IF;

  IF import_scope_kind IS DISTINCT FROM expected_scope_kind
     OR import_scope_id IS DISTINCT FROM expected_scope_id THEN
    RAISE EXCEPTION
      'workspace attachment import scope does not match its durable command'
      USING ERRCODE = '23514';
  END IF;
END;
$fogmoe_workspace_attachment_binding$;

CREATE FUNCTION workspace.validate_attachment_import_intent()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_intent$
BEGIN
  PERFORM workspace.validate_attachment_import_binding(
    NEW.turn_id,
    NEW.conversation_id,
    NEW.source_message_id,
    NEW.scope_kind,
    NEW.scope_id,
    NEW.runtime_path
  );
  RETURN NEW;
END;
$fogmoe_workspace_attachment_intent$;

CREATE TRIGGER workspace_attachment_import_intents_validate_tr
BEFORE INSERT ON workspace.attachment_import_intents
FOR EACH ROW EXECUTE FUNCTION workspace.validate_attachment_import_intent();

-- @brief intent 是恢复边界的审计事实，禁止更新和删除 /
-- An intent is an audit fact at the recovery boundary; prohibit updates and deletes.
CREATE FUNCTION workspace.forbid_attachment_import_intent_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_intent_immutable$
BEGIN
  RAISE EXCEPTION
    'workspace.attachment_import_intents is immutable'
    USING ERRCODE = '55000';
END;
$fogmoe_workspace_attachment_intent_immutable$;

CREATE TRIGGER workspace_attachment_import_intents_immutable_tr
BEFORE UPDATE OR DELETE ON workspace.attachment_import_intents
FOR EACH ROW EXECUTE FUNCTION workspace.forbid_attachment_import_intent_mutation();

-- @brief 0071 receipt gate 改为必须精确引用先前 intent，receipt 仍只允许从 pending source publish /
-- Replace the 0071 receipt gate so a receipt must exactly reference a prior intent, while receipt publication still requires a pending source.
CREATE OR REPLACE FUNCTION workspace.validate_attachment_import_receipt()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_attachment_receipt$
BEGIN
  PERFORM workspace.validate_attachment_import_binding(
    NEW.turn_id,
    NEW.conversation_id,
    NEW.source_message_id,
    NEW.scope_kind,
    NEW.scope_id,
    NEW.runtime_path
  );

  PERFORM 1
  FROM workspace.attachment_import_intents AS intent
  WHERE intent.turn_id = NEW.turn_id
    AND intent.conversation_id = NEW.conversation_id
    AND intent.source_message_id = NEW.source_message_id
    AND intent.scope_kind = NEW.scope_kind
    AND intent.scope_id = NEW.scope_id
    AND intent.request_id = NEW.request_id
    AND intent.request_hash = NEW.request_hash
    AND intent.runtime_path = NEW.runtime_path
    AND intent.byte_size = NEW.byte_size
    AND intent.sha256 = NEW.sha256;
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'workspace attachment receipt requires its exact previously prepared import intent'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$fogmoe_workspace_attachment_receipt$;

-- @brief intent 已提交即表示 source 可恢复，禁止把它终结为 unavailable；imported 仍须 receipt /
-- A committed intent means the source remains recoverable, so it cannot be terminalized unavailable; imported still requires a receipt.
CREATE OR REPLACE FUNCTION workspace.guard_attachment_visibility_transition()
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
    ) OR EXISTS (
      SELECT 1
      FROM workspace.attachment_import_intents AS intent
      WHERE intent.turn_id = NEW.turn_id
    ) THEN
      RAISE EXCEPTION
        'workspace attachment unavailable marker requires final attachment failure without a receipt or prepared intent'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'workspace attachment marker permits only pending-to-imported or pending-to-unavailable'
    USING ERRCODE = '23514';
END;
$fogmoe_workspace_attachment_visibility$;

-- migrate:down

DO $fogmoe_0072_irreversible$
BEGIN
  RAISE EXCEPTION
    '0072 is irreversible: deleting attachment import intents would lose native-recovery provenance'
    USING ERRCODE = '0A000';
END;
$fogmoe_0072_irreversible$;
