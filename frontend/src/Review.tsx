/**
 * 复盘结论面板：把一场直播的分析结论按「结论 → 依据 → 行动」铺开。
 *
 * 呈现的四条原则：
 *
 * 1. **先讲边界，再讲结论**。分析等级与定级依据排在最前——用户得先知道这份结论建立在
 *    多少素材上，才知道能信到什么程度。缺口（missing_info）不藏在末尾的角落里。
 * 2. **每条判断都带证据与等级**。事实、高置信推断、待验证假设三档在界面上分开标注，
 *    这是准则里对「禁止把相关性写成因果」的界面落实。
 * 3. **编号要能查**（V0.8.0）。结论里写「心法3」「使用话术样板 04」，界面必须能就地展开
 *    它们说的是什么——查不到出处的编号等于一句暗语。编号对照表来自 `/api/guideline`，
 *    与提示词同源。
 * 4. **时间戳只作文本坐标**。视频切片播放已撤销，这里不做跳转，只如实显示发生位置，便于
 *    用户带着时间点去原始素材里核对。
 */

import { useEffect, useMemo, useState } from "react";
import {
  fetchGuideline,
  fetchTask,
  reviewDownloadUrl,
  type Guideline,
  type ReviewIssue,
  type ReviewResult,
  type Task,
  type TaskReview,
} from "./api";
import { pollIntervalMs } from "./polling";
import {
  PLACEHOLDER,
  formatMoment,
  formatTimestamp,
  reviewLevelTone,
  reviewStatusLabel,
  reviewStatusTone,
} from "./format";
import { IconReport } from "./icons";

/** 分析等级在界面上的说明：等级决定了哪些结论可以逐句评价。 */
const LEVEL_NOTES: Record<string, string> = {
  完整: "全部片段识别成功，可对内容、话术与互动做较完整分析。",
  部分: "存在识别失败或记录被丢弃的片段，相关时段的结论仅为推断。",
  受限: "转写覆盖不足，本次只给主题与结构层面的观察，不评价具体话术细节。",
};

/** 分轮名称的中文说明：多轮专项调用之后，「跑了哪几轮」是可解释项。 */
const PASS_LABELS: Record<string, string> = {
  structure: "内容结构",
  checks: "六维检核",
  attribution: "问题归因与实验",
  quotes: "金句采集",
};

function moment(seconds: number | null): string {
  return typeof seconds === "number" ? formatTimestamp(seconds) : "时间未给出";
}

