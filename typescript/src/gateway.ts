// Generic OpenAI-compatible gateway detection and billing-evidence extraction.
// A gateway is described by trusted hosts, the endpoints whose responses are
// qualified for cost evidence, and fixed provenance strings. OpenRouter is the
// first qualified contract. The SDK only transports a small scalar allowlist
// from a response it already observes; it never computes cost, fetches catalog
// data, or infers a served provider.

export interface GatewayContract {
  name: string;
  hosts: Set<string>;
  qualifiedEndpoints: Set<string>;
  costSource: string;
  upstreamCostSource: string;
}

const OPENROUTER: GatewayContract = {
  name: "openrouter",
  hosts: new Set(["openrouter.ai"]),
  qualifiedEndpoints: new Set(["chat.completions"]),
  costSource: "openrouter.usage.cost",
  upstreamCostSource: "openrouter.usage.cost_details.upstream_inference_cost",
};

export const GATEWAYS: Record<string, GatewayContract> = { [OPENROUTER.name]: OPENROUTER };

const MAX_IDENTIFIER_LEN = 512;

function get(value: unknown, key: string): unknown {
  return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

// Exact-hostname match over HTTPS, never substring. Paths do not affect it. The
// caller restricts auto-detection to OpenAI-compatible clients.
export function detectGateway(client: unknown): string | undefined {
  const baseUrl = get(client, "baseURL") ?? get(client, "base_url");
  if (typeof baseUrl !== "string" || !baseUrl.trim()) return undefined;
  let url: URL;
  try {
    url = new URL(baseUrl.trim());
  } catch {
    return undefined;
  }
  if (url.protocol !== "https:") return undefined;
  for (const contract of Object.values(GATEWAYS)) {
    if (contract.hosts.has(url.hostname)) return contract.name;
  }
  return undefined;
}

// The caller value is never interpolated into the error: it may be a secret.
export function resolveGateway(value: unknown): string {
  const name = String(value).trim().toLowerCase();
  if (!(name in GATEWAYS)) {
    throw new Error(
      "metergraph.wrap() received an unsupported gateway; supported gateways are: "
        + Object.keys(GATEWAYS).sort().join(", "),
    );
  }
  return name;
}

// Finite, non-negative number; booleans (typeof "boolean") and non-numbers are
// rejected, zero is preserved.
function billingAmount(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
  return value;
}

// Identity (gateway, served_model) is emitted for any detected gateway call;
// cost evidence and its fixed sources only on the contract's qualified
// endpoints. A source appears only alongside the value it describes. The served
// provider is never inferred.
export function gatewayEvidence(
  gateway: string | undefined,
  endpoint: string,
  response: unknown,
): Record<string, unknown> {
  try {
    if (!gateway) return {};
    const contract = GATEWAYS[gateway];
    if (!contract) return {};

    const evidence: Record<string, unknown> = { gateway: contract.name };

    const servedModel = get(response, "model");
    if (typeof servedModel === "string" && servedModel.trim()) {
      evidence.served_model = servedModel.slice(0, MAX_IDENTIFIER_LEN);
    }

    if (contract.qualifiedEndpoints.has(endpoint)) {
      const usage = get(response, "usage");
      const cost = billingAmount(get(usage, "cost"));
      if (cost !== undefined) {
        evidence.reported_cost_usd = cost;
        evidence.reported_cost_source = contract.costSource;
      }
      const details = get(usage, "cost_details");
      const upstream = billingAmount(get(details, "upstream_inference_cost"));
      if (upstream !== undefined) {
        evidence.reported_upstream_cost_usd = upstream;
        evidence.reported_upstream_cost_source = contract.upstreamCostSource;
      }
    }

    return evidence;
  } catch {
    return {};
  }
}
