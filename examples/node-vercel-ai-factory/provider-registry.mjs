import { anthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { openai } from "@ai-sdk/openai";
import { createProviderRegistry, gateway, generateText } from "ai";
import * as mg from "metergraph";

const compatible = createOpenAICompatible({
  name: "compatible",
  apiKey: process.env.COMPATIBLE_API_KEY ?? "local-development-key",
  baseURL: process.env.COMPATIBLE_BASE_URL ?? "http://localhost:11434/v1",
});

const registry = createProviderRegistry(
  { anthropic, compatible, gateway, openai },
  {
    languageModelMiddleware: mg.vercelAISDKMiddleware({
      repository: "owner/repository",
      environment: "example",
    }),
  },
);

// Registry IDs use provider:model. Pick any registered provider without
// changing the instrumentation point.
const modelId = process.argv[2] ?? "gateway:anthropic/claude-sonnet-4.5";
const model = registry.languageModel(modelId);

try {
  if (process.env.DRY_RUN) {
    console.log(`${model.provider}/${model.modelId}`);
  } else {
    const { text } = await mg.track("examples.registry.generate", () =>
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
