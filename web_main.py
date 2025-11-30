#!/usr/bin/env python3
"""
StrmSync Web Dashboard & API
FastAPI-based web interface for managing M3U to STRM conversion
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Import existing application modules
import config
from core import SQLiteCache, build_existing_media_cache, KeyGenerator
from m3u_utils import parse_m3u, split_by_market_filter, Category, VODEntry
from strm_utils import write_strm_file, cleanup_strm_tree, movie_strm_path, tv_strm_path, doc_strm_path
from url_utils import get_m3u_path
from main import refresh_media_server, write_excluded_report
from library_management import (
    StreamHealthMonitor, 
    StreamQuality, 
    LibraryAnalytics,
    HealthStatus,
    StreamHealth
)
from live_tv_utils import LiveTVProcessor, ChannelEditor, Channel, ChannelGroup


# Pydantic models for API
class StatusResponse(BaseModel):
    status: str
    version: str
    uptime: float
    active_jobs: int
    total_jobs: int


class JobRequest(BaseModel):
    config_path: Optional[str] = None
    dry_run: bool = False


class JobStatus(BaseModel):
    job_id: str
    status: str  # running, completed, failed
    start_time: float
    end_time: Optional[float] = None
    progress: float = 0.0
    current_step: str = ""
    logs: List[str] = []
    error: Optional[str] = None


class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: Any


# Global state
app = FastAPI(
    title="StrmSync Dashboard",
    description="StrmSync web interface for M3U to STRM conversion",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
active_jobs: Dict[str, JobStatus] = {}
job_counter = 0
start_time = time.time()

# WebSocket connections
websocket_connections: List[WebSocket] = []

# Setup templates
templates = Jinja2Templates(directory="templates")


async def broadcast_message(message: str, message_type: str = "log"):
    """Broadcast message to all WebSocket connections"""
    data = {"type": message_type, "message": message, "timestamp": time.time()}
    disconnected = []
    for connection in websocket_connections:
        try:
            await connection.send_json(data)
        except Exception:
            disconnected.append(connection)
    
    for connection in disconnected:
        websocket_connections.remove(connection)


async def broadcast_job_update(job: JobStatus):
    """Broadcast job update to all WebSocket connections"""
    data = {"type": "job_update", "message": job.dict()}
    disconnected = []
    for connection in websocket_connections:
        try:
            await connection.send_json(data)
        except Exception:
            disconnected.append(connection)
    
    for connection in disconnected:
        websocket_connections.remove(connection)


class JobManager:
    """Manages background processing jobs"""
    
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
    
    async def run_pipeline_job(self, job_id: str, config_path: Optional[str] = None, dry_run: bool = False):
        """Run the M3U processing pipeline as a background job"""
        job = active_jobs[job_id]
        
        try:
            # Get the current event loop
            loop = asyncio.get_running_loop()
            
            # Load configuration
            if config_path:
                cfg = config.load_config(Path(config_path))
            else:
                cfg = config.load_config(Path(__file__).parent / "config.ini")
            
            # Update job status
            job.status = "running"
            job.current_step = "Loading configuration"
            await broadcast_message("Job started: Loading configuration")
            
            # Setup logging to capture to job logs
            class JobLogHandler(logging.Handler):
                def __init__(self, job_id, loop):
                    super().__init__()
                    self.job_id = job_id
                    self.loop = loop
                
                def emit(self, record):
                    if self.job_id in active_jobs:
                        log_entry = self.format(record)
                        active_jobs[self.job_id].logs.append(log_entry)
                        asyncio.run_coroutine_threadsafe(broadcast_message(log_entry, "log"), self.loop)
            
            job_handler = JobLogHandler(job_id, loop)
            job_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(job_handler)
            
            try:
                # Run the pipeline (this is the existing main.run_pipeline logic)
                await self._run_pipeline_logic(cfg, job, dry_run, loop)
                
                job.status = "completed"
                job.end_time = time.time()
                await broadcast_message(f"Job completed successfully in {job.end_time - job.start_time:.1f}s")
                
            finally:
                logging.getLogger().removeHandler(job_handler)
                
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.end_time = time.time()
            error_msg = f"Job failed: {str(e)}"
            job.logs.append(error_msg)
            await broadcast_message(error_msg, "error")
            logging.error(f"Job {job_id} failed: {e}", exc_info=True)
    
    async def _run_pipeline_logic(self, cfg, job: JobStatus, dry_run: bool, loop):
        """Core pipeline logic adapted from main.py with progress tracking"""
        total_steps = 9
        current_step = 0
        
        def update_progress(step_name: str, step_weight: int = 1):
            nonlocal current_step
            current_step += step_weight
            job.current_step = step_name
            job.progress = min(95, int((current_step / total_steps) * 100))
            asyncio.run_coroutine_threadsafe(broadcast_job_update(job), loop)
        
        # Handle M3U source
        update_progress("Processing M3U source", 1)
        await broadcast_message(f"Processing M3U from: {cfg.m3u}")
        m3u_path = get_m3u_path(cfg.m3u)
        
        # Initialize cache and existing media
        update_progress("Building media cache", 1)
        await broadcast_message("Building existing media cache...")
        cache = SQLiteCache(cfg.sqlite_cache_file)
        existing = {}
        for d in cfg.existing_media_dirs:
            existing.update(build_existing_media_cache(Path(d)))
        cache.replace_existing_media(existing)
        existing_keys = set(existing.keys())
        
        # Parse M3U
        update_progress("Parsing M3U playlist", 1)
        await broadcast_message("Parsing M3U playlist...")
        entries = parse_m3u(
            m3u_path,
            tv_keywords=cfg.tv_group_keywords,
            doc_keywords=cfg.doc_group_keywords,
            movie_keywords=cfg.movie_group_keywords,
            replay_keywords=cfg.replay_group_keywords,
            ignore_keywords=cfg.ignore_keywords,
        )
        
        # Filter out live TV
        original_count = len(entries)
        entries = [entry for entry in entries if entry.category != Category.REPLAY]
        replay_count = original_count - len(entries)
        await broadcast_message(f"Filtered out {replay_count} REPLAY entries, keeping {len(entries)} VOD entries")
        
        # Deduplicate
        update_progress("Deduplicating entries", 1)
        unique_entries = {}
        for e in entries:
            key = KeyGenerator.generate_key(e)
            unique_entries[key] = e
        entries = list(unique_entries.values())
        await broadcast_message(f"Deduplicated: {len(entries)} -> {len(unique_entries)} unique entries")
        
        # Check cache
        update_progress("Checking cache", 1)
        await broadcast_message("Checking cache for existing entries...")
        strm_cache = cache.strm_cache_dict()
        to_check = []
        reused_allowed = []
        reused_excluded = []
        
        for e in entries:
            key = KeyGenerator.generate_key(e)
            if key in existing_keys:
                reused_allowed.append(e)
                continue
            cached = strm_cache.get(key)
            if cached and cached.get("allowed") is not None:
                if cached["allowed"] == 1:
                    reused_allowed.append(e)
                else:
                    reused_excluded.append(e)
            else:
                to_check.append(e)
        
        # Filter by market
        update_progress("Filtering by country", 1)
        await broadcast_message(f"Filtering {len(to_check)} entries by country...")
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
        
        # Write excluded report
        if not dry_run:
            write_excluded_report(cfg.output_dir / "excluded_entries.txt", excluded, len(allowed), cfg.write_non_us_report)
        
        # Process entries
        update_progress("Creating STRM files", 1)
        await broadcast_message(f"Processing {len(allowed)} allowed entries...")
        
        existing_keys = set(existing.keys())
        strm_cache = cache.strm_cache_dict()
        new_cache = strm_cache.copy()
        written_count = 0
        skipped_count = 0
        
        # Track progress during file processing
        total_entries = len(allowed)
        processed_entries = 0
        
        def process_entry(e):
            nonlocal written_count, skipped_count, processed_entries
            try:
                key = KeyGenerator.generate_key(e)
                
                if e.category == Category.MOVIE:
                    rel_path = movie_strm_path(cfg.output_dir, e)
                elif e.category == Category.TVSHOW:
                    import re
                    base = re.sub(r"[sS]\d{1,2}\s*[eE]\d{1,2}.*", "", e.raw_title).strip()
                    m = re.search(r"[sS](\d{1,2})\s*[eE](\d{1,2})", e.raw_title)
                    if m:
                        season, episode = int(m.group(1)), int(m.group(2))
                        rel_path = tv_strm_path(
                            cfg.output_dir,
                            VODEntry(
                                raw_title=base,
                                safe_title=e.safe_title,
                                url=e.url,
                                category=e.category,
                                year=e.year,
                            ),
                            season,
                            episode,
                        )
                    else:
                        rel_path = tv_strm_path(cfg.output_dir, e, 1, 1)
                elif e.category == Category.DOCUMENTARY:
                    rel_path = doc_strm_path(cfg.output_dir, e)
                else:
                    return
                
                abs_path = cfg.output_dir / rel_path
                url = e.url
                
                if key in existing_keys:
                    skipped_count += 1
                    new_cache[key] = {"url": e.url, "path": None, "allowed": 1}
                    return
                
                cached = strm_cache.get(key)
                if cached:
                    cached_path = Path(cached.get("path") or "").resolve() if cached.get("path") else None
                    if cached.get("url") == url and cached.get("path") and cached_path == abs_path.resolve():
                        skipped_count += 1
                        new_cache[key] = {
                            "url": cached.get("url"),
                            "path": cached.get("path"),
                            "allowed": cached.get("allowed", 1),
                        }
                        return
                
                if not dry_run:
                    write_strm_file(cfg.output_dir, rel_path, url)
                    new_cache[key] = {"url": url, "path": str(abs_path.resolve()), "allowed": 1}
                    written_count += 1
                else:
                    # In dry run, count as would-be written
                    written_count += 1
                    
            except Exception as ex:
                logging.error(f"Error processing entry {e.raw_title}: {ex}")
            finally:
                processed_entries += 1
                # Update progress during processing (last 30% of total progress)
                if total_entries > 0:
                    file_progress = int((processed_entries / total_entries) * 30)
                    job.progress = min(95, 65 + file_progress)
                    asyncio.run_coroutine_threadsafe(broadcast_job_update(job), loop)
        
        # Process entries in parallel
        with ThreadPoolExecutor(max_workers=cfg.max_workers) as executor:
            list(executor.map(process_entry, allowed))
        
        # Update cache for excluded entries
        for e in excluded:
            key = KeyGenerator.generate_key(e)
            new_cache[key] = {"url": e.url, "path": None, "allowed": 0}
        
        if not dry_run:
            update_progress("Cleaning up orphan STRMs", 1)
            await broadcast_message("Cleaning up orphan STRMs...")
            cleanup_strm_tree(cfg.output_dir, new_cache)
        
        # Refresh media servers
        if not dry_run:
            if getattr(cfg, "emby_api_url", None) and getattr(cfg, "emby_api_key", None):
                update_progress("Refreshing media server", 1)
                await broadcast_message("Triggering Emby library refresh...")
                refresh_media_server(cfg.emby_api_url, cfg.emby_api_key, "emby")
            elif getattr(cfg, "jellyfin_api_url", None) and getattr(cfg, "jellyfin_api_key", None):
                update_progress("Refreshing media server", 1)
                await broadcast_message("Triggering Jellyfin library refresh...")
                refresh_media_server(cfg.jellyfin_api_url, cfg.jellyfin_api_key, "jellyfin")
            else:
                await broadcast_message("Skipping media server refresh (not configured)")
        else:
            await broadcast_message("Skipping media server refresh (dry_run mode)")
        
        # Final completion
        job.progress = 100
        job.current_step = "Completed"
        asyncio.run_coroutine_threadsafe(broadcast_job_update(job), loop)
        
        summary = f"Process complete: {written_count} STRMs {'would be ' if dry_run else ''}written, {skipped_count} skipped, {len(excluded)} excluded"
        await broadcast_message(summary)
        job.logs.append(summary)


job_manager = JobManager()


# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the dashboard HTML"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/v1/status")
async def get_status() -> StatusResponse:
    """Get application status"""
    return StatusResponse(
        status="running",
        version="1.0.0",
        uptime=time.time() - start_time,
        active_jobs=len([j for j in active_jobs.values() if j.status == "running"]),
        total_jobs=len(active_jobs)
    )


@app.post("/api/v1/jobs/start")
async def start_job(request: JobRequest, background_tasks: BackgroundTasks) -> JobStatus:
    """Start a new processing job"""
    global job_counter
    
    job_id = f"job_{job_counter}"
    job_counter += 1
    
    job = JobStatus(
        job_id=job_id,
        status="queued",
        start_time=time.time(),
        current_step="Initializing"
    )
    
    active_jobs[job_id] = job
    
    # Run the job in background
    background_tasks.add_task(
        job_manager.run_pipeline_job,
        job_id,
        request.config_path,
        request.dry_run
    )
    
    return job


@app.get("/api/v1/jobs")
async def list_jobs() -> List[JobStatus]:
    """List all jobs"""
    return list(active_jobs.values())


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str) -> JobStatus:
    """Get job status"""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return active_jobs[job_id]


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    await websocket.accept()
    websocket_connections.append(websocket)
    
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        websocket_connections.remove(websocket)


# Advanced Library Management API Endpoints
@app.get("/api/v1/library/health")
async def get_library_health():
    """Get overall library health statistics"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    health_monitor = StreamHealthMonitor(cfg, cache)
    
    health_summary = health_monitor.get_library_health_summary()
    return health_summary


