"""Cube 预计算策略与星型查询取数来源测试：

- 按所选策略校验可生成的维度组合（全部/增量/核心组合/无预计算）
- 新构建版本只包含策略选中的组合，未选中组合不计入本次构建
- 星型查询按实际生成的单元决定取数来源，未命中生成完整明细查询路径
- 切换策略后历史查询与新预计算范围的冲突可复算纠正，口径与范围保留
"""
from __future__ import annotations

import copy

import pytest

from app.services import service_for
from app.services.cube_builder import all_combos, parse_dims, plan_precompute_cells
from app.store import store


@pytest.fixture
def clean_tables():
    """每个测试前后备份并还原内存仓库，避免用例间相互污染。"""
    tables_backup = copy.deepcopy(store._tables)
    channels_backup = copy.deepcopy(store.channel._channels)
    snapshots_backup = copy.deepcopy(store.snapshot._snapshots)
    yield
    store._tables = tables_backup
    store.channel._channels = channels_backup
    store.snapshot._snapshots = snapshots_backup


@pytest.fixture
def cube_svc():
    return service_for("cube_builder")


@pytest.fixture
def query_svc():
    return service_for("star_query")


def _create_cube(cube_svc, name: str, strategy: str, dims: str = "日期.门店.产品类目.渠道"):
    entry, missing = cube_svc.create_entry({
        "Cube名称": name,
        "源事实表": "测试销售事实",
        "维度组合": dims,
        "度量集合": "gmv_amt.order_cnt",
        "预计算策略": strategy,
        "分区方式": "按日期分区",
        "查询优化": "复合索引",
    })
    assert not missing
    assert entry is not None
    return entry


def _create_query(query_svc, group: str = "门店", name: str = "门店日销售验证"):
    entry, missing = query_svc.create_entry({
        "查询名称": name,
        "事实表": "测试销售事实",
        "关联维度": "DIM_TIME.DIM_STORE.DIM_PRODUCT",
        "度量选择": "gmv_amt.order_cnt",
        "过滤条件": "ds=最近30天",
        "分组维度": group,
        "结果限制": "1000",
    })
    assert not missing
    assert entry is not None
    return entry


# ======================================================================
# Section A: 预计算单元明细按策略生成
# ======================================================================


class TestPrecomputeCellPlanning:
    """按所选策略校验可生成的维度组合。"""

    def test_full_strategy_covers_all_combos(self):
        dims = parse_dims("日期.门店.产品类目.渠道")
        cells = plan_precompute_cells(dims, "全部预计算")
        assert len(cells) == 2 ** 4 - 1
        assert set(cells) == set(all_combos(dims))

    def test_core_strategy_only_keeps_declared_combo(self):
        dims = parse_dims("日期.门店.产品类目.渠道")
        cells = plan_precompute_cells(dims, "核心组合")
        assert cells == ["日期.门店.产品类目.渠道"]

    def test_none_strategy_generates_no_cells(self):
        dims = parse_dims("日期.门店.产品类目.渠道")
        assert plan_precompute_cells(dims, "无预计算") == []

    def test_incremental_strategy_requires_partition_dim(self):
        dims = parse_dims("日期.门店.产品类目.渠道")
        cells = plan_precompute_cells(dims, "增量预计算")
        assert len(cells) == 2 ** 3
        assert all("日期" in cell.split(".") for cell in cells)


# ======================================================================
# Section B: 构建版本只计入策略选中的组合
# ======================================================================


