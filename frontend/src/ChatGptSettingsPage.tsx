import { useCallback, useEffect, useState } from "react";
import { displayTehranTime } from "./displayTime";
import { ApiError, apiFetch } from "./api";
import { ConnectParamField } from "./ConnectParamField";

const DRAFT_KEY = "tradeonbu_siwc_connect_draft";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  ai_provider?: "siwc" | "openai_api";
  api_key_configured?: boolean;
  email?: string;
  scheduling_active: boolean;
  next_run_at?: string;
  next_run_at_tehran?: string;
  interval_minutes: number;
  ai_analysis_model?: string;
  ai_reasoning_effort?: string;
  ai_model_options?: Array<{ model: string; label: string; reasoning_effort: string; description: string }>;
  preferred_model?: string | null;
  unattended_consent?: boolean;
  hourly_enabled?: boolean;
};

type ConnectDraft = {
  connect_token: string;
  connect_secret: string;
  oauth_state: string;
  authorize_url: string;
  oauth_code?: string;
  callback_client_id?: string;
  callback_paste?: string;
};

const emptyDraft = (): ConnectDraft => ({
  connect_token: "",
  connect_secret: "",
  oauth_state: "",
  authorize_url: "",
  oauth_code: "",
  callback_client_id: "",
  callback_paste: "",
});

function loadDraft(): ConnectDraft {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return emptyDraft();
    const parsed = JSON.parse(raw) as Partial<ConnectDraft>;
    return { ...emptyDraft(), ...parsed };
  } catch {
    return emptyDraft();
  }
}

function saveDraftToStorage(draft: ConnectDraft) {
  localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
}

function parseOAuthCallback(raw: string): { code?: string; state?: string; clientId?: string } {
  const trimmed = raw.trim();
  if (!trimmed) return {};
  try {
    const url = trimmed.includes("://") ? new URL(trimmed) : new URL(`http://local${trimmed.startsWith("/") ? trimmed : `/${trimmed}`}`);
    const qs = url.searchParams;
    return {
      code: qs.get("code") || undefined,
      state: qs.get("state") || undefined,
      clientId: qs.get("client_id") || undefined,
    };
  } catch {
    return {};
  }
}

