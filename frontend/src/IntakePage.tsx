/**
 * 新建分析任务页：选文件、看已选文件、启动上传，并说明后台会做什么。
 *
 * 这一页只负责「把文件交出去」这件事的界面。真正的上传状态机（查断点、补分片、合并入库、
 * 轮询任务）都在 `Workbench` 里，本组件通过 props 接收状态与动作——界面重构不该改变那段
 * 逻辑，因此这里一行处理逻辑都没有。
 *
 * 布局意图：上传区是页面视觉中心，右侧一栏回答「传完之后系统到底在做什么」。
 */

import type { ChangeEvent, DragEvent } from "react";
import UploadProgress from "./UploadProgress";
import { IconFile, IconInfo, IconUploadCloud } from "./icons";
import { formatBytes } from "./format";

/** 处理说明的四个步骤，与后端处理链路的阶段划分一致。 */
const STEPS = [
  { title: "媒体解析", note: "检测视频格式、分辨率、编码信息" },
  { title: "格式处理", note: "按视频格式做必要处理，输出标准容器" },
  { title: "视频切片", note: "按时间切分视频，生成多个片段" },
  { title: "完整性校验", note: "校验切片完整性，确保数据可用" },
] as const;

export type IntakePhase = "idle" | "loading" | "unfinished" | "uploading" | "assembling" | "error";

/** 处理说明栏：上传区右侧的固定内容，任何时候都在。 */
function ProcessNotes() {
  return (
    <aside className="panel" aria-label="处理说明">
      <div className="panel-head">
        <IconInfo size={18} />
        <h2 className="panel-title">处理说明</h2>
      </div>

      <ol className="steps">
        {STEPS.map((step, index) => (
          <li className="step" key={step.title}>
            <span className="step-no num" aria-hidden="true">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span>
              <span className="step-title">{step.title}</span>
              <span className="step-note">{step.note}</span>
            </span>          </li>
        ))}
      </ol>

      <p className="callout">
        <IconInfo size={16} />
        处理完成后，您可以在任务列表中查看分析结果与复盘报告。
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
                : "支持 MP4、TS 格式，单个文件最大 2 GB。上传完成后将自动进行视频处理与分析。"}
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
            <span className="dropzone-note">MP4 / TS · 最大 2 GB</span>
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

        <ProcessNotes />
      </div>
    </div>
  );
}
