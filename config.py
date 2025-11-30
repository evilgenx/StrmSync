import os
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional
import logging


@dataclass
class Config:
    m3u: str
    sqlite_cache_file: Path
    log_file: Path
    output_dir: Path
    existing_media_dirs: List[Path]
    dry_run: bool = False
    max_workers: Optional[int] = None
    write_non_us_report: bool = True
    tv_group_keywords: List[str] = None
    doc_group_keywords: List[str] = None
    movie_group_keywords: List[str] = None
    replay_group_keywords: List[str] = None
    ignore_keywords: Dict[str, List[str]] = None
    emby_api_url: Optional[str] = None
    emby_api_key: Optional[str] = None
    jellyfin_api_url: Optional[str] = None
    jellyfin_api_key: Optional[str] = None
    compare_movies_dir: Optional[Path] = None
    compare_tv_dir: Optional[Path] = None
    # Advanced Library Management settings
    enable_quality_scoring: bool = True
    enable_health_monitoring: bool = True
    enable_auto_replacement: bool = False
    enable_analytics: bool = True
    health_check_interval: int = 3600
    health_check_timeout: int = 10
    health_check_retries: int = 3
    resolution_weight: float = 0.4
    uptime_weight: float = 0.3
    response_time_weight: float = 0.2
    error_rate_weight: float = 0.1
    min_quality_threshold: float = 5.0
    max_replacement_attempts: int = 3
    replacement_cooldown: int = 86400
    # Health check sampling settings
    health_check_mode: str = "random"  # "all", "random", "percentage"
    health_check_sample_size: int = 50  # Number of random files to test
    health_check_sample_percentage: float = 0.0  # Percentage of total library to test (0.0 to 1.0)
    # Live TV settings
    enable_live_tv: bool = False
    live_tv_output_dir: Optional[Path] = None
    epg_url: Optional[str] = None
    channel_groups: List[str] = None
    channel_logos_url: Optional[str] = None
    enable_channel_editor: bool = True


