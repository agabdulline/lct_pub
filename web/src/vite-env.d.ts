/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `1` — the app works on the synthetic mock world instead of the API (docs/architecture/frontend.md §5). */
  readonly VITE_API_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
