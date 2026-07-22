-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief 迁移前拒绝仍可能以旧 JSON 重放的持久化工作 / Reject durable work that could still replay old JSON before migration.
DO $fogmoe_0068_drain$
DECLARE
  nonterminal_inference_count BIGINT;
  nonterminal_effect_count BIGINT;
  nonterminal_compaction_count BIGINT;
BEGIN
  SELECT count(*)
  INTO nonterminal_inference_count
  FROM conversation.inference_activities
  WHERE status NOT IN ('completed', 'failed', 'cancelled');

  IF nonterminal_inference_count > 0 THEN
    RAISE EXCEPTION
      '0068 requires a drained inference queue; found % non-terminal conversation.inference_activities row(s)',
      nonterminal_inference_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*)
  INTO nonterminal_effect_count
  FROM assistant.tool_effect_receipts
  WHERE status NOT IN ('succeeded', 'failed_final');

  IF nonterminal_effect_count > 0 THEN
    RAISE EXCEPTION
      '0068 requires drained tool effects; found % pending or processing assistant.tool_effect_receipts row(s)',
      nonterminal_effect_count
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*)
  INTO nonterminal_compaction_count
  FROM context_window.compactions
  WHERE status NOT IN ('completed', 'failed_final', 'cancelled');

  IF nonterminal_compaction_count > 0 THEN
    RAISE EXCEPTION
      '0068 requires a drained compaction queue; found % non-terminal context_window.compactions row(s)',
      nonterminal_compaction_count
      USING ERRCODE = '55000';
  END IF;
END;
$fogmoe_0068_drain$;

