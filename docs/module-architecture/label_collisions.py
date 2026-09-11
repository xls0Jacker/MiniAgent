#!/usr/bin/env python3
"""自建图元碰撞检测器 —— archify 的 label-route-clearance 不检测「标签压标签」。

从交付后的 HTML 里直接读渲染期坐标（不是 JSON 模型），所以检出的重叠就是
浏览器里真实存在的重叠：

  - 节点矩形  <g id="node-X"> ... <rect x y w h>
  - 标签矩形  <g data-detail="context" data-edge-id="E"> ... <rect x y w h>
  - 边折线    <path data-edge-id="E" data-composition-points="x,y;x,y;...">

检测三类：
  A 标签 × 标签
  B 标签 × 节点
  C 标签 × 别的边的线段

用法: python3 label_collisions.py <artifact.html> [more.html ...]
退出码: 0 = 无碰撞, 1 = 有碰撞
"""
from __future__ import annotations

import re
import sys

SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)
NODE_RE = re.compile(r'<g id="node-([^"]+)"[^>]*>(.*?)</g>\s*(?=<g id="node-|<g data-detail=|</svg>)', re.S)
NODE_RECT_RE = re.compile(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"')
LABEL_RE = re.compile(
    r'<g data-detail="context" data-edge-from="[^"]*" data-edge-to="[^"]*" '
    r'data-edge-label="([^"]*)" data-edge-key="\d+" data-edge-id="([^"]+)">\s*'
    r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"'
)
EDGE_RE = re.compile(
    r'data-edge-id="([^"]+)"\s+data-composition-points="([^"]+)"'
)


def rects_overlap(a, b, tol=0.0):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = min(ax + aw, bx + bw) - max(ax, bx)
    iy = min(ay + ah, by + bh) - max(ay, by)
    if ix <= tol or iy <= tol:
        return 0.0
    return ix * iy


def seg_rect_hit(p, q, r):
    """线段 p-q 是否穿过矩形 r（返回穿过的长度，用于排序）。"""
    rx, ry, rw, rh = r
    # Liang-Barsky 裁剪
    x0, y0 = p
    x1, y1 = q
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, x0 - rx), (dx, rx + rw - x0), (-dy, y0 - ry), (dy, ry + rh - y0)):
        if pp == 0:
            if qq < 0:
                return 0.0
        else:
            t = qq / pp
            if pp < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return 0.0
    return (t1 - t0) * ((dx * dx + dy * dy) ** 0.5)


def parse(path):
    html = open(path, encoding="utf-8").read()
    m = SVG_RE.search(html)
    if not m:
        return None
    svg = m.group(0)

    nodes = {}
    for nid, body in NODE_RE.findall(svg):
        best = None
        for x, y, w, h in NODE_RECT_RE.findall(body):
            x, y, w, h = float(x), float(y), float(w), float(h)
            if w >= 60 and h >= 40:  # 排除图标 / sigil
                best = (x, y, w, h)
                break
        if best:
            nodes[nid] = best

    labels = []
    for text, eid, x, y, w, h in LABEL_RE.findall(svg):
        labels.append((text, eid, (float(x), float(y), float(w), float(h))))

    edges = {}
    for eid, pts in EDGE_RE.findall(svg):
        seq = []
        for pair in pts.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            xs, ys = pair.split(",")
            seq.append((float(xs), float(ys)))
        edges[eid] = seq

    return nodes, labels, edges


def check(path):
    parsed = parse(path)
    if parsed is None:
        # visual-check 回执等边车文件里没有图，跳过
        return 0
    nodes, labels, edges = parsed
    problems = []

    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            area = rects_overlap(labels[i][2], labels[j][2])
            if area > 0:
                problems.append(
                    f"A 标签×标签: 「{labels[i][0]}」({labels[i][1]}) ↔ "
                    f"「{labels[j][0]}」({labels[j][1]}) 重叠 {area:.0f}px²"
                )

    for text, eid, r in labels:
        for nid, nr in nodes.items():
            area = rects_overlap(r, nr)
            if area > 0:
                problems.append(
                    f"B 标签×节点: 「{text}」({eid}) 压住节点 {nid} 重叠 {area:.0f}px²"
                )

    for text, eid, r in labels:
        for other, seq in edges.items():
            if other == eid:
                continue
            for k in range(len(seq) - 1):
                if seg_rect_hit(seq[k], seq[k + 1], r) > 0:
                    problems.append(
                        f"C 标签×他边: 「{text}」({eid}) 被边 {other} "
                        f"的第 {k} 段穿过"
                    )
                    break

    print(f"── {path}")
    print(f"   节点 {len(nodes)} · 标签 {len(labels)} · 边 {len(edges)}")
    if problems:
        for p in problems:
            print("   ✗ " + p)
    else:
        print("   ✓ 0 处碰撞（标签×标签 / 标签×节点 / 标签×他边）")
    return len(problems)


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    total = sum(check(p) for p in argv)
    print()
    print(f"合计碰撞 {total} 处")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
