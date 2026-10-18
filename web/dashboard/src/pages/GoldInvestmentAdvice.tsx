import { ChevronDown, ShieldCheck } from "lucide-react";
import type { GoldResearchResult } from "../api";
import { goldInvestmentEvidence } from "./goldModel";

const STANCES = { WAIT: "等待", GRADUAL_ALLOCATION: "条件式分批配置", DEFENSIVE: "防守观察" };
const AUDIT_DECISIONS = { PASS: "通过", REVISE: "要求修订", REJECT: "拒绝" };

interface Props {
  advice: NonNullable<GoldResearchResult["investment_advice"]> | null | undefined;
  audit?: GoldResearchResult["advice_audit"];
  historical?: boolean;
  asOf: string;
  symbol: string;
}

export default function GoldInvestmentAdvice({ advice, audit, historical = false, asOf, symbol }: Props) {
  const visibleAdvice = !audit || audit.status === "PASS" ? advice : null;
  const auditLabel = audit?.status === "PASS" ? visibleAdvice ? "独立终审通过 · PASS" : "发布记录不完整 · 未发布"
    : audit?.status === "REJECT" ? "终审拒绝 · REJECT" : "独立终审不可用 · UNAVAILABLE";
  const draftHashes = [...new Set(audit?.rounds.map((round) => round.draft_hash) ?? [])];
  const latestReview = audit?.rounds.at(-1)?.review;
  const latestIssues = latestReview ? [
    ...latestReview.unsupported_claims.map((issue) => `未支撑论断：${issue}`),
    ...latestReview.missing_risks.map((issue) => `风险缺漏：${issue}`),
  ] : [];
  return <section className="gold-investment-advice" aria-labelledby="gold-investment-advice-title">
    <div className="gold-heading"><h3 id="gold-investment-advice-title">{historical ? "历史中长期投资建议" : "中长期投资建议"}</h3>
      {visibleAdvice && <div className="gold-advice-badges"><span className="gold-badge">{visibleAdvice.horizon}</span>
        <span className="gold-badge">{STANCES[visibleAdvice.stance]}</span></div>}</div>
    {historical && <p className="gold-meta">历史报告 · 非本次分析结果</p>}
    {audit ? <p className="gold-advice-status" data-status={audit.status === "PASS" && !visibleAdvice ? "UNPUBLISHED" : audit.status}>{auditLabel}</p>
      : visibleAdvice && <p className="gold-advice-status">历史建议 · 未经独立终审</p>}
    {visibleAdvice ? <p className="gold-advice-summary">{visibleAdvice.summary}</p>
      : <p className={audit ? "gold-advice-summary" : "gold-muted"}>{audit ? "证据不足 / 暂不发布投资建议" : "该历史报告未生成中长期建议"}</p>}
    {audit && <p className="gold-advice-audit-reason">{audit.reason}</p>}
    {!visibleAdvice && !!latestIssues.length && <ul className="gold-advice-audit-issues">
      {latestIssues.slice(0, 3).map((issue, index) => <li key={index}>{issue}</li>)}
    </ul>}
    <p className="gold-meta">报告证据截至 <time dateTime={asOf || undefined}>{asOf || "未返回"}</time> · {symbol}</p>
    {visibleAdvice && <>
      <ul className="gold-advice-reasons">{visibleAdvice.reasons.slice(0, 3).map((reason, index) => <li key={index}>
        <strong>{goldInvestmentEvidence(reason)}</strong> → {reason.implication}</li>)}</ul>
      <p className="gold-advice-reassess"><strong>重新评估条件：</strong>{visibleAdvice.reassess_when}</p>
      <p className="gold-advice-limitations">{visibleAdvice.confidence === "LOW" && <strong>证据有限 · </strong>}
        仅研究，非个性化投资建议。{visibleAdvice.limitations}</p>
    </>}
    {audit && <details className="gold-audit gold-advice-audit"><summary><ShieldCheck size={15} />
      终审记录 · {audit.rounds.length} 轮 / {draftHashes.length} 版<ChevronDown size={14} /></summary>
      {audit.published_draft_hash && visibleAdvice && <p className="gold-meta gold-advice-audit-hash">发布哈希 <code>{audit.published_draft_hash}</code></p>}
      {audit.rounds.length ? <ol className="gold-advice-audit-rounds">{audit.rounds.map((round) => <li key={`${round.round}:${round.draft_hash}`}>
        <div className="gold-heading"><h4>第 {round.round} 轮 · 版本 {draftHashes.indexOf(round.draft_hash) + 1}</h4>
          <span className="gold-advice-audit-decision" data-decision={round.review.decision}>{AUDIT_DECISIONS[round.review.decision]} · {round.review.decision}</span></div>
        <p className="gold-meta">{round.round === 1 ? "初次终审" : "修订后复审"} · {
          visibleAdvice && round.review.decision === "PASS" && round.draft_hash === audit.published_draft_hash ? "已发布版本" : "待审草案 / 未发布"}</p>
        <p>{round.review.rationale}</p>
        {(!!round.review.unsupported_claims.length || !!round.review.missing_risks.length) && <ul>
          {round.review.unsupported_claims.map((issue, index) => <li key={`claim-${index}`}>未支撑论断：{issue}</li>)}
          {round.review.missing_risks.map((issue, index) => <li key={`risk-${index}`}>风险缺漏：{issue}</li>)}
        </ul>}
        <p className="gold-meta">未支撑论断 {round.review.unsupported_claims.length} 项 · 风险缺漏 {round.review.missing_risks.length} 项 · 置信度调整 {round.review.confidence_adjustment}</p>
        <p className="gold-meta gold-advice-audit-hash">草案哈希 <code>{round.draft_hash}</code></p>
      </li>)}</ol> : <p className="gold-muted">未返回有效审查轮次</p>}
    </details>}
  </section>;
}