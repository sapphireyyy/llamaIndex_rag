/// <reference types="vite/client" />

interface Window {
  __ENTERPRISE_RAG_CONFIG__?: import("./runtime-config").PublicRuntimeConfigInput;
}
