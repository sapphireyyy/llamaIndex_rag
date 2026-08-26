import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { AuthClient, AuthFailure, AuthSnapshot } from "./auth";
import { AuthContext } from "./auth-context";
import { resetTenantBoundary } from "./transport";

const configurationSnapshot = (failure: AuthFailure): AuthSnapshot => Object.freeze({
  status: "recoverable_error",
  mode: "oidc",
  subject: null,
  displayName: null,
  sessionVersion: 0,
  failure,
});

export default function AuthProvider({
  client,
  configurationFailure,
  children,
}: {
  client: AuthClient | null;
  configurationFailure?: AuthFailure;
  children: ReactNode;
}) {
  const [snapshot, setSnapshot] = useState<AuthSnapshot>(() => client?.getSnapshot()
    ?? configurationSnapshot(configurationFailure ?? { code: "configuration_error" }));
  const previousSubject = useRef<string | null>(null);

  useEffect(() => {
    if (!client) return;
    const unsubscribe = client.subscribe(setSnapshot);
    void client.initialize().then(setSnapshot);
    return unsubscribe;
  }, [client]);

  useEffect(() => {
    const previous = previousSubject.current;
    if (previous && (snapshot.status !== "authenticated" || snapshot.subject !== previous)) {
      void resetTenantBoundary(previous);
    }
    previousSubject.current = snapshot.status === "authenticated" ? snapshot.subject : null;
  }, [snapshot.status, snapshot.subject]);

  const value = useMemo(() => ({
    client,
    snapshot,
    login: async () => { await client?.login(); },
    logout: async () => { await client?.logout(); },
    retry: () => window.location.reload(),
  }), [client, snapshot]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
