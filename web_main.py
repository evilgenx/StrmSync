#!/usr/bin/env python3
"""
M3U2strm_jf Web Dashboard & API
FastAPI-based web interface for managing M3U to STRM conversion
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import existing application modules
import config
from core import SQLiteCache, build_existing_media_cache, KeyGenerator
from m3u_utils import parse_m3u, split_by_market_filter, Category, VODEntry
from strm_utils import write_strm_file, cleanup_strm_tree, movie_strm_path, tv_strm_path, doc_strm_path
from url_utils import get_m3u_path
from main import refresh_media_server, write_excluded_report


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
    title="M3U2strm_jf Web Dashboard",
    description="Web interface for M3U to STRM conversion",
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


class JobManager:
    """Manages background processing jobs"""
    
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
    
    async def run_pipeline_job(self, job_id: str, config_path: Optional[str] = None, dry_run: bool = False):
        """Run the M3U processing pipeline as a background job"""
        job = active_jobs[job_id]
        
        try:
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
                def __init__(self, job_id):
                    super().__init__()
                    self.job_id = job_id
                
                def emit(self, record):
                    if self.job_id in active_jobs:
                        log_entry = self.format(record)
                        active_jobs[self.job_id].logs.append(log_entry)
                        asyncio.create_task(broadcast_message(log_entry, "log"))
            
            job_handler = JobLogHandler(job_id)
            job_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(job_handler)
            
            try:
                # Run the pipeline (this is the existing main.run_pipeline logic)
                await self._run_pipeline_logic(cfg, job, dry_run)
                
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
    
    async def _run_pipeline_logic(self, cfg, job: JobStatus, dry_run: bool):
        """Core pipeline logic adapted from main.py"""
        
        # Handle M3U source
        job.current_step = "Processing M3U source"
        await broadcast_message(f"Processing M3U from: {cfg.m3u}")
        m3u_path = get_m3u_path(cfg.m3u)
        
        # Initialize cache and existing media
        job.current_step = "Building media cache"
        await broadcast_message("Building existing media cache...")
        cache = SQLiteCache(cfg.sqlite_cache_file)
        existing = {}
        for d in cfg.existing_media_dirs:
            existing.update(build_existing_media_cache(Path(d)))
        cache.replace_existing_media(existing)
        existing_keys = set(existing.keys())
        
        # Parse M3U
        job.current_step = "Parsing M3U playlist"
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
        unique_entries = {}
        for e in entries:
            key = KeyGenerator.generate_key(e)
            unique_entries[key] = e
        entries = list(unique_entries.values())
        await broadcast_message(f"Deduplicated: {len(entries)} -> {len(unique_entries)} unique entries")
        
        # Check cache
        job.current_step = "Checking cache"
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
        job.current_step = "Filtering by country"
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
        job.current_step = "Creating STRM files"
        await broadcast_message(f"Processing {len(allowed)} allowed entries...")
        
        existing_keys = set(existing.keys())
        strm_cache = cache.strm_cache_dict()
        new_cache = strm_cache.copy()
        written_count = 0
        skipped_count = 0
        
        def process_entry(e):
            nonlocal written_count, skipped_count
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
        
        # Process entries in parallel
        with ThreadPoolExecutor(max_workers=cfg.max_workers) as executor:
            list(executor.map(process_entry, allowed))
        
        # Update cache for excluded entries
        for e in excluded:
            key = KeyGenerator.generate_key(e)
            new_cache[key] = {"url": e.url, "path": None, "allowed": 0}
        
        if not dry_run:
            cache.replace_strm_cache(new_cache)
            await broadcast_message("Cleaning up orphan STRMs...")
            cleanup_strm_tree(cfg.output_dir, new_cache)
        
        # Refresh media servers
        if not dry_run:
            if getattr(cfg, "emby_api_url", None) and getattr(cfg, "emby_api_key", None):
                await broadcast_message("Triggering Emby library refresh...")
                refresh_media_server(cfg.emby_api_url, cfg.emby_api_key, "emby")
            elif getattr(cfg, "jellyfin_api_url", None) and getattr(cfg, "jellyfin_api_key", None):
                await broadcast_message("Triggering Jellyfin library refresh...")
                refresh_media_server(cfg.jellyfin_api_url, cfg.jellyfin_api_key, "jellyfin")
            else:
                await broadcast_message("Skipping media server refresh (not configured)")
        else:
            await broadcast_message("Skipping media server refresh (dry_run mode)")
        
        summary = f"Process complete: {written_count} STRMs {'would be ' if dry_run else ''}written, {skipped_count} skipped, {len(excluded)} excluded"
        await broadcast_message(summary)
        job.logs.append(summary)


job_manager = JobManager()


# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the dashboard HTML"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>M3U2strm_jf Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .status { background: #f0f0f0; padding: 20px; border-radius: 5px; }
            .job { border: 1px solid #ccc; margin: 10px 0; padding: 10px; }
            .running { background: #e8f5e8; }
            .completed { background: #e8f5e8; }
            .failed { background: #ffe8e8; }
            .logs { background: #000; color: #fff; padding: 10px; font-family: monospace; height: 300px; overflow-y: scroll; }
        </style>
    </head>
    <body>
        <h1>M3U2strm_jf Dashboard</h1>
        <div class="status">
            <h2>System Status</h2>
            <div id="status"></div>
        </div>
        
        <div>
            <h2>Jobs</h2>
            <button onclick="startJob()">Start Processing Job</button>
            <div id="jobs"></div>
        </div>
        
        <div>
            <h2>Real-time Logs</h2>
            <div id="logs" class="logs"></div>
        </div>
        
        <script>
            const ws = new WebSocket(`ws://${window.location.host}/ws`);
            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                if (data.type === 'log') {
                    const logs = document.getElementById('logs');
                    logs.innerHTML += data.message + '\\n';
                    logs.scrollTop = logs.scrollHeight;
                } else if (data.type === 'status') {
                    document.getElementById('status').innerHTML = JSON.stringify(data.message, null, 2);
                }
            };
            
            async function startJob() {
                const response = await fetch('/api/v1/jobs/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ dry_run: false })
                });
                const job = await response.json();
                console.log('Job started:', job);
            }
            
            // Load initial status
            fetch('/api/v1/status').then(r => r.json()).then(status => {
                document.getElementById('status').innerHTML = JSON.stringify(status, null, 2);
            });
            
            // Load jobs
            fetch('/api/v1/jobs').then(r => r.json()).then(jobs => {
                document.getElementById('jobs').innerHTML = JSON.stringify(jobs, null, 2);
            });
        </script>
    </body>
    </html>
    """


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


def main():
    """Main entry point for web server"""
    import argparse
    
    parser = argparse.ArgumentParser(description="M3U2strm_jf Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (development)")
    
    args = parser.parse_args()
    
    print(f"Starting M3U2strm_jf Web Dashboard on http://{args.host}:{args.port}")
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
