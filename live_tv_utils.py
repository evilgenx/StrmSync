#!/usr/bin/env python3
"""
Live TV Channel Management Utilities
Handles live TV channel processing, EPG integration, and channel editing
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urlparse
import requests
from m3u_utils import VODEntry, Category
from strm_utils import write_strm_file


@dataclass
class Channel:
    """Represents a live TV channel"""
    name: str
    safe_name: str
    url: str
    group: Optional[str] = None
    logo: Optional[str] = None
    epg_id: Optional[str] = None
    number: Optional[int] = None
    resolution: Optional[str] = None
    language: Optional[str] = None
    country: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ChannelGroup:
    """Represents a group of channels"""
    name: str
    channels: List[Channel]


@dataclass
class Program:
    """Represents a TV program from EPG"""
    channel_id: str
    start: str
    stop: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    episode_num: Optional[str] = None
    icon: Optional[str] = None


class LiveTVProcessor:
    """Processes live TV channels and generates STRM files"""
    
    def __init__(self, config):
        self.config = config
        self.channels: List[Channel] = []
        self.groups: Dict[str, ChannelGroup] = {}
        self.epg_data: Dict[str, List[Program]] = {}
    
    def parse_m3u_for_live_tv(self, m3u_path: Path) -> List[Channel]:
        """Parse M3U file specifically for live TV channels"""
        channels = []
        cur_title, cur_group, cur_logo = None, None, None
        
        with m3u_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                if line.startswith("#EXTINF:"):
                    if "," in line:
                        cur_title = line.rsplit(",", 1)[-1].strip()
                    else:
                        cur_title = line
                    
                    # Extract metadata from EXTINF line
                    m = re.search(r'group-title="([^"]+)"', line, flags=re.IGNORECASE)
                    if m:
                        cur_group = m.group(1).strip().lower()
                    
                    m = re.search(r'tvg-logo="([^"]+)"', line, flags=re.IGNORECASE)
                    if m:
                        cur_logo = m.group(1).strip()
                    
                    m = re.search(r'tvg-id="([^"]+)"', line, flags=re.IGNORECASE)
                    if m:
                        epg_id = m.group(1).strip()
                    else:
                        epg_id = None
                    
                    m = re.search(r'tvg-name="([^"]+)"', line, flags=re.IGNORECASE)
                    if m:
                        display_name = m.group(1).strip()
                    else:
                        display_name = None
                
                elif cur_title and line.startswith(("http://", "https://")):
                    # Skip VOD entries (those with years in title)
                    if re.search(r"\(\d{4}\)\s*$", cur_title) or re.search(r"[-–]\s*\d{4}\s*$", cur_title):
                        cur_title, cur_group, cur_logo = None, None, None
                        continue
                    
                    # Skip entries that look like TV shows
                    if re.search(r"[Ss]\d{1,2}\s*[Ee]\d{1,2}", cur_title):
                        cur_title, cur_group, cur_logo = None, None, None
                        continue
                    
                    # Determine if this should be processed as live TV
                    should_process = True
                    
                    # Check if it matches any replay keywords
                    replay_keywords = [k.strip().lower() for k in self.config.replay_group_keywords or []]
                    if cur_group and any(keyword in cur_group for keyword in replay_keywords):
                        should_process = False
                    
                    # Check ignore keywords
                    ignore_keywords = self.config.ignore_keywords or {}
                    title_lower = cur_title.lower()
                    for keyword in ignore_keywords.get("tvshows", []):
                        if keyword.lower() in title_lower:
                            should_process = False
                            break
                    
                    if should_process:
                        channel = Channel(
                            name=cur_title,
                            safe_name=self._sanitize_channel_name(cur_title),
                            url=line,
                            group=cur_group,
                            logo=cur_logo,
                            epg_id=epg_id,
                            number=self._extract_channel_number(cur_title)
                        )
                        channels.append(channel)
                    
                    cur_title, cur_group, cur_logo = None, None, None
        
        self.channels = channels
        logging.info(f"Parsed {len(channels)} live TV channels from M3U")
        return channels
    
    def _sanitize_channel_name(self, name: str) -> str:
        """Sanitize channel name for file system"""
        # Remove EPG ID and other metadata that might be in the name
        name = re.sub(r'\s*\(.*?\)\s*', '', name)  # Remove parentheses content
        name = re.sub(r'\s*-\s*.*$', '', name)     # Remove after last dash
        name = re.sub(r'[^\w\s-]', '', name)       # Remove special characters
        name = re.sub(r'\s+', ' ', name).strip()   # Normalize whitespace
        return name
    
    def _extract_channel_number(self, title: str) -> Optional[int]:
        """Extract channel number from title"""
        # Look for patterns like "Channel 5", "CH 5", "5.", etc.
        patterns = [
            r'channel\s*(\d+)',
            r'ch\s*(\d+)',
            r'^(\d+)\.',
            r'^(\d+)\s',
            r'#(\d+)'
        ]
        
        title_lower = title.lower()
        for pattern in patterns:
            match = re.search(pattern, title_lower)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
        
        return None
    
    def group_channels(self) -> Dict[str, ChannelGroup]:
        """Group channels by category"""
        groups = {}
        
        # Use configured channel groups or auto-detect from M3U
        if self.config.channel_groups:
            group_names = self.config.channel_groups
        else:
            # Auto-detect groups from channel data
            group_names = list(set(channel.group for channel in self.channels if channel.group))
        
        for group_name in group_names:
            group_channels = [c for c in self.channels if c.group == group_name]
            if group_channels:
                groups[group_name] = ChannelGroup(name=group_name, channels=group_channels)
        
        # Group channels without explicit group into "General"
        ungrouped = [c for c in self.channels if c.group not in groups]
        if ungrouped:
            groups["General"] = ChannelGroup(name="General", channels=ungrouped)
        
        self.groups = groups
        logging.info(f"Grouped channels into {len(groups)} categories")
        return groups
    
    def load_epg_data(self, epg_url: Optional[str] = None) -> Dict[str, List[Program]]:
        """Load EPG (Electronic Program Guide) data"""
        if not epg_url and self.config.epg_url:
            epg_url = self.config.epg_url
        
        if not epg_url:
            logging.warning("No EPG URL configured, skipping EPG loading")
            return {}
        
        try:
            if epg_url.startswith(('http://', 'https://')):
                response = requests.get(epg_url, timeout=30)
                response.raise_for_status()
                epg_content = response.content
            else:
                with open(epg_url, 'rb') as f:
                    epg_content = f.read()
            
            # Parse XMLTV format
            root = ET.fromstring(epg_content)
            programs = {}
            
            for program in root.findall('programme'):
                channel_id = program.get('channel')
                start = program.get('start')
                stop = program.get('stop')
                
                title_elem = program.find('title')
                title = title_elem.text if title_elem is not None else "Unknown Program"
                
                desc_elem = program.find('desc')
                description = desc_elem.text if desc_elem is not None else None
                
                category_elem = program.find('category')
                category = category_elem.text if category_elem is not None else None
                
                episode_elem = program.find('episode-num')
                episode_num = episode_elem.text if episode_elem is not None else None
                
                icon_elem = program.find('icon')
                icon = icon_elem.get('src') if icon_elem is not None else None
                
                program_data = Program(
                    channel_id=channel_id,
                    start=start,
                    stop=stop,
                    title=title,
                    description=description,
                    category=category,
                    episode_num=episode_num,
                    icon=icon
                )
                
                if channel_id not in programs:
                    programs[channel_id] = []
                programs[channel_id].append(program_data)
            
            self.epg_data = programs
            logging.info(f"Loaded EPG data for {len(programs)} channels")
            return programs
            
        except Exception as e:
            logging.error(f"Failed to load EPG data: {e}")
            return {}
    
    def generate_strm_files(self, dry_run: bool = False) -> int:
        """Generate STRM files for live TV channels"""
        if not self.config.enable_live_tv:
            logging.info("Live TV is disabled, skipping STRM generation")
            return 0
        
        output_dir = self.config.live_tv_output_dir or self.config.output_dir
        if not output_dir:
            logging.error("No output directory configured for live TV")
            return 0
        
        output_dir = output_dir / "Live TV"
        written_count = 0
        
        for group_name, group in self.groups.items():
            group_dir = output_dir / group_name
            
            for channel in group.channels:
                # Create channel STRM file
                strm_path = group_dir / f"{channel.safe_name}.strm"
                
                if not dry_run:
                    try:
                        write_strm_file(output_dir, strm_path.relative_to(output_dir), channel.url)
                        written_count += 1
                        
                        # Create NFO file with channel metadata
                        self._create_channel_nfo(strm_path.with_suffix('.nfo'), channel)
                        
                    except Exception as e:
                        logging.error(f"Failed to create STRM for {channel.name}: {e}")
                else:
                    written_count += 1
                    logging.info(f"Would create STRM for {channel.name} in {group_name}")
        
        logging.info(f"Generated {written_count} live TV STRM files")
        return written_count
    
    def _create_channel_nfo(self, nfo_path: Path, channel: Channel):
        """Create NFO file with channel metadata for Jellyfin/Emby"""
        nfo_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<tvshow>
    <title>{channel.name}</title>
    <plot>{channel.description or f"Live TV channel: {channel.name}"}</plot>
    <genre>{channel.group or "General"}</genre>
    <channel>{channel.epg_id or channel.name}</channel>
    {f'<thumb>{channel.logo}</thumb>' if channel.logo else ''}
    {f'<channelnumber>{channel.number}</channelnumber>' if channel.number else ''}
    {f'<country>{channel.country}</country>' if channel.country else ''}
    {f'<language>{channel.language}</language>' if channel.language else ''}
</tvshow>
"""
        
        try:
            nfo_path.parent.mkdir(parents=True, exist_ok=True)
            with nfo_path.open('w', encoding='utf-8') as f:
                f.write(nfo_content)
            logging.debug(f"Created NFO file for {channel.name}")
        except Exception as e:
            logging.error(f"Failed to create NFO for {channel.name}: {e}")
    
    def get_channel_stats(self) -> Dict[str, Any]:
        """Get statistics about processed channels"""
        total_channels = len(self.channels)
        total_groups = len(self.groups)
        
        group_stats = {}
        for group_name, group in self.groups.items():
            group_stats[group_name] = {
                'channel_count': len(group.channels),
                'channels': [c.name for c in group.channels]
            }
        
        return {
            'total_channels': total_channels,
            'total_groups': total_groups,
            'groups': group_stats,
            'epg_channels': len(self.epg_data) if self.epg_data else 0
        }
    
    def export_channel_list(self, format: str = 'json') -> str:
        """Export channel list in various formats"""
        if format.lower() == 'json':
            import json
            data = {
                'channels': [asdict(channel) for channel in self.channels],
                'groups': {name: [asdict(c) for c in group.channels] for name, group in self.groups.items()},
                'stats': self.get_channel_stats()
            }
            return json.dumps(data, indent=2, ensure_ascii=False)
        
        elif format.lower() == 'm3u':
            m3u_content = "#EXTM3U\n"
            for channel in self.channels:
                tvg_info = []
                if channel.epg_id:
                    tvg_info.append(f'tvg-id="{channel.epg_id}"')
                if channel.logo:
                    tvg_info.append(f'tvg-logo="{channel.logo}"')
                if channel.group:
                    tvg_info.append(f'group-title="{channel.group}"')
                
                tvg_str = ' '.join(tvg_info) if tvg_info else ''
                m3u_content += f'#EXTINF:-1 {tvg_str},{channel.name}\n'
                m3u_content += f'{channel.url}\n\n'
            
            return m3u_content
        
        else:
            raise ValueError(f"Unsupported export format: {format}")


