"""Инструкция и правила работы в «Справке» — как свёрстанная страница, а не как текст в «Блокноте».

Файлы docs/*.txt остаются простым текстом шириной ≤ 101 знак (их печатают и носят на флешке). Для показа в программе
текст переводится в HTML:
  * шапка между строками «=====» — заголовок документа и подзаголовок;
  * «=====» / «N. ЗАГОЛОВОК» / «=====» — заголовок раздела обычным регистром (берётся из содержания документа);
  * «СОДЕРЖАНИЕ» — пункты содержания становятся ссылками на разделы;
  * остальное — моноширинными блоками как есть: в тексте много колонок, выровненных пробелами (словарь терминов,
    кнопки и их назначение), и пропорциональный шрифт их бы разрушил.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List

from . import design

_EQ = re.compile(r"^\s*={10,}\s*$")
_DASH = re.compile(r"^\s*-{10,}\s*$")
_TOC_ITEM = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
_SECTION = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")


def _toc(lines: List[str]) -> Dict[str, str]:
    """{номер раздела: название из содержания (обычным регистром)}."""
    out: Dict[str, str] = {}
    for i, ln in enumerate(lines):
        if ln.strip().upper() == "СОДЕРЖАНИЕ" and i + 1 < len(lines) and _DASH.match(lines[i + 1]):
            for ln2 in lines[i + 2:]:
                if not ln2.strip():
                    if out:
                        break
                    continue
                m = _TOC_ITEM.match(ln2)
                if not m:
                    break
                out[m.group(1)] = m.group(2)
            break
    return out


def _sentence(caps: str) -> str:
    """«КАК ПОНИМАТЬ ПОДСКАЗКИ «ТЕКУЩЕГО ПРОХОДА»» → «Как понимать подсказки «Текущего прохода»» (латиница — как есть)."""
    words = caps.split(" ")
    out = []
    for w in words:
        out.append(w if re.search(r"[A-Za-z]", w) else w.lower())
    s = " ".join(out)
    s = re.sub(r"«(\w)", lambda m: "«" + m.group(1).upper(), s)
    return s[:1].upper() + s[1:]


def to_html(text: str, t: design.Tokens = None) -> str:
    t = t or design.tokens()
    lines = text.replace("\r\n", "\n").split("\n")
    toc = _toc(lines)
    f = design.fonts()
    num_family = html.escape(f["num"])
    parts: List[str] = []
    pre: List[str] = []
    in_toc = False

    def flush() -> None:
        while pre and not pre[-1].strip():
            pre.pop()
        while pre and not pre[0].strip():
            pre.pop(0)
        if pre:
            parts.append("<pre>" + "\n".join(pre) + "</pre>")
        pre.clear()

    i = 0
    n = len(lines)
    first = True
    while i < n:
        ln = lines[i]
        # шапка документа или заголовок раздела: «=====», строки, «=====»
        if _EQ.match(ln):
            j = i + 1
            block = []
            while j < n and not _EQ.match(lines[j]):
                block.append(lines[j].strip())
                j += 1
            if j < n and 0 < len(block) <= 3:
                flush()
                in_toc = False
                if first:
                    title = block[0]
                    if re.search(r"[А-ЯЁ]{4,}", title):         # «DryerCalc 0.9.1 — ИНСТРУКЦИЯ …» → обычный регистр
                        head, _, rest = title.partition(" — ")
                        title = f"{head} — {_sentence(rest)}" if rest else _sentence(title)
                    parts.append(f"<h1 style=\"font-family:'{num_family}'\">{html.escape(title)}</h1>")
                    for sub in block[1:]:
                        parts.append(f"<p class='lead'>{html.escape(sub)}</p>")
                else:
                    m = _SECTION.match(block[0])
                    if m:
                        title = toc.get(m.group(1)) or _sentence(m.group(2))
                        parts.append(f"<a name='s{m.group(1)}'></a><h2 style=\"font-family:'{num_family}'\">"
                                     f"{m.group(1)}. {html.escape(title)}</h2>")
                    else:
                        parts.append(f"<h2 style=\"font-family:'{num_family}'\">{html.escape(_sentence(block[0]))}"
                                     f"</h2>")
                    for sub in block[1:]:
                        parts.append(f"<p class='lead'>{html.escape(sub)}</p>")
                first = False
                i = j + 1
                continue
            i += 1                      # одиночная линия «=====» (не заголовок) — просто разделитель, не показываем
            continue
        # подзаголовок: строка и под ней «-----»
        if i + 1 < n and ln.strip() and _DASH.match(lines[i + 1]):
            flush()
            head = ln.strip()
            in_toc = head.upper() == "СОДЕРЖАНИЕ"
            parts.append(f"<h3 style=\"font-family:'{num_family}'\">"
                         f"{html.escape(_sentence(head) if head.isupper() else head)}</h3>")
            i += 2
            continue
        if in_toc:
            m = _TOC_ITEM.match(ln)
            if m:
                flush()
                parts.append(f"<p class='toc'>{m.group(1)}.&nbsp; <a href='#s{m.group(1)}'>"
                             f"{html.escape(m.group(2))}</a></p>")
                i += 1
                continue
            if ln.strip():
                in_toc = False
        pre.append(html.escape(ln))
        i += 1
    flush()
    css = (f"body {{ color:{t.ink}; }}"
           f"h1 {{ font-size:20pt; font-weight:600; color:{t.ink}; margin:4px 0 2px 0; }}"
           f"h2 {{ font-size:15pt; font-weight:600; color:{t.ink}; margin:22px 0 6px 0; }}"
           f"h3 {{ font-size:12pt; font-weight:600; color:{t.ink2}; margin:14px 0 4px 0; }}"
           f"p.lead {{ color:{t.muted}; margin:0 0 2px 0; }}"
           f"p.toc {{ margin:1px 0 1px 8px; }}"
           f"a {{ color:{t.steel}; text-decoration:none; }}"
           f"pre {{ font-family:'Consolas','Cascadia Mono','DejaVu Sans Mono',monospace; font-size:10pt; "
           f"color:{t.ink}; margin:4px 0 8px 0; }}")
    return f"<html><head><style>{css}</style></head><body>{''.join(parts)}</body></html>"
