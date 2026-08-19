import os

from openai import OpenAI

# MeterGraph integration: import the SDK's explicit BatchFirst API.
import metergraph


# Existing application code: configure the provider client and request.
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
request = {
    "model": "gpt-5-mini",
    "input": "In one sentence, explain why batching can reduce inference cost.",
}


# MeterGraph integration: submit through the provider Batch API first, then
# fall back to one direct request if the deadline expires. A fallback can cause
# the same request to execute and be billed twice, so acknowledgement is explicit.
outcome = metergraph.batch_first(
    client,
    "openai",
    request,
    deadline_seconds=10 * 60,
    # Required: after the deadline, the direct fallback and original batch
    # may both complete and be billed.
    accept_duplicate_provider_execution=True,
)

print(f"result source: {outcome.source}")
print(f"batch outcome: {outcome.metadata.batch_outcome}")
print("provider result:")
print(outcome.result)
