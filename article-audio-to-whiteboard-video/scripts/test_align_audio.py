"""Core alignment invariants. Optional real-package test writes only to a new directory.

python test_align_audio.py
python test_align_audio.py --package PACKAGE --audio AUDIO --transcript JSON --out NEW_DIR
"""
import argparse
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import align_audio as alignment


def fixture():
    fps = 24
    audio = {'file':'recording.wav','sha256':'abc','sample_rate':48000,
             'decoded_samples':480480,'duration_s':10.01}
    transcript = {**audio,'segments':[
        {'start':0.2,'end':2.0,'text':'甲乙丙丁','words':[{'word':'甲乙丙丁','start':0.2,'end':2.0}]},
        {'start':2.6,'end':9.8,'text':'戊己庚辛','words':[{'word':'戊己庚辛','start':2.6,'end':9.8}]}]}
    shots = [{'shot_id':f'S{i+1:03}','clip':f'clips/S{i+1:03}.json','narration':text,
              'duration_estimate_s':20,'asset_ids':[f'A{i+1:03}']} for i,text in enumerate(['甲乙丙丁。','戊己庚辛。'])]
    tl = {'fps':fps,'shots':shots}
    specs = {}
    for shot in shots:
        specs[shot['shot_id']] = {'fps':fps,'duration':20,'width':1920,'height':1080,
            'safe':{'top':60,'bottom':180,'left':80,'right':80},'data':{},'cues':[
                {'kind':'image','at':0,'image':'../assets/a.png','data':{'box':[0,0,1,1]}},
                {'kind':'title','at':0,'text':'标题'},
                {'kind':'subtitle','at':0,'dur':20,'text':shot['narration']} ]}
    handoff={'shots':copy.deepcopy(shots)}
    return tl,specs,handoff,transcript,audio


