"""Apply narrow, reversible integration edits to composite main API and HTML."""
from pathlib import Path


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('integration anchor must occur exactly once: ' + old[:100])
    return text.replace(old, new, 1)


def patch_app(text):
    if 'SYNTHESIS_MATERIAL_SOURCE_V1' in text:
        raise ValueError('synthesis material upload already installed')
    for method in ('GET', 'POST'):
        anchor = '    def do_' + method + '(self):\n'
        code = (anchor + '        # SYNTHESIS_MATERIAL_SOURCE_V1\n'
                '        if re.fullmatch(r"/api/drama-material/jobs/[0-9a-f]{32}/material-source", urlparse(self.path).path):\n'
                '            from features.drama_synthesis.material_source import handle\n'
                '            handle(self, "' + method + '", __import__("sys").modules[__name__])\n'
                '            return\n')
        text = replace_once(text, anchor, code)
    anchor = 'def decorate_drama_synthesis_job(job):\n'
    start = text.index(anchor)
    end = text.index('\ndef ', start + len(anchor))
    function = text[start:end]
    function = replace_once(function, '    return item\n',
        '    import sys\n    from features.drama_synthesis.material_source import decorate\n'
        '    return decorate(item, sys.modules[__name__])\n')
    return text[:start] + function + text[end:]


def patch_html(text, source):
    if 'SYNTHESIS_MATERIAL_SOURCE_V1' in text:
        raise ValueError('synthesis material upload already installed')
    text = replace_once(text, '    async function copyMaterialUrl(jobId) {', source + '\n    async function copyMaterialUrl(jobId) {')
    for anchor in (
        '        <button class="btn btn-secondary mini" type="button" onclick="event.stopPropagation(); copyMaterialUrl',
        '        <button class="btn btn-secondary" type="button" onclick="copyMaterialUrl',
    ):
        text = replace_once(text, anchor, '        ${materialUploadActions(job)}\n' + anchor)
    import re
    text, count = re.subn(r'drama-job-runtime.js\?v=[^"\s]+', 'drama-job-runtime.js?v=20261103-material-source', text)
    if count != 1:
        raise ValueError('runtime script version anchor missing')
    return text


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    p = root / 'app.py'
    p.write_text(patch_app(p.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
    p = root / 'static/index.html'
    source = (root / 'static/drama-material-source.js').read_text(encoding='utf-8')
    p.write_text(patch_html(p.read_text(encoding='utf-8'), source), encoding='utf-8', newline='\n')
