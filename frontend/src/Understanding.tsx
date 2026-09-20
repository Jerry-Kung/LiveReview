/**
 * 识别结果面板：整场汇总、全文与片段级明细。
 *
 * 三块内容对应三种核对动作，顺序即「从结论到依据」：
 *
 * 1. **整场汇总**——识别覆盖了多少、哪几片没过，是判断结果能不能用的前提。
 * 2. **片段明细**——每片的识别状态与失败原因，失败的那片可以单独重试。
 * 3. **识别全文**——按原视频时间轴拼好的语音记录，逐条可按时间核对。
 *
 * 失败与缺失在这里一律显式呈现，不隐藏也不合并进成功计数：本版的核心验收就是
 * 「识别缺口明确可查」，界面上少说一句就够不成验收。
 *
 * V0.4.1 的两处调整：
 * - 全文那一块不再先摆一段等宽纯文本再摆条目。加工过的条目才是可读的内容，默认直接展开；
 *   纯文本全文降级为下载件，页面不再占一屏去重复同一份内容。
 * - 片段明细按组分页，与切片表同一套读法。
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  fetchTranscript,
  transcriptDownloadUrl,
  type Task,
  type TaskClip,
  type TaskUnderstanding,
  type Transcript,
} from "./api";
import GroupPager, { useGroups } from "./GroupedTable";
import {
  PLACEHOLDER,
  clipNumber,
  formatClipRange,
  formatMoment,
  formatTimestamp,
  understandingStatusLabel,
  understandingStatusTone,
} from "./format";
import { IconAlert, IconCheckCircle, IconVoice } from "./icons";

/** 整场汇总：覆盖情况 + 起点终点时间，让「这一轮跑了多久」可见。 */
function Summary({ understanding }: { understanding: TaskUnderstanding }) {
  const facts: Array<[string, string, "ok" | "busy" | "warn" | "error" | null]> = [
    ["识别状态", understandingStatusLabel(understanding.status), understandingStatusTone(understanding.status)],
    [
      "已识别片段",
      understanding.failed_clip_count > 0
        ? `${understanding.clip_count}（${understanding.failed_clip_count} 片失败）`
        : `${understanding.clip_count}`,
      null,
    ],
    ["语音记录", `${understanding.segment_count} 条`, null],
    ["完成时间", formatMoment(understanding.finished_at), null],
    ["模型", understanding.model_name ?? PLACEHOLDER, null],
  ];

  return (
    <dl className="facts-grid" aria-label="识别汇总">
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

/**
 * 识别全文：按原视频时间轴排好的语音条目，进入这一页即拉取并展示。
 *
 * 这里只展示条目，不再展示后端拼好的等宽纯文本：那份文本与下面这些条目是同一批内容，
 * 只是排版不同，页面上并排摆两份只会让人不知道该核对哪一份。纯文本保留为下载件，
 * 需要全文做外部处理时用它。
 *
 * 没有展开/收起：V0.4.1 把识别全文作为「内容理解」页的默认内容，多一次点击没有意义。
 */
function TranscriptPanel({
  taskId,
  refreshKey,
  disabled,
}: {
  taskId: string;
  /** 变化时重新拉取：识别推进或单片重试后由父组件递增。 */
  refreshKey: number;
  disabled: boolean;
}) {
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [error, setError] = useState<string | null>(null);
  const groups = useGroups(transcript?.segments.length ?? 0);

  const load = useCallback(async () => {
    try {
      setTranscript(await fetchTranscript(taskId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "读取识别全文失败");
    }
  }, [taskId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const download = () => {
    window.open(transcriptDownloadUrl(taskId), "_blank");
  };

  if (error !== null) {
    return (
      <>
        <p className="detail-error">{error}</p>
        <div className="actions mt-md">
          <button type="button" className="btn" onClick={() => void load()}>
            重新读取
          </button>
        </div>
      </>
    );
  }

  if (transcript === null) return <p className="hint">正在读取识别全文…</p>;

  const segments = transcript.segments;
  const visible = segments.slice(groups.start, groups.end);

  return (
    <div>
      <div className="transcript-head">
        <span className="hint">
          共 {transcript.clip_count} 片，已识别 {transcript.succeeded_clip_count} 片
          {transcript.failed_clip_count > 0 && `，失败 ${transcript.failed_clip_count} 片`}
          ，语音 <span className="num">{transcript.segment_count}</span> 条
          {disabled && "（识别进行中，内容会继续增长）"}
        </span>
        <button type="button" className="btn btn--flat" onClick={download}>
          下载 txt
        </button>
      </div>

      {segments.length === 0 ? (
        <p className="hint mt-md">这一轮没有识别出语音条目。</p>
      ) : (
        <>
          <GroupPager
            label="识别全文分组"
            total={segments.length}
            page={groups.page}
            pageCount={groups.pageCount}
            start={groups.start}
            end={groups.end}
            onPage={groups.setPage}
          />

          <ol className="segments">
            {visible.map((segment) => (
              <li
                key={`${segment.clip_index}-${segment.index}`}
                className="segment"
                data-state={segment.out_of_range ? "attention" : undefined}
              >
                <span className="segment-time">
                  {formatTimestamp(segment.start_seconds)} ~ {formatTimestamp(segment.end_seconds)}
                </span>
                <span className="segment-clip">片段 {clipNumber(segment.clip_index)}</span>
                <span className="segment-content">
                  {segment.content}
                  {segment.tone && <span className="segment-tone">（{segment.tone}）</span>}
                  {segment.out_of_range && (
                    <span className="clip-error">时间越出该片段范围，未做裁切</span>
                  )}
                </span>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

/** 片段明细：每一片的识别状态与失败原因，失败片可单独重试。按组分页，每次不超过一组。 */
function ClipUnderstandingTable({
  clips,
  busyIndex,
  onRetry,
}: {
  clips: TaskClip[];
  /** 正在重试的片段序号，用于禁用按钮并给出反馈 */
  busyIndex: number | null;
  onRetry: (index: number) => void;
}) {
  const groups = useGroups(clips.length);
  const visible = clips.slice(groups.start, groups.end);

  return (
    <div>
      <GroupPager
        label="片段识别明细分组"
        unit="片"
        total={clips.length}
        page={groups.page}
        pageCount={groups.pageCount}
        start={groups.start}
        end={groups.end}
        onPage={groups.setPage}
      />

      <div className="table-wrap">
        <table className="table clips clips--understanding">
          <caption>片段识别明细（按原视频时间顺序）</caption>
          <thead>
            <tr>
              <th scope="col" className="cell-index">
                序号
              </th>
              <th scope="col">原视频时间范围</th>
              <th scope="col">识别状态</th>
              <th scope="col">语音条数</th>
              <th scope="col">请求次数</th>
              <th scope="col">完成时间</th>
              <th scope="col" className="cell-action">
                <span className="visually-hidden">操作</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {visible.map((clip) => {
              const status = clip.understanding_status ?? "pending";
              const warnings = clip.understanding_warnings ?? [];
              const failed = status === "failed";
              // 可重试的前提是「片段已入库」；识别失败恰恰是最需要重试的情况，不能禁用
              const retryable = clip.status === "uploaded";
              const busy = status === "running";
              const tone = understandingStatusTone(status);
              return (
                <tr key={clip.index} data-state={failed ? "attention" : undefined}>
                  <td className="cell-num cell-index">{clipNumber(clip.index)}</td>
                  <td className="cell-time">
                    {formatClipRange(clip.start_seconds, clip.end_seconds)}
                  </td>
                  <td>
                    <span className="cell-status" data-tone={tone}>
                      {tone === "ok" ? <IconCheckCircle size={16} /> : <IconAlert size={16} />}
                      {understandingStatusLabel(status)}
                    </span>
                    {clip.understanding_error && (
                      <span className="clip-error">{clip.understanding_error}</span>
                    )}
                    {warnings.length > 0 && (
                      <span className="clip-error">
                        {warnings.length} 条记录被丢弃：{warnings[0]}
                      </span>
                    )}
                  </td>
                  <td className="cell-num">{clip.understanding_segment_count ?? 0}</td>
                  <td className="cell-num">{clip.understanding_attempts || PLACEHOLDER}</td>
                  <td className="cell-time">{formatMoment(clip.understanding_at ?? null)}</td>
                  <td className="cell-action">
                    <button
                      type="button"
                      className="btn btn--flat"
                      disabled={!retryable || busy || busyIndex !== null}
                      onClick={() => onRetry(clip.index)}
                      title={
                        !retryable
                          ? "该片段尚未成功入库，无法识别"
                          : busy
                            ? "该片段正在识别中"
                            : failed
                              ? "重新识别这一片（只重发这一片）"
                              : "重新识别这一片"
                      }
                    >
                      {busyIndex === clip.index ? "识别中…" : "重新识别"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export type UnderstandingActions = {
  onStart: () => void;
  onRetryClip: (index: number) => void;
  starting: boolean;
  retryingIndex: number | null;
};

export default function UnderstandingBlock({
  task,
  actions,
  refreshKey,
}: {
  task: Task;
  actions: UnderstandingActions;
  refreshKey: number;
}) {
  // 后端未返回识别字段时按「未开始」处理：老版本记录与部分构造的响应都不会让这一块崩掉
  const understanding = task.understanding ?? null;
  const clips = task.clips ?? [];
  const hasClips = clips.length > 0;
  const running = understanding?.status === "running" || actions.starting;
  // 全文那一块只在「确实识别出过内容」时出现：还没识别时摆一个空全文块，
  // 会让人以为「识别过了但什么也没识别出来」，这两件事必须分开
  const hasTranscript = (understanding?.clip_count ?? 0) > 0;
  // 全部成功时按钮只用于「重新汇总」，仍允许点击，但不该显示成一件待办
  const allDone =
    understanding !== null &&
    understanding.status === "succeeded" &&
    understanding.failed_clip_count === 0;

  return (
    <section className="panel" aria-label="识别结果">
      <div className="panel-head">
        <IconVoice size={18} />
        <h3 className="panel-title">识别结果</h3>
        {understanding && (
          <span className="panel-head-side">
            <span className="status" data-tone={understandingStatusTone(understanding.status)}>
              {understandingStatusLabel(understanding.status)}
            </span>
          </span>
        )}
      </div>

      {understanding && <Summary understanding={understanding} />}

      {understanding?.error && <p className="detail-error mt-md">{understanding.error}</p>}

      <div className="actions mt-lg">
        <button
          type="button"
          className="btn btn--primary"
          onClick={actions.onStart}
          disabled={!hasClips || running || actions.retryingIndex !== null}
          title={hasClips ? undefined : "还没有切片，请先完成切分"}
        >
          {running ? "识别中…" : allDone ? "重新检查识别结果" : "开始识别语音"}
        </button>
        {running && understanding && (
          <span className="hint">
            整场识别进度 {understanding.progress}%，可以关闭页面，稍后回来看
          </span>
        )}
        {!hasClips && <span className="hint">该任务尚无切片，切分完成后才能识别</span>}
      </div>

      {/* 全文排在明细之前：内容是这一页的主体，状态明细是核对用的附栏 */}
      {hasTranscript && (
        <div className="subsection">
          <h4 className="subsection-title">
            识别全文
            <span className="subsection-count num">{understanding?.segment_count ?? 0} 条</span>
            <span className="subsection-note">等宽纯文本全文请用右侧「下载 txt」</span>
          </h4>
          <TranscriptPanel taskId={task.id} refreshKey={refreshKey} disabled={Boolean(running)} />
        </div>
      )}

      {hasClips && (
        <div className="subsection">
          <h4 className="subsection-title">
            片段识别明细
            <span className="subsection-count num">{clips.length}</span>
          </h4>
          <ClipUnderstandingTable
            clips={clips}
            busyIndex={actions.retryingIndex}
            onRetry={actions.onRetryClip}
          />
        </div>
      )}
    </section>
  );
}
