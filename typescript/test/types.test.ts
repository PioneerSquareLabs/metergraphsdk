import type { LanguageModelMiddleware } from "ai";

import { vercelAISDKMiddleware } from "../dist/index.js";

const current: LanguageModelMiddleware = vercelAISDKMiddleware();
const v2Middleware = vercelAISDKMiddleware({
  specificationVersion: "v2",
});
const v2: LanguageModelMiddleware = v2Middleware;
const literalV2: "v2" = v2Middleware.specificationVersion;

void current;
void v2;
void literalV2;
