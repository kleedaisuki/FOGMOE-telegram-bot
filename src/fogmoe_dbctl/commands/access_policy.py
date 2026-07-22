"""@brief dbctl 数据库访问策略 / dbctl database access policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeFunctionGrant:
    """@brief 运行时可调用函数 / Runtime-callable function.

    @param schema 函数所属 schema / Schema containing the function.
    @param name 函数名 / Function name.
    @param argument_signature PostgreSQL 身份参数签名 / PostgreSQL identity-argument signature.
    @note 参数签名是受信任的静态策略数据，不接受 CLI 输入。/
        The argument signature is trusted static policy data and never accepts CLI input.
    """

    schema: str
    name: str
    argument_signature: str

    def __post_init__(self) -> None:
        """@brief 验证函数授权的结构约束 / Validate function-grant structural constraints.

        @return None / None.
        @raise ValueError 授权标识为空或签名可拼接额外语句时抛出 /
            Raised when an identifier is empty or the signature could append another statement.
        """

        if not self.schema or not self.name:
            raise ValueError("Runtime function schema and name cannot be empty")
        if not self.argument_signature or ";" in self.argument_signature:
            raise ValueError(
                f"Runtime function argument signature is invalid: {self.name}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportingRelationGrant:
    """@brief Dashboard 可读关系组 / Dashboard-readable relation group.

    @param schema 关系所属 schema / Schema containing the relations.
    @param relations 允许 SELECT 的关系闭集 / Closed set of SELECT-able relations.
    """

    schema: str
    relations: tuple[str, ...]

    def __post_init__(self) -> None:
        """@brief 拒绝空或重复的报表关系策略 / Reject empty or duplicate reporting-relation policy.

        @return None / None.
        @raise ValueError schema 或关系闭集无效时抛出 / Raised when the schema or relation set is invalid.
        """

        if not self.schema:
            raise ValueError("Reporting relation schema cannot be empty")
        if not self.relations:
            raise ValueError(f"Reporting relation allow-list is empty: {self.schema}")
        if any(not relation for relation in self.relations):
            raise ValueError("Reporting relation name cannot be empty")
        if len(set(self.relations)) != len(self.relations):
            raise ValueError(
                f"Reporting relation allow-list contains duplicates: {self.schema}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseAccessPolicy:
    """@brief 运行时与报表访问的单一权威闭集 / Single authoritative runtime and reporting access closure.

    @param application_schemas 应用角色可访问的 schema 闭集 / Closed set of application-accessible schemas.
    @param runtime_functions 应用可执行的函数闭集 / Closed set of application-executable functions.
    @param reporting_relations Dashboard 可读关系闭集 / Closed set of Dashboard-readable relations.
    """

    application_schemas: tuple[str, ...]
    runtime_functions: tuple[RuntimeFunctionGrant, ...]
    reporting_relations: tuple[ReportingRelationGrant, ...]

    def __post_init__(self) -> None:
        """@brief 验证所有授权都属于应用 schema 闭集 / Verify every grant belongs to the application-schema closure.

        @return None / None.
        @raise ValueError schema 闭集为空、重复或授权越界时抛出 /
            Raised for an empty or duplicate schema closure or an out-of-bound grant.
        """

        if not self.application_schemas:
            raise ValueError("Application schema allow-list cannot be empty")
        if any(not schema for schema in self.application_schemas):
            raise ValueError("Application schema name cannot be empty")
        if len(set(self.application_schemas)) != len(self.application_schemas):
            raise ValueError("Application schema allow-list contains duplicates")
        owned_schemas = frozenset(self.application_schemas)
        function_keys: set[tuple[str, str, str]] = set()
        for function in self.runtime_functions:
            if function.schema not in owned_schemas:
                raise ValueError(
                    "Runtime function schema is not application-owned: "
                    f"{function.schema}"
                )
            function_key = (
                function.schema,
                function.name,
                function.argument_signature,
            )
            if function_key in function_keys:
                raise ValueError(
                    "Runtime function allow-list contains duplicates: "
                    f"{function.schema}.{function.name}"
                )
            function_keys.add(function_key)
        reporting_schemas: set[str] = set()
        for relation_group in self.reporting_relations:
            if relation_group.schema not in owned_schemas:
                raise ValueError(
                    "Reporting relation schema is not application-owned: "
                    f"{relation_group.schema}"
                )
            if relation_group.schema in reporting_schemas:
                raise ValueError(
                    "Reporting schema appears in more than one relation group: "
                    f"{relation_group.schema}"
                )
            reporting_schemas.add(relation_group.schema)


DEFAULT_ACCESS_POLICY = DatabaseAccessPolicy(
    application_schemas=(
        "identity",
        "conversation",
        "context_window",
        "retrieval",
        "user_profile",
        "assistant",
        "scheduling",
        "economy",
        "moderation",
        "crypto",
        "game",
        "media",
        "admin",
        "observability",
        "bank",
        "billing",
        "town",
        "chance",
        "personal_rpg",
    ),
    runtime_functions=(
        RuntimeFunctionGrant(
            schema="observability",
            name="ensure_daily_partitions",
            argument_signature="DATE",
        ),
        RuntimeFunctionGrant(
            schema="observability",
            name="drop_partitions_before",
            argument_signature="DATE",
        ),
    ),
    reporting_relations=(
        ReportingRelationGrant(
            schema="observability",
            relations=(
                "resources",
                "log_records",
                "spans",
                "metric_points",
                "pipeline_health",
                "turn_latency",
                "retrieval_queue_health",
            ),
        ),
    ),
)
"""@brief FogMoe 当前数据库访问策略 / Current FogMoe database access policy."""
