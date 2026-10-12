import { useEffect, useState } from "react";
import { fetchMe, getApiKey, login, logout, setApiKey, setCsrf } from "./api";
import { AiAnalysisPage } from "./AiAnalysisPage";
import { ChatGptSettingsPage } from "./ChatGptSettingsPage";
import { TelegramSettingsPage } from "./TelegramSettingsPage";

type Page = "analysis" | "chatgpt" | "telegram";

export default function App() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginErr, setLoginErr] = useState("");
  const [authBoot, setAuthBoot] = useState<"loading" | "done">("loading");
  const [authed, setAuthed] = useState(false);
  const [sessionUser, setSessionUser] = useState<string | null>(null);
  const [page, setPage] = useState<Page>("analysis");
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
          <h1>TradeOnBU</h1>
          <p className="hint">…</p>
        </div>
      </div>
    );
  }

  if (!authed) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <h1>TradeOnBU</h1>
          <p className="hint">Paper only — ICT + ChatGPT Plus</p>
          <label>نام کاربری</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          <label>رمز عبور</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
          {loginErr && <p className="stale">{loginErr}</p>}
          <button
            type="button"
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
            <button type="button" className="secondary" onClick={() => { setApiKey(apiKeyInput.trim()); setAuthed(true); }}>
              ورود
            </button>
          </details>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">TradeOnBU</div>
        <nav className="main-nav">
          <button type="button" className={page === "chatgpt" ? "active" : ""} onClick={() => setPage("chatgpt")}>
            ChatGPT
          </button>
          <button type="button" className={page === "telegram" ? "active" : ""} onClick={() => setPage("telegram")}>
            تلگرام
          </button>
          <button type="button" className={page === "analysis" ? "active" : ""} onClick={() => setPage("analysis")}>
            تحلیل
          </button>
        </nav>
        <div className="topbar-end">
          {sessionUser && <span className="hint">{sessionUser}</span>}
          <button type="button" className="secondary btn-sm" onClick={() => logout().finally(() => setAuthed(false))}>
            خروج
          </button>
        </div>
      </header>
      <main className="app-main">
        {page === "chatgpt" && <ChatGptSettingsPage />}
        {page === "telegram" && <TelegramSettingsPage />}
        {page === "analysis" && <AiAnalysisPage />}
      </main>
    </div>
  );
}
