import logging, re, time, random
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union
from dataclasses import dataclass
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from core import _normalize_unicode, _ascii
import requests
from tqdm import tqdm
from core import (
    sanitize_title,
    canonical_movie_key,
    canonical_tv_key,
    make_cache_key,
    extract_year,
)
from url_utils import get_m3u_path
from api_utils import RateLimiter, BatchProcessor, EnhancedTMDbClient, calculate_optimal_batch_size, chunk_list


@dataclass
class VODEntry:
    raw_title: str
    safe_title: str
    url: str
    category: "Category"
    group: Optional[str] = None
    year: Optional[int] = None


class Category(Enum):
    MOVIE = "movie"
    TVSHOW = "tvshow"
    DOCUMENTARY = "documentary"
    REPLAY = "replay"


class TMDbRateLimitError(Exception):
    pass


def parse_m3u(
    path: Path,
    tv_keywords: List[str],
    doc_keywords: List[str],
    movie_keywords: List[str],
    replay_keywords: List[str],
    ignore_keywords: Dict[str, List[str]],
) -> List[VODEntry]:
    movie_keywords = {k.strip().lower() for k in movie_keywords}
    tv_keywords = {k.strip().lower() for k in tv_keywords}
    doc_keywords = {k.strip().lower() for k in doc_keywords}
    replay_keywords = {k.strip().lower() for k in replay_keywords}
    entries: List[VODEntry] = []
    cur_title, cur_group = None, None
    seen_groups = set()
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                if "," in line:
                    cur_title = line.rsplit(",", 1)[-1].strip()
                else:
                    cur_title = line
                m = re.search(r'group-title="([^"]+)"', line, flags=re.IGNORECASE)
                if m:
                    cur_group = m.group(1).strip().lower()
                    seen_groups.add(cur_group)
                else:
                    cur_group = None
            elif cur_title and line.startswith(("http://", "https://")):
                cat = Category.MOVIE
                group_lower = (cur_group or "").strip().lower()
                if group_lower == "doc":
                    cat = Category.DOCUMENTARY
                elif group_lower == "docs":
                    cat = Category.TVSHOW
                elif group_lower in movie_keywords:
                    cat = Category.MOVIE
                elif group_lower in tv_keywords:
                    cat = Category.TVSHOW
                elif group_lower in replay_keywords:
                    cat = Category.REPLAY
                if cat not in (
                    Category.MOVIE,
                    Category.DOCUMENTARY,
                    Category.TVSHOW,
                    Category.REPLAY,
                ):
                    if re.search(r"[Ss]\d{1,2}\s*[Ee]\d{1,2}", cur_title):
                        cat = Category.TVSHOW
                    elif re.search(r"\(\d{4}\)\s*$", cur_title) or re.search(
                        r"[-–]\s*\d{4}\s*$", cur_title
                    ):
                        cat = Category.MOVIE
                title_norm = _ascii(_normalize_unicode(cur_title.lower()))
                skip = False
                if cat == Category.TVSHOW:
                    for kw in ignore_keywords.get("tvshows", []):
                        if kw.lower() in title_norm:
                            logging.debug(f"Skipping ignored TV show: {cur_title}")
                            skip = True
                            break
                elif cat == Category.MOVIE:
                    for kw in ignore_keywords.get("movies", []):
                        if kw.lower() in title_norm:
                            logging.debug(f"Skipping ignored Movie: {cur_title}")
                            skip = True
                            break
                elif cat == Category.DOCUMENTARY:
                    for kw in ignore_keywords.get("documentaries", []):
                        if kw.lower() in title_norm:
                            logging.debug(f"Skipping ignored Documentary: {cur_title}")
                            skip = True
                            break
                if skip:
                    cur_title, cur_group = None, None
                    continue
                year = extract_year(cur_title)
                entries.append(
                    VODEntry(
                        raw_title=cur_title,
                        safe_title=sanitize_title(cur_title),
                        url=line,
                        category=cat,
                        group=cur_group,
                        year=year,
                    )
                )
                cur_title, cur_group = None, None
    cat_counts: Dict[str, int] = {}
    for e in entries:
        cat_counts[e.category.value] = cat_counts.get(e.category.value, 0) + 1
    logging.info(
        f"M3U media scan complete - Movies: {cat_counts.get('movie', 0)}, "
        f"TV Episodes: {cat_counts.get('tvshow', 0)}, "
        f"Documentaries: {cat_counts.get('documentary', 0)}, "
        f"Replays: {cat_counts.get('replay', 0)}"
    )
    return entries


