/**
 * 任务详情壳层：任务身份 + 子导航 + 当前子页。
 *
 * V0.4.1 把原本一屏到底的详情页拆成三个子页（视频信息 / 内容理解 / 复盘分析），
 * 壳层负责三件不随子页变化的事：
 *
 * 1. **任务身份常驻**——文件名、状态、时间/体积/分辨率/编码、操作入口（重新执行、查看报告、
 *    删除）在任何子页都看得见且可操作。越往下走越需要「我正在看哪一场」这个锚点。
 * 2. **子导航三项常显**——未达条件的子页置灰并写明原因（如「视频处理完成后开放」），
 *    用户因此能看到完整链路与下一步要做什么，而不是面对一个突然消失的入口。
 * 3. **默认落在最靠后的可进子页**——从侧栏打开一条已复盘的任务，直接停在复盘结论，
 *    少两次点击；每条任务只判定一次，之后不因轮询结果变化而跳页。
 *
 * 子页拆分只动呈现层：切分、识别、复盘的判定与轮询逻辑（`STAGES`、`stageState`、
 * `useUnderstanding`、`useReview`）一概未改。
 */

import { useEffect, useRef } from "react";
import type { Task, TaskMetadata } from "./api";
import UnderstandingBlock, { type UnderstandingActions } from "./Understanding";
import ReviewBlock, { type ReviewActions } from "./Review";
import VideoInfoPage from "./VideoInfoPage";
import {
  SECTION_ORDER,
  deepestOpenSection,
  sectionBlockedReason,
  sectionTitle,
} from "./TaskSections";
import { taskSectionHref, type TaskSection } from "./routing";
import { IconArrowLeft, IconVideo } from "./icons";
import {
  formatBytes,
  formatMoment,
  formatResolution,
  taskStatusLabel,
  taskStatusTone,
} from "./format";

/** 缩略区：后端不提供视频缩略图，用与实际分辨率同比例的占位块表达「这是一条竖屏录屏」。 */
function Thumbnail({ metadata }: { metadata: TaskMetadata | null }) {
  // 无探测结果时按 9:16 兜底——本产品的素材是竖屏直播录屏
  const ratio =
    metadata?.width && metadata?.height && metadata.height > 0
      ? metadata.width / metadata.height
      : 9 / 16;

  return (
    <div className="thumb" aria-hidden="true">
      <span className="thumb-frame" style={{ aspectRatio: `${ratio}` }}>
        <IconVideo size={20} />
      </span>
    </div>
  );
}

export type TaskActions = {
  onRetry: () => void;
  onDelete: () => void;
  onReset: () => void;
  deleting: boolean;
  onDownloadReport: () => void;
  /** 是否有可下载的复盘报告 */
  hasReport: boolean;
};

