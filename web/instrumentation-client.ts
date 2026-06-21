// Next.js client instrumentation (Phase 2). Runs before hydration. Env-gated on
// NEXT_PUBLIC_SENTRY_DSN (must be public to reach the browser bundle); with no
// DSN, Sentry never initializes and the demo behaves exactly as in Phase 1.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    tracesSampleRate: process.env.NODE_ENV === "development" ? 1.0 : 0.1,
    // Session Replay — a strong showpiece for the demo, cheap to leave gated.
    integrations: [Sentry.replayIntegration()],
    replaysSessionSampleRate: 0.1,
    replaysOnErrorSampleRate: 1.0,
    enableLogs: true,
  });
}

// Instruments App Router navigations for tracing. Safe no-op when uninitialized.
export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
