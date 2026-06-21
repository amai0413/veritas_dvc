// Next.js server instrumentation (Phase 2). `register` runs once at server
// start and lazily imports the runtime-appropriate Sentry config — those configs
// are themselves env-gated, so with no SENTRY_DSN this loads the SDK but inits
// nothing. `onRequestError` forwards captured server errors to Sentry (a no-op
// when Sentry is uninitialized).
import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}

export const onRequestError = Sentry.captureRequestError;
