// Calibration experiment: OLD prompt (opinions dumped near 0) vs NEW prompt
// (opinions scored by how well-supported they are), each graded by the SAME
// LLM-as-judge calibration evaluator. Proves we used evaluator feedback to
// improve the app (Arize criterion #4): the evaluator fails the old prompt on
// opinions and passes the new one.
import Anthropic from "@anthropic-ai/sdk";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
// Load ANTHROPIC_API_KEY from .env.local (standalone node script).
for (const line of fs.readFileSync(ROOT + ".env.local", "utf8").split("\n")) {
  const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^"|"$/g, "");
}

const MODEL = "claude-opus-4-8";
const client = new Anthropic();

const BASE = `You are Veritas, an evidence-analysis engine. Given an input, assess how strongly the available evidence supports it. Return a score from 0 (strongly contradicted) to 100 (strongly supported) and one sentence of reasoning. Be calibrated: obvious facts near 100, obvious falsehoods near 0, genuinely contested claims in the middle.`;

// The ONLY difference between the two prompts is how opinions are handled.
const OLD_PROMPT = `${BASE}
- Do NOT give a middling score to input that is not a verifiable factual claim — e.g. an opinion, a value judgment, a preference, a question, or gibberish. For such input, state it is not a verifiable factual claim and set the score LOW (near 0) because the claim cannot be substantiated.`;

const NEW_PROMPT = `${BASE}
- Subjective / opinion statements ("hackathons are fun", "pineapple belongs on pizza") ARE evaluable: score them by how well the stance is SUPPORTED by evidence (surveys, polls, expert or critical consensus, sentiment). A widely-shared, well-supported view scores high; a fringe one scores low. NEVER dump an opinion to a near-zero score just for being subjective — a near-zero score is reserved for a statement evidence actively contradicts. Set a low "cannot assess" score ONLY for true gibberish / empty input.`;

const VERDICT_SCHEMA = {
  type: "object",
  properties: { score: { type: "integer" }, reasoning: { type: "string" } },
  required: ["score", "reasoning"],
  additionalProperties: false,
};

const EVAL_PROMPT = `You are a strict CALIBRATION judge for a fact-checking system. Given a CLAIM and the system's VERDICT (a 0-100 score + reasoning), decide whether the score is well-calibrated.
Well-calibrated means: true facts score high, false claims score low, genuinely contested claims score middling, and SUBJECTIVE / OPINION statements are scored by how widely the view is supported. CRITICAL: an opinion or subjective statement scored near zero (which wrongly brands it as FALSE) is MIS-calibrated and must FAIL. A near-zero score is only valid when evidence actively contradicts a factual claim.
Return JSON: score = calibration quality 0-100, label = "pass" if well-calibrated else "fail", explanation = one sentence.`;

const EVAL_SCHEMA = {
  type: "object",
  properties: {
    score: { type: "integer" },
    label: { type: "string", enum: ["pass", "fail"] },
    explanation: { type: "string" },
  },
  required: ["score", "label", "explanation"],
  additionalProperties: false,
};

async function callJSON(system, user, schema) {
  const r = await client.messages.create({
    model: MODEL,
    max_tokens: 800,
    output_config: { effort: "low", format: { type: "json_schema", schema } },
    system,
    messages: [{ role: "user", content: user }],
  });
  const tb = r.content.find((b) => b.type === "text");
  return JSON.parse(tb.text);
}

async function verdict(prompt, claim) {
  return callJSON(prompt, `Input to evaluate: "${claim}"`, VERDICT_SCHEMA);
}
async function evaluate(claim, v) {
  return callJSON(
    EVAL_PROMPT,
    `CLAIM: "${claim}"\nVERDICT score: ${v.score}\nVERDICT reasoning: ${v.reasoning}`,
    EVAL_SCHEMA,
  );
}

const idMap = JSON.parse(fs.readFileSync(ROOT + "scripts/exp/id_map.json", "utf8"));

async function runPrompt(label, prompt) {
  const runs = [];
  for (const ex of idMap) {
    const v = await verdict(prompt, ex.input);
    const ev = await evaluate(ex.input, v);
    runs.push({
      example_id: ex.example_id,
      output: JSON.stringify(v),
      input: ex.input,
      category: ex.category,
      verdict_score: v.score,
      calibration_score: ev.score,
      calibration_label: ev.label,
      calibration_explanation: ev.explanation,
    });
    console.log(
      `[${label}] ${ex.category.padEnd(11)} score=${String(v.score).padStart(3)} -> eval ${ev.label} (${ev.score})  | ${ex.input}`,
    );
  }
  const file = `${ROOT}scripts/exp/exp_${label}.jsonl`;
  fs.writeFileSync(file, runs.map((r) => JSON.stringify(r)).join("\n") + "\n");
  const avg = runs.reduce((s, r) => s + r.calibration_score, 0) / runs.length;
  const passes = runs.filter((r) => r.calibration_label === "pass").length;
  console.log(`[${label}] avg calibration=${avg.toFixed(1)}  pass=${passes}/${runs.length}  -> ${file}\n`);
  return { avg, passes, total: runs.length };
}

const oldR = await runPrompt("old", OLD_PROMPT);
const newR = await runPrompt("new", NEW_PROMPT);
console.log("=== SUMMARY ===");
console.log(`OLD prompt: avg calibration ${oldR.avg.toFixed(1)}, ${oldR.passes}/${oldR.total} pass`);
console.log(`NEW prompt: avg calibration ${newR.avg.toFixed(1)}, ${newR.passes}/${newR.total} pass`);
