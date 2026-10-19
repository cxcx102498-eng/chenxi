"""Local material inventory only. No image generation, downloads or license verification."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

FIELDS = ['id', 'shot_ids', 'label', 'kind', 'status', 'file',
          'transparent_required', 'license_status', 'source_page',
          'license_url', 'prompt_file', 'visual_review', 'notes']
STATUSES = {'planned', 'located', 'generated', 'saved', 'checked', 'blocked'}
LICENSES = {'unverified', 'terms-checked', 'user-provided-unverified',
            'generated-no-clearance-claim'}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def local_path(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError('empty or non-string path')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or ':' in value or '\\' in value:
        raise ValueError('use a project-relative path with forward slashes')
    p = (root / p).resolve()
    if not p.is_relative_to(root):
        raise ValueError('path escapes project')
    return p


def init(root):
    root.mkdir(parents=True, exist_ok=True)
    for d in ['prompts', 'images/generated', 'images/sourced', 'sources/candidates', 'qa', 'exports/ready']:
        (root / d).mkdir(parents=True, exist_ok=True)
    p = root / 'manifest.json'
    if not p.exists():
        write_json(p, {'schema_version': 1, 'project': root.name, 'assets': []})
    p = root / 'storyboard.csv'
    if not p.exists():
        with p.open('w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(['shot_id', 'narration', 'duration_estimate_s', 'purpose',
                                   'composition', 'camera_motion', 'entry_order', 'subtitle_space', 'asset_ids'])
    p = root / 'sources/source-log.json'
    if not p.exists():
        write_json(p, [])
    print('Initialized; existing files preserved. No images created.')
    return 0


def safe_cell(value):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    value = str(value) if value is not None else ''
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def audit(root):
    data = json.loads((root / 'manifest.json').read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('assets'), list):
        raise ValueError('expected schema_version=1 and assets array')
    assets = data['assets']
    if any(not isinstance(a, dict) for a in assets):
        raise ValueError('assets must contain objects')
    try:
        from PIL import Image
    except ImportError:
        Image = None
    report = {'scope': 'technical only; no visual or license verification',
              'issues': [] if assets else ['empty inventory: no deliverables'], 'assets': []}
    seen = set()
    for a in assets:
        errors = []
        details = {}
        ident = a.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'A\d{3,}', ident):
            errors.append('invalid asset ID')
        elif ident in seen:
            errors.append('duplicate asset ID')
        else:
            seen.add(ident)
        missing = [k for k in FIELDS if k not in a]
        if missing:
            errors.append('missing fields: ' + ', '.join(missing))
        if a.get('kind') not in {'generated', 'sourced', 'user-provided'}:
            errors.append('invalid kind')
        if a.get('status') not in STATUSES:
            errors.append('invalid status')
        elif a['status'] not in {'saved', 'checked'}:
            errors.append('not yet a saved deliverable: ' + a['status'])
        if not isinstance(a.get('transparent_required'), bool):
            errors.append('transparent_required must be boolean')
        if a.get('license_status') not in LICENSES:
            errors.append('invalid license_status')
        if a.get('visual_review') not in {'not-reviewed', 'pass', 'revise'}:
            errors.append('invalid visual_review')
        if a.get('status') == 'checked' and a.get('visual_review') != 'pass':
            errors.append('checked status requires recorded visual pass')
        shots = a.get('shot_ids')
        if not isinstance(shots, list) or not shots or any(not isinstance(s, str) or not re.fullmatch(r'S\d{3,}', s) for s in shots):
            errors.append('shot_ids must be a nonempty S001-style array')
        if a.get('kind') == 'generated' and not a.get('prompt_file'):
            errors.append('generated material requires prompt_file')
        if a.get('prompt_file'):
            try:
                if not local_path(root, a['prompt_file']).is_file():
                    errors.append('prompt file missing')
            except ValueError as e:
                errors.append('prompt: ' + str(e))
        if a.get('kind') == 'sourced':
            if not a.get('source_page'):
                errors.append('sourced material requires source_page')
            if a.get('license_status') == 'terms-checked' and not a.get('license_url'):
                errors.append('terms-checked requires license_url')
        try:
            p = local_path(root, a.get('file'))
            if not p.is_file():
                raise ValueError('file missing')
            details['bytes'] = p.stat().st_size
            if not Image:
                errors.append('Pillow unavailable: image check incomplete')
            else:
                with Image.open(p) as im:
                    im.load()
                    details.update(width=im.width, height=im.height, format=im.format)
                    alpha = im.convert('RGBA').getchannel('A')
                    lo, hi = alpha.getextrema()
                    details['alpha_extrema'] = [lo, hi]
                    if hi == 0:
                        errors.append('image fully transparent/empty')
                    if a.get('transparent_required') and (im.format != 'PNG' or p.suffix.lower() != '.png' or lo == 255):
                        errors.append('requires PNG with actual transparent pixels')
            if 'exports/ready' in str(p.relative_to(root)).replace('\\', '/'):
                if a.get('status') != 'checked' or a.get('visual_review') != 'pass':
                    errors.append('ready export requires checked status and visual pass')
                if a.get('kind') in {'sourced', 'user-provided'} and a.get('license_status') != 'terms-checked':
                    errors.append('ready external material requires recorded applicable terms')
        except (ValueError, OSError) as e:
            errors.append(str(e))
        report['assets'].append({'id': ident, 'technical_pass': not errors, 'issues': errors, **details})
    (root / 'qa').mkdir(exist_ok=True)
    write_json(root / 'qa/technical-report.json', report)
    with (root / 'manifest.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for a in assets:
            writer.writerow({k: safe_cell(a.get(k, '')) for k in FIELDS})
    failed = bool(report['issues']) or any(not a['technical_pass'] for a in report['assets'])
    print(f'Assets: {len(assets)}; technical audit: {"incomplete/failed" if failed else "passed"}. No asset statuses changed.')
    return 2 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'audit'])
    parser.add_argument('project', type=Path)
    args = parser.parse_args()
    try:
        return (init if args.command == 'init' else audit)(args.project.resolve())
    except (ValueError, OSError) as e:
        print('Input error: ' + str(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
