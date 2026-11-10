# Documentation Index

This page is a quick navigator for the main repository documents.

## Core documents

- [System Overview](system_overview.md)
- [Architecture](architecture.md)
- [Schema](schema.md)
- [Benchmark](benchmark.md)
- [Roadmap](roadmap.md)

## Project planning and positioning

- [Open-Source Strategy](open_source_strategy.md)
- [Repository Structure](repository_structure.md)
- [Deployment Guidance](deployment_guidance.md)
- [OpenClaw Setup Guide](openclaw_setup.md)
- [Next Steps](next_steps.md)
- [v0.2 Reliable Alice](v0.2_reliable_alice.md)
- [v0.3 Messaging + Excel](v0.3_messaging_excel.md)
- [v0.4 Benchmark Cases](../benchmark/cases/README.md)
- [v0.4 Rule + Policy Baseline](../benchmark/reports/v0.4_rule_policy_baseline.md)

## Handoff

- [HANDOFF 2026-10-18](../HANDOFF_2026-10-18_cn.md)

## S10 repair round (2026-10-17)

- [Baseline](repair/R00_baseline_cn.md)
- [Repair contracts v2](repair/contracts_v2_cn.md)
- [G1 report](repair/G1_report_cn.md)
- [R11 worker report](repair/R11_report_cn.md)
- [Frontend integration](repair/frontend_integration_cn.md)
- [G2–G4 status](repair/G2_G4_status_cn.md)
- [Issue matrix](repair/issue_matrix.md)
- [Verification log](repair/verification_2026-10-17.md)

## Agent specifications

- [Alice Inventory Specification](../agents/alice_inventory_spec.md)

## Recommended reading path

1. Start with **System Overview** to understand the project problem and workflows.
2. Read **Architecture** to see the system decomposition.
3. Read **Schema** to understand the event-first data model.
4. Read **Benchmark** for the evaluation framing.
5. Read **Roadmap** and **Next Steps** for implementation priorities.

## Runtime boundary

The core is standalone. OpenClaw and other assistant or messaging runtimes are optional
gateway adapters and must not own policy or operational writes.
