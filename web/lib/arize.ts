// Arize AX tracing (Phase 2). Env-gated: only active when ARIZE_API_KEY and
// ARIZE_SPACE_ID are set. We build our OWN tracer provider and DO NOT register
// it globally (no `provider.register()`), so it can never collide with Sentry's
// OpenTelemetry setup. OTel/gRPC modules are dynamically imported so the no-key
// path never loads them.
//
// OpenInference semantic attribute keys are the documented Arize ingestion
// contract:
// https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md

import type { Tracer } from "@opentelemetry/api";

// OpenInference span attribute keys (verbatim from the spec).
export const OI = {
  SPAN_KIND: "openinference.span.kind",
  INPUT_VALUE: "input.value",
  INPUT_MIME: "input.mime_type",
  OUTPUT_VALUE: "output.value",
  OUTPUT_MIME: "output.mime_type",
  LLM_MODEL: "llm.model_name",
  LLM_PROVIDER: "llm.provider",
  LLM_SYSTEM: "llm.system",
  TOK_PROMPT: "llm.token_count.prompt",
  TOK_COMPLETION: "llm.token_count.completion",
  TOK_TOTAL: "llm.token_count.total",
} as const;

type ArizeHandle = { tracer: Tracer; flush: () => Promise<void> };

let cached: ArizeHandle | null | undefined;

export async function getArize(): Promise<ArizeHandle | null> {
  if (cached !== undefined) return cached;

  const apiKey = process.env.ARIZE_API_KEY;
  const spaceId = process.env.ARIZE_SPACE_ID;
  if (!apiKey || !spaceId) {
    cached = null;
    return cached;
  }

  try {
    const { NodeTracerProvider } = await import("@opentelemetry/sdk-trace-node");
    const { BatchSpanProcessor } = await import("@opentelemetry/sdk-trace-base");
    const { resourceFromAttributes } = await import("@opentelemetry/resources");
    const { OTLPTraceExporter } = await import("@opentelemetry/exporter-trace-otlp-grpc");
    const { Metadata } = await import("@grpc/grpc-js");

    const metadata = new Metadata();
    metadata.set("arize-space-id", spaceId);
    metadata.set("arize-api-key", apiKey);

    const provider = new NodeTracerProvider({
      resource: resourceFromAttributes({
        model_id: process.env.ARIZE_PROJECT_NAME ?? "veritas",
        model_version: "phase-2",
      }),
      spanProcessors: [
        new BatchSpanProcessor(
          new OTLPTraceExporter({ url: "https://otlp.arize.com/v1", metadata }),
        ),
      ],
    });

    // Note: intentionally NOT calling provider.register() — we use this
    // provider's own tracer directly to avoid any global-provider conflict.
    cached = {
      tracer: provider.getTracer("veritas"),
      flush: () => provider.forceFlush(),
    };
  } catch {
    cached = null; // tracing must never break the request path
  }

  return cached;
}
