"""端到端测试：计划导入、实发批次、等待区间、冲突报告、持久化、规模。"""
import pytest

DAY = "2026-11-20"


def sample_plan():
    return {
        "trips": [
            {"trip_id": "A", "planned": 100},
            {"trip_id": "B", "planned": 200},
            {"trip_id": "C", "planned": 140},
            {"trip_id": "D", "planned": 300},
        ],
        "connections": [
            {"pred": "A", "succ": "B", "min_interval": 30},
            {"pred": "A", "succ": "C", "min_interval": 10},
            {"pred": "B", "succ": "D", "min_interval": 20},
        ],
    }


def import_plan(client, day=DAY, plan=None):
    r = client.post(f"/days/{day}/plan", json=plan or sample_plan())
    assert r.status_code == 201, r.text
    return r


def post_actuals(client, updates, day=DAY):
    return client.post(f"/days/{day}/actuals", json={"updates": updates})


def get_trip(client, trip_id, day=DAY):
    return client.get(f"/days/{day}/trips/{trip_id}")


# ---------------------------------------------------------------- 计划导入

def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_import_plan_and_query_without_actual(client):
    import_plan(client)
    r = get_trip(client, "A")
    assert r.status_code == 200
    body = r.json()
    assert body["planned"] == 100
    assert body["actual"] is None
    assert body["wait"] is None


def test_duplicate_plan_rejected(client):
    import_plan(client)
    r = client.post(f"/days/{DAY}/plan", json=sample_plan())
    assert r.status_code == 409


def test_delete_day_and_reimport(client):
    import_plan(client)
    assert client.delete(f"/days/{DAY}").status_code == 200
    assert get_trip(client, "A").status_code == 404
    import_plan(client)
    assert get_trip(client, "A").status_code == 200


@pytest.mark.parametrize(
    "plan, fragment",
    [
        # 环
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "Y", "planned": 2}],
                "connections": [
                    {"pred": "X", "succ": "Y", "min_interval": 1},
                    {"pred": "Y", "succ": "X", "min_interval": 1},
                ],
            },
            "环",
        ),
        # 超过 4 条前序
        (
            {
                "trips": [{"trip_id": f"P{i}", "planned": 1} for i in range(5)]
                + [{"trip_id": "T", "planned": 2}],
                "connections": [
                    {"pred": f"P{i}", "succ": "T", "min_interval": 1} for i in range(5)
                ],
            },
            "4",
        ),
        # 自接续
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}],
                "connections": [{"pred": "X", "succ": "X", "min_interval": 1}],
            },
            "自身",
        ),
        # 引用不存在的班次
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}],
                "connections": [{"pred": "X", "succ": "ZZ", "min_interval": 1}],
            },
            "不存在",
        ),
        # 重复接续
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "Y", "planned": 2}],
                "connections": [
                    {"pred": "X", "succ": "Y", "min_interval": 1},
                    {"pred": "X", "succ": "Y", "min_interval": 2},
                ],
            },
            "重复",
        ),
        # 重复班次
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "X", "planned": 2}],
                "connections": [],
            },
            "重复",
        ),
        # 计划分钟越界
        (
            {"trips": [{"trip_id": "X", "planned": 2880}], "connections": []},
            "超出",
        ),
    ],
)
def test_plan_validation_errors(client, plan, fragment):
    r = client.post(f"/days/{DAY}/plan", json=plan)
    assert r.status_code == 400, r.text
    assert fragment in r.text


def test_infeasible_plan_rejected(client):
    # 接续链总长超出运营日：0 + 2000 + 2000 = 4000 > 2879
    plan = {
        "trips": [
            {"trip_id": "X", "planned": 0},
            {"trip_id": "Y", "planned": 0},
            {"trip_id": "Z", "planned": 0},
        ],
        "connections": [
            {"pred": "X", "succ": "Y", "min_interval": 2000},
            {"pred": "Y", "succ": "Z", "min_interval": 2000},
        ],
    }
    r = client.post(f"/days/{DAY}/plan", json=plan)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["total_conflicts"] >= 1
    assert detail["conflicts"][0]["kind"] == "no_feasible_completion"
    # 未写入任何数据
    assert client.get(f"/days/{DAY}").status_code == 404


