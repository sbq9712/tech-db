# 最终验收报告 — Agentic RAG 改造 (TK-24, Q27/Q23)

日期: 2026-08-14/15 (CST) · 执行: 自动化验收 + 本地全链路冒烟
状态: **PASS**（公网 tunnel 一项受网络限制，见豁免节）

## 一、三道门证据链

| 门 | 判据 | 证据 artifact | 结果 |
|----|------|---------------|------|
| 门1 检索层接线 | 新旧检索输出 parity（gate-1 基线 0 drift） | `test_fixtures/parity/baseline_mini.json`、`baseline_hybrid_legacy.json`；tests_parity 5/5 | PASS |
| 门2 延迟+预算+降级 | TTFB guard、budget 硬上限、降级路径 | `test_fixtures/gate2_report.json`（VERDICT: GATE2_PASS，R1a-R3d）；tests_budget_tk08 8/8、tests_ttfb_tk09 5/5、tests_degraded_tk10 6/6 | PASS |
| 门3 质量无退化 | shadow/replay 无 id_overlap/相关性/grounding 退化 | `test_fixtures/holdout/replay/day1.json`（主证据，按所有者压缩裁决）；`test_fixtures/gate3_report.json`；`shadow_diff_full.json` | PASS |

## 二、对照 spec 逐项验收（关键 R-决策）

| 规范决策 | 实现 | 验证 |
|----------|------|------|
| R3/R4 预算分类：循环控制 ≤12/query，超限降级 | budget_guard.QueryBudget + 轮次预留 + spend_or_raise | tests_budget_tk08 8/8；gate2 R1a 12/12 stop=budget_exceeded |
| R4 router 启发式优先，简单 query 零 LLM 成本 | heuristic router + FAST_RAG 单轮零 LLM 契约（TK-19 补齐） | tests_gate3_tk19；live trace: loop_calls=0, iterations=1 |
| Q10/R2 TTFB guard：基线+Δ，超限降级单遍 RAG | ttfb_guard + asyncio.wait_for + ttfb_degrade trace | tests_ttfb_tk09 5/5；live 观察到降级并恢复 |
| Q11 GLM 失败 → UNVERIFIED + 用户警告 | degraded_mode.build_user_warning + done.user_warning | tests_degraded_tk10 6/6；live 冒烟复现（⚠️ 警告条幅） |
| Q16/Q17 holdout 锁定 | sha256 lock + 每次运行校验 + 篡改即 exit 1 | tests_holdout_tk16 4/4 |
| Q19 合成隔离（评测侧） | synthetic_isolation_check + 9 个 as-only 锚点摘除 | tests_synthetic_tk20 5/5；`synthetic_isolation.json` |
| R5 expand-contract：contract 阶段删除旧路径 | TK-23：legacy 函数/逃生口删除，shadow→漂移监视 | tests_shadow_tk17 8/8（contract 形态） |
| Q20/R11 文档真实状态、数字只引 artifact | TK-21 重写 IMPLEMENTATION_STATUS | verify_spec_manifest V2/V6 PASS |
| Q24 sync 失败不重启 | sync_local.sh 门（validator+suite → 才重启） | tests_sync_tk22 4/4 |
| R9 knowledge boundary | 应答边界消息 + P2 落地 | tests_flags_tk06 |
| Q2 flag 全关可回滚 | 21 flag 环境变量 kill switch | tests_flags_tk06 + validator V1/V2 |
| Q25 前端证据卡片/降级标记 | qa.js v=162：claims→citations 卡片、AI_SUMMARY 徽标、警告条幅 | tests_frontend_tk13 7/7；live SSE 验证 claims=4 + warning |

## 三、线上冒烟记录（本机 :8765，gate3 全开）

| 项 | 结果 |
|----|------|
| /api/health | 200；21 flags 全 on；retrieval_legacy=None（contract） |
| /api/search | 200；25 results；top1 相关（钙钛矿报告） |
| /api/graph | 200；300 nodes / 549 edges |
| /api/chat/stream (SSE) | 完整事件序列（status→token→citations→done）；agentic FAST_RAG 1 轮 0 LLM 循环调用；verification PASSED/UNVERIFIED 两形态均复现；claims=4（含 fallback 链）；citation_grounding 25/25 |
| /api/shadow/report | 200；contract 形态（frozen reference） |
| trace | runtime/traces/*.jsonl 全阶段（rewrite→…→post_budget） |

## 四、测试总量（引用 artifact，不手写）
以 `qa-backend/test_summary.json` 为准：
- push tier: run_all_tests --tier push（23 suites）
- nightly tier: shadow_tk17 + final_acceptance（tests_final_acceptance 72/72 ACCEPTANCE PASSED）
- validator: verify_spec_manifest 7/7 PASS

## 五、豁免与后续
1. **公网 tunnel**：cloudflared 进程在跑（quick tunnel → :8765），但本机出网 QUIC 被
   环境阻断（cloudflared.log: "failed to dial to edge with quic: timeout"）。
   curl 不可达（000）。属于环境网络限制，非代码问题；恢复网络后无需改动即可验证。
2. **索引体积**：真实 1.2G 索引仅本机（gitignored）；CI 用 MINI fixture（见
   IMPLEMENTATION_STATUS 豁免节）。
3. **后续 ticket（spec 范围外，已登记）**：LightRAG ingest 恢复（"AI精选+精选情报"
   剩余 ~6000 条图谱构建，用户原话"Q5之后再说"）→ 本轮完成后即开始执行。

## 六、24/24 ticket 关闭确认
TK-01..24 全部关闭；证据链见 IMPLEMENTATION_STATUS.md 的
"Ticket Closure & Evidence Chain" 表（TK-01..24 逐条）。
