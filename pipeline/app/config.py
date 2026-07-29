"""Configuration - all values come from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ARM_URL: str = "http://MiniPC:8080"
    TDARR_URL: str = "http://localhost:8266"

    PATH_MINIPC_COMPLETED: str = "/media/minipc/completed"
    PATH_MINIPC_MUSIC: str     = "/media/minipc/music"
    PATH_NAS_LOSSLESS: str     = "/media/nas/Lossless"
    PATH_NAS_PLEX: str         = "/media/nas/Plex"

    # Bulk intake: drop existing ripped files here for pipeline processing
    # Files are moved into Lossless/ and processed end-to-end
    PATH_BULK_INTAKE: str   = "/media/bulk-intake"

    # Upscaler staging (pipeline queues SD files here; upscaler Docker service reads it)
    PATH_UPSCALE_STAGING: str  = "/media/upscale-queue"
    PATH_UPSCALE_OUTPUT: str   = "/media/upscale-output"

    # Resolution threshold — content BELOW this height is queued for AI upscaling
    # Match this to TARGET_HEIGHT in chrisdesktop/docker-compose.yml
    TARGET_UPSCALE_HEIGHT: int = 2160   # 1080=Full HD, 1440=2K, 2160=4K
    TDARR_LIBRARY_TV: str     = ""

    STUCK_THRESHOLD_RIPPING: int     = 180
    STUCK_THRESHOLD_MOVING: int      = 30
    STUCK_THRESHOLD_TRANSCODING: int = 240
    STUCK_THRESHOLD_UPSCALING: int   = 1440  # 24 hours (upscaling is slow)

    DB_PATH: str = "/data/pipeline.db"

    class Config:
        env_file = ".env"


settings = Settings()