export default function TaskDetail({
  task,
  actions,
  understanding,
  review,
  section,
  onSection,
  taskLoaded,
  sectionLinks = true,
  refreshKey = 0,
  notice = null,
  onTask,
}: {
  task: Task;
  actions: TaskActions;
  /** 识别相关操作：内容理解页的按钮与轮询都从这里来。 */
  understanding: UnderstandingActions;
  /** 复盘相关操作：复盘分析页的按钮与轮询都从这里来。 */
  review: ReviewActions;
  /** 当前子页。路由未点名时为默认值，任务读回后由 `onSection` 改写成按进度选中的那一页。 */
  section: TaskSection;
  /**
   * 切换子页。已认领的任务（地址是 `#/tasks/:id`）由外层 hash 路由承担，
   * 刷新与前进/后退因此在子页之间也成立；本次上传还没认领地址时（仍在 `#/new`）
   * 由 `Workbench` 用本地状态顶着，此时点得动但刷新会回到新建页——如实反映地址。
   */
  onSection: (section: TaskSection) => void;
  /** 子导航项是否渲染成真链接（地址已认领到这条任务）。 */
  sectionLinks?: boolean;
  /**
   * 任务详情是否已读回。上传完成后挂载的那一帧 `task` 还在路上，此时不足以判定默认子页，
   * 因此默认子页的改写要等它变 true 再执行。
   */
  taskLoaded: boolean;
  /** 识别状态变化时递增，用于让全文面板重新拉取。 */
  refreshKey?: number;
  /** 操作层面的失败（如删除未成功）：与任务自身的失败原因分开呈现。 */
  notice?: string | null;
  /** 复盘轮询拿到新状态时回写：复盘结论由任务详情承载。 */
  onTask: (task: Task) => void;
}) {
  const metadata = task.metadata;
  const blockedReason = sectionBlockedReason(task, section);
  const fallbackSection = deepestOpenSection(task);
  /**
   * 把「用户没点名子页」的默认值改写成「按处理进度最靠后的可进子页」：
   * 从侧栏打开一条已复盘的任务，应该直接停在复盘结论，而不是从视频信息再点两次。
   *
   * 三条约束：
   * - **每条任务只判定一次**。之后轮询把任务推进到下一阶段也不替用户翻页——用户自己点了
   *   哪页就停哪页。因此记的是「为哪条任务判定过了」，而不是一个布尔量。
   * - **要等这一条任务真的读回来**。上传完成后挂载的那一帧任务还在路上，此时算出来的
   *   「最靠后可进子页」必然是默认页，据此改写会把随后该落的页又拽回顶部。
   * - **改写走 hash 路由**：已认领地址时刷新与前进/后退在子页之间同样成立。
   */
  const resolvedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!taskLoaded || resolvedFor.current === task.id) return;
    resolvedFor.current = task.id;
    if (blockedReason !== null || fallbackSection !== section) onSection(fallbackSection);
  }, [taskLoaded, task.id, blockedReason, fallbackSection, section, onSection]);

  return (
    <div className="page--data">
      <button type="button" className="back-link" onClick={actions.onReset}>
        <IconArrowLeft size={16} />
        返回任务列表
      </button>

      <header className="detail-head">
        <Thumbnail metadata={metadata} />

        <div className="detail-head-main">
          <div className="detail-title-row">
            <h2 className="detail-title">{task.filename ?? "未命名录屏"}</h2>
            <span className="status" data-tone={taskStatusTone(task.status)}>
              {taskStatusLabel(task.status)}
            </span>
          </div>

          <p className="detail-sub">营销直播录屏分析</p>

          <div className="detail-facts">
            <span className="detail-fact">
              <span className="detail-fact-value num">{formatMoment(task.created_at)}</span>
            </span>
            {task.size !== null && (
              <span className="detail-fact num">{formatBytes(task.size)}</span>
            )}
            {metadata && (
              <span className="detail-fact num">
                {formatResolution(metadata.width, metadata.height)}
              </span>
            )}
            {metadata && (metadata.video_codec || metadata.audio_codec) && (
              <span className="detail-fact num">
                {[metadata.video_codec, metadata.audio_codec].filter(Boolean).join(" / ")}
              </span>
            )}
          </div>
        </div>

        <div className="detail-actions">
          <button type="button" className="btn" onClick={actions.onRetry}>
            重新执行任务
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={actions.onDownloadReport}
            disabled={!actions.hasReport}
            title={actions.hasReport ? "下载 Markdown 复盘报告" : "本任务还没有复盘结论"}
          >
            查看报告
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={actions.onDelete}
            disabled={actions.deleting}
          >
            {actions.deleting ? "删除中…" : "删除视频"}
          </button>
        </div>
      </header>

      <nav className="subnav" aria-label="任务内容分页">
        {SECTION_ORDER.map((item) => {
          const reason = sectionBlockedReason(task, item);
          const locked = reason !== null;
          return (
            <a
              key={item}
              className="subnav-item"
              // 地址未认领时不写 href：此时点了只在本地切页，不该留下一个切不动的地址。
              // 但没有 href 的 <a> 会丢掉隐式的 link 语义（也是读屏软件识别它的依据），
              // 因此显式补上 role
              role="link"
              href={sectionLinks ? taskSectionHref(task.id, item) : undefined}
              aria-current={item === section ? "page" : undefined}
              aria-disabled={locked ? "true" : undefined}
              title={reason ?? undefined}
              onClick={(event) => {
                event.preventDefault();
                onSection(item);
              }}
            >
              {sectionTitle(item)}
              {locked && <span className="subnav-lock">{reason}</span>}
            </a>
          );
        })}
      </nav>

      {blockedReason === null ? (
        <>
          {section === "media" && <VideoInfoPage task={task} />}
          {section === "understanding" && (
            <UnderstandingBlock task={task} actions={understanding} refreshKey={refreshKey} />
          )}
          {section === "review" && <ReviewBlock task={task} actions={review} onTask={onTask} />}
        </>
      ) : (
        <section className="panel" aria-label="子页尚未开放">
          <div className="panel-head">
            <IconVideo size={18} />
            <h3 className="panel-title">{sectionTitle(section)}</h3>
            <span className="panel-head-side">
              <span className="status" data-tone="warn">
                暂不可用
              </span>
            </span>
          </div>
          <p className="hint">{blockedReason}。</p>
          <div className="actions mt-lg">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => onSection(fallbackSection)}
            >
              回到{sectionTitle(fallbackSection)}
            </button>
          </div>
        </section>
      )}

      {task.error && <p className="detail-error">失败原因：{task.error}</p>}
      {notice && <p className="detail-error">{notice}</p>}
    </div>
  );
}
