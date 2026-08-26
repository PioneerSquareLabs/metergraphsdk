// Compile-time contract for wrap()'s gateway option. Compiled by `npm run
// test:types`; the @ts-expect-error lines fail the build if the rejection stops
// working.
import { wrap } from "../dist/index.js";
import type { WrapOptions } from "../dist/index.js";

declare const client: {
  chat: { completions: { create(args: unknown): Promise<unknown> } };
};

// The client type (and return identity) is preserved across every form.
const a: typeof client = wrap(client);
const b: typeof client = wrap(client, "openai"); // provider-string overload remains
const c: typeof client = wrap(client, { gateway: "openrouter" });
const d: typeof client = wrap(client, { provider: "openai", gateway: "openrouter" });
const e: typeof client = wrap(client, { provider: "anthropic" });

// An unsupported gateway value is rejected.
// @ts-expect-error gateway must be "openrouter"
wrap(client, { gateway: "portkey" });

// A provider that contradicts the gateway is rejected.
// @ts-expect-error a non-openai provider cannot combine with gateway
wrap(client, { provider: "anthropic", gateway: "openrouter" });

// An unknown provider string is rejected.
// @ts-expect-error unknown provider
wrap(client, "bedrock");

// WrapOptions is usable as a named public type.
const options: WrapOptions = { provider: "openai", gateway: "openrouter" };

void a;
void b;
void c;
void d;
void e;
void options;
