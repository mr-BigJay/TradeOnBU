# Four-hour market outlook

Docker Compose enables `OUTLOOK_FOUR_HOUR_ENABLED=1`. This refreshes the existing outlook after each UTC 4H close, independently of Verified Plans candidates. The scheduler checks every minute and starts at or just after **00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC** (03:35, 07:35, 11:35, 15:35, 19:35, 23:35 Tehran). Delivery follows model completion.

On first activation the existing outlook schedule is enabled once. Subsequent dashboard disable actions are respected across restarts. The dashboard displays fixed slots instead of an editable daily hour. Setting the environment flag to 0 restores legacy scheduling; Verified Plans mode still suppresses that legacy daily job.

- Six scheduled outlook API calls/day at most, **in addition to** existing candidate-review calls. No extra daily report call.
- A shared analysis lock prevents concurrent inference. A busy lock is retried by later ticks until 20 minutes after candle close. Older slots are skipped on restart rather than generating stale paid reports.
- The latest closed candle must match the reserved UTC slot before invoking the API. One persisted attempt per slot prevents duplicate generation across restarts and Tehran midnight. Only confirmed pre-API failures can be retried manually.
- Completed reports (including WAIT/no candidate) are sent to the configured Telegram destination using the existing integration. Delivery status is recorded in report context; Telegram failures do not regenerate the analysis. Unknown delivery outcomes are not automatically retried to avoid duplicates. Disabled/unconfigured Telegram still permits reports to appear in the dashboard.
- The existing daily/weekly context, channel context and conditional scenarios remain. These reports do not create orders. Verified entry scanning and Paper risk controls are unchanged.

## Upgrade

Use `scripts/deploy_verified_paper.sh <merged commit>`. Its SQLite backup is required before deployment. Startup migrates the report uniqueness key from date to date + UTC slot, preserving historical IDs, stored reports and charts. Historical reports receive slot 0. Do not simply roll back the image after new same-day reports exist: the old application assumes one report/day. A full rollback requires a coordinated database restore from the backup and consideration of any newer Paper activity.

Check the dashboard next-run time and report context `telegram_delivery`; server health alone does not prove Telegram delivery. Do not disclose bot tokens when sharing diagnostics.
