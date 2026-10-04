"""现场停机对账（POST /devices/{id}/reconciliation）端到端测试。

覆盖：现场区间裁剪到窗口并合并、系统侧取截至序号最后修订的固化明细（三类段并集）、
仅在已结算班次覆盖分钟比较、现场有/系统无与系统有/现场无的最大连续区间、
窗口内无班次分钟另计未对账（不算差异）、半开端点相接、序号固定与可重放、
更正/裁定改变系统视图、历史修订明细不可用、序号越界、未知设备、非法时间输入等错误。

运行方式：docker compose exec app pytest tests/test_reconciliation.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "shift_result_segments",
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
PATH = "/devices/CNC-01/reconciliation"


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _shift(day: int, sh: int = 8, eh: int = 16, device="CNC-01") -> dict:
    return {
        "device_id": device,
        "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
        "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
    }


def _event(source: str, version: int, day: int, sh: int, sm: int, eh: int, em: int, reason: str) -> dict:
    return {
        "device_id": "CNC-01",
        "source_id": source,
        "version": version,
        "start": f"2026-10-{day:02d}T{sh:02d}:{sm:02d}:00+08:00",
        "end": f"2026-10-{day:02d}T{eh:02d}:{em:02d}:00+08:00",
        "reason": reason,
    }


def _site(day: int, sh: int, sm: int, eh: int, em: int) -> dict:
    return {
        "start": f"2026-10-{day:02d}T{sh:02d}:{sm:02d}:00+08:00",
        "end": f"2026-10-{day:02d}T{eh:02d}:{em:02d}:00+08:00",
    }


def _settle(client, shift_id) -> None:
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _reconcile(client, body, device="CNC-01"):
    return client.post(f"/devices/{device}/reconciliation", json=body)


WINDOW = {
    "start": "2026-10-01T00:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}


def _setup_one_shift(client) -> int:
    """设备 + 班次 10/1 08:00-16:00（+08:00）+ 故障 10:00-12:00，结算（seq=1）。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=_shift(1))
    assert r.status_code == 201, r.text
    shift_id = r.json()["id"]
    assert (
        client.post("/events", json=_event("PLC-9", 1, 1, 10, 0, 12, 0, "failure")).status_code
        == 201
    )
    _settle(client, shift_id)
    return shift_id


# ---------------------------------------------------------------- 基本语义


def test_exact_match_no_differences(client):
    _setup_one_shift(client)
    body = {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)]}
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["seq"] == 1
    assert out["site_only"] == [] and out["site_only_minutes"] == 0
    assert out["system_only"] == [] and out["system_only_minutes"] == 0
    assert out["unreconciled"] == [] and out["unreconciled_minutes"] == 0
    assert out["compared_minutes"] == 8 * 60  # 班次 08:00-16:00 全覆盖
    assert len(out["shifts"]) == 1
    s = out["shifts"][0]
    assert s["revision"] == 1
    assert s["covered_start"].endswith(("+00:00", "Z"))
    assert s["covered_end"].endswith(("+00:00", "Z"))


def test_site_and_system_differences_both_sides(client):
    _setup_one_shift(client)
    # 现场：11:00-13:00
    # 系统：10:00-12:00 故障
    # 重合 11:00-12:00；site_only 12:00-13:00（60m）；system_only 10:00-11:00（60m）
    body = {**WINDOW, "site_intervals": [_site(1, 11, 0, 13, 0)]}
    out = _reconcile(client, body).json()
    assert [(x["start"][11:16], x["end"][11:16]) for x in out["site_only"]] == [
        ("04:00", "05:00")  # 12:00-13:00 +08:00 = 04:00-05:00 UTC
    ]
    assert out["site_only_minutes"] == 60
    assert [(x["start"][11:16], x["end"][11:16]) for x in out["system_only"]] == [
        ("02:00", "03:00")  # 10:00-11:00 +08:00 = 02:00-03:00 UTC
    ]
    assert out["system_only_minutes"] == 60
    # 区间按时间顺序、含分钟数
    assert all(x["minutes"] == 60 for x in out["site_only"] + out["system_only"])