class TestCubeBuild:
    """构建 Cube：新构建版本只允许该策略选中的组合。"""

    def test_full_build_records_all_cells(self, clean_tables, cube_svc):
        cube = _create_cube(cube_svc, "cube_full", "全部预计算")
        entry, message = cube_svc.run_action(cube["id"], "构建Cube")
        assert entry["status"] == "已就绪"
        assert entry["预计算单元数"] == 15
        assert len(entry["预计算单元明细"]) == 15
        assert entry["未构建组合"] == []
        assert entry["构建版本"] == "v1"

    def test_core_build_excludes_unselected_combos(self, clean_tables, cube_svc):
        cube = _create_cube(cube_svc, "cube_core", "核心组合")
        entry, message = cube_svc.run_action(cube["id"], "构建Cube")
        # 核心组合只生成 1 个单元，其余组合不能算作已构建
        assert entry["预计算单元数"] == 1
        assert entry["预计算单元明细"] == ["日期.门店.产品类目.渠道"]
        assert len(entry["未构建组合"]) == 15 - 1
        assert "门店" in entry["未构建组合"]

    def test_none_build_generates_zero_cells(self, clean_tables, cube_svc):
        cube = _create_cube(cube_svc, "cube_none", "无预计算")
        entry, message = cube_svc.run_action(cube["id"], "构建Cube")
        assert entry["预计算单元数"] == 0
        assert entry["预计算单元明细"] == []
        assert len(entry["未构建组合"]) == 15
        assert entry["status"] == "已就绪"

    def test_rebuild_bumps_version_and_snapshot(self, clean_tables, cube_svc):
        cube = _create_cube(cube_svc, "cube_ver", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")
        cube["预计算策略"] = "核心组合"
        entry, _ = cube_svc.run_action(cube["id"], "构建Cube")
        assert entry["构建版本"] == "v2"
        assert entry["预计算单元数"] == 1
        snaps = store.snapshot.list_snapshots("cube_builder", cube["id"])
        assert len(snaps) == 2
        assert snaps[-1]["version_label"] == "v2"

    def test_invalidate_clears_built_cells(self, clean_tables, cube_svc):
        cube = _create_cube(cube_svc, "cube_inv", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")
        entry, _ = cube_svc.run_action(cube["id"], "失效Cube")
        assert entry["status"] == "已失效"
        assert entry["预计算单元数"] == 0
        assert entry["预计算单元明细"] == []


# ======================================================================
# Section C: 星型查询按实际生成的单元决定取数来源
# ======================================================================


class TestStarQuerySource:
    """星型查询根据实际生成的单元决定是否使用 Cube。"""

    def test_query_hits_cube_when_combo_built(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_hit", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        entry, message = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "Cube预计算"
        assert entry["取数来源"] == "cube_hit"
        assert entry["数据版本"] == "v1"
        assert entry["命中单元"] == "门店"
        assert "Cube路径" in entry["查询路径"]

    def test_query_miss_generates_detail_path(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_core_only", "核心组合")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        entry, message = query_svc.run_action(query["id"], "验证查询")
        # 核心组合未覆盖「门店」组合，回退明细查询路径
        assert entry["来源类型"] == "明细查询"
        assert "明细查询" in entry["取数来源"]
        assert entry["数据版本"]
        assert "明细路径" in entry["明细查询路径"]
        assert "测试销售事实" in entry["明细查询路径"]
        assert entry["命中Cube"] == ""

    def test_detail_path_preserves_measure_and_scope(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_none_src", "无预计算")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        # 切换来源后仍保留原始度量口径和范围
        assert entry["度量口径"] == "gmv_amt.order_cnt"
        assert "ds=最近30天" in entry["复算范围"]
        assert "门店" in entry["复算范围"]
        assert "SUM(gmv_amt)" in entry["明细查询路径"]
        assert "SUM(order_cnt)" in entry["明细查询路径"]
        assert "ds=最近30天" in entry["明细查询路径"]
        assert entry["度量选择"] == "gmv_amt.order_cnt"
        assert entry["过滤条件"] == "ds=最近30天"

    def test_query_exact_core_combo_still_hits(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_core_hit", "核心组合")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="日期.门店.产品类目.渠道")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "Cube预计算"
        assert entry["命中单元"] == "日期.门店.产品类目.渠道"

    def test_no_precompute_cube_never_hit(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_no_hit", "无预计算", dims="日期.门店")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="日期.门店")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "明细查询"

    def test_invalidated_cube_not_hit(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_inv_hit", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")
        cube_svc.run_action(cube["id"], "失效Cube")

        query = _create_query(query_svc, group="门店")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "明细查询"


# ======================================================================
# Section D: 策略切换后的冲突与复算
# ======================================================================


class TestStrategySwitchRecompute:
    """切换到核心策略后，历史查询与新预计算范围冲突，复算纠正取数来源。"""

    def test_historical_query_flagged_after_strategy_switch(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_switch", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["取数来源"] == "cube_switch"

        # 切换到核心组合并重建：旧粒度「门店」不在新范围内
        cube["预计算策略"] = "核心组合"
        entry, message = cube_svc.run_action(cube["id"], "构建Cube")
        assert entry["构建版本"] == "v2"
        assert store.find("star_query", query["id"])["_待复算"] is True

    def test_revalidate_falls_back_and_keeps_caliber(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_recompute", "全部预计算")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        query_svc.run_action(query["id"], "验证查询")

        cube["预计算策略"] = "核心组合"
        cube_svc.run_action(cube["id"], "构建Cube")

        # 复算：必须说明实际取数来源，且保留原始口径
        entry, message = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "明细查询"
        assert "明细查询" in entry["取数来源"]
        assert entry["复算次数"] == 2
        assert entry["度量口径"] == "gmv_amt.order_cnt"
        assert entry["度量选择"] == "gmv_amt.order_cnt"
        assert entry["_待复算"] is False
        assert "取数来源" in message

    def test_revalidate_hits_again_when_strategy_restored(self, clean_tables, cube_svc, query_svc):
        cube = _create_cube(cube_svc, "cube_restore", "核心组合")
        cube_svc.run_action(cube["id"], "构建Cube")

        query = _create_query(query_svc, group="门店")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "明细查询"

        cube["预计算策略"] = "全部预计算"
        cube_svc.run_action(cube["id"], "构建Cube")
        entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert entry["来源类型"] == "Cube预计算"
        assert entry["取数来源"] == "cube_restore"
        assert entry["数据版本"] == "v2"

    def test_out_of_strategy_cells_not_counted_in_build(self, clean_tables, cube_svc, query_svc):
        """当前策略之外的单元不能计入本次构建，也不能被查询命中。"""
        cube = _create_cube(cube_svc, "cube_scope", "核心组合")
        entry, _ = cube_svc.run_action(cube["id"], "构建Cube")
        assert entry["预计算单元数"] == 1
        assert "门店" not in entry["预计算单元明细"]

        query = _create_query(query_svc, group="门店")
        q_entry, _ = query_svc.run_action(query["id"], "验证查询")
        assert q_entry["来源类型"] == "明细查询"
        assert q_entry["命中单元"] == ""