-- @brief 解析旧 function.arguments 并强制 JSON object / Parse legacy function.arguments and require a JSON object.
CREATE FUNCTION conversation.canonical_tool_arguments_v2(
  arguments_value JSONB,
  location TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $fogmoe_0068_arguments$
DECLARE
  raw_arguments TEXT;
  normalized_arguments JSONB;
BEGIN
  IF jsonb_typeof(arguments_value) = 'string' THEN
    raw_arguments := arguments_value #>> '{}';
    BEGIN
      normalized_arguments := raw_arguments::JSONB;
    EXCEPTION
      WHEN invalid_text_representation THEN
        RAISE EXCEPTION
          'malformed legacy tool arguments at %; a JSON string was required',
          location
          USING ERRCODE = '22023';
    END;
  ELSE
    normalized_arguments := arguments_value;
  END IF;

  IF jsonb_typeof(normalized_arguments) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION
      'legacy tool arguments at % must decode to a JSON object',
      location
      USING ERRCODE = '22023';
  END IF;
  RETURN normalized_arguments;
END;
$fogmoe_0068_arguments$;

-- @brief 将旧 OpenAI content 转为 canonical text/image parts / Convert legacy OpenAI content into canonical text/image parts.
CREATE FUNCTION conversation.canonical_content_parts_v2(
  content_value JSONB,
  location TEXT,
  allow_null BOOLEAN
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $fogmoe_0068_content$
DECLARE
  content_kind TEXT;
  part_record RECORD;
  part_type TEXT;
  image_value JSONB;
  image_url TEXT;
  result JSONB := '[]'::JSONB;
BEGIN
  IF content_value IS NULL OR jsonb_typeof(content_value) = 'null' THEN
    IF allow_null THEN
      RETURN result;
    END IF;
    RAISE EXCEPTION
      'missing or null legacy content at %',
      location
      USING ERRCODE = '22023';
  END IF;

  content_kind := jsonb_typeof(content_value);
  IF content_kind = 'string' THEN
    RETURN jsonb_build_array(
      jsonb_build_object(
        'type', 'text',
        'text', content_value #>> '{}'
      )
    );
  END IF;

  IF content_kind <> 'array' THEN
    RAISE EXCEPTION
      'legacy content at % must be a string, an OpenAI content array, or null for an assistant tool call',
      location
      USING ERRCODE = '22023';
  END IF;

  FOR part_record IN
    SELECT expanded.value, expanded.ordinality
    FROM jsonb_array_elements(content_value)
      WITH ORDINALITY AS expanded(value, ordinality)
    ORDER BY expanded.ordinality
  LOOP
    IF jsonb_typeof(part_record.value) <> 'object' THEN
      RAISE EXCEPTION
        'legacy content part at %[%] must be an object',
        location,
        part_record.ordinality
        USING ERRCODE = '22023';
    END IF;

    part_type := part_record.value ->> 'type';
    IF part_type = 'text' THEN
      IF jsonb_typeof(part_record.value -> 'text') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION
          'legacy text content part at %[%] must contain a string text field',
          location,
          part_record.ordinality
          USING ERRCODE = '22023';
      END IF;
      result := result || jsonb_build_array(
        jsonb_build_object(
          'type', 'text',
          'text', part_record.value ->> 'text'
        )
      );
    ELSIF part_type = 'image_url' THEN
      image_value := part_record.value -> 'image_url';
      IF jsonb_typeof(image_value) = 'string' THEN
        image_url := image_value #>> '{}';
      ELSIF jsonb_typeof(image_value) = 'object'
        AND jsonb_typeof(image_value -> 'url') = 'string' THEN
        image_url := image_value ->> 'url';
      ELSE
        RAISE EXCEPTION
          'legacy image_url content part at %[%] must contain a string URL',
          location,
          part_record.ordinality
          USING ERRCODE = '22023';
      END IF;
      IF btrim(image_url) = '' THEN
        RAISE EXCEPTION
          'legacy image_url content part at %[%] has a blank URL',
          location,
          part_record.ordinality
          USING ERRCODE = '22023';
      END IF;
      result := result || jsonb_build_array(
        jsonb_build_object(
          'type', 'image',
          'source', jsonb_build_object(
            'kind', 'url',
            'url', image_url
          )
        )
      );
    ELSE
      RAISE EXCEPTION
        'unsupported legacy content part type % at %[%]',
        coalesce(part_type, '<null>'),
        location,
        part_record.ordinality
        USING ERRCODE = '22023';
    END IF;
  END LOOP;

  RETURN result;
END;
$fogmoe_0068_content$;

-- @brief 将一条 OpenAI 形消息转换为封闭的 canonical V2 / Convert one OpenAI-shaped message into closed canonical V2.
CREATE FUNCTION conversation.canonical_message_v2(
  legacy_message JSONB,
  location TEXT,
  default_include_in_context BOOLEAN
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $fogmoe_0068_message$
DECLARE
  role_value TEXT;
  include_in_context BOOLEAN := default_include_in_context;
  parts_value JSONB;
  tool_calls_value JSONB;
  call_record RECORD;
  call_value JSONB;
  function_value JSONB;
  call_id TEXT;
  tool_name TEXT;
  result_value JSONB;
BEGIN
  IF jsonb_typeof(legacy_message) <> 'object' THEN
    RAISE EXCEPTION
      'legacy message at % must be an object',
      location
      USING ERRCODE = '22023';
  END IF;

  IF jsonb_typeof(legacy_message -> 'role') IS DISTINCT FROM 'string' THEN
    RAISE EXCEPTION
      'legacy message at % must contain a string role',
      location
      USING ERRCODE = '22023';
  END IF;
  role_value := legacy_message ->> 'role';
  IF role_value IS NULL OR role_value NOT IN ('system', 'user', 'assistant', 'tool') THEN
    RAISE EXCEPTION
      'legacy message at % has unsupported role %',
      location,
      role_value
      USING ERRCODE = '22023';
  END IF;

  IF legacy_message ? 'exclude_from_assistant' THEN
    IF jsonb_typeof(legacy_message -> 'exclude_from_assistant') IS DISTINCT FROM 'boolean' THEN
      RAISE EXCEPTION
        'legacy message at % has a non-boolean exclude_from_assistant field',
        location
        USING ERRCODE = '22023';
    END IF;
    include_in_context := NOT ((legacy_message ->> 'exclude_from_assistant')::BOOLEAN);
  END IF;

  IF role_value IN ('system', 'user') THEN
    IF NOT legacy_message ? 'content' THEN
      RAISE EXCEPTION
        'legacy % message at % is missing content',
        role_value,
        location
        USING ERRCODE = '22023';
    END IF;
    parts_value := conversation.canonical_content_parts_v2(
      legacy_message -> 'content',
      location,
      FALSE
    );
  ELSIF role_value = 'assistant' THEN
    IF NOT legacy_message ? 'content' THEN
      RAISE EXCEPTION
        'legacy assistant message at % is missing content',
        location
        USING ERRCODE = '22023';
    END IF;
    parts_value := conversation.canonical_content_parts_v2(
      legacy_message -> 'content',
      location,
      TRUE
    );
    IF legacy_message ? 'tool_calls' THEN
      tool_calls_value := legacy_message -> 'tool_calls';
      IF jsonb_typeof(tool_calls_value) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION
          'legacy assistant tool_calls at % must be an array',
          location
          USING ERRCODE = '22023';
      END IF;
      FOR call_record IN
        SELECT expanded.value, expanded.ordinality
        FROM jsonb_array_elements(tool_calls_value)
          WITH ORDINALITY AS expanded(value, ordinality)
        ORDER BY expanded.ordinality
      LOOP
        call_value := call_record.value;
        IF jsonb_typeof(call_value) IS DISTINCT FROM 'object'
          OR jsonb_typeof(call_value -> 'id') IS DISTINCT FROM 'string'
          OR (call_value ->> 'type') IS DISTINCT FROM 'function'
          OR jsonb_typeof(call_value -> 'function') IS DISTINCT FROM 'object' THEN
          RAISE EXCEPTION
            'malformed legacy tool call at %[%]',
            location,
            call_record.ordinality
            USING ERRCODE = '22023';
        END IF;
        function_value := call_value -> 'function';
        IF jsonb_typeof(function_value -> 'name') IS DISTINCT FROM 'string'
          OR NOT function_value ? 'arguments' THEN
          RAISE EXCEPTION
            'legacy tool call at %[%] is missing a string function name or arguments',
            location,
            call_record.ordinality
            USING ERRCODE = '22023';
        END IF;
        call_id := call_value ->> 'id';
        tool_name := function_value ->> 'name';
        IF btrim(call_id) = '' OR char_length(call_id) > 512
          OR btrim(tool_name) = '' OR char_length(tool_name) > 512 THEN
          RAISE EXCEPTION
            'legacy tool call at %[%] has an invalid id or function name',
            location,
            call_record.ordinality
            USING ERRCODE = '22023';
        END IF;
        parts_value := parts_value || jsonb_build_array(
          jsonb_build_object(
            'type', 'tool_call',
            'call_id', call_id,
            'name', tool_name,
            'arguments', conversation.canonical_tool_arguments_v2(
              function_value -> 'arguments',
              format('%s.tool_calls[%s].function.arguments', location, call_record.ordinality)
            )
          )
        );
      END LOOP;
    END IF;
  ELSE
    IF jsonb_typeof(legacy_message -> 'tool_call_id') IS DISTINCT FROM 'string'
      OR jsonb_typeof(legacy_message -> 'name') IS DISTINCT FROM 'string'
      OR NOT legacy_message ? 'content' THEN
      RAISE EXCEPTION
        'legacy tool message at % requires tool_call_id, name, and content',
        location
        USING ERRCODE = '22023';
    END IF;
    call_id := legacy_message ->> 'tool_call_id';
    tool_name := legacy_message ->> 'name';
    IF btrim(call_id) = '' OR char_length(call_id) > 512
      OR btrim(tool_name) = '' OR char_length(tool_name) > 512 THEN
      RAISE EXCEPTION
        'legacy tool message at % has an invalid tool_call_id or name',
        location
        USING ERRCODE = '22023';
    END IF;
    result_value := legacy_message -> 'content';
    IF jsonb_typeof(result_value) = 'string' THEN
      BEGIN
        result_value := (result_value #>> '{}')::JSONB;
      EXCEPTION
        WHEN invalid_text_representation THEN
          -- @brief 工具结果是可见事实；非 JSON 文本仍保留为 JSON 字符串 / Tool results are visible facts; non-JSON text remains a JSON string.
          result_value := legacy_message -> 'content';
      END;
    END IF;
    parts_value := jsonb_build_array(
      jsonb_build_object(
        'type', 'tool_result',
        'call_id', call_id,
        'name', tool_name,
        'result', result_value,
        'is_error', FALSE
      )
    );
  END IF;

  IF jsonb_array_length(parts_value) = 0 THEN
    RAISE EXCEPTION
      'legacy message at % would produce an empty canonical parts array',
      location
      USING ERRCODE = '22023';
  END IF;

  RETURN jsonb_build_object(
    'schema_version', 2,
    'role', role_value,
    'parts', parts_value,
    'policy', jsonb_build_object(
      'include_in_context', include_in_context
    ),
    'meta', '{}'::JSONB
  );
END;
$fogmoe_0068_message$;

-- @brief 从 legacy durable row 构造其 canonical model_message，保留原 envelope 仅作业务元数据 / Build a canonical model_message from a legacy durable row while retaining the original envelope as business metadata.
CREATE FUNCTION conversation.canonical_row_message_v2(
  envelope JSONB,
  durable_role TEXT,
  location TEXT,
  default_include_in_context BOOLEAN
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $fogmoe_0068_row_message$
DECLARE
  legacy_message JSONB;
  content_value JSONB;
BEGIN
  IF jsonb_typeof(envelope) <> 'object' THEN
    RAISE EXCEPTION
      'conversation envelope at % must be an object',
      location
      USING ERRCODE = '22023';
  END IF;
  IF durable_role NOT IN ('system', 'user', 'assistant', 'tool') THEN
    RAISE EXCEPTION
      'conversation row at % has unsupported durable role %',
      location,
      durable_role
      USING ERRCODE = '22023';
  END IF;

  -- @brief 保持旧投影优先使用 envelope 内 OpenAI 形消息的语义 / Preserve legacy projection's preference for an OpenAI-shaped message embedded in the envelope.
  IF envelope ? 'role' AND envelope ? 'content' THEN
    RETURN conversation.canonical_message_v2(
      envelope,
      format('%s.embedded_message', location),
      default_include_in_context
    );
  END IF;

  IF durable_role = 'tool' THEN
    IF envelope ? 'tool_call_id' AND envelope ? 'name' AND envelope ? 'content' THEN
      legacy_message := envelope || jsonb_build_object('role', durable_role);
      RETURN conversation.canonical_message_v2(
        legacy_message,
        format('%s.tool_message', location),
        default_include_in_context
      );
    END IF;
    RAISE EXCEPTION
      'legacy tool row at % requires tool_call_id, name, and content',
      location
      USING ERRCODE = '22023';
  END IF;

  IF jsonb_typeof(envelope -> 'text') = 'string' THEN
    content_value := envelope -> 'text';
  ELSIF jsonb_typeof(envelope -> 'content') = 'string' THEN
    content_value := envelope -> 'content';
  ELSE
    -- @brief JSONB 文本是持久化值的稳定表示；它避免因无法识别的业务 envelope 而丢失历史事实 / JSONB text is a stable representation of the persisted value and avoids losing a historical fact for an unrecognized business envelope.
    content_value := to_jsonb(envelope::TEXT);
  END IF;
  legacy_message := jsonb_build_object(
    'role', durable_role,
    'content', content_value
  );
  RETURN conversation.canonical_message_v2(
    legacy_message,
    format('%s.row_fallback', location),
    default_include_in_context
  );
END;
$fogmoe_0068_row_message$;

-- @brief 在不拆分 append-only Conversation row 的前提下重写其业务 envelope / Rewrite a business envelope without splitting an append-only Conversation row.
CREATE FUNCTION conversation.canonical_envelope_v2(
  envelope JSONB,
  durable_role TEXT,
  location TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $fogmoe_0068_envelope$
DECLARE
  normalized JSONB := envelope;
  is_assistant_envelope BOOLEAN := durable_role = 'assistant';
  default_include_in_context BOOLEAN := TRUE;
  history_value JSONB;
  model_value JSONB;
  events_value JSONB;
  converted_history JSONB;
  converted_events JSONB := '[]'::JSONB;
  item_record RECORD;
  event_value JSONB;
  changed BOOLEAN := FALSE;
  has_projectable_message BOOLEAN := FALSE;
BEGIN
  IF jsonb_typeof(envelope) <> 'object' THEN
    RAISE EXCEPTION
      'conversation envelope at % must be an object',
      location
      USING ERRCODE = '22023';
  END IF;

  IF envelope ? 'exclude_from_assistant' THEN
    IF jsonb_typeof(envelope -> 'exclude_from_assistant') IS DISTINCT FROM 'boolean' THEN
      RAISE EXCEPTION
        'conversation envelope at % has a non-boolean exclude_from_assistant field',
        location
        USING ERRCODE = '22023';
    END IF;
    default_include_in_context := NOT ((envelope ->> 'exclude_from_assistant')::BOOLEAN);
  END IF;

  IF envelope ? 'history_messages' THEN
    history_value := envelope -> 'history_messages';
    IF jsonb_typeof(history_value) IS DISTINCT FROM 'array' THEN
      RAISE EXCEPTION
        'history_messages at % must be an array',
        location
        USING ERRCODE = '22023';
    END IF;
    SELECT coalesce(
      jsonb_agg(
        conversation.canonical_message_v2(
          expanded.value,
          format('%s.history_messages[%s]', location, expanded.ordinality),
          default_include_in_context
        )
        ORDER BY expanded.ordinality
      ),
      '[]'::JSONB
    )
    INTO converted_history
    FROM jsonb_array_elements(history_value)
      WITH ORDINALITY AS expanded(value, ordinality);
    normalized := jsonb_set(
      normalized,
      '{history_messages}',
      converted_history,
      TRUE
    );
    changed := TRUE;
    has_projectable_message := TRUE;
  END IF;

  IF envelope ? 'model_message' THEN
    model_value := envelope -> 'model_message';
    normalized := jsonb_set(
      normalized,
      '{model_message}',
      conversation.canonical_message_v2(
        model_value,
        format('%s.model_message', location),
        default_include_in_context
      ),
      TRUE
    );
    changed := TRUE;
    has_projectable_message := TRUE;
  END IF;

  IF envelope ? 'runtime_events' THEN
    events_value := envelope -> 'runtime_events';
    IF jsonb_typeof(events_value) IS DISTINCT FROM 'array' THEN
      RAISE EXCEPTION
        'runtime_events at % must be an array',
        location
        USING ERRCODE = '22023';
    END IF;
    FOR item_record IN
      SELECT expanded.value, expanded.ordinality
      FROM jsonb_array_elements(events_value)
        WITH ORDINALITY AS expanded(value, ordinality)
      ORDER BY expanded.ordinality
    LOOP
      event_value := item_record.value;
      IF jsonb_typeof(event_value) <> 'object' THEN
        RAISE EXCEPTION
          'runtime event at %[%] must be an object',
          location,
          item_record.ordinality
          USING ERRCODE = '22023';
      END IF;
      IF event_value ? 'assistant_message' THEN
        event_value := jsonb_set(
          event_value,
          '{assistant_message}',
          conversation.canonical_message_v2(
            event_value -> 'assistant_message',
            format(
              '%s.runtime_events[%s].assistant_message',
              location,
              item_record.ordinality
            ),
            default_include_in_context
          ),
          TRUE
        );
      END IF;
      converted_events := converted_events || jsonb_build_array(event_value);
    END LOOP;
    normalized := jsonb_set(
      normalized,
      '{runtime_events}',
      converted_events,
      TRUE
    );
    changed := TRUE;
  END IF;

  IF NOT has_projectable_message AND durable_role <> 'system' THEN
    normalized := jsonb_set(
      normalized,
      '{model_message}',
      conversation.canonical_row_message_v2(
        envelope,
        durable_role,
        location,
        default_include_in_context
      ),
      TRUE
    );
    changed := TRUE;
  END IF;

  IF changed OR is_assistant_envelope THEN
    normalized := normalized || jsonb_build_object(
      'history_format', 'canonical-v2'
    );
  END IF;
  IF is_assistant_envelope THEN
    normalized := jsonb_set(
      normalized,
      '{schema_version}',
      '2'::JSONB,
      TRUE
    );
  END IF;
  RETURN normalized;
END;
$fogmoe_0068_envelope$;

-- @brief 复现 Python json.dumps(sort_keys=True, ensure_ascii=False) 的 snapshot 编码 / Reproduce Python json.dumps(sort_keys=True, ensure_ascii=False) for snapshots.
CREATE FUNCTION context_window.canonical_json_v2(value JSONB)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
STRICT
PARALLEL SAFE
AS '
  SELECT CASE jsonb_typeof($1)
    WHEN ''object'' THEN COALESCE(
      (
        SELECT
          ''{'' || string_agg(
            to_json(object_item.key)::TEXT || '': '' ||
            context_window.canonical_json_v2(object_item.value),
            '', '' ORDER BY object_item.key COLLATE "C"
          ) || ''}''
        FROM jsonb_each($1) AS object_item
      ),
      ''{}''
    )
    WHEN ''array'' THEN COALESCE(
      (
        SELECT
          ''['' || string_agg(
            context_window.canonical_json_v2(array_item.value),
            '', '' ORDER BY array_item.ordinality
          ) || '']''
        FROM jsonb_array_elements($1)
          WITH ORDINALITY AS array_item(value, ordinality)
      ),
      ''[]''
    )
    WHEN ''number'' THEN CASE
      WHEN position(''.'' IN $1::TEXT) = 0 THEN $1::TEXT
      WHEN abs(($1 #>> ''{}'')::NUMERIC) >
        1.7976931348623157e308::NUMERIC
        THEN CASE
          WHEN ($1 #>> ''{}'')::NUMERIC < 0 THEN ''-Infinity''
          ELSE ''Infinity''
        END
      WHEN to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT ~ ''[.eE]''
        THEN to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT
      ELSE to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT || ''.0''
    END
    ELSE $1::TEXT
  END
';

-- @brief 转换 compaction 冻结 snapshot，保留元素次序并拒绝坏消息 / Convert frozen compaction snapshots, preserve order, and reject malformed messages.
CREATE FUNCTION context_window.canonical_snapshot_v2(
  snapshot JSONB,
  location TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $fogmoe_0068_snapshot$
DECLARE
  converted JSONB;
BEGIN
  IF jsonb_typeof(snapshot) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION
      'compaction source_snapshot at % must be an array',
      location
      USING ERRCODE = '22023';
  END IF;
  SELECT coalesce(
    jsonb_agg(
      conversation.canonical_message_v2(
        expanded.value,
        format('%s.source_snapshot[%s]', location, expanded.ordinality),
        TRUE
      )
      ORDER BY expanded.ordinality
    ),
    '[]'::JSONB
  )
  INTO converted
  FROM jsonb_array_elements(snapshot)
    WITH ORDINALITY AS expanded(value, ordinality);
  IF jsonb_array_length(converted) = 0 THEN
    RAISE EXCEPTION
      'compaction source_snapshot at % must not be empty',
      location
      USING ERRCODE = '22023';
  END IF;
  RETURN converted;
END;
$fogmoe_0068_snapshot$;

-- @brief 原位重写所有可投影 Conversation envelope，绝不拆分既有 append-only 行 / Rewrite every projectable Conversation envelope in place and never split existing append-only rows.
UPDATE conversation.conversation_messages AS message
SET content = conversation.canonical_envelope_v2(
  message.content,
  message.role,
  format('conversation.conversation_messages[%s]', message.message_id)
)
WHERE message.role <> 'system'
  OR message.content ? 'history_messages'
  OR message.content ? 'model_message'
  OR message.content ? 'runtime_events';

-- @brief 升级冻结 compaction snapshot，并以 canonical Python 语义精确重算 SHA-256；保留 projection_version 以维持 UUIDv5 identity / Upgrade frozen compaction snapshots and recompute SHA-256 with canonical Python semantics; retain projection_version to preserve UUIDv5 identity.
WITH converted AS (
  SELECT
    compaction.compaction_id,
    context_window.canonical_snapshot_v2(
      compaction.source_snapshot::JSONB,
      format('context_window.compactions[%s]', compaction.compaction_id)
    ) AS source_snapshot
  FROM context_window.compactions AS compaction
)
UPDATE context_window.compactions AS compaction
SET source_snapshot = converted.source_snapshot::JSON,
    source_digest = encode(
      sha256(convert_to(
        context_window.canonical_json_v2(converted.source_snapshot),
        'UTF8'
      )),
      'hex'
    )
FROM converted
WHERE compaction.compaction_id = converted.compaction_id;

-- @brief 删除仅供此次数据迁移使用的辅助 routine / Drop helper routines used only by this data migration.
DROP FUNCTION context_window.canonical_snapshot_v2(JSONB, TEXT);
DROP FUNCTION context_window.canonical_json_v2(JSONB);
DROP FUNCTION conversation.canonical_envelope_v2(JSONB, TEXT, TEXT);
DROP FUNCTION conversation.canonical_row_message_v2(JSONB, TEXT, TEXT, BOOLEAN);
DROP FUNCTION conversation.canonical_message_v2(JSONB, TEXT, BOOLEAN);
DROP FUNCTION conversation.canonical_content_parts_v2(JSONB, TEXT, BOOLEAN);
DROP FUNCTION conversation.canonical_tool_arguments_v2(JSONB, TEXT);

-- migrate:down

-- @brief canonical V2 丢弃旧 wire shape，无法无损恢复 / Canonical V2 discards the old wire shape and cannot be restored losslessly.
DO $fogmoe_0068_down$
BEGIN
  RAISE EXCEPTION
    '0068_canonical_assistant_messages is irreversible; restore from a pre-0068 backup instead'
    USING ERRCODE = '0A000';
END;
$fogmoe_0068_down$;
