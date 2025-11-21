import os
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional


@dataclass
class Config:
    m3u: str
    sqlite_cache_file: Path
    log_file: Path
    output_dir: Path
    existing_media_dirs: List[Path]
    tmdb_api: str
    dry_run: bool = False
    max_workers: Optional[int] = None
    allowed_movie_countries: List[str] = None
    allowed_tv_countries: List[str] = None
    write_non_us_report: bool = True
    tv_group_keywords: List[str] = None
    doc_group_keywords: List[str] = None
    movie_group_keywords: List[str] = None
    replay_group_keywords: List[str] = None
    ignore_keywords: Dict[str, List[str]] = None
    emby_api_url: Optional[str] = None
    emby_api_key: Optional[str] = None


def _coerce_bool(val, default=False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return default


def _parse_list(value: str) -> List[str]:
    """Parse comma-separated string into list of strings."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config(path: Path) -> Config:
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    
    # Handle max_workers
    mw = config.get("settings", "max_workers", fallback="8")
    if isinstance(mw, str) and mw.lower() == "max":
        mw = os.cpu_count() or 8
    else:
        try:
            mw = int(mw)
        except ValueError:
            mw = 8
    
    # Parse existing_media_dirs
    existing_dirs_str = config.get("paths", "existing_media_dirs", fallback="")
    existing_dirs = [Path(p.strip()) for p in existing_dirs_str.split(",") if p.strip()]
    if not existing_dirs:
        raise KeyError("Config missing 'existing_media_dirs' in [paths] section")
    
    # Parse ignore_keywords
    ignore_tvshows = _parse_list(config.get("ignore", "tvshows", fallback=""))
    ignore_movies = _parse_list(config.get("ignore", "movies", fallback=""))
    ignore_keywords = {
        "tvshows": ignore_tvshows,
        "movies": ignore_movies
    }
    
    # Handle M3U path - don't convert URLs to Path objects
    m3u_source = config.get("paths", "m3u")
    # Remove surrounding quotes if present
    m3u_source = m3u_source.strip('"\'')
    
    return Config(
        m3u=m3u_source,  # Keep as string, let get_m3u_path handle the conversion
        sqlite_cache_file=Path(config.get("paths", "sqlite_cache_file")),
        log_file=Path(config.get("paths", "log_file")),
        output_dir=Path(config.get("paths", "output_dir")),
        existing_media_dirs=existing_dirs,
        tmdb_api=config.get("api", "tmdb_api"),
        dry_run=_coerce_bool(config.get("settings", "dry_run", fallback="false")),
        max_workers=mw,
        allowed_movie_countries=_parse_list(config.get("countries", "allowed_movie_countries", fallback="US")),
        allowed_tv_countries=_parse_list(config.get("countries", "allowed_tv_countries", fallback="US")),
        write_non_us_report=_coerce_bool(config.get("settings", "write_non_us_report", fallback="true")),
        tv_group_keywords=_parse_list(config.get("keywords", "tv_group_keywords", fallback="")),
        doc_group_keywords=_parse_list(config.get("keywords", "doc_group_keywords", fallback="")),
        movie_group_keywords=_parse_list(config.get("keywords", "movie_group_keywords", fallback="")),
        replay_group_keywords=_parse_list(config.get("keywords", "replay_group_keywords", fallback="")),
        ignore_keywords=ignore_keywords,
        emby_api_url=config.get("api", "emby_api_url", fallback=None),
        emby_api_key=config.get("api", "emby_api_key", fallback=None),
    )
