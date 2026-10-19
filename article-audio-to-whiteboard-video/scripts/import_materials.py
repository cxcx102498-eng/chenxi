"""Import a verified local visual-assets package; never fetch, render or synthesize audio.

Usage: python import_materials.py --package PACKAGE --out NEW_DIRECTORY [--draft]
All validation and planning complete before the destination is created.
"""
import argparse
import copy
import csv
import hashlib
import json
import math
import re
import shutil
from pathlib import Path


ALLOWED_CUES = {'title', 'point', 'highlight', 'focus', 'reset', 'subtitle'}
DEFAULT_THEME = {'background': '#FFFFFF', 'ink': '#0B214A',
                 'accent': '#FF7A00', 'blue': '#1597F5'}
PREVIEW_LABEL = '预览·素材待核查'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label, minimum=None, maximum=None):
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite, f'{label}: expected a finite number')
    require(minimum is None or value >= minimum, f'{label}: too small')
    require(maximum is None or value <= maximum, f'{label}: too large')
    return value


def read_json(path):
    def reject(value):
        raise ValueError(f'Non-finite JSON number: {value}')
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            reject(value)
        return result
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=reject,
                      parse_float=finite_float)


def package_file(root, value):
    require(isinstance(value, str) and value and '\\' not in value
            and ':' not in value, f'Invalid package-relative path: {value!r}')
    raw = Path(value)
    require(not raw.is_absolute() and '..' not in raw.parts,
            f'Path escapes package: {value}')
    result = (root / raw).resolve()
    require(result.is_relative_to(root), f'Path escapes package: {value}')
    require(result.is_file(), f'Missing package file: {value}')
    return result


def asset_list(value):
    if isinstance(value, str):
        value = value.strip()
        value = json.loads(value) if value.startswith('[') else re.split(r'[,;|\s]+', value)
    require(isinstance(value, list) and all(isinstance(x, str) for x in value),
            'asset_ids must be an array or a delimited CSV field')
    result = [x.strip() for x in value if x.strip()]
    require(len(result) == len(set(result)), 'Duplicate asset ID inside a shot')
    return result


def indexed(records, key, pattern, label):
    require(isinstance(records, list), f'{label}: expected an array')
    result = {}
    for item in records:
        require(isinstance(item, dict), f'{label}: expected objects')
        ident = item.get(key)
        require(isinstance(ident, str) and re.fullmatch(pattern, ident),
                f'{label}: invalid {key}: {ident!r}')
        require(ident not in result, f'{label}: duplicate {ident}')
        result[ident] = item
    return result


def compact(text):
    return re.sub(r'\s+', '', text)


def rect(value, label):
    require(isinstance(value, list) and len(value) == 4, f'{label}: expected [x,y,w,h]')
    x, y, w, h = [number(v, label, 0, 1) for v in value]
    require(w > 0 and h > 0 and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9,
            f'{label}: rectangle must lie inside its displayed asset')


def subtitle_chunks(text, limit=26):
    """Preserve every character; prefer sentence/clause boundaries over hard wrapping."""
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        boundary = [i + 1 for i, ch in enumerate(rest[:limit]) if ch in '。！？!?；;，,、\n']
        cut = boundary[-1] if boundary else limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    # A whitespace-only fragment belongs to its neighbour rather than an empty cue.
    merged = []
    for part in chunks:
        if merged and not part.strip():
            merged[-1] += part
        else:
            merged.append(part)
    if len(merged) > 1 and not merged[0].strip():
        prefix = merged.pop(0)
        merged[0] = prefix + merged[0]
    return merged


def auto_subtitles(narration, frame_count, fps):
    chunks = subtitle_chunks(narration)
    while len(chunks) > frame_count:
        chunks[-2:] = [''.join(chunks[-2:])]
    weights = [max(1, len(compact(s))) for s in chunks]
    total, cumulative, previous = sum(weights), 0, 0
    cues = []
    for i, (text, weight) in enumerate(zip(chunks, weights)):
        cumulative += weight
        end = frame_count if i == len(chunks) - 1 else min(
            frame_count - (len(chunks) - i - 1), max(previous + 1, round(frame_count * cumulative / total)))
        cues.append({'at': previous / fps, 'kind': 'subtitle', 'text': text,
                     'dur': (end - previous) / fps, 'timing_basis': 'estimated'})
        previous = end
    return cues


