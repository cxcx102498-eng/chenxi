"""Validate a focused spec, then use the extracted renderer or QA."""
import argparse, json, math, os, runpy, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINDS = {
    'y4_media_board': {'title', 'point', 'image', 'highlight', 'focus', 'reset', 'subtitle'},
    'y3_whiteboard': {'title', 'point', 'draw', 'highlight'},
    'y2_vox': {'title', 'point', 'image', 'clip', 'highlight'},
    'y5_kinetic_type': {'title', 'point', 'number', 'highlight'},
}

def _number(value, label, minimum=0, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if value < minimum or (positive and value <= minimum):
        raise ValueError(f'{label} must be {"greater than" if positive else "at least"} {minimum}')
    return value

def _rect(value, label):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f'{label} must be [x,y,width,height]')
    x, y, w, h = [_number(v, label, positive=i >= 2) for i, v in enumerate(value)]
    if x+w > 1+1e-9 or y+h > 1+1e-9:
        raise ValueError(f'{label} must lie completely within normalized coordinates 0..1')
    return x, y, w, h

def _media_board(data):
    """Checks specific to the clean media grammar; old grammar behavior is preserved."""
    width, height = data.get('width', 1920), data.get('height', 1080)
    fps = data.get('fps', 30)
    if fps != int(fps):
        raise ValueError('y4_media_board fps must be a positive integer')
    safe = data.get('safe', {})
    if not isinstance(safe, dict):
        raise ValueError('safe must be an object')
    margins = {k: _number(safe.get(k, 0), f'safe.{k}') for k in ('top', 'bottom', 'left', 'right')}
    if margins['left'] + margins['right'] >= width or margins['top'] + margins['bottom'] >= height:
        raise ValueError('safe margins must leave a positive content area')
    cues = data.get('cues', [])
    if not isinstance(cues, list) or not cues:
        raise ValueError('y4_media_board requires a nonempty cues list')
    if not isinstance(data.get('data', {}), dict):
        raise ValueError('data must be an object')
    if 'draft' in data.get('data', {}) and not isinstance(data['data']['draft'], bool):
        raise ValueError('data.draft must be true or false')
    if 'watermark' in data.get('data', {}) and not isinstance(data['data']['watermark'], str):
        raise ValueError('data.watermark must be a string')
    settings = data.get('data', {})
    if 'show_source_credits' in settings and not isinstance(settings['show_source_credits'], bool):
        raise ValueError('data.show_source_credits must be boolean')
    if 'show_image_labels' in settings and not isinstance(settings['show_image_labels'], bool):
        raise ValueError('data.show_image_labels must be boolean')
    for key in ('reserve_title', 'reserve_point'):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError(f'data.{key} must be boolean')
    def entry_options(options, prefix, mode_key, duration_key):
        mode = options.get(mode_key, 'fade-up')
        if not isinstance(mode, str) or mode not in {'fade-up', 'fade', 'zoom-in', 'none'}:
            raise ValueError(f'{prefix}.{mode_key} is not a supported entry animation')
        if duration_key in options:
            value = _number(options[duration_key], f'{prefix}.{duration_key}', positive=True)
            if not 0.1 <= value <= 2:
                raise ValueError(f'{prefix}.{duration_key} must be between 0.1 and 2 seconds')
    entry_options(settings, 'data', 'image_enter', 'image_enter_duration')
    images, ids = [], {}
    for index, q in enumerate(cues):
        if not isinstance(q, dict):
            raise ValueError('Each cue must be an object')
        if q['at'] >= data['duration']:
            raise ValueError('y4_media_board cue.at must be less than duration')
        if not isinstance(q.get('data', {}), dict):
            raise ValueError('cue.data must be an object')
        if 'dur' in q:
            _number(q['dur'], 'cue.dur', positive=True)
            if q['at'] + q['dur'] > data['duration'] + 1e-9:
                raise ValueError('cue.at + cue.dur must not exceed duration')
        elif q['kind'] in ('highlight', 'focus', 'reset'):
            default_duration = 0.55 if q['kind'] == 'highlight' else 0.65
            if q['at'] + default_duration > data['duration'] + 1e-9:
                raise ValueError(f'{q["kind"]} needs time for its default {default_duration}s animation before the scene ends')
        if q['kind'] in ('title', 'point', 'subtitle'):
            if not isinstance(q.get('text'), str) or not q['text'].strip():
                raise ValueError(f'{q["kind"]} needs nonempty text')
        if q['kind'] == 'subtitle' and 'dur' not in q:
            raise ValueError('subtitle requires an explicit positive dur')
        if q['kind'] == 'image':
            entry_options(q.get('data', {}), 'image.data', 'enter', 'enter_duration')
            if 'label' in q.get('data', {}) and not isinstance(q['data']['label'], str):
                raise ValueError('image.data.label must be a string')
            if not isinstance(q['image'], str) or not q['image'].strip():
                raise ValueError('image must be a nonempty path or URL string')
            if 'text' in q and not isinstance(q['text'], str):
                raise ValueError('image.text source caption must be a string')
            image_id = q.get('asset_id')
            if image_id is not None:
                if not isinstance(image_id, str) or not image_id.strip():
                    raise ValueError('image.asset_id must be a nonempty string')
                if image_id in ids:
                    raise ValueError(f'Duplicate image.asset_id: {image_id}')
                ids[image_id] = (q, index)
            rect = _rect(q.get('data', {}).get('box', [0, 0, 1, 1]), 'image.data.box')
            images.append((q, rect))
    if len(images) > 2:
        raise ValueError('y4_media_board supports at most two persistent images per spec; split additional images into scenes')
    if not images and not any(q['kind'] in ('title', 'point') for q in cues):
        raise ValueError('A scene without images needs a title or point')
    if len(images) == 2:
        if any('box' not in q.get('data', {}) for q, _ in images):
            raise ValueError('Two images require explicit, non-overlapping data.box values')
        a, b = images[0][1], images[1][1]
        if min(a[0]+a[2], b[0]+b[2]) > max(a[0], b[0])+1e-9 and min(a[1]+a[3], b[1]+b[3]) > max(a[1], b[1])+1e-9:
            raise ValueError('Persistent image boxes must not overlap')
    for index, q in enumerate(cues):
        if q['kind'] not in ('highlight', 'focus', 'reset'):
            continue
        target = q.get('data', {}).get('asset_id')
        if not isinstance(target, str) or target not in ids:
            raise ValueError(f'{q["kind"]} needs data.asset_id naming an image in this spec')
        image, image_index = ids[target]
        if (image['at'], image_index) >= (q['at'], index):
            raise ValueError(f'{q["kind"]} target {target} must have appeared before the cue')
        if q['kind'] in ('highlight', 'focus'):
            _rect(q['data'].get('rect'), f'{q["kind"]}.data.rect')
        if q['kind'] == 'highlight' and q['data'].get('style', 'rect') not in ('rect', 'circle', 'underline'):
            raise ValueError('highlight.data.style must be rect, circle, or underline')
    subtitles = sorted((q for q in cues if q['kind'] == 'subtitle'), key=lambda q: q['at'])
    for a, b in zip(subtitles, subtitles[1:]):
        if a['at'] + a['dur'] > b['at'] + 1e-9:
            raise ValueError('Subtitle intervals must not overlap')
    u = min(width, height) / 1080
    if subtitles and margins['bottom'] < 140*u:
        raise ValueError('Built-in subtitles require safe.bottom >= 140 * min(width,height)/1080 pixels')
    title_h = 150*u if any(q['kind'] == 'title' for q in cues) or settings.get('draft') or settings.get('reserve_title') is True else 0
    point_h = 126*u if images and (any(q['kind'] == 'point' for q in cues) or settings.get('reserve_point') is True) else 0
    media_w = width - margins['left'] - margins['right'] - 48*u
    media_h = height - margins['top'] - margins['bottom'] - 48*u - title_h - point_h - (20*u if title_h else 0) - (20*u if point_h else 0)
    if media_w <= 32*u or media_h <= 80*u:
        raise ValueError('Safe margins and text regions leave insufficient media area')
    for q, rect in images:
        focus = any(e['kind'] == 'focus' and e.get('data', {}).get('asset_id') == q.get('asset_id') for e in cues)
        has_caption = (settings.get('show_image_labels') is True and q.get('data', {}).get('label')) or (settings.get('show_source_credits') is True and q.get('text'))
        caption_h = 68*u if focus else 48*u if has_caption else 0
        if rect[2]*media_w <= 32*u or rect[3]*media_h-caption_h <= 32*u:
            raise ValueError('Image box is too small after source/context caption area')

