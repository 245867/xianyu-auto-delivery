"""批量发布队列的失败策略与未知提交恢复回归测试。"""

from __future__ import annotations

import uuid
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import idempotency
import product_template as pt
import rate_limiter


class SubmissionUnknown(RuntimeError):
    submission_status = "submission_unknown"


@pytest.fixture
def isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "DB_PATH", tmp_path / "templates.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(pt, "_running", {})
    monkeypatch.setattr(pt, "_night_ban", lambda: (True, ""))
    monkeypatch.setattr(
        rate_limiter,
        "wait",
        lambda *args, **kwargs: True,
    )

    def make_job(failure_policy: str, names=("A", "B")):
        template = pt.create_template({
            "name": f"tpl-{uuid.uuid4().hex[:8]}",
            "title_tpl": "{{name}} 二手",
            "desc_tpl": "{{name}}，成色好，支持验货",
            "price": 10,
            "quantity": 1,
            "delivery": "包邮",
        })
        assert template["ok"], template
        created = pt.create_job(
            template["id"],
            [{"name": name, "price": 10 + i} for i, name in enumerate(names)],
            f"acct-{uuid.uuid4().hex[:8]}",
            dry_run=False,
            failure_policy=failure_policy,
        )
        assert created["ok"], created
        return created["job_id"]

    return make_job


def test_failure_policy_is_persisted_and_validated(isolated_queue):
    job_id = isolated_queue("pause")
    job = pt.get_job(job_id)["job"]
    assert job["failure_policy"] == "pause"
    assert job["failurePolicy"] == "pause"

    template = pt.create_template({
        "name": "bad-policy",
        "title_tpl": "{{name}}",
        "desc_tpl": "{{name}}",
        "price": 10,
    })
    result = pt.create_job(
        template["id"],
        [{"name": "x"}],
        "acct-validation",
        dry_run=True,
        failure_policy="continue",
    )
    assert result == {
        "ok": False,
        "error": "failurePolicy 只能是 skip 或 pause",
    }


def test_skip_continues_after_ordinary_failure(isolated_queue, monkeypatch):
    job_id = isolated_queue("skip")
    calls = []

    def publish(job, row):
        calls.append(row["seq"])
        if row["seq"] == 1:
            raise RuntimeError("普通失败")
        return {"itemId": "9002"}

    monkeypatch.setattr(pt, "_publish_one", publish)
    result = pt.run_job(job_id)
    job = result["job"]

    assert calls == [1, 2]
    assert job["status"] == "done"
    assert job["failed"] == 1
    assert job["done"] == 1
    assert job["items"][0]["failure_kind"] == "failed"


def test_pause_stops_after_ordinary_failure(isolated_queue, monkeypatch):
    job_id = isolated_queue("pause")
    calls = []

    def publish(job, row):
        calls.append(row["seq"])
        if row["seq"] == 1:
            raise RuntimeError("普通失败")
        return {"itemId": "9002"}

    monkeypatch.setattr(pt, "_publish_one", publish)
    result = pt.run_job(job_id)
    job = result["job"]

    assert calls == [1]
    assert job["status"] == "paused"
    assert job["items"][0]["status"] == "failed"
    assert job["items"][1]["status"] == "pending"

    # 普通失败不属于提交未知，人工确认后可以直接继续下一行。
    assert pt.resume_job(job_id)["ok"] is True


def test_unknown_always_pauses_and_requires_single_row_verification(
    isolated_queue,
    monkeypatch,
):
    job_id = isolated_queue("skip")
    calls = []

    def unknown(job, row):
        calls.append(row["seq"])
        raise SubmissionUnknown("提交阶段断网，结果未知")

    monkeypatch.setattr(pt, "_publish_one", unknown)
    result = pt.run_job(job_id)
    job = result["job"]

    assert calls == [1]
    assert job["status"] == "paused"
    assert job["items"][0]["failure_kind"] == "submission_unknown"
    assert job["items"][1]["status"] == "pending"

    resumed = pt.resume_job(job_id)
    assert resumed["ok"] is False
    assert resumed["requires_verification"] is True
    assert [item["seq"] for item in resumed["unknown_items"]] == [1]

    unresolved = pt.reset_job_unknown(
        job_id,
        seq=1,
        outcome="still_unknown",
        reason="平台待审核列表尚未刷新",
    )
    assert unresolved["ok"] is True
    assert [item["seq"] for item in unresolved["remaining_unknown"]] == [1]
    assert pt.resume_job(job_id)["ok"] is False

    released = pt.reset_job_unknown(
        job_id,
        seq=1,
        outcome="not_received",
        reason="已翻完我发布的全部页面，没有该商品",
    )
    assert released["ok"] is True
    assert released["remaining_unknown"] == []
    assert pt.resume_job(job_id)["ok"] is True

    monkeypatch.setattr(
        pt,
        "_publish_one",
        lambda job, row: {"itemId": f"91{int(row['seq']):03d}"},
    )
    completed = pt.run_job(job_id)["job"]
    assert completed["status"] == "done"
    assert completed["done"] == 2


def test_pending_ledger_from_interrupted_claim_blocks_rerun(
    isolated_queue,
    monkeypatch,
):
    job_id = isolated_queue("skip", names=("C",))
    job = pt.get_job(job_id)["job"]
    key = idempotency.make_key(
        "batch_publish",
        job["cookie_id"],
        job["items"][0]["title"],
        job["items"][0]["price"],
    )
    assert idempotency.claim(key, "publish", note="模拟 claim 后进程中断")["claimed"]

    monkeypatch.setattr(
        pt,
        "_publish_one",
        lambda job, row: pytest.fail("中断记录未核对前不得再次发布"),
    )
    blocked = pt.run_job(job_id)
    assert blocked["ok"] is False
    assert blocked["requires_verification"] is True
    assert blocked["unknown_items"][0]["ledger_status"] == "pending"

    released = pt.reset_job_unknown(
        job_id,
        seq=1,
        outcome="not_received",
        reason="人工核对确认平台没有收到后才放行",
    )
    assert released["ok"] is True
    assert idempotency.get(key) is None
    assert pt.resume_job(job_id)["ok"] is True