# ---------------------------------------------------------------- 实发批次

def test_record_and_known_wait(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 200}])
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 1
    r = post_actuals(client, [{"trip_id": "B", "actual": 240}])
    assert r.status_code == 200, r.text
    body = get_trip(client, "B").json()
    assert body["actual"] == 240
    # 已知等待：200 + 30 - 200 = 30；A 已知，区间塌缩为 [30, 30]
    assert body["wait"] == {"known_wait": 30, "min_wait": 30, "max_wait": 30}


def test_actual_earlier_than_plan_rejected(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 90}])
    assert r.status_code == 400
    assert "早于计划" in r.text
    assert get_trip(client, "A").json()["actual"] is None


def test_actual_beyond_horizon_rejected(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 2880}])
    assert r.status_code == 400


def test_unknown_trip_in_batch(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "ZZ", "actual": 100}])
    assert r.status_code == 400


def test_duplicate_trip_in_batch(client):
    import_plan(client)
    r = post_actuals(
        client, [{"trip_id": "A", "actual": 100}, {"trip_id": "A", "actual": 101}]
    )
    assert r.status_code == 400


def test_revoke_actual(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 150}])
    assert get_trip(client, "A").json()["actual"] == 150
    r = post_actuals(client, [{"trip_id": "A", "actual": None}])
    assert r.status_code == 200
    body = get_trip(client, "A").json()
    assert body["actual"] is None
    assert body["wait"] is None


def test_direct_conflict_reports_chain_and_rolls_back(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    r = post_actuals(client, [{"trip_id": "B", "actual": 220}])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["total_conflicts"] == 1
    conflict = detail["conflicts"][0]
    assert conflict["kind"] == "actual_before_required"
    assert conflict["trip_id"] == "B"
    assert conflict["actual"] == 220
    assert conflict["required_earliest"] == 230
    assert [n["trip_id"] for n in conflict["chain"]] == ["A", "B"]
    assert conflict["edges"] == [{"pred": "A", "succ": "B", "min_interval": 30}]
    # 回滚：原实发保留，B 仍未补录
    assert get_trip(client, "A").json()["actual"] == 200
    assert get_trip(client, "B").json()["actual"] is None


def test_indirect_chain_conflict(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 400}])
    # D 实发 310，但 A->B->D 要求 400+30+20 = 450
    r = post_actuals(client, [{"trip_id": "D", "actual": 310}])
    assert r.status_code == 409, r.text
    conflict = r.json()["detail"]["conflicts"][0]
    assert conflict["required_earliest"] == 450
    assert [n["trip_id"] for n in conflict["chain"]] == ["A", "B", "D"]
    assert conflict["chain"][1]["actual"] is None  # 中间班 B 尚无实发
    assert conflict["edges"] == [
        {"pred": "A", "succ": "B", "min_interval": 30},
        {"pred": "B", "succ": "D", "min_interval": 20},
    ]
    assert get_trip(client, "D").json()["actual"] is None


def test_conflict_beyond_horizon(client):
    import_plan(client)
    # A 实发 2879：B 需 >= 2909、C 需 >= 2889、D 需 >= 2929，均超出运营日
    r = post_actuals(client, [{"trip_id": "A", "actual": 2879}])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    kinds = {c["kind"] for c in detail["conflicts"]}
    assert kinds == {"no_feasible_completion"}
    assert {c["trip_id"] for c in detail["conflicts"]} == {"B", "C", "D"}
    assert get_trip(client, "A").json()["actual"] is None


