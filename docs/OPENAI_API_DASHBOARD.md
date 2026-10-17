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
