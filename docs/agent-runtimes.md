# Which runtime runs which agent

Three runtimes, three jobs. Do not collapse them.

## Claude Code — the build pipeline (Phases 1-5)
Runs the SDLC team from `.claude/agents/`. Interactive, human at the gates, full repo
context, Git-aware. Included in your Claude plan, so marginal cost is zero.
**This is where all code gets written.**

## deepagents — headless CI (later, optional)
Same agent definitions via `agents_loader.py`. Use when you want a contract change to
trigger regeneration + tests automatically in CI with no human in the terminal. Costs API
tokens, so defer until the pipeline is stable. Budget ~$10 here and set a hard spend cap.

## Hermes Agent — the ops/monitor agent (runtime, forever)
`.claude/agents/ops-monitor.md` deploys here as a Hermes skill.
Hermes fits because it has exactly what a monitor needs: a heartbeat for scheduled ticks,
persistent file-based memory across restarts, sandboxed terminal backends (local, Docker,
SSH, Modal), signed outbound webhooks, chat gateways including Slack, and it is
model-agnostic — point it at a cheap model via OpenRouter and running costs round to zero.

Pick Hermes over OpenClaw: same category, actively developed, and `hermes claw migrate`
imports OpenClaw settings if you ever started there. One less decision.

Hermes does NOT write pipeline code. It has no business in the SDLC. Keeping the agent
that can act at runtime separate from the agents that write code is the whole safety model.

    Claude Code  ->  writes code       ->  Git  ->  human gate  ->  platform
    Hermes       ->  watches platform  ->  Slack
                     (read-only SQL)