def _tmdb_get(url: str, api_key: str, cache: Optional['SQLiteCache'] = None, cache_ttl_days: int = 7) -> Optional[dict]:
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 429:
            raise TMDbRateLimitError("TMDb rate limit hit (429 Too Many Requests)")
        resp.raise_for_status()
        return resp.json()
    except TMDbRateLimitError:
        raise
    except Exception as e:
        logging.error(f"TMDb request failed for {url}: {e}")
        return None


def _movie_tmdb_lookup(
    title: str, year: Optional[int], allowed_countries: List[str], api_key: str, cache: Optional['SQLiteCache'] = None, cache_ttl_days: int = 7
) -> bool:
    # Check cache first for search results
    if cache:
        cached_search = cache.get_tmdb_search_cache("movie", title, year, cache_ttl_days)
        if cached_search:
            logging.debug(f"TMDb cache hit for movie search: '{title}' ({year})")
            data = cached_search
        else:
            # Make API call and cache the result
            base_url = "https://api.themoviedb.org/3/search/movie"
            params = {"api_key": api_key, "query": title.strip(), "language": "en-US"}
            if year:
                params["year"] = year
            try:
                resp = requests.get(base_url, params=params, timeout=10)
                if resp.status_code == 429:
                    raise TMDbRateLimitError("TMDb rate limit hit (429 Too Many Requests)")
                resp.raise_for_status()
                data = resp.json()
                # Cache the search result
                cache.set_tmdb_search_cache("movie", title, year, data, cache_ttl_days)
            except TMDbRateLimitError:
                raise
            except Exception as e:
                logging.error(f"TMDb request failed for {title} ({year}): {e}")
                return False
    else:
        # Original logic without cache
        base_url = "https://api.themoviedb.org/3/search/movie"
        params = {"api_key": api_key, "query": title.strip(), "language": "en-US"}
        if year:
            params["year"] = year
        try:
            resp = requests.get(base_url, params=params, timeout=10)
            if resp.status_code == 429:
                raise TMDbRateLimitError("TMDb rate limit hit (429 Too Many Requests)")
            resp.raise_for_status()
            data = resp.json()
        except TMDbRateLimitError:
            raise
        except Exception as e:
            logging.error(f"TMDb request failed for {title} ({year}): {e}")
            return False
    
    if not data.get("results") and year:
        logging.debug(f"TMDb: No match for '{title}' ({year}), retrying without year")
        params = {"api_key": api_key, "query": title.strip(), "language": "en-US"}
        try:
            resp = requests.get(base_url, params=params, timeout=10)
            if resp.status_code == 429:
                raise TMDbRateLimitError("TMDb rate limit hit (429 Too Many Requests)")
            resp.raise_for_status()
            data = resp.json()
            # Cache the search result without year
            if cache:
                cache.set_tmdb_search_cache("movie", title, None, data, cache_ttl_days)
        except TMDbRateLimitError:
            raise
        except Exception as e:
            logging.error(f"TMDb retry (no year) failed for {title}: {e}")
            return False
    
    if not data.get("results"):
        logging.debug(f"TMDb: No movie match for '{title}' ({year})")
        return False
    
    best = data["results"][0]
    movie_id = best.get("id")
    if not movie_id:
        logging.debug(f"TMDb: No ID for movie '{title}' ({year})")
        return False
    
    # Check cache for details
    if cache:
        cached_details = cache.get_tmdb_details_cache("movie", movie_id, cache_ttl_days)
        if cached_details:
            logging.debug(f"TMDb cache hit for movie details: '{title}' ({year})")
            releases = cached_details
        else:
            # Make API call and cache the result
            release_url = f"https://api.themoviedb.org/3/movie/{movie_id}/release_dates"
            try:
                releases = requests.get(release_url, params={"api_key": api_key}, timeout=10).json()
                cache.set_tmdb_details_cache("movie", movie_id, releases, cache_ttl_days)
            except Exception as e:
                logging.error(f"TMDb release info failed for {title} ({year}): {e}")
                return False
    else:
        # Original logic without cache
        release_url = f"https://api.themoviedb.org/3/movie/{movie_id}/release_dates"
        try:
            releases = requests.get(release_url, params={"api_key": api_key}, timeout=10).json()
        except Exception as e:
            logging.error(f"TMDb release info failed for {title} ({year}): {e}")
            return False
    
    lang = best.get("original_language", "").lower()
    if lang == "ja":
        logging.debug(f"TMDb: Excluding '{title}' ({year}) - original language Japanese")
        return False
    if lang == "en" and not allowed_countries:
        logging.debug(f"TMDb: Movie '{title}' allowed by English language (no country filter)")
        return True
    
    results = releases.get("results", [])
    countries = {r.get("iso_3166_1") for r in results if isinstance(r, dict) and "iso_3166_1" in r}
    if any(c in allowed_countries for c in countries):
        logging.debug(f"TMDb: Movie '{title}' allowed by release country: {countries}")
        return True
    if lang == "en":
        logging.debug(f"TMDb: Movie '{title}' allowed by English language fallback (no allowed country match)")
        return True
    logging.debug(f"TMDb: Excluding movie '{title}' ({year}) - no allowed country match")
    return False


