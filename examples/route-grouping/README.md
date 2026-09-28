# Manual route grouping

These examples answer a specific customer question: "How do I decide which
calls appear together as workloads?" They show a user-facing operation wrapped
in one trace, with named route scopes around the product surfaces you want to
compare separately.

Each example identifies:

- the logical `trace()` around the user-visible operation;
- the `route()` scopes that should appear as separate workloads; and
- the provider implementation used by the example.

## Available route-grouping examples

- [Content generation: one trace, two workloads](content-generation/one-trace-two-workloads/)
