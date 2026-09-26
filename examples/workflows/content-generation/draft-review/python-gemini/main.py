import metergraph
from google import genai

metergraph.init(repository="owner/repository", environment="example")
client = metergraph.wrap(genai.Client())


@metergraph.track
def haiku_about(topic: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Write a haiku about {topic}.",
    )
    return response.text


@metergraph.track("haiku.review")
def review_haiku(haiku: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            "Review this haiku for imagery and rhythm, then return a revised "
            f"version:\n\n{haiku}"
        ),
    )
    return response.text


@metergraph.track("haiku.finalize")
def finalize_haiku(haiku: str) -> str:
    chunks: list[str] = []
    for chunk in client.models.generate_content_stream(
        model="gemini-2.5-flash",
        contents=f"Stream a final revision of this haiku:\n\n{haiku}",
    ):
        chunks.append(chunk.text or "")
    return "".join(chunks)


with metergraph.trace("haiku-workflow"):
    with metergraph.route("haiku-draft"):
        draft = haiku_about("metered clouds")

    with metergraph.route("haiku-review"):
        reviewed = review_haiku(draft)
        final = finalize_haiku(reviewed)

print(final)
metergraph.shutdown()