def test_batch_atomicity(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    # B=240 本身合法，但同批 C=145 与 A=200 矛盾（需 >= 210）→ 整批回滚
    r = post_actuals(client, [{"trip_id": "B", "actual": 240}, {"trip_id": "C", "actual": 145}])
    assert r.status_code == 409
    assert get_trip(client, "B").json()["actual"] is None
    assert get_trip(client, "C").json()["actual"] is None
    assert get_trip(client, "A").json()["actual"] == 200


# ---------------------------------------------------------------- 等待区间

def test_wait_interval_and_shared_predecessor(client):
    """共用前序 A 未知：更正/撤销 B 改变 A 的可行时刻，C 的区间随之更新。"""
    import_plan(client)
    post_actuals(
        client,
        [
            {"trip_id": "B", "actual": 230},
            {"trip_id": "C", "actual": 400},
            {"trip_id": "D", "actual": 500},
        ],
    )

    # A 未知：EF(A)=100（计划），LF(A)=min(230-30, 400-10)=200
    wait = get_trip(client, "C").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 70}  # 200+10-140
    wait = get_trip(client, "B").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 30}  # 200+30-200
    # D 的前序 B 已知(230)：230+20-300 < 0 → 等待恒为 0，区间塌缩
    wait = get_trip(client, "D").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 0}

    # 更正 B：LF(A) 变为 230 → C 最多解释 100 分钟
    post_actuals(client, [{"trip_id": "B", "actual": 260}])
    assert get_trip(client, "C").json()["wait"]["max_wait"] == 100
    assert get_trip(client, "B").json()["wait"]["max_wait"] == 60  # 230+30-200

    # 撤销 B：LF(A) 只受 C 约束（390）→ C 最多解释 260 分钟
    post_actuals(client, [{"trip_id": "B", "actual": None}])
    assert get_trip(client, "C").json()["wait"]["max_wait"] == 260

    # 补录 A=180：区间塌缩到已知等待 50
    post_actuals(client, [{"trip_id": "A", "actual": 180}])
    wait = get_trip(client, "C").json()["wait"]
    assert wait == {"known_wait": 50, "min_wait": 50, "max_wait": 50}


def test_list_actuals_with_known_wait(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "B", "actual": 230}, {"trip_id": "C", "actual": 400}])
    post_actuals(client, [{"trip_id": "A", "actual": 180}])
    r = client.get(f"/days/{DAY}/actuals")
    assert r.status_code == 200
    items = {i["trip_id"]: i for i in r.json()["items"]}
    assert items["A"]["known_wait"] == 0
    assert items["B"]["known_wait"] == 10  # 180+30-200
    assert items["C"]["known_wait"] == 50  # 180+10-140


def test_day_overview_endpoints(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 150}])
    r = client.get("/days")
    assert r.json()["days"] == [
        {"day": DAY, "version": 1, "trips": 4, "with_actual": 1}
    ]
    r = client.get(f"/days/{DAY}")
    assert r.json() == {
        "day": DAY,
        "version": 1,
        "trips": 4,
        "with_actual": 1,
        "connections": 3,
    }
    assert client.get("/days").json()["days"] == [
        {"day": DAY, "version": 1, "trips": 4, "with_actual": 1}
    ]
    assert client.get("/days/2099-01-01").status_code == 404
    assert client.get(f"/days/{DAY}/trips/ZZ").status_code == 404


# ---------------------------------------------------------------- 持久化

def test_persistence_across_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "persist.db"))
    with TestClient(app) as c1:
        import_plan(c1)
        post_actuals(c1, [{"trip_id": "A", "actual": 125}])

    # 模拟重启：全新客户端（应用本身无内存状态，全部落在 SQLite）
    with TestClient(app) as c2:
        body = c2.get(f"/days/{DAY}/trips/A").json()
        assert body["actual"] == 125
        assert body["wait"] == {"known_wait": 0, "min_wait": 0, "max_wait": 0}


# ---------------------------------------------------------------- 深度与规模