def timecode(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'


def plan_import(package, out, draft=False):
    root, out = Path(package).resolve(), Path(out).resolve()
    require(root.is_dir(), 'Package must be a directory')
    require(out != root and not out.is_relative_to(root), 'Output must be outside the source package')
    require(not out.exists() or (out.is_dir() and not any(out.iterdir())),
            'Output already exists and is not an empty directory; refusing to overwrite')
    handoff_path = package_file(root, 'video-handoff.json')
    handoff = read_json(handoff_path)
    require(isinstance(handoff, dict) and type(handoff.get('schema_version')) is int and handoff['schema_version'] == 1,
            'Expected video-handoff schema_version=1')
    require(isinstance(handoff.get('package_id'), str) and handoff['package_id'].strip(),
            'package_id is required')
    refs = {name: package_file(root, handoff.get(name))
            for name in ('manifest', 'storyboard', 'narration_file')}
    manifest = read_json(refs['manifest'])
    require(isinstance(manifest, dict) and type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1,
            'Expected manifest schema_version=1')
    manifest_assets = indexed(manifest.get('assets'), 'id', r'A\d{3,}', 'manifest assets')
    handoff_assets = indexed(handoff.get('assets'), 'id', r'A\d{3,}', 'handoff assets')
    with refs['storyboard'].open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    storyboard = indexed(rows, 'shot_id', r'S\d{3,}', 'storyboard')
    shots = indexed(handoff.get('shots'), 'shot_id', r'S\d{3,}', 'handoff shots')
    require(shots and list(shots) == list(storyboard),
            'Handoff must preserve every storyboard shot, in storyboard order')
    for ident, asset in manifest_assets.items():
        linked = asset.get('shot_ids')
        require(isinstance(linked, list) and linked and all(isinstance(x, str) for x in linked)
                and len(linked) == len(set(linked)) and set(linked) <= set(storyboard),
                f'{ident}: manifest shot_ids must reference existing unique storyboard shots')
    for shot_id, row in storyboard.items():
        for ident in asset_list(row.get('asset_ids', '')):
            require(ident in manifest_assets and shot_id in manifest_assets[ident]['shot_ids'],
                    f'{shot_id}: storyboard asset {ident} lacks a matching manifest reference')
    narration = refs['narration_file'].read_text(encoding='utf-8-sig')
    require(narration.strip(), 'Narration is empty')
    require(all(isinstance(s.get('narration'), str) and s['narration'].strip() for s in shots.values()),
            'Every shot requires nonempty narration')
    require(compact(narration) == compact(''.join(s['narration'] for s in shots.values())),
            'Shot narration does not cover narration_file exactly (ignoring whitespace)')

    canvas = handoff.get('canvas')
    require(isinstance(canvas, dict), 'canvas is required')
    width = number(canvas.get('width'), 'canvas.width', 320, 7680)
    height = number(canvas.get('height'), 'canvas.height', 240, 7680)
    require(width == int(width) and height == int(height) and width % 2 == height % 2 == 0,
            'Canvas dimensions must be even integers')
    fps = number(canvas.get('fps'), 'canvas.fps', 1, 120)
    require(fps == int(fps), 'canvas.fps must be a positive integer')
    fps = int(fps)
    safe = canvas.get('safe')
    require(isinstance(safe, dict), 'canvas.safe is required')
    safe = {key: number(safe.get(key, 0), f'safe.{key}', 0)
            for key in ('top', 'bottom', 'left', 'right')}
    require(safe['left'] + safe['right'] <= width - 160
            and safe['top'] + safe['bottom'] <= height - 160, 'Safe areas leave too little content space')
    require(safe['bottom'] >= max(height * 0.12, 140 * min(width, height) / 1080),
            'Subtitle safe.bottom is too small (need 12% of height and at least 140 scaled pixels)')
    require(isinstance(handoff.get('theme', {}), dict), 'theme must be an object')
    theme = {**DEFAULT_THEME, **handoff.get('theme', {})}
    require(all(isinstance(v, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', v)
                for v in theme.values()), 'Theme colors must use #RRGGBB')

    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError('Pillow is required to verify actual image dimensions') from exc
    copies, checked_assets = [], {}
    for ident, asset in handoff_assets.items():
        require(ident in manifest_assets, f'{ident}: missing from manifest')
        original = manifest_assets[ident]
        require(asset.get('file') == original.get('file'), f'{ident}: handoff file differs from manifest file')
        path = package_file(root, asset.get('file'))
        expected = asset.get('sha256')
        require(isinstance(expected, str) and re.fullmatch(r'[0-9a-fA-F]{64}', expected),
                f'{ident}: sha256 must be the actual file digest')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == expected.lower(), f'{ident}: SHA-256 mismatch')
        with Image.open(path) as im:
            im.load()
            require(getattr(im, 'n_frames', 1) == 1, f'{ident}: use an explicit still image, not animated media')
            iw, ih, fmt = im.width, im.height, im.format
        require(fmt in {'PNG', 'JPEG', 'WEBP'}, f'{ident}: supported formats are PNG/JPEG/WebP')
        for key, measured in (('width', iw), ('height', ih)):
            require(number(asset.get(key), f'{ident}.{key}', 1) == measured,
                    f'{ident}: {key} differs from actual image')
        suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[fmt]
        target = f'assets/{ident}{suffix}'
        copies.append((path, target))
        checked_assets[ident] = {'id': ident, 'file': target, 'sha256': digest,
                                 'width': iw, 'height': ih,
                                 'original_package_file': asset['file'],
                                 'status': original.get('status'), 'visual_review': original.get('visual_review'),
                                 'license_status': original.get('license_status'), 'kind': original.get('kind')}

    source_path = root / 'sources/source-log.json'
    sources = []
    if source_path.exists():
        source_path = package_file(root, 'sources/source-log.json')
        sources = read_json(source_path)
        require(isinstance(sources, list) and all(isinstance(x, dict) for x in sources),
                'source-log.json must contain an array of objects')
    presentation = handoff.get('presentation', {})
    require(isinstance(presentation, dict), 'presentation must be an object')
    show_credits = presentation.get('show_source_credits', False)
    require(isinstance(show_credits, bool), 'show_source_credits must be boolean')
    show_labels = presentation.get('show_image_labels', False)
    require(isinstance(show_labels, bool), 'show_image_labels must be boolean')
    image_enter = presentation.get('image_enter', 'fade-up')
    require(isinstance(image_enter, str) and image_enter in {'fade-up', 'fade', 'zoom-in', 'none'}, 'invalid image_enter')
    enter_duration = number(presentation.get('image_enter_duration', 0.6), 'image_enter_duration', 0.1, 2)
    attribution, source_by_id = {}, {}
    for item in sources:
        source_by_id[item.get('asset_id')] = item
        short = item.get('attribution_short')
        full = item.get('attribution')
        value = short if isinstance(short, str) and short.strip() else (
            full if isinstance(full, str) and len(full.strip()) <= 72 else None)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in {'unknown', '未知', '未核实', 'n/a'}:
            attribution[item.get('asset_id')] = value.strip()

    clips, timeline, subtitles, readiness = {}, [], [], {}
    current_frame = 0
    for shot_id, shot in shots.items():
        row = storyboard[shot_id]
        require(shot['narration'] == row.get('narration'), f'{shot_id}: narration differs from storyboard')
        estimate = number(shot.get('duration_estimate_s'), f'{shot_id}.duration_estimate_s', 0.1, 3600)
        try:
            original_estimate = float(row.get('duration_estimate_s', ''))
        except ValueError as exc:
            raise ValueError(f'{shot_id}: invalid storyboard duration') from exc
        number(original_estimate, f'{shot_id}.storyboard duration', 0.1, 3600)
        require(abs(estimate - original_estimate) <= 1e-9, f'{shot_id}: duration differs from storyboard estimate')
        frame_count = round(estimate * fps)
        require(frame_count >= 1, f'{shot_id}: duration produces no frames')
        duration = frame_count / fps
        selected = asset_list(shot.get('asset_ids'))
        original_ids = asset_list(row.get('asset_ids', ''))
        require(set(selected) <= set(original_ids), f'{shot_id}: selected assets are absent from original storyboard')
        layout = shot.get('layout')
        require(layout in {'single', 'compare', 'text'}, f'{shot_id}: invalid layout')
        require(len(selected) == {'single': 1, 'compare': 2, 'text': 0}[layout],
                f'{shot_id}: asset count does not match layout')
        if layout == 'text':
            require(isinstance(shot.get('visual_reason'), str) and shot['visual_reason'].strip(),
                    f'{shot_id}: text layout requires explicit visual_reason')
        image_cues = []
        for i, ident in enumerate(selected):
            require(ident in checked_assets, f'{shot_id}: {ident} missing from handoff assets')
            original = manifest_assets[ident]
            require(isinstance(original.get('shot_ids'), list) and shot_id in original['shot_ids'],
                    f'{shot_id}: {ident} manifest shot_ids does not reference this shot')
            failures = []
            if original.get('status') != 'checked': failures.append('status is not checked')
            if original.get('visual_review') != 'pass': failures.append('visual review is not pass')
            kind = original.get('kind')
            require(kind in {'sourced', 'user-provided', 'generated'}, f'{ident}: invalid manifest kind')
            if kind in {'sourced', 'user-provided'} and original.get('license_status') != 'terms-checked':
                failures.append('external source terms are not checked')
            if kind == 'sourced' and not str(original.get('source_page') or '').strip():
                failures.append('sourced material has no source_page')
            if kind in {'sourced', 'user-provided'}:
                if original.get('license_status') == 'terms-checked' and not str(original.get('license_url') or '').strip():
                    failures.append('terms-checked material has no license_url')
                source_record = source_by_id.get(ident)
                if not source_record:
                    failures.append('external material has no source-log record')
                elif not str(source_record.get('source_page') or '').strip() or not str(source_record.get('license_url') or '').strip():
                    failures.append('source-log record lacks source or applicable terms reference')
                elif source_record.get('license_status') != 'terms-checked':
                    failures.append('source-log terms are not checked')
            if kind == 'generated':
                require(original.get('license_status') in {'generated-no-clearance-claim', 'terms-checked'},
                        f'{ident}: generated asset lacks recorded status')
                package_file(root, original.get('prompt_file'))
            readiness[ident] = failures
            require(draft or not failures, f'{ident}: not ready ({"; ".join(failures)}); use --draft for a marked preview')
            box = [0, 0, 1, 1] if layout == 'single' else [i * 0.515, 0, 0.485, 1]
            q = {'at': 0, 'kind': 'image', 'asset_id': ident,
                 'image': '../' + checked_assets[ident]['file'], 'data': {'box': box}}
            label = handoff_assets[ident].get('display_label', '')
            require(isinstance(label, str), f'{ident}: display_label must be a string')
            if show_labels and label.strip():
                q['data']['label'] = label.strip()
            source = source_by_id.get(ident, {})
            # Diagrams and screenshots enter without displacement to preserve readability.
            if source.get('media_type') in {'diagram', 'screenshot'} and 'image_enter' not in presentation:
                q['data']['enter'] = 'fade'
            if show_credits and ident in attribution:
                q['text'] = attribution[ident]
            image_cues.append(q)

        raw_cues = shot.get('cues', [])
        require(isinstance(raw_cues, list), f'{shot_id}: cues must be an array')
        cues = []
        for raw in raw_cues:
            require(isinstance(raw, dict), f'{shot_id}: cue must be an object')
            require(set(raw) <= {'at', 'kind', 'text', 'sub', 'dur', 'data', 'anchor_text', 'anchor_occurrence'},
                    f'{shot_id}: unsupported cue field; images cannot be redefined')
            q = copy.deepcopy(raw)
            kind = q.get('kind')
            require(kind in ALLOWED_CUES, f'{shot_id}: unsupported cue kind {kind!r}')
            at = number(q.get('at'), f'{shot_id} cue.at', 0)
            require(at < duration, f'{shot_id}: cue begins at/after the final frame boundary')
            if 'dur' in q:
                dur = number(q['dur'], f'{shot_id} cue.dur', 1 / fps)
                require(at + dur <= duration + 1e-9, f'{shot_id}: cue exceeds clip duration')
            if 'anchor_text' in q:
                require(isinstance(q['anchor_text'], str) and q['anchor_text'] in shot['narration'],
                        f'{shot_id}: anchor_text must occur in narration')
            if 'anchor_occurrence' in q:
                occurrence = q['anchor_occurrence']
                require(type(occurrence) is int and occurrence > 0 and bool(q.get('anchor_text')),
                        f'{shot_id}: anchor_occurrence requires anchor_text and a positive integer')
                matches = len(list(re.finditer('(?=' + re.escape(q['anchor_text']) + ')', shot['narration'])))
                require(occurrence <= matches, f'{shot_id}: anchor_occurrence exceeds the narration matches')
            data = q.get('data', {})
            require(isinstance(data, dict), f'{shot_id}: cue.data must be an object')
            if kind in {'highlight', 'focus', 'reset'}:
                require(layout != 'text' and data.get('asset_id') in selected,
                        f'{shot_id}: annotation target must be one of the displayed assets')
                require(set(data) <= {'asset_id', 'rect', 'style'}, f'{shot_id}: unsupported annotation field')
                if kind != 'reset': rect(data.get('rect'), f'{shot_id} {kind}.rect')
                if 'style' in data:
                    require(data['style'] in {'circle', 'rect', 'underline'},
                            f'{shot_id}: unsupported annotation style')
            else:
                require(isinstance(q.get('text'), str) and q['text'].strip(), f'{shot_id}: {kind} needs text')
                require(not data, f'{shot_id}: {kind} does not accept annotation data')
            if kind == 'subtitle':
                require('dur' in q, f'{shot_id}: subtitle needs explicit dur')
                q['timing_basis'] = 'estimated'
            cues.append(q)
        title = shot.get('title', '')
        require(isinstance(title, str), f'{shot_id}: title must be text')
        # shot.title is the main heading; an extra title cue is permitted only when it changes later.
        if title.strip():
            require(not any(q['kind'] == 'title' and q['at'] == 0 for q in cues),
                    f'{shot_id}: duplicate initial title; use shot.title')
            cues.insert(0, {'at': 0, 'kind': 'title', 'text': title})
        require(layout != 'text' or any(q['kind'] in {'title', 'point'} for q in cues),
                f'{shot_id}: text layout needs a meaningful title or point')
        local_subtitles = sorted((q for q in cues if q['kind'] == 'subtitle'), key=lambda q: q['at'])
        if not local_subtitles:
            local_subtitles = auto_subtitles(shot['narration'], frame_count, fps)
            cues.extend(local_subtitles)
        require(compact(''.join(q['text'] for q in local_subtitles)) == compact(shot['narration']),
                f'{shot_id}: subtitles must preserve the complete narration in order')
        last_end = 0
        for q in local_subtitles:
            require(q['at'] >= last_end - 1e-9, f'{shot_id}: overlapping subtitle intervals')
            last_end = q['at'] + q['dur']
            subtitles.append({'start_s': current_frame / fps + q['at'],
                              'end_s': current_frame / fps + last_end, 'text': q['text'], 'shot_id': shot_id})
        spec = {'grammar': 'y4_media_board', 'duration': duration, 'fps': fps,
                'width': int(width), 'height': int(height), 'safe': safe, 'theme': theme,
                'data': {'draft': bool(draft), 'watermark': PREVIEW_LABEL if draft else '',
                         'timing_basis': 'estimated', 'shot_id': shot_id,
                         'show_source_credits': show_credits,
                         'show_image_labels': show_labels,
                         'image_enter': image_enter, 'image_enter_duration': enter_duration},
                'cues': sorted(image_cues + cues, key=lambda q: q['at'])}
        # Use the shipped renderer's pure configuration checks before creating files;
        # this also checks title/point reservations and compare-image box overlap.
        from run import _media_board
        _media_board(spec)
        clips[f'clips/{shot_id}.json'] = spec
        timeline.append({'shot_id': shot_id, 'clip': f'clips/{shot_id}.json',
                         'start_s': current_frame / fps, 'end_s': (current_frame + frame_count) / fps,
                         'start_frame': current_frame, 'frame_count': frame_count,
                         'duration_estimate_s': estimate, 'timing_basis': 'estimated',
                         'narration': shot['narration'], 'asset_ids': selected,
                         'layout': layout, 'visual_reason': shot.get('visual_reason', '')})
        current_frame += frame_count

    snapshots = [(handoff_path, 'source-package/video-handoff.json')]
    for name, path in refs.items():
        snapshots.append((path, 'source-package/' + handoff[name]))
    for relative in ('sources/source-log.json', 'sources/source-plan.csv', 'style-lock.md', 'editing-notes.md'):
        if (root / relative).exists():
            snapshots.append((package_file(root, relative), 'source-package/' + relative))
    # Preserve generation/drawing briefs for any generated asset actually used.
    for ident in readiness:
        brief = manifest_assets[ident].get('prompt_file')
        if brief:
            snapshots.append((package_file(root, brief), 'source-package/' + brief))
    credit_sections = []
    for ident in readiness:
        original = manifest_assets[ident]
        if original.get('kind') not in {'sourced', 'user-provided'}:
            continue
        source = source_by_id.get(ident, {})
        lines = [ident]
        for label, value in (
            ('Attribution', source.get('attribution') or source.get('attribution_short')),
            ('Creator', source.get('creator')),
            ('Title', source.get('title')),
            ('Source', source.get('source_page') or original.get('source_page')),
            ('License', source.get('license_name')),
            ('Terms reference', source.get('license_url') or original.get('license_url')),
            ('Restrictions', source.get('restrictions')),
            ('Recorded status', original.get('license_status')),
        ):
            if value:
                lines.append(f'{label}: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))
        if not source:
            lines.append('Source-log record: missing; preview only')
        credit_sections.append('\n'.join(lines))
    credits = ('Source information carried forward from the material package; no new rights clearance is claimed.\n\n'
               + '\n\n'.join(credit_sections) + '\n')
    report = {'schema_version': 1, 'package_id': handoff['package_id'],
              'draft': bool(draft), 'status': 'draft-preview' if draft else 'imported',
              'timing_basis': 'estimated', 'audio': 'none', 'rendered': False,
              'source_statuses_preserved': True, 'rights_note': 'Existing source status is carried forward; this is not copyright clearance.',
              'credits_file': 'credits.txt',
              'missing_source_records': [ident for ident in readiness
                                         if manifest_assets[ident].get('kind') in {'sourced', 'user-provided'}
                                         and ident not in source_by_id],
              'used_assets': [checked_assets[x] for x in readiness],
              'readiness_issues': {x: issues for x, issues in readiness.items() if issues},
              'shot_count': len(timeline), 'total_frames': current_frame,
              'duration_s': current_frame / fps, 'subtitle_timing': 'estimated; not speech-aligned',
              'source_package': str(root)}
    srt = '\n\n'.join(f'{i}\n{timecode(s["start_s"])} --> {timecode(s["end_s"])}\n{s["text"].strip()}'
                       for i, s in enumerate(subtitles, 1)) + '\n'
    return {'out': out, 'copies': copies + snapshots, 'clips': clips, 'narration': narration,
            'srt': srt, 'credits': credits, 'timeline': {'schema_version': 1, 'package_id': handoff['package_id'],
                                  'timing_basis': 'estimated', 'fps': fps, 'shots': timeline,
                                  'total_frames': current_frame, 'duration_s': current_frame / fps},
            'report': report}


def import_package(package, out, draft=False):
    plan = plan_import(package, out, draft)
    target = plan['out']
    # Recheck immediately before writing; never reuse or erase an existing project.
    require(not target.exists() or (target.is_dir() and not any(target.iterdir())),
            'Output changed during validation; refusing to overwrite')
    target.mkdir(parents=True, exist_ok=True)
    for source, relative in plan['copies']:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for relative, value in {**plan['clips'], 'timeline.json': plan['timeline'],
                            'import-report.json': plan['report']}.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (target / 'narration.txt').write_text(plan['narration'], encoding='utf-8')
    (target / 'subtitles.srt').write_text(plan['srt'], encoding='utf-8')
    (target / 'credits.txt').write_text(plan['credits'], encoding='utf-8')
    return plan['report']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--draft', action='store_true', help='Allow non-ready assets with an explicit preview label')
    args = parser.parse_args()
    try:
        report = import_package(args.package, args.out, args.draft)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Import rejected: {exc}\n')
    print(f'Imported {report["shot_count"]} shots; estimated duration {report["duration_s"]:.3f}s. '
          f'Status: {report["status"]}. No video or audio was produced.')


if __name__ == '__main__':
    main()