/** 汇总：状态、覆盖面、模型、完成时间。 */
function Summary({ review }: { review: TaskReview }) {
  const level = review.result?.analysis_level ?? "";
  const passes = review.result?.passes_run ?? [];
  const facts: Array<[string, string, "ok" | "busy" | "warn" | "error" | null]> = [
    ["复盘状态", reviewStatusLabel(review.status), reviewStatusTone(review.status)],
    ["分析等级", level || PLACEHOLDER, reviewLevelTone(level) ?? null],
    ["投入分析", `${review.segment_count} 条语音记录`, null],
    [
      "模型调用",
      review.batch_count > 1 ? `${review.batch_count} 次（分轮专项分析）` : `${review.batch_count || PLACEHOLDER}`,
      null,
    ],
    [
      "分析轮次",
      passes.length > 0
        ? passes.map((name) => PASS_LABELS[name] ?? name).join(" → ")
        : PLACEHOLDER,
      null,
    ],
    ["完成时间", formatMoment(review.finished_at), null],
    ["模型", review.model_name ?? PLACEHOLDER, null],
  ];

  return (
    <dl className="facts-grid" aria-label="复盘汇总">
      {facts.map(([label, value, tone]) => (
        <div key={label}>
          <dt className="fact-label">{label}</dt>
          <dd className="fact-value">
            {tone === null ? (
              value
            ) : (
              <span className="status" data-tone={tone}>
                {value}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** 编号标签：把「心法3」「04」渲染成可展开的引用，原文来自准则对照表。 */
function CodeChips({
  mindSets,
  samples,
  codes,
}: {
  mindSets: Map<string, Guideline["mindsets"][number]>;
  samples: Map<string, Guideline["samples"][number]>;
  codes: { mindsets?: string[]; samples?: string[] };
}) {
  const chips: Array<{ key: string; label: string; detail: string; kind: "mind" | "sample" }> = [];
  for (const code of codes.mindsets ?? []) {
    const item = mindSets.get(code);
    chips.push({
      key: `m-${code}`,
      label: code,
      detail: item ? `${item.name}：${item.check}` : "准则中无此编号",
      kind: "mind",
    });
  }
  for (const code of codes.samples ?? []) {
    const item = samples.get(code);
    chips.push({
      key: `s-${code}`,
      label: `样板 ${code}`,
      detail: item ? `${item.usage ? `${item.usage}：` : ""}${item.text}` : "准则中无此编号",
      kind: "sample",
    });
  }
  if (chips.length === 0) return null;

  return (
    <ul className="code-chips" aria-label="引用的心法与话术样板">
      {chips.map((chip) => (
        <li key={chip.key} className="code-chip" data-kind={chip.kind}>
          <details>
            <summary>{chip.label}</summary>
            <p className="code-chip-detail">{chip.detail}</p>
          </details>
        </li>
      ))}
    </ul>
  );
}

/** 结论正文：一句话结论 → 主题分布 → 关键事件 → 发现 → 重复度 → 违规词 → 问题 → 动作 → 金句 → 缺口。 */
function Report({ result, guideline }: { result: ReviewResult; guideline: Guideline | null }) {
  const mindSets = useMemo(
    () => new Map((guideline?.mindsets ?? []).map((item) => [item.code, item])),
    [guideline]
  );
  const samples = useMemo(
    () => new Map((guideline?.samples ?? []).map((item) => [item.code, item])),
    [guideline]
  );

  return (
    <div className="report">
      {result.level_reason && (
        <p className="report-lead">
          <strong>定级依据：</strong>
          {result.level_reason}
          {LEVEL_NOTES[result.analysis_level] && ` ${LEVEL_NOTES[result.analysis_level]}`}
        </p>
      )}

      <section>
        <h4 className="subsection-title mt-lg">本场一句话结论</h4>
        <p className="report-one-line">{result.one_line || "（模型未给出结论）"}</p>
      </section>

      {result.topic_distribution && (
        <section className="subsection">
          <h4 className="subsection-title">内容主题分布</h4>
          <p className="report-topic">{result.topic_distribution}</p>
        </section>
      )}

      <section className="subsection">
        <h4 className="subsection-title">
          关键事件
          <span className="subsection-count num">{result.key_events.length} 条</span>
        </h4>
        {result.key_events.length === 0 ? (
          <p className="hint">未识别出关键事件。</p>
        ) : (
          <ol className="timeline">
            {result.key_events.map((event, index) => (
              <li key={`${event.start_seconds}-${index}`} className="timeline-item">
                <span className="timeline-time">{moment(event.start_seconds)}</span>
                <span className="timeline-topic">{event.topic}</span>
                <span className="timeline-summary">{event.summary}</span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="subsection">
        <h4 className="subsection-title">
          分析发现
          <span className="subsection-count num">{result.findings.length} 条</span>
        </h4>
        {result.findings.length === 0 ? (
          <p className="hint">未给出分析发现。</p>
        ) : (
          <ul className="findings">
            {result.findings.map((finding, index) => (
              <li key={`${finding.dimension}-${index}`} className="finding">
                <p className="finding-head">
                  <span className="finding-dimension">{finding.dimension}</span>
                  {/* 证据等级是这条判断的可信度标签，顺着准则的三档取值原样展示 */}
                  <span className="finding-level" data-level={finding.evidence_level}>
                    {finding.evidence_level || "未标注等级"}
                  </span>
                  <span className="finding-time">{moment(finding.start_seconds)}</span>
                </p>
                <p className="finding-judgement">{finding.judgement}</p>
                {finding.evidence && (
                  <p className="finding-evidence">依据：{finding.evidence}</p>
                )}
                {finding.suggestion && finding.suggestion !== "保持" && (
                  <p className="finding-suggestion">建议：{finding.suggestion}</p>
                )}
                <CodeChips
                  mindSets={mindSets}
                  samples={samples}
                  codes={{ mindsets: finding.principle_codes }}
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      {result.repetition.length > 0 && (
        <section className="subsection">
          <h4 className="subsection-title">
            内容重复度
            <span className="subsection-count num">{result.repetition.length} 项</span>
          </h4>
          <ul className="repetition">
            {(result.repetition ?? []).map((item, index) => (
              <li key={`${item.item}-${index}`} className="repetition-item">
                <p className="repetition-head">
                  <span className="repetition-name">{item.item}</span>
                  <span className="repetition-count num">共 {item.count} 次</span>
                  {/* 有无信息增量决定这条要不要当作问题处理，因此单独标出来 */}
                  <span className="repetition-flag" data-increment={item.has_increment ? "yes" : "no"}>
                    {item.has_increment ? "有信息增量" : "同质重复"}
                  </span>
                </p>
                {item.verdict && <p className="repetition-verdict">{item.verdict}</p>}
                {item.times && <p className="repetition-times">时段：{item.times}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {result.violations.length > 0 && (
        <section className="subsection">
          <h4 className="subsection-title">
            违规表达与替换
            <span className="subsection-count num">{result.violations.length} 处</span>
          </h4>
          <ul className="violations">
            {(result.violations ?? []).map((item, index) => (
              <li key={`${item.term}-${index}`} className="violation-item">
                <p className="violation-head">
                  <span className="violation-time">{moment(item.start_seconds)}</span>
                  <span className="violation-term">{item.term}</span>
                  <span className="violation-arrow">改为</span>
                  <span className="violation-replacement">{item.replacement}</span>
                </p>
                {item.quote && <p className="violation-quote">原话：{item.quote}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="subsection">
        <h4 className="subsection-title">
          本场 TOP 问题
          <span className="subsection-count num">{result.top_issues.length} 条</span>
        </h4>
        {result.top_issues.length === 0 ? (
          <p className="hint">未给出需要优先处理的问题。</p>
        ) : (
          <ol className="issues">
            {result.top_issues.map((issue: ReviewIssue, index) => (
              <li key={`${issue.problem}-${index}`} className="issue">
                <p className="issue-problem">{issue.problem}</p>
                <dl className="issue-fields">
                  {issue.evidence && (
                    <div>
                      <dt>证据</dt>
                      <dd>{issue.evidence}</dd>
                    </div>
                  )}
                  {issue.impact && (
                    <div>
                      <dt>影响</dt>
                      <dd>{issue.impact}</dd>
                    </div>
                  )}
                  {issue.root_cause && (
                    <div>
                      <dt>根因</dt>
                      <dd>{issue.root_cause}</dd>
                    </div>
                  )}
                  {issue.action && (
                    <div>
                      <dt>下一场动作</dt>
                      <dd>{issue.action}</dd>
                    </div>
                  )}
                </dl>
                <CodeChips
                  mindSets={mindSets}
                  samples={samples}
                  codes={{ mindsets: issue.principle_codes, samples: issue.script_codes }}
                />
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="subsection">
        <h4 className="subsection-title">
          下一场实验动作
          <span className="subsection-count num">{result.next_actions.length} 条</span>
        </h4>
        {result.next_actions.length === 0 ? (
          <p className="hint">未给出下一场实验动作。</p>
        ) : (
          <ol className="actions-list">
            {result.next_actions.map((action, index) => (
              <li key={`${action.goal}-${index}`} className="action-item">
                <p className="action-goal">{action.goal}</p>
                {action.current_approach && (
                  <p className="action-current">当前方式：{action.current_approach}</p>
                )}
                {action.how && <p className="action-how">怎么做：{action.how}</p>}
                {action.change_reason && (
                  <p className="action-change">为什么换方向：{action.change_reason}</p>
                )}
                {action.observe && <p className="action-observe">观察：{action.observe}</p>}
                {action.success_criteria && (
                  <p className="action-criteria">成功标准：{action.success_criteria}</p>
                )}
                <CodeChips mindSets={mindSets} samples={samples} codes={{ samples: action.script_codes }} />
              </li>
            ))}
          </ol>
        )}
      </section>

      {result.quotes.length > 0 && (
        <section className="subsection">
          <h4 className="subsection-title">
            本场金句收录
            <span className="subsection-count num">{result.quotes.length} 条</span>
          </h4>
          <ul className="quotes">
            {(result.quotes ?? []).map((quote, index) => (
              <li key={`${quote.text}-${index}`} className="quote-item">
                <p className="quote-head">
                  <span className="quote-time">{moment(quote.start_seconds)}</span>
                  {quote.category && <span className="quote-category">{quote.category}</span>}
                </p>
                <p className="quote-text">{quote.text}</p>
                {quote.scene && <p className="quote-scene">适用场景：{quote.scene}</p>}
                {quote.effect && <p className="quote-effect">好在哪：{quote.effect}</p>}
              </li>
            ))}
          </ul>
          {/* 本版没有跨场次的话术库：收录只在结论里留痕，不承诺入库与检索 */}
          <p className="hint">
            本版仅在本场结论中留痕，不写入跨场次话术库（话术库与主播档案不在本版范围）。
          </p>
        </section>
      )}

      {/* 缺口放在结论之后、单独成块：用户读完结论就能看到它的边界在哪 */}
      {result.missing_info.length > 0 && (
        <section className="subsection report-part--gap">
          <h4 className="subsection-title">本场不足以判断</h4>
          <ul className="gaps">
            {result.missing_info.map((item, index) => (
              <li key={`${item}-${index}`}>{item}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export type ReviewActions = {
  onReview: () => void;
  reviewing: boolean;
};

export default function ReviewBlock({
  task,
  actions,
  onTask,
}: {
  task: Task;
  actions: ReviewActions;
  /** 复盘完成后回写最新任务：面板需要拿到结论才能渲染。 */
  onTask: (task: Task) => void;
}) {
  const review = task.review ?? null;
  const understanding = task.understanding ?? null;
  const clips = task.clips ?? [];
  const hasClips = clips.length > 0;
  const understood = (understanding?.clip_count ?? 0) > 0;
  const running = review?.status === "running" || actions.reviewing;

  // 编号对照表：只在结论里真的出现了编号时才去取，取不到就不展开标签（结论本身照常展示）。
  // 失败一律静默——它是辅助信息，不该因为它取不到就让复盘结论显示不出来。
  const [guideline, setGuideline] = useState<Guideline | null>(null);
  // 编号字段按「可能缺」处理：结论可能来自旧版本落库的数据，那时没有这些字段。
  // 少一个辅助标签不是崩掉整块结论的理由。
  const needsGuideline = useMemo(() => {
    const result = review?.result;
    if (!result) return false;
    const hasMind =
      (result.findings ?? []).some((item) => (item.principle_codes ?? []).length > 0) ||
      (result.top_issues ?? []).some((item) => (item.principle_codes ?? []).length > 0);
    const hasSample =
      (result.top_issues ?? []).some((item) => (item.script_codes ?? []).length > 0) ||
      (result.next_actions ?? []).some((item) => (item.script_codes ?? []).length > 0);
    return hasMind || hasSample;
  }, [review?.result]);

  useEffect(() => {
    if (!needsGuideline || guideline) return;
    let cancelled = false;
    void fetchGuideline()
      .then((data) => {
        if (!cancelled) setGuideline(data);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [needsGuideline, guideline]);

  // 复盘在后台跑：处于复盘中就按节奏轮询，直到拿到终态。
  // 守卫用 `running` 而不是 `review?.status`：`running` 含 `actions.reviewing`，
  // 因此点下按钮的那一刻轮询就启动了——否则「复盘中…」已经显示、轮询却没跑，
  // 进度会悬在 0 直到用户自己刷新（V0.6.2）。
  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void fetchTask(task.id)
        .then((latest) => {
          if (!cancelled) onTask(latest);
        })
        // 轮询失败不打断：复盘仍在后台跑，下个周期继续
        .catch(() => undefined);
    }, pollIntervalMs());
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running, task.id, onTask]);

  const blocked = !hasClips ? "还没有切片，请先完成切分" : !understood ? "还没有识别结果，请先完成识别" : null;

  return (
    <section className="panel" aria-label="复盘结论">
      <div className="panel-head">
        <IconReport size={18} />
        <h3 className="panel-title">复盘结论</h3>
        {review && (
          <span className="panel-head-side">
            <span className="status" data-tone={reviewStatusTone(review.status)}>
              {reviewStatusLabel(review.status)}
            </span>
          </span>
        )}
      </div>

      {review && <Summary review={review} />}

      {review?.error && <p className="detail-error mt-md">{review.error}</p>}

      <div className="actions mt-lg">
        <button
          type="button"
          className="btn btn--primary"
          onClick={actions.onReview}
          disabled={blocked !== null || running}
          title={blocked ?? undefined}
        >
          {running ? "复盘中…" : review?.result ? "重新复盘" : "开始复盘分析"}
        </button>
        {running && review && (
          <span className="hint">
            {review.status === "pending"
              ? "已提交，等待后台开始…"
              : `复盘进度 ${review.progress}%，可以关闭页面，稍后回来看`}
          </span>
        )}
        {blocked && <span className="hint">{blocked}</span>}
        {review?.result && (
          <button
            type="button"
            className="btn btn--flat"
            onClick={() => window.open(reviewDownloadUrl(task.id), "_blank")}
          >
            下载 Markdown 报告
          </button>
        )}
      </div>

      {review?.warnings && review.warnings.length > 0 && (
        <ul className="coverage-issues">
          {review.warnings.map((warning, index) => (
            <li key={`${warning}-${index}`}>{warning}</li>
          ))}
        </ul>
      )}

      {review?.result ? (
        <Report result={review.result} guideline={guideline} />
      ) : (
        !running &&
        review?.status !== "succeeded" && (
          <p className="hint mt-md">
            复盘会分四轮专项分析整场语音转写：先理清内容结构与关键事件，再按六个维度逐项对照
            心法检核（含卖点重复度与违规表达），然后据此归因并设计下一场实验，最后采集本场
            金句；本版没有经营数据，结论不涉及在线、停留与转化效果。
          </p>
        )
      )}
    </section>
  );
}