def test_deep_chain(client):
    """2000 班接续长链：深链传播不爆栈，区间计算正确。"""
    n = 2000
    trips = [{"trip_id": f"T{i}", "planned": 100} for i in range(n)]
    conns = [
        {"pred": f"T{i}", "succ": f"T{i+1}", "min_interval": 1} for i in range(n - 1)
    ]
    r = client.post(f"/days/{DAY}/plan", json={"trips": trips, "connections": conns})
    assert r.status_code == 201, r.text
    post_actuals(client, [{"trip_id": "T0", "actual": 150}])
    post_actuals(client, [{"trip_id": f"T{n-1}", "actual": 2200}])
    wait = get_trip(client, f"T{n-1}").json()["wait"]
    # EF(T1998)=150+1998=2148 → min=2148+1-100；LF(T1998)=2200-1=2199 → max=2199+1-100
    assert wait == {"known_wait": 0, "min_wait": 2049, "max_wait": 2100}


def test_scale_50k(client):
    """单日 5 万班次：导入、批录、单班查询（局部子图，无需遍历全日）。"""
    chains, length = 2500, 20
    trips, conns = [], []
    for c in range(chains):
        for i in range(length):
            trips.append({"trip_id": f"C{c}T{i}", "planned": 100})
            if i:
                conns.append(
                    {"pred": f"C{c}T{i-1}", "succ": f"C{c}T{i}", "min_interval": 1}
                )
    assert len(trips) == 50000
    r = client.post(f"/days/{DAY}/plan", json={"trips": trips, "connections": conns})
    assert r.status_code == 201, r.text

    assert post_actuals(client, [{"trip_id": "C0T0", "actual": 150}]).status_code == 200
    assert post_actuals(client, [{"trip_id": "C0T19", "actual": 200}]).status_code == 200
    wait = get_trip(client, "C0T19").json()["wait"]
    # EF(C0T18)=150+18=168 → min=168+1-100；LF(C0T18)=200-1=199 → max=199+1-100
    assert wait == {"known_wait": 0, "min_wait": 69, "max_wait": 100}

    # 与已录实发无关的另一条链：查询只走局部子图
    assert post_actuals(client, [{"trip_id": "C2499T10", "actual": 150}]).status_code == 200
    wait = get_trip(client, "C2499T10").json()["wait"]
    # EF(C2499T9)=109 → min=10；LF=150-1=149 → max=149+1-100=50
    assert wait == {"known_wait": 0, "min_wait": 10, "max_wait": 50}


# ---------------------------------------------------------------- 计划换版

def replan_plan(mapping, trips=None, connections=None):
    return {
        "version": 1,
        "trips": trips
        or [
            {"trip_id": "A", "planned": 100},
            {"trip_id": "B", "planned": 200},
            {"trip_id": "C", "planned": 140},
            {"trip_id": "D", "planned": 300},
        ],
        "connections": connections
        if connections is not None
        else [
            {"pred": "A", "succ": "B", "min_interval": 30},
            {"pred": "A", "succ": "C", "min_interval": 10},
            {"pred": "B", "succ": "D", "min_interval": 20},
        ],
        "mapping": mapping,
    }


def post_replan(client, body, day=DAY):
    return client.post(f"/days/{day}/plan/replan", json=body)


