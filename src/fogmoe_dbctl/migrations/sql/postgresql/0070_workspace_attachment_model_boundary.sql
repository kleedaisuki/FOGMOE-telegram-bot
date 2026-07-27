-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief 切换历史附件模型边界前冻结所有可重放派生状态机 /
-- Freeze every replayable derived-state machine before changing the historical attachment model boundary.
-- Locks close the migration transaction's check-then-write window.  Operators must stop every
-- old Bot/worker binary before deployment and restart the new binary after commit: a lock cannot
-- prevent an old process from writing unsafe derivatives after this transaction ends.
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
           user_profile.dream_sources,
           conversation.group_message_projection
  IN SHARE ROW EXCLUSIVE MODE;

-- @brief 拒绝任何会在清理期间冻结旧文本快照的非终态工作 /
-- Reject non-terminal work that could freeze an old-text snapshot while cleanup is in progress.
DO $fogmoe_0070_drain_model_derivatives$
DECLARE
  nonterminal_inference_count BIGINT;
  nonterminal_compaction_count BIGINT;
  nonterminal_vector_count BIGINT;
  nonterminal_dream_count BIGINT;
BEGIN
  SELECT count(*)
  INTO nonterminal_inference_count
  FROM conversation.inference_activities
  WHERE status NOT IN ('completed', 'failed', 'cancelled');

  IF nonterminal_inference_count > 0 THEN
    RAISE EXCEPTION
      '0070 requires a drained inference queue; found % non-terminal conversation.inference_activities row(s)',
      nonterminal_inference_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*)
  INTO nonterminal_compaction_count
  FROM context_window.compactions
  WHERE status NOT IN ('completed', 'failed_final', 'cancelled');

  IF nonterminal_compaction_count > 0 THEN
    RAISE EXCEPTION
      '0070 requires drained context compactions; found % non-terminal context_window.compactions row(s)',
      nonterminal_compaction_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*)
  INTO nonterminal_vector_count
  FROM retrieval.passage_vectors
  WHERE status NOT IN ('completed', 'failed_final');

  IF nonterminal_vector_count > 0 THEN
    RAISE EXCEPTION
      '0070 requires a drained retrieval vector queue; found % non-terminal retrieval.passage_vectors row(s)',
      nonterminal_vector_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*)
  INTO nonterminal_dream_count
  FROM user_profile.dreams
  WHERE status NOT IN ('completed', 'failed_final');

  IF nonterminal_dream_count > 0 THEN
    RAISE EXCEPTION
      '0070 requires drained Profile Dreaming jobs; found % non-terminal user_profile.dreams row(s)',
      nonterminal_dream_count
      USING ERRCODE = '55000';
  END IF;
END;
$fogmoe_0070_drain_model_derivatives$;

-- @brief 识别所有升级前的 direct-media Turn；它们一律没有 native add_file receipt /
-- Identify every pre-upgrade direct-media Turn; none has a native add_file receipt.
-- A textual ``<workspace_file>`` placeholder cannot prove that bytes reached a RuntimeProcess:
-- canonical V2 messages permit multiple text/image parts, and old data has no receipt binding a
-- placeholder to an atomic native publish.  Do not infer an authorization fact from a string.
CREATE TEMP TABLE wspctl_0070_legacy_attachment_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0070_legacy_attachment_turns (turn_id, conversation_id)
SELECT DISTINCT message.turn_id, message.conversation_id
FROM conversation.conversation_messages AS message
WHERE message.role = 'user'
  AND message.turn_id IS NOT NULL
  AND COALESCE(
    message.content #>> '{media,kind}',
    message.content ->> 'content_kind'
  ) IN ('photo', 'sticker', 'document');

-- @brief 旧群上下文没有每次读取的 provenance，保守隔离所有历史群 Agent Turn /
-- Old group context has no per-read provenance, so conservatively isolate every historical group Agent Turn.
-- Before this migration, ``fetch_group_context`` could return a group-media caption only within an
-- Agent turn.  Its result was intentionally non-durable, so there is no trustworthy way to prove
-- which assistant reply, history snapshot, or retrieval passage saw it.  The smallest sound
-- taint set is therefore all completed-or-terminal assistant Turns whose durable request carries
-- a group scope.  Private Turns remain outside this set.
CREATE TEMP TABLE wspctl_0070_group_side_channel_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0070_group_side_channel_turns (turn_id, conversation_id)
SELECT activity.turn_id, activity.conversation_id
FROM conversation.inference_activities AS activity
WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'
  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'true';

