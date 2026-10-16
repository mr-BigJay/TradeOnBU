import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";

type OppTab = "wait" | "ready" | "blocked" | "invalidated" | "expired" | "paper" | "no_trade" | "other";
type PaperSegment = "pending" | "open" | "closed";

type SetupRow = {
  entity_type: "setup";
  setup_id: string;
  symbol: string;
  direction: string;
  lifecycle_state: string;
  status_label_fa: string;
  entry_zone_low?: number;
  entry_zone_high?: number;
  entry_price?: number;
  stop_loss?: number;
  take_profit?: number;
  missing_confirmations?: string[];
  completed_confirmations?: string[];
  required_confirmations?: string[];
  rejection_label_fa?: string;
  rejection_reason?: string;
  net_rr?: number;
  display_status?: string;
  ict_confirmed?: boolean;
  age_minutes?: number;
  created_at_tehran?: string;
  expires_at_tehran?: string;
  last_monitor_at_tehran?: string;
  lifecycle_history?: unknown[];
  revision_history?: unknown[];
  order_block_reason?: string;
  live_price?: number;
};

type PaperRow = {
  entity_type: "paper_position";
  public_id: string;
  symbol: string;
  direction: string;
  status: string;
  paper_segment: PaperSegment;
  limit_entry_price?: number;
  fill_price?: number;
  stop_loss?: number;
  take_profit?: number;
  ai_setup_id?: string;
  status_label_fa?: string;
  created_at_tehran?: string;
  setup?: SetupRow;
};

type NoTradeRow = {
  entity_type: "no_trade";
  run_id: number;
  created_at_tehran?: string;
  preview?: string;
  report_fa?: string;
  decision?: string;
};

type OppItem = SetupRow | PaperRow | NoTradeRow;

type ListResponse = {
  items: OppItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  live_price?: number;
};

type SummaryResponse = {
  counts: {
    wait: number;
    ready: number;
    blocked: number;
    expired: number;
    invalidated?: number;
  };
};

const TABS: { id: OppTab; label: string }[] = [
  { id: "wait", label: "در انتظار تأیید" },
  { id: "ready", label: "آماده اجرا" },
  { id: "blocked", label: "ICT مسدود" },
  { id: "invalidated", label: "باطل‌شده" },
  { id: "expired", label: "منقضی" },
  { id: "paper", label: "Paper" },
  { id: "no_trade", label: "بدون معامله" },
  { id: "other", label: "سایر" },
];

const PAPER_SEGMENTS: { id: PaperSegment; label: string }[] = [
  { id: "pending", label: "Pending" },
  { id: "open", label: "Open" },
  { id: "closed", label: "Closed" },
];

function pageKey(tab: OppTab, paper: PaperSegment) {
  return tab === "paper" ? `paper:${paper}` : tab;
}

function lifecycleTone(state: string): string {
  if (state.startsWith("WAIT")) return "opp-state-wait";
  if (state.startsWith("READY")) return "opp-state-ready";
  if (state.includes("ICT_CONFIRMED")) return "opp-state-blocked";
  if (state === "INVALIDATED") return "opp-state-invalid";
  if (state === "EXPIRED") return "opp-state-expired";
  if (state === "PENDING_ENTRY") return "opp-state-ready";
  if (state === "POSITION_OPEN") return "opp-state-ready";
  return "opp-state-other";
}

