"""星型查询服务：定义基于星型模型的维度查询，包括事实与维度的关联和度量聚合。

增强能力：
- 消费 fact.published 和 dim.published 通道校验查询组件
- 自动校验关联维度和度量是否可用
- 验证查询（复算）时按 Cube 实际生成的预计算单元决定取数来源：
  命中当前策略有效范围则走 Cube，未命中则生成完整的明细查询路径
- 切换来源后仍保留原始度量口径和过滤/分组范围，复算结果说明实际取数来源
- 记录查询所用的数据版本（Cube 构建版本或明细直查版本）
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cube_builder import parse_dims
from app.store import store


class StarQueryService(BaseModuleService):
    MODULE = "star_query"
    ENTITY = "查询定义"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["查询名称", "事实表", "关联维度", "度量选择", "过滤条件", "分组维度", "结果限制"]
    REQUIRED_FIELDS = ["查询名称", "事实表", "关联维度"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已过期"]
    ACTION_RULES = {"验证查询": "草稿", "发布查询": "已验证"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        fact_name = str(values.get("事实表", "")).strip()
        related_dims = str(values.get("关联维度", "")).strip()
        measures = str(values.get("度量选择", "")).strip()

        # 消费事实和维度通道校验
        fact_msgs = store.channel.consume("fact.published", "star_query")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_fact_confirmed"] = True

        dim_msgs = store.channel.consume("dim.published", "star_query")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                values[f"_dim_{dim_code}_confirmed"] = True

        # 如果未填度量，从channel消费
        if not measures:
            measure_msgs = store.channel.consume("measure.ready", "star_query")
            for m in measure_msgs:
                if m["payload"].get("fact_name") == fact_name:
                    measures = m["payload"].get("measure_name", "")
            if measures:
                values["度量选择"] = measures

        if not values.get("结果限制"):
            values["结果限制"] = "1000"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 复算所需的原始口径与范围：登记后不再被验证动作改写
        entry["度量口径"] = entry.get("度量选择", "")
        entry["复算范围"] = _scope_text(entry)
        entry["取数来源"] = ""
        entry["来源类型"] = ""
        entry["数据版本"] = ""
        entry["命中Cube"] = ""
        entry["命中单元"] = ""
        entry["明细查询路径"] = ""
        entry["查询路径"] = ""
        entry["复算次数"] = 0
        entry["_待复算"] = False

        store.channel.publish("query.defined", {
            "query_name": entry["查询名称"],
            "fact_name": fact_name,
            "related_dims": related_dims,
            "measures": entry.get("度量选择"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "star_query", "star_query_def",
            f"query_{entry['id']}",
            {"query_name": entry["查询名称"], "fact_name": fact_name},
            ["fact_design", "dim_definition", "star_query"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "验证查询":
            return self._validate_query(entry)
        if action == "发布查询":
            entry["status"] = "已发布"
            entry["pending"] = False
            store.channel.publish("query.published", {
                "query_name": entry["查询名称"],
                "source": entry.get("取数来源"),
                "data_version": entry.get("数据版本"),
                "entry_id": entry_id,
            })
            return entry, f"查询定义已发布查询：{entry.get('查询名称')}"
        return entry, message

    def _validate_query(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """验证查询（复算）：按 Cube 当前实际生成的预计算单元决定取数来源。

        复算保留原始度量口径（度量选择）和范围（过滤条件/分组维度），
        只切换取数路径；当前策略之外的单元不计入有效范围。
        """
        previous_source = str(entry.get("取数来源", ""))

        # 原始口径与范围：复算不改动登记时的度量与过滤/分组
        measures = str(entry.get("度量选择", "")).strip()
        entry["度量口径"] = measures
        entry["复算范围"] = _scope_text(entry)

        # 完整的明细查询路径：无论是否命中 Cube 都生成，未命中时即作为取数路径
        detail_path = _detail_query_path(entry)
        entry["明细查询路径"] = detail_path

        required = frozenset(parse_dims(str(entry.get("分组维度", ""))))
        cube, cell = _match_cube_cell(str(entry.get("事实表", "")).strip(), required)

        if cube is not None and cell is not None:
            # 命中 Cube 当前策略实际生成的单元
            version = str(cube.get("构建版本", "")) or "v1"
            entry["来源类型"] = "Cube预计算"
            entry["取数来源"] = str(cube.get("Cube名称", ""))
            entry["数据版本"] = version
            entry["命中Cube"] = str(cube.get("Cube名称", ""))
            entry["命中单元"] = cell
            entry["查询路径"] = _cube_query_path(entry, cube, cell)
        else:
            # 未命中：回退完整的明细查询路径，记录明细直查的数据版本
            entry["来源类型"] = "明细查询"
            entry["取数来源"] = f"明细查询:{entry.get('事实表', '')}"
            entry["数据版本"] = f"{entry.get('事实表', '')}@当前分区"
            entry["命中Cube"] = ""
            entry["命中单元"] = ""
            entry["查询路径"] = detail_path

        entry["复算次数"] = int(entry.get("复算次数", 0) or 0) + 1
        entry["_待复算"] = False
        entry["status"] = "已验证"
        entry["pending"] = True

        source_switched = bool(previous_source) and previous_source != entry["取数来源"]
        store.channel.publish("query.validated", {
            "query_name": entry["查询名称"],
            "source_type": entry["来源类型"],
            "source": entry["取数来源"],
            "data_version": entry["数据版本"],
            "matched_cell": entry["命中单元"],
            "previous_source": previous_source,
            "source_switched": source_switched,
            "generated_sql": entry["查询路径"],
            "entry_id": entry["id"],
        })
        if source_switched:
            store.channel.publish("query.source_switched", {
                "query_name": entry["查询名称"],
                "previous_source": previous_source,
                "current_source": entry["取数来源"],
                "data_version": entry["数据版本"],
                "entry_id": entry["id"],
            })

        message = (
            f"查询定义已验证查询：取数来源={entry['取数来源']}，"
            f"数据版本={entry['数据版本']}，口径与范围保持不变"
        )
        if cube is None:
            message += "；未命中Cube预计算单元，已生成完整明细查询路径"
        if source_switched:
            message += f"；取数来源已从「{previous_source}」切换"
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["consumed_facts"] = len(store.channel.consume("fact.published", "star_query"))
        base["consumed_dims"] = len(store.channel.consume("dim.published", "star_query"))
        cube_hits = sum(1 for r in store.rows(self.MODULE) if r.get("来源类型") == "Cube预计算")
        detail_hits = sum(1 for r in store.rows(self.MODULE) if r.get("来源类型") == "明细查询")
        base["cube_sourced_queries"] = cube_hits
        base["detail_sourced_queries"] = detail_hits
        base["pending_revalidate"] = sum(1 for r in store.rows(self.MODULE) if r.get("_待复算"))
        return base


def _match_cube_cell(fact_name: str, required: frozenset[str]) -> tuple[dict[str, Any] | None, str | None]:
    """在指定事实表的已就绪 Cube 中，查找实际生成过该维度组合的单元。

    只认当前构建版本写入的预计算单元明细；已失效或未构建的 Cube 不参与命中。
    """
    if not fact_name or not required:
        return None, None
    candidates = [
        row for row in store.rows("cube_builder")
        if str(row.get("源事实表", "")).strip() == fact_name
        and row.get("status") == "已就绪"
    ]
    # 优先使用最新构建的 Cube
    candidates.sort(key=lambda r: (int(r.get("_build_count", 0) or 0), int(r.get("id", 0))), reverse=True)
    for cube in candidates:
        cells = cube.get("预计算单元明细") or []
        for cell in cells:
            if frozenset(str(cell).split(".")) == required:
                return cube, str(cell)
    return None, None


def _scope_text(entry: dict[str, Any]) -> str:
    """复算范围：过滤条件 + 分组维度 + 结果限制。"""
    filters = str(entry.get("过滤条件", "")).strip() or "（无过滤）"
    group = str(entry.get("分组维度", "")).strip() or "（无分组）"
    limit = str(entry.get("结果限制", "")).strip() or "1000"
    return f"过滤={filters}；分组={group}；限制={limit}"


def _measure_list(entry: dict[str, Any]) -> list[str]:
    return [m.strip() for m in str(entry.get("度量选择", "")).replace("，", ".").replace(",", ".").split(".") if m.strip()]


def _detail_query_path(entry: dict[str, Any]) -> str:
    """完整的明细查询路径：直查事实表并关联维度，保留原始度量口径和范围。"""
    fact = str(entry.get("事实表", "")).strip()
    related = parse_dims(str(entry.get("关联维度", "")))
    measures = _measure_list(entry)
    filters = str(entry.get("过滤条件", "")).strip() or "1=1"
    group_dims = parse_dims(str(entry.get("分组维度", "")))
    limit = str(entry.get("结果限制", "")).strip() or "1000"

    select_parts = list(group_dims) + [f"SUM({m}) AS {m}" for m in measures] or ["*"]
    join_clauses = " ".join(f"LEFT JOIN {d} ON f.{d}_id = {d}.id" for d in related)
    group_clause = ", ".join(group_dims) if group_dims else ""
    sql = f"SELECT {', '.join(select_parts)} FROM {fact} f"
    if join_clauses:
        sql += f" {join_clauses}"
    sql += f" WHERE {filters}"
    if group_clause:
        sql += f" GROUP BY {group_clause}"
    sql += f" LIMIT {limit}"
    return f"明细路径[{fact}]：{sql}"


def _cube_query_path(entry: dict[str, Any], cube: dict[str, Any], cell: str) -> str:
    """命中 Cube 时的取数路径：使用 Cube 预计算单元与构建版本。"""
    cube_name = str(cube.get("Cube名称", ""))
    version = str(cube.get("构建版本", "")) or "v1"
    measures = _measure_list(entry)
    filters = str(entry.get("过滤条件", "")).strip() or "1=1"
    group_dims = parse_dims(str(entry.get("分组维度", "")))
    limit = str(entry.get("结果限制", "")).strip() or "1000"

    select_parts = list(group_dims) + [f"SUM({m}) AS {m}" for m in measures]
    group_clause = ", ".join(group_dims)
    sql = (
        f"SELECT {', '.join(select_parts)} FROM {cube_name}"
        f" WHERE {filters} GROUP BY {group_clause} LIMIT {limit}"
    )
    return f"Cube路径[{cube_name}@{version}，单元={cell}]：{sql}"
