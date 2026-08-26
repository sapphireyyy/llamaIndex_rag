import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AuthGate from "./AuthGate";
import AuthProvider from "./AuthProvider";
import { createAuthClient, type AuthClient, type AuthFailure } from "./auth";
import { loadPublicRuntimeConfig } from "./runtime-config";
import { setAuthClient } from "./transport";

let authClient: AuthClient | null = null;
let configurationFailure: AuthFailure | undefined;
try {
  authClient = createAuthClient(loadPublicRuntimeConfig());
  setAuthClient(authClient);
} catch {
  configurationFailure = { code: "configuration_error" };
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider client={authClient} configurationFailure={configurationFailure}>
      <AuthGate><App /></AuthGate>
    </AuthProvider>
  </StrictMode>,
);
