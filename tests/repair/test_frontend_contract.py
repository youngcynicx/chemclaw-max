"""Frontend contract: project-scoped sample listing for the read bridge."""

from __future__ import annotations

import pytest

from chemclaw_lab.capabilities import CapabilityError


def test_samples_list_is_project_scoped(lab):
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) VALUES "
        "('smp_p1', 'P1', 'S-1'), ('smp_p2', 'P2', 'S-2')"
    )
    member = lab.context("hum_front", memberships={"P1": "member"})
    result = lab.invoke(
        member,
        "query.samples.list",
        {"project_ids": ["P1"]},
        operation_id="op-front-samples",
    )
    assert result["result"]["scope"] == ["P1"]
    assert [s["sample_id"] for s in result["result"]["samples"]] == ["smp_p1"]
    assert result["result"]["samples"][0]["event_count"] == 0

    # No explicit filter still derives only the principal's visible scope.
    derived = lab.invoke(
        member, "query.samples.list", {}, operation_id="op-front-samples-all"
    )
    assert derived["result"]["scope"] == ["P1"]
    assert all(s["project_id"] == "P1" for s in derived["result"]["samples"])

    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            member,
            "query.samples.list",
            {"project_ids": ["P2"]},
            operation_id="op-front-samples-p2",
        )
    assert excinfo.value.code == "forbidden_scope"