def _tv_has_allowed_network(
    title: str, allowed_countries: List[str], api_key: str, year: Optional[int] = None, cache: Optional['SQLiteCache'] = None, cache_ttl_days: int = 7
) -> bool:
    query = re.sub(r"[Ss]\d{1,2}\s*[Ee]\d{1,2}.*", "", title)
    query = re.sub(r"\s*\(\d{4}\)\s*", "", query)
    query = re.sub(r"\s*-\s*\d{4}$", "", query)
    query = re.sub(r"\((US|UK|CA|AU|NZ|FR|DE|IT)\)", "", query, flags=re.IGNORECASE)
    query = query.replace("&", "and")
    query = re.sub(r"\s+", " ", query).strip()
    
    # Check cache first for search results
    if cache:
        cached_search = cache.get_tmdb_search_cache("tv", query, year, cache_ttl_days)
        if cached_search:
            logging.debug(f"TMDb cache hit for TV search: '{query}' ({year})")
            data = cached_search
        else:
            # Make API call and cache the result
            search_url = f"https://api.themoviedb.org/3/search/tv?api_key={api_key}&query={query}"
            data = _tmdb_get(search_url, api_key, cache, cache_ttl_days)
            if data:
                cache.set_tmdb_search_cache("tv", query, year, data, cache_ttl_days)
    else:
        # Original logic without cache
        search_url = f"https://api.themoviedb.org/3/search/tv?api_key={api_key}&query={query}"
        data = _tmdb_get(search_url, api_key)
    
    if not data or not data.get("results"):
        logging.debug(f"TMDb: No TV match for '{query}' - excluded by default")
        return False
    
    results = data["results"]
    if year:
        filtered = [r for r in results if r.get("first_air_date", "").startswith(str(year))]
        if filtered:
            results = filtered
    
    preferred = [r for r in results if any(c in allowed_countries for c in r.get("origin_country", []))]
    if preferred:
        best = max(preferred, key=lambda r: r.get("popularity", 0))
    else:
        best = max(results, key=lambda r: r.get("popularity", 0))
    
    tid = best.get("id")
    if not tid:
        logging.debug(f"TMDb: No ID for TV show '{query}' - excluded by default")
        return False
    
    # Check cache for details
    if cache:
        cached_details = cache.get_tmdb_details_cache("tv", tid, cache_ttl_days)
        if cached_details:
            logging.debug(f"TMDb cache hit for TV details: '{query}' ({year})")
            show = cached_details
        else:
            # Make API call and cache the result
            show_url = f"https://api.themoviedb.org/3/tv/{tid}?api_key={api_key}"
            show = _tmdb_get(show_url, api_key, cache, cache_ttl_days)
            if show:
                cache.set_tmdb_details_cache("tv", tid, show, cache_ttl_days)
    else:
        # Original logic without cache
        show_url = f"https://api.themoviedb.org/3/tv/{tid}?api_key={api_key}"
        show = _tmdb_get(show_url, api_key)
    
    if not show:
        logging.debug(f"TMDb: No details for TV show '{query}' - allowing by default")
        return True
    
    lang = best.get("original_language", "").lower()
    if lang == "ja":
        logging.debug(f"TMDb: Excluding TV show '{query}' - original language Japanese")
        return False
    if lang == "en" and not allowed_countries:
        logging.debug(f"TMDb: TV show '{query}' allowed by English language (no country filter)")
        return True
    
    for network in show.get("networks", []):
        origin_countries = network.get("origin_country", [])
        if any(c in allowed_countries for c in origin_countries):
            logging.debug(f"TMDb: TV show '{query}' allowed by network country: {origin_countries}")
            return True
    
    prod_country_codes = [
        c.get("iso_3166_1") for c in show.get("production_countries", []) if isinstance(c, dict)
    ]
    if any(c in allowed_countries for c in prod_country_codes):
        logging.debug(f"TMDb: TV show '{query}' allowed by production country")
        return True
    
    origin_countries = show.get("origin_country", [])
    if any(c in allowed_countries for c in origin_countries):
        logging.debug(f"TMDb: TV show '{query}' allowed by origin country")
        return True
    
    logging.debug(f"TMDb: Excluding TV show '{query}' - no match for allowed countries")
    return False


