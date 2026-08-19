import { gateway, generateText, wrapLanguageModel } from "ai";
import { openai } from "@ai-sdk/openai";
import * as mg from "metergraph";

const usingGateway = Boolean(process.env.AI_GATEWAY_API_KEY);
const baseModel = usingGateway
  ? gateway("anthropic/claude-sonnet-4.5")
  : openai("gpt-5-mini");
const model = wrapLanguageModel({
  model: baseModel,
  middleware: mg.vercelAISDKMiddleware({
    repository: "owner/repository",
    environment: "example",
  }),
});

try {
  await mg.trace("haiku-workflow", () =>
    mg.route("haiku.write", async () => {
      const { text } = await generateText({
        model,
        prompt: "Write a haiku about metered clouds.",
      });
      console.log(text);
    }),
  );
} finally {
  try {
    await mg.flush();
  } finally {
    await mg.shutdown();
  }
}
