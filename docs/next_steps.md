# Next Steps

## Immediate hardening

1. Add a reviewed chemical-alias table rather than hard-coded benchmark aliases.
2. Support English and Chinese word numbers through an isolated normalization layer.
3. Add `discard` and `expire` to the event schema and deterministic policy.
4. Define explicit project-reference syntax before attempting free-text attribution.
5. Add parser-only and optional LLM adapters as benchmark baselines.

These changes should improve categories rather than special-case individual benchmark
phrases.

## v0.5 experiment-memory vertical slice

1. Define executable experiment-event and evidence-reference schemas.
2. Implement Agent B extraction from text and attachment descriptions.
3. Add follow-up requirements for missing project, sample, temperature, and duration.
4. Generate evidence-grounded weekly summaries.
5. Extend the v0.4 runner with Task C/D cases and unsupported-claim metrics.

## Gateway hardening

1. Package one deployable OpenClaw or WebChat adapter.
2. Add structured request logging without message-body leakage.
3. Define backup and restore procedures.
4. Add configurable real-lab policy thresholds before a production pilot.

## Deferred intentionally

- Agent C planning and booking
- production WeChat integration
- heavy UI work
- automatic image analysis without explicit user action
