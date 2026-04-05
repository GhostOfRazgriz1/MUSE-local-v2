import React from "react";
import en from "../i18n/en";
import zh from "../i18n/zh";
import { detectLocale } from "../i18n";

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends React.Component<
  React.PropsWithChildren<{}>,
  State
> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      const strings = detectLocale() === "zh" ? zh : en;
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            padding: 32,
            color: "var(--text-secondary, #999)",
            textAlign: "center",
            gap: 12,
          }}
        >
          <h2 style={{ color: "var(--text-primary, #eee)", margin: 0 }}>
            {strings.error_title}
          </h2>
          <p style={{ margin: 0, maxWidth: 400 }}>
            {this.state.error?.message || strings.error_message}
          </p>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{
              marginTop: 8,
              padding: "8px 20px",
              background: "var(--accent, #6366f1)",
              color: "#fff",
              border: "none",
              borderRadius: 6,
              cursor: "pointer",
              fontSize: 14,
            }}
          >
            {strings.error_try_again}
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
