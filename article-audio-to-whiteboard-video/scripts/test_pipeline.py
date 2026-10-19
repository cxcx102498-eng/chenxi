"""Fast invariants plus optional real two-shot browser/FFmpeg integration smoke test.

python test_pipeline.py
python test_pipeline.py --smoke-dir NEW_DIR --audio original.mp3 --image still.png
Smoke fixtures are explicitly non-delivery test media, not replacements for the user's recording.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pipeline as p


class Invariants(unittest.TestCase):
    def test_local_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'a').mkdir()
            (root / 'file').write_text('x')
            self.assertRaises(ValueError, p.local, root / 'a', '../file')

    def test_unknown_shot_id(self):
        self.assertRaises(ValueError, p.selected_shots, {'shots': [{'shot_id': 'S001'}]}, 'S999')

    def test_runtime_and_asset_hash_invalidate_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'renderer').mkdir()
            (root / 'renderer/runtime.js').write_text('one')
            (root / 'image.png').write_bytes(b'first')
            p.write(root / 'clip.json', {'cues': [{'image': 'image.png'}]})
            shot = {'clip': 'clip.json', 'frame_count': 12}
            first = p.shot_hash(root, shot, p.runtime_hash(root))
            (root / 'image.png').write_bytes(b'second')
            second = p.shot_hash(root, shot, p.runtime_hash(root))
            self.assertNotEqual(first, second)
            (root / 'renderer/runtime.js').write_text('two')
            self.assertNotEqual(second, p.shot_hash(root, shot, p.runtime_hash(root)))

    def test_silence_audio_check(self):
        import numpy as np
        self.assertTrue(p.audio_checks(np.zeros(1000), np.zeros(1000))[0]['silent'])

    def test_audio_change_rejected(self):
        import numpy as np
        first = np.sin(np.linspace(0, 100, 1000))
        self.assertRaises(ValueError, p.audio_checks, first, first[::-1])


def smoke(root, audio, image):
    from PIL import Image
    root = root.resolve()
    p.require(not root.exists() or not any(root.iterdir()), 'Smoke output must be new/empty')
    package = root / 'fixture-package'
    package.mkdir(parents=True)
    shutil.copy2(image, package / 'image.png')
    (package / 'brief.txt').write_text('Integration test uses a supplied existing illustration.', encoding='utf-8')
    (package / 'narration.txt').write_text('测试第一镜。测试第二镜。', encoding='utf-8')
    (package / 'editing-notes.md').write_text('Synthetic 3s integration fixture; not a user deliverable.', encoding='utf-8')
    (package / 'credits.txt').write_text('Test credit: preserve original font/illustration information.', encoding='utf-8')
    (package / 'editable').mkdir()
    (package / 'editable/source.txt').write_text('Check full source preservation.', encoding='utf-8')
    with Image.open(image) as im:
        w, h = im.size
    asset = {'id': 'A001', 'file': 'image.png', 'width': w, 'height': h, 'sha256': p.digest(package / 'image.png')}
    shots = [{'shot_id': f'S{i:03}', 'narration': text, 'duration_estimate_s': 1.5,
              'asset_ids': ['A001'], 'layout': 'single', 'title': f'测试 {i}', 'cues': []}
             for i, text in [(1, '测试第一镜。'), (2, '测试第二镜。')]]
    p.write(package / 'manifest.json', {'schema_version': 1, 'assets': [{**asset, 'shot_ids': ['S001', 'S002'],
        'kind': 'generated', 'status': 'checked', 'visual_review': 'pass', 'license_status': 'generated-no-clearance-claim',
        'prompt_file': 'brief.txt'}]})
    with (package / 'storyboard.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['shot_id', 'narration', 'duration_estimate_s', 'asset_ids'])
        writer.writeheader()
        writer.writerows({k: ','.join(s[k]) if k == 'asset_ids' else s[k] for k in writer.fieldnames} for s in shots)
    p.write(package / 'video-handoff.json', {'schema_version': 1, 'package_id': 'synthetic-pipeline-test',
        'manifest': 'manifest.json', 'storyboard': 'storyboard.csv', 'narration_file': 'narration.txt',
        'canvas': {'width': 640, 'height': 360, 'fps': 12, 'safe': {'top': 15, 'bottom': 60, 'left': 20, 'right': 20}},
        'presentation': {'image_enter': 'none'}, 'assets': [asset], 'shots': shots})
    project = root / 'project 中文 space'
    p.init_project(package, project)
    p.require((project / 'source-package/editable/source.txt').is_file(), 'Full source snapshot missing')
    p.require((project / 'editable/source.txt').is_file(), 'Editable source missing')
    p.require('Test credit:' in (project / 'credits.txt').read_text(encoding='utf-8'), 'Original credits not preserved')
    try:
        p.init_project(package, project)
    except ValueError:
        pass
    else:
        raise AssertionError('Repeated init overwrote a project')
    (project / 'audio').mkdir()
    sample = project / 'audio/fixture 3s.wav'
    p.command([p.ffmpeg(), '-y', '-v', 'error', '-i', audio, '-t', '3', '-ar', '48000', '-ac', '1', sample])
    tl = p.read(project / 'timeline.json')
    tl['timing_basis'] = 'synthetic-integration-fixture-not-delivery'
    tl['audio'] = {'file': 'audio/fixture 3s.wav', 'sha256': p.digest(sample), 'duration_s': len(p.pcm(sample)) / 48000}
    p.write(project / 'timeline.json', tl)
    (project / 'subtitles.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.500\n测试第一镜。\n\n00:00:01.500 --> 00:00:03.000\n测试第二镜。\n', encoding='utf-8')
    p.write(project / 'subtitles.json', [{'shot_id': s['shot_id'], 'start_s': s['start_s'], 'end_s': s['end_s'],
                                       'text': s['narration']} for s in tl['shots']])
    p.render_stage(project, 'preview')
    first = p.render_stage(project, 'render')
    p.require(all(x['status'] == 'rendered' for x in first), 'First run did not render')
    second = p.render_stage(project, 'render')
    p.require(all(x['status'] == 'cached' for x in second), 'Second run was not cached')
    spec = p.read(project / 'clips/S002.json')
    next(q for q in spec['cues'] if q['kind'] == 'title')['text'] = '更新测试 2'
    p.write(project / 'clips/S002.json', spec)
    third = {x['shot_id']: x['status'] for x in p.render_stage(project, 'render')}
    p.require(third == {'S001': 'cached', 'S002': 'rendered'}, 'Changed-only rendering failed')
    report = p.export_video(project, 'smoke 中文.mp4')
    p.require(report['frames'] == 36 and report['full_decode'] == 'pass', 'Real final decode failed')
    for changed, expected in [(sample, 'audio'), (project / 'subtitles.srt', 'Subtitles'),
                              (project / 'exports/subtitles.json', 'Subtitles')]:
        original = changed.read_bytes()
        try:
            changed.write_bytes(original + b'\nCHANGED')
            try:
                p.package_project(project)
            except ValueError as exc:
                p.require(expected in str(exc), f'Unexpected stale-file rejection: {exc}')
            else:
                raise AssertionError('Changed audio/subtitles were packaged with stale video')
        finally:
            changed.write_bytes(original)
    try:
        p.package_project(project)
    except ValueError as exc:
        p.require('visual' in str(exc).lower() or 'review' in str(exc).lower(), f'Unexpected package error: {exc}')
    else:
        raise AssertionError('Unreviewed video was packaged')
    p.write(root / 'smoke-report.json', {'status': 'pass', 'frame_count': report['frames'], 'full_decode': report['full_decode'],
        'first_render': first, 'second_render': second, 'changed_only_render': third,
        'review_gate': 'unreviewed export rejected', 'audio_checks': report['audio_waveform_checks'],
        'stale_audio_and_subtitles': 'rejected',
        'project': str(project), 'video_sha256': report['sha256']})
    print('SMOKE PASS', project)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke-dir', type=Path)
    parser.add_argument('--audio', type=Path)
    parser.add_argument('--image', type=Path)
    args = parser.parse_args()
    p.dependencies(Path.cwd())
    if args.smoke_dir:
        parser.error('--audio and --image are required for smoke') if not (args.audio and args.image) else None
        smoke(args.smoke_dir, args.audio, args.image)
    else:
        unittest.main(argv=[sys.argv[0]])
