#!/usr/bin/env python3
"""RT101-V14 grounding repair suite — DEVELOPMENT_ONLY, non-formal.

Locks the V14 grounding contract repair (2026-09-19):

  G1. Single canonical match normalization (canonical_match_view):
      markdown link wrappers collapse to the visible label, bare URLs and
      whitespace runs are formatting-only; substantive words are never
      normalized away.
  G2. Raw offset mapping: every locator hit maps back through the
      reversible normalized→raw map; normalized indexes are NEVER exposed
      as raw offsets (raw slice == evidence_span always).
  G3. Blind prefix extension is REMOVED: a 20/30/40-char anchor can never
      yield a long VALID span. Full-span canonical correspondence is
      required; anchor-only hits stay FUZZY (diagnostic) with
      match_diagnostics.
  G4. Locators (sentence-similarity, keyword) never self-validate: VALID
      only after full correspondence verification of the candidate region.
  G5. FUZZY is diagnostic and carries NO display authority:
      display_authorized ⇒ grounding_status == "VALID" at the display
      integrity seam; withheld FUZZY rows clear supports_claim_ids but
      keep their evidence text for internal verification.
  G6. Scorer structural compatibility: every VALID row's
      (start, end, evidence_span) satisfies the V14 scorer's citation
      structural checks — ints with end > start, non-empty spans list, and
      norm(source[start:end]) == norm(span text) against the same fb>b>as
      record text authority the scorer replicates.

Run: python3 tests_rt101_v14_grounding.py  (exit 0 = all pass)
"""
from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from citation_grounding import (
    canonical_match_view,
    fuzzy_locate_span,
    ground_citation_evidence,
    get_original_text,
    _link_stripped_with_map,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}  {detail}")


# scorer-mirror normalization (same as score_v14 norm())
ZERO_WS = re.compile("[​-‏‪-‮⁠﻿]")
def norm(s):
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = ZERO_WS.sub("", s).replace("×", "x").replace("ｘ", "x")
    s = re.sub(r"\s+", " ", s)
    return s


# ── G1: canonical_match_view semantics ──────────────────────────────────────
def test_canonical_view():
    print("── G1: canonical_match_view")
    check("crlf collapsed",
          canonical_match_view("a\r\nb") == canonical_match_view("a\nb"))
    check("markdown link keeps label",
          canonical_match_view("见[程素微](https://ex.com/a)文章")
          == canonical_match_view("见程素微文章"))
    check("empty link label removed",
          canonical_match_view("学会[](https://mp.weixin.qq.com/s?x=1)正文")
          == canonical_match_view("学会正文"))
    check("bare url removed",
          canonical_match_view("来源https://example.com/x结束")
          == canonical_match_view("来源 结束"))
    check("whitespace runs collapse",
          canonical_match_view("a \t b") == canonical_match_view("a b"))
    check("substantive words preserved",
          "能量密度" in canonical_match_view("能量密度 500Wh/kg"))
    check("no cross-content equality",
          canonical_match_view("完全不同的内容甲") != canonical_match_view("完全不同的内容乙"))


# ── G2: raw offset mapping ──────────────────────────────────────────────────
def test_raw_map():
    print("── G2: raw offset mapping")
    raw = "标题\n\n原创  [程素微](https://mp.weixin.qq.com/s?__biz=x)  程素微 正文继续这里\n尾部"
    stripped, pmap = _link_stripped_with_map(raw)
    check("stripped drops link syntax", "](https" not in stripped)
    check("stripped keeps label", "程素微" in stripped)
    check("map length matches stripped", len(pmap) == len(stripped))
    # every mapped index is a strictly increasing raw position, and each
    # mapped raw char equals the stripped char (the map is the identity
    # outside link wrappers — non-wrapper text is never rewritten)
    check("map strictly increasing over raw positions",
          all(pmap[i] < pmap[i + 1] for i in range(len(pmap) - 1)))
    check("map identity outside link wrappers",
          all(pmap[i] < len(raw) and raw[pmap[i]] == stripped[i]
              for i in range(len(pmap))
              if "](http" not in stripped[max(0, i - 24):i + 1]
              and stripped[i] not in "\n"))
    # full round trip through the locator
    proposed = "程素微 正文继续这里"
    found, s, e, matched = fuzzy_locate_span(proposed, raw)
    check("locator finds link-wrapped region", found)
    check("raw slice equals matched text", raw[s:e] == matched,
          f"{s},{e}")
    check("raw slice view equals proposed view",
          canonical_match_view(raw[s:e]) == canonical_match_view(proposed))
    # raw != normalized coordinates case: leading newlines + double spaces
    raw2 = "\n\n a  [b](http://x) c\n"
    found2, s2, e2, m2 = fuzzy_locate_span("a b c", raw2)
    check("offset-shifted region found", found2 and raw2[s2:e2] == m2)
    check("shifted raw slice views equal",
          canonical_match_view(raw2[s2:e2]) == canonical_match_view("a b c"))


