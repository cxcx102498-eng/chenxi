"""Probe, transcribe and align an imported project to its complete original recording.

Only standard-library modules are imported at startup. FFmpeg (or imageio_ffmpeg)
is needed for probe/align; faster-whisper is needed only for a new transcription.
Alignment is local to ASR words/segments, never a global duration scale.
"""
import argparse
import array
import copy
import csv
import difflib
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class AlignmentError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise AlignmentError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def norm(text):
    # Single Arabic digits and chapter-number characters are equivalent for matching.
    digit_map = str.maketrans('0123456789', '零一二三四五六七八九')
    return ''.join(c.lower() for c in text.translate(digit_map) if c.isalnum())


def ffmpeg_path():
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise AlignmentError('Install imageio-ffmpeg or put ffmpeg on PATH.') from exc


def probe_audio(audio):
    audio = Path(audio).resolve()
    require(audio.is_file(), f'Audio file does not exist: {audio}')
    # Downmix only the verification stream: sample rate and sample-frame count stay
    # native. The delivered input file is copied byte-for-byte, never downmixed.
    command = [ffmpeg_path(), '-hide_banner', '-nostdin', '-i', str(audio),
               '-map', '0:a:0', '-vn', '-ac', '1', '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1']
    count, peak = 0, 0.0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        while True:
            block = process.stdout.read(262144)
            if not block:
                break
            count += len(block)
            require(len(block) % 4 == 0, 'Partial PCM sample returned by FFmpeg.')
            values = array.array('f')
            values.frombytes(block)
            if sys.byteorder != 'little':
                values.byteswap()
            if values:
                peak = max(peak, max(abs(v) for v in values))
        code = process.wait()
        errors.seek(0)
        log = errors.read().decode('utf-8', errors='replace')
    require(code == 0, f'FFmpeg could not decode the complete audio: {log[-1500:]}')
    rates = re.findall(r'Audio: pcm_f32le[^\n]*?,\s*(\d+) Hz', log)
    require(bool(rates), 'Could not determine the decoded sample rate from FFmpeg.')
    rate, samples = int(rates[-1]), count // 4
    require(samples > 0, 'The audio has no decoded samples.')
    return {'file': audio.name, 'sha256': sha256(audio), 'sample_rate': rate,
            'decoded_samples': samples, 'duration_s': samples / rate,
            'verification_channels': 1, 'peak_dbfs': 20 * math.log10(peak) if peak else None,
            'probe_method': 'Complete native-rate FFmpeg float PCM decode; mono verification stream'}


