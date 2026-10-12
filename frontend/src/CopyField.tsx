import { useState } from "react";

export function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="copy-field">
      <div className="copy-field-head">
        <span className="copy-label">{label}</span>
        <button type="button" className="copy-btn" onClick={copy}>
          {copied ? "کپی شد" : "کپی"}
        </button>
      </div>
      <p className="copy-value">{value}</p>
    </div>
  );
}
