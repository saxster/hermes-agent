from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class AudioClip(BaseModel):
    source_title: str = Field(description="Name of the original podcast")
    start_time_sec: float
    end_time_sec: float
    transcript_text: str
    speaker_name: Optional[str]
    local_file_path: Optional[str]

class ResearchCitation(BaseModel):
    source_url: str
    source_type: Literal["browser_scrape", "knowledge_graph", "academic_paper", "news_article"]
    extracted_claim: str
    confidence_score: float

class VisualAsset(BaseModel):
    asset_type: Literal["mermaid_diagram", "scraped_chart", "markdown_table"]
    local_path_or_content: str
    description_for_host: str

class MasterResearchLedger(BaseModel):
    target_topic: str
    core_theses: List[str]
    supporting_audio_clips: List[AudioClip]
    external_research: List[ResearchCitation]
    visual_assets: List[VisualAsset]

class DialogueLine(BaseModel):
    speaker: Literal["Host_1", "Host_2", "Audio_Splice"]
    text: str
    voice_tone: Literal["curious", "excited", "skeptical", "thoughtful", "laughing"] = "thoughtful"
    trigger_visual_asset_index: Optional[int] = None
    audio_splice_ref: Optional[AudioClip] = None
    reference_citation_index: Optional[int] = None

class PodcastProject(BaseModel):
    episode_title: str
    target_duration_minutes: int
    timeline: List[DialogueLine]
    publish_to_rss: bool = True
    generate_vidcast_mp4: bool = True
    youtube_metadata: dict = Field(default_factory=dict)
