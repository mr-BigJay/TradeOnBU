import { useState } from "react";

type Props = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  multiline?: boolean;
  placeholder?: string;
  autoComplete?: string;
};

export function ConnectParamField({
  label,
  value,
  onChange,
  multiline,
  placeholder,
  autoComplete = "off",
}: Props) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="copy-field connect-param">
      <div className="copy-field-head">
        <span className="copy-label">{label}</span>
        <button type="button" className="copy-btn" onClick={copy} disabled={!value}>
          {copied ? "کپی شد" : "کپی"}
        </button>
      </div>
      {multiline ? (
        <textarea
          className="connect-input connect-textarea"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoComplete={autoComplete}
          rows={3}
        />
      ) : (
        <input
          className="connect-input"
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoComplete={autoComplete}
        />
      )}
    </div>
  );
}