def test_empty_site_intervals_marks_all_system_downtime_system_only(client):
    _setup_one_shift(client)
    body = {**WINDOW, "site_intervals": []}
    out = _reconcile(client, body).json()
    assert out["site_only"] == []
    assert out["system_only_minutes"] == 120
    assert [x["minutes"] for x in out["system_only"]] == [120]


def test_three_reason_segments_union_merges_touching_into_maximal_interval(client):
    # 保养 10:00-11:00 + 故障 11:00-12:00：两类段端点相接，系统停机并集为 10:00-12:00
    assert client.post("/devices", json=DEVICE).status_code == 201
    shift_id = client.post("/shifts", json=_shift(1)).json()["id"]
    assert (
        client.post("/events", json=_event("MES-1", 1, 1, 10, 0, 11, 0, "maintenance")).status_code
        == 201
    )
    assert (
        client.post("/events", json=_event("PLC-9", 1, 1, 11, 0, 12, 0, "failure")).status_code
        == 201
    )
    _settle(client, shift_id)

    out = _reconcile(client, {**WINDOW, "site_intervals": []}).json()
    # 最大连续区间：两个不同原因但相接的段合并为一个 120 分钟区间
    assert [(x["start"][11:16], x["end"][11:16], x["minutes"]) for x in out["system_only"]] == [
        ("02:00", "04:00", 120)
    ]