class AlignmentTests(unittest.TestCase):
    def test_realign_preserves_current_visual_edits_and_initial_snapshot(self):
        tl,specs,handoff,transcript,audio=fixture()
        test_root=Path.cwd()/'merged-skill-validation'/'audio'
        test_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='realign-',dir=test_root) as temporary:
            root=Path(temporary)
            source=root/'original.wav'
            source.write_bytes(b'synthetic-audio-probe-is-mocked')
            audio['sha256']=alignment.sha256(source)
            transcript['sha256']=audio['sha256']
            alignment.save(root/'timeline.json',tl)
            alignment.save(root/'source-package/video-handoff.json',handoff)
            alignment.save(root/'input-transcript.json',transcript)
            for shot in tl['shots']:
                alignment.save(root/shot['clip'],specs[shot['shot_id']])
            with mock.patch.object(alignment,'probe_audio',return_value=audio):
                alignment.align_project(root,source,root/'input-transcript.json')
                snapshot=(root/'alignment/upstream-timeline.json').read_bytes()
                clip_snapshot=(root/'alignment/upstream-clips/S001.json').read_bytes()
                first_timing=alignment.read(root/'timeline.json')
                edited=alignment.read(root/'clips/S001.json')
                edited['cues'][0]['data']['box']=[.1,0,.9,1]
                next(q for q in edited['cues'] if q['kind']=='title')['text']='手动改过的标题'
                edited['cues']=[q for q in edited['cues'] if q['kind']!='subtitle']
                edited['cues'].extend([
                    {'kind':'point','at':0,'text':'保留关键词'},
                    {'kind':'highlight','at':.2,'anchor_text':'丙丁','data':{'style':'underline','rect':[.2,.2,.3,.1]}},
                    {'kind':'subtitle','at':.2,'dur':.8,'text':'甲乙'},
                    {'kind':'subtitle','at':1.0,'dur':1.0,'text':'丙丁。'}])
                alignment.save(root/'clips/S001.json',edited)
                alignment.align_project(root,source,root/'input-transcript.json')
                after=alignment.read(root/'clips/S001.json')
                self.assertEqual(after['cues'][0]['data']['box'],[.1,0,.9,1])
                self.assertEqual(next(q for q in after['cues'] if q['kind']=='title')['text'],'手动改过的标题')
                self.assertEqual(next(q for q in after['cues'] if q['kind']=='point')['text'],'保留关键词')
                self.assertEqual([q['text'] for q in after['cues'] if q['kind']=='subtitle'],['甲乙','丙丁。'])
                self.assertEqual(next(q for q in after['cues'] if q['kind']=='highlight')['anchor_text'],'丙丁')
                self.assertEqual((root/'alignment/upstream-timeline.json').read_bytes(),snapshot)
                self.assertEqual((root/'alignment/upstream-clips/S001.json').read_bytes(),clip_snapshot)
                self.assertEqual(alignment.read(root/'timeline.json'),first_timing)
                before_third=(root/'clips/S001.json').read_bytes()
                alignment.align_project(root,source,root/'input-transcript.json')
                self.assertEqual((root/'clips/S001.json').read_bytes(),before_third)

    def test_sub_tenth_second_scene_has_valid_entry_settings(self):
        tl,specs,handoff,transcript,audio=fixture()
        tl['shots']=tl['shots'][:1]
        tl['shots'][0]['narration']='甲。'
        specs['S001']['cues'][-1]['text']='甲。'
        audio.update(duration_s=.04,decoded_samples=1920)
        transcript.update(duration_s=.04,decoded_samples=1920,segments=[
            {'start':0,'end':.04,'text':'甲','words':[{'word':'甲','start':0,'end':.04}]}])
        result=alignment.build_alignment(tl,specs,handoff,transcript,audio)
        spec=result['clips']['S001']
        self.assertEqual(result['timeline']['total_frames'],1)
        self.assertEqual(spec['data']['image_enter'],'none')
        self.assertGreaterEqual(spec['data']['image_enter_duration'],.1)
        image=next(q for q in spec['cues'] if q['kind']=='image')
        self.assertEqual(image['data']['enter'],'none')
        self.assertGreaterEqual(image['data']['enter_duration'],.1)
        from run import _media_board
        _media_board(spec)

    def test_boolean_anchor_occurrence_rejected(self):
        args=list(fixture())
        args[1]['S001']['cues'].append({'kind':'highlight','at':0,'anchor_text':'甲乙',
                                      'anchor_occurrence':True,'data':{'style':'underline'}})
        with self.assertRaises(alignment.AlignmentError):alignment.build_alignment(*args)

    def test_transcription_cache_binds_audio_and_configuration(self):
        *_, transcript, audio = fixture()
        config={'model':'small','language':'zh','device':'cpu','compute_type':'int8',
                'word_timestamps':True,'vad_filter':True,'beam_size':5}
        cached={**transcript,'asr_options':config}
        fake_word=SimpleNamespace(word='声音',start=.1,end=.8,probability=1)
        fake_segment=SimpleNamespace(start=.1,end=.8,text='声音',words=[fake_word])
        worker=mock.Mock()
        worker.transcribe.return_value=([fake_segment],SimpleNamespace(language='zh'))
        factory=mock.Mock(return_value=worker)
        with mock.patch.object(alignment,'probe_audio',return_value=audio), \
             mock.patch.object(alignment,'read',return_value=cached), \
             mock.patch.object(alignment,'save') as saved, \
             mock.patch.object(Path,'is_file',return_value=True), \
             mock.patch.dict(sys.modules,{'faster_whisper':SimpleNamespace(WhisperModel=factory)}):
            result=alignment.transcribe_audio('input.wav','out.json')
            self.assertEqual(result['status'],'reused_exact_audio')
            factory.assert_not_called()
            alignment.transcribe_audio('input.wav','out.json',model='medium')
            self.assertEqual(factory.call_count,1)
            self.assertEqual(saved.call_args.args[1]['asr_options']['model'],'medium')
            alignment.transcribe_audio('input.wav','out.json',language='en')
            self.assertEqual(factory.call_count,2)
            cached['sha256']='another_audio'
            alignment.transcribe_audio('input.wav','out.json')
            self.assertEqual(factory.call_count,3)

    def test_full_audio_local_boundaries_and_text_preserved(self):
        tl,specs,handoff,transcript,audio=fixture()
        original=copy.deepcopy(tl)
        result=alignment.build_alignment(tl,specs,handoff,transcript,audio)
        self.assertEqual(tl,original)
        self.assertEqual(result['timeline']['total_frames'],math.ceil(10.01*24))
        self.assertEqual(result['timeline']['shots'][1]['start_frame'],round(2.3*24))
        self.assertNotEqual(result['timeline']['shots'][1]['start_frame'],round(10.01/2*24))
        self.assertLess(result['timeline']['audio']['tail_pad_s'],1/24)
        self.assertGreaterEqual(result['timeline']['audio']['tail_pad_s'],0)
        self.assertEqual(''.join(c['text'] for c in result['subtitles']),'甲乙丙丁。戊己庚辛。')
        self.assertTrue(all(c['start_frame']<c['end_frame'] for c in result['subtitles']))
        self.assertTrue(all(a['end_frame']<=b['start_frame'] for a,b in zip(result['subtitles'],result['subtitles'][1:])))

    def test_recording_hash_and_timestamps_required(self):
        for mutation in ('hash','duration','backward','nan','outside'):
            with self.subTest(mutation=mutation):
                args=list(fixture())
                if mutation=='hash':args[3]['sha256']='different'
                if mutation=='duration':args[3]['duration_s']=11
                if mutation=='backward':args[3]['segments'][1]['words'][0]['start']=1.0
                if mutation=='nan':args[3]['segments'][1]['words'][0]['end']=float('nan')
                if mutation=='outside':args[3]['segments'][1]['end']=11
                with self.assertRaises(alignment.AlignmentError):alignment.build_alignment(*args)

    def test_missing_fragment_and_unrelated_transcript_fail(self):
        for actual in ('甲乙戊己庚辛','甲乙丙丁','甲乙丙丁戊己庚辛添加了句子','今天天气很好'):
            with self.subTest(actual=actual):
                times=[(i,i+.5) for i in range(len(actual))]
                with self.assertRaises(alignment.AlignmentError):
                    alignment.map_reference('甲乙丙丁戊己庚辛',actual,times,20)

    def test_small_missing_character_local_interpolation(self):
        ref='今天我们一起来学习如何把经验整理成为可以反复使用的方法'
        actual=ref.replace('可以','以')
        times=[(i*.3,(i+1)*.3) for i in range(len(actual))]
        mapped,report=alignment.map_reference(ref,actual,times,20)
        self.assertEqual(len(mapped),len(ref))
        self.assertEqual(report['differences'][0]['reference'],'可')
        self.assertLessEqual(report['changed_fraction'],.05)

    def test_digits_are_matching_equivalents(self):
        self.assertEqual(alignment.norm('一、Prompt；二、Skill；三、Agent'),alignment.norm('1 Prompt 2 Skill 3 Agent'))

    def test_group_layout_reservation_and_no_repeat_fade(self):
        args=list(fixture())
        for shot in args[2]['shots']:shot['scene_group_id']='step'
        args[1]['S001']['cues'].append({'kind':'point','at':0,'text':'关键词'})
        result=alignment.build_alignment(*args)
        for spec in result['clips'].values():
            self.assertEqual(spec['data']['image_enter'],'none')
            self.assertTrue(spec['data']['reserve_point'])
            self.assertTrue(spec['data']['reserve_title'])
        self.assertFalse(any(q['kind']=='point' for q in result['clips']['S002']['cues']))

    def test_repeated_asset_continuity_and_layout_mismatch(self):
        args=list(fixture())
        args[0]['shots'][1]['asset_ids']=args[0]['shots'][0]['asset_ids']
        result=alignment.build_alignment(*args)
        self.assertTrue(all(c['data']['image_enter']=='none' for c in result['clips'].values()))
        args[1]['S002']['cues'][0]['data']['box']=[.1,0,.9,1]
        with self.assertRaises(alignment.AlignmentError):alignment.build_alignment(*args)

    def test_unanchored_estimate_rejected(self):
        args=list(fixture())
        args[1]['S001']['cues'].append({'kind':'point','at':10,'text':'重点'})
        with self.assertRaises(alignment.AlignmentError):alignment.build_alignment(*args)

    def test_repeated_anchor_risk_and_occurrence(self):
        args=list(fixture())
        args[0]['shots'][0]['narration']='甲乙甲乙。'
        args[1]['S001']['cues'][-1]['text']='甲乙甲乙。'
        args[3]['segments'][0]['words'][0]['word']='甲乙甲乙'
        args[1]['S001']['cues'].append({'kind':'highlight','at':1,'anchor_text':'甲乙','anchor_occurrence':2,'data':{'style':'underline'}})
        result=alignment.build_alignment(*args)
        risk=next(r for r in result['risks'] if r['kind']=='repeated_anchor')
        self.assertEqual(risk['selected_occurrence'],2)
        self.assertEqual(result['anchors'][0]['global_s'],round(1.1*24)/24)

    def test_segment_fallback_declared_and_subtitles_generated(self):
        args=list(fixture())
        for seg in args[3]['segments']:seg.pop('words')
        for spec in args[1].values():spec['cues']=[q for q in spec['cues'] if q['kind']!='subtitle']
        result=alignment.build_alignment(*args)
        self.assertEqual(sum(r['kind']=='segment_only_timing' for r in result['risks']),2)
        self.assertEqual(len(result['subtitles']),2)
        self.assertEqual(''.join(alignment.subtitle_chunks('，。很长的中文。'+('内容，'*20))),'，。很长的中文。'+('内容，'*20))