def transcribe_audio(audio, out, model='small', language='zh'):
    metadata = probe_audio(audio)
    out = Path(out)
    options = {'model':model, 'language':language or None, 'device':'cpu', 'compute_type':'int8',
               'word_timestamps':True, 'vad_filter':True, 'beam_size':5}
    if out.is_file():
        existing = read(out)
        if (existing.get('sha256') == metadata['sha256'] and
                existing.get('asr_options') == options and
                abs(existing.get('duration_s', -1) - metadata['duration_s']) <= 1 / metadata['sample_rate']):
            transcript_characters(existing, metadata['duration_s'])
            return {'status': 'reused_exact_audio', 'transcript': str(out), **metadata}
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AlignmentError('Install faster-whisper, or supply a transcript JSON bound to this audio SHA-256.') from exc
    worker = WhisperModel(model, device='cpu', compute_type='int8')
    segments, info = worker.transcribe(str(audio), language=language or None,
                                       word_timestamps=True, vad_filter=True, beam_size=5)
    result = {**metadata, 'language': info.language, 'model': model, 'asr_options':options, 'segments': []}
    for segment in segments:
        result['segments'].append({'start': segment.start, 'end': segment.end, 'text': segment.text,
                                  'words': [{'word': w.word, 'start': w.start, 'end': w.end,
                                             'probability': w.probability} for w in segment.words or []]})
    transcript_characters(result, metadata['duration_s'])
    save(out, result)
    return {'status': 'transcribed', 'transcript': str(out), **metadata}


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def transcript_characters(transcript, duration):
    characters, times, risks = [], [], []
    previous_end = 0.0
    require(isinstance(transcript.get('segments'), list) and transcript['segments'], 'Transcript has no segments.')
    for index, segment in enumerate(transcript['segments']):
        start, end = segment.get('start'), segment.get('end')
        require(finite(start) and finite(end) and 0 <= start <= end <= duration + .02,
                f'Segment {index} has invalid timestamps.')
        words = segment.get('words')
        if not words:
            words = [{'start': start, 'end': end, 'word': segment.get('text', '')}]
            risks.append({'kind': 'segment_only_timing', 'segment': index,
                          'note': 'Character timing is locally interpolated within this ASR segment.'})
        for word in words:
            a, b = word.get('start'), word.get('end')
            require(finite(a) and finite(b) and 0 <= a <= b <= duration + .02,
                    f'Segment {index} has an invalid word timestamp.')
            require(a >= start - .02 and b <= end + .02, f'Word falls outside segment {index}.')
            require(a >= previous_end - .02, f'Non-monotonic word timestamps at segment {index}.')
            # A small timestamp rounding overlap can be removed locally, never stretched.
            a, b = max(a, previous_end), max(b, previous_end)
            text = norm(word.get('word', word.get('text', '')))
            if not text:
                continue
            if a == b:
                risks.append({'kind': 'zero_duration_word', 'segment': index, 'text': text, 'at': a})
            for j, char in enumerate(text):
                characters.append(char)
                times.append((a + (b - a) * j / len(text), a + (b - a) * (j + 1) / len(text)))
            previous_end = b
    require(characters, 'Transcript contains no spoken text.')
    return ''.join(characters), times, risks


def map_reference(reference, actual, times, duration):
    require(reference, 'Reference narration contains no spoken text.')
    mapped, differences, exact = [None] * len(reference), [], 0
    opcodes = difflib.SequenceMatcher(None, reference, actual, autojunk=False).get_opcodes()
    changed = sum(max(b-a, d-c) for kind, a, b, c, d in opcodes if kind != 'equal')
    ratio = changed / max(len(reference), len(actual))
    for kind, a, b, c, d in opcodes:
        if kind == 'equal':
            exact += b - a
            mapped[a:b] = times[c:d]
            continue
        begin = times[c][0] if c < len(times) else times[-1][1]
        end = times[d-1][1] if d > c else begin
        item = {'kind': kind, 'reference': reference[a:b], 'asr': actual[c:d],
                'reference_range': [a, b], 'time': [begin, end],
                'context': reference[max(0, a-12):min(len(reference), b+12)]}
        differences.append(item)
        # Never manufacture a missing sentence/fragment from surrounding timestamps.
        allowed = max(b-a, d-c) <= 3 and (kind != 'delete' or b-a == 1)
        allowed &= not (kind == 'delete' and (a == 0 or b == len(reference)))
        if not allowed:
            item['rejected'] = 'Missing fragment or more than a small local ASR discrepancy.'
        if b > a and end <= begin:
            begin = times[c-1][1] if c else 0
            end = times[c][0] if c < len(times) else duration
            item['time'] = [begin, end]
        if b > a:
            for j in range(b-a):
                mapped[a+j] = (begin + (end-begin)*j/(b-a), begin + (end-begin)*(j+1)/(b-a))
    report = {'normalized_match_fraction': exact / len(reference), 'changed_fraction': ratio,
              'method': 'Reference sequence matching; local ASR word/segment interpolation; no global scaling',
              'differences': differences}
    errors = [item for item in differences if 'rejected' in item]
    if ratio > .05 or errors:
        raise AlignmentErrorWithReport('Narration and recording differ too much or contain a missing fragment; review the diagnostic before retrying.', report)
    require(all(value is not None for value in mapped), 'Some reference characters lack a local timestamp.')
    return mapped, report