-- @brief 计算私聊 direct-media 的后续 Assistant Turn 污染闭包 /
-- Compute the taint closure of Assistant Turns following private direct media.
-- A raw caption can be echoed by a later text-only reply, then enter retrieval/Profile state under
-- that later turn ID.  ``conversation_messages.sequence`` is the sole causal ordering used by
-- ContextWindow; listener ``received_at``/turn timestamps are intentionally not trusted here.
-- Group scope is handled separately above as an all-history closure.
CREATE TEMP TABLE wspctl_0070_private_attachment_descendant_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0070_private_attachment_descendant_turns (turn_id, conversation_id)
SELECT DISTINCT activity.turn_id, activity.conversation_id
FROM conversation.inference_activities AS activity
JOIN wspctl_0070_legacy_attachment_turns AS direct_media
  ON direct_media.conversation_id = activity.conversation_id
WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'
  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'false'
  AND EXISTS (
    SELECT 1
    FROM conversation.conversation_messages AS activity_message
    JOIN conversation.conversation_messages AS direct_media_message
      ON direct_media_message.turn_id = direct_media.turn_id
    WHERE activity_message.turn_id = activity.turn_id
      AND activity_message.role = 'user'
      AND activity_message.conversation_id = activity.conversation_id
      AND direct_media_message.role = 'user'
      AND direct_media_message.conversation_id = direct_media.conversation_id
      AND COALESCE(
        direct_media_message.content #>> '{media,kind}',
        direct_media_message.content ->> 'content_kind'
      ) IN ('photo', 'sticker', 'document')
      AND activity_message.sequence >= direct_media_message.sequence
  );

-- @brief 合并 direct-media、其私聊传播闭包与不可反向归因的群旁路 Turn /
-- Merge direct media, its private propagation closure, and group-side-channel Turns that cannot be traced backwards.
CREATE TEMP TABLE wspctl_0070_tainted_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO wspctl_0070_tainted_turns (turn_id, conversation_id)
SELECT turn_id, conversation_id
FROM wspctl_0070_legacy_attachment_turns
UNION
SELECT turn_id, conversation_id
FROM wspctl_0070_private_attachment_descendant_turns
UNION
SELECT turn_id, conversation_id
FROM wspctl_0070_group_side_channel_turns;

-- @brief 保留 append-only 审计原文，但排除同一受污染 Turn 的 user/assistant/tool 全链路 /
-- Retain append-only audit text, but exclude the full user/assistant/tool chain of the same tainted Turn.
-- An assistant response may already have echoed a caption, so changing only the source user row
-- would be insufficient.  ``project_conversation_message`` and durable history reads honor this
-- existing semantic marker.
UPDATE conversation.conversation_messages AS message
SET content = jsonb_set(
  message.content,
  '{exclude_from_assistant}',
  'true'::JSONB,
  true
)
FROM wspctl_0070_tainted_turns AS affected
WHERE message.turn_id = affected.turn_id
  AND NOT (message.content @> jsonb_build_object('exclude_from_assistant', TRUE));

-- @brief 删除受污染会话的全部 compaction 链，避免 summary/source_snapshot 回灌 raw 文本 /
-- Delete every compaction chain in tainted conversations so neither summary nor source_snapshot can reintroduce raw text.
UPDATE context_window.compactions AS compaction
SET predecessor_compaction_id = NULL
WHERE compaction.conversation_id IN (
  SELECT DISTINCT tainted.conversation_id
  FROM wspctl_0070_tainted_turns AS tainted
)
  AND compaction.predecessor_compaction_id IS NOT NULL;

DELETE FROM context_window.compactions AS compaction
WHERE compaction.conversation_id IN (
  SELECT DISTINCT tainted.conversation_id
  FROM wspctl_0070_tainted_turns AS tainted
);