# ── G3/G4: correspondence ladder ────────────────────────────────────────────
def test_grounding_ladder():
    print("── G3/G4: grounding ladder")
    # exact
    rec = {"fb": "固态电池使用硫化物电解质，能量密度达到500Wh/kg。", "b": "", "as": ""}
    r = ground_citation_evidence(rec, proposed_span="能量密度达到500Wh/kg")
    check("exact → VALID", r["grounding_status"] == "VALID")
    check("exact raw slice round trip",
          get_original_text(rec)[r["start_offset"]:r["end_offset"]]
          == r["evidence_span"])
    check("exact match_type", r["match_type"] == "exact")

    # normalized exact (whitespace + link differences only)
    rec_link = {"fb": "原创 [程素微](https://mp.weixin.qq.com/s?a=1) 报道了固态电池进展。",
                "b": "", "as": ""}
    r2 = ground_citation_evidence(rec_link, proposed_span="原创 程素微 报道了固态电池进展。")
    check("normalized exact → VALID", r2["grounding_status"] == "VALID",
          f"got {r2['grounding_status']} type={r2.get('match_type')}")
    check("normalized raw slice view equality",
          canonical_match_view(
              get_original_text(rec_link)[r2["start_offset"]:r2["end_offset"]])
          == canonical_match_view("原创 程素微 报道了固态电池进展。"))
    check("normalized match_type", r2.get("match_type") == "normalized_exact")

    # blind prefix extension removed: 20-char anchor + divergent remainder
    raw_src = ("原创 [程素微](https://mp.weixin.qq.com/s?__biz=MzA) 程素微 中国可再生能源学会地热能专委会"
               " [](https://mp.weixin.qq.com/s?__biz=MzA)把油气田变热源：探索地热发电新路径 全文继续"
               "这里的内容与提议的后半段完全不同，绝无对应关系可言。")
    rec_anchor = {"fb": raw_src, "b": "", "as": ""}
    # proposed: shares ONLY the first 20 canonical chars, then diverges hard
    proposed_anchor = ("原创 程素微 程素微 中国可再生能源学会地热能专委会 美国 Mantle Energy 宣布完成"
                       " 500万美元种子轮融资用于推进油田地热全新技术路线与公司未来战略方向的完整表述内容。")
    r3 = ground_citation_evidence(rec_anchor, proposed_span=proposed_anchor)
    check("prefix anchor cannot manufacture VALID",
          r3["grounding_status"] != "VALID",
          f"got {r3['grounding_status']}")
    check("anchor hit stays diagnostic FUZZY/fail",
          r3["grounding_status"] in ("FUZZY", "GROUNDING_FAIL"))
    if r3["grounding_status"] == "FUZZY":
        d = r3.get("match_diagnostics") or {}
        check("fuzzy carries diagnostics",
              "similarity" in d and "full_candidate_length" in d, str(d))

    # sentence-similarity locator + full verification → VALID when full span matches
    rec_sent = {"fb": "前导句。Blackwell架构推理性能大幅提升，采用新一代Tensor Core技术。后续句。",
                "b": "", "as": ""}
    r4 = ground_citation_evidence(
        rec_sent, proposed_span="Blackwell架构推理性能大幅提升，采用新一代Tensor Core技术。")
    check("clean full span → VALID", r4["grounding_status"] == "VALID")
    check("verified round trip",
          get_original_text(rec_sent)[r4["start_offset"]:r4["end_offset"]]
          == r4["evidence_span"])

    # ellipsis-truncated windowed snippet: truncation decoration is stripped
    # for matching only; the core corresponds to a contiguous raw region → VALID
    rec_ell = {"fb": "前导。form a stable solid electrolyte interphase. Water-in-salt electrolyte works well. 尾句。", "b": "", "as": ""}
    r_ell = ground_citation_evidence(
        rec_ell,
        proposed_span="...form a stable solid electrolyte interphase. Water-in-salt electrolyte works...")
    check("ellipsis-decorated window → VALID",
          r_ell["grounding_status"] == "VALID",
          f"got {r_ell['grounding_status']} type={r_ell.get('match_type')}")
    check("ellipsis raw slice round trip",
          get_original_text(rec_ell)[r_ell["start_offset"]:r_ell["end_offset"]]
          == r_ell["evidence_span"])

    # keyword locate without proposed span stays FUZZY (locator only)
    r5 = ground_citation_evidence(
        {"fb": "NVIDIA发布Blackwell架构推理性能突破。", "b": "", "as": ""},
        proposed_span="", claim_text="", query="Blackwell架构推理性能")
    check("keyword locate never VALID", r5["grounding_status"] == "FUZZY",
          f"got {r5['grounding_status']}")

    # no correspondence at all → GROUNDING_FAIL, empty span
    r6 = ground_citation_evidence(
        {"fb": "完全无关的内容。", "b": "", "as": ""},
        proposed_span="毫不相干的另外一段很长的证据文本内容，确实不存在于原文中任何位置。")
    check("no correspondence → GROUNDING_FAIL",
          r6["grounding_status"] == "GROUNDING_FAIL")
    check("fail emits no span", r6["evidence_span"] == "")


