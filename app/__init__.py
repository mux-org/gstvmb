# The single source of truth for the API contract version. The REST surface is
# unversioned in the URL (no /v1 prefix); it is additive-only on a single track,
# and the real version boundary is the container image tag. This constant is
# surfaced via FastAPI's OpenAPI metadata (/docs, /openapi.json) so clients and
# tooling can still record which contract they're talking to. Keep it in step
# with the image tag at release time.
__version__ = "1.0.0"