@app.get("/api/v1/library/health/streams")
async def get_low_quality_streams(threshold: float = 5.0):
    """Get streams with quality scores below threshold"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    health_monitor = StreamHealthMonitor(cfg, cache)
    
    streams = health_monitor.get_low_quality_streams(threshold)
    
    # Convert to serializable format
    result = []
    for stream in streams:
        result.append({
            'strm_key': stream.strm_key,
            'status': stream.status.value,
            'response_time': stream.response_time,
            'last_tested': stream.last_tested.isoformat(),
            'success_count': stream.success_count,
            'error_count': stream.error_count,
            'resolution': stream.resolution,
            'quality_score': stream.quality_score,
            'error_message': stream.error_message,
            'success_rate': stream.success_rate,
            'error_rate': stream.error_rate
        })
    
    return result


@app.get("/api/v1/library/analytics/quality-distribution")
async def get_quality_distribution():
    """Get distribution of stream quality scores"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    analytics = LibraryAnalytics(cfg, cache)
    
    distribution = analytics.get_quality_distribution()
    return distribution


@app.get("/api/v1/library/analytics/health-trends")
async def get_health_trends(days: int = 30):
    """Get health trends over time"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    analytics = LibraryAnalytics(cfg, cache)
    
    trends = analytics.get_health_trends(days)
    return trends


@app.get("/api/v1/library/analytics/content-gaps")
async def get_content_gaps():
    """Get content gap analysis"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    analytics = LibraryAnalytics(cfg, cache)
    
    gaps = analytics.get_content_gaps()
    return gaps


