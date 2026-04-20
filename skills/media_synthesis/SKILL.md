# Media Synthesis Skill

## Description
Generate AI-powered podcast episodes and companion articles on any topic. This skill orchestrates research gathering, script generation, text-to-speech synthesis, and audio splicing to produce complete media bundles.

## Usage
When the user asks to generate a podcast, audio episode, deep dive, or media bundle on a topic, use this skill.

## Instructions
To generate a podcast episode, make an HTTP POST request to the gateway's media synthesis endpoint:

```
POST http://localhost:8642/v1/media/synthesize
Content-Type: application/json

{
  "topic": "The topic to generate a podcast about",
  "source_refs": ["optional", "reference", "urls"],
  "tts_provider": "mac_native"
}
```

The `tts_provider` can be `"mac_native"` (default, uses macOS built-in TTS) or `"elevenlabs"` (requires API key).

The response will include:
- `bundle_id`: Unique identifier for the generated media bundle
- `title`: The episode title
- `audio_url`: Path to the generated audio file
- `article_path`: Path to the companion markdown article

The generated episode is automatically published to `~/.hermes/podcasts/` for playback in the Hermes Companion app.

## Examples
- "Generate a podcast about quantum computing breakthroughs"
- "Create a deep dive episode on the state of AI agents in enterprise"
- "Make a quick audio brief about today's market movements"
- "Synthesize a podcast covering the latest developments in Swift concurrency"
