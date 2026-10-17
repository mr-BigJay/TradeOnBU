# SIWC model selection

TradeOnBU supports two explicit model profiles for ChatGPT/SIWC analysis:

- `gpt-6.1-sol` with `reasoning.effort=max` (the existing default).
- `gpt-6-luna` with `reasoning.effort=high` (the lower-cost API model profile).

The dashboard saves the selected profile in the existing `chatgpt_credentials.preferred_model` field; no database migration is needed. The selection is independent from the scheduler interval and does not trigger an analysis when saved.

A model is selectable only when it is returned by the current SIWC connection's model catalog, except for the already configured current model. This avoids saving a model that the connected account does not advertise. API list pricing does not guarantee availability or a specific quota cost through SIWC/ChatGPT subscription usage.

If the selected model returns an error, TradeOnBU records the failed run and does not silently fall back to another model. Switch back to GPT-6.1 Sol in the dashboard if the SIWC connection cannot use GPT-6 Luna.