-- @brief 删除受污染 Turn 的 episodic passage、vector 与 source marker /
-- Delete episodic passages, vectors, and source markers for tainted Turns.
-- Deleting passages cascades their pgvector work rows; deletion of the marker lets only safe
-- future source discovery project a Turn again (the new exclusion predicate rejects these Turns).
DELETE FROM retrieval.source_projections AS projection
WHERE projection.source_kind = 'conversation.turn'
  AND projection.source_id IN (
    SELECT tainted.turn_id
    FROM wspctl_0070_tainted_turns AS tainted
  );

DELETE FROM retrieval.passages AS passage
WHERE passage.source_kind = 'conversation.turn'
  AND passage.source_id IN (
    SELECT tainted.turn_id
    FROM wspctl_0070_tainted_turns AS tainted
  );

-- @brief 仅重建受污染用户的 Profile，而不突破其 forgotten_through 遗忘边界 /
-- Rebuild only tainted users' Profiles without crossing their existing forgotten_through boundary.
CREATE TEMP TABLE wspctl_0070_affected_profile_users (
  user_id BIGINT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO wspctl_0070_affected_profile_users (user_id)
SELECT DISTINCT evidence.owner_user_id
FROM user_profile.evidence_events AS evidence
JOIN wspctl_0070_tainted_turns AS affected
  ON affected.turn_id = evidence.source_turn_id;

-- dream_sources is deleted explicitly before jobs so no old evidence snapshot can survive a
-- deferred foreign-key edge; deleting dreams afterwards also removes any remaining rows by CASCADE.
DELETE FROM user_profile.dream_sources AS source
USING user_profile.dreams AS dream,
      wspctl_0070_affected_profile_users AS affected
WHERE source.dream_id = dream.dream_id
  AND dream.user_id = affected.user_id;

DELETE FROM user_profile.dreams AS dream
USING wspctl_0070_affected_profile_users AS affected
WHERE dream.user_id = affected.user_id;

-- Clear current_revision before deleting revisions; the FK is deferrable, but this makes the
-- intended empty profile state explicit throughout the transaction.
UPDATE user_profile.profiles AS profile
SET current_revision = NULL,
    observed_through_event_id = 0,
    next_eligible_at = CURRENT_TIMESTAMP,
    updated_at = GREATEST(profile.updated_at, CURRENT_TIMESTAMP)
FROM wspctl_0070_affected_profile_users AS affected
WHERE profile.user_id = affected.user_id;

DELETE FROM user_profile.profile_revisions AS revision
USING wspctl_0070_affected_profile_users AS affected
WHERE revision.user_id = affected.user_id;

DELETE FROM user_profile.evidence_events AS evidence
USING wspctl_0070_tainted_turns AS affected
WHERE evidence.source_turn_id = affected.turn_id;

-- @brief 群旁路没有 import receipt；历史媒体一律收束为非可执行标记 /
-- The group-observer side channel has no import receipt; reduce every historical media row to a non-actionable marker.
UPDATE conversation.group_message_projection
SET content = '<group_attachment />',
    content_encoding = 'plain',
    updated_at = GREATEST(updated_at, CURRENT_TIMESTAMP)
WHERE message_type IN ('photo', 'sticker', 'voice', 'video', 'document')
  AND (
    content IS DISTINCT FROM '<group_attachment />'
    OR content_encoding IS DISTINCT FROM 'plain'
  );

UPDATE conversation.group_message_projection
SET content = '[service message]',
    content_encoding = 'plain',
    updated_at = GREATEST(updated_at, CURRENT_TIMESTAMP)
WHERE message_type = 'other'
  AND (
    content IS DISTINCT FROM '[service message]'
    OR content_encoding IS DISTINCT FROM 'plain'
  );

-- Raw attachment audit rows remain deliberately intact.  They are neither translated into a
-- workspace path nor deleted: no historical add_file receipt establishes a file for runtime use.
-- After this transaction, restart the new Bot process so its in-memory ContextWindow cache cannot
-- retain a pre-migration compaction or raw-history projection.

-- migrate:down

DO $fogmoe_0070_irreversible$
BEGIN
  RAISE EXCEPTION
    '0070 is irreversible: restoring raw attachment text to model-derived state would fabricate unsafe workspace semantics'
    USING ERRCODE = '0A000';
END;
$fogmoe_0070_irreversible$;
