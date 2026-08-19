import {
  createProviderRegistry,
  gateway,
  wrapLanguageModel,
  type LanguageModel,
  type LanguageModelMiddleware,
} from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const metergraphMiddleware = vercelAISDKMiddleware({
  aiSdkVersion: 5,
  token: "app-token",
  repository: "acme/widgets",
});
const middleware: LanguageModelMiddleware = metergraphMiddleware;
const protocol: "v2" = metergraphMiddleware.specificationVersion;
declare const provider: Parameters<typeof createProviderRegistry>[0][string];
declare function existingFactory(): LanguageModel;
const registry = createProviderRegistry(
  { provider },
  { languageModelMiddleware: metergraphMiddleware },
);
const baseModel = existingFactory();
const factoryModel = wrapLanguageModel({
  model: typeof baseModel === "string" ? gateway(baseModel) : baseModel,
  middleware: metergraphMiddleware,
});
void middleware;
void protocol;
void factoryModel;
void registry;
