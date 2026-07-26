"""Configuration - all values come from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ARM_URL: str = "http://MiniPC:8080"
    TDARR_URL: str = "http://localhost:8266"

    PATH_MINIPC_COMPLETED: str = "/media/minipc/completed"
    PATH_MINIPC_MUSIC: str     = "/media/minipc/music"
    PATH_NAS_LOSSLESS: str     = "/media/nas/Lossless"
    PATH_NAS_PLEX: str         = "/media/nas/Plex"

    # Upscaler staging (pipeline queues SD files here; upscaler Docker service reads it)
    PATH_UPSCALE_STAGING: str  = "/media/upscale-queue"
    PATH_UPSCALE_OUTPUT: str   = "/media/upscale-output"

    TDARR_LIBRARY_MOVIES: str = ""
    TDARR_LIBRARY_TV: str     = ""

    STUCK_THRESHOLD_RIPPING: int     = 180
    STUCK_THRESHOLD_MOVING: int      = 30
    STUCK_THRESHOLD_TRANSCODING: int = 240
    STUCK_THRESHOLD_UPSCALING: int   = 1440  # 24 hours (upscaling is slow)

    DB_PATH: str = "/data/pipeline.db"

    class Config:
        env_file = ".env"


settings = Settings()
