import type { LanguageModelMiddleware } from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const metergraphMiddleware = vercelAISDKMiddleware({
  aiSdkVersion: 5,
  token: "app-token",
  repository: "acme/widgets",
});
const middleware: LanguageModelMiddleware = metergraphMiddleware;
const protocol: "v2" = metergraphMiddleware.specificationVersion;
void middleware;
void protocol;
