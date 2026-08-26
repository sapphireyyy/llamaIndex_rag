import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AuthGate from "../AuthGate";
import { AuthContext, type AuthContextValue } from "../auth-context";
import type { AuthSnapshot } from "../auth";

function renderGate(snapshot: AuthSnapshot, login = vi.fn()) {
  const value: AuthContextValue = {
    client: null,
    snapshot,
    login,
    logout: vi.fn(),
    retry: vi.fn(),
  };
  return render(<AuthContext.Provider value={value}><AuthGate><div>受保护业务内容</div></AuthGate></AuthContext.Provider>);
}

const snapshot = (values: Partial<AuthSnapshot>): AuthSnapshot => ({
  status: "unauthenticated",
  mode: "oidc",
  subject: null,
  displayName: null,
  sessionVersion: 1,
  failure: null,
  ...values,
});

describe("React authentication gate", () => {
  it("does not render protected content before authentication and exposes an accessible login", () => {
    const login = vi.fn();
    renderGate(snapshot({}), login);
    expect(screen.queryByText("受保护业务内容")).not.toBeInTheDocument();
    const loginButton = screen.getByRole("button", { name: "使用 Keycloak 登录" });
    expect(loginButton).toHaveFocus();
    fireEvent.click(loginButton);
    expect(login).toHaveBeenCalledTimes(1);
  });

  it("keeps initializing and error states mutually exclusive", () => {
    const view = renderGate(snapshot({ status: "initializing" }));
    expect(screen.getByText("正在恢复登录会话")).toBeVisible();
    expect(screen.queryByText("受保护业务内容")).not.toBeInTheDocument();
    view.rerender(<AuthContext.Provider value={{
      client: null,
      snapshot: snapshot({ status: "recoverable_error", failure: { code: "session_expired" } }),
      login: vi.fn(), logout: vi.fn(), retry: vi.fn(),
    }}><AuthGate><div>受保护业务内容</div></AuthGate></AuthContext.Provider>);
    expect(screen.getByRole("alert")).toHaveTextContent("会话已过期");
    expect(screen.getByRole("button", { name: "重新登录" })).toHaveFocus();
  });

  it("renders protected content only for an authenticated subject", () => {
    renderGate(snapshot({ status: "authenticated", subject: "subject-a", displayName: "甲用户" }));
    expect(screen.getByText("受保护业务内容")).toBeVisible();
  });
});
