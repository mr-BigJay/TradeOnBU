import { BehDarkPage } from "./BehDarkPage";
import { BitunixAccountPage } from "./BitunixAccountPage";
import { DailyOutlookPage } from "./DailyOutlookPage";
import { useEffect, useState } from "react";
import { fetchMe, getApiKey, login, logout, setApiKey, setCsrf } from "./api";
import { AiAnalysisPage } from "./AiAnalysisPage";
import { ChatGptSettingsPage } from "./ChatGptSettingsPage";
import { TelegramSettingsPage } from "./TelegramSettingsPage";
import { OpportunitiesPage } from "./OpportunitiesPage";
import { PositionPage, type PositionPrefill } from "./PositionPage";

type Page = "bitunix" | "behdark" | "outlook" | "analysis" | "opportunities" | "chatgpt" | "telegram" | "position";

const NAV: { id: Page; label: string; short: string }[] = [
  { id: "outlook", label: "چشم‌انداز روزانه", short: "🌅" },
  { id: "analysis", label: "تحلیل", short: "📊" },
  { id: "opportunities", label: "فرصت‌ها", short: "🎯" },
  { id: "position", label: "پوزیشن", short: "📈" },
  { id: "bitunix", label: "حساب Bitunix", short: "💼" },
  { id: "chatgpt", label: "ChatGPT", short: "🤖" },
  { id: "telegram", label: "تلگرام", short: "✈️" },
  { id: "behdark", label: "BehDark", short: "BD" },
];

export default function App() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginErr, setLoginErr] = useState("");
  const [authBoot, setAuthBoot] = useState<"loading" | "done">("loading");
  const [authed, setAuthed] = useState(false);
  const [sessionUser, setSessionUser] = useState<string | null>(null);
  const readPage = (): Page => NAV.find(n => n.id === window.location.hash.split("/")[1])?.id || "analysis";
  const [page, setPage] = useState<Page>(readPage);
  const navigate = (next: Page) => { window.location.hash = "/" + next; setPage(next); };
  useEffect(() => {
    const update = () => setPage(readPage());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  const [positionPrefill, setPositionPrefill] = useState<PositionPrefill | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState(getApiKey());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const m = await fetchMe();
      if (cancelled) return;
      if (m.authenticated) {
        setAuthed(true);
        setSessionUser(m.username ?? null);
        if (m.csrf_token) setCsrf(m.csrf_token);
      } else if (getApiKey()) {
        setAuthed(true);
      }
      setAuthBoot("done");
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (authBoot === "loading") {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <div className="logo-mark">BU</div>
          <h1>TradeOnBU</h1>
          <p className="hint">در حال بارگذاری…</p>
        </div>
      </div>
    );
  }

  if (!authed) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <div className="logo-mark">BU</div>
          <h1>TradeOnBU</h1>
          <p className="hint">Paper only · ICT · ChatGPT Plus</p>
          <label>نام کاربری</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" inputMode="text" />
          <label>رمز عبور</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
          {loginErr && <p className="toast toast-error">{loginErr}</p>}
          <button
            type="button"
            className="btn-primary btn-block"
            onClick={() =>
              login(username, password)
                .then((d) => {
                  setAuthed(true);
                  setSessionUser(d.username);
                })
                .catch((e) => setLoginErr(e.message))
            }
          >
            ورود
          </button>
          <details className="dev-key">
            <summary>API Key (dev)</summary>
            <input value={apiKeyInput} onChange={(e) => setApiKeyInput(e.target.value)} />
            <button
              type="button"
              className="secondary btn-block"
              onClick={() => {
                setApiKey(apiKeyInput.trim());
                setAuthed(true);
              }}
            >
              ورود
            </button>
          </details>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-row">
          <div className="brand-block">
            <span className="logo-mark sm">BU</span>
            <span className="brand-name">TradeOnBU</span>
          </div>
          <button type="button" className="ghost-btn" onClick={() => logout().finally(() => setAuthed(false))} aria-label="خروج">
            خروج
          </button>
        </div>
        {sessionUser && <p className="header-user">{sessionUser}</p>}
        <nav className="desktop-tabs" aria-label="منوی اصلی">
          {NAV.map((n) => (
            <button key={n.id} type="button" className={page === n.id ? "active" : ""} onClick={() => navigate(n.id)}>
              {n.label}
            </button>
          ))}
        </nav>
      </header>

      <main className={page === "position" || page === "bitunix" ? "app-main app-main--positions" : "app-main"}>
        {page === "behdark" && <BehDarkPage />}
        {page === "bitunix" && <BitunixAccountPage />}
        {page === "outlook" && <DailyOutlookPage />}
        {page === "chatgpt" && <ChatGptSettingsPage />}
        {page === "telegram" && <TelegramSettingsPage />}
        {page === "analysis" && (
          <AiAnalysisPage
            onOpenPaperPosition={(p) => {
              setPositionPrefill(p);
              navigate("position");
            }}
          />
        )}
        {page === "opportunities" && <OpportunitiesPage />}
        {page === "position" && (
          <PositionPage
            prefill={positionPrefill}
            onConsumePrefill={() => setPositionPrefill(null)}
            initialTab={positionPrefill ? "open" : undefined}
          />
        )}
      </main>

      <nav className="bottom-nav" aria-label="منوی پایین">
        {NAV.map((n) => (
          <button
            key={n.id}
            type="button"
            className={`bottom-nav-item ${page === n.id ? "active" : ""}`}
            onClick={() => navigate(n.id)}
          >
            <span className="bottom-nav-icon" aria-hidden>
              {n.short}
            </span>
            <span className="bottom-nav-label">{n.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

