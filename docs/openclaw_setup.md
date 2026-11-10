# Optional OpenClaw Adapter Guide

OpenClaw is an optional assistant runtime around ChemClaw Core. It is not required to
run or test v0.4.

## Integration contract

An OpenClaw tool or adapter should:

1. receive the channel message;
2. preserve the original text and stable channel message ID;
3. call ChemClaw ingestion with `source="openclaw"` and `source_message_id`;
4. render the returned event, policy decision, and status;
5. route confirmation replies to the HTTP confirmation endpoint.

It must not:

- update inventory tables directly;
- let an LLM override hard policy;
- retry a message with a new ID after an uncertain response;
- send files or images to a model unless the user explicitly triggers analysis.

## HTTP bridge

```bash
chemclaw serve-api
```

Map the external user once, then use `ChemClawClient` from
`chemclaw_lab.gateway` to send messages and resolve pending events. Each retry must
reuse the original channel message ID.

Read-only inventory questions should use `ChemClawClient.ask_inventory`. This path does
not create inventory events or transactions.

The CLI ingestion commands remain useful for diagnostics, but an adapter should use the
HTTP boundary rather than launch a subprocess per message.
