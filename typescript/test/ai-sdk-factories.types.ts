import { anthropic } from "@ai-sdk/anthropic";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { openai } from "@ai-sdk/openai";
import {
  createProviderRegistry,
  defaultSettingsMiddleware,
  gateway,
  wrapLanguageModel,
  type LanguageModel,
} from "ai";

import { vercelAISDKMiddleware } from "../dist/index.js";

const metergraph = vercelAISDKMiddleware({ repository: "acme/widgets" });
const compatible = createOpenAICompatible({
  name: "compatible",
  apiKey: "test-key",
  baseURL: "https://llm.example.test/v1",
});

const registry = createProviderRegistry(
  { anthropic, compatible, gateway, openai },
  { languageModelMiddleware: metergraph },
);

const registryModels: LanguageModel[] = [
  registry.languageModel("anthropic:claude-haiku-4-5"),
  registry.languageModel("compatible:custom-model"),
  registry.languageModel("gateway:anthropic/claude-sonnet-4.5"),
  registry.languageModel("openai:gpt-5-mini"),
];

type ModelChoice =
  | { provider: "anthropic"; model: string }
  | { provider: "compatible"; model: string }
  | { provider: "gateway"; model: string }
  | { provider: "openai"; model: string };

function existingFactory(choice: ModelChoice): LanguageModel {
  switch (choice.provider) {
    case "anthropic": return anthropic(choice.model);
    case "compatible": return compatible(choice.model);
    case "gateway": return gateway(choice.model);
    case "openai": return openai(choice.model);
  }
}

const factoryModel = existingFactory({ provider: "openai", model: "gpt-5-mini" });
const wrappedFactoryModel: LanguageModel = wrapLanguageModel({
  model: typeof factoryModel === "string" ? gateway(factoryModel) : factoryModel,
  middleware: [
    metergraph,
    defaultSettingsMiddleware({ settings: { temperature: 0.2 } }),
  ],
});

void registryModels;
void wrappedFactoryModel;