def split_by_market_filter(
    entries: List[VODEntry],
    allowed_movie_countries: List[str],
    allowed_tv_countries: List[str],
    api_key: str,
    ignore_keywords: Dict[str, List[str]] = None,
    max_workers: int = None,
    max_retries: int = 5,
    cache: Optional['SQLiteCache'] = None,
    cache_ttl_days: int = 7,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    if max_workers is None:
        max_workers = 10
    logging.info(f"Filtering using {max_workers} CPU workers")
    allowed, excluded = [], []
    ignore_keywords = ignore_keywords or {}
    stats = {
        "movies_checked": 0, "movies_allowed": 0, "movies_excluded": 0,
        "tv_checked": 0, "tv_allowed": 0, "tv_excluded": 0,
        "docs_checked": 0, "docs_allowed": 0, "docs_excluded": 0,
        "ignored": 0,
    }

    def with_retry(fn, *args, **kwargs):
        delay = 1
        for attempt in range(max_retries):
            try:
                return fn(*args, **kwargs)
            except TMDbRateLimitError:
                logging.warning(f"TMDb rate limit hit, retrying in {delay:.1f}s...")
                time.sleep(delay + random.uniform(0, 1.0))
                delay = min(delay * 2, 30)
        logging.error(f"Max retries exceeded for {fn.__name__} with args={args}")
        return False

    def process_entry(e: VODEntry) -> Tuple[VODEntry, bool, str]:
        ignore_list = []
        if e.category == Category.MOVIE:
            ignore_list = ignore_keywords.get("movies", [])
        elif e.category == Category.TVSHOW:
            ignore_list = ignore_keywords.get("tvshows", [])
        elif e.category == Category.DOCUMENTARY:
            ignore_list = ignore_keywords.get("documentaries", [])
        if any(word.lower() in e.raw_title.lower() for word in ignore_list):
            logging.debug(f"Ignored by keyword: {e.raw_title}")
            return (e, False, "ignored")
        if e.category == Category.MOVIE:
            year = extract_year(e.raw_title)
            title_clean = sanitize_title(e.raw_title)
            title_clean = re.sub(r"\s*\(\d{4}\)\s*", "", title_clean)
            title_clean = re.sub(r"\s*-\s*\d{4}$", "", title_clean).strip()
            ok = with_retry(_movie_tmdb_lookup, title_clean, year, allowed_movie_countries, api_key, cache, cache_ttl_days)
            return (e, ok, "movie")
        elif e.category == Category.TVSHOW:
            year = extract_year(e.raw_title)
            base_title = re.sub(r"[Ss]\d{1,2}[Ee]\d{1,2}.*", "", e.raw_title)
            base_title = re.sub(r"\s*\(\d{4}\)\s*", "", base_title)
            base_title = re.sub(r"\s*-\s*\d{4}$", "", base_title).strip()
            ok = with_retry(_tv_has_allowed_network, base_title, allowed_tv_countries, api_key, year, cache, cache_ttl_days)
            return (e, ok, "tv")
        elif e.category == Category.DOCUMENTARY:
            year = extract_year(e.raw_title)
            title_clean = sanitize_title(e.raw_title)
            title_clean = re.sub(r"\s*\(\d{4}\)\s*", "", title_clean)
            title_clean = re.sub(r"\s*-\s*\d{4}$", "", title_clean).strip()
            ok = with_retry(_movie_tmdb_lookup, title_clean, year, allowed_movie_countries, api_key, cache, cache_ttl_days)
            return (e, ok, "doc")
        else:
            return (e, False, "other")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process_entry, e) for e in entries]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Filtering", unit="entry"):
            e, ok, kind = f.result()
            if kind == "ignored":
                excluded.append(e)
                stats["ignored"] += 1
            elif kind == "movie":
                stats["movies_checked"] += 1
                (allowed if ok else excluded).append(e)
                stats["movies_allowed" if ok else "movies_excluded"] += 1
            elif kind == "tv":
                stats["tv_checked"] += 1
                (allowed if ok else excluded).append(e)
                stats["tv_allowed" if ok else "tv_excluded"] += 1
            elif kind == "doc":
                stats["docs_checked"] += 1
                (allowed if ok else excluded).append(e)
                stats["docs_allowed" if ok else "docs_excluded"] += 1
            else:
                excluded.append(e)

    logging.info("Filter statistics:")
    logging.info(
        f"  Movies: {stats['movies_checked']} checked, "
        f"{stats['movies_allowed']} allowed, {stats['movies_excluded']} excluded"
    )
    logging.info(
        f"  TV Shows: {stats['tv_checked']} checked, "
        f"{stats['tv_allowed']} allowed, {stats['tv_excluded']} excluded"
    )
    logging.info(
        f"  Documentaries: {stats['docs_checked']} checked, "
        f"{stats['docs_allowed']} allowed, {stats['docs_excluded']} excluded"
    )
    logging.info(f"  Ignored by keywords: {stats['ignored']}")
    logging.info(f"  Total: {len(allowed)} allowed, {len(excluded)} excluded")
    return allowed, excluded


