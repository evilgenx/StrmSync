"""
Advanced Library Management System for StrmSync

This module provides comprehensive library management features including:
- Content Quality Scoring
- Stream Health Monitoring  
- Automatic Stream Replacement
- Library Analytics
"""

import asyncio
import logging
import sqlite3
import time
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import requests
import config
from core import SQLiteCache, KeyGenerator
from m3u_utils import Category, VODEntry


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning" 
    BROKEN = "broken"
    UNKNOWN = "unknown"


class StreamQuality:
    """Quality scoring for streams based on multiple metrics"""
    
    def __init__(self, config: config.Config):
        self.config = config
        self.weights = {
            'resolution': getattr(config, 'resolution_weight', 0.4),
            'uptime': getattr(config, 'uptime_weight', 0.3),
            'response_time': getattr(config, 'response_time_weight', 0.2),
            'error_rate': getattr(config, 'error_rate_weight', 0.1)
        }
    
    def calculate_score(self, health_data: 'StreamHealth') -> float:
        """Calculate overall quality score (0-10) for a stream"""
        resolution_score = self._get_resolution_score(health_data.resolution)
        uptime_score = self._get_uptime_score(health_data.success_rate)
        response_score = self._get_response_score(health_data.response_time)
        error_score = self._get_error_score(health_data.error_rate)
        
        # Weighted average
        total_score = (
            resolution_score * self.weights['resolution'] +
            uptime_score * self.weights['uptime'] +
            response_score * self.weights['response_time'] +
            error_score * self.weights['error_rate']
        )
        
        return round(min(10.0, max(0.0, total_score)), 2)
    
    def _get_resolution_score(self, resolution: Optional[str]) -> float:
        """Score based on video resolution"""
        if not resolution:
            return 3.0
        
        resolution = resolution.lower()
        if '4k' in resolution or '2160' in resolution:
            return 10.0
        elif '1080' in resolution or 'fullhd' in resolution:
            return 7.0
        elif '720' in resolution or 'hd' in resolution:
            return 5.0
        elif '480' in resolution or 'sd' in resolution:
            return 3.0
        else:
            return 4.0
    
    def _get_uptime_score(self, success_rate: float) -> float:
        """Score based on uptime percentage"""
        if success_rate >= 0.95:
            return 10.0
        elif success_rate >= 0.85:
            return 7.0
        elif success_rate >= 0.70:
            return 5.0
        elif success_rate >= 0.50:
            return 3.0
        else:
            return 1.0
    
    def _get_response_score(self, response_time: float) -> float:
        """Score based on response time"""
        if response_time < 1.0:
            return 10.0
        elif response_time < 3.0:
            return 7.0
        elif response_time < 5.0:
            return 5.0
        elif response_time < 10.0:
            return 3.0
        else:
            return 1.0
    
    def _get_error_score(self, error_rate: float) -> float:
        """Score based on error rate (inverse)"""
        return 10.0 - (error_rate * 10.0)