def validate(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('Spec must be a JSON object')
    grammar = data.get('grammar')
    if grammar not in KINDS:
        raise ValueError('grammar must be one of: '+', '.join(KINDS))
    for key, default in [('duration', 0), ('fps', 30), ('width', 1920), ('height', 1080)]:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be a finite positive number')
        if key in ('width','height') and (value != int(value) or int(value)%2):
            raise ValueError(f'{key} must be an even integer')
    if not isinstance(data.get('cues', []), list):
        raise ValueError('cues must be a list')
    for q in data.get('cues', []):
        if not isinstance(q, dict):
            raise ValueError('Each cue must be an object')
        if q.get('kind') not in KINDS[grammar]:
            raise ValueError(f'Unsupported cue kind {q.get("kind")} for {grammar}')
        at=q.get('at')
        if isinstance(at,bool) or not isinstance(at,(int,float)) or not math.isfinite(at) or not 0 <= at <= data['duration']:
            raise ValueError('Every cue needs at between 0 and duration')
        if q['kind'] in ('image','clip') and not q.get('image'):
            raise ValueError('Media cue needs an image path')
    if grammar == 'y4_media_board':
        _media_board(data)
    images=[q.get('image') for q in data.get('cues',[])]+[(data.get('data') or {}).get('image')]
    for value in filter(None,images):
        if value.startswith(('https://','http://','data:')):
            continue
        f=Path(value)
        if not f.is_absolute():f=path.parent/f
        if not f.is_file():raise ValueError(f'Missing local image: {f}')
    return data

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode',choices=['validate','render','qa'])
    ap.add_argument('--spec',required=True,type=Path)
    ap.add_argument('--browser',help='Optional installed Chromium/Chrome executable')
    args,rest=ap.parse_known_args()
    spec=args.spec.resolve();validate(spec)
    if args.mode=='validate':
        if rest:ap.error('Unknown validate arguments: '+' '.join(rest))
        print('Spec valid');return
    # Project-local packages may be installed without changing system Python.
    sys.path.insert(0,str(spec.parent/'.video-deps'))
    if spec.parent.name == 'clips' and (spec.parent.parent/'timeline.json').is_file():
        sys.path.insert(0,str(spec.parent.parent/'.video-deps'))
    try:
        from playwright.sync_api import BrowserType
    except ImportError:
        raise SystemExit('Install scripts/requirements.txt into project .video-deps, then rerun.')
    browser=args.browser or os.environ.get('AI_VIDEO_BROWSER')
    if not browser and os.name=='nt':
        for base,relative in [('PROGRAMFILES','Google/Chrome/Application/chrome.exe'),('PROGRAMFILES(X86)','Microsoft/Edge/Application/msedge.exe')]:
            candidate=Path(os.environ.get(base,''))/relative
            if candidate.is_file():browser=str(candidate);break
    launch=BrowserType.launch
    def local_launch(self,*a,**kw):
        if browser:kw['executable_path']=str(Path(browser).resolve())
        kw['args']=['--disable-gpu','--disable-accelerated-2d-canvas','--font-render-hinting=none']
        return launch(self,*a,**kw)
    BrowserType.launch=local_launch
    needs_encoder = args.mode == 'render' and not any(v == '--stills' or v.startswith('--stills=') for v in rest)
    if needs_encoder and not shutil.which('ffmpeg'):
        try:
            import imageio_ffmpeg
        except ImportError:
            raise SystemExit('FFmpeg is missing; install imageio-ffmpeg or add FFmpeg to PATH.')
        bindir=(Path(tempfile.gettempdir())/'ai-learning-video-tools') if spec.is_relative_to(ROOT.parent) else spec.parent/'.video-tools'
        bindir.mkdir(exist_ok=True)
        target=bindir/('ffmpeg.exe' if os.name=='nt' else 'ffmpeg')
        if not target.exists():
            # Parallel scene renders must never see a partially copied executable.
            fd, temporary = tempfile.mkstemp(prefix='ffmpeg-', suffix='.tmp', dir=bindir)
            os.close(fd)
            staged = Path(temporary)
            try:
                shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), staged)
                try:
                    os.rename(staged, target)
                except FileExistsError:
                    pass  # Another renderer completed the same atomic installation.
            finally:
                staged.unlink(missing_ok=True)
        os.environ['PATH']=str(bindir)+os.pathsep+os.environ.get('PATH','')
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    script=ROOT/'qa.py' if args.mode=='qa' else ROOT/'engine/render.py'
    sys.argv=[str(script),'--spec',str(spec)]+rest
    runpy.run_path(str(script),run_name='__main__')

if __name__=='__main__':
    try:main()
    except (ValueError, json.JSONDecodeError) as e:raise SystemExit(f'Invalid spec: {e}')
