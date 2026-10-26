const labels: Record<string, string> = {
  ict_reversal: "A · برگشت قیمت", trend_continuation: "B · ادامهٔ روند", breakout_retest: "C · شکست و بازآزمایی",
};
const reasons: Record<string, string> = {
  no_candidate: "فرصت معتبری پیشنهاد نشد", not_trade_decision: "بدون برنامهٔ ورود",
  candidate_not_reported: "مدل پیشنهاد مستقل این روش را برنگرداند",
  parallel_same_direction_use_revision: "ستاپ هم‌جهت همین روش موجود است؛ نیاز به اصلاح همان ستاپ",
  entry_strategy_disabled: "روش غیرفعال است", setup_locked_active_position: "ستاپ فعلی سفارش یا معاملهٔ فعال دارد",
  candidate_strategy_mismatch: "شناسهٔ روش در پیشنهاد معتبر نیست",
  invalid_candidate_payload: "ساختار پیشنهاد معتبر نیست", invalid_candidates_object: "فهرست پیشنهادها معتبر نیست",
  strategy_rr_below_two: "نسبت سود به زیان کمتر از ۲ است",
};

export function StrategyDiagnostics({ structured }: { structured?: Record<string, unknown> }) {
  const artifacts = structured?._artifacts as Record<string, unknown> | undefined;
  const results = artifacts?.strategy_persistence as Record<string, { created?: boolean; updated?: boolean; reason?: string; setup_id?: string; lifecycle_state?: string }> | undefined;
  if (!results) return null;
  return <section dir="rtl" aria-label="نتیجهٔ ثبت فرصت‌ها">
    <h4>نتیجهٔ ثبت فرصت‌ها در این تحلیل</h4>
    <p className="hint">این وضعیت هنگام ثبت گزارش است؛ وضعیت جاری ورود و علت توقف را در صفحهٔ ستاپ‌ها ببینید.</p>
    {Object.entries(results).map(([sid, r]) => <p key={sid}>
      <strong>{labels[sid] || sid}: </strong>
      {r.created ? "فرصت ثبت شد" : r.updated ? "ستاپ قبلی به‌روزرسانی شد" : reasons[r.reason || ""] || "ثبت نشد"}
      {r.lifecycle_state && <> · <bdi>{r.lifecycle_state}</bdi></>}
      {r.setup_id && <> · شناسه: <bdi>{r.setup_id}</bdi></>}
      {r.reason && !reasons[r.reason] && !r.created && !r.updated && <> · <code>{r.reason}</code></>}
    </p>)}
  </section>;
}