def test_site_intervals_clipped_to_window_and_merged(client):
    _setup_one_shift(client)
    # 窗口裁剪：两段重叠现场区间，且一段伸出窗口起点；合并后 00:00-12:00（+08:00）
    # 与系统 10:00-12:00 完全重合（在班次 08:00-16:00 内），无差异
    body = {
        **WINDOW,
        "site_intervals": [
            {"start": "2026-09-30T23:30:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
            {"start": "2026-10-01T10:30:00+08:00", "end": "2026-10-01T12:00:00+08:00"},
        ],
    }
    out = _reconcile(client, body).json()
    # 裁剪后 00:00-12:00：00:00-08:00 不在班次 → 未对账 480 分钟；
    # 08:00-10:00 现场有系统无 → site_only 120；10:00-12:00 重合
    assert out["site_only_minutes"] == 120
    assert out["unreconciled_minutes"] == 8 * 60
    assert out["system_only_minutes"] == 0
    unre = out["unreconciled"]
    assert len(unre) == 1
    # 窗口起点（10/1 00:00 +08:00 = 9/30 16:00 UTC）到班次起点 08:00（UTC 00:00）
    assert unre[0]["start"].endswith("T16:00:00+00:00")
    assert unre[0]["end"].endswith("T00:00:00+00:00")
    assert unre[0]["minutes"] == 480


def test_unreconciled_minutes_not_counted_as_differences(client):
    # 两个班次之间留空隙：班次 A 08:00-12:00；班次 B 14:00-16:00
    assert client.post("/devices", json=DEVICE).status_code == 201
    a = client.post("/shifts", json=_shift(1, 8, 12)).json()["id"]
    b = client.post("/shifts", json=_shift(1, 14, 16)).json()["id"]
    _settle(client, a)
    _settle(client, b)
    # 现场停机 11:00-15:00：
    #   11:00-12:00 在 A（系统无）→ site_only 60
    #   12:00-14:00 无班次        → unreconciled 120
    #   14:00-15:00 在 B（系统无）→ site_only 60
    # site_only 两段不跨过未对账间隙合并
    body = {**WINDOW, "site_intervals": [_site(1, 11, 0, 15, 0)]}
    out = _reconcile(client, body).json()
    assert out["site_only_minutes"] == 120
    assert [x["minutes"] for x in out["site_only"]] == [60, 60]
    assert out["unreconciled_minutes"] == 120
    assert [x["minutes"] for x in out["unreconciled"]] == [120]
    assert out["system_only"] == []
    assert out["compared_minutes"] == 6 * 60


def test_window_partial_overlap_clips_both_shifts_and_segments(client):
    _setup_one_shift(client)  # 班次 08:00-16:00，故障 10:00-12:00
    # 对账窗口只覆盖 11:00-13:00：系统段裁剪为 11:00-12:00
    body = {
        "start": "2026-10-01T11:00:00+08:00",
        "end": "2026-10-01T13:00:00+08:00",
        "site_intervals": [],
    }
    out = _reconcile(client, body).json()
    assert out["compared_minutes"] == 120  # 11:00-13:00
    assert out["system_only_minutes"] == 60  # 仅 11:00-12:00
    assert out["start"].endswith("+08:00") and out["end"].endswith("+08:00")  # 窗口原样回显


def test_half_open_adjacency_not_overlapping(client):
    # 现场停机恰好到系统段起点 / 从系统段终点开始：端点相接不算差异
    _setup_one_shift(client)  # 系统 10:00-12:00
    body = {
        **WINDOW,
        "site_intervals": [
            _site(1, 9, 30, 10, 0),  # 09:30-10:00，与 10:00 端点相接 → site_only
            _site(1, 10, 0, 12, 0),  # 完全重合
        ],
    }
    out = _reconcile(client, body).json()
    assert out["site_only_minutes"] == 30
    assert out["system_only_minutes"] == 0


# ---------------------------------------------------------------- 序号与重放


def test_seq_pins_historical_revision(client):
    shift_id = _setup_one_shift(client)  # 系统 10:00-12:00，seq=1
    # 迟到更正：故障缩短为 10:00-11:00 → 修订2（seq=2）
    r = client.post("/events", json=_event("PLC-9", 2, 1, 10, 0, 11, 0, "failure"))
    assert r.status_code == 201

    body = {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)], "seq": 1}
    out = _reconcile(client, body).json()
    assert out["seq"] == 1
    assert out["shifts"][0]["revision"] == 1
    # 现场 10:00-12:00 vs 旧系统 10:00-12:00（修订1）→ 无差异
    assert out["site_only_minutes"] == 0
    assert out["system_only_minutes"] == 0

    # 同请求在当前序号重放：系统只有 10:00-11:00 → 11:00-12:00 site_only
    body_latest = {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)]}
    out2 = _reconcile(client, body_latest).json()
    assert out2["seq"] == 2
    assert out2["shifts"][0]["revision"] == 2
    assert out2["site_only_minutes"] == 60


def test_default_seq_is_latest_and_explicit_seq_replays_identically(client):
    _setup_one_shift(client)  # max seq=1
    body = {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)]}
    first = _reconcile(client, body).json()
    assert first["seq"] == 1

    # 之后结算一个新班次（seq=2），不带 seq 的新请求推进到 2
    shift2 = client.post("/shifts", json=_shift(2, 8, 16)).json()["id"]
    _settle(client, shift2)
    wide = {
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-03T00:00:00+08:00",
        "site_intervals": [],
    }
    latest = _reconcile(client, wide).json()
    assert latest["seq"] == 2
    assert {s["shift_id"] for s in latest["shifts"]} == {1, shift2}

    # 显式 seq=1 重放：仍只看到班次 1 的修订 1，结果与首次一致
    replay = _reconcile(client, {**wide, "seq": 1}).json()
    assert replay["seq"] == 1
    assert [s["shift_id"] for s in replay["shifts"]] == [1]
    assert replay["system_only_minutes"] == first["system_only_minutes"]


