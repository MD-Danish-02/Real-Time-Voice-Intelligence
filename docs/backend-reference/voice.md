# Voice
`VoiceCallOrchestrator` (STT -> LLM with tool loop -> TTS, interruption handling, never-dead-air fallback) is provider-agnostic and unit-tested with mocks. `VoxAIVoiceFrameProcessor` plugs it into Pipecat. The Context Engine assembles lead/company/knowledge/previous calls/rules/actions. The agent can only call 12 named tools (`voice_pipeline/agent_tools.py`), each tenant/lead-scoped and gated by tenant rules.
## Known gaps (need real infrastructure, not testable here)
* Bridging Asterisk audio (ARI `externalMedia` RTP) into the Pipecat pipeline and starting an orchestrator per answered call is **not wired**. `AsteriskARIClient.create_external_media_channel` and the frame processor exist; the per-call session glue does not.
* Silero VAD is expected from Pipecat's pipeline config; it is not instantiated in this repo.
* STT/LLM/TTS latency is not measured yet (the Analytics "AI Performance" tab says so).
* Call recording capture/upload to S3 with signed URLs and retention jobs is not implemented (settings + UI placeholders only).
* Inbound call routing to a tenant (number -> tenant mapping) is not implemented.
