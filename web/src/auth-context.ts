import { createContext, useContext } from "react";
import type { AuthClient, AuthSnapshot } from "./auth";

export type AuthContextValue = Readonly<{
  client: AuthClient | null;
  snapshot: AuthSnapshot;
  login(): Promise<void>;
  logout(): Promise<void>;
  retry(): void;
}>;

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
