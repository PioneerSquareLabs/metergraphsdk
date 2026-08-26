// Capture OpenRouter usage and billing evidence with an ordinary OpenAI client.
// Run: see this folder's README.md. Uses placeholder credentials and synthetic
// prompts; point OPENROUTER_BASE_URL at a local fake to run without a real call.

import { pathToFileURL } from "node:url";

import OpenAI from "openai";
import * as mg from "metergraph";

function reportedCost(usage) {
  // Application helper (example output only): read OpenRouter's reported account
  // charge from usage.cost for display. This is not part of the MeterGraph
  // integration — MeterGraph captures the same field independently.
  const cost = usage?.cost;
  return typeof cost === "number" && Number.isFinite(cost) ? cost : undefined;
}

export async function main() {
  // MeterGraph integration (init): identify captured calls; token and ingest URL
  // come from METERGRAPH_APP_TOKEN / METERGRAPH_INGEST_URL.
  mg.init({ repository: "owner/repository", environment: "example" });

  const apiKey = process.env.OPENROUTER_API_KEY ?? "sk-or-REPLACE_WITH_YOUR_KEY";
  const baseURL = process.env.OPENROUTER_BASE_URL ?? "https://openrouter.ai/api/v1";
  // Application code: an ordinary OpenAI client pointed at OpenRouter.
  const openai = new OpenAI({ apiKey, baseURL });
  // MeterGraph integration (wrap): the public openrouter.ai host over HTTPS is
  // auto-detected; any other base URL uses the explicit gateway override.
  const url = new URL(baseURL);
  const client = url.protocol === "https:" && url.hostname === "openrouter.ai"
    ? mg.wrap(openai)
    : mg.wrap(openai, { gateway: "openrouter" });

  const requestedModel = "anthropic/claude-sonnet-4.6";
  try {
    // Application code: an ordinary non-streaming call.
    const response = await client.chat.completions.create({
      model: requestedModel,
      messages: [{ role: "user", content: "Explain cache-aware pricing in one sentence." }],
    });
    const nonstream = {
      servedModel: response.model,
      content: response.choices[0].message.content,
      reportedCostUsd: reportedCost(response.usage),
    };

    // Application code: an ordinary streaming call and its iteration. OpenRouter
    // sends the final usage event and MeterGraph leaves every chunk visible.
    const stream = await client.chat.completions.create({
      model: requestedModel,
      messages: [{ role: "user", content: "Stream a short note about metered clouds." }],
      stream: true,
    });
    let text = "";
    let chunkCount = 0;
    let servedModel;
    let reportedCostUsd;
    for await (const chunk of stream) {
      chunkCount += 1;
      servedModel ??= chunk.model;
      text += chunk.choices?.[0]?.delta?.content ?? "";
      if (chunk.usage?.cost !== undefined) reportedCostUsd = reportedCost(chunk.usage);
    }
    const streamSummary = { servedModel, content: text, chunkCount, reportedCostUsd };
    return { nonstream, stream: streamSummary };
  } finally {
    // MeterGraph integration (flush/shutdown): deliver captured rows and stop
    // background work.
    await mg.flush();
    await mg.shutdown();
  }
}

// Run only when invoked directly (path-safe against spaces and encoding).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const result = await main();
  console.log(`non-streaming served_model=${result.nonstream.servedModel} reported_cost_usd=${result.nonstream.reportedCostUsd}`);
  console.log(`streaming served_model=${result.stream.servedModel} reported_cost_usd=${result.stream.reportedCostUsd} chunks=${result.stream.chunkCount}`);
}
