"""Render LaTeX fragments with LuaLaTeX and PyMuPDF; embed PNGs in ETQ HTML.
Only process trusted catalog data. -no-shell-escape is not a full TeX sandbox.
"""
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import pymupdf

PREAMBLE = r"""\documentclass[border=1pt]{standalone}
\usepackage{amsmath}
\usepackage{fontspec}
\usepackage{unicode-math}
\setmainfont{Latin Modern Roman}
\setmathfont{Latin Modern Math}
\pagestyle{empty}
"""

# Delimiters are scanned rather than removed/replaced before rendering.
OUTSIDE_COMMAND = re.compile(r"\\([A-Za-z]+)")
TEXT_COMMANDS = {'textrm', 'text', 'textbf', 'textit', 'textsf', 'texttt', 'emph'}
GROUP_COMMANDS = TEXT_COMMANDS | {'mathrm', 'mathbf', 'mathit', 'mathsf', 'mathtt', 'mathcal', 'mathbb', 'sqrt'}
SYMBOL_COMMANDS = {'mu', 'Omega', 'omega', 'alpha', 'beta', 'gamma', 'delta', 'Delta', 'lambda', 'pi', 'phi', 'varphi', 'theta', 'sigma', 'rho', 'eta', 'epsilon', 'varepsilon', 'cdot', 'times', 'approx', 'leq', 'geq', 'neq', 'pm', 'infty', 'degree'}


def escaped(text, pos):
    n = 0
    pos -= 1
    while pos >= 0 and text[pos] == '\\':
        n += 1
        pos -= 1
    return bool(n % 2)


def split_math(text):
    """Yield (math_mode, source, display). Raise on unclosed delimiters."""
    start = i = 0
    while i < len(text):
        if escaped(text, i):
            i += 1
            continue
        opening = None
        for token, closing, display in [('$$', '$$', True), (r'\[', r'\]', True), (r'\(', r'\)', False), ('$', '$', False)]:
            if text.startswith(token, i):
                opening = (token, closing, display)
                break
        if opening is None:
            i += 1
            continue
        token, closing, display = opening
        if i > start:
            yield False, text[start:i], False
        end = i + len(token)
        while True:
            end = text.find(closing, end)
            if end < 0:
                raise ValueError(f'Nicht geschlossenes Mathematik-Trennzeichen {token!r}: {text[:150]}')
            if not escaped(text, end):
                break
            end += len(closing)
        yield True, text[i+len(token):end], display
        i = end + len(closing)
        start = i
    if start < len(text):
        yield False, text[start:], False


def braced_end(text, start):
    if start >= len(text) or text[start] != '{':
        raise ValueError('Erwartete geschweifte Klammer fehlt.')
    depth = 0
    for i in range(start, len(text)):
        if escaped(text, i):
            continue
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError('Nicht geschlossene geschweifte Klammer.')


# Explizit bekannte Einheitenmakros des Eingabekatalogs.
CATALOG_MACROS = {
    # Einheitenmakros aus dem BNetzA-Katalog
    "milliOhm": "mΩ",
    "mOhm": "mΩ",
    "kiloOhm": "kΩ",

    # Anführungszeichen im normalen Text
    "glqq": "„",
    "grqq": "“",
}


def normalize_catalog_macros(text: str) -> str:
    def replace(match):
        name = match.group(1)
        return CATALOG_MACROS.get(name, match.group(0))

    # Nur vollständige Befehlsnamen ersetzen.
    # Ein optionales leeres Argument {} wird mit entfernt.
    return re.sub(
        r"\\([A-Za-z]+)(?:\{\})?",
        replace,
        text,
    )


