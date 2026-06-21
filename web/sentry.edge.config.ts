// Sentry edge-runtime init (Phase 2). Env-gated on SENTRY_DSN, same as the
// server config. Imported from instrumentation.ts when NEXT_RUNTIME === "edge".
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    tracesSampleRate: process.env.NODE_ENV === "development" ? 1.0 : 0.1,
    enableLogs: true,
  });
}
