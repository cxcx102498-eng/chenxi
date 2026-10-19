"""Local temporary-package tests; no network, video rendering or user files are used."""
import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from import_materials import import_package, plan_import, subtitle_chunks


class ImportMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='visual-handoff-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / 'package'
        self.out = self.root / 'video'
        (self.package / 'images').mkdir(parents=True)
        (self.package / 'sources').mkdir()
        self.narration = '先查看这张图片，再用箭头指出重点。'
        self.assets = []
        self.manifest = {'schema_version': 1, 'project': 'test', 'assets': []}
        for i, color in ((1, '#1597F5'), (2, '#FF7A00')):
            ident, relative = f'A{i:03}', f'images/A{i:03}.png'
            path = self.package / relative
            Image.new('RGB', (320, 180), color).save(path)
            self.assets.append({'id': ident, 'file': relative,
                                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                'width': 320, 'height': 180})
            self.manifest['assets'].append({'id': ident, 'shot_ids': ['S001'],
                                           'file': relative, 'kind': 'sourced',
                                           'status': 'checked', 'visual_review': 'pass',
                                           'license_status': 'terms-checked',
                                           'source_page': 'https://example.invalid/fixture',
                                           'license_url': 'https://example.invalid/fixture-terms'})
        self.row = {'shot_id': 'S001', 'narration': self.narration,
                    'duration_estimate_s': '3.01', 'asset_ids': '["A001", "A002"]'}
        self.handoff = {'schema_version': 1, 'package_id': 'local-test',
                        'manifest': 'manifest.json', 'storyboard': 'storyboard.csv',
                        'narration_file': 'narration.txt',
                        'canvas': {'width': 1280, 'height': 720, 'fps': 30,
                                   'safe': {'top': 30, 'bottom': 120, 'left': 40, 'right': 40}},
                        'assets': copy.deepcopy(self.assets),
                        'shots': [{'shot_id': 'S001', 'narration': self.narration,
                                   'duration_estimate_s': 3.01, 'title': '看图解释重点',
                                   'asset_ids': ['A001'], 'layout': 'single',
                                   'cues': [{'at': 1, 'kind': 'highlight',
                                             'anchor_text': '箭头',
                                             'data': {'asset_id': 'A001', 'rect': [.1, .2, .3, .4]}}]}]}
        self.source_log = [{'asset_id': 'A001', 'attribution': '本地测试图（仅测试）',
                            'license_status': 'terms-checked'}, {'asset_id': 'A002', 'attribution': ''}]
        for source in self.source_log:
            source.update(source_page='https://example.invalid/fixture',
                          license_url='https://example.invalid/fixture-terms', license_status='terms-checked')
        self.save()

    def write_json(self, relative, value):
        (self.package / relative).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def save(self):
        self.write_json('manifest.json', self.manifest)
        self.write_json('video-handoff.json', self.handoff)
        self.write_json('sources/source-log.json', self.source_log)
        (self.package / 'narration.txt').write_text(self.narration, encoding='utf-8')
        with (self.package / 'storyboard.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(self.row))
            writer.writeheader()
            writer.writerow(self.row)

    def rejected(self, match, draft=False):
        self.save()
        with self.assertRaisesRegex(ValueError, match):
            import_package(self.package, self.out, draft=draft)
        self.assertFalse(self.out.exists(), 'A validation failure must not create a partial project')

    def test_import_preserves_bytes_narration_sources_and_frame_timing(self):
        manifest_before = (self.package / 'manifest.json').read_bytes()
        report = import_package(self.package, self.out)
        spec = json.loads((self.out / 'clips/S001.json').read_text(encoding='utf-8'))
        timeline = json.loads((self.out / 'timeline.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['grammar'], 'y4_media_board')
        self.assertEqual(spec['duration'], 3)
        self.assertEqual(timeline['shots'][0]['duration_estimate_s'], 3.01)
        self.assertEqual(timeline['shots'][0]['frame_count'], 90)
        self.assertEqual(timeline['shots'][0]['end_s'], 3)
        self.assertEqual(report['timing_basis'], 'estimated')
        subtitles = [q for q in spec['cues'] if q['kind'] == 'subtitle']
        self.assertEqual(''.join(q['text'] for q in subtitles), self.narration)
        self.assertAlmostEqual(sum(q['dur'] for q in subtitles), 3)
        image = next(q for q in spec['cues'] if q['kind'] == 'image')
        self.assertNotIn('text', image)
        self.assertFalse(spec['data']['show_source_credits'])
        self.assertEqual(spec['data']['image_enter'], 'fade-up')
        self.assertIn(self.source_log[0]['attribution'], (self.out / 'credits.txt').read_text(encoding='utf-8'))
        self.assertEqual(image['data']['box'], [0, 0, 1, 1])
        self.assertEqual((self.out / 'assets/A001.png').read_bytes(), (self.package / 'images/A001.png').read_bytes())
        self.assertEqual((self.out / 'source-package/manifest.json').read_bytes(), manifest_before)
        self.assertEqual((self.package / 'manifest.json').read_bytes(), manifest_before)
        self.assertEqual((self.out / 'narration.txt').read_text(encoding='utf-8'), self.narration)
        self.assertIn('00:00:03,000', (self.out / 'subtitles.srt').read_text(encoding='utf-8'))

    def test_compare_uses_explicit_targets_and_does_not_invent_attribution(self):
        self.handoff['shots'][0].update(layout='compare', asset_ids=['A001', 'A002'])
        self.handoff['shots'][0]['cues'][0]['data']['asset_id'] = 'A002'
        self.save()
        plan = plan_import(self.package, self.out)
        cues = plan['clips']['clips/S001.json']['cues']
        images = [q for q in cues if q['kind'] == 'image']
        self.assertEqual([q['asset_id'] for q in images], ['A001', 'A002'])
        self.assertNotIn('text', images[1])
        self.assertGreater(images[1]['data']['box'][0], images[0]['data']['box'][2])
        self.assertEqual(next(q for q in cues if q['kind'] == 'highlight')['data']['asset_id'], 'A002')
        self.assertFalse(self.out.exists())

    def test_unready_asset_requires_marked_draft_without_status_upgrade(self):
        self.manifest['assets'][0]['license_status'] = 'unverified'
        self.rejected('not ready')
        report = import_package(self.package, self.out, draft=True)
        spec = json.loads((self.out / 'clips/S001.json').read_text(encoding='utf-8'))
        self.assertTrue(spec['data']['draft'])
        self.assertIn('素材待核查', spec['data']['watermark'])
        self.assertEqual(next(q for q in spec['cues'] if q['kind'] == 'title')['text'], '看图解释重点')
        self.assertEqual(report['used_assets'][0]['license_status'], 'unverified')
        self.assertEqual(json.loads((self.package / 'manifest.json').read_text(encoding='utf-8'))['assets'][0]['license_status'], 'unverified')

    def test_draft_cannot_bypass_hash_or_dimensions(self):
        self.handoff['assets'][0]['sha256'] = '0' * 64
        self.rejected('SHA-256 mismatch', draft=True)
        self.handoff['assets'][0] = copy.deepcopy(self.assets[0])
        self.handoff['assets'][0]['width'] += 1
        self.rejected('width differs', draft=True)

    def test_missing_file_rejected_before_output(self):
        (self.package / 'images/A001.png').unlink()
        self.rejected('Missing package file', draft=True)

    def test_path_escape_and_manifest_display_mismatch_rejected(self):
        self.handoff['assets'][0]['file'] = '../outside.png'
        self.manifest['assets'][0]['file'] = '../outside.png'
        self.rejected('Path escapes package')
        self.manifest['assets'][0]['file'] = self.assets[0]['file']
        self.rejected('differs from manifest')

    def test_narration_omissions_and_estimate_changes_rejected(self):
        self.handoff['shots'][0]['narration'] = '只有一句。'
        self.rejected('does not cover narration_file')
        self.handoff['shots'][0]['narration'] = self.narration
        self.handoff['shots'][0]['duration_estimate_s'] = 4
        self.rejected('duration differs')

    def test_annotation_target_rect_and_nonfinite_rejected(self):
        cue = self.handoff['shots'][0]['cues'][0]
        cue['data']['asset_id'] = 'A002'
        self.rejected('annotation target')
        cue['data']['asset_id'] = 'A001'
        cue['data']['rect'] = [.8, .1, .4, .2]
        self.rejected('rectangle must lie')
        cue['data']['rect'] = [.1, .1, float('nan'), .2]
        self.rejected('Non-finite JSON')

    def test_cues_cannot_redefine_images_or_overrun(self):
        cue = self.handoff['shots'][0]['cues'][0]
        cue['image'] = 'https://example.invalid/remote.png'
        self.rejected('unsupported cue field')
        del cue['image']
        cue['at'], cue['dur'] = 2.9, .2
        self.rejected('cue exceeds')

    def test_subtitles_require_complete_nonoverlapping_narration(self):
        shot = self.handoff['shots'][0]
        shot['cues'] = [{'kind': 'subtitle', 'at': 0, 'dur': 2, 'text': self.narration[:8]},
                        {'kind': 'subtitle', 'at': 1, 'dur': 2, 'text': self.narration[8:]}]
        self.rejected('overlapping subtitle')
        shot['cues'] = [{'kind': 'subtitle', 'at': 0, 'dur': 2, 'text': self.narration[:8]}]
        self.rejected('preserve the complete narration')

    def test_text_layout_requires_reason_and_cannot_target_an_image(self):
        self.handoff['shots'][0].update(layout='text', asset_ids=[], cues=[])
        self.rejected('visual_reason')
        self.handoff['shots'][0]['visual_reason'] = '这一镜只说明两个概念的关系，文字箭头比照片更直接。'
        self.save()
        plan = plan_import(self.package, self.out)
        self.assertFalse(any(q['kind'] == 'image' for q in plan['clips']['clips/S001.json']['cues']))
        self.handoff['shots'][0]['cues'] = [{'at': 1, 'kind': 'focus', 'data': {'asset_id': 'A001', 'rect': [0, 0, 1, 1]}}]
        self.rejected('annotation target')

    def test_duplicate_ids_and_foreign_asset_reference_rejected(self):
        self.handoff['assets'].append(copy.deepcopy(self.assets[0]))
        self.rejected('duplicate A001')
        self.handoff['assets'].pop()
        self.row['asset_ids'] = '["A002"]'
        self.rejected('absent from original storyboard')

    def test_multishot_reuse_retains_id_and_continuous_frame_timeline(self):
        first = self.handoff['shots'][0]
        second = copy.deepcopy(first)
        second.update(shot_id='S002', narration='同一张图可以在下一镜继续使用。', duration_estimate_s=2.04,
                      title='继续解释', cues=[])
        self.handoff['shots'].append(second)
        for asset in self.manifest['assets']:
            asset['shot_ids'].append('S002')
        self.narration += '\n' + second['narration']
        self.save()
        with (self.package / 'storyboard.csv').open('a', encoding='utf-8', newline='') as f:
            csv.DictWriter(f, fieldnames=list(self.row)).writerow({
                'shot_id': 'S002', 'narration': second['narration'],
                'duration_estimate_s': 2.04, 'asset_ids': '["A001"]'})
        plan = plan_import(self.package, self.out)
        rows = plan['timeline']['shots']
        self.assertEqual(rows[0]['end_s'], rows[1]['start_s'])
        self.assertEqual(rows[1]['start_frame'], 90)
        self.assertEqual(rows[1]['frame_count'], 61)
        self.assertEqual(rows[1]['asset_ids'], ['A001'])
        self.assertEqual(len(plan['report']['used_assets']), 1)

    def test_invalid_manifest_cross_reference_rejected(self):
        self.manifest['assets'][1]['shot_ids'] = ['S999']
        self.rejected('existing unique storyboard shots')

    def test_formal_import_requires_source_records_and_terms_reference(self):
        self.source_log = []
        self.rejected('no source-log record')
        plan = plan_import(self.package, self.out, draft=True)
        self.assertEqual(plan['report']['missing_source_records'], ['A001'])
        self.assertIn('missing; preview only', plan['credits'])
        self.manifest['assets'][0]['license_url'] = ''
        self.rejected('no license_url')

    def test_credits_are_offscreen_by_default_and_screen_credit_requires_opt_in(self):
        full = '这段完整署名包含所有必要的作者、来源和许可信息，不应被截断。' * 8
        self.source_log[0].update(attribution=full, attribution_short='原始作者指定的短署名')
        self.save()
        plan = plan_import(self.package, self.out)
        cue = next(q for q in plan['clips']['clips/S001.json']['cues'] if q['kind'] == 'image')
        self.assertNotIn('text', cue)
        self.assertIn(full, plan['credits'])
        self.handoff['presentation'] = {'show_source_credits': True}
        self.save()
        plan = plan_import(self.package, self.out)
        cue = next(q for q in plan['clips']['clips/S001.json']['cues'] if q['kind'] == 'image')
        self.assertEqual(cue['text'], '原始作者指定的短署名')
        del self.source_log[0]['attribution_short']
        self.save()
        plan = plan_import(self.package, self.out)
        self.assertNotIn('text', next(q for q in plan['clips']['clips/S001.json']['cues'] if q['kind'] == 'image'))
        self.assertIn(full, plan['credits'])

    def test_content_labels_are_separate_from_attribution_and_short_only_credit_is_kept(self):
        self.source_log[0].update(attribution='', attribution_short='作者 / 许可', display_label='旧包的来源混合说明')
        self.handoff['assets'][0]['display_label'] = '人物姓名 · 资料图'
        self.save()
        plan = plan_import(self.package, self.out)
        cue = next(q for q in plan['clips']['clips/S001.json']['cues'] if q['kind'] == 'image')
        self.assertNotIn('label', cue['data'])
        self.assertFalse(plan['clips']['clips/S001.json']['data']['show_image_labels'])
        self.handoff['presentation'] = {'show_image_labels': True}
        self.save()
        plan = plan_import(self.package, self.out)
        cue = next(q for q in plan['clips']['clips/S001.json']['cues'] if q['kind'] == 'image')
        self.assertEqual(cue['data']['label'], '人物姓名 · 资料图')
        self.assertNotIn('text', cue)
        self.assertNotIn('旧包的来源混合说明', json.dumps(cue, ensure_ascii=False))
        self.assertIn('作者 / 许可', plan['credits'])

    def test_diagram_entry_defaults_to_fade_and_explicit_motion_overrides_it(self):
        self.source_log[0]['media_type'] = 'diagram'
        self.save()
        spec = plan_import(self.package, self.out)['clips']['clips/S001.json']
        cue = next(q for q in spec['cues'] if q['kind'] == 'image')
        self.assertEqual(cue['data']['enter'], 'fade')
        self.handoff['presentation'] = {'image_enter': 'none', 'image_enter_duration': 0.5}
        self.save()
        spec = plan_import(self.package, self.out)['clips']['clips/S001.json']
        self.assertEqual(spec['data']['image_enter'], 'none')
        self.assertNotIn('enter', next(q for q in spec['cues'] if q['kind'] == 'image')['data'])

    def test_invalid_presentation_rejected_before_output(self):
        self.handoff['presentation'] = {'show_source_credits': 'false'}
        self.rejected('show_source_credits must be boolean')
        self.handoff['presentation'] = {'show_image_labels': 'false'}
        self.rejected('show_image_labels must be boolean')
        self.handoff['presentation'] = {'image_enter': 'bounce'}
        self.rejected('invalid image_enter')
        self.handoff['presentation'] = {'image_enter_duration': 0}
        self.rejected('too small')

    def test_explicit_anchor_occurrence_survives_import(self):
        self.narration = '先看重点，再看重点。'
        self.row['narration'] = self.narration
        self.handoff['shots'][0]['narration'] = self.narration
        cue = self.handoff['shots'][0]['cues'][0]
        cue.update(anchor_text='重点', anchor_occurrence=2)
        self.save()
        spec = plan_import(self.package, self.out)['clips']['clips/S001.json']
        mapped = next(q for q in spec['cues'] if q['kind'] == 'highlight')
        self.assertEqual(mapped['anchor_occurrence'], 2)
        for value in (True, 0, -1, 3, '2', 1.5):
            cue['anchor_occurrence'] = value
            self.rejected('anchor_occurrence')

    def test_fractional_fps_rejected_to_match_renderer(self):
        self.handoff['canvas']['fps'] = 29.97
        self.rejected('fps must be a positive integer')

    def test_existing_nonempty_output_is_preserved(self):
        self.out.mkdir()
        sentinel = self.out / 'existing.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            import_package(self.package, self.out)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(list(self.out.iterdir()), [sentinel])

    def test_subtitle_chunking_preserves_all_characters(self):
        text = '\n' + '这是一段稍长的测试文案，标点和空格都要保留。' * 8 + ' End.'
        self.assertEqual(''.join(subtitle_chunks(text)), text)
        self.assertTrue(all(part.strip() for part in subtitle_chunks(text)))


if __name__ == '__main__':
    unittest.main()
