/**
 * 新建分析任务页：选文件、看已选文件、启动上传，并说明后台会做什么。
 *
 * 这一页只负责「把文件交出去」这件事的界面。真正的上传状态机（查断点、补分片、合并入库、
 * 轮询任务）都在 `Workbench` 里，本组件通过 props 接收状态与动作——界面重构不该改变那段
 * 逻辑，因此这里一行处理逻辑都没有。
 *
 * 布局意图：上传区是页面视觉中心，右侧一栏给第一次用的人一条从上传到拿到结论的操作路径。
 */

import type { ChangeEvent, DragEvent } from "react";
import UploadProgress from "./UploadProgress";
import { IconFile, IconInfo, IconUploadCloud } from "./icons";
import { formatBytes } from "./format";

/**
 * 使用说明的五个步骤：从上传录屏到拿到复盘报告的完整操作路径。
 *
 * 按钮名与页面名照抄界面文案，用户照着点即可。刻意不写后台做了什么——那些环节由系统
 * 自动完成，用户既看不到也影响不了，摆在这里只会占掉首屏最显眼的一栏。
 */
const GUIDE_STEPS = [
  { title: "上传录屏", note: "支持 MP4 / TS，建议单个文件不超过 2 GB，更大的文件处理时间会明显变长" },
  { title: "等待视频处理", note: "系统自动解析与切片，完成后进入任务详情" },
  { title: "开始内容识别", note: "在「内容理解」页点「开始识别语音」" },
  { title: "开始复盘分析", note: "在「复盘分析」页点「开始复盘分析」" },
  { title: "查看与下载结论", note: "点「查看报告」下载 Markdown 复盘报告" },
] as const;

export type IntakePhase = "idle" | "loading" | "unfinished" | "uploading" | "assembling" | "error";

/** 使用说明栏：上传区右侧的固定内容，任何时候都在。 */
function UsageGuide() {
  return (
    <aside className="panel" aria-label="使用说明">
      <div className="panel-head">
        <IconInfo size={18} />
        <h2 className="panel-title">使用说明</h2>
      </div>

      <ol className="steps">
        {GUIDE_STEPS.map((step, index) => (
          <li className="step" key={step.title}>
            <span className="step-no num" aria-hidden="true">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span>
              <span className="step-title">{step.title}</span>
              <span className="step-note">{step.note}</span>
            </span>
          </li>
        ))}
      </ol>

      <p className="callout">
        <IconInfo size={16} />
        识别与复盘都要点一下按钮才会开始。视频保留 72 小时，转写与复盘结论会一直保留。
      </p>
    </aside>
  );
}