def real_package_test(package,audio,transcript,out):
    out=Path(out).resolve()
    if out.exists():
        raise RuntimeError('Real-package test needs a NEW output directory; existing files are preserved.')
    importer=Path(__file__).with_name('import_materials.py')
    subprocess.run([sys.executable,str(importer),'--package',package,'--out',str(out)],check=True)
    upstream=alignment.read(out/'timeline.json')
    result=alignment.align_project(out,audio,transcript)
    aligned=alignment.read(out/'timeline.json')
    assert result['status']=='pass'
    assert [s['shot_id'] for s in aligned['shots']]==[s['shot_id'] for s in upstream['shots']]
    assert [s['narration'] for s in aligned['shots']]==[s['narration'] for s in upstream['shots']]
    assert [s['asset_ids'] for s in aligned['shots']]==[s['asset_ids'] for s in upstream['shots']]
    assert alignment.sha256(out/aligned['audio']['file'])==alignment.sha256(audio)
    assert aligned['total_frames']==math.ceil(aligned['audio']['duration_s']*aligned['fps'])
    assert 0<=aligned['audio']['tail_pad_s']<1/aligned['fps']
    first_timeline=(out/'timeline.json').read_bytes()
    first_captions=(out/'subtitles.json').read_bytes()
    alignment.align_project(out,audio,transcript)
    assert (out/'timeline.json').read_bytes()==first_timeline
    assert (out/'subtitles.json').read_bytes()==first_captions
    subprocess.run([sys.executable,str(Path(__file__).with_name('run.py')),'validate',
                    '--spec',str(out/aligned['shots'][0]['clip'])],check=True)
    print(json.dumps({'real_package_test':'pass','output':str(out),'shots':len(aligned['shots']),
                      'frames':aligned['total_frames'],'duration_s':aligned['duration_s'],
                      'subtitles':result['subtitle_count'],'idempotence':'pass'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('package','audio','transcript','out'):parser.add_argument('--'+flag)
    options=parser.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AlignmentTests)
    tests=unittest.TextTestRunner(verbosity=2).run(suite)
    if not tests.wasSuccessful():raise SystemExit(1)
    if any(vars(options).values()):
        if not all(vars(options).values()):parser.error('All four real-package options are required together.')
        real_package_test(options.package,options.audio,options.transcript,options.out)
