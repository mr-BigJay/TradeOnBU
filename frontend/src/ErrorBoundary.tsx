import React from "react";

type Props = { children: React.ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("TradeOnBU UI error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="login-wrap" dir="rtl" lang="fa">
          <div className="card" style={{ maxWidth: 520, margin: "8vh auto" }}>
            <h1>خطای نمایش داشبورد</h1>
            <p className="hint">برنامه با خطای غیرمنتظره متوقف شد. داده‌های معاملاتی روی سرور امن هستند (Paper Only).</p>
            <p className="stale" style={{ wordBreak: "break-word" }}>
              {this.state.error.message || "Unknown error"}
            </p>
            <button type="button" style={{ marginTop: 12 }} onClick={() => window.location.reload()}>
              بارگذاری مجدد
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