class LatexRenderer:
    def __init__(self, work_dir, cache_dir, dpi=192, display_dpi=96, font_pt=11, timeout=60, preamble=None):
        self.engine = shutil.which('lualatex')
        if not self.engine:
            raise RuntimeError('lualatex nicht im PATH gefunden. PowerShell: lualatex --version')

        self.pdf = pymupdf
        self.work_dir = Path(work_dir)
        self.output_dir = self.work_dir / 'formula'
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.dpi, self.display_dpi, self.font_pt = dpi, display_dpi, font_pt
        self.timeout = timeout
        self.preamble = PREAMBLE if preamble is None else preamble
        self.rendered = self.cache_hits = 0

    def render_fragment(self, source, math_mode=True, display=False):
        body = (r'\(\displaystyle ' + source + r'\)' if display else r'\(' + source + r'\)') if math_mode else source
        document = (self.preamble + '\n\\begin{document}\n'
                    + f'\\fontsize{{{self.font_pt}}}{{{self.font_pt*1.25}}}\\selectfont\n'
                    + body + '\n\\end{document}\n')
        digest = hashlib.sha256((document + f'\nDPI={self.dpi};display={self.display_dpi}').encode()).hexdigest()
        png = self.cache_dir / (digest + '.png')
        info = self.cache_dir / (digest + '.json')
        if png.is_file() and info.is_file():
            dimensions = json.loads(info.read_text(encoding='utf-8'))
            self.cache_hits += 1
        else:
            with tempfile.TemporaryDirectory(prefix='bnetza_tex_') as tmp:
                tmp = Path(tmp)
                (tmp/'formula.tex').write_text(document, encoding='utf-8')
                try:
                    result = subprocess.run(
                        [self.engine, '-no-shell-escape', '-interaction=nonstopmode',
                         '-halt-on-error', '-file-line-error', 'formula.tex'],
                        cwd=tmp, capture_output=True, timeout=self.timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f'LaTeX-Timeout fuer {source!r}') from exc
                pdf_path = tmp/'formula.pdf'
                if result.returncode or not pdf_path.is_file():
                    log = (tmp/'formula.log').read_text(encoding='utf-8', errors='replace') if (tmp/'formula.log').exists() else result.stdout.decode(errors='replace')
                    log_path = self.cache_dir/(digest+'.error.log')
                    log_path.write_text(log, encoding='utf-8')
                    (self.cache_dir/(digest+'.error.tex')).write_text(document, encoding='utf-8')
                    raise RuntimeError(f'LaTeX-Fehler bei {source!r}; Details: {log_path}')
                with self.pdf.open(pdf_path) as pdf:
                    if len(pdf) != 1:
                        raise RuntimeError(f'Erwartet eine Formelseite, erhalten {len(pdf)}: {source!r}')
                    page = pdf[0]
                    if max(page.rect.width, page.rect.height) > 2000:
                        raise RuntimeError(f'Formelseite unerwartet gross: {source!r}')
                    pix = page.get_pixmap(dpi=self.dpi, alpha=False)
                    pix.save(png)
                    dimensions = {'width': max(1, round(page.rect.width*self.display_dpi/72)),
                                  'height': max(1, round(page.rect.height*self.display_dpi/72))}
                info.write_text(json.dumps(dimensions), encoding='utf-8')
                self.rendered += 1
        target = self.output_dir/png.name
        if not target.exists():
            shutil.copy2(png, target)
        ref = 'formula/' + png.name
        image = (f'<img src="{ref}" width="{dimensions["width"]}" '
                 f'height="{dimensions["height"]}" alt="{html.escape(source, quote=True)}">')
        return '<p>'+image+'</p>' if display else image

    def render_plain(self, text):
        # Handle a small, explicit set of standalone TeX commands outside math.
        result = []
        pos = 0
        while True:
            match = OUTSIDE_COMMAND.search(text, pos)
            if match is None:
                result.append(text[pos:])
                break
            if escaped(text, match.start()):
                result.append(text[pos:match.end()])
                pos = match.end()
                continue
            result.append(text[pos:match.start()])
            command = match.group(1)
            end = match.end()
            if command in GROUP_COMMANDS:
                while end < len(text) and text[end].isspace():
                    end += 1
                end = braced_end(text, end)
                snippet = text[match.start():end]
                result.append(self.render_fragment(snippet, math_mode=command not in TEXT_COMMANDS))
            elif command in SYMBOL_COMMANDS:
                result.append(self.render_fragment(text[match.start():end]))
            else:
                raise ValueError(f'LaTeX-Befehl ausserhalb eines Mathematikbereichs: \\{command}. '
                                 'Bitte Ausdruck pruefen; nicht stillschweigend entfernt.')
            pos = end
        return ''.join(result)

    def render_text(self, text):
        text = "" if text is None else str(text)
        text = normalize_catalog_macros(text)

        return "".join(
            self.render_fragment(source, True, display)
            if math_mode
            else self.render_plain(source)
            for math_mode, source, display in split_math(text)
        )
