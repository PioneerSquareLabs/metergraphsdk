import type { LanguageModelMiddleware } from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const metergraphMiddleware = vercelAISDKMiddleware({ aiSdkVersion: 5 });
const middleware: LanguageModelMiddleware = metergraphMiddleware;
const protocol: "v2" = metergraphMiddleware.specificationVersion;
void middleware;
void protocol;
