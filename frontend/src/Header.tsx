/**
 * 页眉：品牌、全局搜索、新建任务与账号入口。
 *
 * 白底、高度固定，与主体之间只有一条极浅的分隔线——页眉是工具条，不是主视觉。
 *
 * 搜索框的现状必须说清楚：后端没有搜索接口，它过滤的是侧栏「最近任务」这一个列表
 * （`GET /api/tasks?limit=20`），因此文案写的是「筛选最近任务」而不是「搜索全部任务」。
 * 等后端提供检索能力时，这里换成真正的查询即可，界面承诺不需要改。
 *
 * 用户登录是预留功能区，只承载界面状态，后端尚未提供账号体系，界面明确标注「未接入」，
 * 不做假的登录成功反馈。
 */

import { useEffect, useRef, useState } from "react";
import { IconChevronDown, IconPlus, IconSearch, IconUser } from "./icons";

export type SessionUser = { name: string; role: string } | null;

export default function Header({
  user,
  query,
  onQuery,
  onNewTask,
  onSignIn,
  onSignOut,
}: {
  user: SessionUser;
  /** 侧栏「最近任务」的筛选词 */
  query: string;
  onQuery: (value: string) => void;
  onNewTask: () => void;
  onSignIn: () => void;
  onSignOut: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const accountRef = useRef<HTMLDivElement>(null);

  // 点击别处或按 Esc 收起账号菜单；菜单只是快捷方式，不做焦点陷阱
  useEffect(() => {
    if (!menuOpen) return;
    const onPointer = (event: MouseEvent) => {
      if (!accountRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("mousedown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <header className="masthead">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width={14} height={14} aria-hidden="true">
            <path d="M9 6.5l8.5 5.5L9 17.5z" fill="currentColor" />
          </svg>
        </span>
        <h1 className="brand-name">LiveReview</h1>
        <span className="brand-scope">直播复盘工作台</span>
      </div>

      <div className="masthead-search">
        <span className="masthead-search-icon" aria-hidden="true">
          <IconSearch size={16} />
        </span>
        <input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="筛选最近任务：文件名、店铺等…"
          aria-label="筛选最近任务"
        />
      </div>

      <div className="masthead-actions">
        <button type="button" className="btn btn--primary" onClick={onNewTask}>
          <IconPlus size={16} />
          新建任务
        </button>

        {user ? (
          <div className="account" ref={accountRef}>
            <button
              type="button"
              className="account-trigger"
              onClick={() => setMenuOpen((open) => !open)}
              aria-expanded={menuOpen}
              aria-haspopup="menu"
            >
              <span className="avatar" aria-hidden="true">
                {user.name.slice(0, 1)}
              </span>
              <span className="account-name">{user.name}</span>
              <IconChevronDown size={16} />
            </button>

            {menuOpen && (
              <div className="account-menu" role="menu">
                <p className="account-menu-role">{user.role}</p>
                <button
                  type="button"
                  className="account-menu-item"
                  role="menuitem"
                  onClick={() => {
                    setMenuOpen(false);
                    onSignOut();
                  }}
                >
                  退出登录
                </button>
              </div>
            )}
          </div>
        ) : (
          <button type="button" className="btn" onClick={onSignIn}>
            <IconUser size={16} />
            用户登录
          </button>
        )}
      </div>
    </header>
  );
}
