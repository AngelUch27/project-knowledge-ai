from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    project_id: str
    gcp_location: str
    gcs_bucket: str
    gemini_model: str
    agent_search_location: str
    agent_search_app_id: str
    agent_search_serving_config: str

    def missing_gcp_values(self) -> list[str]:
        missing = []
        if not self.project_id:
            missing.append("GCP_PROJECT_ID")
        if not self.gcs_bucket:
            missing.append("GCS_BUCKET")
        return missing

    def missing_agent_search_values(self) -> list[str]:
        missing = []
        if not self.project_id:
            missing.append("GCP_PROJECT_ID")
        if not self.agent_search_app_id:
            missing.append("AGENT_SEARCH_APP_ID")
        if not self.agent_search_location:
            missing.append("AGENT_SEARCH_LOCATION")
        if not self.agent_search_serving_config:
            missing.append("AGENT_SEARCH_SERVING_CONFIG")
        return missing


def get_settings() -> Settings:
    return Settings(
        project_id=os.getenv("GCP_PROJECT_ID", "").strip(),
        gcp_location=os.getenv("GCP_LOCATION", "us-central1").strip(),
        gcs_bucket=os.getenv("GCS_BUCKET", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        agent_search_location=os.getenv("AGENT_SEARCH_LOCATION", "global").strip(),
        agent_search_app_id=os.getenv(
            "AGENT_SEARCH_APP_ID", "knowledge-agent-search_1792367963658"
        ).strip(),
        agent_search_serving_config=os.getenv(
            "AGENT_SEARCH_SERVING_CONFIG", "default_search"
        ).strip(),
    )
