import type { LanguageModelMiddleware } from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const middleware: LanguageModelMiddleware = vercelAISDKMiddleware();

// @ts-expect-error Framework and raw protocol selectors are mutually exclusive.
vercelAISDKMiddleware({ aiSdkVersion: 5, specificationVersion: "v3" });

void middleware;