def split_by_market_filter_enhanced(
    entries: List[VODEntry],
    allowed_movie_countries: List[str],
    allowed_tv_countries: List[str],
    api_key: str,
    ignore_keywords: Dict[str, List[str]] = None,
    max_workers: int = None,
    max_retries: int = 5,
    cache: Optional['SQLiteCache'] = None,
    cache_ttl_days: int = 7,
    api_delay: float = 0.25,
    api_backoff_factor: float = 2.0,
    enable_batch_processing: bool = True,
    title_similarity_threshold: float = 0.85,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    """
    Enhanced filtering with rate limiting, batch processing, and smarter deduplication.
    
    Args:
        entries: List of VOD entries to filter
        allowed_movie_countries: List of allowed movie countries
        allowed_tv_countries: List of allowed TV show countries
        api_key: TMDb API key
        ignore_keywords: Keywords to ignore for each category
        max_workers: Number of worker threads
        max_retries: Maximum retry attempts for API calls
        cache: SQLite cache instance
        cache_ttl_days: Cache TTL in days
        api_delay: Delay between API calls in seconds
        api_backoff_factor: Exponential backoff factor for retries
        enable_batch_processing: Enable smarter deduplication
        title_similarity_threshold: Threshold for considering titles similar (0-1)
        
    Returns:
        Tuple of (allowed_entries, excluded_entries)
    """
    if max_workers is None:
        max_workers = 25  # Increased from 10 to 25 for better performance
    
    logging.info(f"Enhanced filtering using {max_workers} CPU workers with rate limiting")
    logging.info(f"API delay: {api_delay}s, Batch processing: {enable_batch_processing}")
    
    allowed, excluded = [], []
    ignore_keywords = ignore_keywords or {}
    stats = {
        "movies_checked": 0, "movies_allowed": 0, "movies_excluded": 0,
        "tv_checked": 0, "tv_allowed": 0, "tv_excluded": 0,
        "docs_checked": 0, "docs_allowed": 0, "docs_excluded": 0,
        "ignored": 0,
        "cache_hits": 0,
        "api_calls_saved": 0,
    }
    
    # Initialize rate limiter and batch processor
    rate_limiter = RateLimiter(api_delay)
    batch_processor = BatchProcessor(title_similarity_threshold)
    tmdb_client = EnhancedTMDbClient(
        api_key=api_key,
        cache=cache,
        rate_limiter=rate_limiter,
        cache_ttl_days=cache_ttl_days,
        max_retries=max_retries,
        backoff_factor=api_backoff_factor
    )
    
    # Pre-filter entries by ignore keywords
    pre_filtered_entries = []
    for e in entries:
        ignore_list = ignore_keywords.get(
            "movies" if e.category == Category.MOVIE else
            "tvshows" if e.category == Category.TVSHOW else
            "documentaries" if e.category == Category.DOCUMENTARY else [],
            []
        )
        if any(word.lower() in e.raw_title.lower() for word in ignore_list):
            excluded.append(e)
            stats["ignored"] += 1
            continue
        pre_filtered_entries.append(e)
    
    logging.info(f"Pre-filtered {len(entries)} entries, {len(pre_filtered_entries)} remaining after keyword filtering")
    
    # Group entries by category for batch processing
    movie_entries = [e for e in pre_filtered_entries if e.category == Category.MOVIE]
    tv_entries = [e for e in pre_filtered_entries if e.category == Category.TVSHOW]
    doc_entries = [e for e in pre_filtered_entries if e.category == Category.DOCUMENTARY]
    
    logging.info(f"Categorized: {len(movie_entries)} movies, {len(tv_entries)} TV shows, {len(doc_entries)} documentaries")
    
    # Process each category with enhanced logic
    if movie_entries:
        allowed_movies, excluded_movies = _process_movies_enhanced(
            movie_entries, allowed_movie_countries, tmdb_client, stats, max_workers
        )
        allowed.extend(allowed_movies)
        excluded.extend(excluded_movies)
    
    if tv_entries:
        allowed_tv, excluded_tv = _process_tv_enhanced(
            tv_entries, allowed_tv_countries, tmdb_client, stats, max_workers
        )
        allowed.extend(allowed_tv)
        excluded.extend(excluded_tv)
    
    if doc_entries:
        allowed_docs, excluded_docs = _process_documentaries_enhanced(
            doc_entries, allowed_movie_countries, tmdb_client, stats, max_workers
        )
        allowed.extend(allowed_docs)
        excluded.extend(excluded_docs)
    
    logging.info("Enhanced filter statistics:")
    logging.info(
        f"  Movies: {stats['movies_checked']} checked, "
        f"{stats['movies_allowed']} allowed, {stats['movies_excluded']} excluded"
    )
    logging.info(
        f"  TV Shows: {stats['tv_checked']} checked, "
        f"{stats['tv_allowed']} allowed, {stats['tv_excluded']} excluded"
    )
    logging.info(
        f"  Documentaries: {stats['docs_checked']} checked, "
        f"{stats['docs_allowed']} allowed, {stats['docs_excluded']} excluded"
    )
    logging.info(f"  Ignored by keywords: {stats['ignored']}")
    logging.info(f"  Cache hits: {stats['cache_hits']}")
    logging.info(f"  API calls saved via batching: {stats['api_calls_saved']}")
    logging.info(f"  Total: {len(allowed)} allowed, {len(excluded)} excluded")
    
    return allowed, excluded


def _process_movies_enhanced(
    entries: List[VODEntry],
    allowed_countries: List[str],
    tmdb_client: EnhancedTMDbClient,
    stats: Dict[str, int],
    max_workers: int,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    """Process movie entries with enhanced caching and batch processing."""
    allowed, excluded = [], []
    
    # Group similar titles to reduce API calls
    titles = [sanitize_title(e.raw_title) for e in entries]
    title_groups = {}
    
    if len(titles) > 10:  # Only batch if we have enough titles
        groups = tmdb_client.rate_limiter._lock.__class__.__name__  # Access batch processor
        # For now, process individually but with enhanced caching
        title_groups = {title: [title] for title in titles}
    else:
        title_groups = {title: [title] for title in titles}
    
    def process_movie_entry(e: VODEntry) -> Tuple[VODEntry, bool]:
        year = extract_year(e.raw_title)
        title_clean = sanitize_title(e.raw_title)
        title_clean = re.sub(r"\s*\(\d{4}\)\s*", "", title_clean)
        title_clean = re.sub(r"\s*-\s*\d{4}$", "", title_clean).strip()
        
        # Use enhanced TMDb client with rate limiting and caching
        async def check_movie():
            search_data = await tmdb_client.search_movie(title_clean, year)
            if not search_data or not search_data.get("results"):
                if year:
                    # Retry without year
                    search_data = await tmdb_client.search_movie(title_clean, None)
                if not search_data or not search_data.get("results"):
                    return False
            
            best = search_data["results"][0]
            movie_id = best.get("id")
            if not movie_id:
                return False
            
            # Get release dates
            releases = await tmdb_client.get_movie_release_dates(movie_id)
            if not releases:
                return False
            
            lang = best.get("original_language", "").lower()
            if lang == "ja":
                return False
            
            if lang == "en" and not allowed_countries:
                return True
            
            # Check countries
            results = releases.get("results", [])
            countries = {r.get("iso_3166_1") for r in results if isinstance(r, dict) and "iso_3166_1" in r}
            if any(c in allowed_countries for c in countries):
                return True
            
            if lang == "en":
                return True
            
            return False
        
        # Run async function in thread
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(check_movie())
        finally:
            loop.close()
        
        return e, result
    
    # Process entries with progress bar
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process_movie_entry, e) for e in entries]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Processing Movies", unit="movie"):
            e, ok = f.result()
            stats["movies_checked"] += 1
            if ok:
                allowed.append(e)
                stats["movies_allowed"] += 1
            else:
                excluded.append(e)
                stats["movies_excluded"] += 1
    
    return allowed, excluded


