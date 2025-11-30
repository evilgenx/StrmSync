import logging, re, time, random
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union, Callable
from dataclasses import dataclass
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from core import _normalize_unicode, _ascii
from tqdm import tqdm
from core import (
    sanitize_title,
    canonical_movie_key,
    canonical_tv_key,
    make_cache_key,
    extract_year,
)
from url_utils import get_m3u_path


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


def split_by_market_filter(
    entries: List[VODEntry],
    ignore_keywords: Dict[str, List[str]] = None,
) -> Tuple[List[VODEntry], List[VODEntry]]:
    """
    Simplified filtering that allows all content that passes keyword filtering.
    
    Args:
        entries: List of VOD entries to filter
        ignore_keywords: Keywords to ignore for each category
        
    Returns:
        Tuple of (allowed_entries, excluded_entries)
    """
    logging.info("Applying simplified keyword-based filtering")
    allowed, excluded = [], []
    ignore_keywords = ignore_keywords or {}
    stats = {
        "movies_allowed": 0, "movies_excluded": 0,
        "tv_allowed": 0, "tv_excluded": 0,
        "docs_allowed": 0, "docs_excluded": 0,
        "ignored": 0,
    }

    for e in entries:
        # Check ignore keywords
        ignore_list = []
        if e.category == Category.MOVIE:
            ignore_list = ignore_keywords.get("movies", [])
        elif e.category == Category.TVSHOW:
            ignore_list = ignore_keywords.get("tvshows", [])
        elif e.category == Category.DOCUMENTARY:
            ignore_list = ignore_keywords.get("documentaries", [])
        
        if any(word.lower() in e.raw_title.lower() for word in ignore_list):
            excluded.append(e)
            stats["ignored"] += 1
            logging.debug(f"Ignored by keyword: {e.raw_title}")
            continue
        
        # Allow all content that passes keyword filtering
        allowed.append(e)
        if e.category == Category.MOVIE:
            stats["movies_allowed"] += 1
        elif e.category == Category.TVSHOW:
            stats["tv_allowed"] += 1
        elif e.category == Category.DOCUMENTARY:
            stats["docs_allowed"] += 1

    logging.info("Filter statistics:")
    logging.info(
        f"  Movies: {stats['movies_allowed']} allowed, {stats['movies_excluded']} excluded"
    )
    logging.info(
        f"  TV Shows: {stats['tv_allowed']} allowed, {stats['tv_excluded']} excluded"
    )
    logging.info(
        f"  Documentaries: {stats['docs_allowed']} allowed, {stats['docs_excluded']} excluded"
    )
    logging.info(f"  Ignored by keywords: {stats['ignored']}")
    logging.info(f"  Total: {len(allowed)} allowed, {len(excluded)} excluded")
    return allowed, excluded
