from abc import ABC, abstractmethod
import os

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, voice_tone: str, output_path: str) -> str:
        """Synthesize text to audio and save to output_path. Returns the path."""
        pass

class MacNativeTTSProvider(TTSProvider):
    async def synthesize(self, text: str, voice_tone: str, output_path: str) -> str:
        import asyncio
        # We can map 'voice_tone' to specific Mac voices
        # For simplicity, using saying command on macOS
        voice = "Samantha"
        if voice_tone == "skeptical":
            voice = "Alex"
            
        # run the macOS `say` command securely
        # 'say -v VoiceName -o output_path.aiff "text"'
        # Note: 'say' outputs AIFF format. pydub can read it, or we can use AFConvert
        temp_aiff = output_path.replace(".mp3", ".aiff")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-o", temp_aiff, text
        )
        await proc.wait()
        
        # We use pydub to convert audio if needed at orchestrator level,
        # but let's just assume we return the aiff for Mac, or we convert here.
        if temp_aiff != output_path:
            try:
                from pydub import AudioSegment
                sound = AudioSegment.from_file(temp_aiff, format="aiff")
                sound.export(output_path, format="mp3")
                if os.path.exists(temp_aiff):
                    os.remove(temp_aiff)
            except ImportError:
                return temp_aiff # Fallback if pydub is missing
                
        return output_path

class ElevenLabsTTSProvider(TTSProvider):
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        
    async def synthesize(self, text: str, voice_tone: str, output_path: str) -> str:
        if not self.api_key:
            raise ValueError("ELEVENLABS_API_KEY must be set in environment or provided to use ElevenLabsTTSProvider")
        
        # implementation skeleton for elevenlabs
        # Normally would use elevenlabs client securely:
        # client = elevenlabs.Asyncclient(api_key=self.api_key)
        # audio = await client.generate(...)
        # with open(output_path, "wb") as f: f.write(audio)
        
        # Placeholder
        print(f"[ElevenLabs] Faking synthesis for: {text[:20]}...")
        # create a dummy file
        with open(output_path, "wb") as f:
            f.write(b"dummy_audio_bytes")
        return output_path