export default function IntakePage({
  phase,
  fileName,
  totalBytes,
  sentBytes,
  percent,
  error,
  missingChunks,
  dragOver,
  inputId,
  inputRef,
  onPick,
  onFile,
  onDragOver,
  onDragLeave,
  onDrop,
  onCancel,
}: {
  phase: IntakePhase;
  fileName: string | null;
  totalBytes: number;
  sentBytes: number;
  percent: number;
  error: string | null;
  missingChunks: number[];
  dragOver: boolean;
  /** 文件输入框的 id：label 通过它关联，整块上传区都可点击 */
  inputId: string;
  inputRef: React.RefObject<HTMLInputElement>;
  /** 点击「选择文件」按钮：交给同一个文件输入框 */
  onPick: () => void;
  onFile: (event: ChangeEvent<HTMLInputElement>) => void;
  onDragOver: (event: DragEvent<HTMLElement>) => void;
  onDragLeave: (event: DragEvent<HTMLElement>) => void;
  onDrop: (event: DragEvent<HTMLElement>) => void;
  onCancel: () => void;
}) {
  const busy = phase === "uploading" || phase === "assembling";
  const checking = phase === "loading";
  // 断点续传的等待态：文件必须由用户重新选一次，其余阶段用的是本次会话里已选的文件
  const resuming = phase === "unfinished";
  const failed = phase === "error";
  const showFileRow = phase === "idle" && fileName !== null;

  return (
    <div className="page">
      <div>
        <h2 className="page-title">新建分析任务</h2>
      </div>

      <div className="intake-grid">
        <section className="panel" aria-label="上传直播录屏">
          <div className="panel-head">
            <IconUploadCloud size={18} />
            <h3 className="panel-title">
              {failed
                ? "上传未完成"
                : resuming
                  ? "这次上传还没传完"
                  : "上传一场直播录屏"}
            </h3>
          </div>

          <p className="intake-lead">
            {failed
              ? "本次上传没有提交成功。已传完的分片会留在服务端，重新选择同一个文件可继续补齐。"
              : resuming
                ? "已传的分片留在服务端，重新选择同一个文件只会补传缺的部分，不会从头再传一遍。"
                : "支持 MP4、TS 格式，建议单个文件不超过 2 GB。更大的文件同样可以处理，只是上传与处理耗时会更长，上传完成后将自动进行视频处理与分析。"}
          </p>

          {/* 文件输入常驻：进度阶段不能换文件，等待与空闲阶段可以。
              整块上传区是可点的，点击时直接开文件选择框 */}
          <input
            ref={inputRef}
            id={inputId}
            className="visually-hidden"
            type="file"
            accept="video/*,.ts,.mp4"
            onChange={onFile}
            disabled={busy}
            aria-label={resuming ? "选择同一个文件继续上传" : "选择录屏文件"}
          />

          <button
            type="button"
            className="dropzone"
            data-over={dragOver ? "true" : undefined}
            onDragOver={onDragOver}
            onDragLeave={onDragLeave}
            onDrop={onDrop}
            onClick={onPick}
            disabled={busy}
          >
            <span className="dropzone-mark" aria-hidden="true">
              <IconUploadCloud size={20} />
            </span>
            <span className="dropzone-main">
              {resuming ? (
                "拖拽同一个文件到此处，或"
              ) : (
                <>
                  拖拽文件到此处，或
                  <strong> 点击选择文件</strong>
                </>
              )}
            </span>
            <span className="dropzone-note">MP4 / TS · 建议 2 GB 以内</span>
            <span className="btn btn--primary dropzone-action" aria-hidden="true">
              {resuming ? "选择同一个文件" : "选择文件"}
            </span>
          </button>

          {checking && (
            <p className="hint mt-md">
              正在检查有没有没传完的上传…
            </p>
          )}

          {resuming && fileName !== null && (
            <div className="selected-file">
              <p className="selected-file-title">待续传的文件</p>
              <div className="file-row">
                <span className="file-badge" aria-hidden="true">
                  <IconFile size={18} />
                </span>
                <span className="file-text">
                  <span className="file-name">{fileName}</span>
                  <span className="file-meta num">
                    已传 {formatBytes(sentBytes)} / {formatBytes(totalBytes)}
                  </span>
                </span>
                <span className="file-row-end">
                  <span className="status" data-tone="warn">
                    未传完
                  </span>
                </span>
              </div>
            </div>
          )}

          {showFileRow && fileName !== null && (
            <div className="selected-file">
              <p className="selected-file-title">已选择文件</p>
              <div className="file-row">
                <span className="file-badge" aria-hidden="true">
                  <IconFile size={18} />
                </span>
                <span className="file-text">
                  <span className="file-name">{fileName}</span>
                  <span className="file-meta num">{formatBytes(totalBytes)}</span>
                </span>
                <span className="file-row-end">
                  <span className="status" data-tone="ok">
                    已选择
                  </span>
                </span>
              </div>
            </div>
          )}

          {busy && (
            <UploadProgress
              percent={percent}
              sentBytes={sentBytes}
              totalBytes={totalBytes}
              assembling={phase === "assembling"}
            />
          )}

          {error !== null && <p className="detail-error mt-md">{error}</p>}

          {failed && missingChunks.length > 0 && (
            <p className="hint mt-md">
              缺少 {missingChunks.length} 个分片。重新选择同一文件可继续补齐，已上传的分片不会重传。
            </p>
          )}

          <div className="actions mt-lg">
            <button
              type="button"
              className="btn btn--primary"
              onClick={onPick}
              disabled={busy || checking}
            >
              <IconUploadCloud size={16} />
              {busy ? "上传中…" : failed ? "重新发起上传" : resuming ? "继续上传" : "开始处理"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={onCancel}
              disabled={busy}
            >
              取消
            </button>
          </div>
        </section>

        <UsageGuide />
      </div>
    </div>
  );
}
