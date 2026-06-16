"""Stage-2 processor adapters: I/O orchestration for the risk + dedup gates.

These are the *imperative shell* around the pure rules in
``lcp.core.rules.risk_rules`` / ``dedup_rules``: they load inputs (manifest,
local job index, published-URL index), call the pure scoring, map the structured
result onto a :class:`~lcp.core.state.JobState` (+ :class:`ReviewReason`), and
write audit events. No business judgement lives here — only wiring."""
