import { anthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { openai } from "@ai-sdk/openai";
import {
  defaultSettingsMiddleware,
  gateway,
  generateText,
  wrapLanguageModel,
} from "ai";
import * as mg from "metergraph";
import { parseModelSelection } from "./selection.mjs";

const compatible = createOpenAICompatible({
  name: "compatible",
  apiKey: process.env.COMPATIBLE_API_KEY ?? "local-development-key",
  baseURL: process.env.COMPATIBLE_BASE_URL ?? "http://localhost:11434/v1",
});
const metergraphMiddleware = mg.vercelAISDKMiddleware({
  repository: "owner/repository",
  environment: "example",
});
const applicationMiddleware = defaultSettingsMiddleware({
  settings: { temperature: 0.2 },
});

// This represents an existing application factory. It intentionally returns
// the AI SDK's broad LanguageModel union, including a Gateway string model.
export function createBaseModel({ provider, model }) {
  switch (provider) {
    case "anthropic": return anthropic(model);
    case "compatible": return compatible(model);
    case "gateway": return model;
    case "openai": return openai(model);
    default: throw new Error(`Unsupported provider: ${provider}`);
  }
}

function resolveModel(model) {
  // AI SDK creator/model strings use its global Gateway provider. Resolve the
  // string before wrapLanguageModel(), which operates on model objects.
  return typeof model === "string" ? gateway(model) : model;
}

// Factory-wide instrumentation maximizes coverage and keeps call sites clean.
export function createInstrumentedModel(choice) {
  return wrapLanguageModel({
    model: resolveModel(createBaseModel(choice)),
    middleware: [applicationMiddleware, metergraphMiddleware],
  });
}

// Alternatively, leave createBaseModel() unchanged and use this at selected
// call sites when narrower instrumentation or attribution is more important.
export function instrumentAtCallSite(model) {
  return wrapLanguageModel({
    model: resolveModel(model),
    middleware: [applicationMiddleware, metergraphMiddleware],
  });
}

const choice = parseModelSelection(process.argv[2]);
const model = process.env.INSTRUMENT_AT_CALL_SITE
  ? instrumentAtCallSite(createBaseModel(choice))
  : createInstrumentedModel(choice);

try {
  if (process.env.DRY_RUN) {
    console.log(`${model.provider}/${model.modelId}`);
  } else {
    const { text } = await mg.track("examples.factory.generate", () =>
      generateText({ model, prompt: "Write one sentence about observable AI." }),
    );
    console.log(text);
  }
} finally {
  try {
    await mg.flush();
  } finally {
    await mg.shutdown();
  }
}