function fmtNum(v: number | undefined | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function isSetup(row: OppItem): row is SetupRow {
  return row.entity_type === "setup";
}

function isPaper(row: OppItem): row is PaperRow {
  return row.entity_type === "paper_position";
}

export function OpportunitiesPage() {
  const [tab, setTab] = useState<OppTab>("wait");
  const [paperSegment, setPaperSegment] = useState<PaperSegment>("pending");
  const pageByKey = useRef<Record<string, number>>({});
  const [list, setList] = useState<ListResponse | null>(null);
  const [summary, setSummary] = useState<SummaryResponse["counts"] | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<OppItem | null>(null);

  const currentPage = pageByKey.current[pageKey(tab, paperSegment)] ?? 1;

  const setPageForCurrent = useCallback(
    (p: number) => {
      pageByKey.current[pageKey(tab, paperSegment)] = p;
    },
    [tab, paperSegment],
  );

  const loadSummary = useCallback(async () => {
    const d = await apiFetch<SummaryResponse>("/api/gui/opportunities/summary");
    setSummary(d.counts);
  }, []);

  const loadList = useCallback(async () => {
    const pk = pageKey(tab, paperSegment);
    const page = pageByKey.current[pk] ?? 1;
    const qs = new URLSearchParams({ tab, page: String(page), page_size: "20" });
    if (tab === "paper") qs.set("paper_status", paperSegment);
    setLoading(true);
    try {
      const d = await apiFetch<ListResponse>(`/api/gui/opportunities/items?${qs}`);
      if (d.page !== page) {
        pageByKey.current[pk] = d.page;
      }
      setList(d);
    } finally {
      setLoading(false);
    }
  }, [tab, paperSegment]);

  const refresh = useCallback(async () => {
    setErr("");
    try {
      await Promise.all([loadSummary(), loadList()]);
    } catch (e) {
      setErr(String(e));
    }
  }, [loadSummary, loadList]);

  useEffect(() => {
    refresh().catch(() => {});
  }, [refresh]);

  useEffect(() => {
    const t = setInterval(() => {
      loadSummary().catch(() => {});
      loadList().catch(() => {});
    }, 60_000);
    return () => clearInterval(t);
  }, [loadSummary, loadList]);

  function selectTab(next: OppTab) {
    setTab(next);
    if (pageByKey.current[pageKey(next, paperSegment)] == null) {
      pageByKey.current[pageKey(next, paperSegment)] = 1;
    }
  }

  function selectPaperSegment(seg: PaperSegment) {
    setPaperSegment(seg);
    pageByKey.current[pageKey("paper", seg)] = 1;
  }

  function goPage(next: number) {
    setPageForCurrent(next);
    loadList().catch((e) => setErr(String(e)));
  }

  const from = list && list.total > 0 ? (list.page - 1) * list.page_size + 1 : 0;
  const to = list ? Math.min(list.page * list.page_size, list.total) : 0;

  return (
    <div className="page opp-page">
      <header className="opp-header">
        <h1>فرصت‌های ICT</h1>
        <p className="hint">Paper only · مانیتور ~۶۰ ثانیه · داشبورد v2</p>
        {list?.live_price != null && (
          <p className="opp-live">
            BTC <span className="num-ltr">{fmtNum(list.live_price)}</span>
          </p>
        )}
      </header>

      {summary && (
        <div className="opp-summary-grid">
          <div className="opp-summary-card">
            <span className="opp-summary-label">در انتظار</span>
            <span className="opp-summary-value num-ltr">{summary.wait}</span>
          </div>
          <div className="opp-summary-card opp-summary-ready">
            <span className="opp-summary-label">آماده</span>
            <span className="opp-summary-value num-ltr">{summary.ready}</span>
          </div>
          <div className="opp-summary-card opp-summary-blocked">
            <span className="opp-summary-label">مسدود R:R</span>
            <span className="opp-summary-value num-ltr">{summary.blocked}</span>
          </div>
          <div className="opp-summary-card opp-summary-expired">
            <span className="opp-summary-label">منقضی</span>
            <span className="opp-summary-value num-ltr">{summary.expired}</span>
          </div>
        </div>
      )}

      <div className="opp-tabs-scroll" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`opp-tab ${tab === t.id ? "opp-tab-active" : ""}`}
            onClick={() => selectTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "paper" && (
        <div className="opp-subtabs">
          {PAPER_SEGMENTS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`opp-subtab ${paperSegment === s.id ? "opp-subtab-active" : ""}`}
              onClick={() => selectPaperSegment(s.id)}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      {err && <p className="toast toast-error">{err}</p>}
      {loading && !list && <p className="hint">در حال بارگذاری…</p>}

      {list && (
        <>
          <div className="opp-table-wrap desktop-only">
            <table className="opp-table">
              <thead>
                <tr>
                  <th>نماد</th>
                  <th>جهت</th>
                  <th>وضعیت</th>
                  <th>ورود</th>
                  <th>SL</th>
                  <th>TP</th>
                  <th>Net R:R</th>
                  <th>ایجاد</th>
                  <th>انقضا</th>
                </tr>
              </thead>
              <tbody>
                {list.items.length === 0 && (
                  <tr>
                    <td colSpan={9} className="hint">
                      موردی در این صفحه نیست.
                    </td>
                  </tr>
                )}
                {list.items.map((row) => (
                  <OppTableRow key={rowKey(row)} row={row} onOpen={() => setDetail(row)} />
                ))}
              </tbody>
            </table>
          </div>

          <div className="opp-cards mobile-only">
            {list.items.length === 0 && <p className="hint">موردی در این صفحه نیست.</p>}
            {list.items.map((row) => (
              <OppCard key={rowKey(row)} row={row} onOpen={() => setDetail(row)} />
            ))}
          </div>

          <nav className="opp-pagination" aria-label="صفحه‌بندی">
            <button type="button" className="btn-secondary" disabled={list.page <= 1} onClick={() => goPage(list.page - 1)}>
              قبلی
            </button>
            <span className="opp-page-info num-ltr">
              {from}–{to} از {list.total} · صفحه {list.page}/{list.total_pages}
            </span>
            <button
              type="button"
              className="btn-secondary"
              disabled={list.page >= list.total_pages}
              onClick={() => goPage(list.page + 1)}
            >
              بعدی
            </button>
          </nav>
        </>
      )}

      {detail && <DetailSheet item={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

function rowKey(row: OppItem): string {
  if (isSetup(row)) return row.setup_id;
  if (isPaper(row)) return row.public_id;
  return `run-${row.run_id}`;
}

function OppTableRow({ row, onOpen }: { row: OppItem; onOpen: () => void }) {
  if (row.entity_type === "no_trade") {
    return (
      <tr className="opp-row-click" onClick={onOpen}>
        <td>—</td>
        <td>—</td>
        <td>NO_TRADE</td>
        <td colSpan={4}>{row.preview?.slice(0, 80)}</td>
        <td className="num-ltr">{row.created_at_tehran ?? "—"}</td>
        <td>—</td>
      </tr>
    );
  }
  if (isPaper(row)) {
    const st = row.setup;
    return (
      <tr className="opp-row-click" onClick={onOpen}>
        <td>{row.symbol}</td>
        <td>
          <DirectionBadge direction={row.direction} />
        </td>
        <td>{row.status_label_fa ?? row.status}</td>
        <td className="num-ltr">{fmtNum(row.limit_entry_price ?? row.fill_price ?? st?.entry_price)}</td>
        <td className="num-ltr">{fmtNum(row.stop_loss ?? st?.stop_loss)}</td>
        <td className="num-ltr">{fmtNum(row.take_profit ?? st?.take_profit)}</td>
        <td className="num-ltr">{st?.net_rr != null ? fmtNum(st.net_rr) : "—"}</td>
        <td className="num-ltr">{row.created_at_tehran ?? "—"}</td>
        <td className="num-ltr">{st?.expires_at_tehran ?? "—"}</td>
      </tr>
    );
  }
  return (
    <tr className="opp-row-click" onClick={onOpen}>
      <td>{row.symbol}</td>
      <td>
        <DirectionBadge direction={row.direction} />
      </td>
      <td>
        <span className={`opp-state-pill ${lifecycleTone(row.lifecycle_state)}`}>{row.lifecycle_state}</span>
      </td>
      <td className="num-ltr">
        {row.entry_price != null
          ? fmtNum(row.entry_price)
          : `${fmtNum(row.entry_zone_low)}–${fmtNum(row.entry_zone_high)}`}
      </td>
      <td className="num-ltr">{fmtNum(row.stop_loss)}</td>
      <td className="num-ltr">{fmtNum(row.take_profit)}</td>
      <td className="num-ltr">{row.net_rr != null ? fmtNum(row.net_rr) : "—"}</td>
      <td className="num-ltr">{row.created_at_tehran ?? "—"}</td>
      <td className="num-ltr">{row.expires_at_tehran ?? "—"}</td>
    </tr>
  );
}

function OppCard({ row, onOpen }: { row: OppItem; onOpen: () => void }) {
  if (row.entity_type === "no_trade") {
    return (
      <button type="button" className="opp-card" onClick={onOpen}>
        <div className="opp-card-top">
          <span className="opp-state-pill opp-state-other">NO_TRADE</span>
          <span className="num-ltr history-time">{row.created_at_tehran}</span>
        </div>
        <p className="history-preview">{row.preview}</p>
      </button>
    );
  }
  if (isPaper(row)) {
    const st = row.setup;
    return (
      <button type="button" className="opp-card" onClick={onOpen}>
        <div className="opp-card-top">
          <DirectionBadge direction={row.direction} />
          <span className="opp-state-pill opp-state-ready">{row.status}</span>
        </div>
        <p className="opp-card-title">{row.symbol}</p>
        <p className="num-ltr opp-card-meta">
          Limit {fmtNum(row.limit_entry_price)} · SL {fmtNum(row.stop_loss)} · TP {fmtNum(row.take_profit)}
        </p>
        {st && (
          <p className="hint">
            Setup {st.lifecycle_state} · Net R:R <span className="num-ltr">{st.net_rr != null ? fmtNum(st.net_rr) : "—"}</span>
          </p>
        )}
      </button>
    );
  }
  return (
    <button type="button" className="opp-card" onClick={onOpen}>
      <div className="opp-card-top">
        <DirectionBadge direction={row.direction} />
        <span className={`opp-state-pill ${lifecycleTone(row.lifecycle_state)}`}>{row.lifecycle_state}</span>
      </div>
      <p className="opp-card-title">{row.symbol}</p>
      <p className="hint">{row.status_label_fa}</p>
      <p className="num-ltr opp-card-meta">
        SL {fmtNum(row.stop_loss)} · TP {fmtNum(row.take_profit)} · R:R {row.net_rr != null ? fmtNum(row.net_rr) : "—"}
      </p>
      {(row.completed_confirmations?.length ?? 0) > 0 && (
        <p className="hint">✓ {row.completed_confirmations?.join(", ")}</p>
      )}
      {(row.missing_confirmations?.length ?? 0) > 0 && (
        <p className="hint">… {row.missing_confirmations?.join(", ")}</p>
      )}
      <p className="num-ltr history-time">{row.created_at_tehran}</p>
    </button>
  );
}

function DirectionBadge({ direction }: { direction: string }) {
  const long = direction.toUpperCase() === "LONG";
  return <span className={long ? "badge badge-opp-long" : "badge badge-opp-short"}>{direction}</span>;
}

function DetailSheet({ item, onClose }: { item: OppItem; onClose: () => void }) {
  const setup: SetupRow | undefined = isSetup(item) ? item : isPaper(item) ? item.setup : undefined;

  return (
    <div className="sheet-backdrop" onClick={onClose} role="presentation">
      <div className="sheet opp-detail-sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="sheet-header">
          <h2>جزئیات</h2>
          <button type="button" className="btn-secondary chart-lightbox-close" onClick={onClose}>
            بستن
          </button>
        </div>
        <div className="sheet-body">
          {item.entity_type === "no_trade" && (
            <>
              <p className="num-ltr">{item.created_at_tehran}</p>
              <pre className="opp-detail-pre">{item.report_fa || item.preview}</pre>
            </>
          )}
          {isPaper(item) && (
            <>
              <p>
                <DirectionBadge direction={item.direction} /> · {item.status_label_fa}
              </p>
              <p className="num-ltr">ID {item.public_id}</p>
              <p className="num-ltr">
                Entry {fmtNum(item.limit_entry_price ?? item.fill_price)} · SL {fmtNum(item.stop_loss)} · TP{" "}
                {fmtNum(item.take_profit)}
              </p>
            </>
          )}
          {setup && (
            <>
              <p className={`opp-state-pill ${lifecycleTone(setup.lifecycle_state)}`}>{setup.status_label_fa}</p>
              <p className="num-ltr opp-card-meta">
                Zone {fmtNum(setup.entry_zone_low)}–{fmtNum(setup.entry_zone_high)} · Entry {fmtNum(setup.entry_price)} · SL{" "}
                {fmtNum(setup.stop_loss)} · TP {fmtNum(setup.take_profit)} · Net R:R {fmtNum(setup.net_rr)}
              </p>
              <section>
                <h3 className="section-title">تأییدهای ICT</h3>
                <p className="hint">الزامی: {(setup.required_confirmations || []).join(", ") || "—"}</p>
                <p className="hint">تکمیل: {(setup.completed_confirmations || []).join(", ") || "—"}</p>
                <p className="hint">باقی‌مانده: {(setup.missing_confirmations || []).join(", ") || "—"}</p>
              </section>
              {(setup.rejection_label_fa || setup.rejection_reason) && (
                <section>
                  <h3 className="section-title">علت رد / توقف</h3>
                  <p className="toast">{setup.rejection_label_fa || setup.rejection_reason}</p>
                  {setup.order_block_reason && <p className="hint">{setup.order_block_reason}</p>}
                </section>
              )}
              <section>
                <h3 className="section-title">مانیتور</h3>
                <p className="num-ltr">آخرین مانیتور: {setup.last_monitor_at_tehran ?? "—"}</p>
                <p className="num-ltr">سن (دقیقه): {setup.age_minutes ?? "—"}</p>
              </section>
              {(setup.lifecycle_history?.length ?? 0) > 0 && (
                <section>
                  <h3 className="section-title">تاریخچه Lifecycle</h3>
                  <pre className="opp-detail-pre">{JSON.stringify(setup.lifecycle_history, null, 2)}</pre>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