@app.post("/api/v1/library/health/check/{strm_key}")
async def check_stream_health(strm_key: str):
    """Manually check health of a specific stream"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    health_monitor = StreamHealthMonitor(cfg, cache)
    
    # Get the URL from cache
    strm_cache = cache.strm_cache_dict()
    if strm_key not in strm_cache:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    entry_data = strm_cache[strm_key]
    if not entry_data.get('url'):
        raise HTTPException(status_code=400, detail="No URL found for stream")
    
    # Perform health check
    health = await health_monitor.check_stream_health(strm_key, entry_data['url'])
    
    return {
        'strm_key': health.strm_key,
        'status': health.status.value,
        'response_time': health.response_time,
        'last_tested': health.last_tested.isoformat(),
        'success_count': health.success_count,
        'error_count': health.error_count,
        'resolution': health.resolution,
        'quality_score': health.quality_score,
        'error_message': health.error_message,
        'success_rate': health.success_rate,
        'error_rate': health.error_rate
    }


@app.get("/api/v1/library/streams")
async def get_all_streams():
    """Get all streams with their health and quality information"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    cache = SQLiteCache(cfg.sqlite_cache_file)
    health_monitor = StreamHealthMonitor(cfg, cache)
    
    # Get all STRM entries
    strm_cache = cache.strm_cache_dict()
    
    streams = []
    for strm_key, entry_data in strm_cache.items():
        if entry_data.get('allowed') == 1:
            health = health_monitor.get_health_status(strm_key)
            
            stream_info = {
                'strm_key': strm_key,
                'url': entry_data.get('url'),
                'path': entry_data.get('path'),
                'allowed': entry_data.get('allowed')
            }
            
            if health:
                stream_info.update({
                    'status': health.status.value,
                    'response_time': health.response_time,
                    'last_tested': health.last_tested.isoformat(),
                    'success_count': health.success_count,
                    'error_count': health.error_count,
                    'resolution': health.resolution,
                    'quality_score': health.quality_score,
                    'error_message': health.error_message,
                    'success_rate': health.success_rate,
                    'error_rate': health.error_rate
                })
            else:
                stream_info.update({
                    'status': 'unknown',
                    'response_time': 0,
                    'last_tested': None,
                    'success_count': 0,
                    'error_count': 0,
                    'resolution': None,
                    'quality_score': 0,
                    'error_message': None,
                    'success_rate': 0,
                    'error_rate': 0
                })
            
            streams.append(stream_info)
    
    return streams


