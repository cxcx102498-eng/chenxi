"""Portable whiteboard project pipeline. Run --help for the stage commands.

This file is copied with its sibling renderer, fonts and scripts into every project.
It never synthesizes audio, changes narration, or assumes a fixed number of shots.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
EXCLUDED = {'.video-deps', '.video-tools', '__pycache__', '.venv', '.git', 'node_modules'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def local(root, relative):
    require(isinstance(relative, str) and relative, 'Expected a project-relative path')
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), f'Path escapes project: {relative}')
    require(path.is_file(), f'Missing file: {path}')
    return path


def dependencies(root):
    sys.path.insert(0, str(root / '.video-deps'))
    try:
        import av
        import numpy
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('Missing video dependency. Install renderer/requirements.txt with this Python; '
                           'a project-local .video-deps directory is also supported.') from exc


def ffmpeg():
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError('FFmpeg is missing. Install imageio-ffmpeg or add FFmpeg to PATH.') from exc


def command(args, log=None):
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('w', encoding='utf-8') as stream:
            result = subprocess.run([str(x) for x in args], stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'Command failed; see {log}\n' + log.read_text(encoding='utf-8', errors='replace')[-5000:])
    else:
        result = subprocess.run([str(x) for x in args], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            raise RuntimeError('Command failed: ' + result.stderr[-5000:] + result.stdout[-1000:])
    return result


def record(root, stage, started, status, details=None):
    if not root.is_dir():
        return
    log = root / 'logs/stages.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'stage': stage, 'status': status,
                                'utc': datetime.now(timezone.utc).isoformat(),
                                'elapsed_s': round(time.monotonic() - started, 3),
                                'details': details}, ensure_ascii=False) + '\n')


def init_project(package, root):
    from import_materials import import_package
    require(not root.exists() or not any(root.iterdir()), 'init is one-time only; choose a new or empty project directory')
    report = import_package(package, root)
    shutil.copytree(package, root / 'source-package', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*EXCLUDED))
    for name in ('credits.txt', 'credits.md'):
        if (package / name).is_file():
            with (root / 'credits.txt').open('a', encoding='utf-8') as credits:
                credits.write('\n\n--- Original material package ' + name + ' ---\n\n')
                credits.write((package / name).read_text(encoding='utf-8-sig'))
    if (package / 'editable').is_dir():
        shutil.copytree(package / 'editable', root / 'editable')
    for layers in ('exports/layers', 'exports/transparent', 'layers'):
        if (package / layers).is_dir():
            shutil.copytree(package / layers, root / 'assets/layers', dirs_exist_ok=True)
    shutil.copytree(HERE, root / 'renderer', ignore=shutil.ignore_patterns(*EXCLUDED, '*.pyc'))
    write(root / 'project.json', {'schema_version': 1, 'package_id': report['package_id'],
                                'pipeline': 'article-audio-to-whiteboard-video', 'source_snapshot': 'source-package',
                                'created_utc': datetime.now(timezone.utc).isoformat()})
    return {'shots': report['shot_count'], 'project': str(root), 'next': 'Align the supplied complete narration audio.'}


def timeline(root):
    tl = read(root / 'timeline.json')
    require(isinstance(tl.get('shots'), list) and tl['shots'], 'Timeline has no shots')
    require(isinstance(tl.get('fps'), int) and tl['fps'] > 0, 'Timeline fps must be a positive integer')
    previous, ids, shape = 0, set(), None
    for shot in tl['shots']:
        sid = shot.get('shot_id')
        require(isinstance(sid, str) and re.fullmatch(r'S\d{3,}', sid) and sid not in ids, 'Invalid or duplicate shot ID')
        ids.add(sid)
        require(shot.get('start_frame') == previous and isinstance(shot.get('frame_count'), int)
                and shot['frame_count'] > 0, f'{sid}: shots must cover consecutive positive frame intervals')
        spec = read(local(root, shot['clip']))
        current_shape = (spec['width'], spec['height'], spec['fps'])
        require(shape is None or shape == current_shape, 'All shots must share canvas size and fps')
        shape = current_shape
        require(spec['fps'] == tl['fps'] and abs(spec['duration'] * tl['fps'] - shot['frame_count']) < 1e-6,
                f'{sid}: duration/frame-count mismatch')
        previous += shot['frame_count']
    require(previous == tl.get('total_frames') and abs(previous / tl['fps'] - tl['duration_s']) < 1e-7,
            'Timeline total duration/frame count mismatch')
    return tl, shape


def selected_shots(tl, ids):
    requested = set(ids.split(',')) if ids else set()
    known = {s['shot_id'] for s in tl['shots']}
    require(requested <= known, 'Unknown shot IDs: ' + ', '.join(sorted(requested - known)))
    return [s for s in tl['shots'] if not requested or s['shot_id'] in requested]


def media_info(path):
    import av
    with av.open(str(path)) as container:
        require(container.streams.video, f'No video stream: {path}')
        v = container.streams.video[0]
        count = v.frames or sum(1 for _ in container.decode(video=0))
        result = {'width': v.width, 'height': v.height, 'fps': float(v.average_rate), 'frames': count,
                  'video_duration_s': float(v.duration * v.time_base) if v.duration is not None else count / float(v.average_rate)}
        if container.streams.audio:
            a = container.streams.audio[0]
            result.update(audio_duration_s=float(a.duration * a.time_base) if a.duration is not None else None,
                          audio_codec=a.codec_context.name, audio_sample_rate=a.codec_context.sample_rate,
                          audio_channels=a.codec_context.channels)
        return result


def frame_check(path, shot, shape):
    result = media_info(path)
    require((result['width'], result['height'], result['fps']) == shape and result['frames'] == shot['frame_count'],
            f'{shot["shot_id"]}: rendered media does not match the configured frames/canvas/fps')
    return result


def runtime_hash(root):
    renderer = root / 'renderer'
    files = [p for p in renderer.rglob('*') if p.is_file() and not (set(p.relative_to(renderer).parts) & EXCLUDED)
             and p.suffix != '.pyc']
    return hashlib.sha256(json.dumps([(p.relative_to(renderer).as_posix(), digest(p)) for p in sorted(files)],
                                    ensure_ascii=False).encode()).hexdigest()


def shot_hash(root, shot, engine_hash):
    spec_path = local(root, shot['clip'])
    spec = read(spec_path)
    assets = []
    for value in [q.get('image') for q in spec.get('cues', [])] + [spec.get('data', {}).get('image')]:
        if value:
            require(not value.startswith(('http:', 'https:', 'data:')), 'Rendering requires local, auditable asset files')
            asset = (spec_path.parent / value).resolve()
            require(asset.is_relative_to(root) and asset.is_file(), f'Missing/outside project asset: {value}')
            assets.append((asset.relative_to(root).as_posix(), digest(asset)))
    return hashlib.sha256(json.dumps({'spec': digest(spec_path), 'assets': assets, 'engine': engine_hash,
                                     'frame_count': shot['frame_count'], 'render_crf': 18, 'qa_fps': 4},
                                    sort_keys=True).encode()).hexdigest()


def qa_pass(path):
    report = read(path)
    return bool(report.get('segments')) and not report.get('page_errors') and all(
        q.get('deterministic') is True and not q.get('framing') for q in report['segments'])


def cache_valid(root, shot, shape, key):
    try:
        cached = read(root / 'cache' / (shot['shot_id'] + '.json'))
        media = root / 'renders' / (shot['shot_id'] + '.mp4')
        valid = cached.get('hash') == key and digest(media) == cached.get('video_sha256')
        valid = valid and qa_pass(root / 'qa' / shot['shot_id'] / 'qa.json')
        if valid:
            frame_check(media, shot, shape)
        return valid
    except (OSError, ValueError, KeyError):
        return False


def renderer_command(root, mode, spec, tail=()):
    return [sys.executable, '-X', 'utf8', root / 'renderer/run.py', mode, '--spec', spec, *tail]


def contact_sheets(records, target):
    from PIL import Image, ImageDraw, ImageFont
    target.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default()
    for offset in range(0, len(records), 12):
        chunk = records[offset:offset + 12]
        page = Image.new('RGB', (1440, 304 * math.ceil(len(chunk) / 3)), '#eef2f6')
        draw = ImageDraw.Draw(page)
        for index, (label, path) in enumerate(chunk):
            x, y = index % 3 * 480, index // 3 * 304
            with Image.open(path) as source:
                im = source.convert('RGB')
                im.thumbnail((480, 270))
                page.paste(im, (x + (480 - im.width) // 2, y + 30))
            draw.text((x + 10, y + 5), label, font=font, fill='#18345a')
        page.save(target / f'contact-{offset // 12 + 1:02}.jpg', quality=90)


def render_stage(root, mode, ids='', workers=2):
    tl, shape = timeline(root)
    selected = selected_shots(tl, ids)
    engine = runtime_hash(root)
    results = []

    def one(shot):
        sid = shot['shot_id']
        spec = local(root, shot['clip'])
        data = read(spec)
        command(renderer_command(root, 'validate', spec), root / 'logs' / f'{sid}-validate.log')
        key = shot_hash(root, shot, engine)
        if mode == 'render' and cache_valid(root, shot, shape, key):
            return {'shot_id': sid, 'status': 'cached'}
        if mode == 'preview':
            limit = max(0, data['duration'] - 1 / tl['fps'])
            ts = {0, min(.4, limit), limit}
            for q in data['cues']:
                if q['kind'] in {'highlight', 'focus', 'reset', 'point', 'subtitle'}:
                    ts.add(min(limit, q['at'] + min(q.get('dur', .65), .8)))
            folder = root / 'preview' / sid
            if folder.is_dir():
                for stale in folder.glob('*.png'):
                    stale.unlink()
            command(renderer_command(root, 'render', spec, ['--stills', ','.join(f'{t:.6f}' for t in sorted(ts)),
                                                           '--out', folder]), root / 'logs' / f'{sid}-preview.log')
        else:
            qa = root / 'qa' / sid
            command(renderer_command(root, 'qa', spec, ['--out', qa, '--fps', '4', '--sub-band', '0']),
                    root / 'logs' / f'{sid}-qa.log')
            require(qa_pass(qa / 'qa.json'), f'{sid}: deterministic/frame QA failed; inspect qa/{sid}')
            video = root / 'renders' / (sid + '.mp4')
            video.parent.mkdir(parents=True, exist_ok=True)
            command(renderer_command(root, 'render', spec, ['--out', video, '--crf', '18']),
                    root / 'logs' / f'{sid}-render.log')
            measured = frame_check(video, shot, shape)
            write(root / 'cache' / (sid + '.json'), {'hash': key, 'video_sha256': digest(video), 'measured': measured})
        return {'shot_id': sid, 'status': mode + 'ed'}

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(one, shot): shot['shot_id'] for shot in selected}
        for job in concurrent.futures.as_completed(jobs):
            try:
                result = job.result()
                results.append(result)
                print(result['shot_id'], result['status'], flush=True)
            except Exception as exc:
                failures.append(f'{jobs[job]}: {exc}')
    if failures:
        raise RuntimeError('\n'.join(failures))
    if mode == 'preview':
        records = [(f'{s["shot_id"]} / {p.stem}s', p) for s in tl['shots']
                   for p in sorted((root / 'preview' / s['shot_id']).glob('*.png'))]
        contact_sheets(records, root / 'preview')
    return results


def pcm(path, rate=48000):
    import av
    import numpy as np
    blocks = []
    with av.open(str(path)) as c:
        require(c.streams.audio, f'No audio stream: {path}')
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=rate)
        for frame in c.decode(audio=0):
            blocks.extend(out.to_ndarray().flatten() for out in resampler.resample(frame))
        blocks.extend(out.to_ndarray().flatten() for out in resampler.resample(None))
    require(blocks, f'Empty audio: {path}')
    return np.concatenate(blocks)


def audio_checks(source, output, rate=48000):
    import numpy as np
    checks = []
    window = min(rate, len(source))
    starts = sorted(set(int(x) for x in np.linspace(0, max(0, len(source) - window), 6)))
    for start in starts:
        x, y = source[start:start + window], output[start:start + window]
        require(len(x) == len(y), 'Final audio is truncated')
        rms = float(np.sqrt(np.mean(x * x)))
        if rms < 1e-5:
            error = float(np.sqrt(np.mean((x - y) ** 2)))
            require(error < .002, 'Final audio changed a silent source window')
            checks.append({'time_s': start / rate, 'silent': True, 'rms_error': error})
        else:
            corr = float(np.corrcoef(x, y)[0, 1])
            require(math.isfinite(corr) and corr > .97, f'Audio waveform mismatch at {start / rate:.3f}s: {corr}')
            checks.append({'time_s': start / rate, 'correlation': corr})
    return checks


def final_frames(root, video, tl):
    import av
    sample = {0: 'first', tl['total_frames'] - 1: 'last'}
    for shot in tl['shots']:
        start, count, sid = shot['start_frame'], shot['frame_count'], shot['shot_id']
        for delta, label in [(0, 'in'), (count // 2, 'mid'), (count - 1, 'out')]:
            sample[start + delta] = sid + '-' + label
        spec = read(root / shot['clip'])
        if spec.get('data', {}).get('image_enter', 'fade-up') != 'none':
            dur = spec.get('data', {}).get('image_enter_duration', .6)
            for offset, label in [(dur / 2, 'enter-mid'), (dur + .1, 'enter-settled')]:
                sample[start + min(count - 1, round(offset * tl['fps']))] = sid + '-' + label
        for cue in spec['cues']:
            if cue['kind'] in {'highlight', 'focus', 'reset', 'subtitle'}:
                for delta, label in [(0, 'start'), (min(cue.get('dur', .65), .8), 'settled')]:
                    frame = start + min(count - 1, round((cue['at'] + delta) * tl['fps']))
                    sample[frame] = sid + '-' + cue['kind'] + '-' + label
    target = root / 'qa/final-decode'
    target.mkdir(parents=True, exist_ok=True)
    # Remove only this generated review set so old contact pages cannot be mistaken for current output.
    for path in target.glob('*.jpg'):
        path.unlink()
    records = []
    with av.open(str(video)) as container:
        for index, frame in enumerate(container.decode(video=0)):
            if index in sample:
                path = target / f'{index:07}_{sample[index]}.jpg'
                frame.to_image().save(path, quality=92)
                records.append((f'{sample[index]} / {index / tl["fps"]:.2f}s', path))
    require(len(records) == len(sample), 'Final frame extraction missed requested samples')
    contact_sheets(records, target)
    return len(records)


def export_video(root, name):
    require(Path(name).name == name and name.lower().endswith('.mp4'), '--name must be a filename ending in .mp4')
    tl, shape = timeline(root)
    require(tl.get('timing_basis') != 'estimated' and isinstance(tl.get('audio'), dict),
            'Align the supplied audio before export; estimated timing cannot be delivered')
    engine = runtime_hash(root)
    for shot in tl['shots']:
        require(read(local(root, shot['clip'])).get('data', {}).get('draft') is not True,
                f'{shot["shot_id"]}: draft-marked material is not a final deliverable')
        require(cache_valid(root, shot, shape, shot_hash(root, shot, engine)),
                f'{shot["shot_id"]}: render is missing, changed or invalid; run render first')
    audio = local(root, tl['audio']['file'])
    require(digest(audio) == tl['audio']['sha256'], 'Supplied audio hash changed; align again')
    source = pcm(audio)
    measured_duration = len(source) / 48000
    duration, fps = tl['duration_s'], tl['fps']
    require(abs(measured_duration - tl['audio']['duration_s']) <= 2 / 48000,
            'Recorded audio duration differs from the actual decoded samples')
    require(tl['total_frames'] == math.ceil((measured_duration - 1e-8) * fps),
            'Video must use exactly ceil(full source audio seconds × fps) frames; no arbitrary padding or truncation')
    require(-1e-8 <= duration - measured_duration < 1 / fps + 1e-8, 'Audio/video duration mismatch')
    concat = root / 'renders/concat.txt'
    concat.write_text('\n'.join("file '" + s['shot_id'] + ".mp4'" for s in tl['shots']) + '\n', encoding='utf-8')
    output = root / 'exports' / name
    output.parent.mkdir(parents=True, exist_ok=True)
    command([ffmpeg(), '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', concat, '-i', audio,
             '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
             '-af', 'apad', '-t', f'{duration:.9f}', '-movflags', '+faststart', output], root / 'logs/export.log')
    command([ffmpeg(), '-v', 'error', '-xerror', '-i', output, '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
            root / 'logs/full-decode.log')
    report = media_info(output)
    require(report['frames'] == tl['total_frames'] and (report['width'], report['height'], report['fps']) == shape,
            'Export dimensions/fps/frame count mismatch')
    require(abs(report['video_duration_s'] - report['audio_duration_s']) <= 2 / fps, 'Final audio/video stream duration mismatch')
    for filename in ('subtitles.srt', 'subtitles.vtt', 'subtitles.json', 'timeline.json'):
        require((root / filename).is_file(), f'Missing {filename}; align audio first')
        shutil.copy2(root / filename, output.parent / filename)
    report.update(schema_version=1, status='pass', video=output.relative_to(root).as_posix(), full_decode='pass',
                  source_audio_sha256=digest(audio), source_audio_duration_s=measured_duration,
                  tail_pad_s=duration - measured_duration, audio_waveform_checks=audio_checks(source, pcm(output)),
                  sha256=digest(output), timeline_sha256=digest(root / 'timeline.json'),
                  subtitle_sha256={prefix + name: digest(root / (prefix + name))
                                   for prefix in ('', 'exports/')
                                   for name in ('subtitles.srt', 'subtitles.vtt', 'subtitles.json')},
                  reviewed_frames=final_frames(root, output, tl), renderer_hash=engine)
    write(root / 'qa/export-report.json', report)
    return report


def package_project(root):
    tl, shape = timeline(root)
    report = read(root / 'qa/export-report.json')
    video = local(root, report['video'])
    require(report.get('status') == 'pass' and report.get('full_decode') == 'pass', 'Final decode has not passed')
    require(report.get('sha256') == digest(video) and report.get('timeline_sha256') == digest(root / 'timeline.json'),
            'Video/timeline changed after export verification; run export again')
    require(report.get('source_audio_sha256') == tl['audio']['sha256'] == digest(local(root, tl['audio']['file'])),
            'Source audio changed after export verification; align and export again')
    require(all(digest(local(root, name)) == value for name, value in report.get('subtitle_sha256', {}).items())
            and set(report.get('subtitle_sha256', {})) == {prefix + name for prefix in ('', 'exports/')
                                                          for name in ('subtitles.srt', 'subtitles.vtt', 'subtitles.json')},
            'Subtitles changed after export verification; export again')
    require(report.get('renderer_hash') == runtime_hash(root), 'Renderer changed after export; run render and export again')
    for shot in tl['shots']:
        require(cache_valid(root, shot, shape, shot_hash(root, shot, report['renderer_hash'])),
                f'{shot["shot_id"]}: source/config changed after verification')
    review_path = root / 'qa/visual-review.md'
    require(review_path.is_file(), 'View final-decode contact sheets, then write qa/visual-review.md with status: pass and the reviewed video SHA-256')
    review = review_path.read_text(encoding='utf-8-sig')
    require(re.search(r'(?im)^status:\s*pass\s*$', review) and report['sha256'] in review,
            'Visual review must state status: pass and contain the exact current video SHA-256')
    versions = {}
    for name in ('av', 'numpy', 'Pillow', 'playwright', 'imageio-ffmpeg', 'faster-whisper'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    # Outside renderer, so recording the environment cannot invalidate the tested engine hash.
    requirements = root / 'requirements-lock.txt'
    requirements.write_text('\n'.join(f'{name}=={version}' for name, version in versions.items()) + '\n', encoding='utf-8')
    (root / 'README.md').write_text(
        '# 可编辑白板视频工程\n\n'
        f'成片：`{report["video"]}`，{tl["duration_s"]:.3f} 秒，{shape[0]}×{shape[1]} / {tl["fps"]} fps。\n\n'
        '原始音频见 `audio/`，语义分镜与完整源素材见 `source-package/`，逐镜设置见 `clips/`，'
        '可编辑图形见 `editable/`（若素材包提供）。时间轴与字幕在工程根目录及 `exports/`。\n\n'
        '## 本地重建\n\n'
        '使用 Python 3.10+，在工程根目录执行（路径含空格时加引号）：\n\n'
        '```text\npython -m pip install -r requirements-lock.txt\n'
        'python -m playwright install chromium\n'
        'python renderer/pipeline.py preview --project .\n'
        'python renderer/pipeline.py render --project . --workers 2\n'
        f'python renderer/pipeline.py export --project . --name "{video.name}"\n```\n\n'
        '已安装 Chrome/Edge 可由渲染器自动发现；也可设置 `AI_VIDEO_BROWSER`。'
        '修改图像、单镜配置或引擎后，仅受影响镜头会重渲。全片改音频需重新运行 renderer/align_audio.py，'
        '然后预览、渲染和导出。阅读其 `--help` 选择工程、录音与转录输入。\n\n'
        '重新导出后查看 `qa/final-decode/` 和实际视频，将检查结果与新视频哈希写入 '
        '`qa/visual-review.md`（含 `status: pass`），再运行 `python renderer/pipeline.py package --project .`。'
        'ZIP 不包含可重建的 renders、preview、依赖缓存；复制后第一次运行会重新渲染。\n', encoding='utf-8')
    delivery = {'status': 'complete', 'video': report['video'], 'sha256': report['sha256'],
                'shots': len(tl['shots']), 'full_decode': 'pass', 'visual_review': 'pass', 'dependencies': versions}
    write(root / 'qa/delivery-report.json', delivery)
    files = [p for p in root.rglob('*') if p.is_file() and not (set(p.relative_to(root).parts) &
             (EXCLUDED | {'renders', 'preview', 'cache'})) and p.suffix not in {'.pyc', '.zip'}]
    checksums = root / 'checksums.sha256'
    checksums.write_text('\n'.join(digest(p) + '  ' + p.relative_to(root).as_posix()
                                   for p in sorted(files) if p != checksums) + '\n', encoding='utf-8')
    files = [p for p in files if p != checksums] + [checksums]
    archive = root.parent / (root.name + '-editable.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in files:
            z.write(path, root.name + '/' + path.relative_to(root).as_posix())
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None, 'ZIP verification failed')
    return {**delivery, 'archive': str(archive), 'archive_sha256': digest(archive), 'files': len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='stage', required=True)
    init = sub.add_parser('init')
    init.add_argument('--package', type=Path, required=True)
    init.add_argument('--out', type=Path, required=True)
    for name in ('preview', 'render', 'export', 'package'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--project', type=Path, required=True)
        if name in ('preview', 'render'):
            cmd.add_argument('--ids', default='')
            cmd.add_argument('--workers', type=int, default=2)
        if name == 'export':
            cmd.add_argument('--name', default='video.mp4')
    args = parser.parse_args()
    root = (args.out if args.stage == 'init' else args.project).resolve()
    started = time.monotonic()
    try:
        dependencies(root)
        if args.stage == 'init':
            result = init_project(args.package.resolve(), root)
        elif args.stage in ('preview', 'render'):
            result = render_stage(root, args.stage, args.ids, args.workers)
        elif args.stage == 'export':
            result = export_video(root, args.name)
        else:
            result = package_project(root)
        record(root, args.stage, started, 'pass', result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        record(root, args.stage, started, 'failed', str(exc))
        parser.exit(2, f'{args.stage} failed: {exc}\n')


if __name__ == '__main__':
    main()
