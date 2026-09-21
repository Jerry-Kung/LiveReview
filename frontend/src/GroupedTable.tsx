/**
 * 分组呈现：把长列表折成每组固定的若干条。
 *
 * 场景是「几十片切片」与「几十片识别明细」——它们的价值在于逐条核对，而不是一次性铺开，
 * 一次性铺开只会把页面拉得极长、把后面的结论挤到屏幕之外。因此这里不做虚拟滚动，
 * 只做最朴素的分组翻页：任何时刻渲染的条目不超过 `PAGE_SIZE` 条。
 *
 * 组件本身不管数据形态，只交出「当前该显示哪一段」：调用方用 `page` 去切自己的数组。
 * 切片表与识别明细各自持有一个 `useGroups()`，互不影响。
 */

import { useCallback, useEffect, useState } from "react";

/** 每组条数。用户要求「每组不超过十个」，这里固定为 10。 */
export const GROUP_SIZE = 10;

/** 页码状态：只暴露「第几组」与总数，翻组动作在此收口。 */
export function useGroups(total: number): {
  page: number;
  pageCount: number;
  start: number;
  end: number;
  setPage: (next: number) => void;
} {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(total / GROUP_SIZE));

  // 条数变少（如重试后失败片段被合并、或换了任务）时把页码收回范围内，
  // 否则会停在空组上——空表格看起来像「数据没了」
  useEffect(() => {
    setPage((prev) => (prev > pageCount - 1 ? pageCount - 1 : prev));
  }, [pageCount]);

  const start = page * GROUP_SIZE;
  const end = Math.min(start + GROUP_SIZE, total);

  return {
    page,
    pageCount,
    start,
    end,
    setPage: useCallback(
      (next: number) => setPage(Math.min(Math.max(next, 0), pageCount - 1)),
      [pageCount]
    ),
  };
}

/** 分组器：第 X–Y 条 / 共 N 条，加首末与相邻组。 */
export default function GroupPager({
  page,
  pageCount,
  start,
  end,
  total,
  unit = "条",
  label,
  onPage,
}: {
  page: number;
  pageCount: number;
  /** 当前组的起止（从 0 数的下标），用于显示区间 */
  start: number;
  end: number;
  total: number;
  /** 计数单位：切片用「片」，语音条目用「条」 */
  unit?: string;
  /** 这个分组器管的是什么，用于无障碍名称 */
  label: string;
  onPage: (page: number) => void;
}) {
  if (total === 0) return null;

  const first = pageCount === 1;

  return (
    <div className="grouper" role="group" aria-label={label}>
      <span className="grouper-range num">
        第 {start + 1}–{end} {unit} / 共 {total} {unit}
      </span>
      <span className="grouper-page num">
        第 {page + 1} / {pageCount} 组
      </span>
      <span className="grouper-actions">
        <button
          type="button"
          className="btn btn--flat"
          onClick={() => onPage(0)}
          disabled={first || page === 0}
        >
          首组
        </button>
        <button
          type="button"
          className="btn btn--flat"
          onClick={() => onPage(page - 1)}
          disabled={page === 0}
        >
          上一组
        </button>
        <button
          type="button"
          className="btn btn--flat"
          onClick={() => onPage(page + 1)}
          disabled={page >= pageCount - 1}
        >
          下一组
        </button>
        <button
          type="button"
          className="btn btn--flat"
          onClick={() => onPage(pageCount - 1)}
          disabled={first || page >= pageCount - 1}
        >
          末组
        </button>
      </span>
    </div>
  );
}
