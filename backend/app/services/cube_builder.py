"""多维立方服务：构建多维分析立方体，定义维度组合、预计算策略和查询优化。

增强能力：
- 消费 fact.published 通道获取维度组合信息
- 预计算策略选择（全部/增量/核心组合/无预计算）
- 按所选策略计算预计算单元明细：新构建版本只包含策略选中的维度组合，
  未选中组合不计入本次构建
- 每次构建产生新的数据版本（快照），供星型查询核对有效范围
- 分区方式推荐
- Cube构建进度追踪
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

PRECOMPUTE_STRATEGIES = {"全部预计算", "增量预计算", "核心组合", "无预计算"}
PARTITION_METHODS = {"按日期分区", "按周分区", "按月分区", "按维度分区", "无分区"}

_DIM_SEPARATORS = (".", "、", ",", "，", "/", ";")


def parse_dims(combo: str) -> list[str]:
    """把「日期.门店.产品类目」这类维度组合串解析成有序维度列表。"""
    text = str(combo or "").strip()
    for sep in _DIM_SEPARATORS[1:]:
        text = text.replace(sep, _DIM_SEPARATORS[0])
    dims: list[str] = []
    for part in text.split(_DIM_SEPARATORS[0]):
        name = part.strip()
        if name and name not in dims:
            dims.append(name)
    return dims


def all_combos(dims: list[str]) -> list[str]:
    """全部非空维度组合（保持声明顺序），即全量预计算的候选单元。"""
    combos: list[str] = []
    total = 1 << len(dims)
    for mask in range(1, total):
        cell = ".".join(dims[i] for i in range(len(dims)) if mask & (1 << i))
        combos.append(cell)
    # 按维度数升序、同维度数按声明顺序，便于阅读与核对
    combos.sort(key=lambda c: (c.count("."), [dims.index(d) for d in c.split(".")]))
    return combos


def plan_precompute_cells(dims: list[str], strategy: str) -> list[str]:
    """按预计算策略计算可生成的维度组合（预计算单元明细）。

    - 全部预计算：所有非空维度组合（2^N - 1 个单元）
    - 增量预计算：仅包含首维度（分区/时间维度）的组合
    - 核心组合：仅保留声明的核心维度组合本身（1 个单元）
    - 无预计算：不生成任何单元，查询需回退明细路径
    """
    if not dims:
        return []
    if strategy == "无预计算":
        return []
    if strategy == "核心组合":
        return [".".join(dims)]
    candidates = all_combos(dims)
    if strategy == "增量预计算":
        first = dims[0]
        return [cell for cell in candidates if first in cell.split(".")]
    # 默认按全部预计算处理
    return candidates


class CubeBuilderService(BaseModuleService):
    MODULE = "cube_builder"
    ENTITY = "Cube定义"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["Cube名称", "源事实表", "维度组合", "度量集合", "预计算策略", "分区方式", "查询优化"]
    REQUIRED_FIELDS = ["Cube名称", "源事实表", "维度组合"]
    STATUS_ORDER = ["草稿", "计算中", "已就绪", "已失效"]
    ACTION_RULES = {"构建Cube": "草稿", "失效Cube": "已就绪"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        strategy = str(values.get("预计算策略", "")).strip()
        if strategy not in PRECOMPUTE_STRATEGIES:
            values["预计算策略"] = "全部预计算"

        partition = str(values.get("分区方式", "")).strip()
        if partition not in PARTITION_METHODS:
            values["分区方式"] = "按日期分区"

        if not values.get("查询优化"):
            values["查询优化"] = "复合索引"

        # 消费事实发布通道
        store.channel.consume("fact.published", "cube_builder")
        store.channel.consume("measure.ready", "cube_builder")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 新登记的 Cube 尚未构建：预计算单元为空，等待首次构建产生数据版本
        entry["预计算单元明细"] = []
        entry["预计算单元数"] = 0
        entry["未构建组合"] = []
        entry["有效范围"] = "（未构建）"
        entry["构建版本"] = ""
        entry["_build_count"] = 0
        entry["_last_snapshot"] = None

        store.channel.publish("cube.defined", {
            "cube_name": entry["Cube名称"],
            "source_fact": entry.get("源事实表"),
            "strategy": entry.get("预计算策略"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "cube_builder", "cube_def",
            f"cube_{entry['id']}",
            {"cube_name": entry["Cube名称"], "source_fact": entry.get("源事实表")},
            ["fact_design", "measure_config", "cube_builder"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "构建Cube":
            return self._build_cube(entry)
        if action == "失效Cube":
            return self._invalidate_cube(entry)
        return entry, message

    def _build_cube(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """按当前预计算策略构建 Cube：只生成策略选中的维度组合。"""
        entry["status"] = "计算中"
        entry["pending"] = True

        strategy = str(entry.get("预计算策略", "全部预计算")).strip()
        if strategy not in PRECOMPUTE_STRATEGIES:
            strategy = "全部预计算"
            entry["预计算策略"] = strategy

        dims = parse_dims(str(entry.get("维度组合", "")))
        # 预计算单元明细：严格按所选策略校验可生成的维度组合
        cells = plan_precompute_cells(dims, strategy)
        # 当前策略之外的组合不能计入本次构建
        uncovered = [cell for cell in all_combos(dims) if cell not in cells]

        # 新构建版本：版本号递增，并落一份版本快照
        build_count = int(entry.get("_build_count", 0)) + 1
        entry["_build_count"] = build_count
        version = f"v{build_count}"
        built_at = datetime.now(timezone.utc).isoformat()

        entry["预计算单元明细"] = cells
        entry["预计算单元数"] = len(cells)
        entry["未构建组合"] = uncovered
        entry["有效范围"] = "、".join(cells) if cells else "（无预计算单元）"
        entry["构建版本"] = version
        entry["构建时间"] = built_at

        snap_id = store.snapshot.create_version_snapshot(
            self.MODULE, int(entry["id"]),
            {
                "预计算策略": strategy,
                "预计算单元明细": list(cells),
                "未构建组合": list(uncovered),
                "构建版本": version,
            },
            version_label=version,
            parent_snap_id=entry.get("_last_snapshot"),
        )
        entry["_last_snapshot"] = snap_id

        store.channel.publish("cube.built", {
            "cube_name": entry["Cube名称"],
            "source_fact": entry.get("源事实表"),
            "strategy": strategy,
            "version": version,
            "cells": list(cells),
            "cells_precomputed": len(cells),
            "uncovered_combos": list(uncovered),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "cube_builder", "cube_build",
            f"cube_{entry['id']}_{version}",
            {
                "cube_name": entry["Cube名称"],
                "strategy": strategy,
                "version": version,
                "cell_count": len(cells),
            },
            ["cube_builder"],
        )

        # 构建完成后设为已就绪
        entry["status"] = "已就绪"
        entry["pending"] = False
        entry["abnormal"] = False

        # 新构建版本取代同一预计算范围（同源事实表 + 同维度组合）的旧版本：
        # 旧版本的单元不再算作已构建，避免新旧预计算范围并存冲突。
        superseded = self._supersede_conflicting_cubes(entry)

        # 新旧预计算范围冲突检测：同一事实表上的历史查询若仍停留在
        # 旧粒度（本次构建未覆盖的组合），标记为待复算，由星型查询复算纠正。
        stale = self._mark_stale_queries(entry, cells)

        message = (
            f"Cube定义已构建Cube：策略「{strategy}」生成{len(cells)}个预计算单元"
            f"（版本{version}），{len(uncovered)}个组合未纳入本次构建"
        )
        if superseded:
            message += f"，{superseded}个同范围旧版本Cube已失效"
        if stale:
            message += f"，{stale}条历史查询粒度与新版范围冲突，待复算"
        return entry, message

    @staticmethod
    def _supersede_conflicting_cubes(entry: dict[str, Any]) -> int:
        """让同一预计算范围内的旧版本 Cube 失效，返回失效数量。"""
        source_fact = str(entry.get("源事实表", "")).strip()
        dim_key = frozenset(parse_dims(str(entry.get("维度组合", ""))))
        if not source_fact or not dim_key:
            return 0
        superseded = 0
        for other in store.rows("cube_builder"):
            if other is entry or int(other.get("id", 0)) == int(entry.get("id", 0)):
                continue
            if other.get("status") != "已就绪":
                continue
            if str(other.get("源事实表", "")).strip() != source_fact:
                continue
            if frozenset(parse_dims(str(other.get("维度组合", "")))) != dim_key:
                continue
            other["status"] = "已失效"
            other["pending"] = False
            other["预计算单元明细"] = []
            other["预计算单元数"] = 0
            other["有效范围"] = "（已被新版本取代）"
            other["_被取代"] = f"{entry.get('Cube名称')}@{entry.get('构建版本')}"
            store.channel.publish("cube.invalidated", {
                "cube_name": other.get("Cube名称"),
                "reason": f"被 {entry.get('Cube名称')} 的新构建版本取代",
                "entry_id": other.get("id"),
            })
            superseded += 1
        return superseded

    def _invalidate_cube(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """失效 Cube：状态置为已失效，预计算单元不再计入有效范围。"""
        entry["status"] = "已失效"
        entry["pending"] = False
        entry["预计算单元明细"] = []
        entry["预计算单元数"] = 0
        entry["有效范围"] = "（已失效）"
        store.channel.publish("cube.invalidated", {
            "cube_name": entry["Cube名称"],
            "entry_id": entry["id"],
        })
        return entry, f"Cube定义已失效Cube：{entry.get('Cube名称')} 的预计算单元已失效"

    @staticmethod
    def _mark_stale_queries(entry: dict[str, Any], cells: list[str]) -> int:
        """标记与新预计算范围冲突的历史查询，返回标记条数。"""
        source_fact = str(entry.get("源事实表", "")).strip()
        if not source_fact:
            return 0
        cell_sets = {frozenset(cell.split(".")) for cell in cells}
        stale = 0
        for query in store.rows("star_query"):
            if str(query.get("事实表", "")).strip() != source_fact:
                continue
            required = frozenset(parse_dims(str(query.get("分组维度", ""))))
            if required and required not in cell_sets:
                query["_待复算"] = True
                stale += 1
            else:
                query["_待复算"] = False
        return stale

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        ready = 0
        total_cells = 0
        by_strategy: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            if row.get("status") == "已就绪":
                ready += 1
                total_cells += int(row.get("预计算单元数", 0) or 0)
            strategy = str(row.get("预计算策略", ""))
            if strategy:
                by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        base["ready_cubes"] = ready
        base["precomputed_cells"] = total_cells
        base["strategy_distribution"] = by_strategy
        return base
