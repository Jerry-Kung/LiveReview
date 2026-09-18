/**
 * 复盘结论面板：把一场直播的分析结论按「结论 → 依据 → 行动」铺开。
 *
 * 呈现的三条原则：
 *
 * 1. **先讲边界，再讲结论**。分析等级与定级依据排在最前——用户得先知道这份结论建立在
 *    多少素材上，才知道能信到什么程度。缺口（missing_info）不藏在末尾的角落里。
 * 2. **每条判断都带证据与等级**。事实、高置信推断、待验证假设三档在界面上分开标注，
 *    这是准则里对「禁止把相关性写成因果」的界面落实。
 * 3. **时间戳只作文本坐标**。视频切片播放已撤销，这里不做跳转，只如实显示发生位置，
 *    便于用户带着时间点去原始素材里核对。
 */

import { useEffect } from "react";
import {
  fetchTask,
  reviewDownloadUrl,
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

function moment(seconds: number | null): string {
  return typeof seconds === "number" ? formatTimestamp(seconds) : "时间未给出";
}

/** 汇总：状态、覆盖面、模型、完成时间。 */
function Summary({ review }: { review: TaskReview }) {
  const level = review.result?.analysis_level ?? "";
  const facts: Array<[string, string, "ok" | "busy" | "warn" | "error" | null]> = [
    ["复盘状态", reviewStatusLabel(review.status), reviewStatusTone(review.status)],
    ["分析等级", level || PLACEHOLDER, reviewLevelTone(level) ?? null],
    ["投入分析", `${review.segment_count} 条语音记录`, null],
    ["调用批次", review.batch_count > 1 ? `${review.batch_count}（长转写分批）` : `${review.batch_count || PLACEHOLDER}`, null],
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

/** 结论正文：一句话结论 → 关键事件 → 发现 → TOP 问题 → 下一场动作 → 缺口。 */
function Report({ result }: { result: ReviewResult }) {
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
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="subsection">
        <h4 className="subsection-title">
          本场 TOP 问题
          <span className="subsection-count num">{result.top_issues.length} 条</span>
        </h4>
        {result.top_issues.length === 0 ? (
          <p className="hint">未给出需要优先处理的问题。</p>
        ) : (
          <ol className="issues">
            {result.top_issues.map((issue, index) => (
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
                {action.how && <p className="action-how">怎么做：{action.how}</p>}
                {action.observe && <p className="action-observe">观察：{action.observe}</p>}
              </li>
            ))}
          </ol>
        )}
      </section>

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

  // 复盘在后台跑：处于复盘中就按节奏轮询，直到拿到终态
  useEffect(() => {
    if (review?.status !== "running") return;
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
  }, [review?.status, task.id, onTask]);

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
            复盘进度 {review.progress}%，可以关闭页面，稍后回来看
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
        <Report result={review.result} />
      ) : (
        !running &&
        review?.status !== "succeeded" && (
          <p className="hint mt-md">
            复盘会读取整场语音转写，按内容结构、产品讲解、互动、转化与表达方式给出分析与
            下一场动作；本版没有经营数据，结论不涉及在线、停留与转化效果。
          </p>
        )
      )}
    </section>
  );
}
