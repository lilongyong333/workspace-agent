"""生成一组多模态测试样本。

设计意图：每个文件测一个**具体的失败模式**，而不是"随便造几个 PDF"。
含一个对照组（有文本层的 PDF），用来证明视觉解析**不该**被触发 ——
只测正例不算测过，那样分不清"能力有效"还是"什么都往视觉上送"。
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
W, H = 1240, 1754                      # A4 @150dpi


def font(sz: int, bold: bool = False):
    names = (["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"] if bold
             else ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simsun.ttc"])
    for n in names + ["C:/Windows/Fonts/arial.ttf"]:
        try:
            return ImageFont.truetype(n, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), "white")
    return img, ImageDraw.Draw(img)


# ── 1. 扫描件：中文采购合同 ──────────────────────────────────
def sample_contract() -> Image.Image:
    img, d = page()
    d.text((95, 100), "深圳市通达致远科技有限公司", font=font(40, True), fill="black")
    d.text((95, 165), "设备采购合同", font=font(56, True), fill="black")
    d.line([(95, 250), (1145, 250)], fill="black", width=3)
    rows = [
        ("合同编号", "PRJ-2026-0817"),
        ("供应商", "Meridian 科技有限公司"),
        ("采购内容", "GPU 服务器 8 台 / 机架式交换机 2 台"),
        ("合同金额", "人民币 1,187,432.00 元（含税）"),
        ("签订日期", "2026 年 1 月 14 日"),
        ("到期日期", "2026 年 2 月 1 日"),
        ("付款方式", "预付 30%，验收后 60 日内付清余款"),
    ]
    y = 300
    for k, v in rows:
        d.text((110, y), k + "：", font=font(32, True), fill="black")
        d.text((330, y), v, font=font(32), fill="black")
        y += 72
    d.text((95, y + 40), "续约条款", font=font(36, True), fill="black")
    d.text((110, y + 105),
           "合同到期前 60 天，双方须以书面形式确认是否续约；", font=font(28), fill="black")
    d.text((110, y + 155),
           "逾期未确认的，本合同于到期日自动终止，不再顺延。", font=font(28), fill="black")
    d.text((110, y + 240), "违约责任：逾期交付按日万分之五计罚，上限为合同总额 5%。",
           font=font(28), fill="black")
    d.rectangle([820, y + 330, 1120, y + 470], outline="crimson", width=5)
    d.text((855, y + 380), "合同专用章", font=font(34, True), fill="crimson")
    return img


# ── 2. 扫描件：财务表格（考表格结构还原）────────────────────
def sample_table() -> Image.Image:
    img, d = page()
    d.text((95, 100), "2025 第四季度部门预算执行表", font=font(46, True), fill="black")
    d.text((95, 175), "单位：人民币元　　制表日期：2026-01-05", font=font(26), fill="black")
    cols = [95, 420, 680, 940, 1145]
    head = ["部门", "预算", "实际支出", "差异率"]
    rows = [
        ("研发部", "1,200,000", "1,187,432", "-1.0%"),
        ("市场部", "  450,000", "  480,900", "+6.9%"),
        ("运维部", "  320,000", "  298,140", "-6.8%"),
        ("行政部", "  180,000", "  176,500", "-1.9%"),
        ("合计",   "2,150,000", "2,142,972", "-0.3%"),
    ]
    y = 260
    d.rectangle([cols[0], y, cols[-1], y + 60], fill="#e8e8e8", outline="black", width=2)
    for i, h in enumerate(head):
        d.text((cols[i] + 18, y + 12), h, font=font(30, True), fill="black")
    y += 60
    for r in rows:
        bold = r[0] == "合计"
        d.rectangle([cols[0], y, cols[-1], y + 58], outline="black", width=2)
        for i, cell in enumerate(r):
            d.text((cols[i] + 18, y + 12), cell, font=font(28, bold), fill="black")
        y += 58
    for c in cols[1:-1]:
        d.line([(c, 260), (c, y)], fill="black", width=2)
    d.text((95, y + 60), "说明：市场部超支主因为 Q4 追加的展会预算 30,900 元，",
           font=font(26), fill="black")
    d.text((95, y + 105), "已于 2026-01-14 补批 10% 应急金覆盖。", font=font(26), fill="black")
    return img


# ── 3. 扫描件：英文发票（考非中文场景）──────────────────────
def sample_invoice() -> Image.Image:
    img, d = page()
    d.text((95, 100), "INVOICE", font=font(60, True), fill="black")
    d.text((95, 185), "Meridian Technologies Ltd.", font=font(32), fill="black")
    d.text((95, 235), "Invoice No: INV-2026-0042", font=font(28), fill="black")
    d.text((95, 280), "Issue Date: 2026-01-20    Due: 2026-02-19", font=font(28), fill="black")
    d.line([(95, 340), (1145, 340)], fill="black", width=3)
    items = [
        ("GPU Server R760xa", "8", "132,000.00", "1,056,000.00"),
        ("Rack Switch S5248F", "2", "48,716.00", "97,432.00"),
        ("Installation Service", "1", "34,000.00", "34,000.00"),
    ]
    y = 380
    for h, x in zip(["Description", "Qty", "Unit Price", "Amount"], [110, 620, 760, 990]):
        d.text((x, y), h, font=font(28, True), fill="black")
    y += 55
    for desc, qty, up, amt in items:
        d.text((110, y), desc, font=font(26), fill="black")
        d.text((640, y), qty, font=font(26), fill="black")
        d.text((760, y), up, font=font(26), fill="black")
        d.text((990, y), amt, font=font(26), fill="black")
        y += 50
    d.line([(95, y + 20), (1145, y + 20)], fill="black", width=2)
    d.text((760, y + 45), "TOTAL", font=font(30, True), fill="black")
    d.text((990, y + 45), "1,187,432.00", font=font(30, True), fill="black")
    d.text((95, y + 140), "Payment terms: Net 30. Late payment incurs 0.05%/day.",
           font=font(24), fill="black")
    return img


# ── 4. 扫描件：会议手写签批（更难的 OCR）────────────────────
def sample_handwritten() -> Image.Image:
    img, d = page()
    d.text((95, 100), "项目变更审批单", font=font(48, True), fill="black")
    d.line([(95, 175), (1145, 175)], fill="black", width=3)
    d.text((110, 220), "变更事项：Project Falcon 正式更名为 Project Phoenix",
           font=font(30), fill="black")
    d.text((110, 285), "生效日期：2026 年 1 月 22 日", font=font(30), fill="black")
    d.text((110, 350), "影响范围：所有对外文档、API 命名空间、内部看板",
           font=font(30), fill="black")
    d.text((110, 440), "审批意见：", font=font(32, True), fill="black")
    # 用手写风格字体模拟签批
    hand = font(34)
    d.text((140, 510), "同意更名。请运维同步更新 runbook，", font=hand, fill="#1a3a8a")
    d.text((140, 565), "双写窗口关闭前完成切换演练签字。", font=hand, fill="#1a3a8a")
    d.text((140, 620), "—— 张工  2026.01.22", font=hand, fill="#1a3a8a")
    d.rectangle([840, 700, 1130, 830], outline="crimson", width=5)
    d.text((870, 745), "审批通过", font=font(38, True), fill="crimson")
    return img


# ── 5. 对照组：有文本层的 PDF（视觉解析**不该**被触发）────────
def text_layer_pdf(path: Path) -> None:
    """手写最小 PDF。用 Helvetica 基础字体，无需嵌入字体文件。

    这是对照组：它有真正的文本层，索引时必须走文本抽取，
    **一次模型调用都不该发生**。只测正例不算测过 ——
    分不清"视觉能力有效"和"什么都往视觉上送"。
    """
    lines = [
        "QUARTERLY REVIEW - CONTROL SAMPLE",
        "",
        "This PDF has a real text layer.",
        "The indexer must extract it directly.",
        "No vision model call should happen for this file.",
        "",
        "Marker token: TEXTLAYER-CONTROL-9931",
        "Budget figure: 2,150,000 CNY",
    ]
    content = "BT /F1 16 Tf 60 760 Td 20 TL\n"
    for ln in lines:
        content += f"({ln}) Tj T*\n"
    content += "ET"
    stream = zlib.compress(content.encode("latin-1"))

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    path.write_bytes(bytes(out))


# ── 生成 ────────────────────────────────────────────────────
jobs = [
    ("01-扫描件-采购合同.pdf", sample_contract, "纯图像中文合同：编号/金额/日期/条款"),
    ("02-扫描件-预算表格.pdf", sample_table, "图像化表格：考表格结构还原"),
    ("03-扫描件-英文发票.pdf", sample_invoice, "英文场景 + 数字对账"),
    ("04-扫描件-手写签批.pdf", sample_handwritten, "手写体 + 印章，OCR 难度更高"),
]
for name, fn, desc in jobs:
    fn().save(OUT / name, "PDF", resolution=150)
    print(f"  {name:<28} {desc}")

# 多页扫描件：把前两张拼成一个两页 PDF
p1, p2 = sample_contract(), sample_table()
p1.save(OUT / "05-扫描件-两页合订.pdf", "PDF", resolution=150, save_all=True, append_images=[p2])
print(f"  {'05-扫描件-两页合订.pdf':<28} 多页：验证逐页转写与页码定位")

# 单独的图片文件
sample_table().save(OUT / "06-图片-预算表.png")
print(f"  {'06-图片-预算表.png':<28} 图片格式本身能否进索引")

text_layer_pdf(OUT / "07-对照组-有文本层.pdf")
print(f"  {'07-对照组-有文本层.pdf':<28} 对照组：**不该**触发视觉解析")

print(f"\n全部生成于: {OUT}")
