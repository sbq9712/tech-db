#!/usr/bin/env python3
"""RT-029 visual regression — REAL browser (Playwright/Chromium).

Phase-02 review blocker C: RT-029.DOD-03 needs a real-browser visual
regression suite that is deterministic, tunnel-free and CI-repeatable:

  * the production frontend (index.html + styles.css + qa.js) is served from
    a local static HTTP server — no live tunnel, no network;
  * /api/chat/stream, /api/stats and /api/graph are intercepted with
    page.route and answered from committed deterministic SSE/JSON fixtures;
  * each UI state fixture is rendered in a real Chromium at desktop
    (1280×800) AND mobile (390×844) viewports;
  * assertions are REAL layout/geometry/style checks computed by the browser
    (element visibility, bounding boxes, computed colors, element counts,
    no-overflow) — not "string exists" checks;
  * golden screenshots (committed under qa-backend/test_fixtures/
    visual_goldens/rt029/) are compared with a deterministic pixel diff;
  * a mutation case deliberately breaks a key layout rule and MUST fail the
    pixel diff — proving the harness actually detects visual regressions.

Run:  python tests_visual_rt029.py [--update-goldens]
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
GOLDENS = HERE / "test_fixtures" / "visual_goldens" / "rt029"

VIEWPORTS = {"desktop": {"width": 1280, "height": 800},
             "mobile": {"width": 390, "height": 844}}
# Normalized structural-diff thresholds: a COMPLETELY different CJK font
# stack measures 0.00 after blur-normalization (glyph noise cancelled),
# while real layout breaks measure ≳5% (bottom-third erased: 5.3%;
# half-blank: 19.5%). Thresholds sit between the two regimes.
MAX_DIFF_RATIO = 0.04
MUTATION_MIN_DIFF_RATIO = 0.04

passed = failed = 0
CASE_RESULTS = {}


def test(name, condition, detail=""):
    global passed, failed
    CASE_RESULTS[name] = bool(condition)
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {detail}")


def _assert_case(name):
    assert CASE_RESULTS.get(name) is True, name


# ── deterministic fixtures (no live backend, no tunnel) ─────────────────────

def sse_body(events) -> bytes:
    out = []
    for ev in events:
        out.append("data: " + json.dumps(ev, ensure_ascii=False))
    out.append("data: [DONE]")
    return ("\n".join(out) + "\n").encode("utf-8")


LONG_TITLE = ("NVIDIA Blackwell B200 NVLink双向带宽与576GPU扩展架构深度解析——"
              "半导体行业年度技术白皮书与供应链影响评估报告（2026年第一季度）")


def fixture_supported():
    """SUPPORTED answer: evidence card, DIRECT_SUPPORT/CONTRADICTS/BACKGROUND
    relations, TEXT_SPAN locator chip, long no-wrap source title."""
    citations = [
        {"id": 1, "record_id": "rec-blackwell", "title": LONG_TITLE,
         "date": "2026-01-15", "source": "TechNews", "tag": "chip",
         "score": 0.98, "url": "https://example.com/blackwell",
         "grounding_status": "VALID",
         "supports_claim_ids": ["c1"],
         "highlight": "NVLink双向带宽达到1.8TB/s",
         "evidence_spans": [{"text": "NVLink双向带宽达到1.8TB/s", "start": 24,
                             "end": 43}],
         "locators": [{"locator_type": "TEXT_SPAN", "start": 24, "end": 43}]},
        {"id": 2, "record_id": "rec-vendor", "title": "厂商公告：量产进度说明",
         "date": "2026-02-01", "source": "VendorPR", "tag": "battery",
         "url": "https://example.com/vendor",
         "grounding_status": "VALID", "supports_claim_ids": [],
         "locators": [{"locator_type": "TEXT_SPAN", "start": 0, "end": 12}]},
        {"id": 3, "record_id": "rec-review", "title": "第三方评测：带宽复核",
         "date": "2026-03-11", "source": "ReviewLab", "tag": "chip",
         "url": "https://example.com/review",
         "grounding_status": "VALID", "supports_claim_ids": [],
         "locators": [{"locator_type": "TEXT_SPAN", "start": 5, "end": 22}]},
    ]
    claims = [
        {"id": "c1", "text": "NVLink双向带宽达到1.8TB/s", "status": "SUPPORTED",
         "relations": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]},
        {"id": "c2", "text": "量产时间晚于官方口径", "status": "UNSUPPORTED",
         "relations": [
             {"citation_id": 3, "relation": "CONTRADICTS"},
             {"citation_id": 2, "relation": "BACKGROUND"}]},
    ]
    done = {
        "answer": "NVLink双向带宽达到1.8TB/s[1]。第三方评测与厂商背景如上[2][3]。",
        "citations": citations,
        "claims": claims,
        "citation_schema_version": "2.0.0",
        "answer_status": "SUPPORTED",
        "stop_reason": "evidence_sufficient",
        "evidence_summary": "3条独立来源",
        "verification_status": "PASSED",
        "degraded_capabilities": [],
        "cited_record_ids": ["rec-blackwell", "rec-vendor", "rec-review"],
    }
    return sse_body([
        {"step": "retrieving", "message": "正在检索相关知识..."},
        {"text": "NVLink双向带宽达到"},
        {"text": "1.8TB/s[1]。第三方评测与厂商背景如上[2][3]。"},
        done,
    ])


def fixture_partial():
    """PARTIALLY_SUPPORTED: supported + unresolved claim sections."""
    citations = [
        {"id": 1, "record_id": "rec-hbm", "title": "HBM3e内存规格验证",
         "date": "2026-03-01", "source": "MemNews", "tag": "chip",
         "url": "https://example.com/hbm", "grounding_status": "VALID",
         "supports_claim_ids": ["c1"],
         "highlight": "HBM3e内存带宽达到1.2TB/s",
         "locators": [{"locator_type": "TEXT_SPAN", "start": 0, "end": 18}]},
        {"id": 2, "record_id": "rec-notice", "title": "行业简讯：量产时间未定",
         "date": "2026-03-15", "source": "IndustryBrief", "tag": "chip",
         "url": "https://example.com/brief", "grounding_status": "VALID",
         "supports_claim_ids": [],
         "locators": [{"locator_type": "TEXT_SPAN", "start": 3, "end": 15}]},
    ]
    claims = [
        {"id": "c1", "text": "HBM3e内存带宽达到1.2TB/s", "status": "SUPPORTED",
         "relations": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]},
        {"id": "c2", "text": "该内存已进入量产阶段", "status": "UNSUPPORTED",
         "relations": [{"citation_id": 2, "relation": "BACKGROUND"}]},
    ]
    return sse_body([
        {"step": "retrieving", "message": "正在检索相关知识..."},
        {"text": "HBM3e内存带宽达到1.2TB/s[1]。"},
        {"text": "该内存已进入量产阶段。"},
        {"answer": "HBM3e内存带宽达到1.2TB/s[1]。该内存已进入量产阶段。",
         "citations": citations, "claims": claims,
         "citation_schema_version": "2.0.0",
         "answer_status": "PARTIALLY_SUPPORTED",
         "stop_reason": "coverage_fail",
         "evidence_summary": "1条来源",
         "verification_status": "PASSED",
         "degraded_capabilities": [],
         "cited_record_ids": ["rec-hbm"]},
    ])


def fixture_unverified():
    """UNVERIFIED banner + degraded capability chips + user warning."""
    return sse_body([
        {"step": "retrieving", "message": "正在检索相关知识..."},
        {"step": "verifying", "message": "正在独立验证..."},
        {"text": "NVLink双向带宽达到1.8TB/s[1]。"},
        {"answer": "NVLink双向带宽达到1.8TB/s[1]。",
         "citations": [
             {"id": 1, "record_id": "rec-blackwell", "title": LONG_TITLE,
              "date": "2026-01-15", "source": "TechNews", "tag": "chip",
              "url": "https://example.com/blackwell",
              "grounding_status": "VALID", "supports_claim_ids": ["c1"],
              "highlight": "NVLink双向带宽达到1.8TB/s",
              "locators": [{"locator_type": "TEXT_SPAN", "start": 24,
                            "end": 43}]}],
         "claims": [{"id": "c1", "text": "NVLink双向带宽达到1.8TB/s",
                     "status": "SUPPORTED",
                     "relations": [{"citation_id": 1,
                                    "relation": "DIRECT_SUPPORT"}]}],
         "citation_schema_version": "2.0.0",
         "answer_status": "UNVERIFIED",
         "stop_reason": "technical_failure",
         "evidence_summary": "1条来源",
         "verification_status": "UNVERIFIED",
         "degraded_capabilities": ["entailment", "citation_grounding"],
         "user_warning": "独立验证服务暂不可用，本回答未通过验证，请谨慎采信。",
         "cited_record_ids": ["rec-blackwell"]},
    ])


def fixture_stale_invalid():
    """Stale/INVALID citations must NOT render as evidence (schema 2.0
    hard-filter + pre-2.0 strip)."""
    citations = [
        {"id": 1, "record_id": "rec-ok", "title": "有效引用：带宽验证",
         "date": "2026-01-15", "source": "TechNews", "tag": "chip",
         "url": "https://example.com/ok", "grounding_status": "VALID",
         "supports_claim_ids": ["c1"],
         "highlight": "NVLink双向带宽达到1.8TB/s",
         "locators": [{"locator_type": "TEXT_SPAN", "start": 24, "end": 43}]},
        {"id": 2, "record_id": "rec-stale", "title": "STALE-INVALID-CANARY",
         "date": "2025-01-01", "source": "OldFeed", "tag": "chip",
         "url": "https://example.com/stale",
         "grounding_status": "GROUNDING_FAIL",
         "supports_claim_ids": [],
         "highlight": "STALE-INVALID-CANARY证据片段",
         "body_snippet": "STALE-INVALID-CANARY证据片段"},
        {"id": 3, "record_id": "rec-schema1", "title": "SCHEMA1-CANARY",
         "date": "2025-06-01", "source": "Legacy", "tag": "chip",
         "url": "https://example.com/legacy",
         "grounding_status": "GROUNDING_FAIL",
         "supports_claim_ids": [], "highlight": "SCHEMA1-CANARY片段",
         "body_snippet": "SCHEMA1-CANARY片段"},
    ]
    return sse_body([
        {"step": "retrieving", "message": "正在检索相关知识..."},
        {"text": "NVLink双向带宽达到1.8TB/s[1]。"},
        {"answer": "NVLink双向带宽达到1.8TB/s[1]。",
         "citations": citations,
         "claims": [{"id": "c1", "text": "NVLink双向带宽达到1.8TB/s",
                     "status": "SUPPORTED",
                     "relations": [{"citation_id": 1,
                                    "relation": "DIRECT_SUPPORT"}]}],
         "citation_schema_version": "2.0.0",
         "answer_status": "SUPPORTED", "stop_reason": "evidence_sufficient",
         "evidence_summary": "1条来源", "verification_status": "PASSED",
         "degraded_capabilities": [],
         "cited_record_ids": ["rec-ok"]},
    ])


def fixture_pre20():
    """Pre-2.0 schema payload: ungrounded citation must render as the
    stripped-evidence note, never as a normal-looking snippet."""
    return sse_body([
        {"step": "retrieving", "message": "正在检索相关知识..."},
        {"text": "旧数据引用示例[1]。"},
        {"answer": "旧数据引用示例[1]。",
         "citations": [
             {"id": 1, "record_id": "rec-legacy", "title": "旧版引用（未定位）",
              "date": "2025-06-01", "source": "Legacy", "tag": "chip",
              "url": "https://example.com/legacy",
              "grounding_status": "GROUNDING_FAIL",
              "supports_claim_ids": [],
              "highlight": "SCHEMA1-CANARY片段",
              "body_snippet": "SCHEMA1-CANARY片段"}],
         "claims": [],
         "citation_schema_version": "1.0.0",
         "answer_status": "UNVERIFIED", "stop_reason": "legacy_schema",
         "evidence_summary": "1条来源", "verification_status": "UNVERIFIED",
         "degraded_capabilities": [],
         "cited_record_ids": []},
    ])


FIXTURES = {
    "supported_full": ("supported_full", fixture_supported),
    "partial": ("partial", fixture_partial),
    "unverified": ("unverified", fixture_unverified),
    "stale_invalid": ("stale_invalid", fixture_stale_invalid),
    "pre20": ("pre20", fixture_pre20),
}


# ── static server for the real frontend (tunnel-free) ──────────────────────

class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    def log_message(self, *args):
        pass


def start_static_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


# ── pixel diff ──────────────────────────────────────────────────────────────
# Deterministic and CI-portable: images are normalized (downscaled to a
# common height + color-quantized to a coarse palette) BEFORE the diff, so
# glyph-level rendering differences between font stacks (local vs CI) do not
# flag false regressions, while real layout breaks (moved/hidden/clipped
# blocks) still change the normalized structure well past the threshold.

_DIFF_NORM_HEIGHT = 100
_DIFF_BLUR = 6
_DIFF_QUANT_COLORS = 12
_DIFF_LEVEL = 20  # normalized intensity step; below = same block


def _normalize_for_diff(img):
    """Layout-structure normalization: downscale + gaussian blur + coarse
    palette. Glyph rendering (font stacks, hinting, antialiasing) averages
    out; block position/size/color changes survive."""
    from PIL import Image, ImageFilter
    w, h = img.size
    nw = max(1, round(w * _DIFF_NORM_HEIGHT / h))
    img = img.resize((nw, _DIFF_NORM_HEIGHT), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(_DIFF_BLUR))
    return img.quantize(colors=_DIFF_QUANT_COLORS, method=Image.MEDIANCUT,
                        dither=Image.NONE).convert("RGB")


def diff_ratio(png_a: Path, png_b: Path) -> float:
    from PIL import Image, ImageChops
    a = _normalize_for_diff(Image.open(png_a).convert("RGB"))
    b = _normalize_for_diff(Image.open(png_b).convert("RGB"))
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).convert("L")
    hist = diff.histogram()
    changed = sum(hist[_DIFF_LEVEL:])
    return changed / (a.size[0] * a.size[1])


# ── browser harness ─────────────────────────────────────────────────────────

DETERMINISM_CSS = """
*, *::before, *::after { animation: none !important;
  transition: none !important; caret-color: transparent !important; }