@dataclass
class StreamHealth:
    """Health status and metrics for a stream"""
    strm_key: str
    status: HealthStatus
    response_time: float
    last_tested: datetime
    success_count: int = 0
    error_count: int = 0
    resolution: Optional[str] = None
    quality_score: float = 0.0
    error_message: Optional[str] = None
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage"""
        total = self.success_count + self.error_count
        if total == 0:
            return 0.0
        return self.success_count / total
    
    @property
    def error_rate(self) -> float:
        """Calculate error rate as percentage"""
        total = self.success_count + self.error_count
        if total == 0:
            return 0.0
        return self.error_count / total


class StreamHealthMonitor:
    """Monitor and track stream health over time"""
    
    def __init__(self, config: config.Config, cache: SQLiteCache):
        self.config = config
        self.cache = cache
        self.session = requests.Session()
        self.session.timeout = getattr(config, 'health_check_timeout', 10)
        self.ensure_tables()
    
    def ensure_tables(self):
        """Create necessary database tables for health monitoring"""
        self.cache.conn.execute("""
            CREATE TABLE IF NOT EXISTS stream_health (
                id INTEGER PRIMARY KEY,
                strm_key TEXT UNIQUE,
                status TEXT,
                response_time REAL,
                last_tested TIMESTAMP,
                success_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                resolution TEXT,
                quality_score REAL DEFAULT 0.0,
                error_message TEXT,
                FOREIGN KEY (strm_key) REFERENCES strm_cache(key)
            )
        """)
        
        self.cache.conn.execute("""
            CREATE TABLE IF NOT EXISTS library_analytics (
                id INTEGER PRIMARY KEY,
                metric_name TEXT,
                metric_value REAL,
                recorded_at TIMESTAMP,
                metadata TEXT DEFAULT '{}'
            )
        """)
        
        self.cache.conn.commit()
    
    async def check_stream_health(self, strm_key: str, url: str) -> StreamHealth:
        """Perform comprehensive health check on a stream"""
        start_time = time.time()
        
        try:
            # Make test request
            response = await self._make_test_request(url)
            response_time = time.time() - start_time
            
            # Determine health status
            if response.status_code == 200:
                status = HealthStatus.HEALTHY
                success_count = 1
                error_count = 0
                error_message = None
            else:
                status = HealthStatus.WARNING
                success_count = 0
                error_count = 1
                error_message = f"HTTP {response.status_code}"
            
            # Extract resolution from headers if available
            resolution = self._extract_resolution(response.headers)
            
            health = StreamHealth(
                strm_key=strm_key,
                status=status,
                response_time=response_time,
                last_tested=datetime.now(),
                success_count=success_count,
                error_count=error_count,
                resolution=resolution,
                error_message=error_message
            )
            
            # Calculate quality score
            scorer = StreamQuality(self.config)
            health.quality_score = scorer.calculate_score(health)
            
            # Save to database
            self._save_health_data(health)
            
            logging.info(f"Health check for {strm_key}: {status.value}, score: {health.quality_score}")
            return health
            
        except Exception as e:
            response_time = time.time() - start_time
            
            health = StreamHealth(
                strm_key=strm_key,
                status=HealthStatus.BROKEN,
                response_time=response_time,
                last_tested=datetime.now(),
                success_count=0,
                error_count=1,
                error_message=str(e)
            )
            
            # Save to database
            self._save_health_data(health)
            
            logging.warning(f"Health check failed for {strm_key}: {e}")
            return health
    
    async def _make_test_request(self, url: str) -> requests.Response:
        """Make a test request to check stream availability"""
        # Use HEAD request first for faster checking
        try:
            response = self.session.head(url, allow_redirects=True)
            if response.status_code in [200, 206]:  # 206 = Partial Content
                return response
        except requests.RequestException:
            pass
        
        # Fall back to GET request with limited data
        response = self.session.get(url, stream=True, timeout=5)
        # Read only the first chunk to verify stream is working
        next(response.iter_content(1024), None)
        return response
    
    def _extract_resolution(self, headers: Dict[str, str]) -> Optional[str]:
        """Extract resolution information from response headers"""
        # Common header patterns for resolution
        content_type = headers.get('Content-Type', '').lower()
        content_length = headers.get('Content-Length')
        
        if 'video' in content_type:
            # Try to determine resolution from content length (rough estimate)
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > 1000:  # Large file likely 4K
                    return "4K"
                elif size_mb > 500:  # Medium file likely 1080p
                    return "1080p"
                elif size_mb > 200:  # Smaller file likely 720p
                    return "720p"
                else:
                    return "SD"
        
        return None
    
    def _save_health_data(self, health: StreamHealth):
        """Save health data to database"""
        self.cache.conn.execute("""
            INSERT OR REPLACE INTO stream_health 
            (strm_key, status, response_time, last_tested, success_count, error_count, resolution, quality_score, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            health.strm_key,
            health.status.value,
            health.response_time,
            health.last_tested,
            health.success_count,
            health.error_count,
            health.resolution,
            health.quality_score,
            health.error_message
        ))
        self.cache.conn.commit()
    
    def get_health_status(self, strm_key: str) -> Optional[StreamHealth]:
        """Get current health status for a stream"""
        cursor = self.cache.conn.execute("""
            SELECT strm_key, status, response_time, last_tested, success_count, error_count, resolution, quality_score, error_message
            FROM stream_health WHERE strm_key = ?
        """, (strm_key,))
        
        row = cursor.fetchone()
        if not row:
            return None
        
        return StreamHealth(
            strm_key=row[0],
            status=HealthStatus(row[1]),
            response_time=row[2],
            last_tested=datetime.fromisoformat(row[3]),
            success_count=row[4],
            error_count=row[5],
            resolution=row[6],
            quality_score=row[7],
            error_message=row[8]
        )
    
    def get_library_health_summary(self) -> Dict[str, Any]:
        """Get overall library health statistics"""
        cursor = self.cache.conn.execute("""
            SELECT 
                COUNT(*) as total_streams,
                SUM(CASE WHEN status = 'healthy' THEN 1 ELSE 0 END) as healthy,
                SUM(CASE WHEN status = 'warning' THEN 1 ELSE 0 END) as warning,
                SUM(CASE WHEN status = 'broken' THEN 1 ELSE 0 END) as broken,
                AVG(quality_score) as avg_quality
            FROM stream_health
        """)
        
        row = cursor.fetchone()
        if not row:
            return {
                'total_streams': 0,
                'healthy': 0,
                'warning': 0,
                'broken': 0,
                'avg_quality': 0.0,
                'health_percentage': 0.0
            }
        
        total = row[0] or 0
        healthy = row[1] or 0
        
        return {
            'total_streams': total,
            'healthy': healthy,
            'warning': row[2] or 0,
            'broken': row[3] or 0,
            'avg_quality': round(row[4] or 0.0, 2),
            'health_percentage': round((healthy / total * 100) if total > 0 else 0, 1)
        }
    
    def get_low_quality_streams(self, threshold: float = 5.0) -> List[StreamHealth]:
        """Get streams with quality scores below threshold"""
        cursor = self.cache.conn.execute("""
            SELECT strm_key, status, response_time, last_tested, success_count, error_count, resolution, quality_score, error_message
            FROM stream_health WHERE quality_score < ? ORDER BY quality_score ASC
        """, (threshold,))
        
        streams = []
        for row in cursor.fetchall():
            streams.append(StreamHealth(
                strm_key=row[0],
                status=HealthStatus(row[1]),
                response_time=row[2],
                last_tested=datetime.fromisoformat(row[3]),
                success_count=row[4],
                error_count=row[5],
                resolution=row[6],
                quality_score=row[7],
                error_message=row[8]
            ))
        
        return streams


