import base64
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.headings = []
        self.images = []
        self.hidden = 0
        self.heading = None
        self.math = 0
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {'p', 'div', 'br', 'li', 'tr', 'section', 'figure'} or tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')
        if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.heading = (int(tag[1]), len(''.join(self.parts)))
        if tag == 'img':
            alt = attrs.get('alt', '')
            self.images.append({'src': attrs.get('src', ''), 'alt': alt, 'offset': len(''.join(self.parts))})
            if alt:
                self.parts.append(alt)
        if tag == 'math':
            self.math += 1
        if self.math:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if self.math:
            self.parts.append('</' + tag + '>')
            if tag == 'math':
                self.math -= 1
        if self.heading and tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            level, start = self.heading
            self.headings.append({'level': level, 'title': ''.join(self.parts)[start:].strip(), 'offset': start})
            self.heading = None
        if tag in {'p', 'div', 'li', 'tr', 'section', 'figure', 'figcaption'} or tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    @property
    def text(self):
        return ''.join(self.parts)


def image_bytes(path, src):
    if src.startswith('data:'):
        header, data = src.split(',', 1)
        if not header.startswith('data:image/') or ';base64' not in header or len(data) > 12_000_000:
            raise ValueError('Unsupported or oversized inline figure')
        raw = base64.b64decode(data, validate=True)
    else:
        link = urlsplit(src)
        if link.scheme or link.netloc or not link.path:
            raise ValueError('Remote or missing figure is not retained; supply a local HTML asset bundle')
        candidate = path.parent / unquote(link.path)
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(path.parent.resolve()) or not candidate.is_file():
            raise ValueError('Figure is missing or leaves the supplied HTML folder')
        if candidate.stat().st_size > 8_000_000:
            raise ValueError('Figure exceeds the retained asset limit')
        raw = candidate.read_bytes()
    if not raw or len(raw) > 8_000_000:
        raise ValueError('Figure is empty or exceeds the retained asset limit')
    return raw


def stage_assets(path, destination):
    path, destination = Path(path), Path(destination)
    parsed = Markup(path.read_text(encoding='utf-8'))
    entries, total = [], 0
    for image in parsed.images:
        entry = {'src': image['src']}
        try:
            raw = image_bytes(path, image['src'])
            total += len(raw)
            if len(entries) >= 1000 or total > 128_000_000:
                raise ValueError('HTML figure bundle exceeds the retained asset limit')
            token = hashlib.sha256(raw).hexdigest()
            target = destination / (token + '.asset')
            target.write_bytes(raw)
            target.chmod(0o600)
            entry.update(file=target.name, sha256=token)
        except (OSError, ValueError) as error:
            entry['gap'] = str(error)
        entries.append(entry)
    manifest = destination / 'assets.json'
    manifest.write_text(json.dumps({'format': 'topclass-html-assets-v1', 'entries': entries}))
    manifest.chmod(0o600)
    return entries


def retained_assets(path):
    manifest = Path(path).parent / 'assets.json'
    if not manifest.is_file():
        return None
    saved = json.loads(manifest.read_text())
    if saved.get('format') != 'topclass-html-assets-v1' or not isinstance(saved.get('entries'), list):
        raise ValueError('Invalid retained HTML asset manifest')
    for entry in saved['entries']:
        if 'file' in entry:
            asset = manifest.parent / entry['file']
            if asset.is_symlink() or not asset.resolve().is_relative_to(manifest.parent.resolve()) or hashlib.sha256(asset.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('Retained HTML figure changed or leaves its source folder')
    return saved['entries']


def attach_visuals(graph, source_id):
    from core.graph import identity
    from core.sources import checked_source, regions
    import fitz

    source = checked_source(graph, source_id)
    payload = source['payload']
    if not payload.get('retained_html') or not payload.get('retained_visuals'):
        return
    rows = regions(graph, source_id)
    if sum(row['payload'].get('figure_number') is not None for row in rows) == len(payload['retained_visuals']):
        return
    first_index = max((row['payload']['region_index'] for row in rows if row['payload'].get('figure_number') is None), default=-1) + 1
    html = Path(payload['retained_html'])
    if html.is_symlink() or not html.resolve().is_relative_to(Path(payload['original_path']).parent.resolve()):
        raise ValueError('Retained HTML leaves its original source folder')
    if hashlib.sha256(html.read_bytes()).hexdigest() != payload.get('retained_html_sha256'):
        raise ValueError('Retained original HTML changed')
    parsed = Markup(html.read_text(encoding='utf-8'))
    for number, asset in enumerate(payload['retained_visuals']):
        path = Path(asset['path'])
        if path.is_symlink() or not path.resolve().is_relative_to(html.parent.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('Retained original figure changed or leaves its source folder')
        image = next((image for image in parsed.images if Path(unquote(urlsplit(image['src']).path)).name == path.name), None)
        if image is None:
            raise ValueError('Retained figure has no original HTML anchor')
        pixmap = fitz.Pixmap(path.read_bytes())
        if pixmap.width * pixmap.height > 40_000_000:
            raise ValueError('Figure exceeds the decoded image size limit')
        if pixmap.colorspace and pixmap.colorspace.n > 3:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
        target = html.parent / f'figure-retained-{number + 1:05d}.png'
        pixmap.save(target)
        target.chmod(0o600)
        if target.stat().st_size > 8_000_000:
            raise ValueError('Figure exceeds the inline image limit')
        start, end = max(0, image['offset'] - 500), min(len(parsed.text), image['offset'] + 1000)
        index = first_index + number
        with graph.db:
            rid = identity('region', [source_id, 'retained-html-figure', number])
            graph.node(rid, 'region', source['label'] + ': figure ' + str(number + 1), {
                'source_id': source_id, 'source_version': payload['content_hash'], 'region_index': index,
                'text': parsed.text[start:end], 'figure_number': number + 1, 'caption': image['alt'],
                'image_path': str(target.resolve()), 'image_hash': hashlib.sha256(target.read_bytes()).hexdigest(),
                'context_offset_start': start, 'context_offset_end': end, 'context_basis': 'retained original HTML',
                'map_state': 'unread', 'refine_state': 'unread', 'visual_review_required': True,
                'extraction_status': 'retained-html-figure'})
            graph.edge(rid, 'part-of', source_id, 'source-stated', {'source_version': payload['content_hash']})
