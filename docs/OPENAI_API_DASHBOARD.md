## Dashboard configuration (recommended)

The dashboard now supports selecting AI source and saving the OpenAI API key.
The key is encrypted using the existing server-side Fernet master key and persisted
in the existing SettingEntry table. The key is never returned by status endpoints,
never stored in browser localStorage, and never sent to GitHub.

1. Revoke any key previously exposed in screenshots and generate a new project key.
2. Open Dashboard → AI settings → Manage connection.
3. Paste key in password field, save it, and clear the field.
4. Choose OpenAI API (Luna / High), save source, then click Test API and Luna.
5. Check that API billing has funds; metadata probe doesn't guarantee credit.
6. Enable explicit unattended consent and hourly scheduler.

SIWC remains available and separate. Switching providers in the dashboard does not
remove SIWC credentials. The stored key is only decrypted server-side as needed.
Back up both the database and data/secrets/master.key (without disclosing either).
Avoid rotating/deleting master.key without re-encrypting secrets.

Optional existing env-based configuration remains supported. Values saved in the
dashboard take precedence over env configuration.

## Luna High / Max reasoning selector

Under the OpenAI API provider, GPT-6 Luna is the **only** API model. The separate
dashboard reasoning control lets you choose `high` (default) or `max` and
save the choice without re-entering the API key. Its value is stored in the
existing `SettingEntry` table under category `ai`, key
`api_reasoning_effort` (no database migration).

The setting is separate from SIWC model preferences. The next manual or
scheduled inference request sends `gpt-6-luna` and the saved
`reasoning.effort`. The connection test displays the saved effort, but is
only a model-availability probe; it does not test successful paid inference.

Max is not a flat-priced tier: the output (including reasoning) tokens may
increase. The model response usage metadata is recorded with each completed
analysis, so you can compare the actual cost of the two levels.