# Live TV API Endpoints
@app.get("/api/v1/live-tv/status")
async def get_live_tv_status():
    """Get live TV processing status"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"enabled": False, "message": "Live TV is disabled"}
    
    processor = LiveTVProcessor(cfg)
    
    # Parse M3U for live TV channels
    m3u_path = get_m3u_path(cfg.m3u)
    channels = processor.parse_m3u_for_live_tv(m3u_path)
    groups = processor.group_channels()
    
    # Load EPG if configured
    epg_data = {}
    if cfg.epg_url:
        epg_data = processor.load_epg_data()
    
    return {
        "enabled": True,
        "channels_found": len(channels),
        "groups_found": len(groups),
        "epg_channels": len(epg_data),
        "groups": {name: len(group.channels) for name, group in groups.items()}
    }


@app.get("/api/v1/live-tv/channels")
async def get_live_tv_channels():
    """Get all live TV channels"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"error": "Live TV is disabled"}
    
    processor = LiveTVProcessor(cfg)
    m3u_path = get_m3u_path(cfg.m3u)
    channels = processor.parse_m3u_for_live_tv(m3u_path)
    groups = processor.group_channels()
    
    # Convert to serializable format
    result = []
    for group_name, group in groups.items():
        for channel in group.channels:
            result.append({
                'name': channel.name,
                'safe_name': channel.safe_name,
                'url': channel.url,
                'group': channel.group,
                'logo': channel.logo,
                'epg_id': channel.epg_id,
                'number': channel.number,
                'resolution': channel.resolution,
                'language': channel.language,
                'country': channel.country,
                'description': channel.description
            })
    
    return result


