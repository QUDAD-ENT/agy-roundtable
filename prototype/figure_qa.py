"""
Pure-Standard-Library Publication-Grade Scientific Figure QA & Backend Probe (Zero Pillow/PyMuPDF deps).
Checks:
1. Python (matplotlib/seaborn) & R (Rscript + ggplot2/patchwork/ComplexHeatmap) runtime availability.
2. Binary TIFF IFD parsing (XResolution/YResolution/ResolutionUnit + physical mm vs pixel linkage -> catches forged 600 DPI tags!).
3. PNG IHDR + pHYs parsing, SVG XML viewBox/raster-trap/font-size floor inspection, and PDF header/page/MediaBox checks.
4. Panel-level source-data semantic consistency against full frozen CSV/TSV (catches swapped group labels & raw-P vs adjusted-P misuse!).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def probe_plotting_backend(backend: str = "all") -> Dict[str, Any]:
    """Detect real Python (matplotlib/seaborn) and R (Rscript/ggplot2/patchwork/ComplexHeatmap) runtimes."""
    out: Dict[str, Any] = {}
    want = (backend or "all").strip().lower()

    if want in ("all", "python"):
        py_info: Dict[str, Any] = {"available": False, "executable": sys.executable, "packages": {}, "error": ""}
        code = (
            "import importlib.metadata as md, json, sys\n"
            "pkgs = {}\n"
            "for name in ('matplotlib', 'seaborn', 'numpy', 'pandas', 'scipy', 'statsmodels'):\n"
            "    try:\n"
            "        pkgs[name] = md.version(name)\n"
            "    except Exception:\n"
            "        pkgs[name] = None\n"
            "print(json.dumps({'python': sys.version.split()[0], 'packages': pkgs}))\n"
        )
        candidates_py: List[str] = [sys.executable]
        for cand_name in ("python.exe", "python", "python3"):
            found_p = shutil.which(cand_name)
            if found_p and os.path.normcase(found_p) not in {os.path.normcase(x) for x in candidates_py}:
                candidates_py.append(found_p)
        for py_exe in candidates_py:
            try:
                r = subprocess.run([py_exe, "-c", code], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    data = json.loads(r.stdout.strip().splitlines()[-1])
                    pkgs = data.get("packages") or {}
                    has_mpl = bool(pkgs.get("matplotlib"))
                    if has_mpl or not py_info.get("version"):
                        py_info["executable"] = py_exe
                        py_info["version"] = data.get("python")
                        py_info["packages"] = pkgs
                        py_info["available"] = has_mpl
                    if has_mpl:
                        break
                elif not py_info.get("error"):
                    py_info["error"] = (r.stderr or r.stdout).strip()[:200]
            except Exception as exc:
                if not py_info.get("error"):
                    py_info["error"] = str(exc)
        out["python"] = py_info

    if want in ("all", "r"):
        r_exe = shutil.which("Rscript.exe") or shutil.which("Rscript")
        r_info: Dict[str, Any] = {"available": False, "executable": r_exe or "", "packages": {}, "error": ""}
        if r_exe:
            r_expr = (
                "pkgs <- c('ggplot2','patchwork','ComplexHeatmap','svglite','ragg','ggrepel'); "
                "vers <- sapply(pkgs, function(p) if (requireNamespace(p, quietly=TRUE)) as.character(packageVersion(p)) else 'NONE'); "
                "cat(paste(names(vers), vers, sep='=', collapse=';'))"
            )
            try:
                r = subprocess.run([r_exe, "-e", r_expr], capture_output=True, text=True, timeout=12)
                if r.returncode == 0 and "=" in r.stdout:
                    pkgs: Dict[str, Optional[str]] = {}
                    for pair in r.stdout.strip().split(";"):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            pkgs[k.strip()] = None if v.strip() == "NONE" else v.strip()
                    r_info["packages"] = pkgs
                    r_info["available"] = bool(pkgs.get("ggplot2"))
                else:
                    r_info["error"] = (r.stderr or r.stdout).strip()[:200]
            except Exception as exc:
                r_info["error"] = str(exc)
        else:
            r_info["error"] = "Rscript not found in PATH"
        out["r"] = r_info

    return out


def inspect_png(path: str) -> Dict[str, Any]:
    """Parse PNG IHDR (dimensions) and optional pHYs (DPI) using pure stdlib struct."""
    size = os.path.getsize(path)
    if size < 24:
        return {"ok": False, "format": "png", "error": "PNG file too small or empty", "size": size}
    with open(path, "rb") as f:
        header = f.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return {"ok": False, "format": "png", "error": "Invalid PNG signature/IHDR", "size": size}
        width, height = struct.unpack(">II", header[16:24])
        # Scan up to first 64KB for pHYs chunk
        f.seek(8)
        buf = f.read(65536)
    dpi_x: Optional[float] = None
    dpi_y: Optional[float] = None
    pos = 0
    while pos + 12 <= len(buf):
        chunk_len = struct.unpack(">I", buf[pos : pos + 4])[0]
        chunk_type = buf[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + chunk_len
        if chunk_type == b"pHYs" and chunk_len == 9 and data_end <= len(buf):
            ppu_x, ppu_y, unit = struct.unpack(">IIB", buf[data_start:data_end])
            if unit == 1:  # pixels per meter
                dpi_x = round(ppu_x * 0.0254, 1)
                dpi_y = round(ppu_y * 0.0254, 1)
            break
        if chunk_type == b"IDAT" or chunk_type == b"IEND":
            break
        pos = data_end + 4
    return {
        "ok": width > 0 and height > 0,
        "format": "png",
        "width_px": width,
        "height_px": height,
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "size": size,
    }


def _read_tiff_rational(data: bytes, offset: int, endian: str) -> Optional[float]:
    if offset < 0 or offset + 8 > len(data):
        return None
    num, den = struct.unpack(endian + "II", data[offset : offset + 8])
    if den == 0:
        return None
    return num / den


def inspect_tiff(path: str) -> Dict[str, Any]:
    """Parse classic TIFF IFD0 tags (ImageWidth, ImageLength, XResolution, YResolution, ResolutionUnit)."""
    size = os.path.getsize(path)
    if size < 16:
        return {"ok": False, "format": "tiff", "error": "TIFF file too small", "size": size}
    with open(path, "rb") as f:
        # Read enough of file to cover header + IFD + rational tables (up to 4MB or full if smaller)
        data = f.read(min(size, 4 * 1024 * 1024))
    bo = data[:2]
    if bo == b"II":
        endian = "<"
    elif bo == b"MM":
        endian = ">"
    else:
        return {"ok": False, "format": "tiff", "error": "Invalid TIFF byte order header", "size": size}
    magic = struct.unpack(endian + "H", data[2:4])[0]
    if magic != 42:
        return {"ok": False, "format": "tiff", "error": f"Unsupported TIFF magic={magic} (BigTIFF requires manual check)", "size": size}
    ifd_off = struct.unpack(endian + "I", data[4:8])[0]
    if ifd_off + 2 > len(data):
        # IFD located near EOF in large TIFF -> seek directly to IFD offset
        with open(path, "rb") as f:
            f.seek(ifd_off)
            num_entries_raw = f.read(2)
            if len(num_entries_raw) < 2:
                return {"ok": False, "format": "tiff", "error": "Truncated TIFF IFD", "size": size}
            n_entries = struct.unpack(endian + "H", num_entries_raw)[0]
            entries_raw = f.read(n_entries * 12)
            tags: Dict[int, Tuple[int, int, bytes]] = {}
            for i in range(n_entries):
                chunk = entries_raw[i * 12 : (i + 1) * 12]
                if len(chunk) < 12:
                    break
                tag, typ, cnt = struct.unpack(endian + "HHI", chunk[:8])
                tags[tag] = (typ, cnt, chunk[8:12])

            def _val_from_file(tag_id: int) -> Optional[float]:
                if tag_id not in tags:
                    return None
                typ, _cnt, raw4 = tags[tag_id]
                if typ == 3:  # SHORT
                    return float(struct.unpack(endian + "H", raw4[:2])[0])
                if typ == 4:  # LONG
                    return float(struct.unpack(endian + "I", raw4)[0])
                if typ == 5:  # RATIONAL
                    off = struct.unpack(endian + "I", raw4)[0]
                    f.seek(off)
                    rb = f.read(8)
                    if len(rb) == 8:
                        num, den = struct.unpack(endian + "II", rb)
                        return (num / den) if den else None
                return None

            w = int(_val_from_file(256) or 0)
            h = int(_val_from_file(257) or 0)
            xr = _val_from_file(282)
            yr = _val_from_file(283)
            unit = int(_val_from_file(296) or 2)
    else:
        n_entries = struct.unpack(endian + "H", data[ifd_off : ifd_off + 2])[0]
        tags = {}
        pos = ifd_off + 2
        for _ in range(n_entries):
            if pos + 12 > len(data):
                break
            tag, typ, cnt = struct.unpack(endian + "HHI", data[pos : pos + 8])
            tags[tag] = (typ, cnt, data[pos + 8 : pos + 12])
            pos += 12

        def _val_from_buf(tag_id: int) -> Optional[float]:
            if tag_id not in tags:
                return None
            typ, _cnt, raw4 = tags[tag_id]
            if typ == 3:
                return float(struct.unpack(endian + "H", raw4[:2])[0])
            if typ == 4:
                return float(struct.unpack(endian + "I", raw4)[0])
            if typ == 5:
                off = struct.unpack(endian + "I", raw4)[0]
                return _read_tiff_rational(data, off, endian)
            return None

        w = int(_val_from_buf(256) or 0)
        h = int(_val_from_buf(257) or 0)
        xr = _val_from_buf(282)
        yr = _val_from_buf(283)
        unit = int(_val_from_buf(296) or 2)  # 2=inch, 3=cm

    dpi_x = round(xr * 2.54, 1) if (xr and unit == 3) else (round(xr, 1) if (xr and unit == 2) else None)
    dpi_y = round(yr * 2.54, 1) if (yr and unit == 3) else (round(yr, 1) if (yr and unit == 2) else None)
    phys_w_mm = round((w / dpi_x) * 25.4, 1) if (w and dpi_x) else None
    phys_h_mm = round((h / dpi_y) * 25.4, 1) if (h and dpi_y) else None

    return {
        "ok": w > 0 and h > 0,
        "format": "tiff",
        "width_px": w,
        "height_px": h,
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "resolution_unit": {1: "none", 2: "inch", 3: "cm"}.get(unit, f"code_{unit}"),
        "physical_width_mm": phys_w_mm,
        "physical_height_mm": phys_h_mm,
        "size": size,
    }


def inspect_svg(path: str, allow_raster_panels: bool = False) -> Dict[str, Any]:
    """Parse SVG XML to check viewBox, vector paths vs accidental raster-only embedding, and font-size floor."""
    size = os.path.getsize(path)
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as exc:
        return {"ok": False, "format": "svg", "error": f"Invalid SVG XML: {exc}", "size": size}

    viewbox = root.attrib.get("viewBox") or root.attrib.get("viewbox") or ""
    width_attr = root.attrib.get("width") or ""
    height_attr = root.attrib.get("height") or ""

    vector_count = 0
    image_count = 0
    text_count = 0
    min_font_pt: Optional[float] = None

    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag in ("path", "rect", "circle", "line", "polyline", "polygon", "ellipse"):
            vector_count += 1
        elif tag == "image":
            image_count += 1
        elif tag in ("text", "tspan"):
            text_count += 1
            fs_raw = elem.attrib.get("font-size") or ""
            style = elem.attrib.get("style") or ""
            if not fs_raw and "font-size" in style:
                m_fs = re.search(r"font-size\s*:\s*([0-9.]+)\s*(pt|px)?", style, re.I)
                if m_fs:
                    fs_raw = m_fs.group(1) + (m_fs.group(2) or "")
            m_num = re.match(r"^\s*([0-9.]+)\s*(pt|px)?", fs_raw, re.I)
            if m_num:
                val = float(m_num.group(1))
                unit = (m_num.group(2) or "px").lower()
                pt_val = val if unit == "pt" else (val * 0.75)
                if pt_val > 0 and (min_font_pt is None or pt_val < min_font_pt):
                    min_font_pt = round(pt_val, 2)

    warnings: List[str] = []
    blocking: List[str] = []
    if not viewbox and not (width_attr and height_attr):
        blocking.append("SVG 缺少 viewBox 且无 width/height 尺寸声明")
    if image_count > 0 and vector_count < 8 and not allow_raster_panels:
        blocking.append(f"SVG 疑似整图栅格伪矢量（image={image_count}, vector_primitives={vector_count}），未声明为影像/热图面板")
    if min_font_pt is not None and min_font_pt < 4.5:
        warnings.append(f"SVG 检测到极小字号 ({min_font_pt}pt < 4.5pt)，印刷缩小后可能不可读")

    return {
        "ok": len(blocking) == 0,
        "format": "svg",
        "viewBox": viewbox,
        "width": width_attr,
        "height": height_attr,
        "vector_primitives": vector_count,
        "embedded_images": image_count,
        "text_nodes": text_count,
        "min_font_pt": min_font_pt,
        "blocking": blocking,
        "warnings": warnings,
        "size": size,
    }


def inspect_pdf(path: str) -> Dict[str, Any]:
    """Basic pure-stdlib PDF header, MediaBox, and page-count inspection."""
    size = os.path.getsize(path)
    if size < 64:
        return {"ok": False, "format": "pdf", "error": "PDF file too small", "size": size}
    with open(path, "rb") as f:
        head = f.read(16)
        f.seek(0)
        body = f.read(min(size, 512 * 1024))
    if not head.startswith(b"%PDF-"):
        return {"ok": False, "format": "pdf", "error": "Invalid PDF magic header", "size": size}
    pages = len(re.findall(rb"/Type\s*/Page\b", body))
    mb = re.search(rb"/MediaBox\s*\[\s*([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s*\]", body)
    width_mm = height_mm = None
    if mb:
        x0, y0, x1, y1 = (float(mb.group(i)) for i in range(1, 5))
        width_mm = round(abs(x1 - x0) * 25.4 / 72.0, 1)
        height_mm = round(abs(y1 - y0) * 25.4 / 72.0, 1)
    return {
        "ok": True,
        "format": "pdf",
        "page_count_est": max(1, pages),
        "width_mm": width_mm,
        "height_mm": height_mm,
        "size": size,
        "manual_check_required": ["PDF 内嵌字体可编辑性与 CMYK/印刷色彩需人工抽检或矢量工具复核"],
    }


def inspect_any_figure(path: str, allow_raster_panels: bool = False) -> Dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return inspect_png(path)
    if ext in (".tif", ".tiff"):
        return inspect_tiff(path)
    if ext == ".svg":
        return inspect_svg(path, allow_raster_panels=allow_raster_panels)
    if ext == ".pdf":
        return inspect_pdf(path)
    return {"ok": os.path.isfile(path) and os.path.getsize(path) > 0, "format": ext.lstrip("."), "size": os.path.getsize(path)}


def _load_full_delimited_table(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    ext = os.path.splitext(path)[1].lower()
    delim = "\t" if ext == ".tsv" else ","
    with open(path, "rb") as f:
        raw = f.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or []) if h is not None]
    rows: List[Dict[str, str]] = []
    for r in reader:
        rows.append({(k.strip() if k else ""): (v.strip() if v is not None else "") for k, v in r.items()})
    return headers, rows


def _to_float(val: Any) -> Optional[float]:
    try:
        s = str(val).strip().replace(",", "")
        if not s or s.lower() in ("na", "nan", "none", "null", ""):
            return None
        return float(s)
    except Exception:
        return None


def verify_figure_manifest(manifest_path: str, work_dir: str) -> Dict[str, Any]:
    """
    Validate a figure_manifest.json against:
    1. Exported file existence, SHA-256 integrity, TIFF dual-axis 600 DPI + physical mm pixel linkage, SVG vector hygiene.
    2. Panel-level source-data semantic consistency against full CSV/TSV (catches label swaps, wrong N, raw-P vs adjusted-P!).
    """
    blocking: List[str] = []
    observations: List[str] = []
    manual_checks: List[str] = []
    artifacts: List[Dict[str, Any]] = []

    abs_manifest = os.path.normpath(os.path.abspath(manifest_path))
    if not os.path.isfile(abs_manifest):
        return {
            "ok": False,
            "status": "failed",
            "manifest_path": abs_manifest,
            "blocking": [f"图件契约文件不存在: {abs_manifest}"],
            "observations": [],
            "manual_checks": [],
            "artifacts": [],
        }

    try:
        with open(abs_manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "manifest_path": abs_manifest,
            "blocking": [f"figure_manifest.json 解析失败: {exc}"],
            "observations": [],
            "manual_checks": [],
            "artifacts": [],
        }

    base_dir = os.path.dirname(abs_manifest)
    root_real = os.path.realpath(work_dir)

    def _resolve_rel(p: str) -> str:
        if os.path.isabs(p):
            cand = os.path.realpath(p)
        else:
            c1 = os.path.realpath(os.path.join(base_dir, p))
            c2 = os.path.realpath(os.path.join(root_real, p))
            cand = c1 if os.path.exists(c1) else c2
        return cand

    # 1. Script & Source Data Hash checks
    for key_path, key_hash, label in (
        ("script_path", "script_sha256", "绘图脚本"),
        ("source_data_path", "source_data_sha256", "源数据表"),
    ):
        rp = manifest.get(key_path)
        exp_h = (manifest.get(key_hash) or "").strip().lower()
        if rp:
            ap = _resolve_rel(rp)
            if not os.path.isfile(ap):
                blocking.append(f"{label}缺失: {rp}")
            elif exp_h:
                act_h = sha256_file(ap).lower()
                if act_h != exp_h:
                    blocking.append(f"{label} SHA-256 漂移（脚本/数据在导出后被改动）: {rp}")

    # 2. Target journal dimensions & DPI expectations
    target_w_mm = _to_float(manifest.get("target_width_mm")) or 89.0
    target_h_mm = _to_float(manifest.get("target_height_mm"))
    min_dpi = _to_float(manifest.get("min_dpi")) or 300.0
    allow_raster = bool(manifest.get("allow_raster_panels", False))

    exports = manifest.get("exports") or manifest.get("outputs") or {}
    if isinstance(exports, list):
        exp_items = exports
    elif isinstance(exports, dict):
        exp_items = [{"format": k, **(v if isinstance(v, dict) else {"path": v})} for k, v in exports.items()]
    else:
        exp_items = []

    if not exp_items:
        blocking.append("figure_manifest.json 未声明任何导出图件 (exports)")

    for item in exp_items:
        rp = item.get("path") or ""
        if not rp:
            continue
        ap = _resolve_rel(rp)
        if not os.path.isfile(ap):
            blocking.append(f"声明的导出图件不存在: {rp}")
            continue
        act_h = sha256_file(ap).lower()
        exp_h = (item.get("sha256") or "").strip().lower()
        if exp_h and act_h != exp_h:
            blocking.append(f"导出图件 SHA-256 校验失败: {rp}")

        meta = inspect_any_figure(ap, allow_raster_panels=allow_raster)
        meta["path"] = ap
        meta["rel_path"] = rp
        meta["sha256"] = act_h
        artifacts.append(meta)

        if not meta.get("ok"):
            blocking.append(f"图件结构校验失败 ({rp}): {meta.get('error') or '; '.join(meta.get('blocking') or [])}")

        fmt = meta.get("format")
        if fmt == "tiff":
            dx, dy = meta.get("dpi_x"), meta.get("dpi_y")
            w_px, h_px = meta.get("width_px") or 0, meta.get("height_px") or 0
            if dx is None or dy is None:
                blocking.append(f"TIFF 缺少双轴 DPI 元数据 (XResolution/YResolution): {rp}")
            else:
                if dx < min_dpi - 1.0 or dy < min_dpi - 1.0:
                    blocking.append(f"TIFF 分辨率不足 ({dx}x{dy} DPI < 目标 {min_dpi} DPI): {rp}")
                # CRITICAL Anti-Spoofing Check: physical width * min_dpi MUST match actual pixel width!
                min_req_w_px = int((target_w_mm / 25.4) * min_dpi * 0.92)
                if w_px < min_req_w_px:
                    blocking.append(
                        f"TIFF 疑似伪造高 DPI 标签：标称 {dx} DPI 但实际像素宽仅 {w_px}px（目标宽 {target_w_mm}mm @ {min_dpi} DPI 至少需 {min_req_w_px}px）: {rp}"
                    )
                if target_h_mm:
                    min_req_h_px = int((target_h_mm / 25.4) * min_dpi * 0.92)
                    if h_px < min_req_h_px:
                        blocking.append(
                            f"TIFF 像素高不足：{h_px}px < 目标高 {target_h_mm}mm @ {min_dpi} DPI 所需 {min_req_h_px}px: {rp}"
                        )
        elif fmt == "svg":
            for b in meta.get("blocking") or []:
                blocking.append(f"{rp}: {b}")
            for w in meta.get("warnings") or []:
                observations.append(f"{rp}: {w}")
        elif fmt == "pdf":
            manual_checks.extend(meta.get("manual_check_required") or [])

    # 3. Panel-Level Source-Data Semantic Consistency Checks (Solves H-E2!)
    panel_sources = manifest.get("panel_sources") or []
    table_cache: Dict[str, Tuple[List[str], List[Dict[str, str]]]] = {}
    for ps in panel_sources:
        pid = str(ps.get("panel_id") or "panel")
        sp = ps.get("source_table") or manifest.get("source_data_path") or ""
        if not sp:
            observations.append(f"面板 {pid} 未绑定 source_table，跳过语义核验")
            continue
        ap = _resolve_rel(sp)
        if not os.path.isfile(ap):
            blocking.append(f"面板 {pid} 绑定的源数据表不存在: {sp}")
            continue
        if ap not in table_cache:
            table_cache[ap] = _load_full_delimited_table(ap)
        headers, rows = table_cache[ap]

        # Check key column / group label mapping & value consistency
        key_col = ps.get("key_col")
        expected_rows = ps.get("expected_rows") or []
        val_col = ps.get("value_col")
        p_col = ps.get("p_col")
        require_adjusted_p = bool(ps.get("require_adjusted_p", False))

        if p_col:
            if p_col not in headers:
                blocking.append(f"面板 {pid} 声明的 P 值列 '{p_col}' 不存在于源表 {sp}（可用列共 {len(headers)} 列）")
            elif require_adjusted_p:
                low_p = p_col.lower()
                if not any(tok in low_p for tok in ("adj", "fdr", "q_", "qval", "holm", "bonf", "bh")):
                    blocking.append(
                        f"面板 {pid} 要求多重校正 P 值，但绑定列 '{p_col}' 疑似原始未校正 P 值列"
                    )

        if key_col and key_col not in headers:
            blocking.append(f"面板 {pid} 的分组/键列 '{key_col}' 不存在于源表 {sp}")
            continue

        row_index = {r.get(key_col, ""): r for r in rows} if key_col else {}
        for er in expected_rows:
            k_val = str(er.get("key") or "")
            if key_col and k_val not in row_index:
                blocking.append(f"面板 {pid} 标注的组别/键 '{k_val}' 在源表 {sp} 中不存在（疑似标签拼错或伪造）")
                continue
            src_row = row_index.get(k_val, {})
            # Verify plotted value vs source table value (catches group-label swaps!)
            if val_col and "plotted_value" in er:
                if val_col not in headers:
                    blocking.append(f"面板 {pid} 效应列 '{val_col}' 不存在于源表 {sp}")
                    continue
                src_v = _to_float(src_row.get(val_col))
                plt_v = _to_float(er.get("plotted_value"))
                tol = _to_float(er.get("tol")) or 1e-3
                if src_v is None or plt_v is None:
                    blocking.append(f"面板 {pid} 组 '{k_val}' 数值缺失（src={src_row.get(val_col)}, plotted={er.get('plotted_value')}）")
                elif abs(src_v - plt_v) > max(tol, abs(src_v) * 1e-3):
                    blocking.append(
                        f"面板 {pid} 组 '{k_val}' 绘图值 ({plt_v}) 与源表 {val_col} ({src_v}) 不一致（疑似标签互换或数值漂移）"
                    )
            # Verify plotted N vs source N
            n_col = ps.get("n_col")
            if n_col and "plotted_n" in er:
                src_n = _to_float(src_row.get(n_col))
                plt_n = _to_float(er.get("plotted_n"))
                if src_n is None or plt_n is None or int(round(src_n)) != int(round(plt_n)):
                    blocking.append(
                        f"面板 {pid} 组 '{k_val}' 样本量 N 不一致：图注={er.get('plotted_n')} vs 源表 {n_col}={src_row.get(n_col)}"
                    )
            # Verify plotted p-value vs exact p_col (catches plotting raw p while claiming p_adj!)
            if p_col and p_col in headers and "plotted_p" in er:
                src_p = _to_float(src_row.get(p_col))
                plt_p = _to_float(er.get("plotted_p"))
                tol_p = _to_float(er.get("p_tol")) or 1e-4
                if src_p is None or plt_p is None or abs(src_p - plt_p) > tol_p:
                    blocking.append(
                        f"面板 {pid} 组 '{k_val}' 显著性值不一致：图注={er.get('plotted_p')} vs 源表 {p_col}={src_row.get(p_col)}（疑似误绘原始 P 值列）"
                    )

    ok = len(blocking) == 0
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "figure_id": manifest.get("figure_id") or os.path.basename(abs_manifest),
        "manifest_path": abs_manifest,
        "manifest_sha256": sha256_file(abs_manifest),
        "blocking": blocking,
        "observations": observations,
        "manual_checks": list(dict.fromkeys(manual_checks)),
        "artifacts": artifacts,
    }