class StreamReplacer:
    """Handle automatic stream replacement when streams fail"""
    
    def __init__(self, config: config.Config, cache: SQLiteCache, health_monitor: StreamHealthMonitor):
        self.config = config
        self.cache = cache
        self.health_monitor = health_monitor
        self.min_quality_threshold = getattr(config, 'min_quality_threshold', 5.0)
        self.max_replacement_attempts = getattr(config, 'max_replacement_attempts', 3)
    
    async def replace_broken_stream(self, strm_key: str, current_url: str) -> Optional[str]:
        """Attempt to find a replacement for a broken stream"""
        logging.info(f"Attempting to replace broken stream: {strm_key}")
        
        # Get the original entry details
        original_entry = self._get_entry_from_key(strm_key)
        if not original_entry:
            logging.warning(f"Could not find original entry for key: {strm_key}")
            return None
        
        # Search for alternative streams
        alternatives = await self._find_alternatives(original_entry)
        
        if not alternatives:
            logging.warning(f"No alternatives found for: {strm_key}")
            return None
        
        # Test alternatives and find the best one
        best_alternative = None
        best_score = 0
        
        for alt_entry in alternatives:
            # Check if this alternative meets quality threshold
            health = await self.health_monitor.check_stream_health(
                KeyGenerator.generate_key(alt_entry),
                alt_entry.url
            )
            
            if health.quality_score >= self.min_quality_threshold and health.quality_score > best_score:
                best_alternative = alt_entry
                best_score = health.quality_score
        
        if best_alternative:
            logging.info(f"Found replacement for {strm_key}: {best_score}")
            return best_alternative.url
        
        logging.warning(f"No suitable replacement found for: {strm_key}")
        return None
    
    def _get_entry_from_key(self, strm_key: str) -> Optional[VODEntry]:
        """Reconstruct VODEntry from cache key"""
        # This is a simplified version - in practice, you'd need to store more metadata
        # For now, return None and let the caller handle it
        return None
    
    async def _find_alternatives(self, entry: VODEntry) -> List[VODEntry]:
        """Find alternative streams for the same content"""
        # This would need to be implemented based on your M3U source
        # For now, return empty list
        return []


