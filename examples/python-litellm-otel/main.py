from __future__ import annotations

import time

import litellm
import metergraph
from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig
from metergraph.opentelemetry import MetergraphGenAIExporter


# MeterGraph integration: install MeterGraph as LiteLLM's OpenTelemetry span
# exporter. Existing LiteLLM call sites do not need MeterGraph wrappers.
litellm.callbacks.append(
    OpenTelemetry(
        OpenTelemetryConfig(
            exporter=MetergraphGenAIExporter(),
            # GenAI content is sensitive and opt-in. LiteLLM emits the standard
            # gen_ai.input.messages and gen_ai.output.messages span attributes.
            capture_message_content="SPAN_ONLY",
        )
    )
)


def run_example() -> str:
    # Existing application code: an ordinary LiteLLM request. mock_response keeps
    # this example synthetic and offline; remove it to call your configured model.
    response = litellm.completion(
        model="openai/gpt-5-mini",
        messages=[
            {"role": "system", "content": "Use synthetic data only."},
            {"role": "user", "content": "Return a synthetic response."},
        ],
        mock_response="Synthetic LiteLLM response",
    )
    return response.choices[0].message.content


if __name__ == "__main__":
    print(run_example())
    # LiteLLM dispatches sync-call logging callbacks off the request path. A
    # long-running service needs no wait; this one-shot example allows it to finish.
    time.sleep(1)
    metergraph.flush()
    metergraph.shutdown()