@app.get("/api/v1/live-tv/groups")
async def get_live_tv_groups():
    """Get all live TV channel groups"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"error": "Live TV is disabled"}
    
    processor = LiveTVProcessor(cfg)
    m3u_path = get_m3u_path(cfg.m3u)
    channels = processor.parse_m3u_for_live_tv(m3u_path)
    groups = processor.group_channels()
    
    result = {}
    for group_name, group in groups.items():
        result[group_name] = {
            'name': group.name,
            'channel_count': len(group.channels),
            'channels': [c.name for c in group.channels]
        }
    
    return result


@app.post("/api/v1/live-tv/process")
async def process_live_tv(background_tasks: BackgroundTasks):
    """Process live TV channels and generate STRM files"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"error": "Live TV is disabled"}
    
    # Create a job for live TV processing
    global job_counter
    job_id = f"live_tv_job_{job_counter}"
    job_counter += 1
    
    job = JobStatus(
        job_id=job_id,
        status="queued",
        start_time=time.time(),
        current_step="Initializing Live TV processing"
    )
    
    active_jobs[job_id] = job
    
    # Run live TV processing in background
    background_tasks.add_task(process_live_tv_job, job_id)
    
    return {"job_id": job_id, "message": "Live TV processing started"}


async def process_live_tv_job(job_id: str):
    """Background job to process live TV channels"""
    job = active_jobs[job_id]
    
    try:
        cfg = config.load_config(Path(__file__).parent / "config.ini")
        job.status = "running"
        await broadcast_message("Starting Live TV processing...")
        
        # Initialize processor
        processor = LiveTVProcessor(cfg)
        m3u_path = get_m3u_path(cfg.m3u)
        
        # Parse channels
        job.current_step = "Parsing M3U for live TV channels"
        await broadcast_message("Parsing M3U for live TV channels...")
        channels = processor.parse_m3u_for_live_tv(m3u_path)
        
        # Group channels
        job.current_step = "Grouping channels"
        await broadcast_message(f"Grouping {len(channels)} channels...")
        groups = processor.group_channels()
        
        # Load EPG if configured
        if cfg.epg_url:
            job.current_step = "Loading EPG data"
            await broadcast_message("Loading EPG data...")
            processor.load_epg_data()
        
        # Generate STRM files
        job.current_step = "Generating STRM files"
        await broadcast_message("Generating STRM files...")
        written_count = processor.generate_strm_files(cfg.dry_run)
        
        job.status = "completed"
        job.end_time = time.time()
        await broadcast_message(f"Live TV processing completed: {written_count} STRM files generated")
        
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.end_time = time.time()
        error_msg = f"Live TV processing failed: {str(e)}"
        job.logs.append(error_msg)
        await broadcast_message(error_msg, "error")
        logging.error(f"Live TV job {job_id} failed: {e}", exc_info=True)