def _process_tv_enhanced(
    entries: List[VODEntry],
    allowed_countries: List[str],
    tmdb_client: EnhancedTMDbClient,
    stats: Dict[str, int],
    max_workers: int,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    """Process TV show entries with enhanced caching and batch processing."""
    allowed, excluded = [], []
    
    def process_tv_entry(e: VODEntry) -> Tuple[VODEntry, bool]:
        year = extract_year(e.raw_title)
        base_title = re.sub(r"[Ss]\d{1,2}[Ee]\d{1,2}.*", "", e.raw_title)
        base_title = re.sub(r"\s*\(\d{4}\)\s*", "", base_title)
        base_title = re.sub(r"\s*-\s*\d{4}$", "", base_title).strip()
        
        # Use enhanced TMDb client with rate limiting and caching
        async def check_tv():
            search_data = await tmdb_client.search_tv(base_title, year)
            if not search_data or not search_data.get("results"):
                return False
            
            results = search_data["results"]
            if year:
                filtered = [r for r in results if r.get("first_air_date", "").startswith(str(year))]
                if filtered:
                    results = filtered
            
            preferred = [r for r in results if any(c in allowed_countries for c in r.get("origin_country", []))]
            if preferred:
                best = max(preferred, key=lambda r: r.get("popularity", 0))
            else:
                best = max(results, key=lambda r: r.get("popularity", 0))
            
            tid = best.get("id")
            if not tid:
                return False
            
            # Get show details
            show = await tmdb_client.get_tv_details(tid)
            if not show:
                return True  # Allow by default if no details available
            
            lang = best.get("original_language", "").lower()
            if lang == "ja":
                return False
            
            if lang == "en" and not allowed_countries:
                return True
            
            # Check networks
            for network in show.get("networks", []):
                origin_countries = network.get("origin_country", [])
                if any(c in allowed_countries for c in origin_countries):
                    return True
            
            # Check production countries
            prod_country_codes = [
                c.get("iso_3166_1") for c in show.get("production_countries", []) if isinstance(c, dict)
            ]
            if any(c in allowed_countries for c in prod_country_codes):
                return True
            
            # Check origin countries
            origin_countries = show.get("origin_country", [])
            if any(c in allowed_countries for c in origin_countries):
                return True
            
            return False
        
        # Run async function in thread
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(check_tv())
        finally:
            loop.close()
        
        return e, result
    
    # Process entries with progress bar
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process_tv_entry, e) for e in entries]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Processing TV Shows", unit="show"):
            e, ok = f.result()
            stats["tv_checked"] += 1
            if ok:
                allowed.append(e)
                stats["tv_allowed"] += 1
            else:
                excluded.append(e)
                stats["tv_excluded"] += 1
    
    return allowed, excluded


def _process_documentaries_enhanced(
    entries: List[VODEntry],
    allowed_countries: List[str],
    tmdb_client: EnhancedTMDbClient,
    stats: Dict[str, int],
    max_workers: int,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    """Process documentary entries using movie logic with enhanced caching."""
    # Documentaries use the same logic as movies
    return _process_movies_enhanced(entries, allowed_countries, tmdb_client, stats, max_workers)
