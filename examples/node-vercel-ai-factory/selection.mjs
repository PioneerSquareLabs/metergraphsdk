export function parseModelSelection(
  value = "openai:gpt-5-mini",
) {
  const separator = value.indexOf(":");
  if (separator <= 0 || separator === value.length - 1) {
    throw new Error("Model selection must be provider:model");
  }

  return {
    provider: value.slice(0, separator),
    model: value.slice(separator + 1),
  };
}
