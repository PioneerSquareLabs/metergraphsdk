import { anthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { openai } from "@ai-sdk/openai";
import {
  defaultSettingsMiddleware,
  gateway,
  generateText,
  wrapLanguageModel,
} from "ai";

// MeterGraph integration: add this import.
import * as mg from "metergraph";
import { parseModelSelection } from "./selection.mjs";

// Existing application code: provider and application-middleware setup.
const compatible = createOpenAICompatible({
  name: "compatible",
  apiKey: process.env.COMPATIBLE_API_KEY ?? "local-development-key",
  baseURL: process.env.COMPATIBLE_BASE_URL ?? "http://localhost:11434/v1",
});
const applicationMiddleware = defaultSettingsMiddleware({
  settings: { temperature: 0.2 },
});

// Existing application code: keep the factory that the application already uses.
export function createBaseModel({ provider, model }) {
  switch (provider) {
    case "anthropic": return anthropic(model);
    case "compatible": return compatible(model);
    case "gateway": return model;
    case "openai": return openai(model);
    default: throw new Error(`Unsupported provider: ${provider}`);
  }
}

// MeterGraph integration: initialize capture once with the middleware options.
const metergraphMiddleware = mg.vercelAISDKMiddleware({
  repository: "owner/repository",
  environment: "example",
});

// MeterGraph integration: wrap the existing factory's single controlled exit.
export function createInstrumentedModel(choice) {
  const baseModel = createBaseModel(choice);
  return wrapLanguageModel({
    // Existing Gateway selections can be strings; the wrapper needs a model object.
    model: typeof baseModel === "string" ? gateway(baseModel) : baseModel,
    middleware: [applicationMiddleware, metergraphMiddleware],
  });
}

const choice = parseModelSelection(process.argv[2]);
const model = createInstrumentedModel(choice);

try {
  if (process.env.DRY_RUN) {
    console.log(`${model.provider}/${model.modelId}`);
  } else {
    // MeterGraph integration: track() gives this operation a stable route.
    const { text } = await mg.track("examples.factory.generate", () =>
      generateText({ model, prompt: "Write one sentence about observable AI." }),
    );
    console.log(text);
  }
} finally {
  // MeterGraph integration: deliver queued events and stop background work.
  await mg.shutdown();
}
