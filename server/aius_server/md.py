"""把模型回答里的 Markdown 转成 HTML（只支持常用的几种写法）。

所有文字都先转义再拼标签，所以回答里即使带 HTML 也只会原样显示成文字，不会被浏览器执行。
"""

from __future__ import annotations

import re
from html import escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d+[.、)]\s+(.*)$")


def inline(text: str) -> str:
    out = escape(text, quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    return _BOLD.sub(r"<strong>\1</strong>", out)


def render(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.strip().startswith("```"):  # 代码块：原样显示
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + escape("\n".join(block), quote=False) + "</pre>")
            continue
        if m := _HEAD.match(line):
            out.append(f"<h4>{inline(m.group(2))}</h4>")
            i += 1
            continue
        if _ROW.match(line) and i + 1 < len(lines) and _RULE.match(lines[i + 1]):
            head = [c.strip() for c in _ROW.match(line).group(1).split("|")]
            i += 2
            body = []
            while i < len(lines) and _ROW.match(lines[i]):
                body.append([c.strip() for c in _ROW.match(lines[i]).group(1).split("|")])
                i += 1
            rows = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in body)
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head) + f"</tr></thead><tbody>{rows}</tbody></table></div>")
            continue
        for pattern, tag in ((_BULLET, "ul"), (_NUMBER, "ol")):
            if pattern.match(line):
                items = []
                while i < len(lines) and (m := pattern.match(lines[i])):
                    items.append(f"<li>{inline(m.group(1))}</li>")
                    i += 1
                out.append(f"<{tag}>{''.join(items)}</{tag}>")
                break
        else:
            if re.fullmatch(r"\s*-{3,}\s*", line):
                out.append("<hr />")
                i += 1
                continue
            para = []
            while i < len(lines) and lines[i].strip() and not (_HEAD.match(lines[i]) or _BULLET.match(lines[i]) or _NUMBER.match(lines[i]) or lines[i].strip().startswith("```")):
                para.append(inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<p>" + "<br />".join(para) + "</p>")
    return "".join(out)