class AlignmentErrorWithReport(AlignmentError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def subtitle_chunks(text, limit=26):
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        positions = [i+1 for i, char in enumerate(text[:limit]) if char in '。！？!?；;，,、\n']
        cut = positions[-1] if positions else limit
        chunks.append(text[:cut])
        text = text[cut:]
    merged = []
    for chunk in chunks:
        if not norm(chunk) and merged:
            merged[-1] += chunk
        else:
            merged.append(chunk)
    if len(merged) > 1 and not norm(merged[0]):
        prefix = merged.pop(0)
        merged[0] = prefix + merged[0]
    return merged


def local_path(root, path):
    candidate = (root / path).resolve()
    require(candidate.is_relative_to(root), f'Project path escapes its root: {path}')
    return candidate


def build_alignment(timeline, specs, handoff, transcript, audio):
    tl = copy.deepcopy(timeline)
    fps = tl.get('fps')
    require(finite(fps) and fps > 0, 'Timeline fps must be positive.')
    require(transcript.get('sha256') == audio['sha256'], 'Transcript SHA-256 does not match the supplied recording.')
    require(finite(transcript.get('duration_s')) and abs(transcript['duration_s'] - audio['duration_s']) <= max(.002, 1/audio['sample_rate']),
            'Transcript decoded duration differs from the supplied recording.')
    actual, times, risks = transcript_characters(transcript, audio['duration_s'])
    shots = tl.get('shots', [])
    require(shots, 'Timeline contains no shots.')
    reference = norm(''.join(s['narration'] for s in shots))
    mapped, difference_report = map_reference(reference, actual, times, audio['duration_s'])
    source = {s['shot_id']: s for s in handoff.get('shots', [])}
    offsets, offset = [], 0
    for shot in shots:
        text = norm(shot['narration'])
        require(text, f"{shot['shot_id']}: narration has no spoken text.")
        offsets.append(offset)
        shot['speech_start_s'], shot['speech_end_s'] = mapped[offset][0], mapped[offset+len(text)-1][1]
        offset += len(text)
        source_shot = source.get(shot['shot_id'], {})
        for key in ('scene_group_id', 'state_order'):
            if key not in shot and key in source_shot:
                shot[key] = source_shot[key]
    frames = math.ceil(audio['duration_s'] * fps)
    starts = [0] + [round((a['speech_end_s'] + b['speech_start_s']) / 2 * fps) for a,b in zip(shots, shots[1:])]
    ends = starts[1:] + [frames]
    require(all(0 <= a < b <= frames for a,b in zip(starts, ends)), 'Audio boundaries cannot allocate at least one frame per shot.')
    # Adjacent state group / identical-asset runs are connected components.
    groups, group = [], []
    for i, shot in enumerate(shots):
        linked = i > 0 and ((shot.get('scene_group_id') and shot.get('scene_group_id') == shots[i-1].get('scene_group_id')) or
                           (shot.get('asset_ids') and shot.get('asset_ids') == shots[i-1].get('asset_ids')))
        if not linked and group:
            groups.append(group)
            group = []
        group.append(i)
    if group:
        groups.append(group)
    stable, reservations = set(), {}
    for group in groups:
        if len(group) < 2:
            continue
        stable.update(group)
        group_specs = [specs[shots[i]['shot_id']] for i in group]
        signature = lambda spec: (spec.get('width'), spec.get('height'), spec.get('safe'),
                                  [q.get('data', {}).get('box') for q in spec['cues'] if q['kind'] == 'image'])
        require(all(signature(spec) == signature(group_specs[0]) for spec in group_specs),
                'Continuous state group has incompatible image layout/safe areas; fix the source package.')
        for i in group:
            reservations[i] = {f'reserve_{kind}': any(spec.get('data', {}).get(f'reserve_{kind}') or
                              any(q['kind'] == kind for q in spec['cues']) for spec in group_specs)
                               for kind in ('title', 'point')}
    all_captions, anchors, changes, story, outputs = [], [], [], [], {}
    basis = 'recorded_audio_reference_aligned'
    for i, shot in enumerate(shots):
        sid, offset, sf, ef = shot['shot_id'], offsets[i], starts[i], ends[i]
        narration, duration = shot['narration'], (ef-sf)/fps
        shot.update(start_frame=sf, frame_count=ef-sf, start_s=sf/fps, end_s=ef/fps, timing_basis=basis)
        spec = copy.deepcopy(specs[sid])
        require(spec.get('fps') == fps, f'{sid}: clip fps differs from timeline fps.')
        spec['duration'] = duration
        spec.setdefault('data', {}).update(timing_basis=basis, **reservations.get(i, {}))
        mode = 'none' if i in stable or duration < .1 else 'fade'
        # Entry configuration has a >=0.1s schema minimum. The renderer limits the
        # actual animation to the shot duration; sub-0.1s shots have no entry fade.
        enter_duration = max(.1, min(.4, duration))
        spec['data'].update(image_enter=mode, image_enter_duration=enter_duration)
        changes.append({'shot_id': sid, 'image_enter': mode, 'layout_reservation': reservations.get(i, {}),
                        'reason': 'continuous state or repeated asset' if i in stable else 'independent explanatory scene'})
        cues, previous_anchor = [], 0
        for original in spec['cues']:
            if original['kind'] == 'subtitle':
                continue
            cue = copy.deepcopy(original)
            anchor = norm(cue.get('anchor_text', ''))
            if anchor:
                positions = [m.start() for m in re.finditer('(?='+re.escape(anchor)+')', norm(narration))]
                require(positions, f'{sid}: anchor is absent from narration: {cue["anchor_text"]}')
                occurrence = cue.get('anchor_occurrence')
                if occurrence is not None:
                    require(type(occurrence) is int and 1 <= occurrence <= len(positions), f'{sid}: invalid anchor_occurrence.')
                    position = positions[occurrence-1]
                else:
                    position = next((p for p in positions if p >= previous_anchor), positions[0])
                if len(positions) > 1:
                    risks.append({'kind': 'repeated_anchor', 'shot_id': sid, 'anchor_text': cue['anchor_text'],
                                  'positions': positions, 'selected_occurrence': positions.index(position)+1,
                                  'resolution': 'explicit occurrence' if occurrence else 'cue order; review this emphasis frame'})
                previous_anchor = position
                cue['at'] = max(0, (round(mapped[offset+position][0]*fps)-sf)/fps)
            elif cue.get('at', 0) != 0:
                raise AlignmentError(f'{sid}: timed {cue["kind"]} cue needs anchor_text; estimated times cannot be globally rescaled.')
            if cue['kind'] == 'highlight':
                cue['dur'] = min(.45 if cue.get('data', {}).get('style') == 'underline' else .65, max(1/fps, duration-cue['at']))
            if 'dur' in cue:
                cue['dur'] = min(cue['dur'], duration-cue.get('at', 0))
            require(0 <= cue.get('at', 0) < duration and ('dur' not in cue or cue['dur'] > 0), f'{sid}: cue has no drawable interval after alignment.')
            if cue['kind'] == 'image':
                cue.setdefault('data', {}).update(enter=mode, enter_duration=enter_duration)
            if anchor:
                anchors.append({'shot_id': sid, 'anchor_text': cue['anchor_text'], 'kind': cue['kind'],
                                'local_s': cue['at'], 'global_s': shot['start_s']+cue['at'], 'dur': cue.get('dur')})
            cues.append(cue)
        original_subtitles = sorted((q for q in spec['cues'] if q['kind'] == 'subtitle'), key=lambda q:q['at'])
        parts = [q['text'] for q in original_subtitles] or subtitle_chunks(narration)
        require(re.sub(r'\s+', '', ''.join(parts)) == re.sub(r'\s+', '', narration), f'{sid}: subtitles do not preserve the reference narration.')
        captions, cursor = [], 0
        for part in parts:
            length = len(norm(part))
            require(length > 0, f'{sid}: subtitle has no spoken characters.')
            begin, finish = offset+cursor, offset+cursor+length-1
            cs, ce = max(sf, round(mapped[begin][0]*fps)), min(ef, round((mapped[finish][1]+.12)*fps))
            captions.append({'text': part, 'start_frame': cs, 'end_frame': ce, 'shot_id': sid, 'timing_basis': basis})
            cursor += length
        for j, caption in enumerate(captions):
            if j+1 < len(captions):
                caption['end_frame'] = min(caption['end_frame'], captions[j+1]['start_frame'])
            require(caption['end_frame'] > caption['start_frame'], f'{sid}: subtitle cannot fit valid audio timing; inspect ASR timestamps.')
            caption.update(start_s=caption['start_frame']/fps, end_s=caption['end_frame']/fps)
            cues.append({'kind': 'subtitle', 'at': (caption['start_frame']-sf)/fps,
                         'dur': (caption['end_frame']-caption['start_frame'])/fps, 'text': caption['text'], 'timing_basis': basis})
        all_captions.extend(captions)
        spec['cues'] = sorted(cues, key=lambda q:(q.get('at', 0), {'image':0, 'title':1}.get(q['kind'],2)))
        outputs[sid] = spec
        story.append({'shot_id':sid, 'start_s':sf/fps, 'end_s':ef/fps, 'duration_s':duration,
                      'duration_estimate_s':shot.get('duration_estimate_s'), 'narration':narration,
                      'asset_ids':','.join(shot.get('asset_ids', [])), 'scene_group_id':shot.get('scene_group_id', ''),
                      'state_order':shot.get('state_order', ''), 'image_enter':mode, 'timing_basis':basis})
    require(all(a['end_frame'] <= b['start_frame'] for a,b in zip(all_captions, all_captions[1:])), 'Subtitle intervals overlap.')
    require(norm(''.join(c['text'] for c in all_captions)) == reference, 'Subtitle coverage is incomplete.')
    tl.update(timing_basis=basis, total_frames=frames, duration_s=frames/fps, subtitle_timing='Reference aligned to recorded audio; locally interpolated within words or segments')
    tl['audio'] = {'file':'audio/'+audio['file'], 'duration_s':audio['duration_s'], 'sha256':audio['sha256'],
                   'sample_rate':audio['sample_rate'], 'decoded_samples':audio['decoded_samples'],
                   'status':'user_provided_full_narration', 'tail_pad_s':frames/fps-audio['duration_s']}
    return {'timeline':tl, 'clips':outputs, 'subtitles':all_captions, 'differences':difference_report,
            'risks':risks, 'anchors':anchors, 'visual_adjustments':changes, 'storyboard':story}


def stamp(frame, fps, separator=','):
    millis = round(frame/fps*1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}'


def timeline_edit_report(initial, current):
    """Record structural/reference edits without replacing them from the snapshot."""
    old = {s['shot_id']:s for s in initial['shots']}
    new = {s['shot_id']:s for s in current['shots']}
    require(len(new) == len(current['shots']), 'Current timeline has duplicate shot IDs.')
    changes = []
    before, after = list(old), list(new)
    if before != after:
        changes.append({'kind':'shot_order_or_set_changed', 'initial':before, 'current':after})
    if initial.get('fps') != current.get('fps'):
        changes.append({'kind':'fps_changed', 'initial':initial.get('fps'), 'current':current.get('fps')})
    for sid in old.keys() & new.keys():
        for field in ('narration','asset_ids','clip','layout'):
            if old[sid].get(field) != new[sid].get(field):
                changes.append({'kind':'shot_field_changed', 'shot_id':sid, 'field':field,
                                'initial':old[sid].get(field), 'current':new[sid].get(field)})
    return changes


def align_project(project, audio_path, transcript_path):
    root = Path(project).resolve()
    folder = root/'alignment'
    try:
        audio = probe_audio(audio_path)
        transcript = read(transcript_path)
        upstream = folder/'upstream-timeline.json'
        # Current project files are authoritative for narration, subtitle grouping,
        # anchors and visuals. The initial estimate snapshot is an audit artifact,
        # never a restore source during re-alignment.
        tl = read(root/'timeline.json')
        initial = read(upstream) if upstream.exists() else tl
        project_changes = timeline_edit_report(initial, tl)
        specs = {s['shot_id']: read(local_path(root,s['clip'])) for s in tl['shots']}
        handoff_path = root/'source-package/video-handoff.json'
        handoff = read(handoff_path) if handoff_path.exists() else {'shots':[]}
        result = build_alignment(tl, specs, handoff, transcript, audio)
        # Stage all validation before modifying the imported project.
        if not upstream.exists():
            save(upstream, tl)
            for sid, spec in specs.items():
                save(folder/'upstream-clips'/f'{sid}.json', spec)
        target = root/'audio'/audio['file']
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(audio_path).resolve() != target.resolve():
            require(not target.exists() or sha256(target) == audio['sha256'], 'Audio destination already contains a different recording.')
            shutil.copy2(audio_path, target)
        save(root/'audio/transcript.json', transcript)
        save(root/'timeline.json', result['timeline'])
        for shot in result['timeline']['shots']:
            save(local_path(root, shot['clip']), result['clips'][shot['shot_id']])
        save(root/'subtitles.json', result['subtitles'])
        fps = tl['fps']
        srt = '\n\n'.join(f'{i}\n{stamp(c["start_frame"],fps)} --> {stamp(c["end_frame"],fps)}\n{c["text"]}' for i,c in enumerate(result['subtitles'],1))+'\n'
        vtt = 'WEBVTT\n\n'+'\n\n'.join(f'{stamp(c["start_frame"],fps,".")} --> {stamp(c["end_frame"],fps,".")}\n{c["text"]}' for c in result['subtitles'])+'\n'
        (root/'subtitles.srt').write_text(srt, encoding='utf-8-sig')
        (root/'subtitles.vtt').write_text(vtt, encoding='utf-8')
        with (root/'storyboard-actual.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer=csv.DictWriter(handle, fieldnames=result['storyboard'][0].keys())
            writer.writeheader()
            writer.writerows(result['storyboard'])
        save(folder/'asr-text-differences.json', result['differences'])
        save(folder/'anchor-times.json', result['anchors'])
        save(folder/'visual-adjustments.json', result['visual_adjustments'])
        save(folder/'project-edit-report.json', {'current_project_preserved':True,
                                                'initial_estimate_snapshot_unchanged':True,
                                                'changes':project_changes})
        save(folder/'audio-provenance.json', {**audio, 'file':result['timeline']['audio']['file'], 'source_path':str(Path(audio_path).resolve()),
                                            'new_voice_generated':False, 'recording_modified':False,
                                            'subtitle_note':'Approved narration retained; see ASR difference report. No phoneme-level accuracy claim.',
                                            'transcript_bound_by':'exact SHA-256 and complete decoded duration'})
        diagnostic={'status':'pass', 'risks':result['risks'], 'differences':result['differences'],
                    'project_changes':project_changes,
                    'duration_s':result['timeline']['duration_s'], 'frames':result['timeline']['total_frames'],
                    'shot_count':len(tl['shots']), 'subtitle_count':len(result['subtitles'])}
        save(folder/'diagnostic.json', diagnostic)
        return diagnostic
    except Exception as exc:
        save(folder/'diagnostic.json', {'status':'failed', 'error':str(exc), 'report':getattr(exc,'report',None),
                                       'action':'Review narration, the exact-audio transcript and timestamp evidence. Do not globally stretch or truncate.'})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command', required=True)
    for name in ('probe','transcribe','align'):
        command=commands.add_parser(name)
        command.add_argument('--audio',required=True)
        if name == 'align':
            command.add_argument('--project',required=True)
            command.add_argument('--transcript',required=True)
        else:
            command.add_argument('--out',required=True)
        if name == 'transcribe':
            command.add_argument('--model',default='small')
            command.add_argument('--language',default='zh')
    args=parser.parse_args()
    try:
        if args.command == 'probe':
            result=probe_audio(args.audio)
            save(args.out,result)
        elif args.command == 'transcribe':
            result=transcribe_audio(args.audio,args.out,args.model,args.language)
        else:
            result=align_project(args.project,args.audio,args.transcript)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
