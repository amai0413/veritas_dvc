import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

const nextConfig: NextConfig = {
  // jsdom + playwright-core (used by lib/fetchArticle) do dynamic requires — keep
  // them external to the server bundle rather than letting the bundler trace them.
  serverExternalPackages: ["jsdom", "playwright-core"],
};

// Env-gated build integration: only wrap with Sentry when a DSN is configured.
// With no DSN the export is the plain config, so the demo build is unchanged.
// Source-map upload only runs when SENTRY_AUTH_TOKEN is also present; without it
// the wrapper just injects the SDK and skips upload (no build failure offline).
const sentryEnabled =
  !!process.env.SENTRY_DSN || !!process.env.NEXT_PUBLIC_SENTRY_DSN;

export default sentryEnabled
  ? withSentryConfig(nextConfig, {
      org: process.env.SENTRY_ORG,
      project: process.env.SENTRY_PROJECT,
      authToken: process.env.SENTRY_AUTH_TOKEN,
      silent: true,
    })
  : nextConfig;
