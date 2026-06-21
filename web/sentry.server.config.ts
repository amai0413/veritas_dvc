// Sentry server-side init (Phase 2). Env-gated: only initializes when
// SENTRY_DSN is set, so the keyless demo path never sends anything and never
// prints "no DSN" warnings. Imported from instrumentation.ts at server start.
//
// Sentry's server SDK sets up its OWN global OpenTelemetry provider. That does
// not collide with lib/arize.ts, which deliberately uses a non-global provider
// instance — see the note in that file.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    // Sample everything in dev, a slice in prod.
    tracesSampleRate: process.env.NODE_ENV === "development" ? 1.0 : 0.1,
    enableLogs: true,
  });
}
