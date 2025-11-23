import logging
import re
import concurrent.futures
import argparse
from pathlib import Path
from collections import defaultdict
import requests
import config
from folder_utils import compare_and_clean_folders, generate_comparison_report
from core import (
    SQLiteCache,
    build_existing_media_cache,
    canonical_movie_key,
    canonical_tv_key,
    make_cache_key,
    sanitize_title,
    extract_year,
    KeyGenerator,
)
from m3u_utils import (
    parse_m3u,
    split_by_market_filter,
    Category,
    VODEntry,
)
from strm_utils import (
    write_strm_file,
    cleanup_strm_tree,
    movie_strm_path,
    tv_strm_path,
    doc_strm_path,
)
from url_utils import get_m3u_path


def touch_emby(api_url: str, api_key: str):
    try:
        refresh_url = api_url.rstrip("/") + "/Library/Refresh"
        headers = {"X-Emby-Token": api_key}
        r = requests.post(refresh_url, headers=headers, timeout=10)
        if r.status_code in (200, 204):
            logging.info(f"Triggered Emby library refresh via {refresh_url}")
        else:
            logging.warning(f"Emby refresh failed: {r.status_code} - {r.text} ({refresh_url})")
    except Exception as e:
        logging.error(f"Emby refresh error: {e}", exc_info=True)


