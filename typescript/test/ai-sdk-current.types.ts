import type { LanguageModelMiddleware } from "ai";
import {
  setDefaultTags,
  vercelAISDKMiddleware,
  withContext,
  withSession,
  withTags,
} from "../dist/index.js";

const middleware: LanguageModelMiddleware = vercelAISDKMiddleware();
const configuredMiddleware: LanguageModelMiddleware = vercelAISDKMiddleware({
  token: "app-token",
  repository: "acme/widgets",
  environment: "test",
  captureText: false,
});

// @ts-expect-error Framework and raw protocol selectors are mutually exclusive.
vercelAISDKMiddleware({ aiSdkVersion: 5, specificationVersion: "v3" });

void middleware;
void configuredMiddleware;
setDefaultTags({ service: "worker" });
void withContext({ sessionId: "run-1", tags: { customer: 42 } }, async () => "done");
void withSession("run-1", async () => "done");
void withTags({ customer: 42 }, async () => "done");