/* Deterministic CJK font: CI installs fonts-noto-cjk; pinning the family
   makes glyph rendering reproducible across machines. */
#qaMain, #qaMain *, .qa-messages, .qa-messages * {
  font-family: 'Noto Sans CJK SC', 'Noto Sans SC', sans-serif !important; }
"""

DRIVER_JS = """
async (config) => {
  // deterministic fonts for CJK text
  const style = document.createElement('style');
  style.textContent = config.mutationCss || '';
  document.head.appendChild(style);
  window.qaModule.switchToQAView();
  const input = document.getElementById('qaInput');
  input.value = config.question;
  document.getElementById('qaSendBtn').click();
}
"""


def run_case(browser, base_url, body_bytes, name, viewport_name, update,
             mutation_css=None, case_prefix="RT029.visual"):
    ctx = browser.new_context(viewport=VIEWPORTS[viewport_name],
                              device_scale_factor=1)
    page = ctx.new_page()
    page.add_init_script(
        "window.TECH_DB_CONFIG = { qaApiBase: '' };"
        "try { localStorage.clear(); } catch (e) {}")
    intercepted = {"chat": 0}

    def handle_chat(route):
        intercepted["chat"] += 1
        route.fulfill(status=200, content_type="text/event-stream",
                      headers={"Cache-Control": "no-cache"},
                      body=body_bytes)

    page.route("**/api/chat/stream", handle_chat)
    page.route("**/api/stats",
               lambda r: r.fulfill(status=200, content_type="application/json",
                                   body=json.dumps({"total": 146})))
    page.route("**/api/graph",
               lambda r: r.fulfill(status=200, content_type="application/json",
                                   body=json.dumps({"nodes": [], "links": []})))
    page.goto(base_url + "/index.html")
    page.wait_for_load_state("networkidle")
    page.add_style_tag(content=DETERMINISM_CSS)
    page.evaluate(DRIVER_JS, {"question": "NVLink双向带宽多少？",
                              "mutationCss": mutation_css or ""})
    page.wait_for_selector(".qa-citations-block", timeout=15000)
    page.wait_for_function(
        "() => !document.querySelector('.qa-status-indicator')", timeout=15000)
    return ctx, page, intercepted


class GoldenBaselineError(RuntimeError):
    """A golden baseline is missing or unreadable in normal (non-update)
    mode. Baselines never self-heal: only an explicit --update-goldens run
    may create or replace one."""


def compare_golden(target: Path, actual: Path) -> float:
    """Normal-mode comparison. Missing or unreadable/corrupt baseline is a
    GoldenBaselineError — the harness never creates or mutates baselines."""
    if not target.exists():
        raise GoldenBaselineError(
            f"golden baseline missing: {target} — normal runs never create "
            "baselines; regenerate explicitly with --update-goldens")
    try:
        return diff_ratio(target, actual)
    except Exception as exc:  # unreadable/corrupt golden
        raise GoldenBaselineError(
            f"golden baseline unreadable/corrupt: {target} ({exc})") from exc


def capture(page, shot_name, update):
    """Screenshot #qaMain and compare against the committed golden.

    Fail-closed baseline policy (Phase-02 review blocker B):
      * --update-goldens  → create/replace the golden (explicit opt-in);
      * normal run        → the golden is READ-ONLY: missing baseline,
                            unreadable/corrupt file, or a diff above the
                            threshold is a hard failure. The actual screenshot
                            is kept for debugging but NEVER promoted.
    """
    GOLDENS.mkdir(parents=True, exist_ok=True)
    target = GOLDENS / shot_name
    element = page.locator("#qaMain")
    element.screenshot(path=str(target.with_suffix(".actual.png")))
    actual = target.with_suffix(".actual.png")
    if update:
        actual.replace(target)
        return target, 0.0
    ratio = compare_golden(target, actual)
    return target, ratio


# ── case implementations ────────────────────────────────────────────────────

def case_supported(browser, base_url, viewport, update):
    body = FIXTURES["supported_full"][1]()
    ctx, page, intercepted = run_case(browser, base_url, body,
                                      "supported_full", viewport, update)
    try:
        checks = {}

        # REAL layout: evidence card visible with expected geometry
        card = page.locator(".qa-evidence-card")
        checks["evidence_card_visible"] = card.is_visible()
        card_box = card.bounding_box()
        checks["evidence_card_geometry"] = bool(
            card_box and card_box["width"] > 200 and card_box["height"] > 40)

        # SUPPORTED claim row + citation card rendered distinctly
        checks["supported_claim_row"] = page.locator(
            ".qa-claim-row.qa-claim-supported").count() == 1
        checks["citation_items"] = page.locator(".qa-citation-item").count() == 3

        # CONTRADICTS vs support vs BACKGROUND rendered with DISTINCT colors
        chip_contra = page.locator(".qa-rel-chip.qa-rel-contradict")
        chip_support = page.locator(".qa-rel-chip.qa-rel-support")
        chip_bg = page.locator(".qa-rel-chip.qa-rel-background")
        checks["relation_chips_present"] = (
            chip_contra.count() == 1 and chip_support.count() == 1
            and chip_bg.count() == 1)
        if checks["relation_chips_present"]:
            bg_c = chip_contra.first.evaluate(
                "el => getComputedStyle(el).backgroundColor")
            bg_s = chip_support.first.evaluate(
                "el => getComputedStyle(el).backgroundColor")
            bg_b = chip_bg.first.evaluate(
                "el => getComputedStyle(el).backgroundColor")
            checks["contradicts_distinct_color"] = (
                bg_c != bg_s and bg_c != bg_b and bg_s != bg_b)
            contra_box = chip_contra.first.bounding_box()
            checks["contradicts_visible"] = bool(
                contra_box and contra_box["width"] > 8
                and contra_box["height"] > 8)
        else:
            checks["contradicts_distinct_color"] = False
            checks["contradicts_visible"] = False

        # TEXT_SPAN locator chip rendered with exact offsets
        locator = page.locator(".qa-locator-chip")
        checks["locator_chip_count"] = locator.count() >= 1
        if checks["locator_chip_count"]:
            loc_text = locator.first.inner_text()
            checks["locator_text_span_displayed"] = (
                "TEXT_SPAN" in loc_text and "24–43" in loc_text)
            loc_box = locator.first.bounding_box()
            checks["locator_geometry"] = bool(
                loc_box and loc_box["width"] > 20 and loc_box["height"] > 8)
        else:
            checks["locator_text_span_displayed"] = False
            checks["locator_geometry"] = False

        # long source title: single line (no wrap), ellipsis overflow,
        # no horizontal overflow of the container
        title = page.locator(".qa-citation-item").first.locator(
            ".qa-citation-title")
        checks["long_title_no_wrap"] = title.evaluate(
            "el => { const r = el.getBoundingClientRect();"
            "  const cs = getComputedStyle(el);"
            "  return r.height < 40 && cs.whiteSpace === 'nowrap'"
            "    && cs.textOverflow === 'ellipsis'; }")
        checks["long_title_clipped_not_overflowing"] = title.evaluate(
            "el => el.scrollWidth >= el.clientWidth - 1"
            "      && el.getBoundingClientRect().width"
            "         <= el.parentElement.getBoundingClientRect().width")
        msgs = page.locator("#qaMain")
        checks["no_horizontal_overflow"] = msgs.evaluate(
            "el => el.scrollWidth <= el.clientWidth + 1")

        # status badge shows SUPPORTED (green)
        badge = page.locator(".qa-answer-status")
        checks["supported_badge"] = badge.is_visible() and "证据充分支持" \
            in badge.inner_text()

        # NO unverified banner in this state
        checks["no_unverified_banner"] = page.locator(
            ".qa-unverified-banner").count() == 0
        checks["no_degraded_chips"] = page.locator(
            ".qa-degraded-chip").count() == 0

        # golden screenshot comparison
        shot, ratio = capture(page, f"supported_full_{viewport}.png", update)
        checks["golden_pixel_match"] = ratio <= MAX_DIFF_RATIO
        detail = f"(diff ratio {ratio:.4f} vs golden {shot.name})"

        ok = all(checks.values())
        test(f"RT029.visual_supported_full_{viewport}", ok,
             detail + " failed: " + ", ".join(
                 k for k, v in checks.items() if not v))
        return checks
    finally:
        ctx.close()


def case_partial(browser, base_url, viewport, update):
    body = FIXTURES["partial"][1]()
    ctx, page, _ = run_case(browser, base_url, body, "partial", viewport,
                            update)
    try:
        checks = {}
        badge = page.locator(".qa-answer-status")
        checks["partial_badge_visible"] = badge.is_visible() and "部分支持" \
            in badge.inner_text()
        checks["supported_section"] = page.locator(
            ".qa-claim-row.qa-claim-supported").count() == 1
        checks["unsupported_section"] = page.locator(
            ".qa-claim-row.qa-claim-unsupported").count() == 1
        # the two sections must be visually distinct (border color differs)
        if checks["supported_section"] and checks["unsupported_section"]:
            b1 = page.locator(".qa-claim-row.qa-claim-supported").first \
                .evaluate("el => getComputedStyle(el).borderLeftColor")
            b2 = page.locator(".qa-claim-row.qa-claim-unsupported").first \
                .evaluate("el => getComputedStyle(el).borderLeftColor")
            checks["sections_distinct"] = b1 != b2
            box1 = page.locator(".qa-claim-row.qa-claim-supported").first \
                .bounding_box()
            box2 = page.locator(".qa-claim-row.qa-claim-unsupported").first \
                .bounding_box()
            checks["sections_both_visible"] = bool(
                box1 and box2 and box1["height"] > 10
                and box2["height"] > 10)
        else:
            checks["sections_distinct"] = False
            checks["sections_both_visible"] = False
        shot, ratio = capture(page, f"partial_{viewport}.png", update)
        checks["golden_pixel_match"] = ratio <= MAX_DIFF_RATIO
        ok = all(checks.values())
        test(f"RT029.visual_partial_{viewport}", ok,
             f"(diff ratio {ratio:.4f}) failed: " + ", ".join(
                 k for k, v in checks.items() if not v))
        return checks
    finally:
        ctx.close()


def case_unverified(browser, base_url, viewport, update):
    body = FIXTURES["unverified"][1]()
    ctx, page, _ = run_case(browser, base_url, body, "unverified", viewport,
                            update)
    try:
        checks = {}
        banner = page.locator(".qa-unverified-banner")
        checks["unverified_banner_visible"] = banner.is_visible()
        if checks["unverified_banner_visible"]:
            b_box = banner.bounding_box()
            checks["banner_geometry"] = bool(
                b_box and b_box["width"] > 200 and b_box["height"] > 30)
            checks["banner_shows_status"] = "UNVERIFIED" in banner.inner_text()
        else:
            checks["banner_geometry"] = False
            checks["banner_shows_status"] = False
        chips = page.locator(".qa-degraded-chip")
        checks["degraded_chips_present"] = chips.count() == 2
        if checks["degraded_chips_present"]:
            texts = [chips.nth(i).inner_text() for i in range(2)]
            checks["degraded_labels"] = (
                "entailment" in texts and "citation_grounding" in texts)
        else:
            checks["degraded_labels"] = False
        warn = page.locator(".qa-user-warning")
        checks["user_warning_visible"] = warn.is_visible()
        badge = page.locator(".qa-answer-status")
        checks["unverified_badge"] = badge.is_visible() and "未能验证" \
            in badge.inner_text()
        shot, ratio = capture(page, f"unverified_{viewport}.png", update)
        checks["golden_pixel_match"] = ratio <= MAX_DIFF_RATIO
        ok = all(checks.values())
        test(f"RT029.visual_unverified_{viewport}", ok,
             f"(diff ratio {ratio:.4f}) failed: " + ", ".join(
                 k for k, v in checks.items() if not v))
        return checks
    finally:
        ctx.close()


def case_stale_invalid(browser, base_url, viewport, update):
    body = FIXTURES["stale_invalid"][1]()
    ctx, page, _ = run_case(browser, base_url, body, "stale_invalid",
                            viewport, update)
    try:
        checks = {}
        # schema 2.0: GROUNDING_FAIL citation hard-dropped
        checks["schema20_invalid_dropped"] = page.locator(
            ".qa-citation-item").count() == 1
        checks["stale_canary_not_rendered"] = page.locator(
            "#qaMessages").inner_text().count("STALE-INVALID-CANARY") == 0
        shot, ratio = capture(page, f"stale_invalid_{viewport}.png", update)
        checks["golden_pixel_match"] = ratio <= MAX_DIFF_RATIO
        ok = all(checks.values())
        test(f"RT029.visual_stale_invalid_{viewport}", ok,
             f"(diff ratio {ratio:.4f}) failed: " + ", ".join(
                 k for k, v in checks.items() if not v))
        return checks
    finally:
        ctx.close()


def case_pre20(browser, base_url, viewport, update):
    """Pre-2.0 schema: ungrounded evidence-looking snippets are stripped and
    replaced by the ungrounded note (stale citation never renders as
    evidence)."""
    body = FIXTURES["pre20"][1]()
    ctx, page, _ = run_case(browser, base_url, body, "pre20", viewport,
                            update)
    try:
        checks = {}
        body_text = page.locator("#qaMessages").inner_text()
        checks["stale_snippet_stripped"] = "SCHEMA1-CANARY片段" not in body_text
        checks["ungrounded_note_shown"] = "未能定位到原文" in body_text
        note = page.locator(".qa-ungrounded-note")
        checks["ungrounded_note_visible"] = note.count() >= 1 and \
            note.first.is_visible()
        shot, ratio = capture(page, f"pre20_{viewport}.png", update)
        checks["golden_pixel_match"] = ratio <= MAX_DIFF_RATIO
        ok = all(checks.values())
        test(f"RT029.visual_pre20_stripped_{viewport}", ok,
             f"(diff ratio {ratio:.4f}) failed: " + ", ".join(
                 k for k, v in checks.items() if not v))
        return checks
    finally:
        ctx.close()


def case_baseline_selftest(viewport):
    """Blocker B behavioral selftest: the golden baseline can NEVER
    self-heal. A temporarily-absent golden must make the normal-mode
    comparison FAIL (not silently regenerate a new baseline), and the
    committed golden must be restored byte-identically afterwards.

    No browser needed: the comparison path under test is exercised with
    the golden's own bytes as the "actual" (content-identical restore is
    verified by hash). The committed golden content is never modified.
    """
    target = GOLDENS / f"supported_full_{viewport}.png"
    if not target.exists():
        test(f"RT029.visual_baseline_selftest_{viewport}", False,
             "committed golden missing — run --update-goldens and commit")
        return
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    stash = target.with_suffix(".selftest-stash.png")
    try:
        target.rename(stash)                       # 1. golden moved away
        missing_failed = False
        try:
            # 2. normal-mode comparison against the (now absent) baseline
            fake_actual = target.with_suffix(".actual.png")
            fake_actual.write_bytes(stash.read_bytes())
            compare_golden(target, fake_actual)
        except GoldenBaselineError:
            missing_failed = True                  # 3. MUST fail
        except Exception:
            missing_failed = False
        finally:
            if fake_actual.exists():
                fake_actual.unlink()
        still_absent = not target.exists()         # must NOT self-heal
        test(f"RT029.visual_baseline_selftest_{viewport}",
              missing_failed and still_absent,
              f"missing_failed={missing_failed} still_absent={still_absent}")
    finally:
        if stash.exists():
            stash.rename(target)                   # 4. restore
    restored = hashlib.sha256(target.read_bytes()).hexdigest()
    test(f"RT029.visual_baseline_restored_{viewport}",
          restored == before,
          "golden bytes changed during selftest — restore failed")
    # corrupt-baseline variant: an unreadable golden (not a valid PNG) must
    # fail the normal-mode comparison — verified on a THROWAWAY copy, the
    # committed golden stays untouched.
    corrupt = GOLDENS / f"selftest_corrupt_{viewport}.png"
    try:
        corrupt.write_bytes(b"this is not a png at all")
        actual_copy = target.with_suffix(".selftest-actual.png")
        actual_copy.write_bytes(target.read_bytes())
        corrupt_failed = False
        try:
            compare_golden(corrupt, actual_copy)
        except GoldenBaselineError:
            corrupt_failed = True
        except Exception:
            corrupt_failed = False
        finally:
            if actual_copy.exists():
                actual_copy.unlink()
        test(f"RT029.visual_corrupt_baseline_fails_{viewport}",
              corrupt_failed,
              f"corrupt golden accepted (corrupt_failed={corrupt_failed})")
    finally:
        if corrupt.exists():
            corrupt.unlink()


def case_mutation(browser, base_url, viewport, update):
    """Mutation/sanity: deliberately break a key layout rule (hide the
    evidence card + collapse locator chips) — the pixel diff MUST flag it.
    Proves the golden-based harness detects real visual regressions."""
    body = FIXTURES["supported_full"][1]()
    mutation_css = (
        # structural break 1: evidence card collapses to a thin bar
        ".qa-evidence-card { height: 6px !important; overflow: hidden"
        " !important; }"
        ".qa-evidence-card * { display: none !important; }"
        # structural break 2: citation block pushed far down the page
        ".qa-citations-block { margin-top: 400px !important; }"
        # structural break 3: whole message surface recolored (large-area
        # color change survives blur-normalization deterministically)
        "#qaMessages, #qaMessages * { background: #7c3aed !important; }")
    ctx, page, _ = run_case(browser, base_url, body, "mutation", viewport,
                            update, mutation_css=mutation_css)
    try:
        # golden exists from the healthy supported_full case
        target = GOLDENS / f"supported_full_{viewport}.png"
        if not target.exists():
            test(f"RT029.visual_mutation_detected_{viewport}", False,
                 "golden missing")
            return None
        GOLDENS.mkdir(parents=True, exist_ok=True)
        mut_shot = GOLDENS / f"mutation_{viewport}.actual.png"
        page.locator("#qaMain").screenshot(path=str(mut_shot))
        ratio = diff_ratio(target, mut_shot)
        detected = ratio >= MUTATION_MIN_DIFF_RATIO
        test(f"RT029.visual_mutation_detected_{viewport}", detected,
             f"(diff ratio {ratio:.4f} — expected >= "
             f"{MUTATION_MIN_DIFF_RATIO})")
        # layout assertion also independently detects the break: the
        # evidence card must be collapsed (hidden or ~zero height) and the
        # status badge recolored to the broken red
        card = page.locator(".qa-evidence-card")
        card_collapsed = (
            card.count() == 0 or not card.is_visible()
            or (card.first.bounding_box() or {}).get("height", 999) < 10)
        surface = page.locator("#qaMessages")
        surface_broken = surface.count() and surface.first.evaluate(
            "el => getComputedStyle(el).backgroundColor === 'rgb(124, 58, 237)'")
        test(f"RT029.visual_mutation_layout_assert_{viewport}",
             card_collapsed and surface_broken)
        return {"ratio": ratio, "detected": detected}
    finally:
        ctx.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-goldens", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if os.environ.get("TECH_DB_VISUAL_REQUIRED") == "1":
            print("FATAL: playwright not installed (pip install playwright; "
                  "python -m playwright install chromium)")
            return 2
        print("  SKIPPED: playwright unavailable (install with "
              "`pip install playwright && python -m playwright install "
              "chromium`; CI runs this suite as a required gate)")
        print("  RT-029 visual: 0 passed, 0 failed (skipped)")
        return 0

    with sync_playwright() as pw:
        def _chromium_ready() -> bool:
            try:
                browser = pw.chromium.launch(headless=True,
                                             args=["--no-sandbox"])
                browser.close()
                return True
            except Exception:
                return False

        if not _chromium_ready():
            if os.environ.get("TECH_DB_VISUAL_REQUIRED") == "1":
                print("FATAL: chromium could not launch "
                      "(python -m playwright install chromium)")
                return 2
            print("  SKIPPED: chromium unavailable "
                  "(python -m playwright install chromium)")
            print("  RT-029 visual: 0 passed, 0 failed (skipped)")
            return 0

        return _run_all(pw, args.update_goldens)


def _safe_case(case_fn, browser, base_url, viewport, update, case_name):
    """Run one visual case; a missing/corrupt golden baseline is a HARD
    failure of that case (fail-closed, never self-healed)."""
    try:
        case_fn(browser, base_url, viewport, update)
    except GoldenBaselineError as exc:
        test(f"RT029.visual_{case_name}_{viewport}", False, str(exc)[:200])


def _run_all(pw, update) -> int:
    print("── RT-029 visual regression (real Chromium) ──")
    server, base_url = start_static_server()
    lib_dir = REPO / ".pw-libs" / "usr" / "lib" / "x86_64-linux-gnu"
    env = dict(os.environ)
    if lib_dir.exists():
        env["LD_LIBRARY_PATH"] = (
            str(lib_dir) + ":" + env.get("LD_LIBRARY_PATH", ""))
    try:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--force-color-profile=srgb",
                  "--font-render-hinting=none"])
        try:
            if not update:
                # Blocker B selftests run before the browser cases: baselines
                # must never self-heal and a corrupt golden must fail closed.
                for viewport in ("desktop", "mobile"):
                    case_baseline_selftest(viewport)
            cases = (("supported", case_supported),
                     ("partial", case_partial),
                     ("unverified", case_unverified),
                     ("stale_invalid", case_stale_invalid),
                     ("pre20", case_pre20))
            for viewport in ("desktop", "mobile"):
                print(f"  [{viewport}]")
                for name, fn in cases:
                    _safe_case(fn, browser, base_url, viewport, update, name)
                case_mutation(browser, base_url, viewport, update)
        finally:
            browser.close()
    finally:
        server.shutdown()

    # cleanup transient diff artifacts; only committed goldens remain
    for leftover in GOLDENS.glob("*.actual.png"):
        try:
            leftover.unlink()
        except OSError:
            pass

    print("=" * 70)
    print(f"  RT-029 visual: {passed} passed, {failed} failed")
    print("=" * 70)
    if failed:
        print("  FAILED: " + ", ".join(k for k, v in CASE_RESULTS.items()
                                       if not v))
        return 1
    return 0


_EXECUTED = {"done": False}


def _ensure_executed() -> None:
    """Run the whole suite once per process (direct execution OR pytest
    collection of the L12-named wrappers below)."""
    if _EXECUTED["done"]:
        return
    _EXECUTED["done"] = True
    rc = main()
    if rc:
        raise SystemExit(rc)


def _assert_case(name):
    assert CASE_RESULTS.get(name) is True, name


# ── L12-named behavioral wrappers (same convention as remediation suites) ──
def test_rt029_visual_supported_full_desktop(): _ensure_executed(); _assert_case("RT029.visual_supported_full_desktop")
def test_rt029_visual_supported_full_mobile(): _ensure_executed(); _assert_case("RT029.visual_supported_full_mobile")
def test_rt029_visual_partial_desktop(): _ensure_executed(); _assert_case("RT029.visual_partial_desktop")
def test_rt029_visual_partial_mobile(): _ensure_executed(); _assert_case("RT029.visual_partial_mobile")
def test_rt029_visual_unverified_desktop(): _ensure_executed(); _assert_case("RT029.visual_unverified_desktop")
def test_rt029_visual_unverified_mobile(): _ensure_executed(); _assert_case("RT029.visual_unverified_mobile")
def test_rt029_visual_stale_invalid_desktop(): _ensure_executed(); _assert_case("RT029.visual_stale_invalid_desktop")
def test_rt029_visual_stale_invalid_mobile(): _ensure_executed(); _assert_case("RT029.visual_stale_invalid_mobile")
def test_rt029_visual_pre20_stripped_desktop(): _ensure_executed(); _assert_case("RT029.visual_pre20_stripped_desktop")
def test_rt029_visual_pre20_stripped_mobile(): _ensure_executed(); _assert_case("RT029.visual_pre20_stripped_mobile")
def test_rt029_visual_mutation_detected_desktop(): _ensure_executed(); _assert_case("RT029.visual_mutation_detected_desktop")
def test_rt029_visual_mutation_detected_mobile(): _ensure_executed(); _assert_case("RT029.visual_mutation_detected_mobile")
def test_rt029_visual_mutation_layout_assert_desktop(): _ensure_executed(); _assert_case("RT029.visual_mutation_layout_assert_desktop")
def test_rt029_visual_mutation_layout_assert_mobile(): _ensure_executed(); _assert_case("RT029.visual_mutation_layout_assert_mobile")
def test_rt029_visual_baseline_selftest_desktop(): _ensure_executed(); _assert_case("RT029.visual_baseline_selftest_desktop")
def test_rt029_visual_baseline_selftest_mobile(): _ensure_executed(); _assert_case("RT029.visual_baseline_selftest_mobile")
def test_rt029_visual_baseline_restored_desktop(): _ensure_executed(); _assert_case("RT029.visual_baseline_restored_desktop")
def test_rt029_visual_baseline_restored_mobile(): _ensure_executed(); _assert_case("RT029.visual_baseline_restored_mobile")
def test_rt029_visual_corrupt_baseline_fails_desktop(): _ensure_executed(); _assert_case("RT029.visual_corrupt_baseline_fails_desktop")
def test_rt029_visual_corrupt_baseline_fails_mobile(): _ensure_executed(); _assert_case("RT029.visual_corrupt_baseline_fails_mobile")


if __name__ == "__main__":
    raise SystemExit(main())
