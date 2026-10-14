export type OpenFormState = {
  direction: string;
  margin_usdt: string;
  leverage: string;
  order_type: string;
  entry_price: string;
  stop_loss: string;
  take_profit: string;
  ai_setup_id: string;
};

/** Keep AI limit entry / levels when applying dashboard defaults. */
export function mergeOpenFormDefaults(
  form: OpenFormState,
  settings: Record<string, unknown>,
  preserveExecution: boolean,
): OpenFormState {
  const out = { ...form };
  if (settings.default_margin_usdt != null) out.margin_usdt = String(settings.default_margin_usdt);
  if (settings.default_leverage != null) out.leverage = String(settings.default_leverage);
  if (!preserveExecution && settings.default_order_type != null) {
    out.order_type = String(settings.default_order_type);
  }
  return out;
}
