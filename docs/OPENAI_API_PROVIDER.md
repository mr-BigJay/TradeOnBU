# Paid OpenAI API / GPT-6 Luna High

OpenAI API billing is separate from ChatGPT Subscription Sharing.
Do not upload your key to GitHub, chat, or the browser. Revoke any exposed keys first.

To switch TradeOnBU from SIWC to paid API:
1. Merge, build and deploy this PR. Back up the database and .env beforehand.
2. Generate a *new* restricted project API key.
3. On the VPS edit /opt/tradeonbu/.env with an editor and set:
   - AI_PROVIDER=openai_api
   - OPENAI_API_KEY=<new secret key>
4. Run: cd /opt/tradeonbu && docker compose up -d --build --no-deps tradeonbu
5. On the dashboard click 'Test API and Luna'. It checks GPT-6 Luna availability via the model metadata API without performing a billed inference.
6. Confirm analysis model = gpt-6-luna, effort = high. Enable unattended-consent and 60-minute scheduling.
7. Run one manual analysis after verifying API billing. Inspect history and actual token usage.

Switch back with AI_PROVIDER=siwc and rebuild/recreate only the backend service; existing SIWC credentials remain in the database.
Never output the API key in command stdout/logs. The backend receives it via the ignored .env file. Keep it readable only by the appropriate server administrator (chmod 600).

Ensure the API billing project has credit/payment. Creating an API key alone does not grant billing or availability. Configure usage alerts and do not mistake project budgets for a hard instantaneous cutoff.

No automatic failover to SIWC on API failure. API errors are logged as failed analysis, rather than spending subscription quota.