class ConfigValidator:
    """Configuration validation to prevent runtime errors."""
    
    @staticmethod
    def validate(config: Config) -> List[str]:
        """
        Validate configuration values and return list of errors.
        
        Args:
            config: The Config object to validate
            
        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        
        if not config.existing_media_dirs:
            errors.append("At least one existing_media_dir is required")
        
        # Validate directory paths
        for dir_path in config.existing_media_dirs:
            if not dir_path.exists():
                errors.append(f"Existing media directory does not exist: {dir_path}")
        
        if config.output_dir:
            try:
                config.output_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"Cannot create output directory {config.output_dir}: {e}")
        
        # Validate file paths
        if config.sqlite_cache_file:
            try:
                config.sqlite_cache_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"Cannot create cache directory {config.sqlite_cache_file.parent}: {e}")
        
        if config.log_file:
            try:
                config.log_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"Cannot create log directory {config.log_file.parent}: {e}")
        
        # Validate numeric values
        if config.max_workers is not None:
            if config.max_workers < 1:
                errors.append("max_workers must be at least 1")
            elif config.max_workers > 50:
                errors.append("max_workers cannot exceed 50 (sanity check)")
        
        # Validate API rate limiting settings
        if config.api_delay < 0:
            errors.append("api_delay must be non-negative")
        if config.api_max_retries < 1:
            errors.append("api_max_retries must be at least 1")
        if config.api_backoff_factor < 1:
            errors.append("api_backoff_factor must be at least 1")
        
        # Validate batch processing settings
        if config.title_similarity_threshold < 0 or config.title_similarity_threshold > 1:
            errors.append("title_similarity_threshold must be between 0 and 1")
        
        # Validate M3U source
        if not config.m3u:
            errors.append("M3U source path or URL is required")
        elif not (config.m3u.startswith(('http://', 'https://')) or Path(config.m3u).exists()):
            errors.append(f"M3U source does not exist and is not a valid URL: {config.m3u}")
        
        # Validate optional comparison directories
        if config.compare_movies_dir and not config.compare_movies_dir.exists():
            errors.append(f"Comparison movies directory does not exist: {config.compare_movies_dir}")
        
        if config.compare_tv_dir and not config.compare_tv_dir.exists():
            errors.append(f"Comparison TV directory does not exist: {config.compare_tv_dir}")
        
        # Validate Emby configuration (if provided)
        if config.emby_api_url and not config.emby_api_key:
            errors.append("Emby API key is required when Emby API URL is provided")
        
        if config.emby_api_key and not config.emby_api_url:
            errors.append("Emby API URL is required when Emby API key is provided")
        
        # Validate Jellyfin configuration (if provided)
        if config.jellyfin_api_url and not config.jellyfin_api_key:
            errors.append("Jellyfin API key is required when Jellyfin API URL is provided")
        
        if config.jellyfin_api_key and not config.jellyfin_api_url:
            errors.append("Jellyfin API URL is required when Jellyfin API key is provided")
        
        return errors
    
    @staticmethod
    def validate_and_log(config: Config) -> bool:
        """
        Validate configuration and log any errors.
        
        Args:
            config: The Config object to validate
            
        Returns:
            True if valid, False if errors found
        """
        errors = ConfigValidator.validate(config)
        
        if errors:
            logging.error("Configuration validation failed:")
            for error in errors:
                logging.error(f"  - {error}")
            return False
        
        logging.info("Configuration validation passed")
        return True


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
    
    # Parse comparison directories (optional)
    compare_movies_dir_str = config.get("paths", "compare_movies_dir", fallback="").strip()
    compare_movies_dir = Path(compare_movies_dir_str) if compare_movies_dir_str else None
    
    compare_tv_dir_str = config.get("paths", "compare_tv_dir", fallback="").strip()
    compare_tv_dir = Path(compare_tv_dir_str) if compare_tv_dir_str else None
    
    return Config(
        m3u=m3u_source,  # Keep as string, let get_m3u_path handle the conversion
        sqlite_cache_file=Path(config.get("paths", "sqlite_cache_file")),
        log_file=Path(config.get("paths", "log_file")),
        output_dir=Path(config.get("paths", "output_dir")),
        existing_media_dirs=existing_dirs,
        dry_run=_coerce_bool(config.get("settings", "dry_run", fallback="false")),
        max_workers=mw,
        write_non_us_report=_coerce_bool(config.get("settings", "write_non_us_report", fallback="true")),
        tv_group_keywords=_parse_list(config.get("keywords", "tv_group_keywords", fallback="")),
        doc_group_keywords=_parse_list(config.get("keywords", "doc_group_keywords", fallback="")),
        movie_group_keywords=_parse_list(config.get("keywords", "movie_group_keywords", fallback="")),
        replay_group_keywords=_parse_list(config.get("keywords", "replay_group_keywords", fallback="")),
        ignore_keywords=ignore_keywords,
        emby_api_url=config.get("api", "emby_api_url", fallback=None),
        emby_api_key=config.get("api", "emby_api_key", fallback=None),
        jellyfin_api_url=config.get("api", "jellyfin_api_url", fallback=None),
        jellyfin_api_key=config.get("api", "jellyfin_api_key", fallback=None),
        compare_movies_dir=compare_movies_dir,
        compare_tv_dir=compare_tv_dir,
        # Live TV settings
        enable_live_tv=_coerce_bool(config.get("live_tv", "enable_live_tv", fallback="false")),
        live_tv_output_dir=Path(config.get("live_tv", "live_tv_output_dir", fallback="")) if config.get("live_tv", "live_tv_output_dir", fallback="") else None,
        epg_url=config.get("live_tv", "epg_url", fallback=None),
        channel_groups=_parse_list(config.get("live_tv", "channel_groups", fallback="")),
        channel_logos_url=config.get("live_tv", "channel_logos_url", fallback=None),
        enable_channel_editor=_coerce_bool(config.get("live_tv", "enable_channel_editor", fallback="true")),
    )
