# Alerting and monitoring

## Why alerting lives OUTSIDE the platform

Databricks Free Edition restricts outbound internet access from compute to a limited set
of trusted domains. A Slack webhook fired from inside a notebook may silently fail.
Independently of that, you do not want alerting logic embedded in transformation code.

## Pattern

    pipeline run  ->  control.run_registry / dq_results / file_audit  (platform)
                                    |
                                    v
                      monitor process (outside platform)
                                    |
                                    v
                            Slack / email / Telegram

The monitor polls `control.*` on a schedule, evaluates:
- run failed / run missing past `expected_by + grace_minutes`
- `dq_results` with severity=error
- row count outside `row_count_deviation_pct` vs trailing 7-run median (the dip/spike check)
- freshness SLA breach

## Where OpenClaw fits
This monitor is the ONE place a long-lived chat-channel agent earns its keep: it has a
heartbeat, persistent memory, Slack/Telegram channels, and can hold a conversation about
an alert ("why did orders drop 40% yesterday?") by querying the control tables.

Do **not** put OpenClaw in the build pipeline. It is a personal-assistant runtime, not a
governed SDLC harness.