# ── G5: display seam ────────────────────────────────────────────────────────
def test_display_seam():
    print("── G5: display authority (committed seam function)")
    # Exercise the COMMITTED seam authority directly — the very function
    # server.py's RD-1 display seam applies (no local mirror of the
    # invariant: a seam regression cannot leave this suite green).
    from citation_grounding import enforce_display_integrity

    rows = [
        {"id": 1, "grounding_status": "VALID", "display_authorized": True,
         "supports_claim_ids": ["c1"], "evidence_span": "E1"},
        {"id": 2, "grounding_status": "FUZZY", "display_authorized": True,
         "supports_claim_ids": ["c2"], "evidence_span": "E2"},
        {"id": 3, "grounding_status": "GROUNDING_FAIL",
         "display_authorized": True, "supports_claim_ids": ["c3"],
         "evidence_span": "E3"},
        {"id": 4, "grounding_status": "VALID", "display_authorized": False,
         "supports_claim_ids": []},
    ]
    pre_rows = [dict(r) for r in rows]
    out = enforce_display_integrity(rows, "PARTIALLY_SUPPORTED")
    check("seam returns the same row list (in place)",
          out is rows and len(rows) == 4)
    disp = [r for r in rows if r.get("display_authorized")]
    check("only VALID stays displayed",
          [r["id"] for r in disp] == [1], str([(r["id"], r["grounding_status"]) for r in disp]))
    check("displayed FUZZY count == 0",
          sum(1 for r in disp if r["grounding_status"] != "VALID") == 0)
    check("withheld FUZZY clears support links",
          rows[1]["supports_claim_ids"] == [] and rows[1]["display_authorized"] is False)
    check("withheld FAIL clears support links",
          rows[2]["supports_claim_ids"] == [])
    check("withheld row keeps evidence for internal verification",
          all(rows[i]["evidence_span"] == pre_rows[i]["evidence_span"]
              for i in (1, 2)) and rows[1]["evidence_span"] == "E2")

    # UNSUPPORTED terminal: nothing verified-supported → EVERY row
    # withheld, still in place (payload rows retained for diagnostics).
    rows_u = [
        {"id": 1, "grounding_status": "VALID", "display_authorized": True,
         "supports_claim_ids": ["c1"], "evidence_span": "U1"},
        {"id": 2, "grounding_status": "FUZZY", "display_authorized": True,
         "supports_claim_ids": ["c2"], "evidence_span": "U2"},
    ]
    enforce_display_integrity(rows_u, "UNSUPPORTED")
    check("UNSUPPORTED displays zero citations",
          all(r.get("display_authorized") is False for r in rows_u))
    check("UNSUPPORTED clears all support links",
          all(r.get("supports_claim_ids") == [] for r in rows_u))
    check("UNSUPPORTED retains payload rows (no surgery)",
          len(rows_u) == 2
          and all(r.get("evidence_span") == "U%d" % r["id"]
                  for r in rows_u))


# ── G6: scorer structural compatibility over synthetic + real shapes ───────
def test_scorer_compat():
    print("── G6: scorer structural compatibility")

    def scorer_structural_ok(row, source_text):
        """Mirror of the V14 scorer citation_structural grounding checks."""
        if str(row.get("grounding_status") or "") != "VALID":
            return False, "grounding_not_valid"
        spans = row.get("evidence_spans") or []
        if not spans:
            return False, "no_evidence_spans"
        for sp in spans:
            st, en = sp.get("start"), sp.get("end")
            txt = norm(str(sp.get("text") or ""))
            if not isinstance(st, int) or not isinstance(en, int) or en <= st:
                return False, "span_slice_mismatch"
            if source_text and norm(source_text[st:en]) != txt:
                return False, "span_slice_mismatch"
        return True, ""

    recs = [
        {"fb": "固态电池使用硫化物电解质，能量密度达到500Wh/kg。", "b": "", "as": ""},
        {"fb": "原创 [作者名](https://mp.weixin.qq.com/s?a=1&b=2) 报道了新技术路线的进展与产业化前景。",
         "b": "", "as": ""},
        {"fb": "长文本：" + ("风能海上机组可利用率约98%，发电量提升明显。" * 9), "b": "", "as": ""},
    ]
    proposed = [
        "能量密度达到500Wh/kg",
        "作者名 报道了新技术路线的进展与产业化前景。",
        "海上机组可利用率约98%，发电量提升明显。风能海上机组可利用率约98%",
    ]
    for rec, prop in zip(recs, proposed):
        g = ground_citation_evidence(rec, proposed_span=prop)
        text = get_original_text(rec)
        row = {
            "grounding_status": g["grounding_status"],
            "evidence_spans": [{"text": g["evidence_span"],
                                "start": g["start_offset"],
                                "end": g["end_offset"]}],
        }
        ok, why = scorer_structural_ok(row, text)
        check(f"scorer-compatible [{prop[:12]}…]",
              g["grounding_status"] != "VALID" or ok,
              f"status={g['grounding_status']} why={why}")


def main():
    test_canonical_view()
    test_raw_map()
    test_grounding_ladder()
    test_display_seam()
    test_scorer_compat()
    print("═" * 62)
    print(f"  V14 grounding suite: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
