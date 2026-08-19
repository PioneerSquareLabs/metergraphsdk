import {
  createProviderRegistry,
  gateway,
  wrapLanguageModel,
  type LanguageModel,
  type LanguageModelMiddleware,
} from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const middleware: LanguageModelMiddleware = vercelAISDKMiddleware();
const configuredMiddleware: LanguageModelMiddleware = vercelAISDKMiddleware({
  token: "app-token",
  repository: "acme/widgets",
  environment: "test",
  captureText: false,
});
declare const provider: Parameters<typeof createProviderRegistry>[0][string];
declare function existingFactory(): LanguageModel;
const registry = createProviderRegistry(
  { provider },
  { languageModelMiddleware: middleware },
);
const baseModel = existingFactory();
const factoryModel = typeof baseModel === "string"
  ? wrapLanguageModel({ model: gateway(baseModel), middleware })
  : baseModel.specificationVersion === "v2"
    ? (() => { throw new Error("AI SDK 6/7 factories require current provider packages"); })()
    : wrapLanguageModel({ model: baseModel, middleware });

// @ts-expect-error Framework and raw protocol selectors are mutually exclusive.
vercelAISDKMiddleware({ aiSdkVersion: 5, specificationVersion: "v3" });

void middleware;
void configuredMiddleware;
void factoryModel;
void registry;
