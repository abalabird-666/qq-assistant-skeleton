"""极简 docx 生成器：不依赖 python-docx，直接拼 OOXML 再 zip。

只做三件事：标题、小标题、正文段落。够用就行。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

SECT = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')


def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _para(text: str, *, size: int = 22, bold: bool = False,
          italic: bool = False, align: str = "") -> str:
    rpr = []
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    rpr.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    ppr = f'<w:pPr><w:jc w:val="{align}"/></w:pPr>' if align else ""
    return (f"<w:p>{ppr}<w:r><w:rPr>{''.join(rpr)}</w:rPr>"
            f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:p>')


def build_docx(path: Path, title: str, body_md: str) -> Path:
    """把 markdown 风格文本转成 docx。支持 #, ##, ###, - 列表, 1. 列表, **粗体**。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    if title.strip():
        parts.append(_para(title.strip(), size=36, bold=True, align="center"))
        parts.append(_para("", size=12))

    for raw in body_md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            parts.append(_para("", size=14))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line.strip())
        if m:
            level = len(m.group(1))
            size = {1: 30, 2: 26, 3: 24, 4: 23}[level]
            parts.append(_para(m.group(2), size=size, bold=True))
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            parts.append(_para("· " + m.group(1), size=22))
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if m:
            parts.append(_para(f"{m.group(1)}. {m.group(2)}", size=22))
            continue
        # 行内 **粗体** 退化成纯文本（简化）
        parts.append(_para(re.sub(r"\*\*(.+?)\*\*", r"\1", line), size=22))

    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>" + "".join(parts) + SECT + "</w:body></w:document>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)
    return path


def build_txt(path: Path, title: str, body_md: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((title + "\n\n" if title else "") + body_md, encoding="utf-8")
    return path


if __name__ == "__main__":
    p = build_docx(Path(r"C:\Users\yourname\.dsh\qq-duty\out\_selftest.docx"),
                   "自测标题", "# 一级标题\n## 二级\n- 要点一\n- 要点二\n1. 第一\n2. 第二\n普通正文一行。")
    print(p, p.stat().st_size)