def test_seq_zero_empty_reconciliation(client):
    _setup_one_shift(client)
    out = _reconcile(client, {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)], "seq": 0}).json()
    assert out["seq"] == 0
    assert out["shifts"] == []
    assert out["compared_minutes"] == 0
    # 无任何已结算班次：现场分钟全部未对账，不算差异
    assert out["unreconciled_minutes"] == 120
    assert out["site_only"] == [] and out["system_only"] == []


def test_adjudication_changes_system_view(client):
    shift_id = _setup_one_shift(client)  # 故障 10:00-12:00
    # 裁定 12:00-13:00 为故障：系统停机扩大到 13:00，产生修订 2
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-1",
            "start": "2026-10-01T12:00:00+08:00",
            "end": "2026-10-01T13:00:00+08:00",
            "reason": "failure",
        },
    )
    assert r.status_code == 201 and r.json()["revision"] == 2

    body = {**WINDOW, "site_intervals": [_site(1, 10, 0, 12, 0)]}
    out = _reconcile(client, body).json()
    # 系统 10:00-13:00；现场仅到 12:00 → 12:00-13:00 system_only
    assert out["system_only_minutes"] == 60
    assert out["site_only_minutes"] == 0


# ---------------------------------------------------------------- 错误


def test_segments_unavailable_rejected_not_substituted(client):
    shift_id = _setup_one_shift(client)  # 修订1 seq=1 有明细
    # 模拟"结构引入前的历史修订"：其明细不可用
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))

    r = _reconcile(client, {**WINDOW, "site_intervals": []})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"

    # 之后更正产生带明细的新修订（seq=2）；seq=1 仍明确拒绝，seq=2 可用
    assert (
        client.post("/events", json=_event("PLC-9", 2, 1, 10, 0, 11, 0, "failure")).status_code
        == 201
    )
    r1 = _reconcile(client, {**WINDOW, "site_intervals": [], "seq": 1})
    assert r1.status_code == 422
    assert r1.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"
    r2 = _reconcile(client, {**WINDOW, "site_intervals": [], "seq": 2})
    assert r2.status_code == 200, r2.text
    assert r2.json()["shifts"][0]["revision"] == 2


def test_seq_beyond_committed_range_rejected(client):
    _setup_one_shift(client)
    r = _reconcile(client, {**WINDOW, "site_intervals": [], "seq": 999})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_RECONCILIATION_SEQ"

    # 空库指定 seq=1 同样越界；seq=0 合法
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"TRUNCATE {t} CASCADE"))
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = _reconcile(client, {**WINDOW, "site_intervals": [], "seq": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_RECONCILIATION_SEQ"
    assert _reconcile(client, {**WINDOW, "site_intervals": [], "seq": 0}).status_code == 200


def test_unknown_device_404(client):
    r = _reconcile(client, {**WINDOW, "site_intervals": []}, device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_invalid_inputs_rejected(client):
    _setup_one_shift(client)

    def _post(body):
        return _reconcile(client, body)

    bad_bodies = [
        # 窗口缺时区
        {**WINDOW, "start": "2026-10-01T00:00:00"},
        # 窗口未对齐整分钟
        {**WINDOW, "start": "2026-10-01T00:00:30+08:00"},
        # start ≥ end
        {**WINDOW, "start": "2026-10-02T00:00:00+08:00"},
        {**WINDOW, "end": "2026-10-01T00:00:00+08:00"},
        # 现场区间非法
        {**WINDOW, "site_intervals": [{"start": "2026-10-01T10:00:00", "end": "2026-10-01T11:00:00+08:00"}]},
        {**WINDOW, "site_intervals": [{"start": "2026-10-01T11:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"}]},
        {**WINDOW, "site_intervals": [{"start": "2026-10-01T12:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"}]},
        # seq 负数
        {**WINDOW, "seq": -1},
    ]
    for body in bad_bodies:
        r = _post(body)
        assert r.status_code == 422, body
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR", body

    # 非时间格式同样 422
    r = _post({**WINDOW, "start": "not-a-time"})
    assert r.status_code == 422
