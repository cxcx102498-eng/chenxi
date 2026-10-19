"""Measure existing audio and reject silent truncation in clip rendering."""
import json
import math
import re
import shutil
import subprocess
import wave
from pathlib import Path


def probe_duration(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'Audio file missing: {path}')
    if path.suffix.lower() == '.wav':
        try:
            with wave.open(str(path), 'rb') as audio:
                return audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError):
            pass
    probe = shutil.which('ffprobe')
    if probe:
        result = subprocess.run(
            [probe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
        value = float(json.loads(result.stdout)['format']['duration'])
    else:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise ValueError('Audio duration check requires FFmpeg or FFprobe')
        result = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
        if not match:
            raise ValueError('Audio duration unavailable; measure the audio before rendering')
        h, m, s = map(float, match.groups())
        value = h * 3600 + m * 60 + s
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Audio duration must be finite and positive')
    return value


def require_matching_audio(path, duration, fps):
    actual = probe_duration(path)
    tolerance = max(0.1, 2 / fps)
    if abs(actual - duration) > tolerance:
        raise ValueError(
            f'Audio is {actual:.3f}s but clip is {duration:.3f}s. '
            'Align the timeline or provide the correct clip audio; refusing to silently trim or shorten.')
    return actual