@app.get("/api/v1/live-tv/epg")
async def get_epg_data():
    """Get EPG (Electronic Program Guide) data"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv or not cfg.epg_url:
        return {"error": "EPG is not configured"}
    
    processor = LiveTVProcessor(cfg)
    epg_data = processor.load_epg_data()
    
    # Convert to serializable format
    result = {}
    for channel_id, programs in epg_data.items():
        result[channel_id] = []
        for program in programs:
            result[channel_id].append({
                'channel_id': program.channel_id,
                'start': program.start,
                'stop': program.stop,
                'title': program.title,
                'description': program.description,
                'category': program.category,
                'episode_num': program.episode_num,
                'icon': program.icon
            })
    
    return result


@app.get("/api/v1/live-tv/stats")
async def get_live_tv_stats():
    """Get live TV statistics"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"error": "Live TV is disabled"}
    
    processor = LiveTVProcessor(cfg)
    m3u_path = get_m3u_path(cfg.m3u)
    channels = processor.parse_m3u_for_live_tv(m3u_path)
    groups = processor.group_channels()
    
    stats = processor.get_channel_stats()
    
    return stats


@app.post("/api/v1/live-tv/export/{format}")
async def export_live_tv_data(format: str):
    """Export live TV data in various formats"""
    cfg = config.load_config(Path(__file__).parent / "config.ini")
    
    if not cfg.enable_live_tv:
        return {"error": "Live TV is disabled"}
    
    processor = LiveTVProcessor(cfg)
    m3u_path = get_m3u_path(cfg.m3u)
    channels = processor.parse_m3u_for_live_tv(m3u_path)
    groups = processor.group_channels()
    
    try:
        data = processor.export_channel_list(format)
        return {"format": format, "data": data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Export failed: {str(e)}")


def main():
    """Main entry point for web server"""
    import argparse
    
    parser = argparse.ArgumentParser(description="StrmSync Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (development)")
    
    args = parser.parse_args()
    
    print(f"Starting StrmSync Web Dashboard on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop")
    
    uvicorn.run(
        "web_main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