class ChannelEditor:
    """Provides channel editing capabilities"""
    
    def __init__(self, config):
        self.config = config
        self.processor = LiveTVProcessor(config)
    
    def add_channel(self, channel: Channel) -> bool:
        """Add a new channel"""
        try:
            self.processor.channels.append(channel)
            logging.info(f"Added channel: {channel.name}")
            return True
        except Exception as e:
            logging.error(f"Failed to add channel {channel.name}: {e}")
            return False
    
    def remove_channel(self, channel_name: str) -> bool:
        """Remove a channel by name"""
        try:
            self.processor.channels = [c for c in self.processor.channels if c.name != channel_name]
            logging.info(f"Removed channel: {channel_name}")
            return True
        except Exception as e:
            logging.error(f"Failed to remove channel {channel_name}: {e}")
            return False
    
    def update_channel(self, channel_name: str, updates: Dict[str, Any]) -> bool:
        """Update channel information"""
        try:
            for channel in self.processor.channels:
                if channel.name == channel_name:
                    for key, value in updates.items():
                        if hasattr(channel, key):
                            setattr(channel, key, value)
                    logging.info(f"Updated channel: {channel_name}")
                    return True
            logging.warning(f"Channel not found: {channel_name}")
            return False
        except Exception as e:
            logging.error(f"Failed to update channel {channel_name}: {e}")
            return False
    
    def add_group(self, group_name: str, channels: List[Channel]) -> bool:
        """Add a new channel group"""
        try:
            self.processor.groups[group_name] = ChannelGroup(name=group_name, channels=channels)
            logging.info(f"Added group: {group_name}")
            return True
        except Exception as e:
            logging.error(f"Failed to add group {group_name}: {e}")
            return False
    
    def remove_group(self, group_name: str) -> bool:
        """Remove a channel group"""
        try:
            if group_name in self.processor.groups:
                del self.processor.groups[group_name]
                logging.info(f"Removed group: {group_name}")
                return True
            else:
                logging.warning(f"Group not found: {group_name}")
                return False
        except Exception as e:
            logging.error(f"Failed to remove group {group_name}: {e}")
            return False
    
    def import_channels_from_m3u(self, m3u_path: Path) -> bool:
        """Import channels from M3U file"""
        try:
            channels = self.processor.parse_m3u_for_live_tv(m3u_path)
            self.processor.group_channels()
            logging.info(f"Imported {len(channels)} channels from {m3u_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to import channels from {m3u_path}: {e}")
            return False
    
    def export_channels_to_m3u(self, output_path: Path) -> bool:
        """Export channels to M3U file"""
        try:
            m3u_content = self.processor.export_channel_list('m3u')
            with output_path.open('w', encoding='utf-8') as f:
                f.write(m3u_content)
            logging.info(f"Exported channels to {output_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to export channels to {output_path}: {e}")
            return False