def write_excluded_report(path: Path, excluded, allowed_count: int, enabled: bool):
    if not enabled:
        logging.info("Excluded report skipped (write_non_us_report = false)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    movies = [e.raw_title for e in excluded if e.category == Category.MOVIE]
    shows = [e.raw_title for e in excluded if e.category == Category.TVSHOW]
    grouped_shows = defaultdict(list)
    for title in shows:
        base = re.sub(r"[sS]\d{1,2}\s*[eE]\d{1,2}.*", "", title).strip()
        grouped_shows[base].append(title)
    with path.open("w", encoding="utf-8") as f:
        f.write("=== Excluded Entries Report ===\n\n")
        f.write(f"Total allowed: {allowed_count}\n")
        f.write(f"Total excluded: {len(excluded)}\n\n")
        f.write("--- Movies ---\n")
        for m in sorted(movies):
            f.write(f"{m}\n")
        f.write(f"\nTotal movies excluded: {len(movies)}\n\n")
        f.write("--- TV Shows ---\n")
        for base, eps in sorted(grouped_shows.items()):
            f.write(f"{base} — {len(eps)} episodes excluded\n")
        f.write(f"\nTotal shows excluded: {len(grouped_shows)}\n")
        f.write("=== End of Report ===\n")
    logging.info(f"Excluded entries written: {path}")


def run_pipeline():
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(str(cfg.log_file), mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    # Handle M3U source (local file or URL)
    m3u_path = get_m3u_path(cfg.m3u)
    logging.info(f"Processing M3U from: {m3u_path}")
    
    output_dir = cfg.output_dir
    db_path = cfg.sqlite_cache_file
    ignore_keywords = cfg.ignore_keywords or {}
    write_non_us_report = cfg.write_non_us_report
    cache = SQLiteCache(db_path)
    existing = {}
    for d in cfg.existing_media_dirs:
        existing.update(build_existing_media_cache(Path(d)))
    cache.replace_existing_media(existing)
    existing_keys = set(existing.keys())
    entries = parse_m3u(
        m3u_path,
        tv_keywords=cfg.tv_group_keywords,
        doc_keywords=cfg.doc_group_keywords,
        movie_keywords=cfg.movie_group_keywords,
        replay_keywords=cfg.replay_group_keywords,
        ignore_keywords=cfg.ignore_keywords,
    )
    
    # Filter out live TV channels (REPLAY category) to keep only VOD content
    original_count = len(entries)
    entries = [entry for entry in entries if entry.category != Category.REPLAY]
    replay_count = original_count - len(entries)
    logging.info(f"Filtered out {replay_count} REPLAY (live TV) entries, keeping {len(entries)} VOD entries")
    unique_entries = {}
    for e in entries:
        key = KeyGenerator.generate_key(e)
        unique_entries[key] = e
    entries = list(unique_entries.values())
    logging.info("Deduplicated playlist entries: %d -> %d unique", len(entries), len(unique_entries))
    strm_cache = cache.strm_cache_dict()
    logging.debug("Loaded %d entries from strm_cache", len(strm_cache))
    to_check = []
    reused_allowed = []
    reused_excluded = []
    for e in entries:
        key = KeyGenerator.generate_key(e)
        if key in existing_keys:
            reused_allowed.append(e)
            logging.debug(f"Reusing local-existing result for {e.raw_title}")
            continue
        cached = strm_cache.get(key)
        if cached and cached.get("allowed") is not None:
            if cached["allowed"] == 1:
                reused_allowed.append(e)
                logging.debug(f"Reusing cached allowed result for {e.raw_title}")
            else:
                reused_excluded.append(e)
                logging.debug(f"Reusing cached excluded result for {e.raw_title}")
        else:
            logging.debug("CACHE MISS: raw_title=%r key=%s cached_entry=%s", e.raw_title, key, strm_cache.get(key))
            to_check.append(e)
    allowed, excluded = split_by_market_filter(
        to_check,
        allowed_movie_countries=cfg.allowed_movie_countries,
        allowed_tv_countries=cfg.allowed_tv_countries,
        api_key=cfg.tmdb_api,
        max_workers=cfg.max_workers,
        ignore_keywords=cfg.ignore_keywords,
    )
    allowed.extend(reused_allowed)
    excluded.extend(reused_excluded)
    write_excluded_report(output_dir / "excluded_entries.txt", excluded, len(allowed), write_non_us_report)
    existing_keys = set(existing.keys())
    strm_cache = cache.strm_cache_dict()
    new_cache = strm_cache.copy()
    written_count = 0
    skipped_count = 0

    def process_entry(e):
        nonlocal written_count, skipped_count
        key = None
        rel_path = None
        logging.debug(
            "PROCESS START: raw_title=%r, safe_title=%r, category=%s, year=%s, url=%s",
            getattr(e, "raw_title", None),
            getattr(e, "safe_title", None),
            getattr(e, "category", None),
            getattr(e, "year", None),
            getattr(e, "url", None),
        )
        if not e.year:
            e.year = extract_year(e.raw_title)
            if e.year:
                logging.debug("Extracted year=%s from raw_title %r", e.year, e.raw_title)
        ignore = ignore_keywords.get("tvshows" if e.category == Category.TVSHOW else "movies", [])
        if any(word.lower() in e.raw_title.lower() for word in ignore):
            logging.debug("Ignored by keyword: %s", e.raw_title)
            return
        try:
            key = KeyGenerator.generate_key(e)
            logging.debug(f"Key built for {e.raw_title} ({e.category.value}): {key}")
            
            if e.category == Category.MOVIE:
                rel_path = movie_strm_path(output_dir, e)
            elif e.category == Category.TVSHOW:
                base = re.sub(r"[sS]\d{1,2}\s*[eE]\d{1,2}.*", "", e.raw_title).strip()
                m = re.search(r"[sS](\d{1,2})\s*[eE](\d{1,2})", e.raw_title)
                if m:
                    season, episode = int(m.group(1)), int(m.group(2))
                    rel_path = tv_strm_path(
                        output_dir,
                        VODEntry(
                            raw_title=base,
                            safe_title=sanitize_title(base),
                            url=e.url,
                            category=e.category,
                            year=e.year,
                        ),
                        season,
                        episode,
                    )
                else:
                    rel_path = tv_strm_path(output_dir, e, 1, 1)
            elif e.category == Category.DOCUMENTARY:
                rel_path = doc_strm_path(output_dir, e)
            else:
                logging.warning("Unknown category %s for entry %r", e.category, e.raw_title)
                return
            if not key:
                logging.error("No cache key generated for %r", e.raw_title)
                return
            abs_path = output_dir / rel_path
            url = e.url
            if key in existing_keys:
                skipped_count += 1
                logging.debug("Skip existing media: %s", e.raw_title)
                new_cache[key] = {"url": e.url, "path": None, "allowed": 1}
                return
            cached = strm_cache.get(key)
            if cached:
                cached_path = Path(cached.get("path") or "").resolve() if cached.get("path") else None
                if cached.get("url") == url and cached.get("path") and cached_path == abs_path.resolve():
                    skipped_count += 1
                    logging.debug("Skip cached (unchanged): %s", e.raw_title)
                    new_cache[key] = {
                        "url": cached.get("url"),
                        "path": cached.get("path"),
                        "allowed": cached.get("allowed", 1),
                    }
                    return
            write_strm_file(output_dir, rel_path, url)
            new_cache[key] = {"url": url, "path": str(abs_path.resolve()), "allowed": 1}
            written_count += 1
            logging.info("STRM written: %s", abs_path)
        except Exception as ex:
            logging.error(
                "Error processing entry %r (category=%s, year=%s): %s",
                e.raw_title,
                getattr(e, "category", None),
                getattr(e, "year", None),
                ex,
                exc_info=True,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.max_workers) as executor:
        list(executor.map(process_entry, allowed))
    for e in excluded:
        key = KeyGenerator.generate_key(e)
        new_cache[key] = {"url": e.url, "path": None, "allowed": 0}
    cache.replace_strm_cache(new_cache)
    logging.info("Cleaning up orphan STRMs...")
    cleanup_strm_tree(output_dir, new_cache)
    if not cfg.dry_run and getattr(cfg, "emby_api_url", None) and getattr(cfg, "emby_api_key", None):
        logging.info("Triggering Emby library refresh...")
        touch_emby(cfg.emby_api_url, cfg.emby_api_key)
    else:
        logging.info("Skipping Emby refresh (either dry_run or not configured)")
    logging.info(
        f"VOD/Strm process complete: {written_count} STRMs written, {skipped_count} skipped, {len(excluded)} excluded"
    )


def run_folder_comparison():
    """Run folder comparison and duplicate deletion."""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    # Setup logging for folder comparison
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.addHandler(console_handler)
    
    # Check if any comparison directories are configured
    if not cfg.compare_movies_dir and not cfg.compare_tv_dir:
        logging.error("No comparison directories configured. Please set compare_movies_dir and/or compare_tv_dir in config.ini")
        return
    
    logging.info("Starting folder comparison...")
    
    # Run the folder comparison
    results = compare_and_clean_folders(
        output_dir=cfg.output_dir,
        compare_movies_dir=cfg.compare_movies_dir,
        compare_tv_dir=cfg.compare_tv_dir,
        dry_run=cfg.dry_run,
        require_confirmation=True
    )
    
    # Log summary
    total_folders = sum(folders for folders, _ in results.values())
    total_files = sum(files for _, files in results.values())
    
    if cfg.dry_run:
        logging.info(f"DRY RUN: Would delete {total_folders} folders and {total_files} files")
    else:
        logging.info(f"COMPLETED: Deleted {total_folders} folders and {total_files} files")


def generate_folder_report():
    """Generate a report of duplicate folders without deleting anything."""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    # Check if any comparison directories are configured
    if not cfg.compare_movies_dir and not cfg.compare_tv_dir:
        print("No comparison directories configured. Please set compare_movies_dir and/or compare_tv_dir in config.ini")
        return
    
    # Generate and print the report
    report = generate_comparison_report(
        output_dir=cfg.output_dir,
        compare_movies_dir=cfg.compare_movies_dir,
        compare_tv_dir=cfg.compare_tv_dir
    )
    
    print(report)


def main():
    """Main entry point with command-line argument parsing."""
    parser = argparse.ArgumentParser(description="M3U to STRM Converter with Folder Comparison")
    parser.add_argument(
        "--compare-folders", 
        action="store_true",
        help="Compare folders and delete duplicates from output directory"
    )
    parser.add_argument(
        "--report", 
        action="store_true",
        help="Generate a report of duplicate folders without deleting"
    )
    parser.add_argument(
        "--find-duplicates",
        action="store_true",
        help="Run duplicate finder after processing M3U files"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to custom config file (default: config.ini in script directory)"
    )
    
    args = parser.parse_args()
    
    # Determine which mode to run
    if args.compare_folders:
        run_folder_comparison()
    elif args.report:
        generate_folder_report()
    else:
        # Default: run the normal M3U processing pipeline
        run_pipeline()
        
        # Check if we should run duplicate finder
        if args.find_duplicates:
            logging.info("Running duplicate finder as requested...")
            run_folder_comparison()
        else:
            # Ask user if they want to run duplicate finder
            try:
                response = input("\nWould you like to run the duplicate finder now? (y/N): ").strip().lower()
                if response in ['y', 'yes']:
                    logging.info("Running duplicate finder as requested by user...")
                    run_folder_comparison()
                else:
                    logging.info("Skipping duplicate finder.")
            except (KeyboardInterrupt, EOFError):
                logging.info("Skipping duplicate finder.")


if __name__ == "__main__":
    main()