export function ChatGptSettingsPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [msg, setMsg] = useState("");
  const [msgError, setMsgError] = useState(false);
  const [draft, setDraft] = useState<ConnectDraft>(() => loadDraft());
  const [consent, setConsent] = useState(false);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [intervalMin, setIntervalMin] = useState(60);
  const [selectedModel, setSelectedModel] = useState("gpt-6.1-sol");
  const [availableModels, setAvailableModels] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [providerChoice, setProviderChoice] = useState<"siwc" | "openai_api">("siwc");
  const [newApiKey, setNewApiKey] = useState("");

  const patchDraft = useCallback((patch: Partial<ConnectDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
  }, []);

  const refresh = useCallback(async () => {
    const s = await apiFetch("/api/gui/chatgpt/status");
    setStatus(s);
    setProviderChoice(s.ai_provider || "siwc");
    setSelectedModel(s.ai_analysis_model || "gpt-6.1-sol");
    setConsent(!!s.unattended_consent);
    setScheduleEnabled(!!s.hourly_enabled);
    setIntervalMin(s.interval_minutes || 60);
    if (s.ai_provider === "openai_api") {
      setAvailableModels(["gpt-6-luna"]);
    } else if (s.connected && s.plan_usage_authorized) {
      try {
        const modelData = await apiFetch("/api/gui/chatgpt/models");
        const ids = (modelData.models || [])
          .map((model: { slug?: string }) => model.slug)
          .filter((slug: unknown): slug is string => typeof slug === "string");
        setAvailableModels(ids);
      } catch {
        setAvailableModels(null);
      }
    } else {
      setAvailableModels(null);
    }
  }, []);

  useEffect(() => {
    refresh().catch((e) => {
      setMsgError(true);
      setMsg(String(e));
    });
  }, [refresh]);

  function toast(text: string, isError = false) {
    setMsg(text);
    setMsgError(isError);
  }

  async function autoGenerate() {
    setBusy("generate");
    try {
      const data = await apiFetch("/api/gui/chatgpt/connect/begin", { method: "POST" });
      const next: ConnectDraft = {
        ...draft,
        connect_token: data.connect_token,
        connect_secret: data.connect_secret,
        oauth_state: data.oauth_state,
        authorize_url: data.authorize_url,
      };
      setDraft(next);
      saveDraftToStorage(next);
      toast(`مقادیر از سرور ساخته شد (اعتبار ~${Math.round((data.expires_in || 900) / 60)} دقیقه).`);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  function saveDraftLocal() {
    saveDraftToStorage(draft);
    toast("پیش‌نویس اتصال در مرورگر ذخیره شد.");
  }

  function applyCallbackPaste() {
    const parsed = parseOAuthCallback(draft.callback_paste || "");
    if (!parsed.code && !parsed.state && !parsed.clientId) {
      toast("آدرس callback معتبر نیست.", true);
      return;
    }
    patchDraft({
      oauth_code: parsed.code || draft.oauth_code,
      oauth_state: parsed.state || draft.oauth_state,
      callback_client_id: parsed.clientId || draft.callback_client_id,
    });
    toast("کد و state از آدرس callback استخراج شد.");
  }

  async function completeConnect() {
    if (!draft.connect_token || !draft.connect_secret || !draft.oauth_state || !draft.oauth_code) {
      toast("TOKEN، SECRET، STATE و کد OAuth را پر کنید (یا تولید خودکار بزنید).", true);
      return;
    }
    setBusy("complete");
    try {
      const data = await apiFetch("/api/gui/chatgpt/connect/complete", {
        method: "POST",
        body: JSON.stringify({
          connect_token: draft.connect_token.trim(),
          connect_secret: draft.connect_secret.trim(),
          oauth_state: draft.oauth_state.trim(),
          code: draft.oauth_code.trim(),
          callback_client_id: draft.callback_client_id?.trim() || null,
        }),
      });
      toast(`اتصال برقرار شد${data.email ? `: ${data.email}` : ""}.`);
      await refresh();
    } catch (e) {
      const detail = e instanceof ApiError ? e.message : String(e);
      toast(detail, true);
    } finally {
      setBusy(null);
    }
  }

  async function testConnection() {
    setBusy("test");
    try {
      if (status?.ai_provider === "openai_api") {
        const data = await apiFetch("/api/gui/chatgpt/api/test", { method: "POST" });
        toast(`اتصال API معتبر است: ${data.model} / ${data.reasoning_effort}`);
      } else {
        const data = await apiFetch("/api/gui/chatgpt/connect/test", { method: "POST" });
        const names = (data.sample_models || []).map((m: { id?: string }) => m.id || m).join(", ");
        toast(`تست موفق — ${data.model_count} مدل${names ? `: ${names}` : ""}.`);
      }
      await refresh();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  async function saveModel() {
    if (!connected) {
      toast("ابتدا اتصال SIWC را برقرار کنید.", true);
      return;
    }
    setBusy("model");
    try {
      const data = await apiFetch("/api/gui/chatgpt/model", {
        method: "PUT",
        body: JSON.stringify({ model: selectedModel }),
      });
      toast(`مدل ذخیره شد: ${data.model} · reasoning: ${String(data.reasoning_effort).toUpperCase()}`);
      await refresh();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  async function saveApiKey() {
    if (!newApiKey.trim()) return;
    setBusy("api-key");
    try {
      await apiFetch("/api/gui/chatgpt/api/key", {
        method: "PUT",
        body: JSON.stringify({ api_key: newApiKey.trim() }),
      });
      setNewApiKey("");
      toast("کلید API به‌صورت رمزنگاری‌شده روی سرور ذخیره شد.");
      await refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  async function saveProvider() {
    setBusy("provider");
    try {
      await apiFetch("/api/gui/chatgpt/provider", {
        method: "PUT",
        body: JSON.stringify({ provider: providerChoice }),
      });
      toast("منبع تحلیل تغییر کرد.");
      await refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  async function removeApiKey() {
    if (!window.confirm("کلید API ذخیره‌شده حذف شود؟")) return;
    setBusy("api-key");
    try {
      await apiFetch("/api/gui/chatgpt/api/key", { method: "DELETE" });
      setNewApiKey("");
      toast("کلید API ذخیره‌شده حذف شد.");
      await refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(null);
    }
  }

  async function saveSchedule() {
    if (scheduleEnabled && !consent) {
      toast("رضایت تحلیل خودکار را فعال کنید.", true);
      return;
    }
    await apiFetch("/api/gui/chatgpt/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: scheduleEnabled,
        unattended_consent: consent,
        interval_minutes: intervalMin,
      }),
    });
    toast("تنظیمات زمان‌بندی ذخیره شد.");
    await refresh();
  }

  const connected = status?.connected && status?.plan_usage_authorized;
  const modelOptions = status?.ai_model_options || [
    {
      model: "gpt-6.1-sol",
      label: "GPT-6.1 Sol · Max",
      reasoning_effort: "max",
      description: "مدل فعلی با سطح استدلال حداکثری.",
    },
    {
      model: "gpt-6-luna",
      label: "GPT-6 Luna · High",
      reasoning_effort: "high",
      description: "مدل اقتصادی‌تر در API رسمی؛ دسترسی و سهمیه SIWC به حساب شما بستگی دارد.",
    },
  ];

  return (
    <div className="page">
      <header className="page-header">
        <h1>تنظیمات تحلیل هوش مصنوعی</h1>
        <p className="hint">{status?.ai_provider === "openai_api" ? "OpenAI API · مستقل از سهمیه ChatGPT" : "اتصال SIWC · اشتراک ChatGPT"}</p>
      </header>

      {msg && <p className={`toast ${msgError ? "toast-error" : "toast-ok"}`}>{msg}</p>}

      <div className="card status-card">
        <span className={`status-pill ${connected ? "on" : "off"}`}>{connected ? "پیکربندی شده" : "قطع"}</span>
        {status?.ai_provider === "openai_api" ? (
          <p className="hint">کلید API فقط در .env سرور قرار دارد و اینجا نمایش داده نمی‌شود. مدل: GPT-6 Luna / High.</p>
        ) : status?.email ? <p className="status-email">{status.email}</p> : null}
        <div className="stack-btns">
          {status?.ai_provider !== "openai_api" && <button
            type="button"
            className="secondary btn-block"
            onClick={() => apiFetch("/api/gui/chatgpt/disconnect", { method: "POST" }).then(refresh)}
          >
            قطع اتصال
          </button>}
          <button
            type="button"
            className="btn-primary btn-block"
            onClick={testConnection}
            disabled={!!busy || !status?.connected}
          >
            {busy === "test" ? "در حال تست…" : status?.ai_provider === "openai_api" ? "تست کلید API و Luna" : "تست اتصال"}
          </button>
        </div>
      </div>

      <div className="card form-stack">
        <h3 className="section-title">مدیریت اتصال هوش مصنوعی</h3>
        <label>
          منبع تحلیل
          <select value={providerChoice} onChange={(e) => setProviderChoice(e.target.value as "siwc" | "openai_api")} disabled={!!busy}>
            <option value="siwc">ChatGPT SIWC</option>
            <option value="openai_api">OpenAI API · GPT-6 Luna / High</option>
          </select>
        </label>
        <button type="button" className="secondary btn-block" onClick={saveProvider} disabled={!!busy || providerChoice === status?.ai_provider}>
          ذخیره منبع تحلیل
        </button>
        <label>
          API Key جدید (فقط هنگام ثبت)
          <input type="password" value={newApiKey} autoComplete="new-password"
            onChange={(e) => setNewApiKey(e.target.value)} placeholder="sk-proj-…" />
        </label>
        <p className="hint">کلید فقط برای ارسال رمزنگاری‌شده به سرور استفاده می‌شود و در مرورگر ذخیره نمی‌شود. وضعیت: {status?.api_key_configured ? "ثبت‌شده" : "ثبت‌نشده"}</p>
        <div className="row-btns">
          <button type="button" className="btn-primary" onClick={saveApiKey} disabled={!!busy || !newApiKey.trim()}>
            {busy === "api-key" ? "در حال ذخیره…" : "ذخیره API Key"}
          </button>
          <button type="button" className="secondary" onClick={removeApiKey} disabled={!!busy || !status?.api_key_configured}>
            حذف کلید
          </button>
        </div>
      </div>

      {status?.ai_provider === "openai_api" ? (
        <div className="card connect-panel">
          <h3 className="section-title">اتصال API فعال</h3>
          <p className="hint">کلید و منبع تحلیل را می‌توانی از فرم بالای همین صفحه ذخیره کنی؛ نیازی به ویرایش .env نیست.</p>
        </div>
      ) : <div className="card connect-panel">
        <h3 className="section-title">پارامترهای OAuth</h3>
        <p className="hint">
          «تولید خودکار» یک جلسه ۱۵ دقیقه‌ای روی سرور می‌سازد. می‌توانید مقادیر را دستی ویرایش یا از helper کپی کنید؛
          SECRET باید با همان جلسهٔ سرور یکی باشد.
        </p>

        <ConnectParamField
          label="CONNECT_TOKEN"
          value={draft.connect_token}
          onChange={(v) => patchDraft({ connect_token: v })}
        />
        <ConnectParamField
          label="CONNECT_SECRET"
          value={draft.connect_secret}
          onChange={(v) => patchDraft({ connect_secret: v })}
        />
        <ConnectParamField
          label="OAUTH_STATE"
          value={draft.oauth_state}
          onChange={(v) => patchDraft({ oauth_state: v })}
        />
        <ConnectParamField
          label="AUTHORIZE_URL"
          value={draft.authorize_url}
          onChange={(v) => patchDraft({ authorize_url: v })}
          multiline
        />

        <div className="row-btns">
          <button type="button" className="btn-primary" onClick={autoGenerate} disabled={!!busy}>
            {busy === "generate" ? "…" : "تولید خودکار"}
          </button>
          <button type="button" className="secondary" onClick={saveDraftLocal}>
            ذخیره پیش‌نویس
          </button>
          <button
            type="button"
            className="secondary"
            disabled={!draft.authorize_url}
            onClick={() => window.open(draft.authorize_url, "_blank", "noopener,noreferrer")}
          >
            باز کردن لینک
          </button>
        </div>

        <h3 className="section-title section-title-spaced">تکمیل از داشبورد</h3>
        <ConnectParamField
          label="OAuth code"
          value={draft.oauth_code || ""}
          onChange={(v) => patchDraft({ oauth_code: v })}
          placeholder="کد authorization پس از ورود"
        />
        <ConnectParamField
          label="callback client_id (اختیاری)"
          value={draft.callback_client_id || ""}
          onChange={(v) => patchDraft({ callback_client_id: v })}
        />
        <ConnectParamField
          label="یا چسباندن URL callback"
          value={draft.callback_paste || ""}
          onChange={(v) => patchDraft({ callback_paste: v })}
          multiline
          placeholder="http://127.0.0.1:1455/auth/callback?code=…&state=…"
        />
        <button type="button" className="secondary btn-block" onClick={applyCallbackPaste}>
          استخراج از URL
        </button>

        <button
          type="button"
          className="btn-primary btn-block"
          onClick={completeConnect}
          disabled={!!busy}
        >
          {busy === "complete" ? "در حال اتصال…" : "تکمیل اتصال"}
        </button>
      </div>}

      <form
        className="card form-stack"
        onSubmit={(e) => {
          e.preventDefault();
          saveSchedule();
        }}
      >
        <h3 className="section-title">تحلیل خودکار و مدل</h3>
        <p className="hint">تنظیم مدل مستقل از زمان‌بندی ذخیره می‌شود. تغییر مدل، تحلیل خودکار را اجرا نمی‌کند.</p>
        <label>
          مدل تحلیل هوش مصنوعی
          {status?.ai_provider === "openai_api" && <p className="hint">در حالت API مدل روی Luna / High ثابت است و هزینه بر اساس مصرف محاسبه می‌شود.</p>}
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            disabled={!connected || !!busy || status?.ai_provider === "openai_api"}
          >
            {modelOptions.map((option) => {
              const isCurrent = option.model === status?.ai_analysis_model;
              const available = isCurrent || (availableModels !== null && availableModels.includes(option.model));
              return (
                <option key={option.model} value={option.model} disabled={!available}>
                  {option.label}{available ? "" : " · در SIWC تأیید نشده"}
                </option>
              );
            })}
          </select>
        </label>
        <p className="hint">
          {modelOptions.find((option) => option.model === selectedModel)?.description}
        </p>
        {connected && availableModels === null && (
          <p className="hint">فهرست مدل‌های مجاز دریافت نشد؛ برای بررسی اتصال، «تست اتصال» را بزنید.</p>
        )}
        {connected && availableModels !== null && !availableModels.includes("gpt-6-luna") && selectedModel !== "gpt-6-luna" && (
          <p className="hint">
            مدل GPT-6 Luna در فهرست فعلی اتصال SIWC نیست؛ از طریق این اتصال هنوز قابل انتخاب نیست.
            قیمت API رسمی تضمین‌کننده دسترسی با سهمیه ChatGPT نیست.
          </p>
        )}
        <button
          type="button"
          className="secondary btn-block"
          onClick={saveModel}
          disabled={!connected || !!busy || status?.ai_provider === "openai_api" || selectedModel === status?.ai_analysis_model || (availableModels !== null && !availableModels.includes(selectedModel))}
        >
          {busy === "model" ? "در حال ذخیره مدل…" : "ذخیره مدل تحلیل"}
        </button>
        <label className="toggle-row">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>اجازه تحلیل خودکار در پس‌زمینه (در حالت API دارای هزینه)</span>
        </label>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={(e) => setScheduleEnabled(e.target.checked)}
            disabled={!connected}
          />
          <span>فعال‌سازی زمان‌بندی</span>
        </label>
        <label>
          هر چند دقیقه؟ (۶۰–۳۶۰ · پیش‌فرض ۶۰)
          <input
            type="number"
            min={60}
            max={360}
            inputMode="numeric"
            value={intervalMin}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
            disabled={!connected}
          />
        </label>
        {status && (
          <p className="hint">
            {status.scheduling_active ? "زمان‌بندی فعال" : "زمان‌بندی غیرفعال"}
            {status.next_run_at &&
              ` · بعدی: ${displayTehranTime(status.next_run_at_tehran, status.next_run_at)}`}
          </p>
        )}
        <button type="submit" className="btn-primary btn-block" disabled={!connected}>
          ذخیره زمان‌بندی
        </button>
      </form>
    </div>
  );
}
