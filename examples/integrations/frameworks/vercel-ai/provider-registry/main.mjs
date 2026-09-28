import { anthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { openai } from "@ai-sdk/openai";
import { createProviderRegistry, gateway, generateText } from "ai";

// MeterGraph integration: add this import.
import * as mg from "metergraph";

// Existing application code: configure the providers your application uses.
const compatible = createOpenAICompatible({
  name: "compatible",
  apiKey: process.env.COMPATIBLE_API_KEY ?? "local-development-key",
  baseURL: process.env.COMPATIBLE_BASE_URL ?? "http://localhost:11434/v1",
});

// Existing application code: keep providers in the AI SDK registry.
const registry = createProviderRegistry(
  { anthropic, compatible, gateway, openai },
  {
    // MeterGraph integration: add one middleware option to the registry.
    // Every language model obtained from this registry is now captured.
    languageModelMiddleware: mg.vercelAISDKMiddleware({
      repository: "owner/repository",
      environment: "example",
    }),
  },
);

// Existing application code: select and call a model normally.
const modelId = process.argv[2] ?? "gateway:anthropic/claude-sonnet-4.5";
const model = registry.languageModel(modelId);

try {
  if (process.env.DRY_RUN) {
    console.log(`${model.provider}/${model.modelId}`);
  } else {
    // MeterGraph integration: track() gives this operation a stable route.
    const { text } = await mg.track("examples.registry.generate", () =>
      generateText({ model, prompt: "Write one sentence about observable AI." }),
    );
    console.log(text);
  }
} finally {
  // MeterGraph integration: deliver queued events and stop background work.
  await mg.shutdown();
}