def test_replan_renames_and_migrates_actuals(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])

    r = post_replan(
        client,
        replan_plan(
            [{"new_trip_id": "A2", "old_trip_id": "A"}],
            trips=[
                {"trip_id": "A2", "planned": 100},
                {"trip_id": "B", "planned": 200},
                {"trip_id": "C", "planned": 140},
                {"trip_id": "D", "planned": 300},
            ],
            connections=[
                {"pred": "A2", "succ": "B", "min_interval": 30},
                {"pred": "A2", "succ": "C", "min_interval": 10},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["previous_version"] == 1
    assert body["migrated"] == 1

    # 旧班号已不在新版计划中；实发随对照迁到新班号
    assert get_trip(client, "A").status_code == 404
    migrated = get_trip(client, "A2").json()
    assert migrated["version"] == 2
    assert migrated["actual"] == 200
    assert migrated["wait"] == {"known_wait": 0, "min_wait": 0, "max_wait": 0}
    # 改版后的接续表生效：B 的前序是 A2
    assert get_trip(client, "B").json()["planned"] == 200


def test_replan_swaps_trip_numbers(client):
    """允许两班互换编号：实发按对照跟随班次内容。"""
    import_plan(client)
    post_actuals(client, [{"trip_id": "B", "actual": 240}])

    # B 与 C 互换编号：新 B 实际上是旧 C，新 C 是旧 B；接续随之调整。
    r = post_replan(
        client,
        replan_plan(
            [
                {"new_trip_id": "B", "old_trip_id": "C"},
                {"new_trip_id": "C", "old_trip_id": "B"},
            ],
            connections=[
                {"pred": "A", "succ": "C", "min_interval": 30},
                {"pred": "A", "succ": "B", "min_interval": 10},
                {"pred": "C", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 201, r.text
    assert r.json()["migrated"] == 1
    assert get_trip(client, "B").json()["actual"] is None
    assert get_trip(client, "C").json()["actual"] == 240
    assert get_trip(client, "C").json()["version"] == 2


def test_replan_rejects_stale_version(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 130}])
    body = replan_plan([{"new_trip_id": "A", "old_trip_id": "A"}])
    body["version"] = 2
    r = post_replan(client, body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["current_version"] == 1
    assert get_trip(client, "A").json()["version"] == 1
    assert get_trip(client, "A").json()["actual"] == 130


def test_replan_rejects_unmapped_actual(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 130}])
    post_actuals(client, [{"trip_id": "B", "actual": 240}])
    r = post_replan(
        client, replan_plan([{"new_trip_id": "A", "old_trip_id": "A"}])
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["missing_old_trip_ids"] == ["B"]
    # 计划与实发原样保留
    assert get_trip(client, "A").json()["actual"] == 130
    assert get_trip(client, "B").json()["actual"] == 240
    assert get_trip(client, "A").json()["version"] == 1


def test_replan_rejects_mapping_errors(client):
    import_plan(client)
    # 新班号不在新版计划中
    r = post_replan(
        client, replan_plan([{"new_trip_id": "ZZ", "old_trip_id": "A"}])
    )
    assert r.status_code == 400, r.text
    # 对照中新班号重复
    r = post_replan(
        client,
        replan_plan(
            [
                {"new_trip_id": "A", "old_trip_id": "B"},
                {"new_trip_id": "A", "old_trip_id": "C"},
            ]
        ),
    )
    assert r.status_code == 400, r.text
    # 对照中旧班号重复
    r = post_replan(
        client,
        replan_plan(
            [
                {"new_trip_id": "B", "old_trip_id": "A"},
                {"new_trip_id": "C", "old_trip_id": "A"},
            ]
        ),
    )
    assert r.status_code == 400, r.text
    # 新计划本身非法（计划早于 0）
    bad = replan_plan([], trips=[{"trip_id": "A", "planned": -1}], connections=[])
    assert post_replan(client, bad).status_code in (400, 422)
    assert get_trip(client, "A").json()["version"] == 1


def test_replan_rejects_infeasible_new_plan_and_keeps_data(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 400}])
    # 迁入 A=400，但新版接续 A->B->D 要求 D >= 450，与新计划 D=300 无实发冲突可解
    # 另加一条 A=400 -> 后续要求超过运营日，使新计划不可行。
    r = post_replan(
        client,
        replan_plan(
            [{"new_trip_id": "A", "old_trip_id": "A"}],
            connections=[
                {"pred": "A", "succ": "B", "min_interval": 2500},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 409, r.text
    assert "校验" in r.text or "不可行" in r.text
    assert get_trip(client, "A").json()["actual"] == 400
    assert get_trip(client, "A").json()["version"] == 1


def test_replan_rejects_migrated_actual_earlier_than_new_plan(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 120}])
    # 新版把 A 的计划推迟到 150，迁入实发 120 早于新计划 → 拒绝
    r = post_replan(
        client,
        replan_plan(
            [{"new_trip_id": "A", "old_trip_id": "A"}],
            trips=[
                {"trip_id": "A", "planned": 150},
                {"trip_id": "B", "planned": 200},
                {"trip_id": "C", "planned": 140},
                {"trip_id": "D", "planned": 300},
            ],
            connections=[
                {"pred": "A", "succ": "B", "min_interval": 30},
                {"pred": "A", "succ": "C", "min_interval": 10},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 409, r.text
    assert get_trip(client, "A").json()["actual"] == 120
    assert get_trip(client, "A").json()["version"] == 1


def test_actuals_require_current_version_after_replan(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 130}])
    assert post_replan(
        client, replan_plan([{"new_trip_id": "A", "old_trip_id": "A"}])
    ).status_code == 201

    # 缺版号 → 拒绝
    r = post_actuals(client, [{"trip_id": "B", "actual": 240}])
    assert r.status_code == 409, r.text
    assert "版号" in r.text
    # 旧版号 → 拒绝
    r = client.post(
        f"/days/{DAY}/actuals",
        json={"version": 1, "updates": [{"trip_id": "B", "actual": 240}]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["current_version"] == 2
    assert get_trip(client, "B").json()["actual"] is None
    # 当前版号 → 通过
    r = client.post(
        f"/days/{DAY}/actuals",
        json={"version": 2, "updates": [{"trip_id": "B", "actual": 240}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert get_trip(client, "B").json()["actual"] == 240


def test_actuals_before_first_replan_keep_legacy_format(client):
    """旧库/首次换版前：不带 version 的批次仍可提交。"""
    import_plan(client)
    assert post_actuals(client, [{"trip_id": "A", "actual": 130}]).status_code == 200
    assert post_actuals(client, [{"trip_id": "B", "actual": 240}]).status_code == 200


def test_replan_migrates_latest_actual_submitted_during_verification(client, monkeypatch):
    """换版验算期间提交的补录/更正/撤销，切换时按最新已提交实发迁移。"""
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 130}])

    from app import logic

    original = logic.feasibility
    interleaved = {"done": False}

    def feasibility_with_interleaved_write(trips, preds, succs, horizon_minutes):
        if not interleaved["done"]:
            interleaved["done"] = True
            # 换版验算在写锁之外，此刻另一请求提交实发更正，应当成功。
            r = client.post(
                f"/days/{DAY}/actuals",
                json={"version": 1, "updates": [{"trip_id": "A", "actual": 160}]},
            )
            assert r.status_code == 200, r.text
        return original(trips, preds, succs, horizon_minutes)

    monkeypatch.setattr(logic, "feasibility", feasibility_with_interleaved_write)
    r = post_replan(client, replan_plan([{"new_trip_id": "A", "old_trip_id": "A"}]))
    assert r.status_code == 201, r.text
    assert interleaved["done"] is True
    # 切换按验算期间最新提交的 A=160 迁移，而非验算开始时的快照 130。
    assert get_trip(client, "A").json()["actual"] == 160
    assert get_trip(client, "A").json()["version"] == 2


def test_interleaved_new_actual_and_revoke_during_verification(client, monkeypatch):
    """验算期间同时补录新班次、撤销另一班次，切换迁移包含这些最新更改。"""
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    post_actuals(client, [{"trip_id": "B", "actual": 240}])

    from app import logic

    original = logic.feasibility
    interleaved = {"done": False}

    def feasibility_with_interleaved_batch(trips, preds, succs, horizon_minutes):
        if not interleaved["done"]:
            interleaved["done"] = True
            r = client.post(
                f"/days/{DAY}/actuals",
                json={
                    "version": 1,
                    "updates": [
                        {"trip_id": "C", "actual": 400},  # 补录
                        {"trip_id": "B", "actual": None},  # 撤销
                    ],
                },
            )
            assert r.status_code == 200, r.text
        return original(trips, preds, succs, horizon_minutes)

    monkeypatch.setattr(logic, "feasibility", feasibility_with_interleaved_batch)
    r = post_replan(
        client,
        replan_plan(
            [
                {"new_trip_id": "A2", "old_trip_id": "A"},
                {"new_trip_id": "B", "old_trip_id": "B"},
                {"new_trip_id": "C", "old_trip_id": "C"},
            ],
            trips=[
                {"trip_id": "A2", "planned": 100},
                {"trip_id": "B", "planned": 200},
                {"trip_id": "C", "planned": 140},
                {"trip_id": "D", "planned": 300},
            ],
            connections=[
                {"pred": "A2", "succ": "B", "min_interval": 30},
                {"pred": "A2", "succ": "C", "min_interval": 10},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 201, r.text
    assert get_trip(client, "A2").json()["actual"] == 200
    assert get_trip(client, "B").json()["actual"] is None
    assert get_trip(client, "C").json()["actual"] == 400
    assert get_trip(client, "C").json()["version"] == 2


def test_replan_rejects_interleaved_conflict_and_keeps_latest_actuals(
    client, monkeypatch
):
    """验算期间提交的实发若使迁入后不可行，换版拒绝且保留最新实发。"""
    import_plan(client)
    from app import logic

    original = logic.feasibility
    interleaved = {"done": False}

    def feasibility_then_conflicting_write(trips, preds, succs, horizon_minutes):
        if not interleaved["done"]:
            interleaved["done"] = True
            # 新计划把 A 的计划改为 100（不变），B 改为 450；
            # 期间提交 B=240 仍与新计划自洽，但随后把 A 更正为 500 会让 B 需要 >=530。
            r = client.post(
                f"/days/{DAY}/actuals",
                json={"version": 1, "updates": [{"trip_id": "A", "actual": 500}]},
            )
            assert r.status_code == 200, r.text
        return original(trips, preds, succs, horizon_minutes)

    monkeypatch.setattr(logic, "feasibility", feasibility_then_conflicting_write)
    # 新版把 A 计划提到 510，迁入实发 A=500 早于新计划且接续不可行 → 拒绝
    r = post_replan(
        client,
        replan_plan(
            [{"new_trip_id": "A", "old_trip_id": "A"}],
            trips=[
                {"trip_id": "A", "planned": 510},
                {"trip_id": "B", "planned": 200},
                {"trip_id": "C", "planned": 140},
                {"trip_id": "D", "planned": 300},
            ],
            connections=[
                {"pred": "A", "succ": "B", "min_interval": 30},
                {"pred": "A", "succ": "C", "min_interval": 10},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 409, r.text
    # 换版未生效，验算期间提交的 A=500 保留
    body = get_trip(client, "A").json()
    assert body["version"] == 1
    assert body["actual"] == 500


def test_replan_reused_trip_number_does_not_inherit_old_actual(client):
    """新计划重用旧班号时，实发只按对照走，不因同号自动继承。"""
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    # 新 A 是另一班；旧 A 的实发迁到 A2。
    r = post_replan(
        client,
        replan_plan(
            [{"new_trip_id": "A2", "old_trip_id": "A"}],
            trips=[
                {"trip_id": "A", "planned": 600},
                {"trip_id": "A2", "planned": 100},
                {"trip_id": "B", "planned": 200},
                {"trip_id": "C", "planned": 140},
                {"trip_id": "D", "planned": 300},
            ],
            connections=[
                {"pred": "A2", "succ": "B", "min_interval": 30},
                {"pred": "A2", "succ": "C", "min_interval": 10},
                {"pred": "B", "succ": "D", "min_interval": 20},
            ],
        ),
    )
    assert r.status_code == 201, r.text
    assert get_trip(client, "A").json()["actual"] is None
    assert get_trip(client, "A2").json()["actual"] == 200


def test_replan_unknown_day(client):
    r = post_replan(client, replan_plan([]))
    assert r.status_code == 404, r.text


def test_replan_rejects_unknown_old_trip(client):
    import_plan(client)
    r = post_replan(
        client, replan_plan([{"new_trip_id": "A", "old_trip_id": "NOPE"}])
    )
    assert r.status_code == 400, r.text
    assert "旧计划" in r.text


def test_replan_without_actuals_and_repeated_versions(client):
    import_plan(client)
    assert post_replan(client, replan_plan([])).status_code == 201
    body = get_trip(client, "A").json()
    assert body["version"] == 2
    # 第二次换版须带版号 2
    stale = replan_plan([])
    r = post_replan(client, stale)
    assert r.status_code == 409
    assert r.json()["detail"]["current_version"] == 2
    stale["version"] = 2
    r = post_replan(client, stale)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 3
    assert get_trip(client, "A").json()["version"] == 3


def test_replan_persists_across_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "replan.db"))
    with TestClient(app) as c1:
        import_plan(c1)
        post_actuals(c1, [{"trip_id": "A", "actual": 200}])
        r = post_replan(
            c1,
            replan_plan(
                [{"new_trip_id": "A2", "old_trip_id": "A"}],
                trips=[
                    {"trip_id": "A2", "planned": 100},
                    {"trip_id": "B", "planned": 200},
                    {"trip_id": "C", "planned": 140},
                    {"trip_id": "D", "planned": 300},
                ],
                connections=[
                    {"pred": "A2", "succ": "B", "min_interval": 30},
                    {"pred": "A2", "succ": "C", "min_interval": 10},
                    {"pred": "B", "succ": "D", "min_interval": 20},
                ],
            ),
        )
        assert r.status_code == 201, r.text

    with TestClient(app) as c2:
        body = c2.get(f"/days/{DAY}/trips/A2").json()
        assert body["version"] == 2
        assert body["actual"] == 200
        assert c2.get(f"/days/{DAY}").json()["version"] == 2
        # 重启后旧版号仍被拒绝
        r = c2.post(
            f"/days/{DAY}/actuals",
            json={"version": 1, "updates": [{"trip_id": "B", "actual": 240}]},
        )
        assert r.status_code == 409


def test_old_database_migrates_to_initial_version(tmp_path, monkeypatch):
    """旧库（days 无 version 列）升级后，计划直接可查初始版号，且沿用原批录格式。"""
    import sqlite3

    from fastapi.testclient import TestClient
    from app.main import app

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE days (day TEXT PRIMARY KEY);
        CREATE TABLE trips (
            day TEXT NOT NULL REFERENCES days(day) ON DELETE CASCADE,
            trip_id TEXT NOT NULL,
            planned INTEGER NOT NULL,
            topo INTEGER NOT NULL,
            actual INTEGER,
            PRIMARY KEY (day, trip_id)
        );
        CREATE TABLE connections (
            day TEXT NOT NULL REFERENCES days(day) ON DELETE CASCADE,
            pred TEXT NOT NULL,
            succ TEXT NOT NULL,
            min_interval INTEGER NOT NULL,
            PRIMARY KEY (day, pred, succ)
        );
        """
    )
    conn.execute("INSERT INTO days(day) VALUES (?)", (DAY,))
    conn.executemany(
        "INSERT INTO trips(day, trip_id, planned, topo, actual) VALUES (?,?,?,?,?)",
        [(DAY, "A", 100, 0, 130), (DAY, "B", 200, 1, None)],
    )
    conn.execute(
        "INSERT INTO connections(day, pred, succ, min_interval) VALUES (?,?,?,?)",
        (DAY, "A", "B", 30),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(path))
    with TestClient(app) as c:
        body = c.get(f"/days/{DAY}/trips/A").json()
        assert body["version"] == 1
        assert body["actual"] == 130
        # 首次换版前沿用原批录格式（不带 version）
        r = c.post(
            f"/days/{DAY}/actuals",
            json={"updates": [{"trip_id": "B", "actual": 240}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 1
