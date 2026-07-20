/** Issue one real, Metergraph-wrapped call through each TypeScript provider SDK. */

import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI } from "@google/genai";
import * as metergraph from "metergraph";
import OpenAI from "openai";

const MODELS = {
  openai: "gpt-5.6-luna",
  anthropic: "claude-haiku-4-5-20251001",
  google: "gemini-2.5-flash",
} as const;
const PROMPT = "Reply with exactly OK.";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`missing required environment variable ${name}`);
  return value;
}

function routeName(provider: keyof typeof MODELS): string {
  return [
    "live-bench",
    required("METERGRAPH_BENCH_RUN_ID"),
    required("METERGRAPH_BENCH_TARGET"),
    "typescript",
    provider,
  ].join(":");
}

async function callOpenAI(): Promise<void> {
  const client = metergraph.wrap(
    new OpenAI({ apiKey: required("OPENAI_API_KEY") }),
    "openai",
  );
  await metergraph.route(routeName("openai"), async () => {
    await client.chat.completions.create({
      model: MODELS.openai,
      messages: [{ role: "user", content: PROMPT }],
      max_completion_tokens: 32,
    });
  });
}

async function callAnthropic(): Promise<void> {
  const client = metergraph.wrap(
    new Anthropic({ apiKey: required("ANTHROPIC_API_KEY") }),
    "anthropic",
  );
  await metergraph.route(routeName("anthropic"), async () => {
    await client.messages.create({
      model: MODELS.anthropic,
      max_tokens: 16,
      messages: [{ role: "user", content: PROMPT }],
    });
  });
}

async function callGoogle(): Promise<void> {
  const client = metergraph.wrap(
    new GoogleGenAI({ apiKey: required("GOOGLE_GENAI_API_KEY") }),
    "google",
  );
  await metergraph.route(routeName("google"), async () => {
    await client.models.generateContent({
      model: MODELS.google,
      contents: PROMPT,
      config: {
        maxOutputTokens: 16,
        thinkingConfig: { thinkingBudget: 0 },
      },
    });
  });
}

interface Outcome {
  provider: keyof typeof MODELS;
  model: string;
  ok: boolean;
  error?: string;
}

async function main(): Promise<void> {
  metergraph.init({
    token: required("METERGRAPH_APP_TOKEN"),
    ingestUrl: required("METERGRAPH_INGEST_URL"),
    captureText: false,
    environment: required("METERGRAPH_ENV"),
    appRoot: new URL(".", import.meta.url).pathname,
    transport: "background",
  });

  const outcomes: Outcome[] = [];
  const calls: Array<[keyof typeof MODELS, () => Promise<void>]> = [
    ["openai", callOpenAI],
    ["anthropic", callAnthropic],
    ["google", callGoogle],
  ];
  for (const [provider, call] of calls) {
    try {
      await call();
      outcomes.push({ provider, model: MODELS[provider], ok: true });
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      outcomes.push({ provider, model: MODELS[provider], ok: false, error: message.slice(0, 1000) });
    }
  }

  const flushed = await metergraph.flush(15_000);
  await metergraph.shutdown();
  console.log(JSON.stringify({ language: "typescript", flushed, calls: outcomes }));
  if (!flushed || outcomes.some((item) => !item.ok)) process.exitCode = 1;
}

await main();