class LibraryAnalytics:
    """Collect and analyze library usage statistics"""
    
    def __init__(self, config: config.Config, cache: SQLiteCache):
        self.config = config
        self.cache = cache
    
    def record_metric(self, metric_name: str, value: float, metadata: Dict[str, Any] = None):
        """Record a new metric"""
        metadata_str = json.dumps(metadata or {})
        
        self.cache.conn.execute("""
            INSERT INTO library_analytics (metric_name, metric_value, recorded_at, metadata)
            VALUES (?, ?, ?, ?)
        """, (metric_name, value, datetime.now(), metadata_str))
        
        self.cache.conn.commit()
    
    def get_quality_distribution(self) -> Dict[str, int]:
        """Get distribution of stream quality scores"""
        cursor = self.cache.conn.execute("""
            SELECT 
                CASE 
                    WHEN quality_score >= 8 THEN 'Excellent (8-10)'
                    WHEN quality_score >= 6 THEN 'Good (6-7.9)'
                    WHEN quality_score >= 4 THEN 'Fair (4-5.9)'
                    ELSE 'Poor (0-3.9)'
                END as quality_range,
                COUNT(*) as count
            FROM stream_health
            GROUP BY quality_range
            ORDER BY quality_range
        """)
        
        return dict(cursor.fetchall())
    
    def get_health_trends(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get health trends over time"""
        since = datetime.now() - timedelta(days=days)
        
        cursor = self.cache.conn.execute("""
            SELECT 
                DATE(last_tested) as date,
                AVG(quality_score) as avg_quality,
                SUM(CASE WHEN status = 'healthy' THEN 1 ELSE 0 END) as healthy,
                SUM(CASE WHEN status = 'broken' THEN 1 ELSE 0 END) as broken,
                COUNT(*) as total
            FROM stream_health
            WHERE last_tested >= ?
            GROUP BY DATE(last_tested)
            ORDER BY date
        """, (since,))
        
        trends = []
        for row in cursor.fetchall():
            trends.append({
                'date': row[0],
                'avg_quality': round(row[1] or 0.0, 2),
                'healthy': row[2] or 0,
                'broken': row[3] or 0,
                'total': row[4] or 0
            })
        
        return trends
    
    def get_content_gaps(self) -> Dict[str, Any]:
        """Analyze content gaps in the library"""
        # This would analyze what popular content is missing
        # For now, return basic statistics
        cursor = self.cache.conn.execute("""
            SELECT 
                COUNT(*) as total_streams,
                COUNT(DISTINCT resolution) as resolution_types,
                AVG(quality_score) as avg_quality
            FROM stream_health
        """)
        
        row = cursor.fetchone()
        return {
            'total_streams': row[0] or 0,
            'resolution_types': row[1] or 0,
            'avg_quality': round(row[2] or 0.0, 2)
        }


# Background task for periodic health checks
async def periodic_health_check(config: config.Config, cache: SQLiteCache):
    """Background task to periodically check stream health"""
    if not getattr(config, 'enable_health_monitoring', False):
        return
    
    health_monitor = StreamHealthMonitor(config, cache)
    interval = getattr(config, 'health_check_interval', 3600)  # Default 1 hour
    
    while True:
        try:
            # Get all STRM entries from cache
            strm_cache = cache.strm_cache_dict()
            
            # Filter to only allowed streams with URLs
            allowed_streams = [
                (strm_key, entry_data['url'])
                for strm_key, entry_data in strm_cache.items()
                if entry_data.get('allowed') == 1 and entry_data.get('url')
            ]
            
            if not allowed_streams:
                logging.info("No streams to check")
                continue
            
            # Determine which streams to test based on sampling mode
            streams_to_test = select_streams_for_testing(
                allowed_streams,
                config.health_check_mode,
                config.health_check_sample_size,
                config.health_check_sample_percentage
            )
            
            logging.info(f"Testing {len(streams_to_test)} out of {len(allowed_streams)} streams")
            
            # Check health of selected streams
            for strm_key, url in streams_to_test:
                await health_monitor.check_stream_health(strm_key, url)
            
            logging.info(f"Completed periodic health check: tested {len(streams_to_test)} streams")
            
        except Exception as e:
            logging.error(f"Error in periodic health check: {e}")
        
        # Wait for next interval
        await asyncio.sleep(interval)


def select_streams_for_testing(
    streams: List[Tuple[str, str]],
    mode: str,
    sample_size: int,
    sample_percentage: float
) -> List[Tuple[str, str]]:
    """
    Select streams for health testing based on sampling mode
    
    Args:
        streams: List of (strm_key, url) tuples
        mode: Sampling mode ('all', 'random', 'percentage')
        sample_size: Number of streams to test (for 'random' mode)
        sample_percentage: Percentage of streams to test (for 'percentage' mode, 0.0-1.0)
    
    Returns:
        List of selected (strm_key, url) tuples
    """
    total_streams = len(streams)
    
    if mode == "all":
        return streams
    
    elif mode == "random":
        # Select random sample, ensuring we don't exceed total
        sample_size = min(sample_size, total_streams)
        return random.sample(streams, sample_size)
    
    elif mode == "percentage":
        # Select percentage of total streams
        if sample_percentage <= 0.0 or sample_percentage > 1.0:
            logging.warning(f"Invalid sample percentage: {sample_percentage}. Using default of 10%.")
            sample_percentage = 0.1
        
        sample_size = max(1, int(total_streams * sample_percentage))
        sample_size = min(sample_size, total_streams)
        return random.sample(streams, sample_size)
    
    else:
        logging.warning(f"Unknown health check mode: {mode}. Defaulting to 'random'.")
        sample_size = min(sample_size, total_streams)
        return random.sample(streams, sample_size)
