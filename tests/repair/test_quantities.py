"""R05/F07: quantity decoding, dimensions and lawful aggregation."""

from __future__ import annotations

from chemclaw_lab import answers, db


def _sample(lab, label="S-1", sample_id="smp_q"):
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) VALUES (?, 'P1', ?)",
        (sample_id, label),
    )
    return sample_id


def _event(lab, page, text, sample_id, amount, unit, kind="operation"):
    transcript = lab.add_transcript(page["page_id"], [text])
    return lab.add_event(
        project_id="P1",
        sample_id=sample_id,
        amount=amount,
        unit=unit,
        event_kind=kind,
        raw_text=text,
        evidence_refs=[
            {
                "page_id": page["page_id"],
                "transcript_revision": 1,
                "block_id": transcript["block_ids"][0],
            }
        ],
    )


def _answer(lab, question):
    member = lab.context("hum_q", memberships={"P1": "member"})
    conn = db.connect(lab.db_path)
    try:
        return answers.build_answer(conn, member, question)
    finally:
        conn.close()


def test_same_dimension_conversion_produces_0_7_g(lab):
    sample_id = _sample(lab)
    page = lab.add_page("P1")
    _event(lab, page, "实际取用 0.5 g S-1", sample_id, 0.5, "g")
    _event(lab, page, "实际取用 200 mg S-1", sample_id, 200.0, "mg")
    answer = _answer(lab, "S-1 实际取用总量？")
    derived = [c for c in answer["claims"] if c["kind"] == "derived"]
    assert len(derived) == 1
    assert abs(derived[0]["value"] - 0.7) < 1e-12
    assert derived[0]["unit"] == "g"
    assert len(derived[0]["inputs"]) == 2
    rules = [item["conversion_rule"] for item in derived[0]["conversion_inputs"]]
    assert any("mg→g" in rule for rule in rules)
    # Every input claim is unit-decoded (never null from a dict-shaped unit).
    amount_claims = [
        c for c in answer["claims"]
        if c["kind"] == "reported_fact" and c["field_pointer"] == "fields.amount"
    ]
    assert all(c["unit"] and c["normalized_unit"] for c in amount_claims)


def test_mass_and_volume_are_never_added(lab):
    sample_id = _sample(lab, "S-2", "smp_mix")
    page = lab.add_page("P1")
    _event(lab, page, "实际取用 0.5 g S-2", sample_id, 0.5, "g")
    _event(lab, page, "实际取用 2 mL S-2", sample_id, 2.0, "mL")
    answer = _answer(lab, "S-2 实际取用总量？")
    assert all(c.get("value") != 2.5 for c in answer["claims"])
    assert any(g["kind"] == "dimension_mismatch" for g in answer["gaps"])
    derived = [c for c in answer["claims"] if c["kind"] == "derived"]
    assert all(c["dimension"] in ("mass", "volume") for c in derived)
    assert not any(
        c["dimension"] == "mass" and c["value"] == 2.5 for c in derived
    )


def test_missing_units_do_not_sum(lab):
    sample_id = _sample(lab, "S-3", "smp_nounit")
    page = lab.add_page("P1")
    _event(lab, page, "实际取用 2 S-3", sample_id, 2.0, None)
    _event(lab, page, "实际取用 3 S-3", sample_id, 3.0, None)
    answer = _answer(lab, "S-3 实际取用总量？")
    assert all(c.get("value") != 5 for c in answer["claims"])
    assert any(g["kind"] == "missing_unit" for g in answer["gaps"])
    assert not [c for c in answer["claims"] if c["kind"] == "derived"]


def test_plan_amount_never_enters_actual_total(lab):
    sample_id = _sample(lab, "S-4", "smp_plan")
    page = lab.add_page("P1")
    _event(lab, page, "明天拟取 2 mL S-4", sample_id, 2.0, "mL", kind="plan")
    _event(lab, page, "实际取用 1 mL S-4", sample_id, 1.0, "mL", kind="operation")
    answer = _answer(lab, "S-4 用量情况？")
    by_usage: dict[str, list] = {}
    for claim in answer["claims"]:
        if claim["field_pointer"] == "fields.amount":
            by_usage.setdefault(claim["usage_semantics"], []).append(claim)
    assert by_usage["计划/配方用量（未执行）"][0]["value"] == 2.0
    assert by_usage["实际取用"][0]["value"] == 1.0
    assert not [c for c in answer["claims"] if c["kind"] == "derived"]
