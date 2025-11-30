"""
API Utilities for rate limiting, batch processing, and enhanced caching.

This module provides utilities to optimize TMDb API calls and reduce the number
of requests made to improve performance.
"""

import asyncio
import logging
import time
import re
from typing import List, Dict, Tuple, Optional, Any
from difflib import SequenceMatcher
import requests
from core import SQLiteCache


class RateLimiter:
    """Rate limiter to control API request frequency."""
    
    def __init__(self, delay: float = 0.25):
        """
        Initialize rate limiter.
        
        Args:
            delay: Time to wait between requests in seconds
        """
        self.delay = delay
        self.last_request_time = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """Acquire permission to make an API request."""
        async with self._lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            
            if time_since_last < self.delay:
                wait_time = self.delay - time_since_last
                await asyncio.sleep(wait_time)
            
            self.last_request_time = time.time()


class BatchProcessor:
    """Handle batch processing of similar requests to reduce API calls."""
    
    def __init__(self, similarity_threshold: float = 0.85):
        """
        Initialize batch processor.
        
        Args:
            similarity_threshold: Threshold for considering titles similar (0-1)
        """
        self.similarity_threshold = similarity_threshold
    
    def group_similar_titles(self, titles: List[str]) -> Dict[str, List[str]]:
        """
        Group similar titles together to avoid duplicate API calls.
        
        Args:
            titles: List of titles to group
            
        Returns:
            Dictionary mapping representative titles to lists of similar titles
        """
        groups = {}
        processed = set()
        
        for title in titles:
            if title in processed:
                continue
            
            # Clean and normalize title for comparison
            clean_title = self._normalize_title(title)
            
            # Find similar titles
            similar = [title]
            processed.add(title)
            
            for other_title in titles:
                if other_title in processed:
                    continue
                
                if self._is_similar(clean_title, self._normalize_title(other_title)):
                    similar.append(other_title)
                    processed.add(other_title)
            
            # Use the longest title as the representative
            representative = max(similar, key=len)
            groups[representative] = similar
        
        return groups
    
    def _normalize_title(self, title: str) -> str:
        """Normalize title for comparison."""
        # Remove year, quality indicators, and special characters
        title = re.sub(r'\(\d{4}\)', '', title)
        title = re.sub(r'\s*-\s*\d{4}$', '', title)
        title = re.sub(r'\s*\(\d{3,4}p\)', '', title)
        title = re.sub(r'[^a-zA-Z0-9\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip().lower()
        return title
    
    def _is_similar(self, title1: str, title2: str) -> bool:
        """Check if two titles are similar."""
        similarity = SequenceMatcher(None, title1, title2).ratio()
        return similarity >= self.similarity_threshold


class EnhancedTMDbClient:
    """Enhanced TMDb client with rate limiting and caching."""
    
    def __init__(self, api_key: str, cache: SQLiteCache, rate_limiter: RateLimiter, 
                 cache_ttl_days: int = 7, max_retries: int = 5, backoff_factor: float = 2.0):
        """
        Initialize enhanced TMDb client.
        
        Args:
            api_key: TMDb API key
            cache: SQLite cache instance
            rate_limiter: Rate limiter instance
            cache_ttl_days: Cache TTL in days
            max_retries: Maximum retry attempts
            backoff_factor: Exponential backoff factor
        """
        self.api_key = api_key
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.cache_ttl_days = cache_ttl_days
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.base_url = "https://api.themoviedb.org/3"
    
    async def search_movie(self, title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Search for a movie with enhanced caching and rate limiting.
        
        Args:
            title: Movie title to search for
            year: Optional release year
            
        Returns:
            Movie search results or None if not found
        """
        await self.rate_limiter.acquire()
        
        # Check cache first
        cached = self.cache.get_tmdb_search_cache("movie", title, year, self.cache_ttl_days)
        if cached:
            logging.debug(f"TMDb cache hit for movie search: '{title}' ({year})")
            return cached
        
        # Make API request
        url = f"{self.base_url}/search/movie"
        params = {
            "api_key": self.api_key,
            "query": title.strip(),
            "language": "en-US"
        }
        if year:
            params["year"] = year
        
        data = await self._make_request(url, params)
        
        # Cache the result
        if data:
            self.cache.set_tmdb_search_cache("movie", title, year, data, self.cache_ttl_days)
        
        return data
    
    async def search_tv(self, title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Search for a TV show with enhanced caching and rate limiting.
        
        Args:
            title: TV show title to search for
            year: Optional first air date year
            
        Returns:
            TV show search results or None if not found
        """
        await self.rate_limiter.acquire()
        
        # Check cache first
        cached = self.cache.get_tmdb_search_cache("tv", title, year, self.cache_ttl_days)
        if cached:
            logging.debug(f"TMDb cache hit for TV search: '{title}' ({year})")
            return cached
        
        # Make API request
        url = f"{self.base_url}/search/tv"
        params = {
            "api_key": self.api_key,
            "query": title.strip(),
            "language": "en-US"
        }
        if year:
            params["first_air_date_year"] = year
        
        data = await self._make_request(url, params)
        
        # Cache the result
        if data:
            self.cache.set_tmdb_search_cache("tv", title, year, data, self.cache_ttl_days)
        
        return data
    
    async def get_movie_details(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """
        Get movie details with enhanced caching and rate limiting.
        
        Args:
            movie_id: TMDb movie ID
            
        Returns:
            Movie details or None if not found
        """
        await self.rate_limiter.acquire()
        
        # Check cache first
        cached = self.cache.get_tmdb_details_cache("movie", movie_id, self.cache_ttl_days)
        if cached:
            logging.debug(f"TMDb cache hit for movie details: {movie_id}")
            return cached
        
        # Make API request
        url = f"{self.base_url}/movie/{movie_id}"
        params = {"api_key": self.api_key}
        
        data = await self._make_request(url, params)
        
        # Cache the result
        if data:
            self.cache.set_tmdb_details_cache("movie", movie_id, data, self.cache_ttl_days)
        
        return data
    
    async def get_tv_details(self, tv_id: int) -> Optional[Dict[str, Any]]:
        """
        Get TV show details with enhanced caching and rate limiting.
        
        Args:
            tv_id: TMDb TV show ID
            
        Returns:
            TV show details or None if not found
        """
        await self.rate_limiter.acquire()
        
        # Check cache first
        cached = self.cache.get_tmdb_details_cache("tv", tv_id, self.cache_ttl_days)
        if cached:
            logging.debug(f"TMDb cache hit for TV details: {tv_id}")
            return cached
        
        # Make API request
        url = f"{self.base_url}/tv/{tv_id}"
        params = {"api_key": self.api_key}
        
        data = await self._make_request(url, params)
        
        # Cache the result
        if data:
            self.cache.set_tmdb_details_cache("tv", tv_id, data, self.cache_ttl_days)
        
        return data
    
    async def get_movie_release_dates(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """
        Get movie release dates with enhanced caching and rate limiting.
        
        Args:
            movie_id: TMDb movie ID
            
        Returns:
            Movie release dates or None if not found
        """
        await self.rate_limiter.acquire()
        
        # Check cache first
        cache_key = f"movie_release_dates_{movie_id}"
        cached = self.cache.get_tmdb_details_cache("movie_release_dates", movie_id, self.cache_ttl_days)
        if cached:
            logging.debug(f"TMDb cache hit for movie release dates: {movie_id}")
            return cached
        
        # Make API request
        url = f"{self.base_url}/movie/{movie_id}/release_dates"
        params = {"api_key": self.api_key}
        
        data = await self._make_request(url, params)
        
        # Cache the result
        if data:
            self.cache.set_tmdb_details_cache("movie_release_dates", movie_id, data, self.cache_ttl_days)
        
        return data
    
    async def _make_request(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Make an HTTP request with retry logic and error handling.
        
        Args:
            url: API endpoint URL
            params: Request parameters
            
        Returns:
            Response data or None if request failed
        """
        delay = 1.0
        
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, params=params, timeout=10)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Rate limited - wait and retry
                    logging.warning(f"Rate limited on attempt {attempt + 1}, waiting {delay}s...")
                    await asyncio.sleep(delay)
                    delay = min(delay * self.backoff_factor, 30)  # Cap at 30 seconds
                    continue
                else:
                    logging.error(f"API request failed with status {response.status_code}: {response.text}")
                    return None
                    
            except requests.RequestException as e:
                logging.error(f"Request failed on attempt {attempt + 1}: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * self.backoff_factor, 30)
                else:
                    return None
        
        return None


def calculate_optimal_batch_size(total_items: int, max_workers: int) -> int:
    """
    Calculate optimal batch size for processing.
    
    Args:
        total_items: Total number of items to process
        max_workers: Maximum number of worker threads
        
    Returns:
        Optimal batch size
    """
    if total_items <= max_workers:
        return 1
    
    # Aim for each worker to handle 5-10 items per batch
    base_batch_size = max(1, total_items // (max_workers * 5))
    return max(base_batch_size, 1)


def chunk_list(items: List[Any], chunk_size: int) -> List[List[Any]]:
    """
    Split a list into chunks of specified size.
    
    Args:
        items: List of items to chunk
        chunk_size: Size of each chunk
        
    Returns:
        List of chunks
    """
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
