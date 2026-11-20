"""
Pure-Standard-Library Publication-Grade Scientific Figure QA & Backend Probe (Zero Pillow/PyMuPDF deps).
Checks:
1. Python (matplotlib/seaborn) & R (Rscript + ggplot2/patchwork/ComplexHeatmap) runtime availability.
2. Binary TIFF IFD parsing (XResolution/YResolution/ResolutionUnit + physical mm vs pixel linkage -> catches forged 600 DPI tags!).
3. Full PNG chunk parsing (IHDR + pHYs + IDAT + IEND + zlib decode + exact 100%-pixel blank-canvas check + physical mm/DPI linkage).
4. SVG XML 4-stage font calibration (CSS-to-user * ancestor CTM scale * root pt/user * target shrink ratio), outlined-text (`svg.fonttype='path'`) detection, viewBox clipping heuristic, dark-canvas & red-green/rainbow palette advisory, and raster-trap detection.
5. PDF header/page/MediaBox + Type 3 bitmap font check.
6. Hardened figure_manifest.json verification: strict root sandbox + ambiguity guard, fail-closed exports/duplicate-keys/exact-N/adjusted-P/threshold-crossing checks, canonical (project_rel_path, sha256) binding, and 3-state verdict (passed / not_verifiable / failed).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from typing import Any, Dict, List, Optional, Tuple


_UNIT_TO_PT = {
    "pt": 1.0,
    "px": 0.75,
    "mm": 72.0 / 25.4,
    "cm": 72.0 / 2.54,
    "in": 72.0,
    "pc": 12.0,
    "": 0.75,
}

_UNIT_TO_USER_96 = {
    "px": 1.0,
    "": 1.0,
    "pt": 96.0 / 72.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
    "pc": 16.0,
}

_ADJ_P_REJECT_RE = re.compile(r"(?:^|[_\-\s])(unadj(?:usted)?|raw|nominal|uncorrected)(?:$|[_\-\s])", re.I)
_ADJ_P_ACCEPT_RE = re.compile(
    r"(?:^|[_\-\s])(p[_\-]?adj|adj[_\-]?p|adjusted|padj|fdr|q[_\-]?val(?:ue)?|qvalue|holm|bonf(?:erroni)?|bh)(?:$|[_\-\s])",
    re.I,
)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_within_root(root: str, target: str) -> bool:
    r = os.path.normcase(os.path.realpath(root))
    t = os.path.normcase(os.path.realpath(target))
    try:
        return os.path.commonpath([r, t]) == r
    except ValueError:
        return False


def probe_plotting_backend(backend: str = "all") -> Dict[str, Any]:
    """Detect real Python (matplotlib/seaborn) and R (Rscript/ggplot2/patchwork/ComplexHeatmap) runtimes."""
    out: Dict[str, Any] = {}
    def _hidden_kw() -> Dict[str, Any]:
        if os.name != "nt":
            return {}
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), "startupinfo": si}

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
                r = subprocess.run([py_exe, "-c", code], capture_output=True, text=True, timeout=10, **_hidden_kw())
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
                r = subprocess.run([r_exe, "-e", r_expr], capture_output=True, text=True, timeout=12, **_hidden_kw())
                if r.returncode == 0 and "=" in r.stdout:
                    pkgs: Dict[str, Optional[str]] = {}
                    for pair in r.stdout.strip().split(";"):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            pkgs[k.strip()] = None if v.strip() == "NONE" else v.strip()
                    r_info["packages"] = pkgs
                    r_info["available"] = bool(pkgs.get("ggplot2"))
                else:
                    msg = (r.stderr or r.stdout).strip()[:200]
                    r_info["error"] = msg or f"Rscript exited with code {r.returncode}"
            except Exception as exc:
                r_info["error"] = str(exc)
        else:
            r_info["error"] = "Rscript not found in PATH"
        out["r"] = r_info

    return out


def _unfilter_row0(ftype: int, raw_row: bytes, bpp: int) -> bytes:
    """Unfilter PNG scanline 0 (where prior scanline is all zeros)."""
    if ftype == 0 or ftype == 2:
        return raw_row
    out = bytearray(raw_row)
    n = len(out)
    if ftype == 1:  # Sub
        for i in range(bpp, n):
            out[i] = (out[i] + out[i - bpp]) & 0xFF
    elif ftype == 3:  # Average (up = 0)
        for i in range(bpp, n):
            out[i] = (out[i] + (out[i - bpp] >> 1)) & 0xFF
    elif ftype == 4:  # Paeth (up = 0, up_left = 0 -> Paeth(a, 0, 0) = a)
        for i in range(bpp, n):
            out[i] = (out[i] + out[i - bpp]) & 0xFF
    return bytes(out)


def _check_png_exact_blank(
    raw_scanlines: bytes,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
) -> Tuple[Optional[bool], str]:
    """
    Exact 100%-pixel blank / uniform-color check on full decompressed PNG scanlines (zero sampling blind spots!).
    Uses C-speed bytes equality on row 1..H-1 (verified mathematically for all 5 PNG filters),
    so even 1024x1024 with a single dark pixel at row 701 returns False in <3ms!
    """
    channels_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    ch = channels_map.get(color_type)
    if ch is None or bit_depth not in (8, 16):
        return None, "skipped_format"
    bpp = ch * (bit_depth // 8)
    row_bytes = width * bpp
    stride = 1 + row_bytes
    if len(raw_scanlines) < stride * height:
        return None, "truncated_scanlines"

    # Decode row 0 completely and verify all pixels in row 0 are identical
    f0 = raw_scanlines[0]
    if f0 > 4:
        return None, "invalid_filter"
    row0 = _unfilter_row0(f0, raw_scanlines[1 : 1 + row_bytes], bpp)
    c0 = row0[:bpp]
    if row0 != c0 * width:
        # Row 0 already has >=2 distinct pixel values -> definitely not blank!
        # (Exception: RGBA/GA where every pixel has alpha=0 with varying RGB; check alpha channel if present)
        if color_type in (4, 6) and bit_depth == 8:
            alpha_idx = bpp - 1
            if any(row0[i] != 0 for i in range(alpha_idx, row_bytes, bpp)):
                return False, "full_decoded"
        else:
            return False, "full_decoded"

    # Fast exact patterns for rows 1..H-1 when EVERY pixel equals c0 (left=c0, up=c0, up_left=c0):
    # Filter 0 (None):    c0 * width
    # Filter 1 (Sub):     c0 + 0x00 * ((width - 1) * bpp)
    # Filter 2 (Up):      0x00 * (width * bpp)
    # Filter 3 (Average): ((c - (c >> 1)) & 0xFF for c in c0) + 0x00 * ((width - 1) * bpp)
    # Filter 4 (Paeth):   0x00 * (width * bpp)  [since Paeth(0, c0, 0) = c0 and Paeth(c0, c0, c0) = c0]
    zeros_full = b"\x00" * row_bytes
    zeros_tail = b"\x00" * ((width - 1) * bpp)
    pat_none = c0 * width
    pat_sub = c0 + zeros_tail
    pat_avg = bytes((b - (b >> 1)) & 0xFF for b in c0) + zeros_tail

    mv = memoryview(raw_scanlines)
    for y in range(1, height):
        off = y * stride
        fy = raw_scanlines[off]
        seg = mv[off + 1 : off + stride]
        if fy == 0:
            if seg != pat_none:
                return False, "full_decoded"
        elif fy == 1:
            if seg != pat_sub:
                return False, "full_decoded"
        elif fy == 2 or fy == 4:
            if seg != zeros_full:
                return False, "full_decoded"
        elif fy == 3:
            if seg != pat_avg:
                return False, "full_decoded"
        else:
            return None, "invalid_filter"
    return True, "full_decoded"


def inspect_png(path: str) -> Dict[str, Any]:
    """Parse all PNG chunks (IHDR, pHYs, IDAT, IEND), validate zlib decode, and detect 100%-uniform blank PNGs."""
    size = os.path.getsize(path)
    if size < 33:
        return {"ok": False, "format": "png", "error": "PNG file too small or truncated", "size": size}
    with open(path, "rb") as f:
        data = f.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return {"ok": False, "format": "png", "error": "Invalid PNG signature", "size": size}

    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    has_ihdr = False
    has_idat = False
    has_iend = False
    dpi_x: Optional[float] = None
    dpi_y: Optional[float] = None
    idat_parts: List[bytes] = []
    idat_total = 0
    max_idat_collect = 24 * 1024 * 1024  # 24 MiB compressed safety cap
    chunk_error = ""

    while pos + 12 <= len(data):
        chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + chunk_len
        crc_end = data_end + 4
        if crc_end > len(data):
            chunk_error = f"Truncated PNG chunk {chunk_type!r}"
            break
        if chunk_type == b"IHDR":
            if chunk_len != 13:
                chunk_error = "Invalid IHDR chunk length"
                break
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", data[data_start:data_end]
            )
            has_ihdr = True
        elif chunk_type == b"pHYs" and chunk_len == 9:
            ppu_x, ppu_y, unit = struct.unpack(">IIB", data[data_start:data_end])
            if unit == 1:  # pixels per meter
                dpi_x = round(ppu_x * 0.0254, 1)
                dpi_y = round(ppu_y * 0.0254, 1)
        elif chunk_type == b"IDAT":
            has_idat = True
            if idat_total < max_idat_collect:
                take = min(chunk_len, max_idat_collect - idat_total)
                idat_parts.append(data[data_start : data_start + take])
                idat_total += take
        elif chunk_type == b"IEND":
            has_iend = True
            break
        pos = crc_end

    blocking: List[str] = []
    warnings: List[str] = []
    if chunk_error:
        blocking.append(chunk_error)
    if not has_ihdr or width <= 0 or height <= 0:
        blocking.append("PNG 缺少合法 IHDR 图像头")
    if not has_idat:
        blocking.append("PNG 缺少 IDAT 像素数据块（文件残缺）")
    if not has_iend:
        blocking.append("PNG 缺少 IEND 结束块（文件被截断）")

    is_blank: Optional[bool] = None
    blank_check = "not_run"
    if not blocking and has_idat:
        pixels = width * height
        max_pixels_decode = 25_000_000
        if interlace != 0:
            # Still verify zlib header/stream is decodable
            try:
                dec = zlib.decompressobj()
                dec.decompress(b"".join(idat_parts), 65536)
                blank_check = "skipped_interlaced"
            except zlib.error as exc:
                blocking.append(f"PNG IDAT zlib 解压失败（图像损坏）: {exc}")
        elif pixels > max_pixels_decode or idat_total >= max_idat_collect:
            try:
                dec = zlib.decompressobj()
                dec.decompress(b"".join(idat_parts), 65536)
                blank_check = "skipped_large"
            except zlib.error as exc:
                blocking.append(f"PNG IDAT zlib 解压失败（图像损坏）: {exc}")
        else:
            try:
                raw_scanlines = zlib.decompress(b"".join(idat_parts))
                is_blank, blank_check = _check_png_exact_blank(raw_scanlines, width, height, bit_depth, color_type)
                if is_blank is True:
                    blocking.append(
                        "检测到 100% 纯单色空白 PNG 图像（全图零像素变化，常见于 savefig 前误调 plt.show() 或空画布导出）"
                    )
            except zlib.error as exc:
                blocking.append(f"PNG IDAT zlib 解压失败（图像损坏）: {exc}")

    phys_w_mm = round((width / dpi_x) * 25.4, 1) if (width and dpi_x) else None
    phys_h_mm = round((height / dpi_y) * 25.4, 1) if (height and dpi_y) else None

    return {
        "ok": len(blocking) == 0,
        "format": "png",
        "width_px": width,
        "height_px": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "interlace": interlace,
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "physical_width_mm": phys_w_mm,
        "physical_height_mm": phys_h_mm,
        "is_blank": is_blank,
        "blank_check": blank_check,
        "blocking": blocking,
        "warnings": warnings,
        "error": "; ".join(blocking) if blocking else "",
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
                if typ == 3:
                    return float(struct.unpack(endian + "H", raw4[:2])[0])
                if typ == 4:
                    return float(struct.unpack(endian + "I", raw4)[0])
                if typ == 5:
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
        unit = int(_val_from_buf(296) or 2)

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


def _parse_len_pt(raw: str) -> Optional[float]:
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z%]*)\s*$", raw or "", re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "%":
        return None
    factor = _UNIT_TO_PT.get(unit)
    return (val * factor) if factor is not None else None


def _parse_font_size_user(raw: str) -> Optional[float]:
    """Convert a CSS font-size string into SVG user units (px=1, pt=4/3, mm=96/25.4)."""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z%]*)\s*$", raw or "", re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if val <= 0 or unit in ("%", "em", "rem"):
        return None
    factor = _UNIT_TO_USER_96.get(unit)
    return (val * factor) if factor is not None else None


def _extract_elem_font_size_user(elem: ET.Element, css_class_fs: Dict[str, float]) -> Optional[float]:
    fs_raw = (elem.attrib.get("font-size") or "").strip()
    style = elem.attrib.get("style") or ""
    if not fs_raw and style:
        m_fs = re.search(r"(?:^|;)\s*font-size\s*:\s*([^;]+)", style, re.I)
        if m_fs:
            fs_raw = m_fs.group(1).strip()
        else:
            m_sh = re.search(r"(?:^|;)\s*font\s*:\s*[^;]*?\b([0-9]+(?:\.[0-9]+)?(?:pt|px|mm|cm|in))\b", style, re.I)
            if m_sh:
                fs_raw = m_sh.group(1).strip()
    if fs_raw:
        parsed = _parse_font_size_user(fs_raw)
        if parsed is not None:
            return parsed
    cls_str = (elem.attrib.get("class") or "").strip()
    if cls_str and css_class_fs:
        for c in cls_str.split():
            if c in css_class_fs:
                return css_class_fs[c]
    return None


def _mul_ctm(m1: Tuple[float, float, float, float, float, float], m2: Tuple[float, float, float, float, float, float]) -> Tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _parse_svg_transform(raw: str) -> Tuple[float, float, float, float, float, float]:
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if not raw:
        return ctm
    for fn, args_s in re.findall(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)", raw, re.I):
        nums = [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", args_s)]
        fn_l = fn.lower()
        if fn_l == "translate" and len(nums) >= 1:
            tx = nums[0]
            ty = nums[1] if len(nums) >= 2 else 0.0
            ctm = _mul_ctm(ctm, (1.0, 0.0, 0.0, 1.0, tx, ty))
        elif fn_l == "scale" and len(nums) >= 1:
            sx = nums[0]
            sy = nums[1] if len(nums) >= 2 else sx
            ctm = _mul_ctm(ctm, (sx, 0.0, 0.0, sy, 0.0, 0.0))
        elif fn_l == "rotate" and len(nums) >= 1:
            rad = math.radians(nums[0])
            cs, sn = math.cos(rad), math.sin(rad)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                ctm = _mul_ctm(ctm, (1.0, 0.0, 0.0, 1.0, cx, cy))
                ctm = _mul_ctm(ctm, (cs, sn, -sn, cs, 0.0, 0.0))
                ctm = _mul_ctm(ctm, (1.0, 0.0, 0.0, 1.0, -cx, -cy))
            else:
                ctm = _mul_ctm(ctm, (cs, sn, -sn, cs, 0.0, 0.0))
        elif fn_l == "matrix" and len(nums) == 6:
            ctm = _mul_ctm(ctm, tuple(nums))  # type: ignore[arg-type]
    return ctm


def _parse_hex_rgb(val: str) -> Optional[Tuple[int, int, int]]:
    s = (val or "").strip().lower()
    if s.startswith("#"):
        hx = s[1:]
        if len(hx) == 3 and all(c in "0123456789abcdef" for c in hx):
            return (int(hx[0] * 2, 16), int(hx[1] * 2, 16), int(hx[2] * 2, 16))
        if len(hx) == 6 and all(c in "0123456789abcdef" for c in hx):
            return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def inspect_svg(
    path: str,
    allow_raster_panels: bool = False,
    target_width_mm: Optional[float] = None,
    min_font_floor_pt: float = 6.0,
) -> Dict[str, Any]:
    """
    Parse SVG XML with 4-stage font calibration (CSS-to-user * CTM scale * root pt_per_user * shrink ratio),
    detect vector vs raster-only trap, outlined text (`svg.fonttype='path'`), viewBox clipping, dark background,
    and red-green / rainbow palette advisories.
    """
    size = os.path.getsize(path)
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as exc:
        return {"ok": False, "format": "svg", "error": f"Invalid SVG XML: {exc}", "size": size}

    viewbox = (root.attrib.get("viewBox") or root.attrib.get("viewbox") or "").strip()
    width_attr = (root.attrib.get("width") or "").strip()
    height_attr = (root.attrib.get("height") or "").strip()

    vb_nums: List[float] = []
    if viewbox:
        try:
            vb_nums = [float(x) for x in re.split(r"[\s,]+", viewbox) if x]
        except ValueError:
            vb_nums = []
    vb_ok = len(vb_nums) == 4 and vb_nums[2] > 0 and vb_nums[3] > 0

    w_pt = _parse_len_pt(width_attr)
    h_pt = _parse_len_pt(height_attr)
    if vb_ok and w_pt is not None and w_pt > 0:
        pt_per_user = w_pt / vb_nums[2]
        unit_assumed = False
    else:
        pt_per_user = 0.75
        unit_assumed = True

    if w_pt is not None and w_pt > 0:
        canvas_w_mm: Optional[float] = round(w_pt * 25.4 / 72.0, 1)
    elif vb_ok:
        canvas_w_mm = round(vb_nums[2] * 0.75 * 25.4 / 72.0, 1)
    else:
        canvas_w_mm = None

    if h_pt is not None and h_pt > 0:
        canvas_h_mm: Optional[float] = round(h_pt * 25.4 / 72.0, 1)
    elif vb_ok:
        canvas_h_mm = round(vb_nums[3] * 0.75 * 25.4 / 72.0, 1)
    else:
        canvas_h_mm = None

    # Parse simple <style> class font-size rules
    css_class_fs: Dict[str, float] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1].lower() == "style" and elem.text:
            for cls_name, fs_s in re.findall(r"\.([A-Za-z0-9_\-]+)\s*\{[^}]*font-size\s*:\s*([^;}\n]+)", elem.text, re.I):
                val_u = _parse_font_size_user(fs_s)
                if val_u is not None:
                    css_class_fs[cls_name] = val_u

    vector_count = 0
    image_count = 0
    text_count = 0
    has_glyph_defs = False
    min_font_pt: Optional[float] = None
    clipped_text_count = 0
    dark_background = False
    seen_sat_red = False
    seen_sat_green = False
    seen_jet_cyan_yellow = 0

    def _walk(
        elem: ET.Element,
        ctm: Tuple[float, float, float, float, float, float],
        inh_fs_user: Optional[float],
        in_defs: bool,
    ) -> None:
        nonlocal vector_count, image_count, text_count, has_glyph_defs
        nonlocal min_font_pt, clipped_text_count, dark_background
        nonlocal seen_sat_red, seen_sat_green, seen_jet_cyan_yellow

        tag = elem.tag.split("}")[-1].lower()
        eid = (elem.attrib.get("id") or "").lower()
        if tag in ("defs", "clippath", "mask"):
            if tag == "defs":
                in_defs = True
        if in_defs and ("dejavu" in eid or "arial" in eid or "helvetica" in eid or "glyph" in eid or "font" in eid):
            has_glyph_defs = True

        local_ctm = _mul_ctm(ctm, _parse_svg_transform(elem.attrib.get("transform") or ""))
        cur_fs_user = _extract_elem_font_size_user(elem, css_class_fs)
        eff_fs_user = cur_fs_user if cur_fs_user is not None else inh_fs_user

        if not in_defs:
            # Color hygiene inspection (advisory only)
            style_s = elem.attrib.get("style") or ""
            color_vals = [elem.attrib.get("fill") or "", elem.attrib.get("stroke") or ""]
            for prop in ("fill", "stroke"):
                m_c = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", style_s, re.I)
                if m_c:
                    color_vals.append(m_c.group(1).strip())
            for cv in color_vals:
                rgb = _parse_hex_rgb(cv)
                if rgb:
                    r_c, g_c, b_c = rgb
                    if r_c > 185 and g_c < 75 and b_c < 75:
                        seen_sat_red = True
                    if g_c > 145 and r_c < 85 and b_c < 85:
                        seen_sat_green = True
                    if (r_c < 30 and g_c > 210 and b_c > 210) or (r_c > 220 and g_c > 210 and b_c < 35):
                        seen_jet_cyan_yellow += 1

            if tag in ("path", "rect", "circle", "line", "polyline", "polygon", "ellipse"):
                vector_count += 1
                # Detect dark full-canvas background rect
                if tag == "rect" and vb_ok:
                    rw = _to_float(elem.attrib.get("width", "").replace("px", "").replace("pt", ""))
                    rh = _to_float(elem.attrib.get("height", "").replace("px", "").replace("pt", ""))
                    if rw and rh and rw >= vb_nums[2] * 0.85 and rh >= vb_nums[3] * 0.85:
                        fill_raw = elem.attrib.get("fill") or ""
                        if not fill_raw:
                            mf = re.search(r"(?:^|;)\s*fill\s*:\s*([^;]+)", style_s, re.I)
                            fill_raw = mf.group(1).strip() if mf else ""
                        rgb_bg = _parse_hex_rgb(fill_raw)
                        if rgb_bg:
                            lum = (0.2126 * rgb_bg[0] + 0.7152 * rgb_bg[1] + 0.0722 * rgb_bg[2]) / 255.0
                            if lum < 0.25:
                                dark_background = True
            elif tag == "image":
                image_count += 1
            elif tag in ("text", "tspan"):
                txt_content = "".join(elem.itertext()).strip()
                if tag == "text" or txt_content:
                    text_count += 1
                    if eff_fs_user is not None:
                        a, b, c, d, _e, _f = local_ctm
                        det = abs(a * d - b * c)
                        ctm_scale = math.sqrt(det) if det > 0 else 1.0
                        pt_val = eff_fs_user * ctm_scale * pt_per_user
                        if pt_val > 0 and (min_font_pt is None or pt_val < min_font_pt):
                            min_font_pt = round(pt_val, 2)
                    # ViewBox text-anchor clipping heuristic
                    if tag == "text" and vb_ok:
                        tx = _to_float((elem.attrib.get("x") or "").split()[0] if elem.attrib.get("x") else None)
                        ty = _to_float((elem.attrib.get("y") or "").split()[0] if elem.attrib.get("y") else None)
                        if tx is not None or ty is not None:
                            ux = tx if tx is not None else 0.0
                            uy = ty if ty is not None else 0.0
                            a, b, c, d, e, f = local_ctm
                            gx = a * ux + c * uy + e
                            gy = b * ux + d * uy + f
                            vx, vy, vw, vh = vb_nums
                            pad_x, pad_y = vw * 0.02, vh * 0.02
                            if gx < vx - pad_x or gx > vx + vw + pad_x or gy < vy - pad_y or gy > vy + vh + pad_y:
                                clipped_text_count += 1

        for child in elem:
            _walk(child, local_ctm, eff_fs_user, in_defs)

    _walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), None, False)

    # Stage 4: effective printed font size when canvas will be shrunk to target_width_mm
    effective_min_font_pt = min_font_pt
    shrink_ratio = 1.0
    if (
        min_font_pt is not None
        and target_width_mm is not None
        and target_width_mm > 0
        and canvas_w_mm is not None
        and canvas_w_mm > target_width_mm * 1.05
    ):
        shrink_ratio = target_width_mm / canvas_w_mm
        effective_min_font_pt = round(min_font_pt * shrink_ratio, 2)

    if min_font_pt is not None:
        font_status = "measured"
    elif text_count == 0 and (has_glyph_defs or vector_count >= 20):
        font_status = "no_text_nodes"
    elif text_count == 0:
        font_status = "no_text"
    else:
        font_status = "unmeasured_complex"

    warnings: List[str] = []
    blocking: List[str] = []
    palette_warnings: List[str] = []

    if not viewbox and not (width_attr and height_attr):
        blocking.append("SVG 缺少 viewBox 且无 width/height 尺寸声明")
    if image_count > 0 and vector_count < 8 and not allow_raster_panels:
        blocking.append(f"SVG 疑似整图栅格伪矢量（image={image_count}, vector_primitives={vector_count}），未声明为影像/热图面板")

    if font_status == "no_text_nodes":
        warnings.append(
            "SVG 未检出可编辑 <text> 节点（疑似文字已转曲，Matplotlib 请设置 rcParams['svg.fonttype']='none'，R 请使用 svglite）"
        )
    elif effective_min_font_pt is not None and effective_min_font_pt < min_font_floor_pt:
        shrink_note = f"（原 {min_font_pt}pt 按目标宽 {target_width_mm}mm 缩印后）" if shrink_ratio < 0.99 else ""
        warnings.append(
            f"SVG 终稿有效最小字号偏小 ({effective_min_font_pt}pt < 建议 {min_font_floor_pt}pt){shrink_note}，印刷可能难以辨认"
        )
    if clipped_text_count > 0:
        warnings.append(f"SVG 检测到 {clipped_text_count} 处文本锚点落出 viewBox 边界外（建议导出时使用 bbox_inches='tight' 或检查裁切）")
    if dark_background:
        warnings.append("SVG 检测到深色画布底板，投稿论文通常要求纯白底板 (#FFFFFF)")
    if seen_sat_red and seen_sat_green:
        msg = "SVG 同时使用高饱和纯红与纯绿配色，对红绿色盲读者不友好（建议改用 Okabe-Ito / viridis 色板）"
        palette_warnings.append(msg)
        warnings.append(msg)
    elif seen_jet_cyan_yellow >= 4 and (seen_sat_red or seen_sat_green):
        msg = "SVG 疑似使用彩虹/jet 高饱和渐变色阶，建议改用感知均匀色板（viridis / cividis / magma）"
        palette_warnings.append(msg)
        warnings.append(msg)

    return {
        "ok": len(blocking) == 0,
        "format": "svg",
        "viewBox": viewbox,
        "width": width_attr,
        "height": height_attr,
        "canvas_width_mm": canvas_w_mm,
        "canvas_height_mm": canvas_h_mm,
        "pt_per_user": round(pt_per_user, 4),
        "unit_assumed": unit_assumed,
        "vector_primitives": vector_count,
        "embedded_images": image_count,
        "text_nodes": text_count,
        "min_font_pt": min_font_pt,
        "effective_min_font_pt": effective_min_font_pt,
        "font_status": font_status,
        "clipped_text_count": clipped_text_count,
        "dark_background": dark_background,
        "palette_warnings": palette_warnings,
        "blocking": blocking,
        "warnings": warnings,
        "size": size,
    }


def inspect_pdf(path: str) -> Dict[str, Any]:
    """Pure-stdlib PDF header, MediaBox, page-count, and Type 3 bitmap font inspection."""
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
    has_type3 = bool(re.search(rb"/Subtype\s*/Type3\b", body))
    warnings: List[str] = []
    if has_type3:
        warnings.append("PDF 检测到 Type 3 点阵字体，投稿请设置 matplotlib rcParams['pdf.fonttype']=42 或 R cairo_pdf")
    return {
        "ok": True,
        "format": "pdf",
        "page_count_est": max(1, pages),
        "width_mm": width_mm,
        "height_mm": height_mm,
        "has_type3_font": has_type3,
        "warnings": warnings,
        "size": size,
        "manual_check_required": ["PDF 内嵌字体可编辑性与 CMYK/印刷色彩需人工抽检或矢量工具复核"],
    }


def inspect_any_figure(
    path: str,
    allow_raster_panels: bool = False,
    target_width_mm: Optional[float] = None,
    min_font_floor_pt: float = 6.0,
) -> Dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return inspect_png(path)
    if ext in (".tif", ".tiff"):
        return inspect_tiff(path)
    if ext == ".svg":
        return inspect_svg(
            path,
            allow_raster_panels=allow_raster_panels,
            target_width_mm=target_width_mm,
            min_font_floor_pt=min_font_floor_pt,
        )
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
        if not s or s.lower() in ("na", "nan", "none", "null", "inf", "-inf", ""):
            return None
        v = float(s)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _parse_exact_nonneg_int(raw_val: Any) -> Optional[int]:
    """Strict non-negative integer parser via Decimal (rejects 24.0000000001 and booleans)."""
    from decimal import Decimal, InvalidOperation
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if raw_val >= 0 else None
    s = str(raw_val).strip().replace(",", "")
    if not s or s.lower() in ("na", "nan", "none", "null", "inf", "-inf"):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0:
        return None
    if d != d.to_integral_value():
        return None
    return int(d)


def _is_exact_nonneg_int(val: Any) -> bool:
    return _parse_exact_nonneg_int(val) is not None


def _is_adjusted_p_col(col_name: str) -> bool:
    if not col_name:
        return False
    if _ADJ_P_REJECT_RE.search(col_name):
        return False
    return bool(_ADJ_P_ACCEPT_RE.search(col_name))


def _normalize_exports(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    items: List[Dict[str, Any]] = []
    errs: List[str] = []
    if raw is None:
        return [], ["figure_manifest.json 未声明任何导出图件 (exports)"]
    if isinstance(raw, dict):
        raw_list: List[Any] = []
        for k, v in raw.items():
            if isinstance(v, dict):
                raw_list.append({"format": str(k), **v})
            elif isinstance(v, str):
                raw_list.append({"format": str(k), "path": v})
            else:
                errs.append(f"exports['{k}'] 类型非法或为空 ({type(v).__name__})")
    elif isinstance(raw, list):
        raw_list = raw
    else:
        return [], [f"exports 字段类型非法 ({type(raw).__name__})，应为对象或列表"]

    for i, it in enumerate(raw_list):
        if isinstance(it, str):
            it = {"path": it}
        if not isinstance(it, dict):
            errs.append(f"exports[{i}] 类型非法 ({type(it).__name__})")
            continue
        p = it.get("path")
        if not isinstance(p, str) or not p.strip():
            errs.append(f"exports[{i}] 缺少非空 path 字段")
            continue
        items.append(it)
    return items, errs


def verify_figure_manifest(manifest_path: str, work_dir: str) -> Dict[str, Any]:
    """
    Validate a figure_manifest.json with strict root sandboxing, decoupled PNG/TIFF DPI checks,
    4-stage SVG font calibration, exact blank-PNG detection, fail-closed duplicate-key & exact-N &
    significance-threshold-crossing statistical checks, and canonical (project_rel_path, sha256) binding.
    """
    blocking: List[str] = []
    observations: List[str] = []
    manual_checks: List[str] = []
    artifacts: List[Dict[str, Any]] = []
    contract_missing: List[str] = []

    abs_manifest = os.path.normpath(os.path.abspath(manifest_path))
    if not os.path.isfile(abs_manifest):
        return {
            "ok": False,
            "status": "failed",
            "verification_level": "failed",
            "manifest_path": abs_manifest,
            "checked_artifacts": 0,
            "checked_panels": 0,
            "contract_missing": [],
            "blocking": [f"图件契约文件不存在: {abs_manifest}"],
            "observations": [],
            "manual_checks": [],
            "artifacts": [],
        }

    try:
        with open(abs_manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict):
            raise ValueError("JSON root must be an object")
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "verification_level": "failed",
            "manifest_path": abs_manifest,
            "checked_artifacts": 0,
            "checked_panels": 0,
            "contract_missing": [],
            "blocking": [f"figure_manifest.json 解析失败: {exc}"],
            "observations": [],
            "manual_checks": [],
            "artifacts": [],
        }

    base_dir = os.path.dirname(abs_manifest)
    root_real = os.path.realpath(work_dir)
    if not _is_within_root(root_real, abs_manifest):
        return {
            "ok": False,
            "status": "failed",
            "verification_level": "failed",
            "manifest_path": abs_manifest,
            "checked_artifacts": 0,
            "checked_panels": 0,
            "contract_missing": [],
            "blocking": [f"图件契约路径越出项目根目录: {abs_manifest}"],
            "observations": [],
            "manual_checks": [],
            "artifacts": [],
        }

    def _resolve_rel(p: str) -> Tuple[Optional[str], str, Optional[str]]:
        """Resolve path strictly inside work_dir, preferring manifest_dir first and flagging dual-target ambiguity."""
        if os.path.isabs(p):
            cand = os.path.realpath(p)
            if not _is_within_root(root_real, cand):
                return None, "escaped", f"路径越出项目根目录: {p}"
            return cand, "abs", None
        c1 = os.path.realpath(os.path.join(base_dir, p))
        c2 = os.path.realpath(os.path.join(root_real, p))
        c1_ok = _is_within_root(root_real, c1) and os.path.exists(c1)
        c2_ok = _is_within_root(root_real, c2) and os.path.exists(c2)
        if c1_ok and c2_ok and os.path.normcase(c1) != os.path.normcase(c2):
            return None, "ambiguous", (
                f"相对路径 '{p}' 同时命中两个不同文件（{os.path.relpath(c1, root_real)} 与 "
                f"{os.path.relpath(c2, root_real)}），请改用明确路径消除串图风险"
            )
        if c1_ok:
            return c1, "manifest_dir", None
        if c2_ok:
            return c2, "work_dir", None
        if not _is_within_root(root_real, c1) and not _is_within_root(root_real, c2):
            return None, "escaped", f"路径越出项目根目录: {p}"
        return c1, "manifest_dir", None

    # 1. Script & Source Data Hash checks
    for key_path, key_hash, label in (
        ("script_path", "script_sha256", "绘图脚本"),
        ("source_data_path", "source_data_sha256", "源数据表"),
    ):
        rp = manifest.get(key_path)
        exp_h = str(manifest.get(key_hash) or "").strip().lower()
        if rp:
            if not isinstance(rp, str):
                blocking.append(f"{label}路径类型非法")
                continue
            ap, _base, r_err = _resolve_rel(rp)
            if r_err:
                blocking.append(f"{label}: {r_err}")
            elif not ap or not os.path.isfile(ap):
                blocking.append(f"{label}缺失: {rp}")
            elif exp_h:
                act_h = sha256_file(ap).lower()
                if act_h != exp_h:
                    blocking.append(f"{label} SHA-256 漂移（脚本/数据在导出后被改动）: {rp}")

    # 2. Target journal dimensions & decoupled per-format DPI expectations
    target_w_mm = _to_float(manifest.get("target_width_mm")) or 89.0
    target_h_mm = _to_float(manifest.get("target_height_mm"))
    top_min_dpi = _to_float(manifest.get("min_dpi"))
    tiff_min_dpi = _to_float(manifest.get("tiff_min_dpi")) or top_min_dpi or 600.0
    min_font_floor = _to_float(manifest.get("min_font_pt")) or 6.0
    strict_font_floor = bool(manifest.get("strict_font_floor", False))
    allow_raster = bool(manifest.get("allow_raster_panels", False))
    figure_type = str(manifest.get("figure_type") or "quantitative").strip().lower()

    raw_exports = manifest.get("exports") if "exports" in manifest else manifest.get("outputs")
    exp_items, exp_errs = _normalize_exports(raw_exports)
    blocking.extend(exp_errs)
    if not exp_items and not exp_errs:
        blocking.append("figure_manifest.json 未声明任何有效导出图件 (exports)")

    # Determine if only PNG is exported (for decoupled PNG DPI threshold)
    exp_exts = {os.path.splitext(str(it.get("path") or ""))[1].lower() for it in exp_items}
    only_png_exported = exp_exts == {".png"}
    png_min_dpi = (
        _to_float(manifest.get("png_min_dpi"))
        or (top_min_dpi if (only_png_exported and top_min_dpi) else 300.0)
    )

    for item in exp_items:
        rp = str(item.get("path") or "").strip()
        ap, resolved_base, r_err = _resolve_rel(rp)
        if r_err:
            blocking.append(f"导出图件路径校验失败 ({rp}): {r_err}")
            continue
        if not ap or not os.path.isfile(ap):
            blocking.append(f"声明的导出图件不存在: {rp}")
            continue
        act_h = sha256_file(ap).lower()
        exp_h = str(item.get("sha256") or "").strip().lower()
        if exp_h and act_h != exp_h:
            blocking.append(f"导出图件 SHA-256 校验失败: {rp}")

        meta = inspect_any_figure(
            ap,
            allow_raster_panels=allow_raster,
            target_width_mm=target_w_mm,
            min_font_floor_pt=min_font_floor,
        )
        proj_rel = os.path.relpath(ap, root_real).replace("\\", "/")
        meta["path"] = ap
        meta["rel_path"] = rp
        meta["project_rel_path"] = proj_rel
        meta["resolved_base"] = resolved_base
        meta["sha256"] = act_h
        artifacts.append(meta)

        if not meta.get("ok"):
            blocking.append(f"图件结构校验失败 ({rp}): {meta.get('error') or '; '.join(meta.get('blocking') or [])}")

        fmt = meta.get("format")
        item_min_dpi = _to_float(item.get("min_dpi"))
        if fmt == "tiff":
            req_dpi = item_min_dpi or tiff_min_dpi
            dx, dy = meta.get("dpi_x"), meta.get("dpi_y")
            w_px, h_px = meta.get("width_px") or 0, meta.get("height_px") or 0
            if dx is None or dy is None:
                blocking.append(f"TIFF 缺少双轴 DPI 元数据 (XResolution/YResolution): {rp}")
            else:
                if dx < req_dpi - 1.0 or dy < req_dpi - 1.0:
                    blocking.append(f"TIFF 分辨率不足 ({dx}x{dy} DPI < 目标 {req_dpi} DPI): {rp}")
                min_req_w_px = int((target_w_mm / 25.4) * req_dpi * 0.92)
                if w_px < min_req_w_px:
                    blocking.append(
                        f"TIFF 疑似伪造高 DPI 标签：标称 {dx} DPI 但实际像素宽仅 {w_px}px（目标宽 {target_w_mm}mm @ {req_dpi} DPI 至少需 {min_req_w_px}px）: {rp}"
                    )
                if target_h_mm:
                    min_req_h_px = int((target_h_mm / 25.4) * req_dpi * 0.92)
                    if h_px < min_req_h_px:
                        blocking.append(
                            f"TIFF 像素高不足：{h_px}px < 目标高 {target_h_mm}mm @ {req_dpi} DPI 所需 {min_req_h_px}px: {rp}"
                        )
        elif fmt == "png":
            req_dpi = item_min_dpi or png_min_dpi
            dx, dy = meta.get("dpi_x"), meta.get("dpi_y")
            w_px = meta.get("width_px") or 0
            min_req_w_px = int((target_w_mm / 25.4) * req_dpi * 0.90)
            if w_px > 0 and w_px < min_req_w_px:
                blocking.append(
                    f"PNG 像素宽不足：{w_px}px < 目标宽 {target_w_mm}mm @ {req_dpi} DPI 至少所需 {min_req_w_px}px: {rp}"
                )
            if dx is not None and dy is not None:
                if dx < req_dpi - 1.0 or dy < req_dpi - 1.0:
                    observations.append(f"PNG pHYs 元数据 DPI 偏低 ({dx}x{dy} DPI < 目标 {req_dpi} DPI): {rp}")
            else:
                observations.append(f"PNG 未写入 pHYs DPI 元数据（已按像素宽 {w_px}px 核对 {target_w_mm}mm @ {req_dpi} DPI）: {rp}")
        elif fmt == "svg":
            for b in meta.get("blocking") or []:
                if f"{rp}: {b}" not in blocking:
                    blocking.append(f"{rp}: {b}")
            for w in meta.get("warnings") or []:
                if strict_font_floor and ("最小字号偏小" in w or "未检出可编辑" in w):
                    blocking.append(f"{rp}: {w}")
                else:
                    observations.append(f"{rp}: {w}")
        elif fmt == "pdf":
            for w in meta.get("warnings") or []:
                observations.append(f"{rp}: {w}")
            manual_checks.extend(meta.get("manual_check_required") or [])

    if len(artifacts) == 0 and not any("导出图件" in b or "exports" in b for b in blocking):
        blocking.append("实际核验的导出图件数为 0，无法签核通过")

    # 3. Panel-Level Source-Data Consistency Checks (Fail-Closed on Duplicate Keys, Exact N, Adjusted-P & Threshold Crossing!)
    raw_panel_sources = manifest.get("panel_sources")
    if raw_panel_sources is None:
        panel_sources: List[Any] = []
    elif isinstance(raw_panel_sources, list):
        panel_sources = raw_panel_sources
    else:
        blocking.append(f"panel_sources 类型非法 ({type(raw_panel_sources).__name__})，应为列表")
        panel_sources = []

    table_cache: Dict[str, Tuple[List[str], List[Dict[str, str]]]] = {}
    checked_panels = 0
    for idx_ps, ps in enumerate(panel_sources):
        if not isinstance(ps, dict):
            blocking.append(f"panel_sources[{idx_ps}] 类型非法 ({type(ps).__name__})")
            continue
        pid = str(ps.get("panel_id") or f"panel_{idx_ps + 1}")
        sp = ps.get("source_table") or manifest.get("source_data_path") or ""
        if not sp or not isinstance(sp, str):
            observations.append(f"面板 {pid} 未绑定 source_table，跳过源表数值核验")
            continue
        ap, _base, r_err = _resolve_rel(sp)
        if r_err:
            blocking.append(f"面板 {pid} 源数据表路径错误: {r_err}")
            continue
        if not ap or not os.path.isfile(ap):
            blocking.append(f"面板 {pid} 绑定的源数据表不存在: {sp}")
            continue
        if ap not in table_cache:
            table_cache[ap] = _load_full_delimited_table(ap)
        headers, rows = table_cache[ap]

        key_cols_raw = ps.get("key_cols")
        key_col = ps.get("key_col")
        if isinstance(key_cols_raw, list) and key_cols_raw:
            key_cols = [str(c).strip() for c in key_cols_raw if str(c).strip()]
        elif key_col:
            key_cols = [str(key_col).strip()]
        else:
            key_cols = []

        raw_expected_rows = ps.get("expected_rows")
        if raw_expected_rows is None:
            expected_rows: List[Any] = []
        elif isinstance(raw_expected_rows, list):
            expected_rows = raw_expected_rows
        else:
            blocking.append(f"面板 {pid} expected_rows 类型非法 ({type(raw_expected_rows).__name__})")
            continue

        val_col = ps.get("value_col")
        p_col = ps.get("p_col")
        require_adjusted_p = bool(ps.get("require_adjusted_p", False))
        alpha_thresholds = ps.get("alpha_thresholds") or [0.05, 0.01, 0.001]

        if p_col:
            if p_col not in headers:
                blocking.append(f"面板 {pid} 声明的 P 值列 '{p_col}' 不存在于源表 {sp}（可用列共 {len(headers)} 列）")
            elif require_adjusted_p and not _is_adjusted_p_col(str(p_col)):
                blocking.append(
                    f"面板 {pid} 要求多重校正 P 值，但绑定列 '{p_col}' 疑似原始/未校正 P 值列"
                )

        missing_kcols = [kc for kc in key_cols if kc not in headers]
        if missing_kcols:
            blocking.append(f"面板 {pid} 的分组/键列 {missing_kcols} 不存在于源表 {sp}")
            continue

        # Fail-closed duplicate-key detection (prevents CSV row-order flipping PASS/FAIL!)
        row_index: Dict[Tuple[str, ...], Dict[str, str]] = {}
        dup_keys: Dict[Tuple[str, ...], List[int]] = {}
        if key_cols:
            for r_i, r in enumerate(rows, start=2):  # 1-based CSV row with header=1
                kt = tuple(r.get(kc, "") for kc in key_cols)
                if kt in row_index:
                    dup_keys.setdefault(kt, []).append(r_i)
                else:
                    row_index[kt] = r
            if dup_keys:
                sample_dups = ", ".join(
                    f"{'/'.join(k)}(重复行 {v[:3]})" for k, v in list(dup_keys.items())[:3]
                )
                blocking.append(
                    f"面板 {pid} 源表 {sp} 键列 {key_cols} 存在重复键 [{sample_dups}]，无法唯一映射（请使用 key_cols 复合键或去重）"
                )
                continue

        panel_assertions = 0
        for er_i, er in enumerate(expected_rows):
            if not isinstance(er, dict):
                blocking.append(f"面板 {pid} expected_rows[{er_i}] 类型非法 ({type(er).__name__})")
                continue
            if len(key_cols) > 1:
                raw_keys = er.get("keys") if "keys" in er else er.get("key")
                if isinstance(raw_keys, dict):
                    kt = tuple(str(raw_keys.get(kc) or "") for kc in key_cols)
                elif isinstance(raw_keys, list) and len(raw_keys) == len(key_cols):
                    kt = tuple(str(x or "") for x in raw_keys)
                else:
                    blocking.append(f"面板 {pid} expected_rows[{er_i}] 缺少与复合键 {key_cols} 对应的 keys")
                    continue
                k_disp = "/".join(kt)
            elif len(key_cols) == 1:
                k_val = str(er.get("key") or "")
                kt = (k_val,)
                k_disp = k_val
            else:
                kt = ()
                k_disp = f"row_{er_i}"

            if key_cols and kt not in row_index:
                blocking.append(f"面板 {pid} 标注的组别/键 '{k_disp}' 在源表 {sp} 中不存在（疑似标签拼错或伪造）")
                continue
            src_row = row_index.get(kt, {})

            if val_col and "plotted_value" in er:
                if val_col not in headers:
                    blocking.append(f"面板 {pid} 效应列 '{val_col}' 不存在于源表 {sp}")
                    continue
                panel_assertions += 1
                src_v = _to_float(src_row.get(val_col))
                plt_v = _to_float(er.get("plotted_value"))
                tol = _to_float(er.get("tol")) or 1e-3
                if src_v is None or plt_v is None:
                    blocking.append(f"面板 {pid} 组 '{k_disp}' 数值缺失（src={src_row.get(val_col)}, plotted={er.get('plotted_value')}）")
                elif abs(src_v - plt_v) > max(tol, abs(src_v) * 1e-3):
                    blocking.append(
                        f"面板 {pid} 组 '{k_disp}' 绘图值 ({plt_v}) 与源表 {val_col} ({src_v}) 不一致（疑似标签互换或数值漂移）"
                    )

            n_col = ps.get("n_col")
            if n_col and "plotted_n" in er:
                if n_col not in headers:
                    blocking.append(f"面板 {pid} 样本量列 '{n_col}' 不存在于源表 {sp}")
                else:
                    panel_assertions += 1
                    src_n_int = _parse_exact_nonneg_int(src_row.get(n_col))
                    plt_n_int = _parse_exact_nonneg_int(er.get("plotted_n"))
                    if src_n_int is None or plt_n_int is None or src_n_int != plt_n_int:
                        blocking.append(
                            f"面板 {pid} 组 '{k_disp}' 样本量 N 不一致或非精确非负整数：图注={er.get('plotted_n')} vs 源表 {n_col}={src_row.get(n_col)}"
                        )

            if p_col and p_col in headers and "plotted_p" in er:
                panel_assertions += 1
                src_p = _to_float(src_row.get(p_col))
                plt_p = _to_float(er.get("plotted_p"))
                explicit_tol = _to_float(er.get("p_tol"))
                tol_p = explicit_tol if explicit_tol is not None else 1e-4
                if src_p is None or plt_p is None or src_p < 0 or plt_p < 0 or src_p > 1 or plt_p > 1:
                    blocking.append(
                        f"面板 {pid} 组 '{k_disp}' 显著性值非法或缺失：图注={er.get('plotted_p')} vs 源表 {p_col}={src_row.get(p_col)}"
                    )
                else:
                    crossed = [
                        thr for thr in alpha_thresholds
                        if isinstance(thr, (int, float)) and ((src_p < thr) != (plt_p < thr))
                    ]
                    p_mismatch = False
                    if crossed:
                        p_mismatch = True
                    elif abs(src_p - plt_p) > tol_p:
                        p_mismatch = True
                    elif explicit_tol is None and min(src_p, plt_p) < 1e-3:
                        if (src_p == 0.0) != (plt_p == 0.0):
                            p_mismatch = True
                        elif src_p > 0 and plt_p > 0 and abs(math.log10(src_p) - math.log10(plt_p)) > 0.15:
                            p_mismatch = True
                    if p_mismatch:
                        cross_msg = f"且跨越显著性阈值 {crossed[0]}（疑似误绘原始 P 值列或数值漂移）" if crossed else "（疑似误绘原始 P 值列或数值漂移）"
                        blocking.append(
                            f"面板 {pid} 组 '{k_disp}' 显著性值不一致：图注={er.get('plotted_p')} vs 源表 {p_col}={src_row.get(p_col)} {cross_msg}"
                        )

        if panel_assertions > 0:
            checked_panels += 1
        else:
            observations.append(f"面板 {pid} 实际执行数值/统计断言数为 0（expected_rows 为空或未声明有效比对列），不计入已核验面板")

    # 4. Honest 3-State Verdict (passed / not_verifiable / failed)
    if figure_type != "schematic":
        if not manifest.get("script_path"):
            contract_missing.append("script_path")
        if not manifest.get("source_data_path") and checked_panels == 0:
            contract_missing.append("source_data_path")
        if checked_panels == 0:
            contract_missing.append("panel_sources")

    has_blocking = len(blocking) > 0 or len(artifacts) == 0
    if has_blocking:
        status = "failed"
        ok = False
        ver_level = "failed"
    elif figure_type != "schematic" and checked_panels == 0:
        status = "not_verifiable"
        ok = True  # Structural export checks succeeded, but quantitative panel-level numeric verification is absent
        has_prov = bool(manifest.get("script_path") or manifest.get("source_data_path"))
        ver_level = "provenance_only" if has_prov else "structure_only"
        observations.append("仅完成图件格式结构检查；定量图未完成面板级源表数值断言核验，无法签发完整数据一致性 PASS")
    else:
        status = "passed"
        ok = True
        ver_level = "full_manifest" if checked_panels > 0 else "schematic_verified"

    return {
        "ok": ok,
        "status": status,
        "verification_level": ver_level,
        "figure_id": manifest.get("figure_id") or os.path.basename(abs_manifest),
        "manifest_path": abs_manifest,
        "manifest_rel_path": os.path.relpath(abs_manifest, root_real).replace("\\", "/"),
        "manifest_sha256": sha256_file(abs_manifest),
        "checked_artifacts": len(artifacts),
        "checked_panels": checked_panels,
        "contract_missing": contract_missing,
        "blocking": blocking,
        "observations": observations,
        "manual_checks": list(dict.fromkeys(manual_checks)),
        "artifacts": artifacts,
    }
